"""task_db_maintenance must not fill the disk: VACUUM is skipped when free space is
under twice the DB size, and the bulk DELETE runs with secure_delete off (the VPS
SQLite is compiled with SECURE_DELETE, which journals every freed page) — 2026-10-19."""
import shutil
import sqlite3
from collections import namedtuple

import services.scheduler as sched
from db import connect as db_connect

Usage = namedtuple("Usage", "total used free")


def _seed(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE forecast_log (timestamp TEXT, payload TEXT)")
    con.executemany("INSERT INTO forecast_log VALUES (?, ?)",
                    [("2000-01-01T00:00:00+00:00", "x" * 2000)] * 500)
    con.commit()
    con.close()


def _freelist(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("PRAGMA freelist_count").fetchone()[0]
    finally:
        con.close()


def _point_at(monkeypatch, tmp_path, free_bytes, statements=None):
    db = tmp_path / "shadow_trades.db"
    _seed(db)
    monkeypatch.setattr(sched, "DB_PATH", db)

    def traced(*a, **k):
        con = db_connect(*a, **k)
        if statements is not None:
            con.set_trace_callback(statements.append)
        return con

    monkeypatch.setattr(sched, "db_connect", traced)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(10**12, 0, free_bytes))
    return db


def test_vacuum_skipped_when_free_space_under_twice_db_size(monkeypatch, tmp_path):
    db = _point_at(monkeypatch, tmp_path, free_bytes=1000)
    sched.task_db_maintenance()
    assert _freelist(db) > 0          # rows deleted, file not rewritten


def test_vacuum_runs_when_space_is_ample(monkeypatch, tmp_path):
    db = _point_at(monkeypatch, tmp_path, free_bytes=10**12)
    sched.task_db_maintenance()
    assert _freelist(db) == 0


def test_bulk_delete_runs_with_secure_delete_off(monkeypatch, tmp_path):
    statements = []
    _point_at(monkeypatch, tmp_path, free_bytes=10**12, statements=statements)
    sched.task_db_maintenance()
    norm = [s.strip().upper().replace(" ", "") for s in statements]
    delete_at = next(i for i, s in enumerate(norm) if s.startswith("DELETEFROMFORECAST_LOG"))
    assert "PRAGMASECURE_DELETE=OFF" in norm[:delete_at]

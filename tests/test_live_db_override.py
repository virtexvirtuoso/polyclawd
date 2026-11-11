"""POLYCLAWD_LIVE_DB override for execution.live_db.connect (plan task 1.0c, part 2).

No test here opens the default (production) path: the default is asserted on the
resolver function only.
"""

from pathlib import Path

import pytest

from execution import live_db


def _main_db_file(conn) -> str:
    return conn.execute("PRAGMA database_list").fetchone()[2]


def test_connect_without_path_uses_override(tmp_path, monkeypatch):
    target = tmp_path / "override.db"
    monkeypatch.setenv("POLYCLAWD_LIVE_DB", str(target))
    default_existed = live_db.DB_PATH.exists()
    conn = live_db.connect()
    try:
        assert Path(_main_db_file(conn)).resolve() == target.resolve()
        # production DDL ran against the override file
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "live_positions" in tables
    finally:
        conn.close()
    assert live_db.DB_PATH.exists() == default_existed


def test_resolver_without_variable_is_default_path(monkeypatch):
    monkeypatch.delenv("POLYCLAWD_LIVE_DB", raising=False)
    assert live_db.resolve_db_path() == live_db.DB_PATH


@pytest.mark.parametrize("empty", ["", "   "])
def test_resolver_ignores_empty_variable(monkeypatch, empty):
    monkeypatch.setenv("POLYCLAWD_LIVE_DB", empty)
    assert live_db.resolve_db_path() == live_db.DB_PATH


def test_resolver_uses_variable(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYCLAWD_LIVE_DB", str(tmp_path / "x.db"))
    assert live_db.resolve_db_path() == tmp_path / "x.db"


def test_explicit_path_ignores_variable(tmp_path, monkeypatch):
    other = tmp_path / "from-env.db"
    explicit = tmp_path / "explicit.db"
    monkeypatch.setenv("POLYCLAWD_LIVE_DB", str(other))
    assert live_db.resolve_db_path(explicit) == explicit
    conn = live_db.connect(explicit)
    try:
        assert Path(_main_db_file(conn)).resolve() == explicit.resolve()
    finally:
        conn.close()
    assert not other.exists()

"""Task 2.1 Steps 2+3 — stop-evaluator silence alarm (scheduler) + daily
paper-P&L proof-of-life lines (stops heartbeat + delivery success rate)."""
import json
import sqlite3
import time

import pytest

import services.scheduler as sched
import scripts.paper_pnl_report as ppr

NOW = 1_800_000_000


# ── helpers ───────────────────────────────────────────────────────────────────

def _heartbeat_db(tmp_path, ts=None, checked=3, warned=1):
    db = tmp_path / "shadow.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS stop_heartbeat ("
        "id INTEGER PRIMARY KEY, ts INTEGER, "
        "positions_checked INTEGER, warnings_fired INTEGER)")
    if ts is not None:
        conn.execute(
            "INSERT OR REPLACE INTO stop_heartbeat "
            "(id, ts, positions_checked, warnings_fired) VALUES (1, ?, ?, ?)",
            (int(ts), checked, warned))
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake_send(msg, **kw):
        calls.append((msg, kw))
        return True

    monkeypatch.setattr("scripts.openclaw_alerts.alert_openclaw", fake_send)
    return calls


# ── silence alarm ─────────────────────────────────────────────────────────────

def test_healthy_heartbeat_is_silent(tmp_path, sent):
    db = _heartbeat_db(tmp_path, ts=NOW - 60)
    sched.task_stop_silence_alarm(db_path=db, now=NOW)
    assert sent == []


def test_silent_over_30min_fires_plain_text_alarm(tmp_path, sent):
    db = _heartbeat_db(tmp_path, ts=NOW - 45 * 60)
    sched.task_stop_silence_alarm(db_path=db, now=NOW)
    assert len(sent) == 1
    msg, kw = sent[0]
    assert "SILENT" in msg and "45m" in msg
    assert kw.get("parse_mode") is None


def test_refire_gated_to_6h(tmp_path, sent):
    db = _heartbeat_db(tmp_path, ts=NOW - 45 * 60)
    sched.task_stop_silence_alarm(db_path=db, now=NOW)
    sched.task_stop_silence_alarm(db_path=db, now=NOW + 30 * 60)   # within gate
    assert len(sent) == 1
    sched.task_stop_silence_alarm(db_path=db, now=NOW + 7 * 3600)  # gate expired
    assert len(sent) == 2


def test_failed_send_does_not_arm_gate(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "scripts.openclaw_alerts.alert_openclaw",
        lambda msg, **kw: calls.append(msg) or False)
    db = _heartbeat_db(tmp_path, ts=NOW - 45 * 60)
    sched.task_stop_silence_alarm(db_path=db, now=NOW)
    sched.task_stop_silence_alarm(db_path=db, now=NOW + 60)  # retry: gate not set
    assert len(calls) == 2


def test_missing_table_and_row_are_silent(tmp_path, sent):
    empty = tmp_path / "empty.db"
    sqlite3.connect(str(empty)).close()                 # no tables at all
    sched.task_stop_silence_alarm(db_path=empty, now=NOW)
    norow = _heartbeat_db(tmp_path, ts=None)            # table, no row
    sched.task_stop_silence_alarm(db_path=norow, now=NOW)
    assert sent == []


def test_registered_in_30min_tick():
    assert "stop_silence_alarm" in sched.TICK_TASKS["30min"]
    assert callable(sched._task_fn("stop_silence_alarm"))


# ── daily P&L proof-of-life lines ────────────────────────────────────────────

def test_stops_line_reads_heartbeat(tmp_path):
    db = _heartbeat_db(tmp_path, ts=NOW - 10 * 60, checked=7, warned=2)
    line = ppr.stops_proof_line(db_path=db, now=NOW)
    assert line.startswith("stops:")
    assert "7" in line and "2" in line and "10m" in line


def test_stops_line_degrades_without_heartbeat(tmp_path):
    empty = tmp_path / "empty.db"
    sqlite3.connect(str(empty)).close()
    line = ppr.stops_proof_line(db_path=empty, now=NOW)
    assert line.startswith("stops:") and "no heartbeat" in line


def test_delivery_line_computes_24h_success_rate(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    from datetime import datetime, timezone, timedelta
    now_dt = datetime.fromtimestamp(NOW, tz=timezone.utc)
    rows = [
        {"ts": (now_dt - timedelta(hours=1)).isoformat(), "ok": True},
        {"ts": (now_dt - timedelta(hours=2)).isoformat(), "ok": True},
        {"ts": (now_dt - timedelta(hours=3)).isoformat(), "ok": True},
        {"ts": (now_dt - timedelta(hours=4)).isoformat(), "ok": False},
        {"ts": (now_dt - timedelta(hours=48)).isoformat(), "ok": False},  # outside window
    ]
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    line = ppr.delivery_line(ledger_path=ledger, now=NOW)
    assert line.startswith("delivery:")
    assert "75%" in line and "3/4" in line


def test_delivery_line_degrades_without_ledger(tmp_path):
    line = ppr.delivery_line(ledger_path=tmp_path / "missing.jsonl", now=NOW)
    assert line.startswith("delivery:")

#!/usr/bin/env python3
"""Tests for scripts/send_ledger_watchdog.py --min-rate hourly mode (Task 5.4).

Alarm fires ONLY when failure rate >= --min-rate AND failures >= 3.
Forged ledger lines in a temp file via POLYCLAWD_LEDGER_PATH; sends monkeypatched.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.send_ledger_watchdog as wd
import scripts.openclaw_alerts as oa


def forge_ledger(path: Path, n_ok: int, n_fail: int, minutes_ago: float = 5.0):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    lines = []
    for _ in range(n_ok):
        lines.append(
            {"ts": ts, "caller": "scheduler", "channel": "telegram", "ok": True, "parse_mode": None, "len": 42}
        )
    for _ in range(n_fail):
        lines.append(
            {
                "ts": ts,
                "caller": "whale_alert_drain.py",
                "channel": "telegram",
                "ok": False,
                "parse_mode": None,
                "len": 42,
                "err": "http_502:bad gateway",
            }
        )
    path.write_text("\n".join(json.dumps(r) for r in lines) + "\n")


@pytest.fixture
def run(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("POLYCLAWD_LEDGER_PATH", str(ledger))
    sent = []
    monkeypatch.setattr(oa, "alert_openclaw", lambda msg, **kw: (sent.append({"msg": msg, **kw}), True)[1])

    def _run(argv):
        monkeypatch.setattr(sys, "argv", ["send_ledger_watchdog.py"] + argv)
        wd.main()
        return sent

    return ledger, _run


def test_alarm_fires_at_rate_and_count(run):
    ledger, go = run
    forge_ledger(ledger, n_ok=7, n_fail=3)  # 30% >= 10%, 3 >= 3
    sent = go(["--hours", "1", "--min-rate", "0.10"])
    assert len(sent) == 1
    assert sent[0]["parse_mode"] is None  # plain text
    assert "3" in sent[0]["msg"] and "whale_alert_drain.py" in sent[0]["msg"]


def test_no_alarm_below_min_failures(run):
    ledger, go = run
    forge_ledger(ledger, n_ok=8, n_fail=2)  # 20% >= 10% BUT only 2 failures
    sent = go(["--hours", "1", "--min-rate", "0.10"])
    assert sent == []


def test_no_alarm_below_min_rate(run):
    ledger, go = run
    forge_ledger(ledger, n_ok=97, n_fail=3)  # 3 failures BUT 3% < 10%
    sent = go(["--hours", "1", "--min-rate", "0.10"])
    assert sent == []


def test_default_daily_mode_unchanged(run):
    """Without --min-rate, ANY failure still alarms (existing behavior)."""
    ledger, go = run
    forge_ledger(ledger, n_ok=10, n_fail=1)
    sent = go(["--hours", "24"])
    assert len(sent) == 1


def test_old_rows_outside_window_ignored(run):
    ledger, go = run
    forge_ledger(ledger, n_ok=0, n_fail=5, minutes_ago=120)  # outside 1h window
    sent = go(["--hours", "1", "--min-rate", "0.10"])
    assert sent == []
# --- Digest liveness + shadow stall (2026-09-21 durability plan) ------------

import os
import sqlite3
from types import SimpleNamespace


def _hb(hours_ago, batches=1):
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{ts} sent_batches={batches}"


def _err(hours_ago):
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{ts} [dispatch] digest error: 'sqlite3.Row' object has no attribute 'get'"


_QUEUE_DDL = (
    "CREATE TABLE alert_queue (id INTEGER PRIMARY KEY, ts INTEGER, pipeline TEXT,"
    " tier INTEGER, dedup_key TEXT DEFAULT '', message TEXT, parse_mode TEXT,"
    " shadow INTEGER DEFAULT 0)"
)


def _make_queue_db(path, n_tier3):
    con = sqlite3.connect(str(path))
    con.execute("DROP TABLE IF EXISTS alert_queue")
    con.execute(_QUEUE_DDL)
    con.executemany(
        "INSERT INTO alert_queue (ts, pipeline, tier, message, shadow) VALUES (?,?,?,?,0)",
        [(0, "wallet_moves", 3, "m") for _ in range(n_tier3)],
    )
    con.commit()
    con.close()


def _make_shadow_db(path, last_ts):
    con = sqlite3.connect(str(path))
    con.execute("DROP TABLE IF EXISTS smart_wallet_shadows")
    con.execute("CREATE TABLE smart_wallet_shadows (id INTEGER PRIMARY KEY, ts_alert INTEGER)")
    if last_ts is not None:
        con.execute("INSERT INTO smart_wallet_shadows (ts_alert) VALUES (?)", (last_ts,))
    con.commit()
    con.close()


@pytest.fixture(autouse=True)
def _isolate_liveness_inputs(tmp_path, monkeypatch):
    """Keep the new switches inert in the pre-existing ledger tests: fresh
    heartbeat, empty queue, fresh shadow, isolated state file."""
    digest_log = tmp_path / "alert-digest.log"
    hb = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest_log.write_text(f"{hb} sent_batches=1\n")
    q = sqlite3.connect(str(tmp_path / "queue.db"))
    q.execute(_QUEUE_DDL)
    q.commit()
    q.close()
    s = sqlite3.connect(str(tmp_path / "shadow.db"))
    s.execute("CREATE TABLE smart_wallet_shadows (id INTEGER PRIMARY KEY, ts_alert INTEGER)")
    s.execute("INSERT INTO smart_wallet_shadows (ts_alert) VALUES (?)", (int(datetime.now(timezone.utc).timestamp()),))
    s.commit()
    s.close()
    monkeypatch.setenv("POLYCLAWD_DIGEST_LOG_PATH", str(digest_log))
    monkeypatch.setenv("POLYCLAWD_LIVENESS_STATE_PATH", str(tmp_path / "liveness_state.json"))
    monkeypatch.setenv("POLYCLAWD_QUEUE_DB_PATH", str(tmp_path / "queue.db"))
    monkeypatch.setenv("POLYCLAWD_SHADOW_DB_PATH", str(tmp_path / "shadow.db"))


@pytest.fixture
def liveness(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(oa, "alert_openclaw", lambda msg, **kw: (sent.append({"msg": msg, **kw}), True)[1])
    return SimpleNamespace(
        digest_log=Path(os.environ["POLYCLAWD_DIGEST_LOG_PATH"]),
        state=Path(os.environ["POLYCLAWD_LIVENESS_STATE_PATH"]),
        queue_db=Path(os.environ["POLYCLAWD_QUEUE_DB_PATH"]),
        shadow_db=Path(os.environ["POLYCLAWD_SHADOW_DB_PATH"]),
        sent=sent,
    )


def test_digest_liveness_silent_on_healthy_log(liveness):
    _make_queue_db(liveness.queue_db, 0)
    assert wd.check_digest_liveness() == []


def test_digest_liveness_quiet_flush_silent(liveness):
    """sent_batches=0 with fresh ts and no error = legitimately quiet day."""
    liveness.digest_log.write_text(_hb(2, batches=0) + "\n")
    _make_queue_db(liveness.queue_db, 0)
    assert wd.check_digest_liveness() == []


def test_digest_liveness_stale_heartbeat_alarms(liveness):
    liveness.digest_log.write_text(_hb(30) + "\n")
    alerts = wd.check_digest_liveness()
    assert len(alerts) == 1 and "DIGEST STALE" in alerts[0]


def test_digest_liveness_missing_log_alarms(liveness):
    liveness.digest_log.unlink()
    alerts = wd.check_digest_liveness()
    assert len(alerts) == 1 and "DIGEST STALE" in alerts[0]


def test_digest_liveness_missing_log_fresh_state_silent(liveness):
    """Logrotate ate the log but state recorded a fresh heartbeat -> silent."""
    liveness.digest_log.unlink()
    liveness.state.write_text(
        json.dumps({"heartbeat_ts": datetime.now(timezone.utc).timestamp() - 3600, "pages": {}})
    )
    assert wd.check_digest_liveness() == []


def test_digest_liveness_error_line_alarms(liveness):
    liveness.digest_log.write_text(_hb(2) + "\n" + _err(3) + "\n")
    alerts = wd.check_digest_liveness()
    assert len(alerts) == 1 and "DIGEST CRASH" in alerts[0]


def test_digest_liveness_old_error_silent(liveness):
    liveness.digest_log.write_text(_hb(2) + "\n" + _err(40) + "\n")
    assert wd.check_digest_liveness() == []


def test_digest_liveness_queue_backlog_alarms(liveness):
    _make_queue_db(liveness.queue_db, 61)
    alerts = wd.check_digest_liveness()
    assert len(alerts) == 1 and "DIGEST QUEUE BACKLOG" in alerts[0]


def test_digest_liveness_cooldown_suppresses_repage(liveness):
    liveness.digest_log.write_text(_hb(30) + "\n")
    assert len(wd.check_digest_liveness()) == 1
    assert wd.check_digest_liveness() == []


def test_shadow_stall_silent_when_fresh(liveness):
    _make_shadow_db(liveness.shadow_db, int(datetime.now(timezone.utc).timestamp()) - 3600)
    assert wd.check_shadow_stall() == []


def test_shadow_stall_alarms_after_36h(liveness):
    _make_shadow_db(liveness.shadow_db, int(datetime.now(timezone.utc).timestamp()) - 40 * 3600)
    alerts = wd.check_shadow_stall()
    assert len(alerts) == 1 and "SHADOW FEED STALL" in alerts[0]


def test_shadow_stall_empty_db_skips(liveness):
    _make_shadow_db(liveness.shadow_db, None)
    assert wd.check_shadow_stall() == []


def test_main_runs_liveness_before_ledger_early_return(liveness, monkeypatch):
    """No ledger file: the old code returned before any liveness check ran."""
    monkeypatch.setenv("POLYCLAWD_LEDGER_PATH", str(liveness.digest_log.parent / "absent.jsonl"))
    liveness.digest_log.write_text(_hb(30) + "\n")
    monkeypatch.setattr(sys, "argv", ["send_ledger_watchdog.py"])
    wd.main()
    assert len(liveness.sent) == 1 and "DIGEST STALE" in liveness.sent[0]["msg"]

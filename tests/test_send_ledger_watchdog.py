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

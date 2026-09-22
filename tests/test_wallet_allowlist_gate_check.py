#!/usr/bin/env python3
"""Tests for scripts/wallet_allowlist_gate_check.py — verdict delivery + failure paging."""

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.openclaw_alerts as oa
import scripts.wallet_allowlist_gate_check as gc


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Minimal snapshots + empty shadows DB so report() runs end-to-end."""
    snaps = {
        "snapshots": [
            {
                "date": time.strftime("%Y-%m-%d", time.gmtime(time.time() - 3600)),
                "ts": int(time.time()) - 3600,
                "window_days": 30,
                "min_n": 10,
                "top_n": 8,
                "n_eligible_rows": 0,
                "n_qualifying": 0,
                "wallets": {},
            }
        ]
    }
    monkeypatch.setenv("POLYCLAWD_ALLOWLIST_SNAPSHOTS", str(tmp_path / "snaps.json"))
    monkeypatch.setenv("POLYCLAWD_ALLOWLIST_CURRENT", str(tmp_path / "current.json"))
    monkeypatch.setenv("POLYCLAWD_ALLOWLIST_DB", str(tmp_path / "shadow.db"))
    (tmp_path / "snaps.json").write_text(json.dumps(snaps))
    con = sqlite3.connect(str(tmp_path / "shadow.db"))
    con.execute(
        "CREATE TABLE smart_wallet_shadows (id INTEGER PRIMARY KEY, wallet TEXT, ts_alert INTEGER,"
        " price_at_alert REAL, outcome_result TEXT, alert_type TEXT, direction TEXT,"
        " resolved INTEGER, near_settled INTEGER)"
    )
    con.commit()
    con.close()


def test_gate_check_sends_verdict(env, monkeypatch):
    sent = []
    monkeypatch.setattr("scripts.openclaw_alerts.alert_openclaw", lambda msg, **kw: (sent.append(msg), True)[1])
    gc.main()
    assert len(sent) == 1
    assert "Allowlist gate: NOT YET" in sent[0]
    assert "forward window" in sent[0]
    assert "on the list: (none)" in sent[0]


def test_gate_check_pages_on_own_failure(env, monkeypatch):
    """A broken check must page with the error, not fail silently."""
    sent = []
    monkeypatch.setattr("scripts.openclaw_alerts.alert_openclaw", lambda msg, **kw: (sent.append(msg), True)[1])

    def boom():
        raise RuntimeError("db locked")

    monkeypatch.setattr(gc, "build_message", boom)
    gc.main()
    assert len(sent) == 1
    assert "GATE CHECK FAILED" in sent[0] and "db locked" in sent[0]


def test_build_message_parses_gate_components(env):
    msg = gc.build_message()
    assert "Allowlist gate: NOT YET" in msg
    assert "day 0.0 of 21" in msg
    assert "trades 0 of 100" in msg
"""data-api TRADE rows vs live_fills — drift means untracked fills."""

import os
import sqlite3
import pytest

from scripts import position_sync as ps


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.db"))
    c.execute("""CREATE TABLE live_fills (id INTEGER PRIMARY KEY, ts TEXT,
        position_id INTEGER, order_id TEXT, side TEXT, liquidity TEXT,
        price REAL, shares REAL, usd REAL, fee_paid REAL,
        fair_price REAL, slippage_vs_fair REAL)""")
    c.commit()
    return c


@pytest.fixture(autouse=True)
def _isolated_drift_marker(tmp_path, monkeypatch):
    # Never let tests touch the real /tmp marker — that path is shared with
    # the live cron and a stale mtime there would make alert-cooldown tests
    # flaky (or silently suppress a real alert during manual testing).
    monkeypatch.setattr(ps, "_FILL_DRIFT_ALERT_FILE", str(tmp_path / "fill_drift_alerted.txt"))


def test_untracked_trade_activity_flags_drift(conn, monkeypatch):
    alerts = []
    monkeypatch.setattr(ps, "_fetch_trade_activity_usd", lambda since_ts: (3, 25.0))  # 3 chain trades, $25
    monkeypatch.setattr(ps, "_alert_fill_drift", lambda msg: alerts.append(msg))
    drift = ps.check_fill_reconciliation(conn, since_ts=0)
    assert drift["chain_trades"] == 3 and drift["db_fills"] == 0
    assert alerts, "drift must alert"


def test_matching_counts_no_alert(conn, monkeypatch):
    conn.execute(
        "INSERT INTO live_fills (ts, side, price, shares, usd, fee_paid)"
        " VALUES ('2026-08-18T00:00:00+00:00','BUY',0.5,10,5.0,0)"
    )
    conn.commit()
    alerts = []
    monkeypatch.setattr(ps, "_fetch_trade_activity_usd", lambda since_ts: (1, 5.0))
    monkeypatch.setattr(ps, "_alert_fill_drift", lambda msg: alerts.append(msg))
    ps.check_fill_reconciliation(conn, since_ts=0)
    assert alerts == []


def test_fetch_failure_returns_error_no_alert(conn, monkeypatch):
    alerts = []
    monkeypatch.setattr(ps, "_fetch_trade_activity_usd", lambda since_ts: (_ for _ in ()).throw(OSError("down")))
    monkeypatch.setattr(ps, "_alert_fill_drift", lambda msg: alerts.append(msg))
    drift = ps.check_fill_reconciliation(conn, since_ts=0)
    assert "error" in drift
    assert alerts == []


def test_usd_tolerance_drift_flags_untracked_direction(conn, monkeypatch):
    conn.execute(
        "INSERT INTO live_fills (ts, side, price, shares, usd, fee_paid)"
        " VALUES ('2026-08-18T00:00:00+00:00','BUY',0.5,10,5.0,0)"
    )
    conn.commit()
    alerts = []
    # Same count (1 == 1) but usd diff ($4) exceeds the $1 tolerance.
    monkeypatch.setattr(ps, "_fetch_trade_activity_usd", lambda since_ts: (1, 9.0))
    monkeypatch.setattr(ps, "_alert_fill_drift", lambda msg: alerts.append(msg))
    drift = ps.check_fill_reconciliation(conn, since_ts=0)
    assert drift["chain_trades"] == 1 and drift["db_fills"] == 1
    assert len(alerts) == 1
    assert "UNTRACKED FILLS" in alerts[0]


def test_cooldown_suppresses_second_alert_same_run(conn, monkeypatch):
    alerts = []
    monkeypatch.setattr(ps, "_fetch_trade_activity_usd", lambda since_ts: (3, 25.0))
    monkeypatch.setattr(ps, "_alert_fill_drift", lambda msg: alerts.append(msg))
    ps.check_fill_reconciliation(conn, since_ts=0)
    ps.check_fill_reconciliation(conn, since_ts=0)
    assert len(alerts) == 1, "cooldown must suppress the second alert"


def test_clears_on_healthy_then_realerts(conn, monkeypatch):
    alerts = []
    monkeypatch.setattr(ps, "_alert_fill_drift", lambda msg: alerts.append(msg))

    # 1) drift -> alert fires, marker written.
    monkeypatch.setattr(ps, "_fetch_trade_activity_usd", lambda since_ts: (3, 25.0))
    ps.check_fill_reconciliation(conn, since_ts=0)
    assert len(alerts) == 1
    assert os.path.exists(ps._FILL_DRIFT_ALERT_FILE)

    # 2) healthy -> marker removed.
    monkeypatch.setattr(ps, "_fetch_trade_activity_usd", lambda since_ts: (0, 0.0))
    ps.check_fill_reconciliation(conn, since_ts=0)
    assert not os.path.exists(ps._FILL_DRIFT_ALERT_FILE)

    # 3) drift again -> cooldown was cleared, so it alerts again.
    monkeypatch.setattr(ps, "_fetch_trade_activity_usd", lambda since_ts: (3, 25.0))
    ps.check_fill_reconciliation(conn, since_ts=0)
    assert len(alerts) == 2, "healthy clear must reset the cooldown"

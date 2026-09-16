"""Task 2.1 Step 1 — stop-evaluator heartbeat row (DB-backed, restart-proof).

evaluate_stops() must INSERT OR REPLACE a stop_heartbeat row after every
run — INCLUDING when there are zero open positions (the function used to
early-return before any write on empty books). The scheduler-side silence
alarm (another task) reads this row.
"""

import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

import services.stop_evaluator as se
from tests.test_stop_thresholds import SCHEMA, insert_pos


@pytest.fixture
def stop_db(tmp_path, monkeypatch):
    db = tmp_path / "shadow_trades.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO paper_portfolio_state (timestamp, bankroll, peak_bankroll)"
        " VALUES (?, 1000.0, 1000.0)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(se, "DB_PATH", db)
    monkeypatch.setattr(se, "_load_engine_state", lambda: {})
    monkeypatch.setattr(se, "_get_live_position", lambda market_id: None)
    monkeypatch.setattr(se, "_send_discord_alert", lambda info: None)
    return db


def read_heartbeat(db):
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT id, ts, positions_checked, warnings_fired FROM stop_heartbeat"
    ).fetchall()
    conn.close()
    return row


def test_heartbeat_written_with_zero_open_positions(stop_db):
    se.evaluate_stops()
    rows = read_heartbeat(stop_db)
    assert len(rows) == 1
    rid, ts, checked, warned = rows[0]
    assert rid == 1
    assert checked == 0
    assert warned == 0
    assert abs(int(time.time()) - ts) < 60


def test_heartbeat_counts_positions_and_replaces_row(stop_db, monkeypatch):
    insert_pos(stop_db)
    insert_pos(stop_db, market_id="KXTEST-STOPS-2")
    # healthy prices: entry 0.50 -> current 0.50, no loss, no warning
    monkeypatch.setattr(se, "_fetch_price", lambda pos: (pos["id"], 0.50))
    se.evaluate_stops()
    se.evaluate_stops()  # second run must REPLACE, not duplicate
    rows = read_heartbeat(stop_db)
    assert len(rows) == 1
    _, _, checked, warned = rows[0]
    assert checked == 2
    assert warned == 0


def test_heartbeat_counts_warnings_fired(stop_db, monkeypatch):
    insert_pos(stop_db)
    # -35%: warning territory, below every close threshold
    monkeypatch.setattr(se, "_fetch_price", lambda pos: (pos["id"], 0.325))
    monkeypatch.setattr(
        se, "_parse_market_date",
        lambda title: datetime.now(timezone.utc) + timedelta(hours=3),
    )
    import scripts.alert_formatter as af
    monkeypatch.setattr(af, "send_telegram", lambda msg, *a, **k: True)

    se.evaluate_stops()
    rows = read_heartbeat(stop_db)
    assert len(rows) == 1
    _, _, checked, warned = rows[0]
    assert checked == 1
    assert warned == 1

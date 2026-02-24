"""Tests for dynamic Kelly recalibration."""
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "signals"))

from paper_portfolio import (
    _get_dynamic_kelly,
    _init_tables,
    get_kelly_status,
    KELLY_FRACTION,
    KELLY_FRACTION_COLD,
    KELLY_MIN_WR,
    KELLY_ROLLING_WINDOW,
    DRAWDOWN_PAUSE_PCT,
    STARTING_BANKROLL,
)
from unittest.mock import patch


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _init_tables(conn)
    return conn


def _insert_trades(conn, wins, losses):
    now = datetime.now(timezone.utc).isoformat()
    for i in range(wins):
        conn.execute(
            "INSERT INTO paper_positions (opened_at, market_id, market_title, side, entry_price, bet_size, status, closed_at, pnl) "
            "VALUES (?, ?, ?, 'NO', 0.5, 100, 'won', ?, 100)",
            (now, f"win-{i}", f"Win {i}", now)
        )
    for i in range(losses):
        conn.execute(
            "INSERT INTO paper_positions (opened_at, market_id, market_title, side, entry_price, bet_size, status, closed_at, pnl) "
            "VALUES (?, ?, ?, 'NO', 0.5, 100, 'lost', ?, -100)",
            (now, f"loss-{i}", f"Loss {i}", now)
        )
    conn.commit()


def _set_state(conn, bankroll, peak=None):
    if peak is None:
        peak = max(bankroll, STARTING_BANKROLL)
    conn.execute(
        "INSERT INTO paper_portfolio_state (timestamp, bankroll, peak_bankroll, total_pnl) VALUES (?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), bankroll, peak, bankroll - STARTING_BANKROLL)
    )
    conn.commit()


def test_no_trades_normal():
    conn = _make_db()
    _set_state(conn, STARTING_BANKROLL)
    r = _get_dynamic_kelly(conn)
    assert r["status"] == "normal"
    assert r["fraction"] == KELLY_FRACTION


def test_winning_streak_normal():
    conn = _make_db()
    _set_state(conn, 11000)
    _insert_trades(conn, 15, 5)  # 75% WR
    r = _get_dynamic_kelly(conn)
    assert r["status"] == "normal"
    assert r["fraction"] == KELLY_FRACTION
    assert r["rolling_wr"] == 0.75


def test_cold_streak_downsizes():
    conn = _make_db()
    _set_state(conn, 9000)
    _insert_trades(conn, 4, 16)  # 20% WR
    r = _get_dynamic_kelly(conn)
    assert r["status"] == "cold"
    assert r["fraction"] == KELLY_FRACTION_COLD


def test_drawdown_pauses():
    conn = _make_db()
    _set_state(conn, 8000, peak=10000)  # 20% drawdown
    _insert_trades(conn, 10, 10)
    r = _get_dynamic_kelly(conn)
    assert r["status"] == "paused"
    assert r["fraction"] == 0


def test_drawdown_below_threshold_ok():
    conn = _make_db()
    _set_state(conn, 9000, peak=10000)  # 10% drawdown
    _insert_trades(conn, 12, 8)  # 60% WR
    r = _get_dynamic_kelly(conn)
    assert r["status"] == "normal"


def test_few_trades_stays_normal():
    """With <10 trades, don't downshift even if WR is low."""
    conn = _make_db()
    _set_state(conn, STARTING_BANKROLL)
    _insert_trades(conn, 2, 7)  # 22% WR but only 9 trades
    r = _get_dynamic_kelly(conn)
    assert r["status"] == "normal"  # Not enough trades to judge


def test_boundary_wr():
    """Exactly at threshold — should downshift."""
    conn = _make_db()
    _set_state(conn, 9500)
    _insert_trades(conn, 10, 10)  # 50% < 55%
    r = _get_dynamic_kelly(conn)
    assert r["status"] == "cold"


@patch("paper_portfolio._get_db")
def test_kelly_status_api(mock_db):
    conn = _make_db()
    mock_db.return_value = conn
    _set_state(conn, STARTING_BANKROLL)
    r = get_kelly_status()
    assert "fraction" in r
    assert "status" in r
    assert "rolling_wr" in r


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"✓ {name}")
    print("\nALL TESTS PASSED ✅")

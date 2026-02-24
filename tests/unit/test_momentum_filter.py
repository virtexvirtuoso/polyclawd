"""Tests for price momentum filter."""
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "signals"))

from price_momentum_filter import (
    calculate_momentum,
    check_entry,
    RISING_THRESHOLD,
    FALLING_THRESHOLD,
)


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE signal_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_date TEXT, snapshot_time TEXT, source TEXT, platform TEXT,
        market_id TEXT, market TEXT, category TEXT, side TEXT,
        price REAL, confidence REAL, volume INTEGER, days_to_close REAL,
        confirmations INTEGER, reasoning TEXT, raw_json TEXT
    )""")
    return conn


def _insert_prices(conn, market_id, prices):
    """Insert price snapshots spaced 1h apart, ending now."""
    now = datetime.now(timezone.utc)
    for i, price in enumerate(prices):
        t = now - timedelta(hours=len(prices) - 1 - i)
        conn.execute(
            "INSERT INTO signal_snapshots (snapshot_date, snapshot_time, market_id, market, price) VALUES (?, ?, ?, ?, ?)",
            (t.strftime("%Y-%m-%d"), t.isoformat(), market_id, "Test", price)
        )
    conn.commit()


def test_no_history_allows():
    conn = _make_db()
    r = calculate_momentum("mkt-1", 0.5, conn)
    assert r["recommendation"] == "enter"


def test_rising_detected():
    conn = _make_db()
    _insert_prices(conn, "mkt-1", [0.50, 0.52, 0.55, 0.58])
    r = calculate_momentum("mkt-1", conn=conn)
    assert r["direction"] == "rising"
    assert r["momentum"] > RISING_THRESHOLD


def test_falling_detected():
    conn = _make_db()
    _insert_prices(conn, "mkt-1", [0.60, 0.57, 0.54, 0.50])
    r = calculate_momentum("mkt-1", conn=conn)
    assert r["direction"] == "falling"
    assert r["momentum"] < FALLING_THRESHOLD


def test_flat_detected():
    conn = _make_db()
    _insert_prices(conn, "mkt-1", [0.50, 0.51, 0.50, 0.51])
    r = calculate_momentum("mkt-1", conn=conn)
    assert r["direction"] == "flat"


def test_check_entry_blocks_falling_no():
    conn = _make_db()
    _insert_prices(conn, "mkt-1", [0.60, 0.55, 0.50, 0.45])
    r = check_entry("mkt-1", 0.45, "NO", conn)
    assert r["allow"] is False


def test_check_entry_boosts_rising_no():
    conn = _make_db()
    _insert_prices(conn, "mkt-1", [0.50, 0.53, 0.56, 0.60])
    r = check_entry("mkt-1", 0.60, "NO", conn)
    assert r["allow"] is True
    assert r["multiplier"] > 1.0


def test_check_entry_passthrough_yes():
    conn = _make_db()
    _insert_prices(conn, "mkt-1", [0.60, 0.55, 0.50, 0.45])
    r = check_entry("mkt-1", 0.45, "YES", conn)
    assert r["allow"] is True  # Momentum filter only applies to NO


def test_current_price_overrides():
    conn = _make_db()
    _insert_prices(conn, "mkt-1", [0.50, 0.50, 0.50])
    # Current price much higher than history → rising
    r = calculate_momentum("mkt-1", current_price=0.60, conn=conn)
    assert r["direction"] == "rising"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"✓ {name}")
    print("\nALL TESTS PASSED ✅")

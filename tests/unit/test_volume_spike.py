"""Tests for volume spike detector."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "signals"))

from volume_spike_detector import (
    detect_spike,
    get_volume_baseline,
    SPIKE_RATIO,
    MEGA_SPIKE_RATIO,
    MIN_HISTORY_POINTS,
    MIN_VOLUME,
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


def _insert_snapshots(conn, market_id, volumes):
    for i, vol in enumerate(volumes):
        conn.execute(
            "INSERT INTO signal_snapshots (snapshot_date, snapshot_time, market_id, market, volume) VALUES (?, ?, ?, ?, ?)",
            ("2026-02-24", f"{10+i}:00", market_id, "Test market", vol)
        )
    conn.commit()


# --- Tests ---

def test_no_history_no_spike():
    conn = _make_db()
    r = detect_spike("mkt-1", 5000, conn)
    assert r["spike"] is False
    assert "reason" in r or r["data_points"] == 0


def test_below_min_volume():
    conn = _make_db()
    r = detect_spike("mkt-1", 50, conn)  # Below MIN_VOLUME
    assert r["spike"] is False


def test_no_spike_normal_volume():
    conn = _make_db()
    _insert_snapshots(conn, "mkt-1", [1000, 1200, 900, 1100])
    r = detect_spike("mkt-1", 1500, conn)  # ~1.4x, not a spike
    assert r["spike"] is False
    assert r["level"] == "none"


def test_spike_detected():
    conn = _make_db()
    _insert_snapshots(conn, "mkt-1", [1000, 1000, 1000, 1000])
    r = detect_spike("mkt-1", 4000, conn)  # 4x = spike
    assert r["spike"] is True
    assert r["level"] == "spike"
    assert r["ratio"] >= SPIKE_RATIO


def test_mega_spike():
    conn = _make_db()
    _insert_snapshots(conn, "mkt-1", [1000, 1000, 1000, 1000])
    r = detect_spike("mkt-1", 15000, conn)  # 15x = mega
    assert r["spike"] is True
    assert r["level"] == "mega"
    assert r["ratio"] >= MEGA_SPIKE_RATIO


def test_baseline_calculation():
    conn = _make_db()
    _insert_snapshots(conn, "mkt-1", [1000, 2000, 3000])
    b = get_volume_baseline("mkt-1", conn)
    assert b["avg_volume"] == 2000
    assert b["data_points"] == 3
    assert b["min_volume"] == 1000
    assert b["max_volume"] == 3000


def test_insufficient_history():
    conn = _make_db()
    _insert_snapshots(conn, "mkt-1", [1000, 1000])  # Only 2, need 3
    r = detect_spike("mkt-1", 5000, conn)
    assert r["spike"] is False
    assert "reason" in r


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"✓ {name}")
    print("\nALL TESTS PASSED ✅")

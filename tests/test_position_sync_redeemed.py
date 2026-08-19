"""Redeemed-and-gone positions must close as wins via data-api REDEEM activity."""
import sqlite3
import pytest

from scripts import position_sync as ps


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.db"))
    c.execute("""CREATE TABLE live_positions (
        id INTEGER PRIMARY KEY, opened_at TEXT, market_id TEXT, market_slug TEXT,
        market_title TEXT, token_id TEXT, side TEXT, entry_price REAL, shares REAL,
        cost_usd REAL, status TEXT, closed_at TEXT, exit_price REAL, pnl REAL,
        close_reason TEXT, fee_paid_total REAL, archetype TEXT)""")
    c.execute("""INSERT INTO live_positions
        (id, opened_at, market_id, market_title, token_id, side, entry_price,
         shares, cost_usd, status, fee_paid_total)
        VALUES (8, '2026-07-14T01:37:00+00:00', 'tok123', 'Granby tennis',
                'tok123', 'BUY', 0.74, 13.51, 9.9974, 'open', 0.0)""")
    c.commit()
    return c


def test_redeemed_absent_position_closes_as_win(conn, monkeypatch):
    monkeypatch.setattr(ps, "_already_resolution_alerted", lambda pid: False)
    monkeypatch.setattr(ps, "_mark_resolution_alerted", lambda pid: None)
    monkeypatch.setattr(ps, "_sdk_token_price_map", lambda: {})  # token gone from SDK
    monkeypatch.setattr(ps, "_fetch_redeem_assets", lambda: {"tok123"})
    resolved = ps.check_resolutions(conn)
    assert len(resolved) == 1
    row = conn.execute("SELECT status, pnl, close_reason FROM live_positions WHERE id=8").fetchone()
    assert row[0] == "closed"
    assert row[1] == pytest.approx((1 - 0.74) * 13.51, abs=0.001)
    assert row[2] == "redeemed_detected"


def test_absent_without_redeem_activity_stays_open(conn, monkeypatch):
    monkeypatch.setattr(ps, "_already_resolution_alerted", lambda pid: False)
    monkeypatch.setattr(ps, "_sdk_token_price_map", lambda: {})
    monkeypatch.setattr(ps, "_fetch_redeem_assets", lambda: set())
    ps.check_resolutions(conn)
    assert conn.execute("SELECT status FROM live_positions WHERE id=8").fetchone()[0] == "open"

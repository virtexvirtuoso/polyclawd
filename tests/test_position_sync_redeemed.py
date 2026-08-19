"""Redeemed-and-gone positions must close as wins via data-api REDEEM activity."""
import sqlite3
import sys
import types

import pytest

from scripts import position_sync as ps


def _inject_inert_clob_client(monkeypatch):
    """Prevent the fallback SDK/get_market chain from doing real I/O in tests."""
    fake = types.ModuleType("execution.clob_client")
    fake._get_client = lambda: (_ for _ in ()).throw(RuntimeError("no client in tests"))
    monkeypatch.setitem(sys.modules, "execution.clob_client", fake)


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
    monkeypatch.setattr(ps, "_fetch_redeem_payouts", lambda: {"tok123": 13.51})
    _inject_inert_clob_client(monkeypatch)
    resolved = ps.check_resolutions(conn)
    assert len(resolved) == 1
    row = conn.execute("SELECT status, pnl, close_reason FROM live_positions WHERE id=8").fetchone()
    assert row[0] == "closed"
    assert row[1] == pytest.approx(13.51 - 9.9974, abs=0.001)
    assert row[2] == "redeemed_detected"


def test_absent_without_redeem_activity_stays_open(conn, monkeypatch):
    monkeypatch.setattr(ps, "_already_resolution_alerted", lambda pid: False)
    monkeypatch.setattr(ps, "_mark_resolution_alerted", lambda pid: None)
    monkeypatch.setattr(ps, "_sdk_token_price_map", lambda: {})
    monkeypatch.setattr(ps, "_fetch_redeem_payouts", lambda: {})
    monkeypatch.setattr(ps, "_fetch_gamma_market", lambda x: {})
    _inject_inert_clob_client(monkeypatch)
    ps.check_resolutions(conn)
    assert conn.execute("SELECT status FROM live_positions WHERE id=8").fetchone()[0] == "open"


def test_fetch_redeem_payouts_bridges_condition_to_token(monkeypatch):
    import io, json as _json
    payload = [
        {"type": "REDEEM", "asset": "", "conditionId": "0xbadd", "usdcSize": 13.51, "size": 13.51},
        {"type": "TRADE", "side": "BUY", "asset": "tok123", "conditionId": "0xbadd", "usdcSize": 9.9974},
        {"type": "TRADE", "side": "BUY", "asset": "tok999", "conditionId": "0xother", "usdcSize": 5.0},
        {"type": "REDEEM", "asset": "", "conditionId": "0xzero", "usdcSize": 0.0, "size": 250.0},
        {"type": "TRADE", "side": "BUY", "asset": "tokzero", "conditionId": "0xzero", "usdcSize": 45.5},
    ]
    class _Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(ps.urllib.request, "urlopen",
                        lambda req, timeout=15: _Resp(_json.dumps(payload).encode()))
    out = ps._fetch_redeem_payouts()
    assert out == {"tok123": 13.51}  # zero-payout redemption excluded, unrelated token excluded


def test_fetch_redeem_payouts_returns_none_on_failure(monkeypatch):
    def boom(req, timeout=15):
        raise OSError("network down")
    monkeypatch.setattr(ps.urllib.request, "urlopen", boom)
    assert ps._fetch_redeem_payouts() is None

"""Regression tests for the 2026-08-26 → 09-19 smart-wallet shadow blackout.

A stray `"market_slug": meta.get("slug", "")` key in fills_from_trades
referenced an undefined `meta` and NameError'd EVERY smart-wallet fill, while
scanner_hook's blanket except returned [] silently — 25 days of zero shadows
logged while the scheduler line read "0 alerts fired" like a quiet market.
Accumulator froze at 2026-08-25 22:05:32; dedup ledger stayed fresh (the crash
sits after dedup, before accumulation).
"""
import sqlite3

import pytest

from scripts import smart_wallet_alert as swa


SMART = {"0xabc": {"name": "Test Whale", "win_rate": 0.6, "net_pnl": 1000,
                   "closed_positions": 50, "source_category": "sports",
                   "is_bot": 0}}


def _trade(wallet="0xabc", cid="0xcid", side="BUY", size=100.0, price=0.5):
    return {"proxyWallet": wallet, "conditionId": cid, "side": side,
            "size": size, "price": price, "outcome": "Yes",
            "outcomeIndex": 0, "title": "Test Market"}


def test_fills_from_trades_maps_a_valid_fill():
    """The exact input class that NameError'd every fill for 25 days."""
    out = swa.fills_from_trades([_trade()], SMART)
    assert len(out) == 1
    assert out[0]["wallet"] == "0xabc"
    assert out[0]["direction"] == "BUY"
    assert out[0]["usd"] == pytest.approx(50.0)


def test_fills_from_trades_skips_junk_without_raising():
    out = swa.fills_from_trades([
        _trade(wallet=None),
        _trade(cid=None),
        _trade(side="HOLD"),
        _trade(size=0),
    ], SMART)
    assert out == []


def test_check_and_fire_records_market_slug():
    """market_slug now comes from meta_for's gamma slug inside check_and_fire,
    so alert formatting keeps its polymarket.com/event/<slug> URL."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    swa.init_accum(conn)
    swa.init_shadows(conn)

    def meta_for(cid):
        return {"volume": 1_000_000, "price": 0.40, "title": "Test Market",
                "close_time": "2026-12-31", "slug": "test-slug"}

    fired = swa.check_and_fire(
        conn, conn,
        [dict(wallet="0xabc", market="0xcid", direction="BUY",
              usd=swa.THRESHOLD * 1.2, price=0.40, outcome="Yes",
              outcome_index=0, name="Test Whale", title="Test Market")],
        meta_for, now=1000)
    assert len(fired) == 1
    assert fired[0]["market_slug"] == "test-slug"


def test_scanner_hook_failure_is_loud_not_silent(capsys):
    """The silent `except: return []` is what hid the NameError for 25 days.
    A scanner_hook crash must hit stderr (journald captures it) — a gate that
    rejects 100% of actions is indistinguishable from no signal."""

    class Boom:
        def execute(self, *a, **k):
            raise RuntimeError("boom")

    assert swa.scanner_hook(Boom(), [], {}, {}) == []
    err = capsys.readouterr().err
    assert "scanner_hook FAILED" in err
    assert "RuntimeError" in err
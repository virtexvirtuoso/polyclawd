"""Contract tests for scripts/smart_wallet_alert.py — the shadow-first smart
wallet entry/exit alert. Pure logic (no network): fills are injected directly
and market metadata comes from a stub provider.
"""
import sqlite3
import pytest

from scripts import smart_wallet_alert as swa


@pytest.fixture()
def conns():
    """One in-memory DB holding both the accumulator and shadow tables."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    swa.init_accum(c)
    swa.init_shadows(c)
    return c, c  # (meta_conn, shadow_conn)


def _meta(volume=1_000_000, price=0.40, title="Test Market"):
    def provider(cid):
        return {"volume": volume, "price": price, "title": title,
                "close_time": "2026-12-31"}
    return provider


# Amounts are expressed as multiples of the LIVE threshold, not hardcoded
# dollars. These tests pinned $500 and went red the moment it was raised to
# $1000 (2026-06-25). Deriving from swa.THRESHOLD means the next retune cannot
# silently desync them.
T = swa.THRESHOLD
CROSS = T * 0.4      # 3 of these (1.2T) cross the threshold on the third fill
OVER = T * 1.2       # a single fill that clears the threshold outright
UNDER = T * 0.4      # tops up without reaching the 2x refire multiple


def _fill(wallet="RN1", market="m1", direction="BUY", usd=200.0, price=0.40,
          outcome="Yes", outcome_index=0, name="RN1", title="Test Market"):
    return dict(wallet=wallet, market=market, direction=direction, usd=usd,
                price=price, outcome=outcome, outcome_index=outcome_index,
                name=name, title=title)


def _shadows(conn):
    return conn.execute("SELECT * FROM smart_wallet_shadows").fetchall()


def test_sub_threshold_does_not_fire(conns):
    meta, shadow = conns
    fired = swa.check_and_fire(meta, shadow, [_fill(usd=T * 0.2), _fill(usd=T * 0.2)],
                               _meta(), now=1000)
    assert fired == []
    assert _shadows(shadow) == []


def test_crossing_threshold_fires_one_entry(conns):
    meta, shadow = conns
    # three fills in the same cycle window -> 1.2x THRESHOLD, crossed once
    fired = swa.check_and_fire(
        meta, shadow,
        [_fill(usd=CROSS, price=0.40), _fill(usd=CROSS, price=0.41),
         _fill(usd=CROSS, price=0.42)],
        _meta(), now=1000)
    assert len(fired) == 1
    assert fired[0]["alert_type"] == "entry"
    rows = _shadows(shadow)
    assert len(rows) == 1
    assert rows[0]["direction"] == "BUY"
    assert rows[0]["cumulative_usd"] >= T
    # price_at_alert is the crossing fill's price
    assert rows[0]["price_at_alert"] == pytest.approx(0.42)


def test_rolling_window_prunes_old_flow(conns):
    meta, shadow = conns
    # $300 now, then $300 five hours later: first fill aged out of the 4h
    # window -> total is $300, NOT $600 -> no fire (proves true rolling window)
    swa.check_and_fire(meta, shadow, [_fill(usd=T * 0.6)], _meta(), now=1000)
    fired = swa.check_and_fire(meta, shadow, [_fill(usd=T * 0.6)],
                               _meta(), now=1000 + 5 * 3600)
    assert fired == []
    assert _shadows(shadow) == []


def test_refire_on_double(conns):
    meta, shadow = conns
    swa.check_and_fire(meta, shadow, [_fill(usd=OVER)], _meta(), now=1000)
    # cumulative now 1.2T (fired). add the same again -> 2.4T >= 2x -> refire
    fired = swa.check_and_fire(meta, shadow, [_fill(usd=OVER)], _meta(), now=1100)
    assert len(fired) == 1
    assert fired[0]["alert_type"] == "refire"
    assert len(_shadows(shadow)) == 2


def test_no_refire_below_double(conns):
    meta, shadow = conns
    swa.check_and_fire(meta, shadow, [_fill(usd=OVER)], _meta(), now=1000)
    fired = swa.check_and_fire(meta, shadow, [_fill(usd=UNDER)], _meta(), now=1100)
    assert fired == []  # 1.6T < the 2.4T refire multiple
    assert len(_shadows(shadow)) == 1


def test_sell_fires_exit_independently(conns):
    meta, shadow = conns
    fired = swa.check_and_fire(meta, shadow, [_fill(direction="SELL", usd=OVER)],
                               _meta(), now=1000)
    assert len(fired) == 1
    assert fired[0]["alert_type"] == "exit"
    assert _shadows(shadow)[0]["direction"] == "SELL"


def test_gate_thin_market_suppresses_and_allows_later(conns):
    meta, shadow = conns
    # thin market -> suppressed, and alert_fired must NOT be set (so it can fire
    # once liquidity arrives)
    fired = swa.check_and_fire(meta, shadow, [_fill(usd=OVER)],
                               _meta(volume=50_000), now=1000)
    assert fired == []
    assert _shadows(shadow) == []
    # same market, now liquid -> should fire
    fired2 = swa.check_and_fire(meta, shadow, [_fill(usd=T * 0.05)],
                                _meta(volume=200_000), now=1100)
    assert len(fired2) == 1


def test_gate_near_settled_suppresses(conns):
    meta, shadow = conns
    fired = swa.check_and_fire(meta, shadow, [_fill(usd=OVER)],
                               _meta(price=0.95), now=1000)
    assert fired == []


def test_restart_does_not_double_fire(conns):
    meta, shadow = conns
    swa.check_and_fire(meta, shadow, [_fill(usd=OVER)], _meta(), now=1000)
    # simulate a process restart: state persists in the same DB; a tiny new
    # fill must not re-fire (already fired, below 2x)
    fired = swa.check_and_fire(meta, shadow, [_fill(usd=T * 0.01)], _meta(), now=1200)
    assert fired == []
    assert len(_shadows(shadow)) == 1

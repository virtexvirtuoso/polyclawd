"""Tests for the PM maker shadow (knob 7) — fill judgment + tier classify."""
from datetime import datetime, timedelta, timezone

from signals import pm_maker_shadow as ps


def _order(side="NO", level=0.95, mode="join", contracts=100):
    now = datetime.now(timezone.utc)
    return dict(side=side, level=level, mode=mode, contracts=contracts,
                ts=now.isoformat(),
                rest_until=(now + timedelta(hours=3)).isoformat())


def _trade(outcome, side, price, size=50, dt_offset=60):
    ts = datetime.now(timezone.utc).timestamp() + dt_offset
    return dict(outcome=outcome, side=side, price=price, size=size, timestamp=ts)


def test_join_fills_on_strict_no_sell_through():
    o = _order(level=0.95, mode="join")
    # NO sold at 0.94 (< level) -> our bid swept first
    f = ps.judge_fill(o, [_trade("No", "SELL", 0.94)])
    assert f.fill_rate if False else f["filled_contracts"] == 50
    # NO sold AT 0.95 -> queue unknown, join must NOT count
    f2 = ps.judge_fill(o, [_trade("No", "SELL", 0.95)])
    assert f2["filled_contracts"] == 0


def test_improve_counts_at_level():
    o = _order(level=0.95, mode="improve")
    f = ps.judge_fill(o, [_trade("No", "SELL", 0.95)])
    assert f["filled_contracts"] == 50


def test_complementary_yes_buy_fills_no_bid():
    # NO bid at 0.95 <=> YES at 0.05; a taker BUYing Yes at 0.06 (>1-0.95) mints
    o = _order(level=0.95, mode="join")
    f = ps.judge_fill(o, [_trade("Yes", "BUY", 0.06)])
    assert f["filled_contracts"] == 50
    # Yes bought AT 0.05 -> not strict-through for join
    f2 = ps.judge_fill(o, [_trade("Yes", "BUY", 0.05)])
    assert f2["filled_contracts"] == 0


def test_trades_outside_window_ignored():
    o = _order(level=0.95, mode="join")
    f = ps.judge_fill(o, [_trade("No", "SELL", 0.90, dt_offset=999999)])
    assert f["filled_contracts"] == 0


def test_classify_tiers():
    assert ps.classify(0.08)[0] == "pm_fade_longshot_no"
    assert ps.classify(0.60)[0] == "pm_fade_favorite_yes"
    assert ps.classify(0.30)[0] is None
    assert ps.classify(None)[0] is None

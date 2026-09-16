"""Regression tests for the wallet-selection defects exposed by the canary's
first live trade (2026-08-21).

The trade followed wallet 0x5f659bcc...482036 into a tennis market. That wallet:
  * 501 closed positions, 29 wins -- a 5.8% win rate
  * -$93,006.77 realized / -$95,496.03 net
  * -$1,807.97 over 127 trades in *sports*, the archetype being traded
and was nonetheless flagged smart=1, because is_smart() short-circuited on a
skill gate computed from an UNWEIGHTED mean per-position return.

Two independent holes, either of which alone would have blocked the trade:
  1. skill_returns/skill_score ignored position size, so losses concentrated in
     the largest positions could not register.
  2. wallet_archetype_pnl was computed, stored, and never consulted.
"""

import sqlite3

import pytest

from signals import whale_wallets as ww
from signals.whale_wallets import (
    ARCHETYPE_GATE_MIN_TRADES,
    SKILL_GATE_MIN_NET,
    archetype_gate_ok,
    is_smart,
    skill_returns,
    skill_score,
)


# ---------------------------------------------------------------------------
# 1. Size weighting
# ---------------------------------------------------------------------------


def _pos(avg_price, realized_pnl, initial_value, current_value=0.0, redeemable=False):
    return {
        "avgPrice": avg_price,
        "realizedPnl": realized_pnl,
        "initialValue": initial_value,
        "currentValue": current_value,
        "size": 1.0,
        "redeemable": redeemable,
    }


def test_skill_returns_emits_size_weights():
    rows = [_pos(0.5, 10.0, 100.0)]
    out = skill_returns(rows)
    assert len(out) == 1
    ret, weight = out[0]
    assert weight == pytest.approx(100.0)
    assert ret == pytest.approx(10.0 * 0.5 / 100.0)


def test_many_small_wins_cannot_outvote_one_huge_loss():
    """The exact shape of the live wallet: good small, catastrophic large.

    Unweighted, the mean return is positive. Dollar-weighted, it is deeply
    negative -- which is what actually determines whether copying it pays.
    """
    rows = [_pos(0.5, 5.0, 10.0) for _ in range(40)]           # 40 x +$5 on $10
    rows.append(_pos(0.5, 0.0, 20000.0, current_value=0.0))    # one $20k zombie

    pairs = skill_returns(rows)
    unweighted_mean = sum(r for r, _ in pairs) / len(pairs)
    result = skill_score(pairs, sims=2000)

    assert unweighted_mean > 0, "precondition: unweighted metric looks skilled"
    assert result["skill_ret"] < 0, "weighted metric must see the capital loss"


def test_skill_score_accepts_bare_floats_for_back_compat():
    result = skill_score([0.1, 0.2, 0.3], sims=1000)
    assert result["skill_n"] == 3
    assert result["skill_ret"] == pytest.approx(0.2)


def test_skill_score_ignores_zero_weight_positions():
    """A position with no cost basis deployed no capital and must not vote."""
    result = skill_score([(0.9, 0.0), (0.1, 100.0)], sims=1000)
    assert result["skill_n"] == 1
    assert result["skill_ret"] == pytest.approx(0.1)


def test_skill_score_empty_is_neutral():
    assert skill_score([], sims=100) == {"skill_n": 0, "skill_ret": None, "skill_p": None}


# ---------------------------------------------------------------------------
# 2. The capital floor the skill path may not bypass
# ---------------------------------------------------------------------------


def _skilled(net):
    """Stats that pass the statistical skill gate outright."""
    return {"skill_n": 501, "skill_ret": 0.0128, "skill_p": 0.0165,
            "closed": 501, "wins": 29, "realized": net, "net": net}


def test_skill_path_rejects_capital_destroying_wallet():
    """The live wallet: skill-gate positive, -$95,496 net. Must NOT be smart."""
    assert is_smart(_skilled(-95496.03)) is False


def test_skill_path_still_admits_a_profitable_skilled_wallet():
    """The gate's legitimate purpose survives: low WR is still forgiven."""
    stats = _skilled(2500.0)
    assert stats["wins"] / stats["closed"] < 0.10   # 5.8% WR, way under the floor
    assert is_smart(stats) is True


def test_skill_path_floor_is_the_named_constant():
    assert is_smart(_skilled(SKILL_GATE_MIN_NET)) is True
    assert is_smart(_skilled(SKILL_GATE_MIN_NET - 0.01)) is False


def test_non_skill_path_unchanged_by_the_floor():
    """Wallets that fail the skill gate still face the ordinary thresholds."""
    stats = {"skill_n": 0, "skill_ret": None, "skill_p": None,
             "closed": 40, "wins": 28, "realized": 5000.0, "net": 5000.0}
    # 70% WR but net well under SMART_MIN_NET ($100k) -> not smart.
    assert is_smart(stats) is False


# ---------------------------------------------------------------------------
# 3. Per-archetype gate
# ---------------------------------------------------------------------------


@pytest.fixture()
def meta():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE wallet_archetype_pnl (
        wallet TEXT, archetype TEXT, trades INT, wins INT, pnl REAL,
        concentration REAL DEFAULT 0, updated REAL)""")
    yield c
    c.close()


def _arch_row(c, wallet, archetype, trades, wins, pnl):
    c.execute("INSERT INTO wallet_archetype_pnl (wallet, archetype, trades, wins, pnl)"
              " VALUES (?,?,?,?,?)", (wallet, archetype, trades, wins, pnl))
    c.commit()


def test_archetype_gate_blocks_the_actual_live_trade(meta):
    """Replays the real numbers: -$1,807.97 over 127 sports trades."""
    w = "0x5f659bccbc353dbf7bcdffdee73bee60bb482036"
    _arch_row(meta, w, "sports", 127, 62, -1807.97)

    ok, why = archetype_gate_ok(meta, w, "sports")
    assert ok is False
    assert "archetype_gate" in why
    assert "127" in why


def test_archetype_gate_allows_profitable_record(meta):
    w = "0xabc"
    _arch_row(meta, w, "sports", 50, 30, 4200.0)
    ok, why = archetype_gate_ok(meta, w, "sports")
    assert ok is True


def test_archetype_gate_abstains_on_thin_record(meta):
    """Losing but too few trades to judge -- must abstain, not block."""
    w = "0xabc"
    _arch_row(meta, w, "crypto", ARCHETYPE_GATE_MIN_TRADES - 1, 0, -500.0)
    ok, why = archetype_gate_ok(meta, w, "crypto")
    assert ok is True
    assert "abstain" in why


def test_archetype_gate_abstains_when_no_record(meta):
    ok, why = archetype_gate_ok(meta, "0xnew", "sports")
    assert ok is True
    assert "abstain" in why


def test_archetype_gate_is_per_archetype_not_global(meta):
    """A wallet may be good at one vertical and bad at another."""
    w = "0xabc"
    _arch_row(meta, w, "sports", 127, 62, -1807.97)
    _arch_row(meta, w, "crypto", 40, 30, 9000.0)

    assert archetype_gate_ok(meta, w, "sports")[0] is False
    assert archetype_gate_ok(meta, w, "crypto")[0] is True


def test_archetype_gate_never_raises(meta):
    """A gate that throws would take execution down with it."""
    meta.execute("DROP TABLE wallet_archetype_pnl")
    ok, why = archetype_gate_ok(meta, "0xabc", "sports")
    assert ok is True
    assert "abstain" in why


def test_archetype_gate_abstains_on_missing_inputs(meta):
    assert archetype_gate_ok(meta, "", "sports")[0] is True
    assert archetype_gate_ok(meta, "0xabc", "")[0] is True


# ---------------------------------------------------------------------------
# 4. The classifier must actually route this market to "sports"
# ---------------------------------------------------------------------------


def test_tennis_market_classifies_as_sports():
    """The gate is useless if the live market lands in the wrong archetype."""
    from signals.whale_follower import classify_archetype

    arch = classify_archetype(
        "polymarket",
        "0x61667bf31ebd267b84655d20b438a9d9bec099fd26894613bf5fc3798406350f",
        "Cincinnati Open: Taylor Fritz vs Brandon Nakashima",
    )
    assert arch == "sports"

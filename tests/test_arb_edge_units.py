"""Regression tests for cross-platform arb edge math and matcher polarity.

Replaces the 2026-08-26 first draft, which an independent review correctly
rejected: it was a top-level script (pytest collected ZERO tests from it) and its
only assertion, `net_edge_pp <= gross_pp`, is a tautology under the new
implementation (`net_edge_pp = gross_edge_pp - cost_pp`, `cost_pp >= 0`). A
mutation with the exact bug class being fixed still passed it.

These tests assert COMPUTED VALUES against independently hand-derived numbers.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.fee_model import taker_fee_fraction  # noqa: E402
from odds.edge_math import net_arb_edge  # noqa: E402
from signals.cross_platform_arb import _polarity_compatible  # noqa: E402

SLIP_PER_LEG = 0.005
# An arb crosses TWO spreads (buy leg + sell leg), so slippage is charged twice.
# These 4 tests FAILED when two-leg slippage landed, which is the point: they
# assert computed values, so a cost-model change cannot pass silently.
SLIP_TOTAL = SLIP_PER_LEG * 2


def _expected(buy, sell, cat="politics"):
    """Independently recompute what net_arb_edge should return."""
    bf = taker_fee_fraction(buy, "polymarket", cat)
    sf = taker_fee_fraction(sell, "kalshi", cat)
    gross_pp = (sell - buy) * 100.0
    net_pp = gross_pp - (bf + sf + SLIP_TOTAL) * 100.0
    capital = buy + (1.0 - sell)
    return gross_pp, net_pp, capital, net_pp / 100.0 / capital


@pytest.mark.parametrize("buy,sell", [(0.45, 0.55), (0.05, 0.67), (0.20, 0.30), (0.90, 0.95)])
def test_net_arb_edge_values_match_hand_derivation(buy, sell):
    r = net_arb_edge(buy_price=buy, sell_price=sell,
                     buy_platform="polymarket", sell_platform="kalshi")
    gross_pp, net_pp, capital, net_ret = _expected(buy, sell)
    assert r["gross_edge_pp"] == pytest.approx(gross_pp, abs=0.01)
    assert r["net_edge_pp"] == pytest.approx(net_pp, abs=0.01)
    assert r["capital_per_contract"] == pytest.approx(capital, abs=1e-6)
    assert r["net_return"] == pytest.approx(net_ret, abs=1e-4)


def test_capital_is_both_legs_not_buy_leg_only():
    """The original bug: return computed on the buy leg alone.

    buy=0.05, sell=0.67 -> capital must be 0.38, NOT 0.05. The old code's
    (sell/buy)-1 = 12.4 implied a 0.05 capital base and reported 1240.
    """
    r = net_arb_edge(buy_price=0.05, sell_price=0.67,
                     buy_platform="polymarket", sell_platform="kalshi")
    assert r["capital_per_contract"] == pytest.approx(0.38, abs=1e-6)
    assert r["gross_edge_pp"] == pytest.approx(62.0, abs=0.01)
    # Would be ~1240 under the bug; must be nowhere near it.
    assert r["net_edge_pp"] < 100.0


def test_costs_are_converted_to_percentage_points_not_left_as_fractions():
    """Mutation guard: if costs were subtracted as fractions, net ~= gross.

    Hand-derived at buy=0.45/sell=0.55 the cost term is ~3.2pp, so the gap
    between gross and net must be >= 1pp. Leaving costs as fractions makes the
    gap ~0.03pp, which this catches and the old tautology did not.
    """
    r = net_arb_edge(buy_price=0.45, sell_price=0.55,
                     buy_platform="polymarket", sell_platform="kalshi")
    gap = r["gross_edge_pp"] - r["net_edge_pp"]
    assert gap >= 1.0, f"cost term only {gap:.4f}pp - costs likely not scaled to pp"


@pytest.mark.parametrize("a,b", [
    ("Will Bitcoin be above 100000 on Dec 31?", "Will Bitcoin be below 100000 on Dec 31?"),
    ("Will inflation be more than 3% in 2027?", "Will inflation be less than 3% in 2027?"),
    ("Will Trump win the 2028 election?", "Will Trump not win the 2028 election?"),
    ("Will Zelenskyy and Putin meet before 2027?",
     "Will Zelenskyy and Putin not meet before 2027?"),
])
def test_polarity_opposites_are_rejected(a, b):
    """STOPWORDS strips not/no/above/below/more/less, so these score sim=1.000."""
    assert _polarity_compatible(a, b) is False


@pytest.mark.parametrize("a,b", [
    ("Will Jon Ossoff be the Democratic Presidential nominee in 2028?",
     "Will Jon Ossoff win the 2028 Democratic presidential nomination?"),
    ("Will Naftali Bennett be the next Prime Minister of Israel?",
     "Will Naftali Bennett be the next Prime Minister of Israel?"),
])
def test_genuine_pairs_survive_polarity_guard(a, b):
    assert _polarity_compatible(a, b) is True

"""Pin every Polymarket taker rate to its documented value, so drift fails loudly.

Why this file exists: `fee_model.TAKER_RATE` carried `sports: 0.03` -- correct
when it was verified 2026-06-02, but Polymarket raised sports to 0.05 in July
2026. Nothing failed. `odds/sports_edge_common.fee_adjusted_edge` quietly
understated the fee on EVERY sports edge by ~0.42-0.50pp for weeks, which made
the alert gate that much looser than intended.

A rate table with no test is a comment. These tests are the gate: if Polymarket
moves a rate again, this file goes red instead of the edge silently inflating.

Rates verified 2026-08-26 against three mutually-consistent sources:
  - docs.polymarket.com/trading/fees                   (AUTHORITATIVE, primary --
    developer fee reference; all 11 rates below match it exactly, as does the
    formula `fee = C * feeRate * p * (1-p)`. Confirms makers pay 0 and states NO
    fee on winnings/at settlement. NOTE: carries no changelog or effective date,
    so it pins the CURRENT rates but not when any of them changed.)
  - docs.polymarket.com/polymarket-learn/trading/fees  (authoritative, user-facing
    duplicate of the same table -- agrees on all 11)
  - marketmath.io/blog/polymarket-fees-explained       ("sports ... rose to 0.05
    in July 2026"; "charged per trade, not on winnings at settlement")
  - pineanalytics.substack.com/p/polymarket-fee-rollout (March-2026 snapshot;
    its "peak effective" figures are exactly rate/4 -- sports 0.75% -> 0.03,
    crypto 1.80% -> 0.072, tech 1.00% -> 0.04 -- independently corroborating
    both the March baseline and that sports later moved)
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.fee_model import (  # noqa: E402
    TAKER_RATE,
    fee_per_share,
    taker_fee_fraction,
)

# The documented schedule. Update ONLY alongside a re-verified source + date.
DOCUMENTED = {
    "crypto": 0.07,
    "sports": 0.05,
    "finance": 0.04,
    "politics": 0.04,
    "mentions": 0.04,
    "tech": 0.04,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "other": 0.05,
    "geopolitics": 0.0,
}


@pytest.mark.parametrize("category,rate", sorted(DOCUMENTED.items()))
def test_taker_rate_matches_documented_schedule(category, rate):
    assert category in TAKER_RATE, f"{category!r} missing from TAKER_RATE"
    assert TAKER_RATE[category] == pytest.approx(rate, abs=1e-9), (
        f"{category}: table has {TAKER_RATE[category]}, docs say {rate}"
    )


def test_no_undocumented_categories_have_crept_in():
    """A category with no documented source is a guess. Catch it here."""
    assert set(TAKER_RATE) == set(DOCUMENTED), (
        f"undocumented: {sorted(set(TAKER_RATE) - set(DOCUMENTED))}; "
        f"missing: {sorted(set(DOCUMENTED) - set(TAKER_RATE))}"
    )


def test_sports_is_the_july_2026_rate_not_the_march_launch_rate():
    """Explicit guard on the exact drift that motivated this file."""
    assert TAKER_RATE["sports"] != 0.03, "sports reverted to the stale March rate"
    assert TAKER_RATE["sports"] == pytest.approx(0.05, abs=1e-9)


# --- formula shape --------------------------------------------------------

@pytest.mark.parametrize("category", sorted(DOCUMENTED))
@pytest.mark.parametrize("p", [0.2, 0.5, 0.73])
def test_fee_is_rate_times_p_times_one_minus_p(category, p):
    expected = DOCUMENTED[category] * p * (1.0 - p)
    assert taker_fee_fraction(p, "polymarket", category) == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize("category", ["sports", "crypto", "politics"])
def test_fee_is_symmetric_around_one_half(category):
    assert (taker_fee_fraction(0.2, "polymarket", category)
            == taker_fee_fraction(0.8, "polymarket", category))


@pytest.mark.parametrize("category", ["sports", "crypto"])
def test_settlement_is_free_fee_vanishes_at_the_endpoints(category):
    """'0% on winnings' is structural: rate*p*(1-p) is 0 at p=0 and p=1."""
    assert taker_fee_fraction(0.0, "polymarket", category) == 0.0
    assert taker_fee_fraction(1.0, "polymarket", category) == 0.0


def test_makers_never_pay():
    for category in DOCUMENTED:
        assert taker_fee_fraction(0.5, "polymarket", category, maker=True) == 0.0
        assert fee_per_share(0.5, category, maker=True) == 0.0


def test_geopolitics_is_fee_free_at_every_price():
    for p in (0.01, 0.25, 0.5, 0.99):
        assert taker_fee_fraction(p, "polymarket", "geopolitics") == 0.0
        assert fee_per_share(p, "geopolitics") == 0.0


def test_sports_fee_understatement_regression_is_gone():
    """At p=0.5 the correct sports fee is 1.25pp, not the old 0.75pp."""
    assert taker_fee_fraction(0.5, "polymarket", "sports") * 100 == pytest.approx(1.25, abs=1e-6)

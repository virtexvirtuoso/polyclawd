"""Regression tests: single-market arb fees must come from the fee_model SSOT.

Context (2026-08-26, settled by Mr. V): the repo asserted TWO contradictory
Polymarket fee schedules.

  api/routes/markets.py : poly_fee = profit * 0.02   ("~2% on net winnings")
  execution/fee_model.py: rate * p * (1-p) PER SHARE, PER LEG, 0% on winnings
                          (Source: docs.polymarket.com, verified 2026-06-02)

`fee_model` is the single source of truth -- it is already imported by
odds/edge_math.py, odds/sports_edge_common.py, odds/poly_executable_edge.py and
odds/ufc_prop_edge.py. `markets.py` was the lone outlier, and it under-charged
fees by 19-44x, which made `actionable_count` roughly 40x too loose.

These tests assert COMPUTED VALUES against independently hand-derived numbers,
per the convention established in test_arb_edge_units.py. A tautology that holds
under any cost model is not a test.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.routes.markets import _arb_fee_fraction  # noqa: E402
from execution.fee_model import TAKER_RATE  # noqa: E402


def _mkt(fee_type, fees_enabled=True):
    """Minimal Gamma /markets record carrying only the fee-relevant fields."""
    return {"feeType": fee_type, "feesEnabled": fees_enabled}


# ---------------------------------------------------------------------------
# The core regression: fee basis is per-leg p*(1-p), NOT 2% of winnings.
# ---------------------------------------------------------------------------

def test_fee_is_per_leg_price_based_not_two_percent_of_winnings():
    """Hand-derived, politics market, YES 0.48 / NO 0.48 (total 0.96).

    fee_model: 0.04 * 0.48 * 0.52 = 0.0099840 per leg, two legs = 0.0199680
    old model: profit(0.04) * 0.02 = 0.0008

    The two differ by 25x. Asserting the exact fee_model value means the old
    2%-of-winnings basis cannot pass, and neither can a one-leg variant.
    """
    fee = _arb_fee_fraction(_mkt("politics_fees"), 0.48, 0.48)
    per_leg = 0.04 * 0.48 * 0.52
    assert fee == pytest.approx(per_leg * 2, abs=1e-9)
    # Mutation guard: the discarded 2%-on-winnings value must NOT satisfy this.
    assert fee != pytest.approx((1.0 - 0.96) * 0.02, abs=1e-9)


def test_fee_is_charged_on_both_legs():
    """A single-market arb buys YES *and* NO, so the fee applies twice.

    Asymmetric prices make the one-leg bug visible: charging only the YES leg
    yields 0.0084, only the NO leg 0.00516, both legs 0.01356.
    """
    fee = _arb_fee_fraction(_mkt("politics_fees"), 0.30, 0.65)
    yes_leg = 0.04 * 0.30 * 0.70
    no_leg = 0.04 * 0.65 * 0.35
    assert fee == pytest.approx(yes_leg + no_leg, abs=1e-9)
    assert fee > yes_leg, "fee must exceed either single leg"
    assert fee > no_leg, "fee must exceed either single leg"


@pytest.mark.parametrize("fee_type,category", [
    ("sports_fees_v3", "sports"),
    ("sports_fees_v2", "sports"),
    ("politics_fees", "politics"),
    ("crypto_fees_v2", "crypto"),
    ("economics_fees", "economics"),
    ("culture_fees", "culture"),
    ("weather_fees", "weather"),
    ("finance_prices_fees", "finance"),
    ("tech_fees", "tech"),
])
def test_fee_type_maps_to_its_fee_model_category_rate(fee_type, category):
    """Every feeType observed live on Gamma resolves to its TAKER_RATE entry.

    Sampled 2026-08-26 over the 200 highest-volume open markets; these eight
    strings plus None were the complete observed set.
    """
    fee = _arb_fee_fraction(_mkt(fee_type), 0.40, 0.55)
    expected = TAKER_RATE[category] * (0.40 * 0.60 + 0.55 * 0.45)
    assert fee == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# Fail-closed behaviour on the two "no usable rate" paths.
# ---------------------------------------------------------------------------

def test_fees_disabled_market_pays_zero():
    """feesEnabled=False markets genuinely pay no taker fee.

    Verified 2026-08-26 across 200 markets: feesEnabled=False and feeType=None
    co-occur perfectly (45/45), with zero mixed cases.
    """
    assert _arb_fee_fraction(_mkt(None, fees_enabled=False), 0.48, 0.48) == 0.0


def test_unknown_fee_type_returns_none_rather_than_a_default_rate():
    """An unmapped feeType must fail closed, never take fee_model's 0.05 default.

    poly_executable_edge.py:142 sets the precedent -- "an unknown category must
    not silently" get a fee -- so this returns None and the caller excludes the
    market from actionable_count.

    FEE_RATE_REFRESH_2026_08_26: this originally tripwired on `tech_fees`, which
    was live with NO TAKER_RATE entry, asserting `"tech" not in TAKER_RATE` with
    the note "if tech was added upstream, revisit this". It fired as designed the
    same day, when the documented tech rate (0.04) was sourced and added.
    tech_fees is now mapped, so the fail-closed case rides on feeTypes that are
    genuinely unknown.
    """
    assert _arb_fee_fraction(_mkt("brand_new_fees"), 0.48, 0.48) is None
    assert _arb_fee_fraction(_mkt("sports_fees_v9_unreleased"), 0.48, 0.48) is None


def test_fees_enabled_true_but_fee_type_missing_fails_closed():
    """Never observed live, but the payload could drift. Must not become 0.0."""
    assert _arb_fee_fraction(_mkt(None, fees_enabled=True), 0.48, 0.48) is None


# ---------------------------------------------------------------------------
# The decision this actually changes.
# ---------------------------------------------------------------------------

def test_crypto_arb_that_cleared_the_old_gate_is_now_unprofitable():
    """This is the whole point of the fix.

    A crypto market at YES 0.48 / NO 0.48 shows 4.0pp gross. Two-leg slippage
    is 1.0pp. Under the old 2%-of-winnings fee (0.08pp) it netted +2.92pp and
    cleared the >= 2.0pp gate. Under the real crypto rate (0.07) the fee is
    3.49pp and the trade nets NEGATIVE.
    """
    fee = _arb_fee_fraction(_mkt("crypto_fees_v2"), 0.48, 0.48)
    gross_pp = (1.0 - 0.96) * 100
    slippage_pp = 0.005 * 2 * 100
    net_pp = gross_pp - fee * 100 - slippage_pp
    assert fee * 100 == pytest.approx(3.4944, abs=1e-4)
    assert net_pp < 0, f"expected a loss, got {net_pp:+.2f}pp"
    # And confirm the old basis really would have passed the gate.
    old_net_pp = gross_pp - ((1.0 - 0.96) * 0.02) * 100 - slippage_pp
    assert old_net_pp >= 2.0

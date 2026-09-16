"""Fee-model correctness for cross-platform arb edge.

Pins the verified 2026 fee reality (primary sources: docs.polymarket.com,
kalshi.com fee schedule / CFTC filing):
  - Polymarket taker fee = feeRate * p * (1-p) per share, by category
    (sports 0.03, politics/finance 0.04, crypto 0.07, weather/econ/culture/other 0.05,
     geopolitics/world-events 0.0). 0% on winnings.
  - Kalshi general taker fee = 0.07 * p * (1-p) per contract (peaks 1.75% at 50c).
Both are SYMMETRIC around p=0.5 and shrink to ~0 at the extremes.

Regression target: the old edge_math.FEE_MAP used a flat {polymarket: 0.02,
kalshi: 0.01} with a fee-on-profit term — both wrong, and they overstated
Polymarket cost ~2x, silently killing real cross-platform opportunities.
"""

from execution.fee_model import taker_fee_fraction, KALSHI_TAKER_RATE
from odds.edge_math import net_arb_edge


def test_kalshi_taker_fee_peaks_at_half():
    # 0.07 * 0.5 * 0.5 = 0.0175 (= 1.75% at midpoint)
    assert taker_fee_fraction(0.5, "kalshi") == 0.0175
    assert KALSHI_TAKER_RATE == 0.07


def test_kalshi_fee_symmetric_around_half():
    assert taker_fee_fraction(0.3, "kalshi") == taker_fee_fraction(0.7, "kalshi")


def test_kalshi_fee_shrinks_at_extremes():
    assert taker_fee_fraction(0.05, "kalshi") < taker_fee_fraction(0.5, "kalshi")


def test_polymarket_politics_fee_at_half():
    # politics rate 0.04 -> 0.04 * 0.5 * 0.5 = 0.01
    assert taker_fee_fraction(0.5, "polymarket", "politics") == 0.01


def test_polymarket_world_events_are_free():
    # geopolitics / world events are fee-free on Polymarket
    assert taker_fee_fraction(0.5, "polymarket", "geopolitics") == 0.0


def test_net_arb_edge_uses_real_per_leg_taker_fees():
    # buy PM politics @0.55, "sell" Kalshi @0.72
    r = net_arb_edge(0.55, 0.72, "polymarket", "kalshi", category="politics")
    # PM leg: 0.04 * 0.55 * 0.45 = 0.0099  (NOT old flat 0.55*0.02 = 0.011)
    assert r["buy_fee"] == round(0.04 * 0.55 * 0.45, 6)
    # Kalshi leg: 0.07 * 0.72 * 0.28 = 0.014112 (NOT old profit*0.01 = 0.0017)
    assert r["sell_fee"] == round(0.07 * 0.72 * 0.28, 6)


def test_sell_leg_fee_is_a_taker_fee_not_a_profit_fee():
    # Even when the legs are near-flat (tiny profit), the closing leg is a real
    # taker fill and still incurs a fee. Old model charged 0 when profit<=0.
    r = net_arb_edge(0.60, 0.60, "polymarket", "kalshi", category="politics")
    assert r["sell_fee"] > 0.0

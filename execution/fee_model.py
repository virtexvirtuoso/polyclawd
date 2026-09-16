"""Prediction-market taker-fee model. Pure math, no I/O.

Polymarket: fee = category_rate * p * (1-p) per share, min 0.00001 USDC. Makers pay 0.
  Source: docs.polymarket.com fees (re-verified 2026-08-26; rates pinned by
  tests/test_fee_model_rates.py -- see FEE_RATE_REFRESH_2026_08_26 below).
Kalshi: general taker fee = 0.07 * p * (1-p) per contract; maker = 0.0175 * p * (1-p).
  Source: kalshi.com fee schedule / CFTC filing (verified 2026-06-20).

Both venues are symmetric around p=0.5, charge per-trade (0% on winnings), and
shrink to ~0 at the price extremes. `taker_fee_fraction` is the single source of
truth for cross-platform arb math (see odds.edge_math.net_arb_edge)."""

# FEE_RATE_REFRESH_2026_08_26 -- verified against
# docs.polymarket.com/polymarket-learn/trading/fees, cross-checked against
# marketmath.io and pineanalytics' March-2026 snapshot (whose "peak effective"
# figures are exactly rate/4: sports 0.75% -> 0.03, crypto 1.80% -> 0.072,
# tech 1.00% -> 0.04, independently corroborating the March baseline).
#
# `sports` sat at 0.03 -- the March launch rate -- until Polymarket raised it to
# 0.05 in July 2026. Nothing failed, and sports_edge_common.fee_adjusted_edge
# understated the fee on every sports edge by ~0.42-0.50pp for weeks, loosening
# the alert gate by that much. tests/test_fee_model_rates.py now PINS every
# entry below, so the next drift goes red instead of inflating edge.
#
# Do not edit a rate without a re-verified source and date.
TAKER_RATE = {
    "weather": 0.05,
    "economics": 0.05,
    "culture": 0.05,
    "other": 0.05,
    "sports": 0.05,     # 0.03 -> 0.05, raised by Polymarket July 2026
    "finance": 0.04,
    "politics": 0.04,
    "mentions": 0.04,   # added 2026-08-26, was absent
    "tech": 0.04,       # added 2026-08-26, was absent (silently took .get default 0.05)
    "crypto": 0.07,
    "geopolitics": 0.0,
}
# Kalshi general fee schedule (round-up-to-cent applied per discrete contract;
# the continuous fraction below is the correct basis for edge gating).
KALSHI_TAKER_RATE = 0.07
KALSHI_MAKER_RATE = 0.0175
_MIN_FEE = 0.00001


def fee_per_share(price: float, category: str = "weather", maker: bool = False) -> float:
    """Return fee per share for a taker fill at `price` in `category`.

    Formula: rate * p * (1-p), minimum _MIN_FEE (unless rate is zero).
    Maker fills are always free.
    """
    if maker:
        return 0.0
    rate = TAKER_RATE.get(category, 0.05)
    if rate == 0.0:
        return 0.0
    # Round to kill float-multiply asymmetry: rate*p*(1-p) is not bit-identical to
    # rate*(1-p)*p, so an unrounded fee breaks symmetry (fee(0.2) != fee(0.8)).
    fee = round(rate * price * (1.0 - price), 10)
    return max(fee, _MIN_FEE)


def taker_fee_fraction(price: float, platform: str, category: str = "politics", maker: bool = False) -> float:
    """Taker fee as a fraction of $1 contract face value, unified across platforms.

    Polymarket: TAKER_RATE[category] * p * (1-p)  (world-events/geopolitics = 0).
    Kalshi: KALSHI_TAKER_RATE * p * (1-p) (general schedule), maker = KALSHI_MAKER_RATE.

    Single source of truth for cross-platform arb gating. Returns 0.0 for an
    unknown platform so a missing mapping fails safe (no phantom fee), and for
    maker fills on Polymarket (makers pay 0). Symmetric around p=0.5.
    """
    p = price
    if platform == "polymarket":
        if maker:
            return 0.0
        rate = TAKER_RATE.get(category, 0.05)
    elif platform == "kalshi":
        rate = KALSHI_MAKER_RATE if maker else KALSHI_TAKER_RATE
    else:
        return 0.0
    # round to kill float-multiply asymmetry (see fee_per_share)
    return round(rate * p * (1.0 - p), 10)


def leg_fee(shares: float, price: float, category: str = "weather", maker: bool = False) -> float:
    """Fee for a SINGLE leg (entry OR exit) of `shares` at `price` in `category`.

    A round trip (maker entry + taker exit) costs two separate leg fees at their
    respective fill prices/liquidity — call this once per leg.
    """
    return shares * fee_per_share(price, category, maker)

"""Prediction-market taker-fee model. Pure math, no I/O.

Polymarket: fee = category_rate * p * (1-p) per share, min 0.00001 USDC. Makers pay 0.
  Source: docs.polymarket.com fees (verified 2026-06-02).
Kalshi: general taker fee = 0.07 * p * (1-p) per contract; maker = 0.0175 * p * (1-p).
  Source: kalshi.com fee schedule / CFTC filing (verified 2026-06-20).

Both venues are symmetric around p=0.5, charge per-trade (0% on winnings), and
shrink to ~0 at the price extremes. `taker_fee_fraction` is the single source of
truth for cross-platform arb math (see odds.edge_math.net_arb_edge)."""

TAKER_RATE = {
    "weather": 0.05,
    "economics": 0.05,
    "culture": 0.05,
    "other": 0.05,
    "sports": 0.03,
    "finance": 0.04,
    "politics": 0.04,
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

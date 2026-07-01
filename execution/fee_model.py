"""Polymarket taker-fee model. Pure math, no I/O.
Fee = category_rate * p * (1-p) per share, min 0.00001 USDC. Makers pay 0.
Source: docs.polymarket.com fees (verified 2026-06-02)."""

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


def leg_fee(shares: float, price: float, category: str = "weather", maker: bool = False) -> float:
    """Fee for a SINGLE leg (entry OR exit) of `shares` at `price` in `category`.

    A round trip (maker entry + taker exit) costs two separate leg fees at their
    respective fill prices/liquidity — call this once per leg.
    """
    return shares * fee_per_share(price, category, maker)

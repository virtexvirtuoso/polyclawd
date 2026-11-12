"""Fill identity helpers, standard library only.

Two things key on these: the fast poll's cross-poll dedup
(scripts/smart_wallet_fast_poll.py) and the alert accumulator's per-trade dedup
(scripts/smart_wallet_alert.py). They live in this leaf module so the fast poll
can use them without importing the alert module (config.data_api,
signals.alert_dispatch, ...) at import time.
"""


def _norm(v) -> str:
    """Normalise a numeric field so the same value keys identically across polls.

    The PM feed is JSON: size/price can arrive as int, float or string, so a bare
    str() makes 100, 100.0 and "100" three different keys for ONE fill — which
    defeats the dedup on exactly the repeats it exists to catch. %.10g collapses
    those to "100" while keeping genuinely different sizes distinct.
    """
    if v is None or v == "":
        return ""
    try:
        return f"{float(v):.10g}"
    except (TypeError, ValueError):
        return str(v)


def _trade_key(t: dict) -> str:
    """Stable identity for one fill.

    transactionHash alone is not unique — one transaction can carry several fills
    (measured: 7,500 trades / 4,856 distinct hashes) — so qualify it with the
    asset, wallet, side and size. Numeric fields go through _norm so JSON type
    drift across polls cannot fork the key.
    """
    return "|".join((
        str(t.get("transactionHash", "")),
        str(t.get("asset", "")),
        str(t.get("proxyWallet", "")),
        str(t.get("side", "")),
        _norm(t.get("size")),
        _norm(t.get("price")),
    ))

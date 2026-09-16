"""
HF Hyperliquid — HYPE perpetual data from Hyperliquid public API

Fetches:
  - HYPE perp mark price, funding rate, open interest
  - Aggregated trade flow (buy/sell volume in last 15min)
  - Top whale positions if available

Exposes:
  - get_hype_signal() → dict with all current data
  - get_hype_funding() → funding rate only
  - get_hype_oi() → open interest trend

Used by:
  - hf_enrichment: adds HYPE-specific signal to confluence when evaluating HYPE markets
  - hf_window_snapshot: captures HYPE deriv data at window opens

All calls use the public Hyperliquid Info API (no auth required).
"""

import json
import time
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional

from loguru import logger

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
HYPE_COIN = "HYPE"

# Cache state (module-level, avoids hammering the API)
_cache: Dict = {}
_cache_ts: float = 0
_CACHE_TTL = 45  # seconds — refresh faster than 1min HF tick


def _post(payload: Dict, timeout: int = 8) -> Optional[Dict]:
    """POST to Hyperliquid info endpoint."""
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            HL_INFO_URL,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Polyclawd-HLFeed/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug(f"Hyperliquid API error: {e}")
        return None


def _get_meta_and_ctx() -> Optional[Dict]:
    """Fetch all perp metadata and asset contexts in one call."""
    data = _post({"type": "metaAndAssetCtxs"})
    if not data or len(data) < 2:
        return None
    meta = data[0]
    ctxs = data[1]

    # Find HYPE index
    universe = meta.get("universe", [])
    hype_idx = None
    for i, asset in enumerate(universe):
        if asset.get("name") == HYPE_COIN:
            hype_idx = i
            break

    if hype_idx is None or hype_idx >= len(ctxs):
        logger.debug("HYPE not found in HL universe")
        return None

    return ctxs[hype_idx]


def _get_recent_trades(limit: int = 50) -> List[Dict]:
    """Fetch recent HYPE trades for flow analysis."""
    data = _post({"type": "recentTrades", "coin": HYPE_COIN})
    if not isinstance(data, list):
        return []
    return data[:limit]


def _compute_flow(trades: List[Dict]) -> Dict:
    """Compute buy/sell flow imbalance from recent trades."""
    buy_vol = 0.0
    sell_vol = 0.0
    cutoff = time.time() * 1000 - 15 * 60 * 1000  # 15min window

    for t in trades:
        ts = t.get("time", 0)
        if ts < cutoff:
            continue
        px = float(t.get("px", 0))
        sz = float(t.get("sz", 0))
        notional = px * sz
        # Hyperliquid: side="B" = buy, "A" = sell (aggressor)
        if t.get("side") == "B":
            buy_vol += notional
        else:
            sell_vol += notional

    total = buy_vol + sell_vol
    if total > 0:
        imbalance = (buy_vol - sell_vol) / total  # -1 to +1
    else:
        imbalance = 0.0

    return {
        "buy_vol_15m": round(buy_vol, 0),
        "sell_vol_15m": round(sell_vol, 0),
        "flow_imbalance": round(imbalance, 3),  # positive = buy pressure
        "flow_bias": "bullish" if imbalance > 0.1 else "bearish" if imbalance < -0.1 else "neutral",
    }


def _classify_oi_trend(oi_current: float, oi_prev: Optional[float] = None) -> str:
    """Classify OI trend. Without historical data, use absolute threshold."""
    if oi_prev is None:
        return "unknown"
    pct_change = (oi_current - oi_prev) / oi_prev if oi_prev else 0
    if pct_change > 0.02:
        return "rising"
    elif pct_change < -0.02:
        return "falling"
    return "flat"


def _refresh_cache() -> Dict:
    global _cache, _cache_ts

    ctx = _get_meta_and_ctx()
    trades = _get_recent_trades()
    flow = _compute_flow(trades) if trades else {}

    if ctx is None:
        logger.warning("HYPE: failed to fetch HL meta/ctx")
        _cache = {"error": "fetch_failed", "ts": time.time()}
        _cache_ts = time.time()
        return _cache

    # Parse context fields
    mark_price = float(ctx.get("markPx", 0))
    funding_rate_raw = ctx.get("funding")
    funding_rate = float(funding_rate_raw) if funding_rate_raw is not None else None
    open_interest = float(ctx.get("openInterest", 0))
    prev_day_px = float(ctx.get("prevDayPx", mark_price) or mark_price)

    # Funding annualized (HL gives 1h rate, 8760 hours/year)
    funding_8h = funding_rate * 8 if funding_rate is not None else None  # 8h rate
    funding_annualized = funding_rate * 8760 if funding_rate is not None else None

    # Price change
    price_change_pct = (mark_price - prev_day_px) / prev_day_px * 100 if prev_day_px else 0

    # OI-based signal
    oi_prev = _cache.get("open_interest")
    oi_trend = _classify_oi_trend(open_interest, oi_prev)

    # Derive composite signal
    # Rising price + rising OI = strong conviction up
    # Falling price + rising OI = short squeeze risk (bearish)
    # Rising price + falling OI = shorts covering (bullish but weakening)
    signal = "NEUTRAL"
    if flow.get("flow_imbalance", 0) > 0.15 and (funding_rate or 0) >= 0:
        signal = "LONG"
    elif flow.get("flow_imbalance", 0) < -0.15 and (funding_rate or 0) <= 0:
        signal = "SHORT"
    elif (funding_rate or 0) > 0.001:  # positive funding, longs paying → momentum bullish
        signal = "LONG"
    elif (funding_rate or 0) < -0.001:
        signal = "SHORT"

    result = {
        "asset": "HYPE",
        "mark_price": mark_price,
        "price_change_pct_24h": round(price_change_pct, 2),
        "funding_rate_1h": funding_rate,
        "funding_rate_8h": round(funding_8h, 6) if funding_8h is not None else None,
        "funding_rate_annualized": round(funding_annualized, 4) if funding_annualized is not None else None,
        "open_interest": open_interest,
        "oi_trend": oi_trend,
        "flow_imbalance": flow.get("flow_imbalance"),
        "flow_bias": flow.get("flow_bias"),
        "buy_vol_15m": flow.get("buy_vol_15m"),
        "sell_vol_15m": flow.get("sell_vol_15m"),
        "signal": signal,            # 'LONG' / 'SHORT' / 'NEUTRAL'
        "source": "hyperliquid_public",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "ts": time.time(),
    }

    _cache = result
    _cache_ts = time.time()
    logger.debug(
        f"HYPE: mark={mark_price:.4f}, funding_8h={funding_8h:.5f}%, "
        f"OI={open_interest:.0f}, signal={signal}, flow={flow.get('flow_bias')}"
    )
    return result


def get_hype_signal(force_refresh: bool = False) -> Dict:
    """
    Get full HYPE perpetual signal. Cached for 45s.

    Returns dict with mark_price, funding_rate, OI, flow, and composite signal.
    """
    now = time.time()
    if force_refresh or not _cache or now - _cache_ts > _CACHE_TTL:
        return _refresh_cache()
    return _cache


def get_hype_funding() -> Optional[float]:
    """Get HYPE 8h funding rate (positive = longs pay shorts). Returns None on error."""
    data = get_hype_signal()
    return data.get("funding_rate_8h")


def get_hype_oi_trend() -> str:
    """Get HYPE OI trend: 'rising'/'falling'/'flat'/'unknown'."""
    data = get_hype_signal()
    return data.get("oi_trend", "unknown")


def get_hype_mark_price() -> Optional[float]:
    """Get current HYPE mark price from Hyperliquid."""
    data = get_hype_signal()
    return data.get("mark_price")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.DEBUG)
    print("Fetching HYPE signal...")
    sig = get_hype_signal(force_refresh=True)
    print(json.dumps(sig, indent=2))

"""
Whale Wall Scanner — Automated orderbook imbalance detection for Polymarket.

Scans top markets by 24h volume, analyzes full orderbook depth,
flags significant bid/ask imbalances as directional signals.

Feeds into paper portfolio as signal source + Discord alerts.
"""

import json
import logging
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# --- Config ---
MIN_IMBALANCE_RATIO = 3.0    # 3:1 bid/ask ratio to flag
MIN_VOLUME_24H = 50_000      # $50K minimum 24h volume
MIN_LIQUIDITY = 5_000        # $5K minimum total orderbook depth
MIN_MID_PRICE = 0.05         # Skip dead markets (<5¢)
MAX_MID_PRICE = 0.95         # Skip resolved markets (>95¢)
WALL_THRESHOLD_USD = 10_000  # $10K+ single level = wall
TOP_MARKETS = 15             # Scan top N by volume
MAX_BOOK_LEVELS = 100        # Fetch deeper than default 10

# --- Cache ---
_scan_cache: Dict = {"data": None, "ts": 0}
_SCAN_CACHE_TTL = 300  # 5 min


def _fetch_json(url: str, timeout: int = 12) -> Optional[dict]:
    """Fetch JSON with error handling."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug("Fetch failed %s: %s", url[:60], e)
        return None


def _get_top_markets(n: int = TOP_MARKETS) -> List[dict]:
    """Get top N Polymarket events by 24h volume."""
    url = f"{GAMMA_API}/events?active=true&closed=false&limit={n}&order=volume24hr&ascending=false"
    data = _fetch_json(url)
    if not data or not isinstance(data, list):
        return []

    markets = []
    for event in data:
        for m in event.get("markets", [event]):
            volume = float(m.get("volume24hr", 0) or m.get("volumeNum", 0) or 0)
            if volume < MIN_VOLUME_24H:
                continue

            # Get token IDs
            clob_ids = m.get("clobTokenIds", "[]")
            if isinstance(clob_ids, str):
                try:
                    clob_ids = json.loads(clob_ids)
                except Exception:
                    clob_ids = []

            outcomes = m.get("outcomes", "[]")
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except Exception:
                    outcomes = []

            # Get YES price
            prices = m.get("outcomePrices", "[]")
            if isinstance(prices, str):
                try:
                    prices = json.loads(prices)
                except Exception:
                    prices = []

            yes_price = float(prices[0]) if prices else 0.5

            if not clob_ids or len(clob_ids) < 1:
                continue
            if yes_price < MIN_MID_PRICE or yes_price > MAX_MID_PRICE:
                continue

            markets.append({
                "question": m.get("question", "")[:100],
                "slug": m.get("slug", ""),
                "market_id": m.get("conditionId", m.get("id", "")),
                "yes_token": clob_ids[0],
                "no_token": clob_ids[1] if len(clob_ids) > 1 else None,
                "yes_price": yes_price,
                "volume_24h": volume,
                "liquidity": float(m.get("liquidityNum", 0) or 0),
                "end_date": m.get("endDate", ""),
            })

    # Sort by volume, take top N
    markets.sort(key=lambda x: x["volume_24h"], reverse=True)
    return markets[:n]


def _fetch_full_orderbook(token_id: str) -> Optional[dict]:
    """Fetch orderbook with full depth (not just top 10)."""
    url = f"{CLOB_API}/book?token_id={token_id}"
    data = _fetch_json(url, timeout=10)
    if not data or "error" in data:
        return None

    bids = []
    for b in data.get("bids", [])[:MAX_BOOK_LEVELS]:
        try:
            bids.append({"price": float(b["price"]), "size": float(b["size"])})
        except (KeyError, ValueError):
            continue

    asks = []
    for a in data.get("asks", [])[:MAX_BOOK_LEVELS]:
        try:
            asks.append({"price": float(a["price"]), "size": float(a["size"])})
        except (KeyError, ValueError):
            continue

    return {"bids": bids, "asks": asks}


def _analyze_depth(book: dict, yes_price: float) -> dict:
    """Analyze orderbook depth for imbalances and walls."""
    bids = book.get("bids", [])
    asks = book.get("asks", [])

    if not bids and not asks:
        return {}

    # Dollar-weighted depth
    bid_depth = sum(b["price"] * b["size"] for b in bids)
    ask_depth = sum((1 - a["price"]) * a["size"] for a in asks)  # Cost to buy ask side
    bid_size = sum(b["size"] for b in bids)
    ask_size = sum(a["size"] for a in asks)

    total = bid_depth + ask_depth
    if total < MIN_LIQUIDITY:
        return {}

    # Imbalance ratio
    ratio = bid_depth / ask_depth if ask_depth > 0 else 999
    inv_ratio = ask_depth / bid_depth if bid_depth > 0 else 999

    # Detect walls ($10K+ at a single level)
    bid_walls = [b for b in bids if b["price"] * b["size"] >= WALL_THRESHOLD_USD]
    ask_walls = [a for a in asks if (1 - a["price"]) * a["size"] >= WALL_THRESHOLD_USD]

    # Largest single wall
    max_bid_wall = max((b["price"] * b["size"] for b in bids), default=0)
    max_ask_wall = max(((1 - a["price"]) * a["size"] for a in asks), default=0)

    # Spread
    best_bid = bids[0]["price"] if bids else 0
    best_ask = asks[0]["price"] if asks else 1
    spread = best_ask - best_bid

    # Direction signal
    if ratio >= MIN_IMBALANCE_RATIO:
        direction = "BID_HEAVY"
        signal_side = "YES"
        imbalance_ratio = ratio
    elif inv_ratio >= MIN_IMBALANCE_RATIO:
        direction = "ASK_HEAVY"
        signal_side = "NO"
        imbalance_ratio = inv_ratio
    else:
        direction = "BALANCED"
        signal_side = None
        imbalance_ratio = max(ratio, inv_ratio)

    return {
        "bid_depth_usd": round(bid_depth, 2),
        "ask_depth_usd": round(ask_depth, 2),
        "bid_levels": len(bids),
        "ask_levels": len(asks),
        "bid_size": round(bid_size, 2),
        "ask_size": round(ask_size, 2),
        "imbalance_ratio": round(imbalance_ratio, 1),
        "direction": direction,
        "signal_side": signal_side,
        "spread": round(spread, 4),
        "spread_cents": round(spread * 100, 2),
        "bid_walls": len(bid_walls),
        "ask_walls": len(ask_walls),
        "max_bid_wall_usd": round(max_bid_wall, 2),
        "max_ask_wall_usd": round(max_ask_wall, 2),
        "total_depth_usd": round(total, 2),
    }


def scan_whale_walls(top_n: int = TOP_MARKETS) -> dict:
    """
    Scan top Polymarket markets for orderbook imbalances.

    Returns:
        {
            "scanned": int,
            "alerts": [market dicts with imbalance >= 3:1],
            "all_markets": [all scanned markets with depth data],
            "scan_time": float,
        }
    """
    now = time.time()

    # Cache check
    if _scan_cache["data"] and (now - _scan_cache["ts"]) < _SCAN_CACHE_TTL:
        return _scan_cache["data"]

    t0 = time.time()
    markets = _get_top_markets(top_n)
    if not markets:
        return {"scanned": 0, "alerts": [], "all_markets": [], "scan_time": 0}

    # Fetch all orderbooks in parallel
    def _fetch_market_book(m):
        book = _fetch_full_orderbook(m["yes_token"])
        if not book:
            return None
        analysis = _analyze_depth(book, m["yes_price"])
        if not analysis:
            return None
        return {**m, **analysis}

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_fetch_market_book, markets))

    all_markets = [r for r in results if r]
    alerts = [m for m in all_markets if m.get("direction") in ("BID_HEAVY", "ASK_HEAVY")]

    # Sort alerts by imbalance ratio
    alerts.sort(key=lambda x: x.get("imbalance_ratio", 0), reverse=True)

    result = {
        "scanned": len(markets),
        "alerts": alerts,
        "all_markets": all_markets,
        "scan_time": round(time.time() - t0, 2),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    _scan_cache["data"] = result
    _scan_cache["ts"] = time.time()

    logger.info("Whale wall scan: %d markets, %d alerts (%.1fs)",
                len(markets), len(alerts), result["scan_time"])
    return result


def get_whale_portfolio_signals(min_imbalance: float = 3.0, max_signals: int = 3) -> List[dict]:
    """
    Get whale wall signals formatted for paper_portfolio.process_signals().

    Only returns signals where:
    - Imbalance ratio >= min_imbalance
    - Market has meaningful mid-price (5-95¢)
    - Sufficient liquidity ($5K+)
    """
    scan = scan_whale_walls()
    alerts = scan.get("alerts", [])
    if not alerts:
        return []

    signals = []
    for m in alerts[:max_signals]:
        if m.get("imbalance_ratio", 0) < min_imbalance:
            continue

        side = m.get("signal_side", "YES")
        yes_price = m.get("yes_price", 0.5)
        entry_price = yes_price if side == "YES" else (1 - yes_price)

        # Confidence from imbalance strength
        ratio = m.get("imbalance_ratio", 3.0)
        confidence = min(0.80, 0.55 + (ratio - 3.0) * 0.05)  # 3:1 = 55%, 8:1 = 80%

        # Edge estimate: imbalance suggests true price differs from mid
        # Conservative: 5% base + 1% per ratio point above 3
        edge_pct = 5.0 + max(0, (ratio - 3.0)) * 1.0
        edge_pct = min(edge_pct, 20.0)  # Cap at 20%

        signals.append({
            "market_id": m.get("market_id", ""),
            "market": m.get("question", "")[:120],
            "market_title": m.get("question", "")[:120],
            "side": side,
            "entry_price": entry_price,
            "market_price": yes_price,
            "confidence": confidence,
            "edge_pct": edge_pct,
            "strategy": "whale_wall",
            "archetype": "orderbook_flow",
            "platform": "polymarket",
            "source": "whale_wall_scanner",
            "slug": m.get("slug", ""),
            "days_to_close": 7,  # Default, could be refined from end_date
            "volume": m.get("volume_24h", 0),
            "whale_detail": {
                "imbalance_ratio": m.get("imbalance_ratio"),
                "direction": m.get("direction"),
                "bid_depth_usd": m.get("bid_depth_usd"),
                "ask_depth_usd": m.get("ask_depth_usd"),
                "bid_walls": m.get("bid_walls"),
                "ask_walls": m.get("ask_walls"),
                "max_bid_wall_usd": m.get("max_bid_wall_usd"),
                "max_ask_wall_usd": m.get("max_ask_wall_usd"),
                "spread_cents": m.get("spread_cents"),
            },
        })

    logger.info("Whale wall signals: %d/%d pass min_imbalance=%.1f",
                len(signals), len(alerts), min_imbalance)
    return signals

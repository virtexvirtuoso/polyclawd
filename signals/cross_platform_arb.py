#!/usr/bin/env python3
"""
Cross-Platform Arbitrage Scanner — Kalshi vs Polymarket.

Uses TF-IDF + cosine similarity for semantic matching of markets across platforms,
then compares prices to find arbitrage opportunities.

Becker dataset analysis showed cross-platform spreads exist but require semantic
matching (keyword overlap fails due to different market phrasing styles).
"""

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Thresholds
MIN_SPREAD_PCT = 5       # Minimum spread in percentage points to flag as arb
MIN_SIMILARITY = 0.35    # Minimum cosine similarity to consider a match
MIN_VOLUME = 10000       # Minimum volume on both sides
MAX_RESULTS = 50         # Maximum arb opportunities to return

# Cache
_cache: Dict = {"data": None, "timestamp": 0}
CACHE_TTL = 300  # 5 min

# Stopwords for TF-IDF
STOPWORDS = {
    'will', 'the', 'a', 'an', 'in', 'on', 'at', 'to', 'of', 'by', 'or', 'and',
    'be', 'is', 'for', 'any', 'other', 'another', 'before', 'after', 'than',
    'this', 'that', 'does', 'do', 'has', 'have', 'their', 'more', 'less',
    'win', 'not', 'above', 'below', 'between', 'what', 'how', 'when', 'where',
    'which', 'who', 'whom', 'yes', 'no',
}


def _tokenize(text: str) -> List[str]:
    """Extract meaningful tokens from market title."""
    text = text.lower()
    # Extract numbers with units (e.g., "$64,000", "1,800")
    text = re.sub(r'[$,]', '', text)
    tokens = re.findall(r'[a-z]+|[0-9]+', text)
    return [t for t in tokens if t not in STOPWORDS and len(t) > 1]


def _cosine_similarity(tokens_a: List[str], tokens_b: List[str]) -> float:
    """Simple TF-based cosine similarity between two token lists."""
    if not tokens_a or not tokens_b:
        return 0.0
    
    # Build term frequency vectors
    vocab = set(tokens_a) | set(tokens_b)
    vec_a = {t: tokens_a.count(t) for t in vocab}
    vec_b = {t: tokens_b.count(t) for t in vocab}
    
    dot = sum(vec_a.get(t, 0) * vec_b.get(t, 0) for t in vocab)
    mag_a = sum(v ** 2 for v in vec_a.values()) ** 0.5
    mag_b = sum(v ** 2 for v in vec_b.values()) ** 0.5
    
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _fetch_json(url: str, params: dict = None, timeout: int = 15) -> Optional[dict]:
    """Fetch JSON with error handling."""
    try:
        r = httpx.get(url, params=params, timeout=timeout,
                      headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"Fetch failed {url}: {e}")
        return None


def fetch_kalshi_active(limit: int = 200) -> List[Dict]:
    """Fetch active Kalshi markets with volume by scanning popular series."""
    markets = []
    seen_tickers = set()
    
    # Popular Kalshi series that overlap with Polymarket topics
    POPULAR_SERIES = [
        "KXBTCD", "KXBTC", "KXETHD",       # Crypto prices
        "KXPRES", "KXSENATE", "KXHOUSE",     # Elections
        "KXFEDRATE",                          # Fed rates
        "KXRECESSION",                        # Recession
        "KXCPI", "KXGDP",                    # Economic indicators
        "KXTIKTOK", "KXAI",                   # Tech/AI
        "KXNBA", "KXNFL", "KXMLB",          # Sports
        "KXOSCAR", "KXGRAMMY",               # Entertainment
        "KXSPACEX",                           # Space
    ]
    
    # Also do a general scan with pagination
    for series in POPULAR_SERIES + [None]:  # None = general scan
        cursor = None
        for page in range(3):
            params = {"status": "open", "limit": 100}
            if series:
                params["series_ticker"] = series
            if cursor:
                params["cursor"] = cursor
            
            data = _fetch_json(f"{KALSHI_API}/markets", params=params)
            if not data:
                break
            
            page_markets = data.get("markets", [])
            if not page_markets:
                break
            
            for m in page_markets:
                ticker = m.get("ticker", "")
                if ticker in seen_tickers:
                    continue
                seen_tickers.add(ticker)
                
                vol = m.get("volume", 0) or 0
                last_price = m.get("last_price", 0) or 0
                if vol >= MIN_VOLUME and 5 <= last_price <= 95:
                    markets.append({
                        "id": ticker,
                        "title": m.get("title", ""),
                        "price_yes": last_price / 100,
                        "volume": vol,
                        "close_time": m.get("close_time", ""),
                        "platform": "kalshi",
                    })
            
            cursor = data.get("cursor")
            if not cursor or len(markets) >= limit:
                break
        
        if len(markets) >= limit:
            break
    
    return markets


def fetch_polymarket_active(limit: int = 200) -> List[Dict]:
    """Fetch active Polymarket markets with decent volume."""
    markets = []
    
    # Flat markets
    data = _fetch_json(f"{GAMMA_API}/markets", params={
        "active": "true", "closed": "false",
        "limit": limit, "order": "volume24hr", "ascending": "false"
    })
    if data:
        for m in data:
            vol = float(m.get("volume", 0) or 0)
            if vol < MIN_VOLUME:
                continue
            
            op = m.get("outcomePrices", "")
            try:
                if isinstance(op, str):
                    op = json.loads(op)
                yes_price = float(op[0])
            except Exception:
                yes_price = 0.5
            
            if 0.05 <= yes_price <= 0.95:
                markets.append({
                    "id": m.get("conditionId", m.get("id", "")),
                    "title": m.get("question", ""),
                    "price_yes": yes_price,
                    "volume": vol,
                    "close_time": m.get("endDate", ""),
                    "slug": m.get("slug", ""),
                    "platform": "polymarket",
                })
    
    # Events (nested markets)
    events = _fetch_json(f"{GAMMA_API}/events", params={
        "active": "true", "closed": "false",
        "limit": 50, "order": "volume24hr", "ascending": "false"
    })
    if events:
        seen = {m["id"] for m in markets}
        for event in events:
            for m in event.get("markets", []):
                mid = m.get("conditionId", m.get("id", ""))
                if mid in seen or not m.get("active") or m.get("closed"):
                    continue
                
                vol = float(m.get("volume", 0) or 0)
                if vol < MIN_VOLUME:
                    continue
                
                op = m.get("outcomePrices", "")
                try:
                    if isinstance(op, str):
                        op = json.loads(op)
                    yes_price = float(op[0])
                except Exception:
                    continue
                
                if 0.05 <= yes_price <= 0.95:
                    markets.append({
                        "id": mid,
                        "title": m.get("question", ""),
                        "price_yes": yes_price,
                        "volume": vol,
                        "close_time": m.get("endDate", ""),
                        "slug": m.get("slug", ""),
                        "platform": "polymarket",
                    })
                    seen.add(mid)
    
    return markets


def find_arb_opportunities(
    kalshi_markets: List[Dict],
    poly_markets: List[Dict],
    min_spread: float = MIN_SPREAD_PCT,
    min_sim: float = MIN_SIMILARITY,
) -> List[Dict]:
    """Find cross-platform arbitrage opportunities using semantic matching.
    
    Uses inverted index for efficient O(n*k) matching instead of O(n*m) brute force.
    """
    # Build inverted index on Polymarket tokens
    poly_index = {}  # token -> list of (market_idx, token_list)
    poly_tokens = []
    for i, pm in enumerate(poly_markets):
        toks = _tokenize(pm["title"])
        poly_tokens.append(toks)
        for tok in set(toks):  # unique tokens only
            poly_index.setdefault(tok, []).append(i)
    
    opportunities = []
    
    for km in kalshi_markets:
        k_tokens = _tokenize(km["title"])
        if len(k_tokens) < 2:
            continue
        
        # Find candidate Polymarket markets via inverted index
        # (markets sharing at least 2 tokens)
        candidate_counts = {}
        for tok in set(k_tokens):
            for pi in poly_index.get(tok, []):
                candidate_counts[pi] = candidate_counts.get(pi, 0) + 1
        
        for pi, shared_count in candidate_counts.items():
            if shared_count < 2:
                continue
            
            sim = _cosine_similarity(k_tokens, poly_tokens[pi])
            if sim < min_sim:
                continue
            
            pm = poly_markets[pi]
            
            # Calculate spread (both directions)
            # If Kalshi YES=70% and Poly YES=60%, spread=10pp
            spread = abs(km["price_yes"] - pm["price_yes"]) * 100
            
            if spread < min_spread:
                continue
            
            # Determine arb direction
            if km["price_yes"] > pm["price_yes"]:
                arb_direction = "Buy YES on Polymarket, sell YES on Kalshi"
                buy_platform = "polymarket"
                buy_price = pm["price_yes"]
                sell_price = km["price_yes"]
            else:
                arb_direction = "Buy YES on Kalshi, sell YES on Polymarket"
                buy_platform = "kalshi"
                buy_price = km["price_yes"]
                sell_price = pm["price_yes"]
            
            opportunities.append({
                "kalshi_title": km["title"][:100],
                "kalshi_id": km["id"],
                "kalshi_price": round(km["price_yes"] * 100, 1),
                "kalshi_volume": km["volume"],
                "poly_title": pm["title"][:100],
                "poly_id": pm["id"],
                "poly_price": round(pm["price_yes"] * 100, 1),
                "poly_volume": pm["volume"],
                "poly_slug": pm.get("slug", ""),
                "spread_pp": round(spread, 1),
                "similarity": round(sim, 3),
                "shared_tokens": shared_count,
                "direction": arb_direction,
                "buy_platform": buy_platform,
                "buy_price": round(buy_price * 100, 1),
                "sell_price": round(sell_price * 100, 1),
                "min_volume": min(km["volume"], pm["volume"]),
            })
    
    # Sort by spread descending, filter top results
    opportunities.sort(key=lambda x: x["spread_pp"], reverse=True)
    
    # Deduplicate: keep best spread per Kalshi market
    seen_kalshi = set()
    unique = []
    for opp in opportunities:
        if opp["kalshi_id"] not in seen_kalshi:
            seen_kalshi.add(opp["kalshi_id"])
            unique.append(opp)
    
    return unique[:MAX_RESULTS]


def scan_cross_platform_arb() -> Dict:
    """Main entry point: fetch markets from both platforms and find arb opportunities."""
    now = time.time()
    if _cache["data"] and (now - _cache["timestamp"]) < CACHE_TTL:
        return _cache["data"]
    
    logger.info("Scanning cross-platform arb opportunities...")
    
    kalshi = fetch_kalshi_active(limit=200)
    poly = fetch_polymarket_active(limit=200)
    
    logger.info(f"Fetched {len(kalshi)} Kalshi + {len(poly)} Polymarket active markets")
    
    arbs = find_arb_opportunities(kalshi, poly)
    
    result = {
        "kalshi_markets": len(kalshi),
        "poly_markets": len(poly),
        "arb_opportunities": len(arbs),
        "arbs": arbs,
        "avg_spread": round(sum(a["spread_pp"] for a in arbs) / max(len(arbs), 1), 1),
        "max_spread": arbs[0]["spread_pp"] if arbs else 0,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    
    _cache["data"] = result
    _cache["timestamp"] = now
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    print("=== Cross-Platform Arb Scanner ===\n")
    result = scan_cross_platform_arb()
    
    print(f"Kalshi: {result['kalshi_markets']} markets")
    print(f"Polymarket: {result['poly_markets']} markets")
    print(f"Arb opportunities: {result['arb_opportunities']}")
    print(f"Avg spread: {result['avg_spread']}pp")
    print(f"Max spread: {result['max_spread']}pp")
    
    for arb in result["arbs"][:10]:
        print(f"\n  K: {arb['kalshi_title'][:70]}")
        print(f"  P: {arb['poly_title'][:70]}")
        print(f"  Kalshi: {arb['kalshi_price']}¢ | Poly: {arb['poly_price']}¢ | Spread: {arb['spread_pp']}pp | Sim: {arb['similarity']}")

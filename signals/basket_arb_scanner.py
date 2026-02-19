"""
Sum-to-One Basket Arbitrage Scanner for Polymarket.

Scans multi-outcome events where the sum of all outcome prices < $1.00,
meaning buying all outcomes guarantees profit.

This is the #1 profitable pattern on Polymarket per X/Twitter alpha.

Signal source for Polyclawd pipeline.
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"

# Thresholds
MIN_ARB_EDGE_PCT = 0.5          # Minimum guaranteed profit % (after fees)
MAX_ARB_EDGE_PCT = 15.0         # Skip if too good to be true (likely stale/illiquid)
MIN_EVENT_VOLUME = 10000        # Minimum event volume in USD
MIN_MARKET_LIQUIDITY = 1000     # Minimum per-outcome liquidity
POLYMARKET_FEE_PCT = 2.0        # ~2% fee on winnings (conservative estimate)
MAX_OUTCOMES = 30               # Skip events with too many outcomes (liquidity spread thin)
MIN_OUTCOMES = 2                # Need at least 2 outcomes

# Cache
_cache: Dict = {"data": None, "timestamp": 0}
CACHE_TTL = 30  # 30s cache — arb windows close fast


def _fetch_events(limit: int = 100) -> List[Dict]:
    """Fetch active events with multiple outcomes from Gamma API."""
    try:
        r = httpx.get(
            f"{GAMMA_API}/events",
            params={
                "active": "true",
                "closed": "false",
                "limit": limit,
                "order": "volume24hr",
                "ascending": "false",
            },
            timeout=20,
            headers={"User-Agent": "Polyclawd/1.0"},
        )
        if r.status_code == 200:
            return r.json()
        logger.warning(f"Gamma events API returned {r.status_code}")
        return []
    except Exception as e:
        logger.error(f"Gamma events fetch failed: {e}")
        return []


def _get_outcome_prices(market: Dict) -> Tuple[Optional[float], Optional[float]]:
    """Extract YES and NO prices from a market."""
    prices = market.get("outcomePrices")
    if not prices:
        return None, None
    try:
        if isinstance(prices, str):
            prices = json.loads(prices)
        yes_price = float(prices[0])
        no_price = float(prices[1]) if len(prices) > 1 else (1.0 - yes_price)
        return yes_price, no_price
    except Exception:
        return None, None


def scan_basket_arb() -> List[Dict]:
    """Scan for sum-to-one arbitrage opportunities.
    
    For each multi-outcome event:
    1. Sum the YES prices of all outcomes
    2. If sum < 1.0 (minus fees), there's guaranteed profit
    3. For 2-outcome markets: check if YES + NO < 1.0
    
    Returns list of arb signals sorted by edge.
    """
    events = _fetch_events(limit=200)
    signals = []
    now = datetime.now(timezone.utc)
    
    for event in events:
        markets = event.get("markets", [])
        if not markets:
            continue
        
        event_title = event.get("title", "")
        event_volume = float(event.get("volume", 0) or 0)
        event_vol24h = float(event.get("volume24hr", 0) or 0)
        
        if event_volume < MIN_EVENT_VOLUME and event_vol24h < MIN_EVENT_VOLUME:
            continue
        
        # Filter: only active, non-closed markets
        active_markets = [
            m for m in markets
            if m.get("active") and not m.get("closed")
        ]
        
        if len(active_markets) < MIN_OUTCOMES or len(active_markets) > MAX_OUTCOMES:
            continue
        
        # === Strategy A: Multi-outcome event basket ===
        # In a neg-risk event (mutually exclusive outcomes), buying YES on all
        # outcomes guarantees exactly one wins → payout = $1
        # If sum of all YES prices < $1, that's arb
        
        if event.get("negRisk") or event.get("enableNegRisk"):
            yes_prices = []
            all_valid = True
            min_liquidity = float("inf")
            
            for m in active_markets:
                yp, _ = _get_outcome_prices(m)
                if yp is None or yp <= 0:
                    all_valid = False
                    break
                yes_prices.append({
                    "question": m.get("question", "")[:80],
                    "yes_price": yp,
                    "condition_id": m.get("conditionId", ""),
                    "volume": float(m.get("volume", 0) or 0),
                })
                liq = float(m.get("volume", 0) or 0)
                if liq < min_liquidity:
                    min_liquidity = liq
            
            if not all_valid or not yes_prices:
                continue
            
            total_cost = sum(p["yes_price"] for p in yes_prices)
            gross_profit_pct = ((1.0 - total_cost) / total_cost) * 100 if total_cost > 0 else 0
            net_profit_pct = gross_profit_pct - POLYMARKET_FEE_PCT
            
            if net_profit_pct >= MIN_ARB_EDGE_PCT and net_profit_pct <= MAX_ARB_EDGE_PCT:
                signals.append({
                    "type": "basket_arb",
                    "event_title": event_title,
                    "event_id": event.get("id", ""),
                    "num_outcomes": len(yes_prices),
                    "total_cost": round(total_cost, 4),
                    "gross_profit_pct": round(gross_profit_pct, 2),
                    "net_profit_pct": round(net_profit_pct, 2),
                    "event_volume": event_volume,
                    "event_vol24h": event_vol24h,
                    "outcomes": yes_prices,
                    "platform": "polymarket",
                    "source": "basket_arb",
                    "confidence": min(0.95, 0.7 + net_profit_pct / 20),  # High confidence — it's math
                    "generated_at": now.isoformat(),
                })
        
        # === Strategy B: Single-market YES+NO arb ===
        # For individual 2-outcome markets: if YES + NO < 1.0, buy both
        for m in active_markets:
            yp, np = _get_outcome_prices(m)
            if yp is None or np is None:
                continue
            if yp <= 0 or np <= 0:
                continue
            
            total_cost = yp + np
            if total_cost >= 1.0:
                continue  # No arb
            
            gross_profit_pct = ((1.0 - total_cost) / total_cost) * 100
            net_profit_pct = gross_profit_pct - POLYMARKET_FEE_PCT
            
            if net_profit_pct < MIN_ARB_EDGE_PCT or net_profit_pct > MAX_ARB_EDGE_PCT:
                continue
            
            market_vol = float(m.get("volume", 0) or 0)
            if market_vol < MIN_MARKET_LIQUIDITY:
                continue
            
            signals.append({
                "type": "yes_no_arb",
                "event_title": event_title,
                "market_title": m.get("question", "")[:100],
                "condition_id": m.get("conditionId", ""),
                "yes_price": round(yp, 4),
                "no_price": round(np, 4),
                "total_cost": round(total_cost, 4),
                "gross_profit_pct": round(gross_profit_pct, 2),
                "net_profit_pct": round(net_profit_pct, 2),
                "market_volume": market_vol,
                "platform": "polymarket",
                "source": "basket_arb",
                "confidence": min(0.95, 0.7 + net_profit_pct / 20),
                "generated_at": now.isoformat(),
            })
    
    # Sort by net profit descending
    signals.sort(key=lambda x: x["net_profit_pct"], reverse=True)
    
    return signals


def get_basket_arb_signals() -> Dict:
    """Main entry point with caching."""
    now = time.time()
    if _cache["data"] and (now - _cache["timestamp"]) < CACHE_TTL:
        return _cache["data"]
    
    signals = scan_basket_arb()
    
    basket_count = sum(1 for s in signals if s["type"] == "basket_arb")
    yesno_count = sum(1 for s in signals if s["type"] == "yes_no_arb")
    
    result = {
        "signals": signals,
        "total": len(signals),
        "basket_arb_count": basket_count,
        "yes_no_arb_count": yesno_count,
        "strategy": "SumToOneArbitrage",
        "description": "Guaranteed profit when sum of all outcome prices < $1 (minus fees)",
        "fee_assumption_pct": POLYMARKET_FEE_PCT,
        "min_edge_pct": MIN_ARB_EDGE_PCT,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cached": False,
    }
    
    _cache["data"] = {**result, "cached": True}
    _cache["timestamp"] = now
    
    return result


# Bot-war detector
def check_spread_compression(markets: List[Dict], window_minutes: int = 10) -> Dict:
    """Detect if arb spreads are collapsing (bot competition).
    
    If average spread < 2¢ across 5-min markets for >10 min, 
    signal to pause the strategy.
    """
    compressed = []
    for m in markets:
        yp, np = _get_outcome_prices(m)
        if yp is None or np is None:
            continue
        spread = abs(1.0 - yp - np)
        if spread < 0.02:  # < 2¢
            compressed.append({
                "question": m.get("question", "")[:60],
                "spread": round(spread, 4),
                "spread_cents": round(spread * 100, 1),
            })
    
    return {
        "compressed_count": len(compressed),
        "total_checked": len(markets),
        "compression_ratio": len(compressed) / max(len(markets), 1),
        "should_pause": len(compressed) > 5 and (len(compressed) / max(len(markets), 1)) > 0.5,
        "compressed_markets": compressed[:10],
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = get_basket_arb_signals()
    print(f"\n=== Basket Arb Scanner ===")
    print(f"Total signals: {result['total']}")
    print(f"Basket arbs: {result['basket_arb_count']}")
    print(f"YES/NO arbs: {result['yes_no_arb_count']}")
    for sig in result["signals"][:10]:
        if sig["type"] == "basket_arb":
            print(f"\n🎯 BASKET: {sig['event_title'][:60]}")
            print(f"   {sig['num_outcomes']} outcomes, cost=${sig['total_cost']:.4f}, net={sig['net_profit_pct']:.2f}%")
        else:
            print(f"\n💰 YES+NO: {sig['market_title'][:60]}")
            print(f"   YES={sig['yes_price']:.4f} NO={sig['no_price']:.4f} cost=${sig['total_cost']:.4f} net={sig['net_profit_pct']:.2f}%")

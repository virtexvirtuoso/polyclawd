"""
High-Frequency Polymarket Scanner — Phase 1

Three capabilities:
1. Short-duration market discovery (5/15-min crypto Up/Down markets)
2. Negative vig detection (Yes+No < $0.99 = free money)
3. Combined HF opportunities endpoint

Based on: [[Polymarket 134 to 200K Story]] and [[HF_MODULE_PLAN]]
"""

import json
import urllib.request
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Resilient fetch wrapper
try:
    from api.services.resilient_fetch import resilient_call
    HAS_RESILIENT = True
except ImportError:
    HAS_RESILIENT = False


def _fetch_json(source: str, url: str, timeout: int = 15):
    """Fetch JSON with optional resilient wrapper."""
    def _do():
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd-HF/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    if HAS_RESILIENT:
        return resilient_call(source, _do, retries=2, backoff_base=1.0)
    return _do()


# ============================================================================
# 1. Short-Duration Market Discovery
# ============================================================================

# Keywords that identify short-duration crypto prediction markets
HF_KEYWORDS = [
    "5-minute", "5 minute", "5min", "15-minute", "15 minute", "15min",
    "1-minute", "1 minute", "1min", "next 5", "next 15",
    "btc up", "btc down", "eth up", "eth down",
    "bitcoin up", "bitcoin down", "ethereum up", "ethereum down",
    "sol up", "sol down", "solana up", "solana down",
]

# Broader crypto short-duration patterns
CRYPTO_ASSETS = ["btc", "eth", "sol", "bitcoin", "ethereum", "solana", "bnb", "xrp", "doge"]
DURATION_PATTERNS = ["minute", "min", "5m", "15m", "1m", "hour", "1h"]


@dataclass
class HFMarket:
    """A short-duration crypto prediction market."""
    market_id: str
    condition_id: str
    question: str
    slug: str
    asset: str  # BTC, ETH, SOL, etc.
    duration_hint: str  # "5min", "15min", "1h", etc.
    yes_price: float
    no_price: float
    price_sum: float  # yes + no — key for neg vig detection
    volume_24h: float
    liquidity: float
    end_date: Optional[str]
    created_at: Optional[str]
    clob_token_ids: List[str]
    neg_vig: bool  # True if price_sum < 0.99
    neg_vig_edge: float  # How much below $1.00


def _detect_asset(text: str) -> Optional[str]:
    """Detect which crypto asset a market is about."""
    text_lower = text.lower()
    for asset in ["bitcoin", "btc"]:
        if asset in text_lower:
            return "BTC"
    for asset in ["ethereum", "eth"]:
        if asset in text_lower:
            return "ETH"
    for asset in ["solana", "sol"]:
        if asset in text_lower:
            return "SOL"
    for asset in ["bnb", "binance coin"]:
        if asset in text_lower:
            return "BNB"
    for asset in ["dogecoin", "doge"]:
        if asset in text_lower:
            return "DOGE"
    for asset in ["xrp", "ripple"]:
        if asset in text_lower:
            return "XRP"
    return None


def _detect_duration(text: str) -> Optional[str]:
    """Detect the time duration of a market."""
    text_lower = text.lower()
    if any(p in text_lower for p in ["5-minute", "5 minute", "5min", "5m ", "next 5"]):
        return "5min"
    if any(p in text_lower for p in ["15-minute", "15 minute", "15min", "15m ", "next 15"]):
        return "15min"
    if any(p in text_lower for p in ["1-minute", "1 minute", "1min"]):
        return "1min"
    if any(p in text_lower for p in ["1-hour", "1 hour", "1h ", "hourly"]):
        return "1h"
    if any(p in text_lower for p in ["30-minute", "30 minute", "30min"]):
        return "30min"
    
    # Detect from time-range pattern like "10:30AM-10:35AM" (5 min gap)
    import re
    time_range = re.search(r'(\d{1,2}):(\d{2})\s*([AP]M)\s*[-–]\s*(\d{1,2}):(\d{2})\s*([AP]M)', text, re.IGNORECASE)
    if time_range:
        h1, m1, p1, h2, m2, p2 = time_range.groups()
        # Convert to minutes
        hour1 = int(h1) % 12 + (12 if p1.upper() == 'PM' else 0)
        hour2 = int(h2) % 12 + (12 if p2.upper() == 'PM' else 0)
        mins1 = hour1 * 60 + int(m1)
        mins2 = hour2 * 60 + int(m2)
        delta = mins2 - mins1
        if delta <= 0:
            delta += 1440  # cross midnight
        if delta <= 2:
            return "1min"
        if delta <= 7:
            return "5min"
        if delta <= 20:
            return "15min"
        if delta <= 35:
            return "30min"
        if delta <= 75:
            return "1h"
    
    # "Up or Down" pattern with date but no time range = likely daily
    if "up or down" in text_lower and not time_range:
        return None  # Could be daily, let date-based detection handle it
    
    return None


def _detect_duration_from_dates(created: str, end: str) -> Optional[str]:
    """Infer duration from created/end timestamps."""
    try:
        if not created or not end:
            return None
        # Parse ISO timestamps
        c = datetime.fromisoformat(created.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end.replace("Z", "+00:00"))
        delta = (e - c).total_seconds()
        if delta <= 120:  # 2 min
            return "1min"
        if delta <= 600:  # 10 min
            return "5min"
        if delta <= 1200:  # 20 min
            return "15min"
        if delta <= 2400:  # 40 min
            return "30min"
        if delta <= 4800:  # 80 min
            return "1h"
    except Exception:
        pass
    return None


def discover_hf_markets(limit: int = 200) -> List[HFMarket]:
    """
    Discover short-duration crypto prediction markets on Polymarket.
    
    Searches for 5-min, 15-min, and hourly BTC/ETH/SOL up/down markets.
    These are the exact market types the $134→$200K bot traded.
    
    Returns:
        List of HFMarket objects, sorted by neg vig edge (best first)
    """
    markets = []
    
    # Strategy 1: Events endpoint (most reliable — HF markets are grouped under events)
    try:
        url = f"{GAMMA_API}/events?active=true&closed=false&limit=100&order=startDate&ascending=false"
        events = _fetch_json("gamma_hf_events", url, timeout=20)
        if events:
            for event in events:
                for m in event.get("markets", []):
                    # Inject event-level fields if missing
                    if not m.get("question"):
                        m["question"] = event.get("title", "")
                    markets.append(m)
    except Exception:
        pass
    
    # Strategy 2: Direct market search (may 403 on some queries, best-effort)
    for keyword in ["up or down bitcoin", "up or down ethereum"]:
        try:
            url = f"{GAMMA_API}/markets?active=true&closed=false&_q={urllib.request.quote(keyword)}&limit=50"
            results = _fetch_json("gamma_hf_search", url)
            if results:
                markets.extend(results)
        except Exception:
            continue
    
    # Strategy 3: Get newest markets
    try:
        url = f"{GAMMA_API}/markets?active=true&closed=false&limit={limit}&order=createdAt&ascending=false"
        newest = _fetch_json("gamma_hf_newest", url)
        if newest:
            markets.extend(newest)
    except Exception:
        pass
    
    # Deduplicate by condition_id
    seen = set()
    unique_markets = []
    for m in markets:
        cid = m.get("conditionId", m.get("id", ""))
        if cid and cid not in seen:
            seen.add(cid)
            unique_markets.append(m)
    
    # Filter to crypto short-duration markets
    hf_markets = []
    for m in unique_markets:
        question = m.get("question", "")
        
        # Must be crypto-related
        asset = _detect_asset(question)
        if not asset:
            continue
        
        # Must be short-duration — require time-range in title or explicit duration keyword
        duration = _detect_duration(question)
        if not duration:
            # Only fall back to date-based detection if title contains "up or down"
            # This prevents long-term markets (e.g. "bitcoin hit $1m before GTA VI") 
            # from being misclassified as HF markets
            if "up or down" in question.lower():
                duration = _detect_duration_from_dates(
                    m.get("createdAt"), m.get("endDate")
                )
        if not duration:
            continue
        
        # Parse prices
        try:
            prices = json.loads(m.get("outcomePrices", "[0,0]"))
            yes_price = float(prices[0]) if prices[0] else 0.0
            no_price = float(prices[1]) if len(prices) > 1 and prices[1] else 0.0
        except Exception:
            yes_price, no_price = 0.0, 0.0
        
        price_sum = yes_price + no_price
        
        # Parse CLOB token IDs
        try:
            token_ids = json.loads(m.get("clobTokenIds", "[]"))
            if isinstance(token_ids, str):
                token_ids = [token_ids]
        except Exception:
            token_ids = []
        
        neg_vig = price_sum < 0.99 and price_sum > 0.50  # Sanity check
        neg_vig_edge = max(0, 1.0 - price_sum) if neg_vig else 0.0
        
        hf_markets.append(HFMarket(
            market_id=m.get("id", ""),
            condition_id=m.get("conditionId", ""),
            question=question,
            slug=m.get("slug", ""),
            asset=asset,
            duration_hint=duration,
            yes_price=round(yes_price, 4),
            no_price=round(no_price, 4),
            price_sum=round(price_sum, 4),
            volume_24h=float(m.get("volume24hr", 0) or 0),
            liquidity=float(m.get("liquidityNum", 0) or 0),
            end_date=m.get("endDate"),
            created_at=m.get("createdAt"),
            clob_token_ids=token_ids,
            neg_vig=neg_vig,
            neg_vig_edge=round(neg_vig_edge, 4),
        ))
    
    # Sort: neg vig opportunities first, then by liquidity
    hf_markets.sort(key=lambda m: (-m.neg_vig_edge, -m.liquidity))
    
    return hf_markets


# ============================================================================
# 2. Negative Vig Scanner (CLOB-level precision)
# ============================================================================

@dataclass
class NegVigOpportunity:
    """A market where buying both sides costs < $1.00."""
    market_id: str
    question: str
    asset: str
    duration: str
    yes_best_ask: float  # Cheapest Yes share
    no_best_ask: float   # Cheapest No share
    total_cost: float    # yes_ask + no_ask
    free_edge_pct: float # (1.0 - total_cost) * 100
    yes_ask_size: float  # Available size at best ask
    no_ask_size: float
    max_risk_free_size: float  # min(yes_size, no_size)
    timestamp: str


def scan_neg_vig(markets: List[HFMarket] = None, threshold: float = 0.99) -> List[NegVigOpportunity]:
    """
    Scan CLOB orderbooks for negative vig opportunities.
    
    For each market, fetches the actual Yes and No orderbooks and checks
    if best_ask(Yes) + best_ask(No) < threshold.
    
    This is the "free money" edge from the $134→$200K story:
    buy both sides for < $1.00, guaranteed profit on resolution.
    
    Args:
        markets: Pre-discovered HF markets (or discovers fresh if None)
        threshold: Maximum sum to flag as opportunity (default 0.99 = 1% edge)
    
    Returns:
        List of NegVigOpportunity, sorted by edge size
    """
    if markets is None:
        markets = discover_hf_markets()
    
    opportunities = []
    
    for market in markets:
        if len(market.clob_token_ids) < 2:
            continue
        
        try:
            # Fetch Yes orderbook (token 0)
            yes_url = f"{CLOB_API}/book?token_id={market.clob_token_ids[0]}"
            yes_book = _fetch_json("clob_yes", yes_url, timeout=8)
            
            # Fetch No orderbook (token 1)
            no_url = f"{CLOB_API}/book?token_id={market.clob_token_ids[1]}"
            no_book = _fetch_json("clob_no", no_url, timeout=8)
            
            # Get best asks (cheapest price to buy)
            yes_asks = yes_book.get("asks", [])
            no_asks = no_book.get("asks", [])
            
            if not yes_asks or not no_asks:
                continue
            
            yes_best_ask = float(yes_asks[0]["price"])
            no_best_ask = float(no_asks[0]["price"])
            yes_ask_size = float(yes_asks[0]["size"])
            no_ask_size = float(no_asks[0]["size"])
            
            total_cost = yes_best_ask + no_best_ask
            
            if total_cost < threshold and total_cost > 0.50:  # Sanity bound
                free_edge = (1.0 - total_cost) * 100
                
                opportunities.append(NegVigOpportunity(
                    market_id=market.market_id,
                    question=market.question,
                    asset=market.asset,
                    duration=market.duration_hint,
                    yes_best_ask=round(yes_best_ask, 4),
                    no_best_ask=round(no_best_ask, 4),
                    total_cost=round(total_cost, 4),
                    free_edge_pct=round(free_edge, 2),
                    yes_ask_size=round(yes_ask_size, 2),
                    no_ask_size=round(no_ask_size, 2),
                    max_risk_free_size=round(min(yes_ask_size, no_ask_size), 2),
                    timestamp=datetime.utcnow().isoformat(),
                ))
        except Exception as e:
            continue
    
    # Sort by edge (biggest free money first)
    opportunities.sort(key=lambda o: -o.free_edge_pct)
    
    return opportunities


# ============================================================================
# 3. Combined HF Scan — Discovery + Neg Vig + Summary
# ============================================================================

def full_hf_scan(neg_vig_threshold: float = 0.99) -> Dict:
    """
    Run complete HF scan: discover markets, check neg vig, summarize.
    
    This is the main entry point for the HF module Phase 1.
    
    Returns:
        Complete scan result with markets, opportunities, and summary
    """
    scan_start = datetime.utcnow()
    
    # 1. Discover short-duration crypto markets
    hf_markets = discover_hf_markets()
    
    # 2. Scan for negative vig on discovered markets
    neg_vig_opps = scan_neg_vig(hf_markets, threshold=neg_vig_threshold)
    
    # 3. Build summary
    asset_breakdown = {}
    duration_breakdown = {}
    for m in hf_markets:
        asset_breakdown[m.asset] = asset_breakdown.get(m.asset, 0) + 1
        duration_breakdown[m.duration_hint] = duration_breakdown.get(m.duration_hint, 0) + 1
    
    total_neg_vig_edge = sum(o.free_edge_pct for o in neg_vig_opps)
    
    scan_duration = (datetime.utcnow() - scan_start).total_seconds()
    
    return {
        "scan_time": datetime.utcnow().isoformat(),
        "scan_duration_seconds": round(scan_duration, 1),
        "summary": {
            "total_hf_markets": len(hf_markets),
            "neg_vig_opportunities": len(neg_vig_opps),
            "total_neg_vig_edge_pct": round(total_neg_vig_edge, 2),
            "best_neg_vig_edge_pct": neg_vig_opps[0].free_edge_pct if neg_vig_opps else 0,
            "assets_found": asset_breakdown,
            "durations_found": duration_breakdown,
        },
        "neg_vig_opportunities": [asdict(o) for o in neg_vig_opps[:20]],
        "hf_markets": [asdict(m) for m in hf_markets[:50]],
        "phase": "Phase 1 — Discovery + Neg Vig Scanner",
        "note": "Phase 2 adds Virtuoso MCP directional signals. Phase 3 adds real-time Binance WS + Chainlink latency arb."
    }


# ============================================================================
# CLI test
# ============================================================================

if __name__ == "__main__":
    import pprint
    
    print("=" * 60)
    print("Polyclawd HF Scanner — Phase 1")
    print("=" * 60)
    
    print("\n📡 Discovering short-duration crypto markets...")
    markets = discover_hf_markets()
    print(f"Found {len(markets)} HF markets")
    
    for m in markets[:10]:
        vig_flag = " 🎯 NEG VIG!" if m.neg_vig else ""
        print(f"  [{m.asset}] [{m.duration_hint}] {m.question[:60]}... "
              f"Y:{m.yes_price} N:{m.no_price} Sum:{m.price_sum}{vig_flag}")
    
    if markets:
        print(f"\n🔍 Scanning CLOB for negative vig (threshold < $0.99)...")
        opps = scan_neg_vig(markets)
        print(f"Found {len(opps)} neg vig opportunities")
        
        for o in opps[:5]:
            print(f"  🎯 [{o.asset}] [{o.duration}] Edge: {o.free_edge_pct}% "
                  f"(Y:{o.yes_best_ask} + N:{o.no_best_ask} = {o.total_cost}) "
                  f"Max size: ${o.max_risk_free_size}")
    
    print("\nDone.")

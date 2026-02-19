"""
Copy-Trade Watcher — Track top Polymarket whale wallets.

Scans recent large trades to identify active whales, then checks their
positions for overlap with our signals (confirmation layer).

Uses Polymarket data-api.polymarket.com endpoints.
"""

import json
import logging
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# Thresholds
MIN_WHALE_VOLUME = 200          # Min total volume to qualify as whale in recent trades
MIN_WHALE_TRADES = 2            # Or min trades count
MIN_POSITION_SIZE = 50          # Min position size to track (shares)
MAX_WALLETS_TO_SCAN = 25        # Top N wallets to deep-scan
OVERLAP_CONFIDENCE_BOOST = 0.10 # Confidence boost when whale confirms our signal
TRADE_FETCH_LIMIT = 1000        # Recent trades to analyze

# Cache
_cache: Dict = {"data": None, "timestamp": 0}
CACHE_TTL = 300  # 5 min


def _fetch(url: str, params: dict = None, timeout: int = 15) -> Optional[any]:
    """Fetch with error handling."""
    try:
        r = httpx.get(url, params=params, timeout=timeout, 
                      headers={"User-Agent": "Polyclawd/1.0"})
        if r.status_code == 200:
            return r.json()
        logger.debug(f"HTTP {r.status_code} from {url}")
        return None
    except Exception as e:
        logger.debug(f"Fetch failed {url}: {e}")
        return None


def discover_whales(trade_limit: int = TRADE_FETCH_LIMIT) -> List[Dict]:
    """Discover active whale wallets from recent trade activity."""
    trades = _fetch(f"{DATA_API}/trades", {"limit": trade_limit})
    if not trades:
        return []
    
    # Aggregate by wallet
    wallet_stats = defaultdict(lambda: {"volume": 0, "trades": 0, "markets": set()})
    
    for t in trades:
        wallet = t.get("proxyWallet", "")
        if not wallet:
            continue
        size = float(t.get("size", 0) or 0)
        cid = t.get("conditionId", "")
        
        wallet_stats[wallet]["volume"] += size
        wallet_stats[wallet]["trades"] += 1
        if cid:
            wallet_stats[wallet]["markets"].add(cid)
    
    # Filter to whales
    whales = []
    for addr, stats in wallet_stats.items():
        if stats["volume"] >= MIN_WHALE_VOLUME or stats["trades"] >= MIN_WHALE_TRADES:
            whales.append({
                "address": addr,
                "volume": stats["volume"],
                "trades": stats["trades"],
                "markets": len(stats["markets"]),
                "name": addr[:10] + "...",
            })
    
    whales.sort(key=lambda w: w["volume"], reverse=True)
    logger.info(f"Discovered {len(whales)} whales from {len(trades)} recent trades")
    return whales[:MAX_WALLETS_TO_SCAN]


def fetch_wallet_positions(address: str) -> List[Dict]:
    """Fetch active positions for a wallet from data API."""
    data = _fetch(f"{DATA_API}/positions", {"user": address})
    if not data or not isinstance(data, list):
        return []
    return data


def _resolve_condition_to_question(condition_id: str) -> str:
    """Look up market question from condition ID via Gamma API."""
    data = _fetch(f"{GAMMA_API}/markets", {"condition_id": condition_id})
    if data and isinstance(data, list) and data:
        return data[0].get("question", "")[:100]
    return ""


def scan_whale_positions(whales: List[Dict] = None) -> Dict:
    """Scan whale positions and aggregate by market."""
    if whales is None:
        whales = discover_whales()
    
    if not whales:
        return {"markets": {}, "whales_scanned": 0, "error": "No whales found"}
    
    market_agg = {}
    whales_scanned = 0
    
    for whale in whales:
        positions = fetch_wallet_positions(whale["address"])
        if not positions:
            continue
        whales_scanned += 1
        
        for pos in positions:
            condition_id = pos.get("conditionId", "")
            if not condition_id:
                continue
            
            size = float(pos.get("size", 0) or 0)
            if size < MIN_POSITION_SIZE:
                continue
            
            # Determine side from outcome index
            outcome_idx = pos.get("outcomeIndex")
            outcome = pos.get("outcome", "")
            if outcome_idx == 0 or str(outcome).upper() in ("YES", "1"):
                side = "YES"
            else:
                side = "NO"
            
            avg_price = float(pos.get("avgPrice", 0) or 0)
            cur_price = float(pos.get("curPrice", 0) or 0)
            
            if condition_id not in market_agg:
                # Try to get market question
                question = pos.get("title", "") or pos.get("eventTitle", "")
                market_agg[condition_id] = {
                    "condition_id": condition_id,
                    "question": question[:100],
                    "whale_count": 0,
                    "total_size": 0,
                    "yes_whales": 0,
                    "no_whales": 0,
                    "yes_size": 0,
                    "no_size": 0,
                    "whale_addrs": [],
                    "avg_price": 0,
                    "cur_price": cur_price,
                }
            
            m = market_agg[condition_id]
            m["whale_count"] += 1
            m["total_size"] += size
            m["whale_addrs"].append(whale["address"][:10])
            if not m["question"]:
                m["question"] = pos.get("title", "") or pos.get("eventTitle", "")
            if cur_price > 0:
                m["cur_price"] = cur_price
            
            if side == "YES":
                m["yes_whales"] += 1
                m["yes_size"] += size
            else:
                m["no_whales"] += 1
                m["no_size"] += size
        
        time.sleep(0.2)
    
    # Calculate consensus
    for m in market_agg.values():
        if m["yes_size"] > m["no_size"] * 2:
            m["consensus"] = "STRONG YES"
            m["consensus_side"] = "YES"
        elif m["no_size"] > m["yes_size"] * 2:
            m["consensus"] = "STRONG NO"
            m["consensus_side"] = "NO"
        elif m["yes_size"] > m["no_size"]:
            m["consensus"] = "LEAN YES"
            m["consensus_side"] = "YES"
        elif m["no_size"] > m["yes_size"]:
            m["consensus"] = "LEAN NO"
            m["consensus_side"] = "NO"
        else:
            m["consensus"] = "SPLIT"
            m["consensus_side"] = None
    
    return {
        "markets": market_agg,
        "whales_scanned": whales_scanned,
        "total_whales": len(whales),
        "markets_with_activity": len(market_agg),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def find_signal_overlaps(our_signals: List[Dict], whale_data: Dict) -> List[Dict]:
    """Find overlaps between our signals and whale positions."""
    overlaps = []
    whale_markets = whale_data.get("markets", {})
    
    for signal in our_signals:
        market_id = signal.get("market_id", "") or signal.get("condition_id", "")
        if not market_id or market_id not in whale_markets:
            continue
        
        whale = whale_markets[market_id]
        our_side = signal.get("side", "")
        whale_side = whale.get("consensus_side")
        if not whale_side:
            continue
        
        agrees = (our_side == whale_side)
        overlaps.append({
            "market": signal.get("market", "") or whale["question"],
            "market_id": market_id,
            "our_side": our_side,
            "our_confidence": signal.get("confidence", 0),
            "whale_consensus": whale["consensus"],
            "whale_side": whale_side,
            "whale_count": whale["whale_count"],
            "total_size": whale["total_size"],
            "agrees": agrees,
            "boosted_confidence": min(0.95, signal.get("confidence", 0) + 
                                      (OVERLAP_CONFIDENCE_BOOST if agrees else -0.05)),
        })
    
    overlaps.sort(key=lambda x: (x["agrees"], x["whale_count"]), reverse=True)
    return overlaps


def get_copy_trade_signals() -> Dict:
    """Main entry point with caching."""
    now = time.time()
    if _cache["data"] and (now - _cache["timestamp"]) < CACHE_TTL:
        return _cache["data"]
    
    whales = discover_whales()
    whale_data = scan_whale_positions(whales)
    
    # Get our signals for overlap check
    try:
        from signals.mispriced_category_signal import get_mispriced_category_signals
        our_data = get_mispriced_category_signals()
        our_signals = our_data.get("signals", [])
    except Exception as e:
        logger.error(f"Failed to get our signals: {e}")
        our_signals = []
    
    overlaps = find_signal_overlaps(our_signals, whale_data)
    
    # Whale-only markets (no signal from us)
    our_ids = {s.get("market_id", "") or s.get("condition_id", "") for s in our_signals}
    whale_only = []
    for mid, mdata in whale_data.get("markets", {}).items():
        if mid not in our_ids and mdata["whale_count"] >= 2:
            whale_only.append({
                "market": mdata["question"],
                "market_id": mid,
                "whale_count": mdata["whale_count"],
                "consensus": mdata["consensus"],
                "total_size": mdata["total_size"],
            })
    whale_only.sort(key=lambda x: x["whale_count"], reverse=True)
    
    result = {
        "whales_discovered": len(whales),
        "whales_scanned": whale_data["whales_scanned"],
        "whale_markets": whale_data["markets_with_activity"],
        "our_signals": len(our_signals),
        "overlaps": overlaps,
        "overlap_count": len(overlaps),
        "agreeing_count": sum(1 for o in overlaps if o["agrees"]),
        "whale_only": whale_only[:15],
        "top_whales": [{
            "address": w["address"][:14] + "...",
            "volume": w["volume"],
            "trades": w["trades"],
            "markets": w["markets"],
        } for w in whales[:10]],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    
    _cache["data"] = result
    _cache["timestamp"] = now
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    print("=== Copy-Trade Watcher ===\n")
    
    whales = discover_whales()
    print(f"Whales found: {len(whales)}")
    for w in whales[:5]:
        print(f"  {w['name']:14}  vol={w['volume']:>8.0f}  trades={w['trades']:>3}  markets={w['markets']}")
    
    if whales:
        print(f"\nScanning positions for top {min(10, len(whales))} whales...")
        whale_data = scan_whale_positions(whales[:10])
        print(f"Scanned: {whale_data['whales_scanned']}")
        print(f"Markets: {whale_data['markets_with_activity']}")
        
        # Show top whale markets
        top_markets = sorted(whale_data["markets"].values(), key=lambda m: m["whale_count"], reverse=True)
        print(f"\nTop whale markets:")
        for m in top_markets[:15]:
            q = m["question"][:55] if m["question"] else m["condition_id"][:20]
            print(f"  {m['consensus']:12} {m['whale_count']}W sz={m['total_size']:>6.0f}  {q}")

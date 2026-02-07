"""
Manifold Markets Integration
Free API, play money but good signal quality
"""

import json
import urllib.request
from datetime import datetime
from typing import List, Dict, Optional
from dataclasses import dataclass

MANIFOLD_API = "https://api.manifold.markets/v0"

@dataclass
class ManifoldMarket:
    id: str
    question: str
    probability: float
    volume: float
    liquidity: float
    close_time: Optional[str]
    url: str

def fetch_markets(limit: int = 100, sort: str = "liquidity") -> List[Dict]:
    """Fetch active markets from Manifold"""
    try:
        url = f"{MANIFOLD_API}/markets?limit={limit}"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            # API returns array directly
            if isinstance(data, list):
                # Sort by liquidity locally
                if sort == "liquidity":
                    data.sort(key=lambda x: x.get("totalLiquidity", 0), reverse=True)
                return data
            return []
    except Exception as e:
        print(f"Manifold fetch error: {e}")
        return []

def search_markets(query: str, limit: int = 20) -> List[Dict]:
    """Search Manifold markets by keyword"""
    try:
        encoded = urllib.parse.quote(query)
        url = f"{MANIFOLD_API}/search-markets?term={encoded}&limit={limit}"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"Manifold search error: {e}")
        return []

def get_market(market_id: str) -> Optional[Dict]:
    """Get specific market details"""
    try:
        url = f"{MANIFOLD_API}/market/{market_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except:
        return None

def find_polymarket_overlaps(poly_events: List[Dict], min_liquidity: float = 1000) -> List[Dict]:
    """Find Manifold markets that match Polymarket events"""
    try:
        from smart_matcher import match_markets
    except ImportError:
        from odds.smart_matcher import match_markets
    
    # Get top Manifold markets
    manifold_markets = fetch_markets(limit=200)
    
    # Filter for active, liquid markets
    active = [
        m for m in manifold_markets 
        if m.get("isResolved") == False 
        and m.get("totalLiquidity", 0) >= min_liquidity
    ]
    
    overlaps = []
    
    for poly in poly_events:
        poly_title = poly.get("title", "")
        
        # Build candidate list from Manifold
        candidates = [
            {
                "title": m.get("question", ""),
                "probability": m.get("probability", 0.5),
                "volume": m.get("volume", 0),
                "liquidity": m.get("totalLiquidity", 0),
                "url": m.get("url", ""),
                "id": m.get("id", "")
            }
            for m in active
        ]
        
        # Find matches
        matches = match_markets(
            source_title=poly_title,
            candidates=candidates,
            title_key="title",
            min_entity_overlap=1,
            min_confidence=0.5,
            max_matches=2
        )
        
        for match in matches:
            poly_price = None
            for mkt in poly.get("markets", []):
                # Handle outcomePrices which might be a JSON string
                outcome_prices = mkt.get("outcomePrices", {})
                if isinstance(outcome_prices, str):
                    try:
                        import json
                        outcome_prices = json.loads(outcome_prices)
                    except:
                        outcome_prices = {}
                poly_price = mkt.get("bestAsk") or (outcome_prices.get("Yes") if isinstance(outcome_prices, dict) else None)
                break
            
            if poly_price:
                manifold_prob = match.get("probability", 0.5)
                poly_prob = float(poly_price) if poly_price else 0.5
                edge = (manifold_prob - poly_prob) * 100
                
                overlaps.append({
                    "polymarket_title": poly_title,
                    "polymarket_price": round(poly_prob * 100, 1),
                    "manifold_question": match.get("title", ""),
                    "manifold_prob": round(manifold_prob * 100, 1),
                    "manifold_liquidity": match.get("liquidity", 0),
                    "manifold_url": match.get("url", ""),
                    "edge_pct": round(edge, 1),
                    "match_confidence": match.get("_match_confidence", 0),
                    "direction": "YES" if edge > 0 else "NO"
                })
    
    # Sort by absolute edge
    overlaps.sort(key=lambda x: abs(x["edge_pct"]), reverse=True)
    return overlaps

async def get_manifold_edges(min_edge: float = 5.0) -> Dict:
    """Get Manifold vs Polymarket edges"""
    import urllib.request
    
    # Fetch Polymarket events
    try:
        req = urllib.request.Request(
            "https://gamma-api.polymarket.com/events?closed=false&limit=200",
            headers={"User-Agent": "Polyclawd/1.0"}
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            poly_events = json.loads(resp.read().decode())
    except:
        poly_events = []
    
    overlaps = find_polymarket_overlaps(poly_events)
    
    # Filter by minimum edge
    edges = [o for o in overlaps if abs(o["edge_pct"]) >= min_edge]
    
    return {
        "source": "manifold",
        "timestamp": datetime.utcnow().isoformat(),
        "total_overlaps": len(overlaps),
        "edges_found": len(edges),
        "min_edge_filter": min_edge,
        "edges": edges[:20]
    }

def get_manifold_summary() -> Dict:
    """Get summary of Manifold markets"""
    markets = fetch_markets(limit=50, sort="liquidity")
    
    total_liquidity = sum(m.get("totalLiquidity", 0) for m in markets)
    total_volume = sum(m.get("volume", 0) for m in markets)
    
    return {
        "source": "manifold",
        "timestamp": datetime.utcnow().isoformat(),
        "markets_fetched": len(markets),
        "total_liquidity": round(total_liquidity),
        "total_volume": round(total_volume),
        "top_markets": [
            {
                "question": m.get("question", "")[:60],
                "probability": round(m.get("probability", 0) * 100, 1),
                "liquidity": round(m.get("totalLiquidity", 0)),
                "url": m.get("url", "")
            }
            for m in markets[:10]
        ]
    }


if __name__ == "__main__":
    import asyncio
    
    print("Testing Manifold integration...")
    summary = get_manifold_summary()
    print(f"Markets: {summary['markets_fetched']}")
    print(f"Total liquidity: ${summary['total_liquidity']:,}")
    print("\nTop markets:")
    for m in summary['top_markets'][:5]:
        print(f"  {m['probability']}% - {m['question']}")

"""
PredictIt Integration
Free public API, politics focused
"""

import json
import urllib.request
from datetime import datetime
from typing import List, Dict, Optional

PREDICTIT_API = "https://www.predictit.org/api/marketdata/all/"

def fetch_all_markets() -> List[Dict]:
    """Fetch all PredictIt markets"""
    try:
        req = urllib.request.Request(
            PREDICTIT_API,
            headers={"User-Agent": "Polyclawd/1.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            return data.get("markets", [])
    except Exception as e:
        print(f"PredictIt fetch error: {e}")
        return []

def parse_contracts(market: Dict) -> List[Dict]:
    """Parse contracts from a market"""
    contracts = []
    for c in market.get("contracts", []):
        contracts.append({
            "id": c.get("id"),
            "name": c.get("name", ""),
            "short_name": c.get("shortName", ""),
            "yes_price": c.get("lastTradePrice"),
            "buy_yes": c.get("bestBuyYesCost"),
            "buy_no": c.get("bestBuyNoCost"),
            "sell_yes": c.get("bestSellYesCost"),
            "sell_no": c.get("bestSellNoCost"),
        })
    return contracts

def find_polymarket_overlaps(poly_events: List[Dict]) -> List[Dict]:
    """Find PredictIt markets that match Polymarket events"""
    try:
        from smart_matcher import match_markets
    except ImportError:
        from odds.smart_matcher import match_markets
    
    predictit_markets = fetch_all_markets()
    
    overlaps = []
    
    for poly in poly_events:
        poly_title = poly.get("title", "")
        
        # Build candidate list from PredictIt
        candidates = []
        for mkt in predictit_markets:
            market_name = mkt.get("name", "")
            for contract in mkt.get("contracts", []):
                candidates.append({
                    "title": f"{market_name} - {contract.get('name', '')}",
                    "market_name": market_name,
                    "contract_name": contract.get("name", ""),
                    "yes_price": contract.get("lastTradePrice"),
                    "buy_yes": contract.get("bestBuyYesCost"),
                    "market_id": mkt.get("id"),
                    "contract_id": contract.get("id"),
                    "url": mkt.get("url", "")
                })
        
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
                poly_price = mkt.get("bestAsk", mkt.get("outcomePrices", {}).get("Yes"))
                break
            
            if poly_price and match.get("yes_price"):
                predictit_prob = match.get("yes_price", 0.5)
                poly_prob = float(poly_price) if poly_price else 0.5
                edge = (predictit_prob - poly_prob) * 100
                
                overlaps.append({
                    "polymarket_title": poly_title,
                    "polymarket_price": round(poly_prob * 100, 1),
                    "predictit_market": match.get("market_name", ""),
                    "predictit_contract": match.get("contract_name", ""),
                    "predictit_price": round(predictit_prob * 100, 1),
                    "predictit_url": match.get("url", ""),
                    "edge_pct": round(edge, 1),
                    "match_confidence": match.get("_match_confidence", 0),
                    "direction": "YES" if edge > 0 else "NO"
                })
    
    # Sort by absolute edge
    overlaps.sort(key=lambda x: abs(x["edge_pct"]), reverse=True)
    return overlaps

async def get_predictit_edges(min_edge: float = 5.0) -> Dict:
    """Get PredictIt vs Polymarket edges"""
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
        "source": "predictit",
        "timestamp": datetime.utcnow().isoformat(),
        "total_overlaps": len(overlaps),
        "edges_found": len(edges),
        "min_edge_filter": min_edge,
        "edges": edges[:20]
    }

def get_predictit_summary() -> Dict:
    """Get summary of PredictIt markets"""
    markets = fetch_all_markets()
    
    total_contracts = sum(len(m.get("contracts", [])) for m in markets)
    
    # Find most active by looking at bid/ask spread
    active_markets = []
    for m in markets:
        for c in m.get("contracts", []):
            if c.get("bestBuyYesCost") and c.get("bestSellYesCost"):
                active_markets.append({
                    "market": m.get("name", "")[:50],
                    "contract": c.get("name", ""),
                    "price": c.get("lastTradePrice"),
                    "url": m.get("url", "")
                })
    
    return {
        "source": "predictit",
        "timestamp": datetime.utcnow().isoformat(),
        "markets": len(markets),
        "total_contracts": total_contracts,
        "active_contracts": len(active_markets),
        "sample": active_markets[:10]
    }


if __name__ == "__main__":
    print("Testing PredictIt integration...")
    summary = get_predictit_summary()
    print(f"Markets: {summary['markets']}")
    print(f"Contracts: {summary['total_contracts']}")
    print("\nSample:")
    for m in summary['sample'][:5]:
        print(f"  {m['price']*100:.0f}¢ - {m['market']} / {m['contract']}")

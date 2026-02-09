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


def get_bets(
    market_id: str = None,
    username: str = None,
    limit: int = 100,
    before: str = None
) -> List[Dict]:
    """
    Fetch bets with optional filters.
    
    Args:
        market_id: Filter by market (contractId)
        username: Filter by user
        limit: Max bets to return
        before: Pagination cursor (bet ID)
    
    Returns:
        List of bet objects with amount, shares, outcome, etc.
    """
    try:
        url = f"{MANIFOLD_API}/bets?limit={limit}"
        if market_id:
            url += f"&contractId={market_id}"
        if username:
            url += f"&username={username}"
        if before:
            url += f"&before={before}"
        
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"Manifold bets error: {e}")
        return []


def get_user(username: str) -> Optional[Dict]:
    """Get user profile by username"""
    try:
        url = f"{MANIFOLD_API}/user/{username}"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except:
        return None


def get_user_portfolio(user_id: str) -> Optional[Dict]:
    """
    Get user's live portfolio metrics.
    
    Returns:
        Portfolio with balance, investmentValue, profit, etc.
    """
    try:
        url = f"{MANIFOLD_API}/get-user-portfolio?userId={user_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except:
        return None


def get_top_traders(limit: int = 20) -> List[Dict]:
    """
    Get top traders by profit.
    
    Note: Uses leaderboard data from markets endpoint.
    """
    try:
        # Get recent high-volume markets and extract top traders
        markets = fetch_markets(limit=50, sort="liquidity")
        
        # Track unique traders
        trader_profits = {}
        
        for market in markets:
            creator = market.get("creatorUsername", "")
            if creator and creator not in trader_profits:
                user = get_user(creator)
                if user:
                    profit = user.get("profitCached", {}).get("allTime", 0)
                    trader_profits[creator] = {
                        "username": creator,
                        "name": user.get("name", ""),
                        "profit": profit,
                        "balance": user.get("balance", 0),
                        "url": f"https://manifold.markets/{creator}"
                    }
        
        # Sort by profit
        sorted_traders = sorted(
            trader_profits.values(),
            key=lambda x: x["profit"],
            reverse=True
        )[:limit]
        
        return sorted_traders
        
    except Exception as e:
        print(f"Error getting top traders: {e}")
        return []


def track_sharp_bettors(market_id: str, min_profit: float = 1000) -> List[Dict]:
    """
    Track bets from profitable traders on a specific market.
    
    Args:
        market_id: The market to analyze
        min_profit: Minimum all-time profit to consider "sharp"
    
    Returns:
        List of bets from profitable traders with their track record
    """
    bets = get_bets(market_id=market_id, limit=100)
    
    sharp_bets = []
    checked_users = {}
    
    for bet in bets:
        user_id = bet.get("userId", "")
        if user_id in checked_users:
            user = checked_users[user_id]
        else:
            # We'd need username to get user, but bets don't include it
            # In practice, you'd maintain a user_id -> username mapping
            continue
        
        if user and user.get("profit", 0) >= min_profit:
            sharp_bets.append({
                "bet_id": bet.get("id"),
                "amount": bet.get("amount", 0),
                "outcome": bet.get("outcome"),
                "shares": bet.get("shares", 0),
                "trader": user.get("username"),
                "trader_profit": user.get("profit"),
                "created": bet.get("createdTime"),
            })
    
    return sharp_bets


def get_market_bets_flow(market_id: str) -> Dict:
    """
    Analyze betting flow on a market.
    
    Returns:
        Summary of YES vs NO volume, recent momentum, etc.
    """
    bets = get_bets(market_id=market_id, limit=200)
    
    if not bets:
        return {}
    
    yes_volume = sum(b.get("amount", 0) for b in bets if b.get("outcome") == "YES")
    no_volume = sum(b.get("amount", 0) for b in bets if b.get("outcome") == "NO")
    total_volume = yes_volume + no_volume
    
    # Recent bets (last 50)
    recent = bets[:50]
    recent_yes = sum(b.get("amount", 0) for b in recent if b.get("outcome") == "YES")
    recent_no = sum(b.get("amount", 0) for b in recent if b.get("outcome") == "NO")
    
    return {
        "market_id": market_id,
        "total_bets": len(bets),
        "yes_volume": yes_volume,
        "no_volume": no_volume,
        "total_volume": total_volume,
        "yes_pct": round(yes_volume / total_volume * 100, 1) if total_volume > 0 else 50,
        "recent_yes_volume": recent_yes,
        "recent_no_volume": recent_no,
        "recent_momentum": "YES" if recent_yes > recent_no * 1.5 else "NO" if recent_no > recent_yes * 1.5 else "NEUTRAL"
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
    
    print("\n" + "="*50)
    print("\nTesting bets endpoint...")
    bets = get_bets(limit=5)
    print(f"Recent bets: {len(bets)}")
    for b in bets[:3]:
        print(f"  {b.get('outcome')}: ${b.get('amount', 0):.0f}")

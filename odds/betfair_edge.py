"""
Betfair Exchange Edge Finder
Compares Betfair Exchange odds (via The Odds API) with Polymarket
"""

import requests
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional
from datetime import datetime
import os

# The Odds API key
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "8f5b987dcee59ee4d05473290624411c")

@dataclass
class BetfairEdge:
    event: str
    selection: str
    sport: str
    betfair_price: float
    betfair_prob: float
    polymarket_price: Optional[float]
    edge_pct: Optional[float]
    direction: Optional[str]
    poly_market_id: Optional[str] = None

def decimal_to_prob(price: float) -> float:
    """Convert decimal odds to implied probability"""
    if price <= 1:
        return 1.0
    return 1 / price

def _fetch_betfair_odds_sync(sport: str) -> list[dict]:
    """Fetch Betfair exchange odds for a sport"""
    try:
        resp = requests.get(
            f"https://api.the-odds-api.com/v4/sports/{sport}/odds",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "uk",
                "markets": "h2h,outrights",
                "bookmakers": "betfair_ex_uk"
            },
            timeout=30
        )
        return resp.json() if resp.status_code == 200 else []
    except Exception as e:
        print(f"Error fetching Betfair odds for {sport}: {e}")
        return []

async def get_betfair_odds(sport: str) -> list[dict]:
    """Async wrapper for Betfair odds fetch"""
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as executor:
        return await loop.run_in_executor(executor, _fetch_betfair_odds_sync, sport)

def _fetch_polymarket_sync() -> dict:
    """Fetch Polymarket events"""
    try:
        resp = requests.get(
            "https://gamma-api.polymarket.com/events",
            params={"closed": "false", "limit": "300"},
            timeout=30
        )
        return resp.json() if resp.status_code == 200 else []
    except Exception as e:
        print(f"Error fetching Polymarket: {e}")
        return []

# Sport mappings for cross-reference
SPORT_MAPPINGS = {
    "basketball_nba_championship_winner": {
        "polymarket_search": "NBA Champion",
        "sport_name": "NBA"
    },
    "icehockey_nhl_championship_winner": {
        "polymarket_search": "NHL Stanley Cup",
        "sport_name": "NHL"
    },
    "soccer_fifa_world_cup_winner": {
        "polymarket_search": "World Cup Winner",
        "sport_name": "World Cup"
    },
    "soccer_epl": {
        "polymarket_search": "Premier League",
        "sport_name": "EPL"
    },
    "politics_us_presidential_election_winner": {
        "polymarket_search": "President",
        "sport_name": "Politics"
    }
}

# Team name normalization
TEAM_ALIASES = {
    "Manchester City": ["Man City", "Manchester City"],
    "Manchester United": ["Man Utd", "Manchester United", "Man United"],
    "Tottenham Hotspur": ["Tottenham", "Spurs"],
    "Boston Celtics": ["Celtics", "Boston"],
    "Los Angeles Lakers": ["Lakers", "LA Lakers"],
    "Golden State Warriors": ["Warriors", "Golden State"],
    "Oklahoma City Thunder": ["Thunder", "OKC"],
}

def normalize_name(name: str) -> list[str]:
    """Get all variations of a team/selection name"""
    for key, aliases in TEAM_ALIASES.items():
        if name in aliases or key == name:
            return aliases + [key]
    return [name]

def find_polymarket_price(selection: str, poly_events: list, search_term: str) -> tuple[Optional[float], Optional[str]]:
    """Find matching Polymarket price for a selection"""
    selection_variations = normalize_name(selection)
    
    for event in poly_events:
        title = event.get("title", "").lower()
        if search_term.lower() not in title:
            continue
            
        for market in event.get("markets", []):
            question = market.get("question", "")
            price = market.get("bestAsk", 0)
            market_id = market.get("id", "")
            
            # Check if selection matches
            for var in selection_variations:
                if var.lower() in question.lower():
                    return (float(price) if price else None, market_id)
    
    return (None, None)

async def find_betfair_edges(sports: list[str] = None, min_edge: float = 0.02) -> list[BetfairEdge]:
    """
    Find edges between Betfair Exchange and Polymarket
    """
    if sports is None:
        sports = list(SPORT_MAPPINGS.keys())
    
    edges = []
    
    # Fetch Polymarket data once
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as executor:
        poly_events = await loop.run_in_executor(executor, _fetch_polymarket_sync)
    
    for sport in sports:
        mapping = SPORT_MAPPINGS.get(sport)
        if not mapping:
            continue
            
        betfair_data = await get_betfair_odds(sport)
        
        for event in betfair_data:
            for bookmaker in event.get("bookmakers", []):
                if bookmaker.get("key") != "betfair_ex_uk":
                    continue
                    
                for market in bookmaker.get("markets", []):
                    for outcome in market.get("outcomes", []):
                        selection = outcome.get("name")
                        price = outcome.get("price", 0)
                        
                        if not selection or price <= 1:
                            continue
                        
                        betfair_prob = decimal_to_prob(price)
                        
                        # Find Polymarket match
                        poly_price, market_id = find_polymarket_price(
                            selection, 
                            poly_events, 
                            mapping["polymarket_search"]
                        )
                        
                        edge = None
                        direction = None
                        
                        if poly_price and poly_price > 0:
                            edge = betfair_prob - poly_price
                            if abs(edge) >= min_edge:
                                direction = "BUY" if edge > 0 else "SELL"
                        
                        if edge is not None and abs(edge) >= min_edge:
                            event_name = event.get("title") or f"{event.get('home_team', '')} vs {event.get('away_team', '')}"
                            edges.append(BetfairEdge(
                                event=event_name,
                                selection=selection,
                                sport=mapping["sport_name"],
                                betfair_price=price,
                                betfair_prob=betfair_prob,
                                polymarket_price=poly_price,
                                edge_pct=edge,
                                direction=direction,
                                poly_market_id=market_id
                            ))
    
    # Sort by edge size
    edges.sort(key=lambda x: abs(x.edge_pct or 0), reverse=True)
    return edges

async def get_betfair_edge_summary() -> dict:
    """Get summary of Betfair vs Polymarket edges"""
    edges = await find_betfair_edges()
    
    return {
        "source": "Betfair Exchange (via The Odds API)",
        "timestamp": datetime.utcnow().isoformat(),
        "total_edges": len(edges),
        "edges": [
            {
                "event": e.event,
                "selection": e.selection,
                "sport": e.sport,
                "betfair_price": e.betfair_price,
                "betfair_prob": round(e.betfair_prob * 100, 1),
                "polymarket_price": round(e.polymarket_price * 100, 1) if e.polymarket_price else None,
                "edge_pct": round(e.edge_pct * 100, 1) if e.edge_pct else None,
                "direction": e.direction,
                "market_id": e.poly_market_id
            }
            for e in edges
        ],
        "top_opportunities": [
            {
                "selection": e.selection,
                "sport": e.sport,
                "edge": f"{e.edge_pct*100:+.1f}%" if e.edge_pct else None,
                "action": f"{e.direction} on Poly at {e.polymarket_price*100:.0f}¢" if e.polymarket_price else None
            }
            for e in edges[:5]
        ]
    }


if __name__ == "__main__":
    async def test():
        print("Finding Betfair vs Polymarket edges...")
        summary = await get_betfair_edge_summary()
        
        print(f"\nFound {summary['total_edges']} edges:\n")
        for e in summary['edges'][:10]:
            print(f"⚡ {e['selection']} ({e['sport']})")
            print(f"   Betfair: {e['betfair_prob']}% (@ {e['betfair_price']})")
            print(f"   Polymarket: {e['polymarket_price']}¢")
            print(f"   Edge: {e['edge_pct']}% → {e['direction']}")
            print()
    
    asyncio.run(test())

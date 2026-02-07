"""
ESPN Odds Scraper
Free API that provides DraftKings odds for all major US sports
No API key required, unlimited calls
"""

import json
import urllib.request
from datetime import datetime
from typing import List, Dict, Optional
from dataclasses import dataclass

ESPN_API = "https://site.api.espn.com/apis/site/v2/sports"

SPORTS = {
    "nfl": ("football", "nfl"),
    "nba": ("basketball", "nba"),
    "nhl": ("hockey", "nhl"),
    "mlb": ("baseball", "mlb"),
    "ncaaf": ("football", "college-football"),
    "ncaab": ("basketball", "mens-college-basketball"),
}


@dataclass
class GameOdds:
    sport: str
    game_id: str
    home_team: str
    away_team: str
    start_time: str
    spread: Optional[float]
    spread_odds: Optional[int]
    favorite: str  # HOME or AWAY
    over_under: Optional[float]
    provider: str
    

def fetch_odds(sport: str = "nfl") -> List[GameOdds]:
    """Fetch current odds for a sport from ESPN (DraftKings source)"""
    if sport not in SPORTS:
        raise ValueError(f"Sport must be one of: {list(SPORTS.keys())}")
    
    sport_path, league = SPORTS[sport]
    url = f"{ESPN_API}/{sport_path}/{league}/scoreboard"
    
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"ESPN fetch error: {e}")
        return []
    
    games = []
    
    for event in data.get("events", []):
        comp = event.get("competitions", [{}])[0]
        odds_list = comp.get("odds", [])
        
        if not odds_list:
            continue
            
        odds = odds_list[0]
        competitors = comp.get("competitors", [])
        
        if len(competitors) != 2:
            continue
        
        # ESPN: first competitor is home, second is away
        home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
        away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])
        
        home_team = home.get("team", {}).get("displayName", "Unknown")
        away_team = away.get("team", {}).get("displayName", "Unknown")
        
        spread = odds.get("spread")
        details = odds.get("details", "")
        
        # Determine favorite from details (e.g., "SEA -4.5")
        favorite = "HOME"
        if details:
            if away_team[:3].upper() in details.upper() and "-" in details:
                favorite = "AWAY"
            elif home_team[:3].upper() in details.upper() and "-" in details:
                favorite = "HOME"
        
        games.append(GameOdds(
            sport=sport.upper(),
            game_id=event.get("id", ""),
            home_team=home_team,
            away_team=away_team,
            start_time=event.get("date", ""),
            spread=float(spread) if spread else None,
            spread_odds=odds.get("spreadOdds"),
            favorite=favorite,
            over_under=odds.get("overUnder"),
            provider=odds.get("provider", {}).get("name", "Unknown"),
        ))
    
    return games


def fetch_all_odds() -> Dict[str, List[GameOdds]]:
    """Fetch odds for all supported sports"""
    all_odds = {}
    for sport in SPORTS.keys():
        try:
            all_odds[sport] = fetch_odds(sport)
        except Exception as e:
            print(f"Error fetching {sport}: {e}")
            all_odds[sport] = []
    return all_odds


def american_to_prob(odds: int) -> float:
    """Convert American odds to implied probability"""
    if odds < 0:
        return abs(odds) / (abs(odds) + 100)
    else:
        return 100 / (odds + 100)


def spread_to_moneyline_prob(spread: float) -> tuple:
    """
    Rough conversion of spread to implied moneyline probability.
    Each point of spread ≈ 2.5-3% probability difference from 50%.
    """
    if spread is None:
        return 0.5, 0.5
    
    # Favorite (negative spread) gets higher probability
    # Rough formula: 50% + (spread * 2.75%)
    adjustment = abs(spread) * 0.0275
    
    if spread > 0:  # Home is underdog
        home_prob = 0.5 - adjustment
        away_prob = 0.5 + adjustment
    else:  # Home is favorite
        home_prob = 0.5 + adjustment
        away_prob = 0.5 - adjustment
    
    # Clamp to reasonable range
    home_prob = max(0.05, min(0.95, home_prob))
    away_prob = max(0.05, min(0.95, away_prob))
    
    return home_prob, away_prob


def find_polymarket_edges(poly_events: List[Dict], min_edge: float = 5.0) -> List[Dict]:
    """
    Compare ESPN/DraftKings odds against Polymarket prices.
    Returns list of edges where ESPN probability differs from Poly by min_edge%.
    """
    try:
        from smart_matcher import match_markets
    except ImportError:
        from odds.smart_matcher import match_markets
    
    all_odds = fetch_all_odds()
    edges = []
    
    for sport, games in all_odds.items():
        for game in games:
            # Build search terms
            home_short = game.home_team.split()[-1]  # "Patriots"
            away_short = game.away_team.split()[-1]  # "Seahawks"
            
            # Get implied probabilities from spread
            home_prob, away_prob = spread_to_moneyline_prob(game.spread)
            
            # Search Polymarket for matching events
            for poly in poly_events:
                poly_title = poly.get("title", "").lower()
                
                # Check if this Poly market matches the game
                if home_short.lower() in poly_title or away_short.lower() in poly_title:
                    # Get Poly price
                    for mkt in poly.get("markets", []):
                        question = mkt.get("question", "").lower()
                        
                        # Match team to market
                        if home_short.lower() in question:
                            poly_price = None
                            outcome_prices = mkt.get("outcomePrices", {})
                            if isinstance(outcome_prices, str):
                                try:
                                    outcome_prices = json.loads(outcome_prices)
                                except:
                                    outcome_prices = {}
                            
                            poly_price = outcome_prices.get("Yes") if isinstance(outcome_prices, dict) else None
                            if poly_price:
                                poly_price = float(poly_price)
                                edge = (home_prob - poly_price) * 100
                                
                                if abs(edge) >= min_edge:
                                    edges.append({
                                        "sport": sport,
                                        "game": f"{away_short} @ {home_short}",
                                        "team": game.home_team,
                                        "espn_prob": round(home_prob * 100, 1),
                                        "espn_spread": game.spread,
                                        "polymarket_price": round(poly_price * 100, 1),
                                        "polymarket_id": mkt.get("id", ""),
                                        "edge_pct": round(edge, 1),
                                        "direction": "BUY" if edge > 0 else "SELL",
                                        "provider": game.provider,
                                    })
                        
                        elif away_short.lower() in question:
                            poly_price = None
                            outcome_prices = mkt.get("outcomePrices", {})
                            if isinstance(outcome_prices, str):
                                try:
                                    outcome_prices = json.loads(outcome_prices)
                                except:
                                    outcome_prices = {}
                            
                            poly_price = outcome_prices.get("Yes") if isinstance(outcome_prices, dict) else None
                            if poly_price:
                                poly_price = float(poly_price)
                                edge = (away_prob - poly_price) * 100
                                
                                if abs(edge) >= min_edge:
                                    edges.append({
                                        "sport": sport,
                                        "game": f"{away_short} @ {home_short}",
                                        "team": game.away_team,
                                        "espn_prob": round(away_prob * 100, 1),
                                        "espn_spread": game.spread,
                                        "polymarket_price": round(poly_price * 100, 1),
                                        "polymarket_id": mkt.get("id", ""),
                                        "edge_pct": round(edge, 1),
                                        "direction": "BUY" if edge > 0 else "SELL",
                                        "provider": game.provider,
                                    })
    
    # Sort by absolute edge
    edges.sort(key=lambda x: abs(x["edge_pct"]), reverse=True)
    return edges


async def get_espn_edges(min_edge: float = 5.0) -> Dict:
    """Main entry point for ESPN edge detection"""
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
    
    edges = find_polymarket_edges(poly_events, min_edge)
    
    # Also return raw odds data
    all_odds = fetch_all_odds()
    games_summary = {}
    for sport, games in all_odds.items():
        games_summary[sport] = [
            {
                "game": f"{g.away_team} @ {g.home_team}",
                "spread": g.spread,
                "favorite": g.favorite,
                "over_under": g.over_under,
                "start_time": g.start_time,
            }
            for g in games
        ]
    
    return {
        "source": "ESPN (DraftKings odds)",
        "timestamp": datetime.utcnow().isoformat(),
        "sports_covered": list(SPORTS.keys()),
        "total_games": sum(len(g) for g in all_odds.values()),
        "edges_found": len(edges),
        "min_edge_filter": min_edge,
        "edges": edges[:20],
        "games": games_summary,
    }


def get_espn_summary() -> Dict:
    """Get summary of all current ESPN odds"""
    all_odds = fetch_all_odds()
    
    summary = {
        "source": "ESPN (DraftKings)",
        "timestamp": datetime.utcnow().isoformat(),
        "provider": "DraftKings",
        "sports": {},
    }
    
    for sport, games in all_odds.items():
        summary["sports"][sport.upper()] = {
            "games": len(games),
            "matchups": [
                {
                    "game": f"{g.away_team} @ {g.home_team}",
                    "spread": f"{g.favorite} {g.spread}",
                    "over_under": g.over_under,
                    "start": g.start_time,
                }
                for g in games
            ]
        }
    
    summary["total_games"] = sum(len(g) for g in all_odds.values())
    
    return summary


if __name__ == "__main__":
    import asyncio
    
    print("Testing ESPN Odds integration...")
    print()
    
    # Test individual sport
    nfl = fetch_odds("nfl")
    print(f"NFL Games: {len(nfl)}")
    for g in nfl:
        print(f"  {g.away_team} @ {g.home_team}: spread={g.spread}, o/u={g.over_under}")
    
    print()
    
    # Test all sports
    summary = get_espn_summary()
    print(f"Total games across all sports: {summary['total_games']}")
    
    print()
    
    # Test edge detection
    result = asyncio.run(get_espn_edges())
    print(f"Edges found: {result['edges_found']}")
    for e in result["edges"][:5]:
        print(f"  {e['game']} - {e['team']}: ESPN {e['espn_prob']}% vs Poly {e['polymarket_price']}% = {e['edge_pct']}%")

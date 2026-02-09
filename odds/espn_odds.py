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
    # Moneyline odds (added Feb 2026)
    home_moneyline: Optional[int] = None
    away_moneyline: Optional[int] = None
    home_moneyline_open: Optional[int] = None
    away_moneyline_open: Optional[int] = None
    

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
        
        # Parse moneyline odds
        home_ml = None
        away_ml = None
        home_ml_open = None
        away_ml_open = None
        
        moneyline = odds.get("moneyline", {})
        if moneyline:
            home_ml_data = moneyline.get("home", {})
            away_ml_data = moneyline.get("away", {})
            
            # Current (close) odds
            if home_ml_data.get("close", {}).get("odds"):
                try:
                    home_ml = int(home_ml_data["close"]["odds"].replace("+", ""))
                except (ValueError, TypeError):
                    pass
            
            if away_ml_data.get("close", {}).get("odds"):
                try:
                    away_ml = int(away_ml_data["close"]["odds"].replace("+", ""))
                except (ValueError, TypeError):
                    pass
            
            # Opening odds
            if home_ml_data.get("open", {}).get("odds"):
                try:
                    home_ml_open = int(home_ml_data["open"]["odds"].replace("+", ""))
                except (ValueError, TypeError):
                    pass
            
            if away_ml_data.get("open", {}).get("odds"):
                try:
                    away_ml_open = int(away_ml_data["open"]["odds"].replace("+", ""))
                except (ValueError, TypeError):
                    pass
        
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
            home_moneyline=home_ml,
            away_moneyline=away_ml,
            home_moneyline_open=home_ml_open,
            away_moneyline_open=away_ml_open,
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


def get_moneyline(sport: str = "nfl") -> List[Dict]:
    """
    Fetch moneyline odds for a sport from ESPN (DraftKings source).
    Returns list of games with moneyline odds and implied probabilities.
    """
    games = fetch_odds(sport)
    
    results = []
    for game in games:
        if game.home_moneyline is None or game.away_moneyline is None:
            continue
        
        home_prob = american_to_prob(game.home_moneyline)
        away_prob = american_to_prob(game.away_moneyline)
        
        # Remove vig to get true probabilities
        total_prob = home_prob + away_prob
        home_true = home_prob / total_prob if total_prob > 0 else 0.5
        away_true = away_prob / total_prob if total_prob > 0 else 0.5
        
        results.append({
            "sport": sport.upper(),
            "game_id": game.game_id,
            "matchup": f"{game.away_team} @ {game.home_team}",
            "home_team": game.home_team,
            "away_team": game.away_team,
            "start_time": game.start_time,
            "home_moneyline": game.home_moneyline,
            "away_moneyline": game.away_moneyline,
            "home_prob_raw": round(home_prob, 4),
            "away_prob_raw": round(away_prob, 4),
            "home_prob_true": round(home_true, 4),
            "away_prob_true": round(away_true, 4),
            "vig_pct": round((total_prob - 1) * 100, 2),
            "provider": game.provider,
            # Movement from open
            "home_ml_open": game.home_moneyline_open,
            "away_ml_open": game.away_moneyline_open,
        })
    
    return results


def get_all_moneylines() -> Dict[str, List[Dict]]:
    """Fetch moneyline odds for all supported sports."""
    all_ml = {}
    for sport in SPORTS.keys():
        try:
            all_ml[sport] = get_moneyline(sport)
        except Exception as e:
            print(f"Error fetching {sport} moneylines: {e}")
            all_ml[sport] = []
    return all_ml


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
    """Get summary of all current ESPN odds including moneylines"""
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
                    # Moneyline data
                    "moneyline": {
                        "home": g.home_moneyline,
                        "away": g.away_moneyline,
                    } if g.home_moneyline else None,
                }
                for g in games
            ]
        }
    
    summary["total_games"] = sum(len(g) for g in all_odds.values())
    
    return summary


def get_injuries(sport: str = "nfl") -> List[Dict]:
    """
    Fetch injury report for a sport.
    
    Returns:
        List of injured players with status (Out, Doubtful, Questionable, Probable)
    """
    if sport not in SPORTS:
        return []
    
    sport_path, league = SPORTS[sport]
    url = f"{ESPN_API}/{sport_path}/{league}/injuries"
    
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"ESPN injuries error: {e}")
        return []
    
    injuries = []
    
    for team in data.get("athletes", []):
        team_name = team.get("team", {}).get("displayName", "Unknown")
        
        for athlete in team.get("injuries", []):
            player = athlete.get("athlete", {})
            injury = athlete.get("injuries", [{}])[0] if athlete.get("injuries") else {}
            
            status = injury.get("status", "Unknown")
            
            # Only include significant injuries (Out, Doubtful, Questionable)
            if status in ["Out", "Doubtful", "Questionable", "Injured Reserve", "IR"]:
                injuries.append({
                    "player": player.get("displayName", "Unknown"),
                    "team": team_name,
                    "position": player.get("position", {}).get("abbreviation", ""),
                    "status": status,
                    "injury": injury.get("type", {}).get("description", ""),
                    "detail": injury.get("longComment", "")[:100] if injury.get("longComment") else "",
                })
    
    return injuries


def get_key_injuries(sport: str = "nfl") -> Dict:
    """
    Get key injuries that could impact betting lines.
    
    Filters to starters and important players.
    """
    all_injuries = get_injuries(sport)
    
    # Key positions by sport
    key_positions = {
        "nfl": ["QB", "RB", "WR", "TE", "LT", "DE", "LB", "CB"],
        "nba": ["PG", "SG", "SF", "PF", "C"],
        "nhl": ["G", "C", "LW", "RW", "D"],
        "mlb": ["P", "SP", "RP", "C", "SS"],
    }
    
    positions = key_positions.get(sport, [])
    
    key = [i for i in all_injuries if i["position"] in positions]
    
    # Group by team
    by_team = {}
    for inj in key:
        team = inj["team"]
        if team not in by_team:
            by_team[team] = []
        by_team[team].append(inj)
    
    return {
        "sport": sport.upper(),
        "total_injuries": len(all_injuries),
        "key_injuries": len(key),
        "by_team": by_team,
        "impactful": [i for i in key if i["status"] in ["Out", "Doubtful"]],
    }


def get_standings(sport: str = "nfl") -> List[Dict]:
    """
    Fetch current standings for a sport.
    
    Returns:
        List of teams with wins, losses, division, etc.
    """
    if sport not in SPORTS:
        return []
    
    sport_path, league = SPORTS[sport]
    url = f"{ESPN_API}/{sport_path}/{league}/standings"
    
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"ESPN standings error: {e}")
        return []
    
    standings = []
    
    for group in data.get("children", []):
        division = group.get("name", "Unknown")
        
        for conf in group.get("standings", {}).get("entries", []):
            team = conf.get("team", {})
            stats = {s.get("name"): s.get("value") for s in conf.get("stats", [])}
            
            standings.append({
                "team": team.get("displayName", "Unknown"),
                "abbrev": team.get("abbreviation", ""),
                "division": division,
                "wins": int(stats.get("wins", 0)),
                "losses": int(stats.get("losses", 0)),
                "ties": int(stats.get("ties", 0)) if "ties" in stats else None,
                "win_pct": float(stats.get("winPercent", 0)),
                "points_for": stats.get("pointsFor"),
                "points_against": stats.get("pointsAgainst"),
            })
    
    return sorted(standings, key=lambda x: x["win_pct"], reverse=True)


def get_team_form(team_name: str, sport: str = "nfl") -> Dict:
    """
    Get recent form for a team (wins/losses in last 5-10 games).
    
    Useful for momentum-based betting.
    """
    standings = get_standings(sport)
    
    # Find team
    team = None
    for t in standings:
        if team_name.lower() in t["team"].lower() or team_name.lower() in t["abbrev"].lower():
            team = t
            break
    
    if not team:
        return {"error": f"Team {team_name} not found"}
    
    return {
        "team": team["team"],
        "record": f"{team['wins']}-{team['losses']}" + (f"-{team['ties']}" if team["ties"] else ""),
        "win_pct": round(team["win_pct"], 3),
        "division": team["division"],
        "points_for": team.get("points_for"),
        "points_against": team.get("points_against"),
    }


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
    
    print("\n" + "="*50)
    print("\nTesting injuries...")
    injuries = get_key_injuries("nfl")
    print(f"Key injuries: {injuries['key_injuries']}")
    for i in injuries.get("impactful", [])[:5]:
        print(f"  {i['player']} ({i['team']} - {i['position']}): {i['status']} - {i['injury']}")

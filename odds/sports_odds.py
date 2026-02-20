"""
Sports Odds Module — ActionNetwork + cross-reference with Polymarket.

Free API, no key required. Covers NBA, NFL, NHL, MLB, NCAAF, NCAAB.
Provides moneyline odds from 6+ books, converts to implied probabilities,
and compares against Polymarket single-game markets for edge detection.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

ACTION_API = "https://api.actionnetwork.com/web/v1/scoreboard"

SPORTS = {
    "nba": "nba",
    "nfl": "nfl",
    "nhl": "nhl",
    "mlb": "mlb",
    "ncaaf": "ncaaf",
    "ncaab": "ncaab",
    "soccer": "soccer",
}

# Book ID mapping (ActionNetwork)
BOOK_NAMES = {
    15: "DraftKings",
    30: "FanDuel",
    68: "BetMGM",
    69: "Caesars",
    71: "PointsBet",
    75: "BetRivers",
    76: "Bet365",
}


@dataclass
class GameOdds:
    game_id: int
    sport: str
    home_team: str
    away_team: str
    home_abbr: str
    away_abbr: str
    start_time: str
    # Consensus (average across books)
    home_ml: Optional[int] = None
    away_ml: Optional[int] = None
    home_implied_prob: Optional[float] = None
    away_implied_prob: Optional[float] = None
    spread: Optional[float] = None
    total: Optional[float] = None
    # Best line
    best_home_ml: Optional[int] = None
    best_away_ml: Optional[int] = None
    best_home_book: str = ""
    best_away_book: str = ""
    # Number of books
    num_books: int = 0


def american_to_implied_prob(odds: int) -> float:
    """Convert American odds to implied probability (0-1)."""
    if odds is None:
        return 0.5
    if odds > 0:
        return 100 / (odds + 100)
    else:
        return abs(odds) / (abs(odds) + 100)


def devig_probs(home_prob: float, away_prob: float) -> Tuple[float, float]:
    """Remove vig from implied probabilities to get true probabilities."""
    total = home_prob + away_prob
    if total == 0:
        return 0.5, 0.5
    return home_prob / total, away_prob / total


def fetch_action_odds(sport: str = "nba") -> List[GameOdds]:
    """Fetch current odds from ActionNetwork API."""
    if sport not in SPORTS:
        return []
    
    try:
        r = httpx.get(
            f"{ACTION_API}/{SPORTS[sport]}",
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code != 200:
            logger.warning(f"ActionNetwork {sport} returned {r.status_code}")
            return []
        
        data = r.json()
        games = data.get("games", [])
    except Exception as e:
        logger.error(f"ActionNetwork fetch failed: {e}")
        return []
    
    results = []
    for g in games:
        teams = g.get("teams", [])
        if len(teams) < 2:
            continue
        
        home = next((t for t in teams if t.get("home_away") == "home"), teams[0])
        away = next((t for t in teams if t.get("home_away") == "away"), teams[1])
        
        odds_list = g.get("odds", [])
        if not odds_list:
            continue
        
        # Collect moneylines from all books
        home_mls = []
        away_mls = []
        spreads = []
        totals = []
        best_home = (None, "")
        best_away = (None, "")
        
        for o in odds_list:
            ml_h = o.get("ml_home")
            ml_a = o.get("ml_away")
            book_id = o.get("book_id")
            book_name = BOOK_NAMES.get(book_id, str(book_id))
            
            if ml_h is not None and ml_a is not None:
                home_mls.append(ml_h)
                away_mls.append(ml_a)
                
                # Track best line for each side
                if best_home[0] is None or ml_h > best_home[0]:
                    best_home = (ml_h, book_name)
                if best_away[0] is None or ml_a > best_away[0]:
                    best_away = (ml_a, book_name)
            
            sp = o.get("spread_home")
            if sp is not None:
                spreads.append(sp)
            tot = o.get("total")
            if tot is not None:
                totals.append(tot)
        
        if not home_mls:
            continue
        
        avg_home_ml = int(sum(home_mls) / len(home_mls))
        avg_away_ml = int(sum(away_mls) / len(away_mls))
        
        home_prob = american_to_implied_prob(avg_home_ml)
        away_prob = american_to_implied_prob(avg_away_ml)
        true_home, true_away = devig_probs(home_prob, away_prob)
        
        game = GameOdds(
            game_id=g.get("id", 0),
            sport=sport,
            home_team=home.get("full_name", ""),
            away_team=away.get("full_name", ""),
            home_abbr=home.get("abbr", ""),
            away_abbr=away.get("abbr", ""),
            start_time=g.get("start_time", ""),
            home_ml=avg_home_ml,
            away_ml=avg_away_ml,
            home_implied_prob=round(true_home, 4),
            away_implied_prob=round(true_away, 4),
            spread=round(sum(spreads) / len(spreads), 1) if spreads else None,
            total=round(sum(totals) / len(totals), 1) if totals else None,
            best_home_ml=best_home[0],
            best_away_ml=best_away[0],
            best_home_book=best_home[1],
            best_away_book=best_away[1],
            num_books=len(home_mls),
        )
        results.append(game)
    
    return results


def find_polymarket_sports_edges(sport: str = "nba", min_edge: float = 5.0) -> List[Dict]:
    """Compare ActionNetwork odds vs Polymarket single-game markets.
    
    Returns edges where Polymarket price diverges from sharp books.
    """
    games = fetch_action_odds(sport)
    if not games:
        return []
    
    # Fetch Polymarket sports markets
    try:
        r = httpx.get(
            "https://gamma-api.polymarket.com/events",
            params={"active": "true", "closed": "false", "limit": 100,
                    "order": "volume24hr", "ascending": "false"},
            timeout=20,
            headers={"User-Agent": "Polyclawd/1.0"},
        )
        events = r.json() if r.status_code == 200 else []
    except Exception:
        events = []
    
    # Build lookup of Polymarket prices by team name
    poly_markets = {}
    for ev in events:
        for m in ev.get("markets", []):
            q = m.get("question", "").lower()
            prices = m.get("outcomePrices")
            if not prices:
                continue
            try:
                pl = json.loads(prices) if isinstance(prices, str) else prices
                yes_price = float(pl[0])
            except:
                continue
            
            # Store with condition_id
            poly_markets[q] = {
                "yes_price": yes_price,
                "condition_id": m.get("conditionId", ""),
                "question": m.get("question", ""),
                "volume": float(m.get("volume", 0) or 0),
            }
    
    edges = []
    for game in games:
        # Try to match game to Polymarket market
        home_lower = game.home_team.lower()
        away_lower = game.away_team.lower()
        home_abbr = game.home_abbr.lower()
        away_abbr = game.away_abbr.lower()
        
        for q, pm in poly_markets.items():
            # Only match single-game markets: "Will X win on YYYY-MM-DD?"
            is_single_game = bool(re.search(r"win on \d{4}-\d{2}-\d{2}", q))
            if not is_single_game:
                continue
            if (home_lower in q or home_abbr in q) and ("win" in q):
                # Polymarket YES price = probability home wins
                poly_prob = pm["yes_price"]
                sharp_prob = game.home_implied_prob
                edge = (sharp_prob - poly_prob) * 100  # Positive = undervalued on Poly
                
                if abs(edge) >= min_edge:
                    edges.append({
                        "game": f"{game.away_team} @ {game.home_team}",
                        "team": game.home_team,
                        "side": "YES" if edge > 0 else "NO",
                        "polymarket_prob": round(poly_prob * 100, 1),
                        "sharp_prob": round(sharp_prob * 100, 1),
                        "edge_pct": round(abs(edge), 1),
                        "edge_direction": "undervalued" if edge > 0 else "overvalued",
                        "consensus_ml": game.home_ml,
                        "best_ml": game.best_home_ml,
                        "best_book": game.best_home_book,
                        "num_books": game.num_books,
                        "polymarket_volume": pm["volume"],
                        "condition_id": pm["condition_id"],
                        "sport": game.sport,
                        "start_time": game.start_time,
                    })
            
            elif (away_lower in q or away_abbr in q) and ("win" in q):
                poly_prob = pm["yes_price"]
                sharp_prob = game.away_implied_prob
                edge = (sharp_prob - poly_prob) * 100
                
                if abs(edge) >= min_edge:
                    edges.append({
                        "game": f"{game.away_team} @ {game.home_team}",
                        "team": game.away_team,
                        "side": "YES" if edge > 0 else "NO",
                        "polymarket_prob": round(poly_prob * 100, 1),
                        "sharp_prob": round(sharp_prob * 100, 1),
                        "edge_pct": round(abs(edge), 1),
                        "edge_direction": "undervalued" if edge > 0 else "overvalued",
                        "consensus_ml": game.away_ml,
                        "best_ml": game.best_away_ml,
                        "best_book": game.best_away_book,
                        "num_books": game.num_books,
                        "polymarket_volume": pm["volume"],
                        "condition_id": pm["condition_id"],
                        "sport": game.sport,
                        "start_time": game.start_time,
                    })
    
    edges.sort(key=lambda x: x["edge_pct"], reverse=True)
    return edges


def get_sports_odds_summary(sports: List[str] = None) -> Dict:
    """Get odds summary across all sports."""
    if sports is None:
        sports = ["nba", "nfl", "nhl"]
    
    all_games = []
    for sport in sports:
        games = fetch_action_odds(sport)
        for g in games:
            all_games.append({
                "sport": g.sport.upper(),
                "game": f"{g.away_team} @ {g.home_team}",
                "home_ml": g.home_ml,
                "away_ml": g.away_ml,
                "home_prob": round(g.home_implied_prob * 100, 1) if g.home_implied_prob else None,
                "away_prob": round(g.away_implied_prob * 100, 1) if g.away_implied_prob else None,
                "spread": g.spread,
                "total": g.total,
                "books": g.num_books,
                "start_time": g.start_time,
            })
    
    return {
        "games": all_games,
        "total_games": len(all_games),
        "sports_covered": sports,
        "source": "ActionNetwork (DraftKings, FanDuel, BetMGM, Caesars, etc.)",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    print("=== Sports Odds (ActionNetwork) ===\n")
    for sport in ["nba", "nhl"]:
        games = fetch_action_odds(sport)
        print(f"{sport.upper()}: {len(games)} games")
        for g in games[:3]:
            print(f"  {g.away_team} @ {g.home_team}")
            print(f"    ML: {g.away_ml}/{g.home_ml}  Prob: {g.away_implied_prob:.1%}/{g.home_implied_prob:.1%}  Spread: {g.spread}  Books: {g.num_books}")
        print()
    
    print("=== Polymarket Edge Scan ===")
    for sport in ["nba"]:
        edges = find_polymarket_sports_edges(sport, min_edge=3.0)
        print(f"\n{sport.upper()} edges: {len(edges)}")
        for e in edges[:5]:
            print(f"  {e['edge_pct']:.1f}% {e['side']} {e['team']} | Sharp: {e['sharp_prob']:.1f}% Poly: {e['polymarket_prob']:.1f}% | {e['game']}")

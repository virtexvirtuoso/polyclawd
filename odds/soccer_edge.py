"""
Soccer Edge Finder - Compare Vegas odds with Polymarket

⚠️ DEPRECATED (2026-06-02): superseded by the shared-core engines
  - odds/soccer_match_edge.py    (per-match 3-way, Shin devig, executable-gated)
  - odds/soccer_futures_edge.py  (World Cup / outright winners)
  served at /api/soccer/match-edge, /api/soccer/futures-edge (+ soccer.html).

NOT yet deleted: this module is still imported by api/edge_cache.py
(fetch_soccer_edges -> aggregate_all_signals, the LIVE signal pipeline),
odds/__init__.py, and odds/the_odds_api.py (which baseball also depends on).
Full removal requires decoupling those in a dedicated, paper-tested change so
the live aggregation/baseball paths are not disturbed. Do not edit for new work.
"""

import requests
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional
from datetime import datetime

# Import from same directory
try:
    from .vegas_scraper import get_vegas_odds_with_fallback, VegasOdds
    from .client import devig_multiway
except ImportError:
    from vegas_scraper import get_vegas_odds_with_fallback
    from client import devig_multiway


@dataclass
class SoccerEdge:
    team: str
    league: str
    vegas_prob: float
    vegas_odds: int
    polymarket_price: float
    edge_pct: float
    direction: str  # "BUY" or "SELL"
    poly_market_id: Optional[str] = None
    poly_yes_token: Optional[str] = None
    executable_price: Optional[float] = None
    executable_edge: Optional[float] = None
    book_spread: Optional[float] = None
    slippage_bps: Optional[float] = None
    tradeable: bool = False


# Polymarket market mappings
POLYMARKET_SEARCHES = {
    "epl": "Premier League Winner",
    "ucl": "Champions League Winner",
    "world_cup": "World Cup Winner",
    "la_liga": "La Liga Winner",
    "bundesliga": "Bundesliga Winner",
}

# Team name normalization (Vegas -> Polymarket)
TEAM_ALIASES = {
    "Man City": ["Manchester City", "Man City"],
    "Manchester City": ["Manchester City", "Man City"],
    "Man Utd": ["Manchester United", "Man United", "Man Utd"],
    "PSG": ["Paris Saint-Germain", "PSG"],
    "Paris Saint-Germain": ["Paris Saint-Germain", "PSG"],
    "Bayern Munich": ["Bayern Munich", "Bayern"],
    "Spurs": ["Tottenham", "Spurs"],
    "Tottenham": ["Tottenham", "Spurs"],
    "Inter Milan": ["Inter", "Inter Milan"],
    "Inter": ["Inter", "Inter Milan"],
    "Atletico Madrid": ["Atletico Madrid", "Atlético Madrid", "Atletico"],
    "Dortmund": ["Dortmund", "Borussia Dortmund"],
    "Borussia Dortmund": ["Dortmund", "Borussia Dortmund"],
    "Leverkusen": ["Bayer Leverkusen", "Leverkusen"],
    "Bayer Leverkusen": ["Bayer Leverkusen", "Leverkusen"],
}


def normalize_team(team: str) -> list[str]:
    """Get all possible team name variations"""
    team = team.strip()
    if team in TEAM_ALIASES:
        return TEAM_ALIASES[team]
    return [team]


def _fetch_polymarket_sync() -> dict:
    """Synchronous fetch of Polymarket data"""
    try:
        resp = requests.get(
            "https://gamma-api.polymarket.com/events", params={"closed": "false", "limit": "200"}, timeout=30
        )
        return resp.json()
    except Exception as e:
        print(f"Error fetching Polymarket: {e}")
        return []


async def get_polymarket_soccer_markets() -> dict[str, dict[str, tuple[float, str]]]:
    """
    Fetch Polymarket soccer markets
    Returns: {league: {team: (price, market_id)}}
    """
    results = {}

    # Run sync request in thread pool
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as executor:
        events = await loop.run_in_executor(executor, _fetch_polymarket_sync)

    for search_key, search_term in POLYMARKET_SEARCHES.items():
        results[search_key] = {}

        for event in events:
            if search_term.lower() in event.get("title", "").lower():
                for market in event.get("markets", []):
                    question = market.get("question", "")
                    price = market.get("bestAsk", 0)
                    market_id = market.get("id", "")
                    toks = market.get("clobTokenIds", "[]")
                    if isinstance(toks, str):
                        try:
                            toks = json.loads(toks)
                        except Exception:
                            toks = []
                    yes_token = toks[0] if isinstance(toks, list) and toks else None

                    # Extract team name from question
                    # Pattern: "Will X win the ..."
                    if "Will " in question and " win " in question:
                        team = question.split("Will ")[1].split(" win ")[0].strip()
                        if price and price < 1:  # Valid price
                            results[search_key][team] = (float(price), market_id, yes_token)
                break  # Found the event

    return results


def match_team(vegas_team: str, poly_teams: dict[str, tuple[float, str]]) -> Optional[tuple[str, float, str]]:
    """
    Match Vegas team name to Polymarket team
    Returns: (matched_name, price, market_id) or None
    """
    variations = normalize_team(vegas_team)

    for var in variations:
        for poly_team, vals in poly_teams.items():
            price, market_id = vals[0], vals[1]
            yes_token = vals[2] if len(vals) > 2 else None
            if var.lower() == poly_team.lower():
                return (poly_team, price, market_id, yes_token)
            # Partial match
            if var.lower() in poly_team.lower() or poly_team.lower() in var.lower():
                return (poly_team, price, market_id, yes_token)

    return None


async def find_soccer_edges(min_edge: float = 0.01) -> list[SoccerEdge]:
    """
    Find edges between Vegas odds and Polymarket for soccer

    Args:
        min_edge: Minimum edge percentage to include (default 1%)

    Returns:
        List of SoccerEdge objects sorted by edge size
    """
    edges = []

    # Get both data sources
    vegas_data = await get_vegas_odds_with_fallback()
    poly_data = await get_polymarket_soccer_markets()

    for league, vegas_odds_list in vegas_data.items():
        poly_markets = poly_data.get(league, {})

        if not poly_markets or not vegas_odds_list:
            continue

        # DEVIG: Remove vig from Vegas odds for this league
        # Sum implied probs and normalize to get true probabilities
        raw_probs = [v.implied_prob for v in vegas_odds_list]
        devigged_probs = devig_multiway(raw_probs)

        # Create mapping of team -> devigged prob
        team_to_devigged = {vegas_odds_list[i].team: devigged_probs[i] for i in range(len(vegas_odds_list))}

        for vegas in vegas_odds_list:
            match = match_team(vegas.team, poly_markets)

            if match:
                poly_team, poly_price, market_id, yes_token = match

                # Use DEVIGGED probability for comparison
                true_vegas_prob = team_to_devigged.get(vegas.team, vegas.implied_prob)

                # Calculate edge using devigged prob
                # Positive edge = Vegas prob > Polymarket price (underpriced on Poly)
                edge = true_vegas_prob - poly_price

                if abs(edge) >= min_edge:
                    edges.append(
                        SoccerEdge(
                            team=vegas.team,
                            league=league.upper(),
                            vegas_prob=true_vegas_prob,  # Now using devigged prob
                            vegas_odds=vegas.american_odds,
                            polymarket_price=poly_price,
                            edge_pct=edge,
                            direction="BUY" if edge > 0 else "SELL",
                            poly_market_id=market_id,
                            poly_yes_token=yes_token,
                        )
                    )

    # --- Executable-edge enrichment: soccer already uses bestAsk (top-of-book);
    # this adds VWAP-to-$100 depth + slippage + tradeable for BUY edges.
    try:
        from odds import poly_executable_edge as pee
    except Exception:
        try:
            import poly_executable_edge as pee
        except Exception:
            pee = None
    if pee is not None:
        for e in edges:
            if e.direction != "BUY" or not e.poly_yes_token:
                continue
            try:
                ex = pee.executable_edge(e.vegas_prob, "YES", token_id=e.poly_yes_token, target_usd=100.0)
            except Exception:
                ex = {"available": False}
            if ex.get("available"):
                e.executable_price = ex["executable_price"]
                e.executable_edge = ex["executable_edge"]
                e.book_spread = ex["spread"]
                e.slippage_bps = ex["slippage_bps"]
                e.tradeable = ex["tradeable"]

    # Sort by absolute edge size
    edges.sort(key=lambda x: abs(x.edge_pct), reverse=True)

    return edges


async def get_soccer_edge_summary() -> dict:
    """
    Get a summary of soccer edges for API response
    """
    edges = await find_soccer_edges(min_edge=0.01)

    return {
        "timestamp": datetime.utcnow().isoformat(),
        "total_edges": len(edges),
        "edges": [
            {
                "team": e.team,
                "league": e.league,
                "vegas_prob": round(e.vegas_prob * 100, 1),
                "vegas_odds": f"{e.vegas_odds:+d}",
                "polymarket_price": round(e.polymarket_price * 100, 1),
                "edge_pct": round(e.edge_pct * 100, 1),
                "direction": e.direction,
                "market_id": e.poly_market_id,
                "executable_price": (round(e.executable_price * 100, 1) if e.executable_price is not None else None),
                "executable_edge": (round(e.executable_edge * 100, 1) if e.executable_edge is not None else None),
                "book_spread_pct": (round(e.book_spread * 100, 1) if e.book_spread is not None else None),
                "slippage_bps": e.slippage_bps,
                "tradeable": e.tradeable,
            }
            for e in edges
        ],
        "top_opportunities": [
            {
                "team": e.team,
                "league": e.league,
                "edge": f"{e.edge_pct * 100:+.1f}%",
                "action": f"{e.direction} at {e.polymarket_price * 100:.0f}¢",
            }
            for e in edges[:5]
        ],
    }


if __name__ == "__main__":

    async def test():
        print("Finding soccer edges...")
        summary = await get_soccer_edge_summary()

        print(f"\nFound {summary['total_edges']} edges:\n")

        for e in summary["edges"][:10]:
            emoji = "⚡" if abs(e["edge_pct"]) >= 2 else "📊"
            print(f"{emoji} {e['team']} ({e['league']})")
            print(f"   Vegas: {e['vegas_prob']}% ({e['vegas_odds']})")
            print(f"   Polymarket: {e['polymarket_price']}¢")
            print(f"   Edge: {e['edge_pct']:+.1f}% → {e['direction']}")
            print()

    asyncio.run(test())

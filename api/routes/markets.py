"""Market discovery and edge detection endpoints - consolidated from all sources.

This router consolidates all market and edge detection endpoints:
- /arb-scan, /rewards - Polymarket arbitrage and rewards scanning
- /markets/* - Market discovery (trending, search, new, opportunities)
- /vegas/* - Vegas odds edge detection (sports, odds, edge, soccer)
- /espn/* - ESPN/DraftKings odds edge detection
- /betfair/edge - Betfair Exchange edge detection
- /kalshi/markets - Kalshi vs Polymarket comparison
- /manifold/* - Manifold Markets edge detection
- /predictit/* - PredictIt edge detection
- /polyrouter/* - Cross-platform unified API (7 platforms)
"""
import json
import os
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional
import logging

import httpx
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()
logger = logging.getLogger(__name__)

# Constants
GAMMA_API = "https://gamma-api.polymarket.com"

# Sports calendar for seasonal Vegas scanning
SPORTS_CALENDAR = {
    1: ["americanfootball_nfl", "basketball_nba", "icehockey_nhl", "mma_mixed_martial_arts"],
    2: ["americanfootball_nfl", "basketball_nba", "basketball_ncaab", "icehockey_nhl", "mma_mixed_martial_arts"],
    3: ["basketball_ncaab", "basketball_nba", "icehockey_nhl", "mma_mixed_martial_arts"],
    4: ["basketball_nba", "baseball_mlb", "icehockey_nhl", "mma_mixed_martial_arts"],
    5: ["basketball_nba", "baseball_mlb", "icehockey_nhl", "mma_mixed_martial_arts"],
    6: ["basketball_nba", "baseball_mlb", "mma_mixed_martial_arts"],
    7: ["mma_mixed_martial_arts"],
    8: ["mma_mixed_martial_arts", "americanfootball_ncaaf"],
    9: ["americanfootball_nfl", "americanfootball_ncaaf", "mma_mixed_martial_arts"],
    10: ["americanfootball_nfl", "basketball_nba", "icehockey_nhl", "mma_mixed_martial_arts"],
    11: ["americanfootball_nfl", "basketball_nba", "icehockey_nhl", "mma_mixed_martial_arts"],
    12: ["americanfootball_nfl", "basketball_nba", "icehockey_nhl", "mma_mixed_martial_arts"]
}
ALWAYS_SCAN = ["politics_us_presidential_election_winner"]


# ============================================================================
# Helper Functions
# ============================================================================

async def handle_edge_request(source: str, coro):
    """Standard error handling for edge detection endpoints.

    Args:
        source: Name of the edge source (for logging/error messages)
        coro: Coroutine to execute

    Returns:
        Result from the coroutine

    Raises:
        HTTPException with appropriate status codes:
        - 503: Service unavailable (ImportError)
        - 502: Bad gateway (upstream HTTP error)
        - 422: Unprocessable entity (ValueError)
        - 500: Internal server error (other exceptions)
    """
    try:
        return await coro
    except ImportError as e:
        logger.exception(f"Failed to import {source} module: {e}")
        raise HTTPException(status_code=503, detail=f"{source} service unavailable")
    except httpx.HTTPError as e:
        logger.warning(f"{source} upstream error: {e}")
        raise HTTPException(status_code=502, detail=f"{source} upstream error")
    except ValueError as e:
        logger.warning(f"Invalid {source} data: {e}")
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.exception(f"Unexpected error in {source}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


def _api_get(endpoint: str, params: dict = None) -> list:
    """GET request to Polymarket Gamma API."""
    url = f"{GAMMA_API}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Polymarket API error: {str(e)}")


def _get_market(market_id: str) -> Optional[dict]:
    """Get market by ID, slug, or conditionId."""
    for param in ["id", "slug", "conditionId"]:
        try:
            markets = _api_get("/markets", {param: market_id})
            if markets:
                return markets[0]
        except Exception:
            pass
    return None


def _get_market_prices(market: dict) -> tuple:
    """Extract YES/NO prices from market."""
    try:
        prices = json.loads(market.get("outcomePrices", "[0, 0]"))
        return float(prices[0]) if prices[0] else 0.0, float(prices[1]) if prices[1] else 0.0
    except Exception:
        return 0.0, 0.0


def _get_odds_modules_path() -> str:
    """Get path to odds modules directory."""
    return str(Path(__file__).parent.parent.parent / "odds")


def _scan_new_markets() -> dict:
    """Scan for newly created markets."""
    try:
        markets = _api_get("/markets", {
            "closed": "false",
            "active": "true",
            "limit": "100",
            "order": "createdAt",
            "ascending": "false"
        })

        new_markets = []
        for market in markets[:20]:  # Top 20 newest
            yes_price, no_price = _get_market_prices(market)
            new_markets.append({
                "id": market.get("id"),
                "question": market.get("question", "Unknown"),
                "slug": market.get("slug", ""),
                "yes_price": yes_price,
                "no_price": no_price,
                "liquidity": float(market.get("liquidityNum", 0)),
                "created_at": market.get("createdAt")
            })

        return {
            "new_markets": new_markets,
            "count": len(new_markets),
            "scan_time": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Failed to scan new markets: {e}")
        return {"new_markets": [], "count": 0, "error": str(e)}


# ============================================================================
# Arbitrage & Rewards Endpoints
# ============================================================================

@router.get("/arb-scan")
async def arb_scan(limit: int = Query(default=50, ge=1, le=100)):
    """Scan for arbitrage opportunities on Polymarket.

    Finds markets where YES + NO prices deviate from 1.0, indicating
    potential arbitrage or mispricing.
    """
    try:
        markets = _api_get("/markets", {
            "closed": "false",
            "active": "true",
            "limit": str(limit),
            "order": "volume24hr",
            "ascending": "false"
        })

        opportunities = []
        for market in markets:
            yes_price, no_price = _get_market_prices(market)
            if yes_price <= 0 or no_price <= 0:
                continue
            total = yes_price + no_price
            if total < 0.99 or total > 1.01:
                opportunities.append({
                    "market_id": market["id"],
                    "question": market.get("question", "Unknown"),
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "total": total,
                    "spread": abs(1.0 - total),
                    "type": "underpriced" if total < 0.99 else "overpriced",
                })

        opportunities.sort(key=lambda x: x["spread"], reverse=True)
        return {
            "count": len(opportunities),
            "opportunities": opportunities[:20],
            "scanned_at": datetime.now().isoformat()
        }
    except Exception as e:
        logger.exception("Arb scan failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/rewards")
async def get_rewards():
    """Find markets with liquidity rewards opportunities.

    Scans for markets with reward incentives and calculates an
    opportunity score based on daily rates, competitiveness, and liquidity.
    """
    try:
        markets = _api_get("/markets", {"closed": "false", "active": "true", "limit": "500"})
        opportunities = []

        for market in markets:
            min_size = market.get("rewardsMinSize", 0)
            max_spread = market.get("rewardsMaxSpread", 0)
            clob_rewards = market.get("clobRewards", [])

            if not (min_size > 0 and max_spread > 0):
                if not clob_rewards or not any(r.get("rewardsDailyRate", 0) > 0 for r in clob_rewards):
                    continue

            daily_rate = sum(r.get("rewardsDailyRate", 0) for r in clob_rewards) if clob_rewards else (1.0 if max_spread > 0 else 0)
            yes_price, no_price = _get_market_prices(market)
            midpoint = (yes_price + no_price) / 2 if yes_price > 0 and no_price > 0 else 0.5
            liquidity = float(market.get("liquidityNum", 0))
            competitive = float(market.get("competitive", 0.5))

            score = min(daily_rate / 5.0, 2.0) * 30 + (1 - competitive) * 25 + min(max_spread / 3.0, 2.0) * 15
            score += 15 if liquidity > 100000 else (10 if liquidity > 10000 else 5)
            score += 15 if 0.2 <= midpoint <= 0.8 else (10 if 0.1 <= midpoint <= 0.9 else 5)

            opportunities.append({
                "market_id": market["id"],
                "question": market.get("question", "Unknown"),
                "rewards_min_size": min_size,
                "rewards_max_spread": max_spread,
                "daily_reward_rate": daily_rate,
                "midpoint": midpoint,
                "liquidity": liquidity,
                "opportunity_score": round(score, 2)
            })

        opportunities.sort(key=lambda x: x["opportunity_score"], reverse=True)
        return {
            "count": len(opportunities),
            "opportunities": opportunities[:30],
            "scanned_at": datetime.now().isoformat()
        }
    except Exception as e:
        logger.exception("Rewards scan failed")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================================
# Market Discovery Endpoints
# ============================================================================

@router.get("/markets/trending")
async def get_trending_markets(limit: int = Query(default=20, ge=1, le=50)):
    """Get trending markets by 24h volume."""
    try:
        markets = _api_get("/markets", {
            "closed": "false",
            "active": "true",
            "limit": str(limit),
            "order": "volume24hr",
            "ascending": "false"
        })

        result = []
        for market in markets:
            yes_price, no_price = _get_market_prices(market)
            result.append({
                "id": market["id"],
                "question": market.get("question", "Unknown"),
                "slug": market.get("slug", ""),
                "yes_price": yes_price,
                "no_price": no_price,
                "volume_24h": market.get("volume24hr", 0),
                "liquidity": market.get("liquidityNum", 0)
            })

        return {"markets": result}
    except Exception as e:
        logger.exception("Trending markets fetch failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/markets/search")
async def search_markets(
    q: str = Query(..., min_length=2),
    limit: int = Query(default=15, ge=1, le=30)
):
    """Search markets by query string."""
    try:
        markets = _api_get("/markets", {
            "closed": "false",
            "active": "true",
            "_q": q,
            "limit": str(limit)
        })

        result = []
        for market in markets:
            yes_price, no_price = _get_market_prices(market)
            result.append({
                "id": market["id"],
                "question": market.get("question", "Unknown"),
                "slug": market.get("slug", ""),
                "yes_price": yes_price,
                "no_price": no_price,
                "volume_24h": market.get("volume24hr", 0)
            })

        return {"markets": result, "query": q}
    except Exception as e:
        logger.exception("Market search failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/markets/new")
async def get_new_markets():
    """Detect newly created markets on Polymarket."""
    return _scan_new_markets()


@router.get("/markets/opportunities")
async def get_market_opportunities(
    min_liquidity: float = Query(default=1000, description="Minimum liquidity USD")
):
    """Get new markets with enough liquidity to trade."""
    result = _scan_new_markets()

    tradeable = [m for m in result.get("new_markets", [])
                 if m.get("liquidity", 0) >= min_liquidity]

    return {
        "opportunities": tradeable,
        "count": len(tradeable),
        "scan_time": result.get("scan_time"),
        "note": "New markets with liquidity - early mover opportunities"
    }


@router.get("/markets/{market_id}")
async def get_market_details(market_id: str):
    """Get detailed information about a specific market."""
    market = _get_market(market_id)
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")

    yes_price, no_price = _get_market_prices(market)
    return {
        "id": market["id"],
        "question": market.get("question"),
        "description": market.get("description"),
        "slug": market.get("slug"),
        "yes_price": yes_price,
        "no_price": no_price,
        "volume_24h": market.get("volume24hr", 0),
        "liquidity": market.get("liquidityNum", 0),
        "created_at": market.get("createdAt"),
        "end_date": market.get("endDate"),
        "closed": market.get("closed", False)
    }


# ============================================================================
# Vegas Odds Edge Detection
# ============================================================================

@router.get("/vegas/sports")
async def get_active_sports():
    """Get sports to scan based on current month."""
    current_month = datetime.now().month
    seasonal_sports = SPORTS_CALENDAR.get(current_month, [])
    all_sports = list(set(seasonal_sports + ALWAYS_SCAN))

    return {
        "month": current_month,
        "month_name": datetime.now().strftime("%B"),
        "sports": all_sports,
        "count": len(all_sports),
        "notes": {
            2: "Super Bowl month - NFL ends after game",
            3: "March Madness - NCAAB peak",
            9: "NFL returns!"
        }.get(current_month, None)
    }


@router.get("/vegas/odds")
async def get_vegas_odds(sport: str = Query(default="americanfootball_nfl")):
    """Get current Vegas odds from The Odds API.

    Args:
        sport: Sport key (americanfootball_nfl, basketball_nba, etc.)
    """
    # Get API key - try environment first, then keychain
    api_key = os.getenv("ODDS_API_KEY")

    if not api_key:
        # Try macOS keychain as fallback
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-a", "the-odds-api", "-s", "the-odds-api", "-w"],
                capture_output=True, text=True
            )
            api_key = result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            pass

    if not api_key:
        return {"error": "No API key configured. Set ODDS_API_KEY environment variable."}

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"https://api.the-odds-api.com/v4/sports/{sport}/odds",
                params={
                    "apiKey": api_key,
                    "regions": "us",
                    "markets": "h2h",
                    "oddsFormat": "american"
                }
            )

            if response.status_code != 200:
                return {"error": f"API error: {response.status_code}"}

            data = response.json()

            # Calculate consensus for each event
            events = []
            for event in data:
                home = event.get("home_team")
                away = event.get("away_team")
                bookmakers = event.get("bookmakers", [])

                # Get all odds for home team
                home_odds = []
                away_odds = []
                for book in bookmakers:
                    for market in book.get("markets", []):
                        if market["key"] == "h2h":
                            for outcome in market["outcomes"]:
                                if outcome["name"] == home:
                                    home_odds.append(outcome["price"])
                                elif outcome["name"] == away:
                                    away_odds.append(outcome["price"])

                if home_odds and away_odds:
                    avg_home = sum(home_odds) / len(home_odds)
                    avg_away = sum(away_odds) / len(away_odds)

                    # Convert to probability
                    def to_prob(odds):
                        if odds < 0:
                            return abs(odds) / (abs(odds) + 100)
                        return 100 / (odds + 100)

                    home_prob = to_prob(avg_home)
                    away_prob = to_prob(avg_away)

                    # Remove vig
                    total = home_prob + away_prob
                    home_true = home_prob / total
                    away_true = away_prob / total

                    events.append({
                        "event_id": event.get("id"),
                        "sport": sport,
                        "home_team": home,
                        "away_team": away,
                        "commence_time": event.get("commence_time"),
                        "home_odds": round(avg_home),
                        "away_odds": round(avg_away),
                        "home_prob_raw": round(home_prob, 4),
                        "away_prob_raw": round(away_prob, 4),
                        "home_prob_true": round(home_true, 4),
                        "away_prob_true": round(away_true, 4),
                        "vig": round((total - 1) * 100, 2),
                        "books_count": len(bookmakers)
                    })

            return {
                "sport": sport,
                "events": events,
                "quota": {
                    "remaining": response.headers.get("x-requests-remaining"),
                    "used": response.headers.get("x-requests-used")
                }
            }
        except httpx.HTTPError as e:
            logger.warning(f"Vegas odds API error: {e}")
            return {"error": f"API error: {str(e)}"}


@router.get("/vegas/edge")
async def find_vegas_edge(
    min_edge: float = Query(default=0.05, ge=0, le=1),
    sports: str = Query(default="auto")
):
    """Find edges between Vegas odds and Polymarket prices.

    Args:
        min_edge: Minimum edge to return (default 5%)
        sports: Comma-separated sport keys, or "auto" to use seasonal calendar

    Compares implied probability from sportsbooks against Polymarket prices.
    Returns opportunities where the gap exceeds min_edge.
    """
    # Use dynamic sports calendar if "auto"
    if sports == "auto":
        current_month = datetime.now().month
        sports_list = SPORTS_CALENDAR.get(current_month, ["basketball_nba", "mma_mixed_martial_arts"])
        sports = ",".join(sports_list[:3])  # Limit to 3 to conserve API calls

    # Manual mapping of known markets with current prices
    MARKET_MAPPINGS = {
        "Seattle Seahawks": {
            "polymarket_id": "540234",
            "polymarket_question": "Will the Seattle Seahawks win Super Bowl 2026?",
            "polymarket_price": 0.68,
            "sport": "americanfootball_nfl"
        },
        "New England Patriots": {
            "polymarket_id": "540227",
            "polymarket_question": "Will the New England Patriots win Super Bowl 2026?",
            "polymarket_price": 0.32,
            "sport": "americanfootball_nfl"
        },
    }

    all_edges = []
    all_errors = []

    # Scan each sport
    for sport in sports.split(","):
        sport = sport.strip()
        vegas_data = await get_vegas_odds(sport)

        if "error" in vegas_data:
            all_errors.append({"sport": sport, "error": vegas_data["error"]})
            continue

        # Process events for this sport
        for event in vegas_data.get("events", []):
            for team_key in ["home_team", "away_team"]:
                team = event.get(team_key)

                if team in MARKET_MAPPINGS:
                    mapping = MARKET_MAPPINGS[team]

                    # Get true probability from Vegas
                    prob_key = "home_prob_true" if team_key == "home_team" else "away_prob_true"
                    vegas_prob = event.get(prob_key, 0)

                    # Get Polymarket price from mapping
                    poly_price = mapping.get("polymarket_price", 0.50)

                    edge = vegas_prob - poly_price

                    if abs(edge) >= min_edge:
                        all_edges.append({
                            "team": team,
                            "sport": sport,
                            "event": f"{event['away_team']} @ {event['home_team']}",
                            "vegas_prob": round(vegas_prob, 4),
                            "vegas_odds": event.get(f"{team_key.split('_')[0]}_odds"),
                            "polymarket_price": poly_price,
                            "edge": round(edge, 4),
                            "edge_pct": round(edge * 100, 1),
                            "signal": "BUY" if edge > 0 else "SELL",
                            "polymarket_id": mapping["polymarket_id"],
                            "polymarket_question": mapping["polymarket_question"],
                            "commence_time": event.get("commence_time")
                        })

    # Sort by edge size
    all_edges.sort(key=lambda x: abs(x["edge"]), reverse=True)

    return {
        "edges": all_edges,
        "count": len(all_edges),
        "sports_scanned": sports.split(","),
        "errors": all_errors if all_errors else None,
        "min_edge_filter": min_edge,
        "generated_at": datetime.now().isoformat()
    }


@router.get("/vegas/soccer")
async def get_soccer_edges(min_edge: float = Query(default=0.01, ge=0, le=1)):
    """Get soccer futures edges between Vegas odds and Polymarket.

    Compares odds from VegasInsider with Polymarket prices for:
    - EPL (English Premier League)
    - UCL (Champions League)
    - World Cup 2026
    - La Liga
    - Bundesliga
    """
    async def _get_soccer():
        import sys
        odds_path = _get_odds_modules_path()
        if odds_path not in sys.path:
            sys.path.insert(0, odds_path)
        from soccer_edge import get_soccer_edge_summary
        return await get_soccer_edge_summary()

    return await handle_edge_request("soccer", _get_soccer())


# ----------------------------------------------------------------------------
# Individual Vegas League Endpoints
# ----------------------------------------------------------------------------

async def _get_league_edges(league: str, min_edge: float = 0.01):
    """Helper to get edges for a specific soccer league."""
    import sys
    odds_path = _get_odds_modules_path()
    if odds_path not in sys.path:
        sys.path.insert(0, odds_path)
    from soccer_edge import find_soccer_edges

    all_edges = await find_soccer_edges(min_edge)
    # Filter to specific league
    league_edges = [e for e in all_edges if e.league.lower() == league.lower()]

    return {
        "league": league.upper(),
        "timestamp": datetime.now().isoformat(),
        "total_edges": len(league_edges),
        "edges": [
            {
                "team": e.team,
                "vegas_prob": round(e.vegas_prob, 4),
                "vegas_odds": e.vegas_odds,
                "polymarket_price": round(e.polymarket_price, 4),
                "edge_pct": round(e.edge_pct * 100, 2),
                "direction": e.direction,
                "market_id": e.poly_market_id
            }
            for e in league_edges
        ]
    }


@router.get("/vegas/epl")
async def get_epl_edges(min_edge: float = Query(default=0.01, ge=0, le=1)):
    """Get English Premier League futures edges.

    Compares Vegas odds with Polymarket for EPL Winner market.
    """
    return await handle_edge_request("epl", _get_league_edges("epl", min_edge))


@router.get("/vegas/ucl")
async def get_ucl_edges(min_edge: float = Query(default=0.01, ge=0, le=1)):
    """Get UEFA Champions League futures edges.

    Compares Vegas odds with Polymarket for UCL Winner market.
    """
    return await handle_edge_request("ucl", _get_league_edges("ucl", min_edge))


@router.get("/vegas/bundesliga")
async def get_bundesliga_edges(min_edge: float = Query(default=0.01, ge=0, le=1)):
    """Get Bundesliga futures edges.

    Compares Vegas odds with Polymarket for Bundesliga Winner market.
    """
    return await handle_edge_request("bundesliga", _get_league_edges("bundesliga", min_edge))


@router.get("/vegas/laliga")
async def get_laliga_edges(min_edge: float = Query(default=0.01, ge=0, le=1)):
    """Get La Liga futures edges.

    Compares Vegas odds with Polymarket for La Liga Winner market.
    """
    return await handle_edge_request("laliga", _get_league_edges("la_liga", min_edge))


@router.get("/vegas/worldcup")
async def get_worldcup_edges(min_edge: float = Query(default=0.01, ge=0, le=1)):
    """Get World Cup 2026 futures edges.

    Compares Vegas odds with Polymarket for World Cup Winner market.
    """
    return await handle_edge_request("worldcup", _get_league_edges("world_cup", min_edge))


# ============================================================================
# ESPN Odds Edge Detection
# ============================================================================

@router.get("/espn/odds")
async def get_espn_odds():
    """Get current odds from ESPN (DraftKings source).

    Free API, no key required. Covers: NFL, NBA, NHL, MLB, NCAAF, NCAAB
    """
    try:
        import sys
        odds_path = _get_odds_modules_path()
        if odds_path not in sys.path:
            sys.path.insert(0, odds_path)
        from espn_odds import get_espn_summary
        return get_espn_summary()
    except ImportError as e:
        logger.exception("ESPN module import failed")
        raise HTTPException(status_code=503, detail="ESPN service unavailable")
    except Exception as e:
        logger.exception("ESPN odds error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/espn/edge")
async def get_espn_edge(min_edge: float = Query(default=5.0, ge=0, le=100)):
    """Find edges between ESPN/DraftKings odds and Polymarket.

    Free API, no key required. Compares spread-implied probabilities
    against Polymarket prices. Covers all major US sports.
    """
    async def _get_espn_edges():
        import sys
        odds_path = _get_odds_modules_path()
        if odds_path not in sys.path:
            sys.path.insert(0, odds_path)
        from espn_odds import get_espn_edges as espn_edge_fn
        return await espn_edge_fn(min_edge)

    return await handle_edge_request("espn", _get_espn_edges())


# ----------------------------------------------------------------------------
# Individual ESPN Sport Endpoints
# ----------------------------------------------------------------------------

def _format_espn_games(games, sport: str):
    """Format ESPN games for API response."""
    return {
        "sport": sport.upper(),
        "timestamp": datetime.now().isoformat(),
        "total_games": len(games),
        "provider": "DraftKings (via ESPN)",
        "games": [
            {
                "game_id": g.game_id,
                "matchup": f"{g.away_team} @ {g.home_team}",
                "home_team": g.home_team,
                "away_team": g.away_team,
                "spread": g.spread,
                "favorite": g.favorite,
                "over_under": g.over_under,
                "start_time": g.start_time,
            }
            for g in games
        ]
    }


async def _get_sport_odds(sport: str):
    """Helper to get odds for a specific sport."""
    import sys
    odds_path = _get_odds_modules_path()
    if odds_path not in sys.path:
        sys.path.insert(0, odds_path)
    from espn_odds import fetch_odds

    games = fetch_odds(sport)
    return _format_espn_games(games, sport)


@router.get("/espn/nfl")
async def get_espn_nfl():
    """Get NFL spreads and totals from ESPN (DraftKings source).

    Free API, no key required. Returns current week's games with
    point spreads, over/unders, and implied favorites.
    """
    return await handle_edge_request("espn-nfl", _get_sport_odds("nfl"))


@router.get("/espn/nba")
async def get_espn_nba():
    """Get NBA spreads and totals from ESPN (DraftKings source).

    Free API, no key required. Returns today's games with
    point spreads, over/unders, and implied favorites.
    """
    return await handle_edge_request("espn-nba", _get_sport_odds("nba"))


@router.get("/espn/nhl")
async def get_espn_nhl():
    """Get NHL spreads and totals from ESPN (DraftKings source).

    Free API, no key required. Returns today's games with
    puck lines, over/unders, and implied favorites.
    """
    return await handle_edge_request("espn-nhl", _get_sport_odds("nhl"))


@router.get("/espn/mlb")
async def get_espn_mlb():
    """Get MLB spreads and totals from ESPN (DraftKings source).

    Free API, no key required. Returns today's games with
    run lines, over/unders, and implied favorites.
    """
    return await handle_edge_request("espn-mlb", _get_sport_odds("mlb"))


@router.get("/espn/ncaaf")
async def get_espn_ncaaf():
    """Get College Football spreads and totals from ESPN (DraftKings source).

    Free API, no key required. Returns this week's games with
    point spreads, over/unders, and implied favorites.
    """
    return await handle_edge_request("espn-ncaaf", _get_sport_odds("ncaaf"))


@router.get("/espn/ncaab")
async def get_espn_ncaab():
    """Get College Basketball spreads and totals from ESPN (DraftKings source).

    Free API, no key required. Returns today's games with
    point spreads, over/unders, and implied favorites.
    """
    return await handle_edge_request("espn-ncaab", _get_sport_odds("ncaab"))


# ============================================================================
# Betfair Edge Detection
# ============================================================================

@router.get("/betfair/edge")
async def get_betfair_edge():
    """Get edges between Betfair Exchange and Polymarket.

    Uses The Odds API to fetch Betfair Exchange odds and compares with Polymarket.
    Covers: NBA, NHL, World Cup, EPL, Politics
    """
    async def _get_betfair():
        import sys
        odds_path = _get_odds_modules_path()
        if odds_path not in sys.path:
            sys.path.insert(0, odds_path)
        from betfair_edge import get_betfair_edge_summary
        return await get_betfair_edge_summary()

    return await handle_edge_request("betfair", _get_betfair())


# ============================================================================
# Kalshi Edge Detection
# ============================================================================

@router.get("/kalshi/markets")
async def get_kalshi_markets():
    """Get Kalshi market summary and compare with Polymarket.

    Returns overlapping markets between Kalshi and Polymarket for arbitrage detection.
    """
    async def _get_kalshi():
        import sys
        odds_path = _get_odds_modules_path()
        if odds_path not in sys.path:
            sys.path.insert(0, odds_path)
        from kalshi_edge import get_kalshi_polymarket_comparison
        return await get_kalshi_polymarket_comparison()

    return await handle_edge_request("kalshi", _get_kalshi())


# ============================================================================
# Manifold Markets Edge Detection
# ============================================================================

@router.get("/manifold/edge")
async def get_manifold_edge(min_edge: float = Query(default=5.0, ge=0, le=100)):
    """Get Manifold vs Polymarket edges."""
    async def _get_manifold():
        import sys
        odds_path = _get_odds_modules_path()
        if odds_path not in sys.path:
            sys.path.insert(0, odds_path)
        from manifold import get_manifold_edges
        return await get_manifold_edges(min_edge)

    return await handle_edge_request("manifold", _get_manifold())


@router.get("/manifold/markets")
async def get_manifold_markets():
    """Get Manifold market summary."""
    try:
        import sys
        odds_path = _get_odds_modules_path()
        if odds_path not in sys.path:
            sys.path.insert(0, odds_path)
        from manifold import get_manifold_summary
        return get_manifold_summary()
    except ImportError as e:
        logger.exception("Manifold module import failed")
        raise HTTPException(status_code=503, detail="Manifold service unavailable")
    except Exception as e:
        logger.exception("Manifold markets error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================================
# PredictIt Edge Detection
# ============================================================================

@router.get("/predictit/edge")
async def get_predictit_edge(min_edge: float = Query(default=5.0, ge=0, le=100)):
    """Get PredictIt vs Polymarket edges."""
    async def _get_predictit():
        import sys
        odds_path = _get_odds_modules_path()
        if odds_path not in sys.path:
            sys.path.insert(0, odds_path)
        from predictit import get_predictit_edges
        return await get_predictit_edges(min_edge)

    return await handle_edge_request("predictit", _get_predictit())


@router.get("/predictit/markets")
async def get_predictit_markets():
    """Get PredictIt market summary."""
    try:
        import sys
        odds_path = _get_odds_modules_path()
        if odds_path not in sys.path:
            sys.path.insert(0, odds_path)
        from predictit import get_predictit_summary
        return get_predictit_summary()
    except ImportError as e:
        logger.exception("PredictIt module import failed")
        raise HTTPException(status_code=503, detail="PredictIt service unavailable")
    except Exception as e:
        logger.exception("PredictIt markets error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================================
# PolyRouter - Unified API for 7 Platforms
# ============================================================================

@router.get("/polyrouter/markets")
async def get_polyrouter_markets(
    platform: str = Query(default=None, description="Filter by platform: polymarket, kalshi, manifold, limitless, prophetx, novig, sxbet"),
    query: str = Query(default=None, description="Search query"),
    limit: int = Query(default=50, ge=1, le=100)
):
    """Get markets from PolyRouter (7 platforms unified)."""
    async def _get_markets():
        import sys
        odds_path = _get_odds_modules_path()
        if odds_path not in sys.path:
            sys.path.insert(0, odds_path)
        from polyrouter import get_markets
        return await get_markets(platform=platform, limit=limit, query=query)

    return await handle_edge_request("polyrouter", _get_markets())


@router.get("/polyrouter/search")
async def search_polyrouter(
    query: str = Query(..., min_length=1),
    limit: int = Query(default=20, ge=1, le=50)
):
    """Search across all 7 platforms."""
    async def _search():
        import sys
        odds_path = _get_odds_modules_path()
        if odds_path not in sys.path:
            sys.path.insert(0, odds_path)
        from polyrouter import search_markets
        return await search_markets(query, limit)

    return await handle_edge_request("polyrouter", _search())


@router.get("/polyrouter/edge")
async def get_polyrouter_edge(min_edge: float = Query(default=3.0, ge=0, le=100)):
    """Find cross-platform arbitrage via PolyRouter."""
    async def _get_edges():
        import sys
        odds_path = _get_odds_modules_path()
        if odds_path not in sys.path:
            sys.path.insert(0, odds_path)
        from polyrouter import find_cross_platform_edges
        edges = await find_cross_platform_edges(min_edge)
        return {"edges": edges, "count": len(edges), "min_edge_pct": min_edge}

    return await handle_edge_request("polyrouter", _get_edges())


@router.get("/polyrouter/sports/{league}")
async def get_polyrouter_sports(
    league: str,
    limit: int = Query(default=20, ge=1, le=100)
):
    """Get sports games/odds from PolyRouter (nfl, nba, mlb, nhl, epl, etc.)."""
    async def _get_games():
        import sys
        odds_path = _get_odds_modules_path()
        if odds_path not in sys.path:
            sys.path.insert(0, odds_path)
        from polyrouter import list_games
        return await list_games(league, limit)

    return await handle_edge_request("polyrouter", _get_games())


@router.get("/polyrouter/futures/{league}")
async def get_polyrouter_futures(league: str):
    """Get championship futures (Super Bowl, World Series, etc.)."""
    async def _get_futures():
        import sys
        odds_path = _get_odds_modules_path()
        if odds_path not in sys.path:
            sys.path.insert(0, odds_path)
        from polyrouter import list_futures
        return await list_futures(league)

    return await handle_edge_request("polyrouter", _get_futures())


@router.get("/polyrouter/platforms")
async def get_polyrouter_platforms():
    """List all 7 supported platforms."""
    try:
        import sys
        odds_path = _get_odds_modules_path()
        if odds_path not in sys.path:
            sys.path.insert(0, odds_path)
        from polyrouter import list_platforms
        return {"platforms": list_platforms()}
    except ImportError as e:
        logger.exception("PolyRouter module import failed")
        raise HTTPException(status_code=503, detail="PolyRouter service unavailable")
    except Exception as e:
        logger.exception("PolyRouter platforms error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================================
# Metaculus API endpoints
# ============================================================================

@router.get("/metaculus/questions")
async def get_metaculus_questions(
    limit: int = Query(default=50, ge=1, le=200),
    min_forecasters: int = Query(default=30, ge=1)
):
    """Fetch Metaculus forecasts - free crowd predictions for politics, economics, etc."""
    try:
        import sys
        odds_path = _get_odds_modules_path()
        if odds_path not in sys.path:
            sys.path.insert(0, odds_path)
        from metaculus import fetch_questions
        questions = fetch_questions(limit=limit)
        # Filter by minimum forecasters
        filtered = [q for q in questions if q.get("forecasters", 0) >= min_forecasters]
        return {"questions": filtered, "count": len(filtered), "total_fetched": len(questions)}
    except ImportError as e:
        logger.exception("Metaculus module import failed")
        raise HTTPException(status_code=503, detail="Metaculus service unavailable")
    except Exception as e:
        logger.exception("Metaculus questions error")
        raise HTTPException(status_code=500, detail=f"Metaculus error: {str(e)}")


@router.get("/metaculus/edge")
async def get_metaculus_edge(
    min_edge: float = Query(default=0.1, ge=0.01, le=1.0)
):
    """Find edge between Metaculus forecasts and Polymarket prices."""
    try:
        import sys
        odds_path = _get_odds_modules_path()
        if odds_path not in sys.path:
            sys.path.insert(0, odds_path)
        from metaculus import find_edges
        edges = find_edges(min_edge_pct=min_edge * 100)
        return {"edges": edges, "count": len(edges), "min_edge_pct": min_edge * 100}
    except ImportError as e:
        logger.exception("Metaculus module import failed")
        raise HTTPException(status_code=503, detail="Metaculus service unavailable")
    except Exception as e:
        logger.exception("Metaculus edge error")
        raise HTTPException(status_code=500, detail=f"Metaculus error: {str(e)}")


# ============================================================================
# Polymarket direct endpoints (supplement to PolyRouter)
# ============================================================================

@router.get("/polymarket/events")
async def get_polymarket_events(
    limit: int = Query(default=100, ge=1, le=500)
):
    """Fetch active Polymarket events directly from Gamma API."""
    try:
        url = f"{GAMMA_API}/events?active=true&closed=false&limit={limit}"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            events = json.loads(resp.read().decode())
        
        # Parse and normalize events
        normalized = []
        for event in events:
            markets = event.get("markets", [])
            for market in markets:
                try:
                    prices = json.loads(market.get("outcomePrices", "[]"))
                    yes_price = float(prices[0]) if prices else None
                except:
                    yes_price = None
                
                normalized.append({
                    "id": market.get("conditionId"),
                    "slug": event.get("slug", ""),
                    "title": market.get("question", event.get("title", "")),
                    "yes_price": yes_price,
                    "volume": market.get("volumeNum", 0),
                    "liquidity": market.get("liquidityNum", 0),
                })
        
        return {"events": normalized, "count": len(normalized)}
    except Exception as e:
        logger.exception("Polymarket events error")
        raise HTTPException(status_code=500, detail=f"Polymarket error: {str(e)}")

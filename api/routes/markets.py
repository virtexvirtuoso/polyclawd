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
# Vegas Odds API Rate Limiting
# ============================================================================

@router.get("/vegas/quota")
async def get_odds_api_quota():
    """Check The Odds API usage and remaining quota.
    
    Free tier: 500 calls/month
    Tracks usage to prevent exhaustion.
    """
    try:
        from odds.rate_limiter import get_usage, get_scan_schedule, can_make_call
        
        usage = get_usage()
        schedule = get_scan_schedule()
        can_normal, reason_normal = can_make_call("normal")
        can_critical, reason_critical = can_make_call("critical")
        
        return {
            **usage,
            "schedule": schedule,
            "can_call_normal": can_normal,
            "can_call_critical": can_critical,
            "reason": reason_normal if not can_normal else None
        }
    except Exception as e:
        return {"error": str(e), "calls_remaining": "unknown"}


@router.post("/vegas/quota/reset")
async def reset_odds_api_quota():
    """Reset quota tracking (use after switching API keys)."""
    try:
        from odds.rate_limiter import RATE_FILE
        import os
        if RATE_FILE.exists():
            os.remove(RATE_FILE)
        return {"status": "reset", "message": "Quota tracking reset. Will sync with API on next call."}
    except Exception as e:
        return {"error": str(e)}


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
async def get_vegas_odds(
    sport: str = Query(default="americanfootball_nfl"),
    priority: str = Query(default="normal", description="Priority: critical, high, normal, low")
):
    """Get current Vegas odds from The Odds API.

    Args:
        sport: Sport key (americanfootball_nfl, basketball_nba, etc.)
        priority: Call priority for rate limiting
    
    Rate limited: 500 calls/month on free tier.
    """
    # Check rate limit first
    try:
        from odds.rate_limiter import can_make_call, record_call, update_from_headers
        can_call, reason = can_make_call(priority)
        if not can_call:
            return {"error": f"Rate limited: {reason}", "rate_limited": True}
    except ImportError:
        pass  # Rate limiter not available, continue anyway
    
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

            # Record the call and update quota from headers
            try:
                from odds.rate_limiter import record_call, update_from_headers
                record_call(1, f"vegas/odds/{sport}")
                update_from_headers(dict(response.headers))
            except:
                pass
            
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


# ----------------------------------------------------------------------------
# NFL Futures Endpoints
# ----------------------------------------------------------------------------

@router.get("/vegas/nfl")
async def get_nfl_futures():
    """Get NFL futures odds from VegasInsider.
    
    Returns Super Bowl winner odds, AFC/NFC conference winner odds.
    Data is cached for 12 hours.
    """
    async def _get_nfl():
        import sys
        odds_path = _get_odds_modules_path()
        if odds_path not in sys.path:
            sys.path.insert(0, odds_path)
        from vegas_scraper import get_nfl_odds_with_fallback
        
        data = await get_nfl_odds_with_fallback()
        
        return {
            "source": "VegasInsider",
            "timestamp": datetime.now().isoformat(),
            "markets": {
                "super_bowl": {
                    "name": "Super Bowl Winner",
                    "teams": [
                        {
                            "team": o.team,
                            "american_odds": o.american_odds,
                            "implied_prob": round(o.implied_prob, 4),
                        }
                        for o in data.get("super_bowl", [])
                    ]
                },
                "afc_winner": {
                    "name": "AFC Conference Winner",
                    "teams": [
                        {
                            "team": o.team,
                            "american_odds": o.american_odds,
                            "implied_prob": round(o.implied_prob, 4),
                        }
                        for o in data.get("afc_winner", [])
                    ]
                },
                "nfc_winner": {
                    "name": "NFC Conference Winner",
                    "teams": [
                        {
                            "team": o.team,
                            "american_odds": o.american_odds,
                            "implied_prob": round(o.implied_prob, 4),
                        }
                        for o in data.get("nfc_winner", [])
                    ]
                },
            },
            "total_teams": sum(len(v) for v in data.values()),
        }
    
    return await handle_edge_request("nfl", _get_nfl())


@router.get("/vegas/nfl/superbowl")
async def get_superbowl_odds():
    """Get Super Bowl winner odds specifically.
    
    Focuses on Super Bowl futures with Polymarket comparison ready.
    """
    async def _get_sb():
        import sys
        odds_path = _get_odds_modules_path()
        if odds_path not in sys.path:
            sys.path.insert(0, odds_path)
        from vegas_scraper import get_nfl_odds_with_fallback
        
        data = await get_nfl_odds_with_fallback()
        sb_odds = data.get("super_bowl", [])
        
        return {
            "market": "Super Bowl LX Winner",
            "source": "VegasInsider",
            "timestamp": datetime.now().isoformat(),
            "favorites": [
                {
                    "team": o.team,
                    "american_odds": o.american_odds,
                    "implied_prob_pct": round(o.implied_prob * 100, 1),
                }
                for o in sb_odds[:10]  # Top 10
            ],
            "total_teams": len(sb_odds),
        }
    
    return await handle_edge_request("superbowl", _get_sb())


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


# ----------------------------------------------------------------------------
# ESPN Moneyline Endpoints
# ----------------------------------------------------------------------------

@router.get("/espn/moneyline/{sport}")
async def get_espn_moneyline(sport: str):
    """Get moneyline odds for a specific sport from ESPN (DraftKings source).
    
    Returns true probabilities after removing vig, line movement from open.
    Free API, no key required.
    
    Args:
        sport: One of nfl, nba, nhl, mlb, ncaaf, ncaab
    """
    async def _get_ml():
        import sys
        odds_path = _get_odds_modules_path()
        if odds_path not in sys.path:
            sys.path.insert(0, odds_path)
        from espn_odds import get_moneyline
        
        games = get_moneyline(sport)
        return {
            "sport": sport.upper(),
            "source": "ESPN (DraftKings)",
            "timestamp": datetime.now().isoformat(),
            "total_games": len(games),
            "games": games,
        }
    
    return await handle_edge_request(f"espn-ml-{sport}", _get_ml())


@router.get("/espn/moneylines")
async def get_all_espn_moneylines():
    """Get moneyline odds for all sports from ESPN (DraftKings source).
    
    Aggregates moneyline data across NFL, NBA, NHL, MLB, NCAAF, NCAAB.
    Free API, no key required.
    """
    async def _get_all_ml():
        import sys
        odds_path = _get_odds_modules_path()
        if odds_path not in sys.path:
            sys.path.insert(0, odds_path)
        from espn_odds import get_all_moneylines
        
        all_ml = get_all_moneylines()
        total = sum(len(v) for v in all_ml.values())
        
        return {
            "source": "ESPN (DraftKings)",
            "timestamp": datetime.now().isoformat(),
            "total_games": total,
            "sports": {
                sport.upper(): {
                    "games": len(games),
                    "matchups": games[:10]  # Limit to 10 per sport
                }
                for sport, games in all_ml.items()
            },
        }
    
    return await handle_edge_request("espn-ml-all", _get_all_ml())


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
# High-Frequency Polymarket Scanner (Phase 1)
# ============================================================================

@router.get("/hf/scan")
async def hf_full_scan(
    threshold: float = Query(default=0.99, ge=0.90, le=1.0, description="Neg vig threshold")
):
    """Full HF scan: discover short-duration crypto markets + neg vig detection.
    
    Finds 5-min, 15-min, and hourly BTC/ETH/SOL prediction markets,
    then checks CLOB orderbooks for negative vig (Yes+No < threshold).
    
    Based on the $134→$200K Polymarket bot strategy.
    """
    async def _scan():
        from odds.hf_scanner import full_hf_scan
        return full_hf_scan(neg_vig_threshold=threshold)
    
    return await handle_edge_request("hf-scanner", _scan())


@router.get("/hf/markets")
async def hf_discover_markets():
    """Discover active short-duration crypto prediction markets.
    
    Searches for 5-min, 15-min BTC/ETH/SOL up/down markets on Polymarket.
    """
    async def _discover():
        from odds.hf_scanner import discover_hf_markets
        from dataclasses import asdict
        markets = discover_hf_markets()
        return {
            "markets": [asdict(m) for m in markets],
            "count": len(markets),
            "timestamp": datetime.now().isoformat(),
        }
    
    return await handle_edge_request("hf-discovery", _discover())


@router.get("/hf/negvig")
async def hf_neg_vig_scan(
    threshold: float = Query(default=0.99, ge=0.90, le=1.0)
):
    """Scan for negative vig opportunities on short-duration markets.
    
    Checks CLOB orderbooks where Yes_ask + No_ask < threshold.
    Buying both sides = guaranteed profit on resolution.
    """
    async def _negvig():
        from odds.hf_scanner import discover_hf_markets, scan_neg_vig
        from dataclasses import asdict
        markets = discover_hf_markets()
        opps = scan_neg_vig(markets, threshold=threshold)
        return {
            "opportunities": [asdict(o) for o in opps],
            "count": len(opps),
            "markets_scanned": len(markets),
            "threshold": threshold,
            "timestamp": datetime.now().isoformat(),
        }
    
    return await handle_edge_request("hf-negvig", _negvig())


@router.get("/hf/signal/{asset}")
async def hf_directional_signal(asset: str):
    """Get Virtuoso-powered directional signal for a crypto asset.
    
    Combines fusion signal, market regime, kill switch, and manipulation
    alerts to produce a trade recommendation for Polymarket 5/15-min markets.
    
    Args:
        asset: BTC or ETH
    """
    async def _signal():
        from services.virtuoso_bridge import get_directional_signal
        from dataclasses import asdict
        sig = get_directional_signal(asset)
        return asdict(sig)
    
    return await handle_edge_request("hf-signal", _signal())


@router.get("/hf/signals")
async def hf_all_signals():
    """Get directional signals for all supported assets (BTC, ETH)."""
    async def _signals():
        from services.virtuoso_bridge import scan_all_assets
        return scan_all_assets()
    
    return await handle_edge_request("hf-signals", _signals())


@router.get("/hf/opportunities")
async def hf_opportunities():
    """Match Virtuoso directional signals to available Polymarket HF markets.
    
    Full pipeline: signal generation → market discovery → matching → ranking.
    Returns tradeable opportunities sorted by estimated edge.
    """
    async def _opps():
        from services.virtuoso_bridge import match_signals_to_markets
        return match_signals_to_markets()
    
    return await handle_edge_request("hf-opportunities", _opps())


@router.get("/hf/latency")
async def hf_latency_state():
    """Get real-time latency engine state (Binance vs Chainlink oracle).
    
    Shows current price divergence, active latency signals, and engine stats.
    Proxies to the HF engine service on port 8422.
    """
    async def _latency():
        import urllib.request
        try:
            req = urllib.request.Request("http://127.0.0.1:8422/state", 
                                        headers={"User-Agent": "Polyclawd-API"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                return json.loads(resp.read().decode())
        except Exception:
            return {"error": "HF engine not running", "hint": "Start with: sudo systemctl start polyclawd-hf"}
    
    return await handle_edge_request("hf-latency", _latency())


@router.get("/hf/latency/events")
async def hf_latency_events():
    """Get recent latency divergence events detected by the HF engine."""
    async def _events():
        import urllib.request
        try:
            req = urllib.request.Request("http://127.0.0.1:8422/events",
                                        headers={"User-Agent": "Polyclawd-API"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                return json.loads(resp.read().decode())
        except Exception:
            return {"error": "HF engine not running"}
    
    return await handle_edge_request("hf-latency-events", _events())


@router.get("/hf/latency/signals")
async def hf_latency_signals():
    """Get current actionable latency signals (Binance ahead of oracle)."""
    async def _signals():
        import urllib.request
        try:
            req = urllib.request.Request("http://127.0.0.1:8422/signals",
                                        headers={"User-Agent": "Polyclawd-API"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                return json.loads(resp.read().decode())
        except Exception:
            return {"error": "HF engine not running"}
    
    return await handle_edge_request("hf-latency-signals", _signals())


@router.post("/hf/trade")
async def hf_process_signals():
    """Process HF signals and open paper positions.
    
    Reads from: trigger engine, Virtuoso bridge, neg vig scanner.
    Opens paper positions via the existing portfolio system.
    Positions appear on portfolio.html tagged as hf_crypto.
    """
    async def _trade():
        from services.hf_paper_trader import process_hf_signals
        return process_hf_signals()
    
    return await handle_edge_request("hf-trade", _trade())


@router.get("/hf/paper-performance")
async def hf_paper_performance():
    """Get HF paper trading performance stats."""
    async def _perf():
        from services.hf_paper_trader import get_hf_performance
        return get_hf_performance()
    
    return await handle_edge_request("hf-paper-perf", _perf())


@router.post("/hf/resolve")
async def hf_resolve_positions():
    """Auto-resolve HF paper positions against market outcomes."""
    async def _resolve():
        from services.hf_paper_trader import resolve_hf_positions
        return resolve_hf_positions()
    
    return await handle_edge_request("hf-resolve", _resolve())


@router.post("/hf/daily-summary")
async def hf_daily_summary():
    """Send daily HF performance summary to Telegram."""
    async def _summary():
        from services.hf_paper_trader import send_daily_summary
        return send_daily_summary()
    
    return await handle_edge_request("hf-daily-summary", _summary())


@router.get("/hf/collect")
async def hf_run_collection():
    """Run one data collection cycle (resolutions + divergence + signals).
    
    Call periodically to build backtesting dataset.
    """
    async def _collect():
        from services.hf_collector import run_collection_cycle
        return run_collection_cycle()
    
    return await handle_edge_request("hf-collect", _collect())


@router.get("/hf/collection-stats")
async def hf_collection_stats():
    """Get stats on collected HF data for backtesting."""
    async def _stats():
        from services.hf_collector import get_collection_stats
        return get_collection_stats()
    
    return await handle_edge_request("hf-collection-stats", _stats())


@router.get("/hf/backtest")
async def hf_backtest(
    balance: float = Query(default=134.0, ge=1.0, description="Starting balance"),
    simulations: int = Query(default=500, ge=50, le=5000),
    trades: int = Query(default=200, ge=10, le=2000),
):
    """Run Monte Carlo backtest for all HF strategies.
    
    Simulates latency_arb, neg_vig, directional, and combined strategies.
    Uses collected data when available, falls back to parameterized estimates.
    """
    async def _backtest():
        from services.hf_backtest import full_backtest_report
        return full_backtest_report(
            starting_balance=balance,
            num_simulations=simulations,
            trades_per_sim=trades,
        )
    
    return await handle_edge_request("hf-backtest", _backtest())


@router.get("/hf/backtest/{strategy}")
async def hf_backtest_strategy(
    strategy: str,
    balance: float = Query(default=134.0, ge=1.0),
    simulations: int = Query(default=1000, ge=50, le=5000),
    trades: int = Query(default=200, ge=10, le=2000),
    kelly: float = Query(default=0.10, ge=0.01, le=0.5),
):
    """Run Monte Carlo backtest for a specific strategy.
    
    Strategies: latency_arb, neg_vig, directional, combined
    """
    async def _bt():
        from services.hf_backtest import run_monte_carlo
        from dataclasses import asdict
        result = run_monte_carlo(
            starting_balance=balance,
            num_simulations=simulations,
            trades_per_sim=trades,
            strategy=strategy,
            kelly_fraction=kelly,
        )
        return asdict(result)
    
    return await handle_edge_request(f"hf-backtest-{strategy}", _bt())


@router.get("/hf/risk")
async def hf_risk_gate(
    max_drawdown: float = Query(default=10.0, ge=1.0, le=50.0),
    window_min: int = Query(default=60, ge=5, le=1440),
):
    """Run all risk checks — determines if HF trading is allowed.
    
    Hard blocks (any one = no trading):
    - Kill switch triggered
    - Manipulation detected  
    - Drawdown exceeded
    - API unhealthy
    
    Soft warnings (logged, don't block):
    - Low volatility regime
    """
    async def _risk():
        from services.hf_risk_gate import evaluate_risk_gate
        from dataclasses import asdict
        result = evaluate_risk_gate(
            max_drawdown_pct=max_drawdown,
            drawdown_window_min=window_min,
        )
        return {
            "trading_allowed": result.trading_allowed,
            "summary": result.summary,
            "hard_blocks": result.hard_blocks,
            "soft_warnings": result.soft_warnings,
            "checks": [asdict(c) for c in result.checks],
            "timestamp": result.timestamp,
        }
    
    return await handle_edge_request("hf-risk", _risk())


# ============================================================================
# Kalshi Edge Detection
# ============================================================================

@router.get("/kalshi/markets")
async def get_kalshi_markets():
    """Get Kalshi market summary and compare with Polymarket.

    Returns overlapping markets between Kalshi and Polymarket for arbitrage detection.
    Uses comprehensive fetching (all events, markets, and series with pagination).
    """
    async def _get_kalshi():
        import sys
        odds_path = _get_odds_modules_path()
        if odds_path not in sys.path:
            sys.path.insert(0, odds_path)
        from kalshi_edge import get_kalshi_polymarket_comparison
        return await get_kalshi_polymarket_comparison()

    return await handle_edge_request("kalshi", _get_kalshi())


@router.get("/kalshi/entertainment")
async def get_kalshi_entertainment():
    """Get entertainment and sports prop markets from Kalshi.

    Discovers markets for:
    - Super Bowl halftime (KXFIRSTSUPERBOWLSONG, KXSBSETLISTS, KXHALFTIMESHOW)
    - Grammy/Oscar/Emmy awards
    - Celebrity props
    - NFL/NBA/MLB/NHL sports props
    
    Returns structured data with odds for betting opportunities.
    """
    async def _get_entertainment():
        import sys
        odds_path = _get_odds_modules_path()
        if odds_path not in sys.path:
            sys.path.insert(0, odds_path)
        from kalshi_edge import get_kalshi_entertainment_props
        return await get_kalshi_entertainment_props()

    return await handle_edge_request("kalshi-entertainment", _get_entertainment())


@router.get("/kalshi/all")
async def get_all_kalshi_markets():
    """Get ALL Kalshi markets with comprehensive pagination.
    
    Returns full market data including:
    - All open markets (paginated through entire catalog)
    - All series
    - Category breakdown
    - Top markets by volume
    """
    async def _get_all():
        import sys
        odds_path = _get_odds_modules_path()
        if odds_path not in sys.path:
            sys.path.insert(0, odds_path)
        from kalshi_edge import get_kalshi_all_markets
        return await get_kalshi_all_markets()

    return await handle_edge_request("kalshi-all", _get_all())


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


# ============================================================================
# NEW: Polymarket CLOB (orderbook depth)
# ============================================================================

@router.get("/polymarket/orderbook/{slug}")
async def get_polymarket_orderbook(
    slug: str,
    outcome: str = Query(default="Yes")
):
    """Get Polymarket orderbook for a market."""
    try:
        from odds.polymarket_clob import get_orderbook_for_market
        orderbook = get_orderbook_for_market(slug, outcome)
        if orderbook:
            return {
                "market_slug": slug,
                "outcome": outcome,
                "bids": [{"price": b.price, "size": b.size} for b in orderbook.bids[:10]],
                "asks": [{"price": a.price, "size": a.size} for a in orderbook.asks[:10]],
                "spread": orderbook.spread,
                "mid_price": orderbook.mid_price
            }
        return {"error": "Orderbook not found"}
    except Exception as e:
        logger.exception("Orderbook error")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/polymarket/microstructure/{slug}")
async def get_polymarket_microstructure(slug: str):
    """Get market microstructure analysis."""
    try:
        from odds.polymarket_clob import get_market_microstructure
        return get_market_microstructure(slug)
    except Exception as e:
        logger.exception("Microstructure error")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# NEW: Manifold Markets
# ============================================================================

@router.get("/manifold/bets")
async def get_manifold_bets(limit: int = Query(default=50, ge=1, le=200)):
    """Get recent bets on Manifold."""
    try:
        from odds.manifold import get_bets
        bets = get_bets(limit=limit)
        return {"bets": bets, "count": len(bets)}
    except Exception as e:
        logger.exception("Manifold bets error")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/manifold/top-traders")
async def get_manifold_top_traders():
    """Get top Manifold traders."""
    try:
        from odds.manifold import get_top_traders
        return get_top_traders()
    except Exception as e:
        logger.exception("Manifold top traders error")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# NEW: Metaculus divergence
# ============================================================================

@router.get("/metaculus/divergence")
async def get_metaculus_divergence():
    """Get Metaculus vs community prediction divergence."""
    try:
        from odds.metaculus import get_divergence_signals
        return get_divergence_signals()
    except Exception as e:
        logger.exception("Metaculus divergence error")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# NEW: ESPN injuries and standings
# ============================================================================

@router.get("/espn/injuries/{sport}")
async def get_espn_injuries(sport: str):
    """Get injury report for a sport."""
    try:
        from odds.espn_odds import get_injuries
        return get_injuries(sport)
    except Exception as e:
        logger.exception("ESPN injuries error")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/espn/standings/{sport}")
async def get_espn_standings(sport: str):
    """Get standings for a sport."""
    try:
        from odds.espn_odds import get_standings
        return get_standings(sport)
    except Exception as e:
        logger.exception("ESPN standings error")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# NEW: Vegas futures (NBA, MLB, NHL)
# ============================================================================

@router.get("/vegas/nba")
async def get_vegas_nba():
    """Get NBA championship futures."""
    try:
        from odds.vegas_scraper import scrape_vegasinsider_nba
        return scrape_vegasinsider_nba()
    except Exception as e:
        logger.exception("Vegas NBA error")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/vegas/mlb")
async def get_vegas_mlb():
    """Get MLB World Series futures."""
    try:
        from odds.vegas_scraper import scrape_vegasinsider_mlb
        return scrape_vegasinsider_mlb()
    except Exception as e:
        logger.exception("Vegas MLB error")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/vegas/nhl")
async def get_vegas_nhl():
    """Get NHL Stanley Cup futures."""
    try:
        from odds.vegas_scraper import scrape_vegasinsider_nhl
        return scrape_vegasinsider_nhl()
    except Exception as e:
        logger.exception("Vegas NHL error")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# NEW: PolyRouter arbitrage and props
# ============================================================================

@router.get("/polyrouter/arbitrage")
async def get_polyrouter_arbitrage():
    """Find cross-platform arbitrage opportunities."""
    try:
        from odds.polyrouter import find_arbitrage_opportunities
        import asyncio
        return asyncio.get_event_loop().run_until_complete(find_arbitrage_opportunities())
    except Exception as e:
        logger.exception("PolyRouter arbitrage error")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/polyrouter/props/{league}")
async def get_polyrouter_props(league: str):
    """Get player props from PolyRouter."""
    try:
        from odds.polyrouter import get_player_props
        import asyncio
        return asyncio.get_event_loop().run_until_complete(get_player_props(league))
    except Exception as e:
        logger.exception("PolyRouter props error")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# NEW: Kalshi entertainment props
# ============================================================================

@router.get("/kalshi/entertainment")
async def get_kalshi_entertainment():
    """Get Kalshi entertainment/sports props (Super Bowl, Grammys, Oscars)."""
    try:
        from odds.kalshi_edge import get_kalshi_entertainment_props
        import asyncio
        return asyncio.get_event_loop().run_until_complete(get_kalshi_entertainment_props())
    except Exception as e:
        logger.exception("Kalshi entertainment error")
        raise HTTPException(status_code=500, detail=str(e))

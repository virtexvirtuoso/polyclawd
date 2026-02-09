"""
PolyRouter API Integration

Unified API for 7 prediction market platforms:
- Polymarket, Kalshi, Manifold, Limitless, ProphetX, Novig, SX.bet

Get API key: https://polyrouter.io (join Discord)
Docs: https://docs.polyrouter.io
"""

import os
import httpx
from typing import Optional
from functools import lru_cache

POLYROUTER_API_KEY = os.environ.get("POLYROUTER_API_KEY", "")
BASE_URL = "https://api-v2.polyrouter.io"

def _headers():
    return {"X-API-Key": POLYROUTER_API_KEY}

async def get_markets(
    platform: Optional[str] = None,
    status: str = "open",
    limit: int = 50,
    query: Optional[str] = None
) -> dict:
    """
    Fetch markets from all platforms or filter by platform.
    
    Platforms: polymarket, kalshi, manifold, limitless, prophetx, novig, sxbet
    """
    if not POLYROUTER_API_KEY:
        return {"error": "POLYROUTER_API_KEY not set"}
    
    params = {"status": status, "limit": limit}
    if platform:
        params["platform"] = platform
    if query:
        params["query"] = query
    
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{BASE_URL}/markets", headers=_headers(), params=params)
        return resp.json()

async def search_markets(query: str, limit: int = 20) -> dict:
    """Search markets across all platforms."""
    if not POLYROUTER_API_KEY:
        return {"error": "POLYROUTER_API_KEY not set"}
    
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{BASE_URL}/search",
            headers=_headers(),
            params={"query": query, "limit": limit}
        )
        return resp.json()

async def get_market(market_id: str) -> dict:
    """Get detailed market info."""
    if not POLYROUTER_API_KEY:
        return {"error": "POLYROUTER_API_KEY not set"}
    
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{BASE_URL}/markets/{market_id}", headers=_headers())
        return resp.json()

async def get_orderbook(market_id: str) -> dict:
    """Get real-time orderbook for a market."""
    if not POLYROUTER_API_KEY:
        return {"error": "POLYROUTER_API_KEY not set"}
    
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{BASE_URL}/orderbook/{market_id}", headers=_headers())
        return resp.json()

async def get_price_history(market_id: str, interval: str = "1h", limit: int = 100) -> dict:
    """Get OHLC price history."""
    if not POLYROUTER_API_KEY:
        return {"error": "POLYROUTER_API_KEY not set"}
    
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{BASE_URL}/history/{market_id}",
            headers=_headers(),
            params={"interval": interval, "limit": limit}
        )
        return resp.json()

# Sports endpoints
async def list_games(league: str, limit: int = 20) -> dict:
    """
    List sports games.
    
    Leagues: nfl, nba, mlb, nhl, ncaaf, ncaab, mls, epl, etc.
    """
    if not POLYROUTER_API_KEY:
        return {"error": "POLYROUTER_API_KEY not set"}
    
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{BASE_URL}/list-games",
            headers=_headers(),
            params={"league": league, "limit": limit}
        )
        return resp.json()

async def get_game(game_id: str) -> dict:
    """Get odds for a specific game across platforms."""
    if not POLYROUTER_API_KEY:
        return {"error": "POLYROUTER_API_KEY not set"}
    
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{BASE_URL}/games/{game_id}", headers=_headers())
        return resp.json()

async def list_futures(league: str) -> dict:
    """Get championship futures (e.g., Super Bowl winner)."""
    if not POLYROUTER_API_KEY:
        return {"error": "POLYROUTER_API_KEY not set"}
    
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{BASE_URL}/list-futures",
            headers=_headers(),
            params={"league": league}
        )
        return resp.json()

async def list_awards(league: str) -> dict:
    """Get award markets (MVP, DPOY, etc.)."""
    if not POLYROUTER_API_KEY:
        return {"error": "POLYROUTER_API_KEY not set"}
    
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{BASE_URL}/list-awards",
            headers=_headers(),
            params={"league": league}
        )
        return resp.json()

# Cross-platform edge finding
async def find_cross_platform_edges(min_edge_pct: float = 3.0) -> list:
    """
    Find arbitrage opportunities across platforms.
    Returns markets where same outcome has different prices.
    """
    if not POLYROUTER_API_KEY:
        return []
    
    edges = []
    
    # Get markets from each live platform
    platforms = LIVE_PLATFORMS
    all_markets = {}
    
    for platform in platforms:
        try:
            data = await get_markets(platform=platform, limit=100)
            if "markets" in data:
                for m in data["markets"]:
                    title = m.get("title", "").lower()
                    if title not in all_markets:
                        all_markets[title] = []
                    all_markets[title].append({
                        "platform": platform,
                        "id": m.get("id"),
                        "title": m.get("title"),
                        "yes_price": m.get("current_prices", {}).get("yes", {}).get("price", 0),
                        "no_price": m.get("current_prices", {}).get("no", {}).get("price", 0),
                    })
        except Exception as e:
            continue
    
    # Find overlapping markets with price differences
    for title, markets in all_markets.items():
        if len(markets) < 2:
            continue
        
        # Compare prices across platforms
        for i, m1 in enumerate(markets):
            for m2 in markets[i+1:]:
                if m1["yes_price"] and m2["yes_price"]:
                    edge = abs(m1["yes_price"] - m2["yes_price"]) * 100
                    if edge >= min_edge_pct:
                        edges.append({
                            "title": m1["title"],
                            "platform_1": m1["platform"],
                            "price_1": m1["yes_price"],
                            "platform_2": m2["platform"],
                            "price_2": m2["yes_price"],
                            "edge_pct": round(edge, 1),
                            "direction": "buy" if m1["yes_price"] < m2["yes_price"] else "sell",
                            "buy_on": m1["platform"] if m1["yes_price"] < m2["yes_price"] else m2["platform"],
                        })
    
    return sorted(edges, key=lambda x: x["edge_pct"], reverse=True)

# Platforms info (currently live)
PLATFORMS = {
    "polymarket": {"name": "Polymarket", "type": "crypto", "region": "global", "live": True},
    "kalshi": {"name": "Kalshi", "type": "regulated", "region": "US", "live": True},
    "manifold": {"name": "Manifold", "type": "play-money", "region": "global", "live": True},
    "limitless": {"name": "Limitless", "type": "crypto", "region": "global", "live": True},
    # Coming soon per API validation:
    "prophetx": {"name": "ProphetX", "type": "prediction", "region": "global", "live": False},
    "novig": {"name": "Novig", "type": "sports", "region": "US", "live": False},
    "sxbet": {"name": "SX.bet", "type": "crypto-sports", "region": "global", "live": False},
}

LIVE_PLATFORMS = ["polymarket", "kalshi", "manifold", "limitless"]

def list_platforms():
    """List all supported platforms."""
    return PLATFORMS


async def list_props(league: str, limit: int = 20) -> dict:
    """
    List player prop markets for a league.
    
    Args:
        league: nfl, nba, mlb, nhl
        limit: Max props to return
    
    Returns:
        Player prop markets across platforms
    """
    if not POLYROUTER_API_KEY:
        return {"error": "POLYROUTER_API_KEY not set"}
    
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{BASE_URL}/list-props",
            headers=_headers(),
            params={"league": league, "limit": limit}
        )
        return resp.json()


async def get_prop(prop_id: str) -> dict:
    """Get odds for a specific player prop across platforms."""
    if not POLYROUTER_API_KEY:
        return {"error": "POLYROUTER_API_KEY not set"}
    
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{BASE_URL}/props/{prop_id}", headers=_headers())
        return resp.json()


async def find_best_odds(market_id: str) -> dict:
    """
    Find the best odds for a market across all platforms.
    
    Returns:
        Best YES and NO prices with platform info
    """
    if not POLYROUTER_API_KEY:
        return {"error": "POLYROUTER_API_KEY not set"}
    
    market = await get_market(market_id)
    
    if "error" in market:
        return market
    
    # Get prices from all platforms for this market
    # The API normalizes market IDs across platforms
    prices = market.get("current_prices", {})
    
    yes_price = prices.get("yes", {}).get("price", 0)
    no_price = prices.get("no", {}).get("price", 0)
    platform = market.get("platform", "unknown")
    
    return {
        "market_id": market_id,
        "title": market.get("title", ""),
        "best_yes": {
            "price": yes_price,
            "platform": platform,
        },
        "best_no": {
            "price": no_price,
            "platform": platform,
        },
        "spread": round((1 - yes_price - no_price) * 100, 2) if yes_price and no_price else None,
    }


async def find_arbitrage_opportunities(min_edge_pct: float = 2.0) -> list:
    """
    Find true arbitrage opportunities across platforms.
    
    Arbitrage exists when:
    - Platform A has YES at X cents
    - Platform B has NO at Y cents
    - X + Y < 100 (guaranteed profit)
    
    Returns:
        List of arbitrage opportunities with profit %
    """
    if not POLYROUTER_API_KEY:
        return []
    
    opportunities = []
    
    # Get markets from all platforms
    all_markets = {}
    
    for platform in LIVE_PLATFORMS:
        try:
            data = await get_markets(platform=platform, limit=100)
            markets = data.get("markets", []) if isinstance(data, dict) else []
            
            for m in markets:
                # Normalize title for matching
                title = m.get("title", "").lower().strip()
                title_key = "".join(c for c in title if c.isalnum())[:50]
                
                if title_key not in all_markets:
                    all_markets[title_key] = []
                
                prices = m.get("current_prices", {})
                yes_price = prices.get("yes", {}).get("price", 0)
                no_price = prices.get("no", {}).get("price", 0)
                
                if yes_price > 0:
                    all_markets[title_key].append({
                        "platform": platform,
                        "id": m.get("id"),
                        "title": m.get("title"),
                        "yes_price": yes_price,
                        "no_price": no_price,
                    })
        except Exception as e:
            print(f"Error fetching {platform}: {e}")
            continue
    
    # Find arbitrage across platforms
    for title_key, markets in all_markets.items():
        if len(markets) < 2:
            continue
        
        # Check all pairs
        for i, m1 in enumerate(markets):
            for m2 in markets[i+1:]:
                if m1["platform"] == m2["platform"]:
                    continue
                
                # Arbitrage 1: Buy YES on m1, Buy NO on m2
                cost1 = m1["yes_price"] + m2["no_price"]
                if cost1 < 1.0:
                    profit_pct = (1.0 - cost1) * 100
                    if profit_pct >= min_edge_pct:
                        opportunities.append({
                            "title": m1["title"],
                            "strategy": f"BUY YES on {m1['platform']} @ {m1['yes_price']:.2f}, BUY NO on {m2['platform']} @ {m2['no_price']:.2f}",
                            "total_cost": round(cost1, 4),
                            "guaranteed_profit_pct": round(profit_pct, 2),
                            "type": "ARBITRAGE",
                            "platforms": [m1["platform"], m2["platform"]],
                        })
                
                # Arbitrage 2: Buy NO on m1, Buy YES on m2
                cost2 = m1["no_price"] + m2["yes_price"]
                if cost2 < 1.0:
                    profit_pct = (1.0 - cost2) * 100
                    if profit_pct >= min_edge_pct:
                        opportunities.append({
                            "title": m1["title"],
                            "strategy": f"BUY NO on {m1['platform']} @ {m1['no_price']:.2f}, BUY YES on {m2['platform']} @ {m2['yes_price']:.2f}",
                            "total_cost": round(cost2, 4),
                            "guaranteed_profit_pct": round(profit_pct, 2),
                            "type": "ARBITRAGE",
                            "platforms": [m1["platform"], m2["platform"]],
                        })
    
    return sorted(opportunities, key=lambda x: x["guaranteed_profit_pct"], reverse=True)


async def get_sports_summary(leagues: list = None) -> dict:
    """
    Get summary of sports betting markets across platforms.
    
    Args:
        leagues: List of leagues (nfl, nba, etc.) or None for all
    
    Returns:
        Summary with game counts, futures, and props by league
    """
    if not POLYROUTER_API_KEY:
        return {"error": "POLYROUTER_API_KEY not set"}
    
    if leagues is None:
        leagues = ["nfl", "nba", "nhl", "mlb", "ncaaf", "ncaab"]
    
    summary = {"leagues": {}}
    
    for league in leagues:
        try:
            games = await list_games(league, limit=20)
            futures = await list_futures(league)
            awards = await list_awards(league)
            
            summary["leagues"][league] = {
                "games": len(games.get("games", [])) if isinstance(games, dict) else 0,
                "futures": len(futures.get("futures", [])) if isinstance(futures, dict) else 0,
                "awards": len(awards.get("awards", [])) if isinstance(awards, dict) else 0,
            }
        except:
            summary["leagues"][league] = {"error": "Failed to fetch"}
    
    return summary

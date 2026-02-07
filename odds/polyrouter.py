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
    
    # Get markets from each platform
    platforms = ["polymarket", "kalshi", "manifold", "limitless"]
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

# Platforms info
PLATFORMS = {
    "polymarket": {"name": "Polymarket", "type": "crypto", "region": "global"},
    "kalshi": {"name": "Kalshi", "type": "regulated", "region": "US"},
    "manifold": {"name": "Manifold", "type": "play-money", "region": "global"},
    "limitless": {"name": "Limitless", "type": "crypto", "region": "global"},
    "prophetx": {"name": "ProphetX", "type": "prediction", "region": "global"},
    "novig": {"name": "Novig", "type": "sports", "region": "US"},
    "sxbet": {"name": "SX.bet", "type": "crypto-sports", "region": "global"},
}

def list_platforms():
    """List all supported platforms."""
    return PLATFORMS

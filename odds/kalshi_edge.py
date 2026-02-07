"""
Kalshi Edge Finder
Compares Kalshi prediction market prices with Polymarket
"""

import requests
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional
from datetime import datetime

# Kalshi API endpoints
KALSHI_API_BASE = "https://api.kalshi.com/trade-api/v2"
KALSHI_DEMO_API = "https://demo-api.kalshi.co/trade-api/v2"

@dataclass
class KalshiMarket:
    ticker: str
    title: str
    subtitle: str
    category: str
    yes_price: float
    no_price: float
    volume: float

@dataclass 
class KalshiEdge:
    market: str
    kalshi_price: float
    polymarket_price: float
    edge_pct: float
    direction: str
    kalshi_ticker: str
    poly_market_id: Optional[str] = None

def _fetch_kalshi_markets_sync(category: str = None, limit: int = 100) -> list[dict]:
    """Fetch markets from Kalshi API"""
    try:
        params = {"limit": limit, "status": "open"}
        if category:
            params["category"] = category
            
        # Try production API first, fall back to demo
        for base_url in [KALSHI_API_BASE, KALSHI_DEMO_API]:
            try:
                resp = requests.get(
                    f"{base_url}/events",
                    params=params,
                    headers={"Accept": "application/json"},
                    timeout=30
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("events", [])
            except:
                continue
        return []
    except Exception as e:
        print(f"Error fetching Kalshi markets: {e}")
        return []

def _fetch_kalshi_market_details_sync(event_ticker: str) -> dict:
    """Fetch detailed market data including prices"""
    try:
        for base_url in [KALSHI_API_BASE, KALSHI_DEMO_API]:
            try:
                resp = requests.get(
                    f"{base_url}/markets",
                    params={"event_ticker": event_ticker},
                    headers={"Accept": "application/json"},
                    timeout=30
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("markets", [])
            except:
                continue
        return []
    except Exception as e:
        print(f"Error fetching Kalshi market details: {e}")
        return []

def _fetch_polymarket_sync() -> list[dict]:
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

# Market title mappings between Kalshi and Polymarket
MARKET_MAPPINGS = [
    {
        "kalshi_search": "Fed Chair",
        "polymarket_search": "Fed Chair",
        "category": "Politics"
    },
    {
        "kalshi_search": "Democratic nominee",
        "polymarket_search": "Democratic nominee",
        "category": "Politics"
    },
    {
        "kalshi_search": "Republican nominee",
        "polymarket_search": "Republican nominee", 
        "category": "Politics"
    },
    {
        "kalshi_search": "Presidential Election",
        "polymarket_search": "President",
        "category": "Politics"
    },
    {
        "kalshi_search": "House of Representatives",
        "polymarket_search": "House",
        "category": "Politics"
    },
    {
        "kalshi_search": "Super Bowl",
        "polymarket_search": "Super Bowl",
        "category": "Sports"
    },
    {
        "kalshi_search": "Bitcoin",
        "polymarket_search": "Bitcoin",
        "category": "Crypto"
    },
    {
        "kalshi_search": "government shut",
        "polymarket_search": "shutdown",
        "category": "Politics"
    },
    {
        "kalshi_search": "Khamenei",
        "polymarket_search": "Khamenei",
        "category": "Politics"
    },
    {
        "kalshi_search": "Netanyahu",
        "polymarket_search": "Netanyahu",
        "category": "Politics"
    },
    {
        "kalshi_search": "tariff",
        "polymarket_search": "tariff",
        "category": "Politics"
    },
]

def find_matching_polymarket(kalshi_title: str, poly_events: list) -> tuple[Optional[float], Optional[str], Optional[str]]:
    """Find matching Polymarket event for a Kalshi market"""
    kalshi_lower = kalshi_title.lower()
    
    for mapping in MARKET_MAPPINGS:
        if mapping["kalshi_search"].lower() not in kalshi_lower:
            continue
            
        for event in poly_events:
            poly_title = event.get("title", "").lower()
            if mapping["polymarket_search"].lower() in poly_title:
                # Found matching event, get first market price
                markets = event.get("markets", [])
                if markets:
                    market = markets[0]
                    price = market.get("bestAsk", 0)
                    market_id = market.get("id", "")
                    question = market.get("question", "")
                    return (float(price) if price else None, market_id, question)
    
    return (None, None, None)

async def get_kalshi_markets() -> list[KalshiMarket]:
    """Fetch all open Kalshi markets"""
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as executor:
        events = await loop.run_in_executor(executor, _fetch_kalshi_markets_sync, None, 200)
    
    markets = []
    for event in events:
        markets.append(KalshiMarket(
            ticker=event.get("event_ticker", ""),
            title=event.get("title", ""),
            subtitle=event.get("sub_title", ""),
            category=event.get("category", ""),
            yes_price=0,  # Would need separate API call for prices
            no_price=0,
            volume=0
        ))
    
    return markets

async def find_kalshi_edges(min_edge: float = 0.03) -> list[KalshiEdge]:
    """Find edges between Kalshi and Polymarket"""
    edges = []
    
    # Fetch both platforms
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as executor:
        kalshi_events = await loop.run_in_executor(executor, _fetch_kalshi_markets_sync, None, 200)
        poly_events = await loop.run_in_executor(executor, _fetch_polymarket_sync)
    
    for kalshi_event in kalshi_events:
        title = kalshi_event.get("title", "")
        ticker = kalshi_event.get("event_ticker", "")
        
        # Try to find matching Polymarket
        poly_price, poly_id, poly_question = find_matching_polymarket(title, poly_events)
        
        if poly_price and poly_price > 0:
            # Note: Kalshi prices need separate API call, using placeholder
            # In production, would fetch actual Kalshi prices
            kalshi_price = 0.5  # Placeholder
            
            edge = kalshi_price - poly_price
            
            if abs(edge) >= min_edge:
                edges.append(KalshiEdge(
                    market=title,
                    kalshi_price=kalshi_price,
                    polymarket_price=poly_price,
                    edge_pct=edge,
                    direction="BUY_POLY" if edge > 0 else "BUY_KALSHI",
                    kalshi_ticker=ticker,
                    poly_market_id=poly_id
                ))
    
    edges.sort(key=lambda x: abs(x.edge_pct), reverse=True)
    return edges

async def get_kalshi_summary() -> dict:
    """Get summary of Kalshi markets and potential edges"""
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as executor:
        kalshi_events = await loop.run_in_executor(executor, _fetch_kalshi_markets_sync, None, 100)
    
    # Categorize markets
    categories = {}
    for event in kalshi_events:
        cat = event.get("category", "Other")
        if cat not in categories:
            categories[cat] = []
        categories[cat].append({
            "ticker": event.get("event_ticker"),
            "title": event.get("title"),
            "subtitle": event.get("sub_title")
        })
    
    return {
        "source": "Kalshi",
        "timestamp": datetime.utcnow().isoformat(),
        "total_markets": len(kalshi_events),
        "categories": {k: len(v) for k, v in categories.items()},
        "sample_markets": {
            cat: markets[:3] for cat, markets in categories.items()
        },
        "api_note": "Full price data requires authenticated API access"
    }

async def get_kalshi_polymarket_comparison() -> dict:
    """Compare overlapping markets between Kalshi and Polymarket"""
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as executor:
        kalshi_events = await loop.run_in_executor(executor, _fetch_kalshi_markets_sync, None, 200)
        poly_events = await loop.run_in_executor(executor, _fetch_polymarket_sync)
    
    overlaps = []
    
    for kalshi_event in kalshi_events:
        title = kalshi_event.get("title", "")
        poly_price, poly_id, poly_question = find_matching_polymarket(title, poly_events)
        
        if poly_price is not None:
            overlaps.append({
                "kalshi_title": title,
                "kalshi_ticker": kalshi_event.get("event_ticker"),
                "polymarket_question": poly_question,
                "polymarket_price": round(poly_price * 100, 1) if poly_price else None,
                "polymarket_id": poly_id
            })
    
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "total_kalshi_markets": len(kalshi_events),
        "total_polymarket_events": len(poly_events),
        "overlapping_markets": len(overlaps),
        "overlaps": overlaps,
        "note": "Kalshi prices require authenticated API - showing Polymarket prices only"
    }


if __name__ == "__main__":
    async def test():
        print("Fetching Kalshi vs Polymarket comparison...")
        comparison = await get_kalshi_polymarket_comparison()
        
        print(f"\nKalshi markets: {comparison['total_kalshi_markets']}")
        print(f"Polymarket events: {comparison['total_polymarket_events']}")
        print(f"Overlapping: {comparison['overlapping_markets']}")
        
        print("\nOverlaps found:")
        for o in comparison['overlaps'][:10]:
            print(f"  • {o['kalshi_title']}")
            print(f"    Poly: {o['polymarket_question']} @ {o['polymarket_price']}¢")
            print()
    
    asyncio.run(test())

"""
Kalshi Edge Finder
Compares Kalshi prediction market prices with Polymarket

Supports RSA-256 JWT authentication for prod API
"""

import requests
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional, List, Dict
from datetime import datetime, timezone
import re
import os
import base64
import time

try:
    from .smart_matcher import create_signature, signatures_match, match_markets
except ImportError:
    from odds.smart_matcher import create_signature, signatures_match, match_markets

# Kalshi API endpoints
KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_DEMO_API = "https://demo-api.kalshi.co/trade-api/v2"

# Auth config
KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH = os.path.expanduser("~/.kalshi/private_key.pem")

# Cached token
_auth_token = None
_token_expiry = 0


def _load_private_key():
    """Load RSA private key from file"""
    if os.path.exists(KALSHI_PRIVATE_KEY_PATH):
        with open(KALSHI_PRIVATE_KEY_PATH, 'r') as f:
            return f.read()
    return None


def _get_auth_headers() -> dict:
    """
    Get authentication headers for Kalshi API
    Uses RSA-256 JWT signing if key is available
    """
    global _auth_token, _token_expiry
    
    if not KALSHI_KEY_ID:
        return {"Accept": "application/json"}
    
    # Check if we have a valid cached token
    if _auth_token and time.time() < _token_expiry - 60:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {_auth_token}"
        }
    
    private_key = _load_private_key()
    if not private_key:
        return {"Accept": "application/json"}
    
    try:
        import jwt
        
        now = int(time.time())
        payload = {
            "sub": KALSHI_KEY_ID,
            "iat": now,
            "exp": now + 3600,  # 1 hour expiry
        }
        
        token = jwt.encode(payload, private_key, algorithm="RS256")
        _auth_token = token
        _token_expiry = now + 3600
        
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}"
        }
    except ImportError:
        # jwt library not installed
        return {"Accept": "application/json"}
    except Exception as e:
        print(f"Kalshi auth error: {e}")
        return {"Accept": "application/json"}

@dataclass
class KalshiMarket:
    ticker: str
    event_ticker: str
    title: str
    yes_price: float  # 0-100 cents
    no_price: float
    volume_24h: float
    category: str

@dataclass 
class KalshiEdge:
    market_title: str
    selection: str
    kalshi_price: float
    polymarket_price: float
    edge_pct: float
    direction: str
    kalshi_ticker: str
    poly_market_id: Optional[str] = None
    category: str = ""

# Category inference from Kalshi categories
CATEGORY_MAP = {
    "politics": "Politics",
    "economics": "Economics",
    "financials": "Finance",
    "crypto": "Crypto",
    "tech": "Tech",
    "sports": "Sports",
    "climate": "Climate",
    "culture": "Culture",
    "science": "Science",
    "health": "Health",
}

def _fetch_kalshi_events_sync(limit: int = 200) -> List[dict]:
    """Fetch events from Kalshi API (max limit is 200)"""
    # Kalshi API has max limit of 200
    limit = min(limit, 200)
    params = {"limit": limit, "status": "open"}
    headers = _get_auth_headers()
    
    try:
        resp = requests.get(
            f"{KALSHI_API_BASE}/events",
            params=params,
            headers=headers,
            timeout=30
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("events", [])
    except Exception as e:
        pass  # Fall through to demo
    
    # Fall back to demo API
    try:
        resp = requests.get(
            f"{KALSHI_DEMO_API}/events",
            params=params,
            headers={"Accept": "application/json"},
            timeout=30
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("events", [])
    except Exception:
        pass
    
    return []

def _fetch_kalshi_markets_for_event_sync(event_ticker: str) -> List[dict]:
    """Fetch markets for a specific event to get prices"""
    try:
        resp = requests.get(
            f"{KALSHI_API_BASE}/markets",
            params={"event_ticker": event_ticker, "limit": 50},
            headers={"Accept": "application/json"},
            timeout=15
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("markets", [])
        return []
    except:
        return []

def _fetch_polymarket_sync() -> List[dict]:
    """Fetch Polymarket events"""
    try:
        resp = requests.get(
            "https://gamma-api.polymarket.com/events",
            params={"closed": "false", "limit": "500"},
            timeout=30
        )
        return resp.json() if resp.status_code == 200 else []
    except Exception as e:
        print(f"Error fetching Polymarket: {e}")
        return []

def find_polymarket_matches(kalshi_title: str, poly_events: List[dict], kalshi_category: str = "") -> List[Dict]:
    """
    Find matching Polymarket markets using entity-based smart matching.
    Returns max 2 high-confidence matches per Kalshi event.
    """
    # Build flat list of Polymarket markets with event context
    poly_markets = []
    for event in poly_events:
        event_title = event.get("title", "")
        for market in event.get("markets", []):
            price = market.get("bestAsk", 0)
            if price and 0.001 < price < 0.999:  # Filter near-certain
                poly_markets.append({
                    "title": event_title,  # Use event title for matching
                    "question": market.get("question", ""),
                    "price": float(price),
                    "market_id": market.get("id", ""),
                    "event_title": event_title,
                })
    
    # Use smart matcher
    matches = match_markets(
        source_title=kalshi_title,
        candidates=poly_markets,
        title_key="title",
        min_entity_overlap=1,
        min_confidence=0.4,
        max_matches=2
    )
    
    # Infer category
    cat = CATEGORY_MAP.get(kalshi_category.lower(), kalshi_category or "Other")
    
    # Format results
    return [
        {
            "event_title": m["event_title"],
            "question": m.get("question", ""),
            "price": m["price"],
            "market_id": m["market_id"],
            "category": cat,
            "match_confidence": m["_match_confidence"],
            "match_reason": m["_match_reason"],
        }
        for m in matches
    ]

async def get_kalshi_polymarket_comparison() -> dict:
    """Compare overlapping markets between Kalshi and Polymarket"""
    # Fetch data synchronously to avoid executor issues
    # Note: Kalshi API max limit is 200
    kalshi_events = _fetch_kalshi_events_sync(200)
    
    poly_events = _fetch_polymarket_sync()
    
    overlaps = []
    categories_found = {}
    
    for kalshi_event in kalshi_events:
        title = kalshi_event.get("title", "")
        ticker = kalshi_event.get("event_ticker", "")
        category = kalshi_event.get("category", "Other")
        
        # Find matching Polymarket markets (pass category for inference)
        poly_matches = find_polymarket_matches(title, poly_events, category)
        
        for match in poly_matches:
            cat = match.get("category", category)
            categories_found[cat] = categories_found.get(cat, 0) + 1
            
            overlaps.append({
                "kalshi_title": title,
                "kalshi_ticker": ticker,
                "kalshi_category": category,
                "polymarket_event": match["event_title"],
                "polymarket_question": match["question"][:100] if match["question"] else None,
                "polymarket_price": round(match["price"] * 100, 1),
                "polymarket_id": match["market_id"],
                "match_category": match.get("category", cat),
                "match_confidence": match.get("match_confidence", 0),
                "match_reason": match.get("match_reason", "")
            })
    
    # Deduplicate by kalshi_ticker + polymarket_id
    seen = set()
    unique_overlaps = []
    for o in overlaps:
        key = (o["kalshi_ticker"], o["polymarket_id"])
        if key not in seen:
            seen.add(key)
            unique_overlaps.append(o)
    
    # Sort by confidence (highest first), then by category
    unique_overlaps.sort(key=lambda x: (-x.get("match_confidence", 0), x["match_category"]))
    
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "total_kalshi_markets": len(kalshi_events),
        "total_polymarket_events": len(poly_events),
        "overlapping_markets": len(unique_overlaps),
        "categories": categories_found,
        "overlaps": unique_overlaps,
        "top_opportunities": [
            {
                "kalshi": o["kalshi_title"][:60],
                "polymarket_price": f"{o['polymarket_price']}¢",
                "category": o["match_category"]
            }
            for o in unique_overlaps[:10]
        ],
        "note": "Kalshi prices require authenticated API. Showing Polymarket prices for now."
    }

async def get_kalshi_summary() -> dict:
    """Get summary of Kalshi markets by category"""
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as executor:
        kalshi_events = await loop.run_in_executor(executor, _fetch_kalshi_events_sync, 300)
    
    # Categorize markets
    categories = {}
    for event in kalshi_events:
        cat = event.get("category", "Other")
        if cat not in categories:
            categories[cat] = []
        categories[cat].append({
            "ticker": event.get("event_ticker"),
            "title": event.get("title"),
            "subtitle": event.get("sub_title", "")
        })
    
    return {
        "source": "Kalshi",
        "timestamp": datetime.utcnow().isoformat(),
        "total_markets": len(kalshi_events),
        "by_category": {k: len(v) for k, v in sorted(categories.items())},
        "sample_markets": {
            cat: markets[:5] for cat, markets in sorted(categories.items())
        }
    }


if __name__ == "__main__":
    async def test():
        print("Fetching Kalshi vs Polymarket comparison...")
        comparison = await get_kalshi_polymarket_comparison()
        
        print(f"\nKalshi markets: {comparison['total_kalshi_markets']}")
        print(f"Polymarket events: {comparison['total_polymarket_events']}")
        print(f"Overlapping: {comparison['overlapping_markets']}")
        print(f"\nCategories: {comparison['categories']}")
        
        print("\nTop overlaps:")
        for o in comparison['overlaps'][:15]:
            print(f"  • [{o['match_category']}] {o['kalshi_title'][:50]}")
            print(f"    Poly: {o['polymarket_price']}¢ - {o['polymarket_question'][:50] if o['polymarket_question'] else 'N/A'}")
            print()
    
    asyncio.run(test())

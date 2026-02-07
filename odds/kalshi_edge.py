"""
Kalshi Edge Finder
Compares Kalshi prediction market prices with Polymarket
"""

import requests
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional, List, Dict
from datetime import datetime
import re

# Kalshi API endpoints
KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_DEMO_API = "https://demo-api.kalshi.co/trade-api/v2"

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

# Expanded market title mappings between Kalshi and Polymarket
MARKET_MAPPINGS = [
    # Politics - Fed & Economy
    {"kalshi_search": "Fed Chair", "polymarket_search": "Fed Chair", "category": "Politics"},
    {"kalshi_search": "Fed Chair", "polymarket_search": "Warsh", "category": "Politics"},
    {"kalshi_search": "interest rate", "polymarket_search": "interest rate", "category": "Economics"},
    {"kalshi_search": "recession", "polymarket_search": "recession", "category": "Economics"},
    
    # Politics - Elections
    {"kalshi_search": "Democratic nominee", "polymarket_search": "Democratic", "category": "Politics"},
    {"kalshi_search": "Republican nominee", "polymarket_search": "Republican", "category": "Politics"},
    {"kalshi_search": "Presidential Election", "polymarket_search": "President", "category": "Politics"},
    {"kalshi_search": "presidential election", "polymarket_search": "2028", "category": "Politics"},
    {"kalshi_search": "House of Representatives", "polymarket_search": "House", "category": "Politics"},
    {"kalshi_search": "control of the House", "polymarket_search": "House", "category": "Politics"},
    {"kalshi_search": "Senate", "polymarket_search": "Senate", "category": "Politics"},
    {"kalshi_search": "Governor", "polymarket_search": "Governor", "category": "Politics"},
    {"kalshi_search": "Newsom", "polymarket_search": "Newsom", "category": "Politics"},
    {"kalshi_search": "Vance", "polymarket_search": "Vance", "category": "Politics"},
    {"kalshi_search": "DeSantis", "polymarket_search": "DeSantis", "category": "Politics"},
    {"kalshi_search": "AOC", "polymarket_search": "AOC", "category": "Politics"},
    {"kalshi_search": "Ocasio-Cortez", "polymarket_search": "Ocasio", "category": "Politics"},
    
    # Politics - International
    {"kalshi_search": "Khamenei", "polymarket_search": "Khamenei", "category": "Politics"},
    {"kalshi_search": "Netanyahu", "polymarket_search": "Netanyahu", "category": "Politics"},
    {"kalshi_search": "Zelensky", "polymarket_search": "Zelensky", "category": "Politics"},
    {"kalshi_search": "Putin", "polymarket_search": "Putin", "category": "Politics"},
    {"kalshi_search": "Xi Jinping", "polymarket_search": "Xi", "category": "Politics"},
    {"kalshi_search": "Ukraine", "polymarket_search": "Ukraine", "category": "Politics"},
    {"kalshi_search": "Israel", "polymarket_search": "Israel", "category": "Politics"},
    {"kalshi_search": "Gaza", "polymarket_search": "Gaza", "category": "Politics"},
    {"kalshi_search": "Iran", "polymarket_search": "Iran", "category": "Politics"},
    {"kalshi_search": "Venezuela", "polymarket_search": "Venezuela", "category": "Politics"},
    {"kalshi_search": "Maduro", "polymarket_search": "Maduro", "category": "Politics"},
    
    # Politics - Trump & Policy
    {"kalshi_search": "Trump", "polymarket_search": "Trump", "category": "Politics"},
    {"kalshi_search": "tariff", "polymarket_search": "tariff", "category": "Politics"},
    {"kalshi_search": "executive order", "polymarket_search": "executive order", "category": "Politics"},
    {"kalshi_search": "government shut", "polymarket_search": "shutdown", "category": "Politics"},
    {"kalshi_search": "SCOTUS", "polymarket_search": "Supreme Court", "category": "Politics"},
    {"kalshi_search": "Supreme Court", "polymarket_search": "Supreme Court", "category": "Politics"},
    {"kalshi_search": "impeach", "polymarket_search": "impeach", "category": "Politics"},
    {"kalshi_search": "Cabinet", "polymarket_search": "Cabinet", "category": "Politics"},
    
    # Sports
    {"kalshi_search": "Super Bowl", "polymarket_search": "Super Bowl", "category": "Sports"},
    {"kalshi_search": "NBA Champion", "polymarket_search": "NBA Champion", "category": "Sports"},
    {"kalshi_search": "World Series", "polymarket_search": "World Series", "category": "Sports"},
    {"kalshi_search": "Stanley Cup", "polymarket_search": "Stanley Cup", "category": "Sports"},
    {"kalshi_search": "March Madness", "polymarket_search": "NCAA", "category": "Sports"},
    {"kalshi_search": "Premier League", "polymarket_search": "Premier League", "category": "Sports"},
    {"kalshi_search": "Champions League", "polymarket_search": "Champions League", "category": "Sports"},
    {"kalshi_search": "World Cup", "polymarket_search": "World Cup", "category": "Sports"},
    
    # Crypto
    {"kalshi_search": "Bitcoin", "polymarket_search": "Bitcoin", "category": "Crypto"},
    {"kalshi_search": "BTC", "polymarket_search": "BTC", "category": "Crypto"},
    {"kalshi_search": "Ethereum", "polymarket_search": "Ethereum", "category": "Crypto"},
    {"kalshi_search": "ETH", "polymarket_search": "ETH", "category": "Crypto"},
    {"kalshi_search": "crypto", "polymarket_search": "crypto", "category": "Crypto"},
    
    # Tech & Companies
    {"kalshi_search": "OpenAI", "polymarket_search": "OpenAI", "category": "Tech"},
    {"kalshi_search": "Anthropic", "polymarket_search": "Anthropic", "category": "Tech"},
    {"kalshi_search": "Tesla", "polymarket_search": "Tesla", "category": "Tech"},
    {"kalshi_search": "Elon Musk", "polymarket_search": "Elon", "category": "Tech"},
    {"kalshi_search": "IPO", "polymarket_search": "IPO", "category": "Tech"},
    {"kalshi_search": "TikTok", "polymarket_search": "TikTok", "category": "Tech"},
    
    # Other
    {"kalshi_search": "aliens", "polymarket_search": "alien", "category": "Science"},
    {"kalshi_search": "UFO", "polymarket_search": "UFO", "category": "Science"},
    {"kalshi_search": "AI", "polymarket_search": "AI", "category": "Tech"},
    {"kalshi_search": "bird flu", "polymarket_search": "bird flu", "category": "Health"},
    {"kalshi_search": "pandemic", "polymarket_search": "pandemic", "category": "Health"},
]

def _fetch_kalshi_events_sync(limit: int = 200) -> List[dict]:
    """Fetch events from Kalshi API (max limit is 200)"""
    # Kalshi API has max limit of 200
    limit = min(limit, 200)
    params = {"limit": limit, "status": "open"}
    
    try:
        resp = requests.get(
            f"{KALSHI_API_BASE}/events",
            params=params,
            headers={"Accept": "application/json"},
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

def normalize_text(text: str) -> str:
    """Normalize text for matching"""
    text = text.lower()
    text = re.sub(r'[^\w\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def texts_match(text1: str, text2: str, threshold: float = 0.5) -> bool:
    """Check if two texts have significant word overlap"""
    # Filter out common stop words
    stop_words = {'will', 'the', 'a', 'an', 'be', 'to', 'of', 'in', 'for', 'on', 'at', 'by', 'is', 'it', 'as', 'or', 'and', 'this', 'that', 'with', 'from', 'are', 'was', 'were', 'been', 'have', 'has', 'had', 'do', 'does', 'did', 'but', 'if', 'then', 'than', 'so', 'what', 'when', 'where', 'who', 'which', 'how', 'before', 'after', 'during', 'while', 'until'}
    
    words1 = set(normalize_text(text1).split()) - stop_words
    words2 = set(normalize_text(text2).split()) - stop_words
    
    if len(words1) < 2 or len(words2) < 2:
        return False
    
    intersection = words1 & words2
    
    # Require at least 2 meaningful words in common
    if len(intersection) < 2:
        return False
    
    # Jaccard similarity on remaining words
    union = words1 | words2
    similarity = len(intersection) / len(union)
    return similarity >= threshold

def find_polymarket_matches(kalshi_title: str, poly_events: List[dict]) -> List[Dict]:
    """Find matching Polymarket markets for a Kalshi event (max 3 per event)"""
    matches = []
    kalshi_lower = kalshi_title.lower()
    seen_events = set()
    
    # First try mapping-based matching (more precise)
    for mapping in MARKET_MAPPINGS:
        if mapping["kalshi_search"].lower() not in kalshi_lower:
            continue
            
        for event in poly_events:
            event_title = event.get("title", "")
            poly_title = event_title.lower()
            
            if mapping["polymarket_search"].lower() in poly_title:
                if event_title in seen_events:
                    continue
                seen_events.add(event_title)
                
                # Only take the first (most relevant) market from each event
                for market in event.get("markets", [])[:1]:
                    price = market.get("bestAsk", 0)
                    if price and 0.001 < price < 0.999:  # Filter out near-certain markets
                        matches.append({
                            "event_title": event_title,
                            "question": market.get("question", ""),
                            "price": float(price),
                            "market_id": market.get("id", ""),
                            "category": mapping["category"]
                        })
                        
                if len(matches) >= 3:  # Max 3 matches per Kalshi event
                    return matches
    
    # Only try direct title matching if no mapping matches found
    if not matches:
        for event in poly_events:
            event_title = event.get("title", "")
            if event_title in seen_events:
                continue
                
            if texts_match(kalshi_title, event_title, 0.4):  # Higher threshold
                seen_events.add(event_title)
                
                for market in event.get("markets", [])[:1]:
                    price = market.get("bestAsk", 0)
                    if price and 0.001 < price < 0.999:
                        matches.append({
                            "event_title": event_title,
                            "question": market.get("question", ""),
                            "price": float(price),
                            "market_id": market.get("id", ""),
                            "category": "Matched"
                        })
                        
                if len(matches) >= 2:  # Max 2 for text matches
                    return matches
    
    return matches

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
        
        # Find matching Polymarket markets
        poly_matches = find_polymarket_matches(title, poly_events)
        
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
                "match_category": cat
            })
    
    # Deduplicate by kalshi_ticker + polymarket_id
    seen = set()
    unique_overlaps = []
    for o in overlaps:
        key = (o["kalshi_ticker"], o["polymarket_id"])
        if key not in seen:
            seen.add(key)
            unique_overlaps.append(o)
    
    # Sort by category then by title
    unique_overlaps.sort(key=lambda x: (x["match_category"], x["kalshi_title"]))
    
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

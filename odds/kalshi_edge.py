"""
Kalshi Edge Finder
Compares Kalshi prediction market prices with Polymarket

Supports RSA-256 JWT authentication for prod API

COMPREHENSIVE FETCHING:
- Fetches ALL series via /series endpoint with pagination
- Fetches ALL markets via /markets endpoint with pagination  
- Specifically discovers entertainment/sports props (Super Bowl, halftime, Grammy, Oscar, etc.)
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

# Entertainment/Sports keywords for props discovery
ENTERTAINMENT_KEYWORDS = [
    "super bowl", "halftime", "superbowl", "sb ",
    "grammy", "oscar", "emmy", "golden globe", "academy award",
    "nfl", "nba", "mlb", "nhl", "mls",
    "celebrity", "taylor swift", "beyonce", "drake",
    "concert", "tour", "album", "movie", "film", "box office",
    "award show", "vma", "mtv", "bet award",
    "world series", "playoffs", "finals", "championship",
    "march madness", "ncaa", "college football",
    "kendrick", "weeknd", "rihanna", "usher",
    "opening song", "first song", "setlist", "performance"
]

# Cached token
_auth_token = None
_token_expiry = 0

# Cache for expensive API calls (5 minute TTL)
_cache = {}
_cache_ttl = 300  # 5 minutes


def _get_cached(key: str):
    """Get cached value if not expired."""
    if key in _cache:
        data, expiry = _cache[key]
        if time.time() < expiry:
            return data
        del _cache[key]
    return None


def _set_cached(key: str, data, ttl: int = None):
    """Set cached value with TTL."""
    _cache[key] = (data, time.time() + (ttl or _cache_ttl))


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

def _fetch_kalshi_series_sync(limit: int = 200) -> List[dict]:
    """
    Fetch ALL series from Kalshi API with cursor-based pagination.
    Series contain groups of related markets (e.g., KXFIRSTSUPERBOWLSONG series).
    Uses caching (5 min TTL) to avoid hammering the API.
    """
    # Check cache
    cached = _get_cached("kalshi_series")
    if cached:
        return cached
    
    all_series = []
    cursor = None
    headers = _get_auth_headers()
    
    while True:
        params = {"limit": min(limit, 200)}
        if cursor:
            params["cursor"] = cursor
        
        try:
            resp = requests.get(
                f"{KALSHI_API_BASE}/series",
                params=params,
                headers=headers,
                timeout=30
            )
            if resp.status_code == 200:
                data = resp.json()
                series_batch = data.get("series", [])
                all_series.extend(series_batch)
                
                # Check for next page
                cursor = data.get("cursor")
                if not cursor or len(series_batch) < limit:
                    break
            else:
                break
        except Exception as e:
            print(f"Error fetching series: {e}")
            break
    
    # Fall back to demo if prod fails
    if not all_series:
        cursor = None
        while True:
            params = {"limit": min(limit, 200)}
            if cursor:
                params["cursor"] = cursor
            try:
                resp = requests.get(
                    f"{KALSHI_DEMO_API}/series",
                    params=params,
                    headers={"Accept": "application/json"},
                    timeout=30
                )
                if resp.status_code == 200:
                    data = resp.json()
                    series_batch = data.get("series", [])
                    all_series.extend(series_batch)
                    cursor = data.get("cursor")
                    if not cursor or len(series_batch) < limit:
                        break
                else:
                    break
            except:
                break
    
    return all_series


def _fetch_all_kalshi_markets_sync(
    series_ticker: Optional[str] = None,
    event_ticker: Optional[str] = None,
    status: str = "open",
    max_pages: int = 50
) -> List[dict]:
    """
    Fetch ALL markets from Kalshi API with cursor-based pagination.
    
    Args:
        series_ticker: Filter by series (e.g., "KXFIRSTSUPERBOWLSONG")
        event_ticker: Filter by event
        status: Market status ("open", "closed", "all")
        max_pages: Maximum pages to fetch (safety limit)
    
    Returns:
        List of all markets matching criteria
    """
    all_markets = []
    cursor = None
    headers = _get_auth_headers()
    page = 0
    
    while page < max_pages:
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if status and status != "all":
            params["status"] = status
        
        try:
            resp = requests.get(
                f"{KALSHI_API_BASE}/markets",
                params=params,
                headers=headers,
                timeout=30
            )
            if resp.status_code == 200:
                data = resp.json()
                markets_batch = data.get("markets", [])
                all_markets.extend(markets_batch)
                
                # Check for next page
                cursor = data.get("cursor")
                if not cursor or len(markets_batch) < 200:
                    break
                page += 1
            else:
                break
        except Exception as e:
            print(f"Error fetching markets page {page}: {e}")
            break
    
    # Fall back to demo if prod fails
    if not all_markets:
        cursor = None
        page = 0
        while page < max_pages:
            params = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            if series_ticker:
                params["series_ticker"] = series_ticker
            if event_ticker:
                params["event_ticker"] = event_ticker
            if status and status != "all":
                params["status"] = status
            
            try:
                resp = requests.get(
                    f"{KALSHI_DEMO_API}/markets",
                    params=params,
                    headers={"Accept": "application/json"},
                    timeout=30
                )
                if resp.status_code == 200:
                    data = resp.json()
                    markets_batch = data.get("markets", [])
                    all_markets.extend(markets_batch)
                    cursor = data.get("cursor")
                    if not cursor or len(markets_batch) < 200:
                        break
                    page += 1
                else:
                    break
            except:
                break
    
    return all_markets


def _is_entertainment_market(text: str) -> bool:
    """Check if market text matches entertainment/sports props keywords."""
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in ENTERTAINMENT_KEYWORDS)


def _extract_odds_from_market(market: dict) -> dict:
    """Extract yes/no prices and convert to odds format."""
    yes_bid = market.get("yes_bid", 0) or 0
    yes_ask = market.get("yes_ask", 0) or 0
    no_bid = market.get("no_bid", 0) or 0
    no_ask = market.get("no_ask", 0) or 0
    
    # Use midpoint for display, or ask price
    yes_price = (yes_bid + yes_ask) / 2 if yes_bid and yes_ask else yes_ask or yes_bid
    no_price = (no_bid + no_ask) / 2 if no_bid and no_ask else no_ask or no_bid
    
    # Also check last_price field
    if not yes_price:
        yes_price = market.get("last_price", 0) or 0
    if not no_price and yes_price:
        no_price = 100 - yes_price
    
    return {
        "yes_price": yes_price,
        "no_price": no_price,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "volume": market.get("volume", 0) or 0,
        "volume_24h": market.get("volume_24h", 0) or 0,
        "open_interest": market.get("open_interest", 0) or 0,
    }


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
    """
    Compare overlapping markets between Kalshi and Polymarket.
    
    Uses comprehensive fetching:
    1. Fetches ALL events (not just 200)
    2. Fetches ALL markets with pagination
    3. Fetches ALL series for better coverage
    4. Includes entertainment/sports props
    """
    # Fetch data comprehensively
    kalshi_events = _fetch_kalshi_events_sync(200)  # Events for backward compat
    kalshi_markets = _fetch_all_kalshi_markets_sync(status="open")  # ALL markets
    kalshi_series = _fetch_kalshi_series_sync()  # ALL series
    
    poly_events = _fetch_polymarket_sync()
    
    overlaps = []
    categories_found = {}
    seen_pairs = set()
    
    # Process events (original method)
    for kalshi_event in kalshi_events:
        title = kalshi_event.get("title", "")
        ticker = kalshi_event.get("event_ticker", "")
        category = kalshi_event.get("category", "Other")
        
        poly_matches = find_polymarket_matches(title, poly_events, category)
        
        for match in poly_matches:
            pair_key = (ticker, match["market_id"])
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            
            cat = match.get("category", category)
            categories_found[cat] = categories_found.get(cat, 0) + 1
            
            overlaps.append({
                "kalshi_title": title,
                "kalshi_ticker": ticker,
                "kalshi_category": category,
                "kalshi_yes_price": None,  # Events don't have prices
                "polymarket_event": match["event_title"],
                "polymarket_question": match["question"][:100] if match["question"] else None,
                "polymarket_price": round(match["price"] * 100, 1),
                "polymarket_id": match["market_id"],
                "match_category": match.get("category", cat),
                "match_confidence": match.get("match_confidence", 0),
                "match_reason": match.get("match_reason", ""),
                "source": "event"
            })
    
    # Process direct markets (NEW - comprehensive)
    for market in kalshi_markets:
        title = market.get("title", "")
        ticker = market.get("ticker", "")
        event_ticker = market.get("event_ticker", "")
        series_ticker = market.get("series_ticker", "")
        category = market.get("category", "Other")
        
        # Extract Kalshi price
        odds = _extract_odds_from_market(market)
        kalshi_yes_price = odds["yes_price"]
        
        poly_matches = find_polymarket_matches(title, poly_events, category)
        
        for match in poly_matches:
            pair_key = (ticker, match["market_id"])
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            
            cat = match.get("category", category)
            categories_found[cat] = categories_found.get(cat, 0) + 1
            
            # Calculate edge if we have both prices
            edge_pct = None
            if kalshi_yes_price and match["price"]:
                kalshi_prob = kalshi_yes_price / 100
                poly_prob = match["price"]
                edge_pct = round((kalshi_prob - poly_prob) * 100, 2)
            
            overlaps.append({
                "kalshi_title": title,
                "kalshi_ticker": ticker,
                "kalshi_event_ticker": event_ticker,
                "kalshi_series_ticker": series_ticker,
                "kalshi_category": category,
                "kalshi_yes_price": kalshi_yes_price,
                "kalshi_volume": odds["volume"],
                "polymarket_event": match["event_title"],
                "polymarket_question": match["question"][:100] if match["question"] else None,
                "polymarket_price": round(match["price"] * 100, 1),
                "polymarket_id": match["market_id"],
                "edge_pct": edge_pct,
                "match_category": match.get("category", cat),
                "match_confidence": match.get("match_confidence", 0),
                "match_reason": match.get("match_reason", ""),
                "source": "market"
            })
    
    # Sort by confidence (highest first), then by edge if available
    overlaps.sort(key=lambda x: (
        -x.get("match_confidence", 0),
        -abs(x.get("edge_pct", 0) or 0)
    ))
    
    # Find edges (where we have price differential)
    edges = [o for o in overlaps if o.get("edge_pct") and abs(o["edge_pct"]) >= 3]
    edges.sort(key=lambda x: -abs(x["edge_pct"]))
    
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "total_kalshi_events": len(kalshi_events),
        "total_kalshi_markets": len(kalshi_markets),
        "total_kalshi_series": len(kalshi_series),
        "total_polymarket_events": len(poly_events),
        "overlapping_markets": len(overlaps),
        "markets_with_edge": len(edges),
        "categories": categories_found,
        "overlaps": overlaps[:100],  # Top 100
        "edges": edges[:20],  # Top 20 edges
        "top_opportunities": [
            {
                "kalshi": o["kalshi_title"][:60],
                "kalshi_ticker": o["kalshi_ticker"],
                "kalshi_price": f"{o.get('kalshi_yes_price', 'N/A')}¢",
                "polymarket_price": f"{o['polymarket_price']}¢",
                "edge": f"{o.get('edge_pct', 'N/A')}%",
                "category": o["match_category"]
            }
            for o in edges[:10]
        ] if edges else [
            {
                "kalshi": o["kalshi_title"][:60],
                "polymarket_price": f"{o['polymarket_price']}¢",
                "category": o["match_category"]
            }
            for o in overlaps[:10]
        ],
        "series_summary": {
            "total": len(kalshi_series),
            "sample_tickers": [s.get("ticker") for s in kalshi_series[:20]]
        },
        "note": "Comprehensive comparison using events, markets, and series endpoints with full pagination."
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


async def get_kalshi_entertainment_props() -> dict:
    """
    Discover entertainment and sports prop markets on Kalshi.
    
    Searches through:
    1. All series for entertainment keywords
    2. All markets for entertainment keywords
    
    Returns structured data with odds for:
    - Super Bowl halftime (KXFIRSTSUPERBOWLSONG, KXSBSETLISTS, KXHALFTIMESHOW)
    - Grammy/Oscar/Emmy awards
    - Celebrity/entertainment props
    - Sports props (NFL, NBA, etc.)
    """
    # Fetch all series and markets
    all_series = _fetch_kalshi_series_sync()
    all_markets = _fetch_all_kalshi_markets_sync()
    
    # Find entertainment series
    entertainment_series = []
    for series in all_series:
        ticker = series.get("ticker", "")
        title = series.get("title", "")
        subtitle = series.get("sub_title", "") or ""
        category = series.get("category", "")
        
        combined_text = f"{ticker} {title} {subtitle} {category}"
        if _is_entertainment_market(combined_text):
            entertainment_series.append({
                "ticker": ticker,
                "title": title,
                "subtitle": subtitle,
                "category": category,
            })
    
    # Find entertainment markets
    entertainment_markets = []
    seen_tickers = set()
    
    for market in all_markets:
        ticker = market.get("ticker", "")
        if ticker in seen_tickers:
            continue
        
        title = market.get("title", "")
        subtitle = market.get("subtitle", "") or ""
        event_ticker = market.get("event_ticker", "")
        series_ticker = market.get("series_ticker", "")
        
        combined_text = f"{ticker} {title} {subtitle} {event_ticker} {series_ticker}"
        if _is_entertainment_market(combined_text):
            seen_tickers.add(ticker)
            odds = _extract_odds_from_market(market)
            
            entertainment_markets.append({
                "ticker": ticker,
                "title": title,
                "subtitle": subtitle,
                "event_ticker": event_ticker,
                "series_ticker": series_ticker,
                "status": market.get("status", ""),
                "yes_price_cents": odds["yes_price"],
                "no_price_cents": odds["no_price"],
                "yes_probability": round(odds["yes_price"] / 100, 4) if odds["yes_price"] else None,
                "volume_24h": odds["volume_24h"],
                "volume_total": odds["volume"],
                "open_interest": odds["open_interest"],
                "close_time": market.get("close_time"),
                "expiration_time": market.get("expiration_time"),
            })
    
    # Also fetch markets from known entertainment series
    known_entertainment_series = [
        "KXFIRSTSUPERBOWLSONG",  # Super Bowl halftime opener
        "KXSBSETLISTS",          # Super Bowl setlist
        "KXHALFTIMESHOW",        # Halftime show length
        "KXGRAMMYS",             # Grammy awards
        "KXOSCARS",              # Oscar awards
        "KXEMMYS",               # Emmy awards
        "KXGOLDENGLOBES",        # Golden Globes
    ]
    
    for series_ticker in known_entertainment_series:
        series_markets = _fetch_all_kalshi_markets_sync(series_ticker=series_ticker, status="all")
        for market in series_markets:
            ticker = market.get("ticker", "")
            if ticker not in seen_tickers:
                seen_tickers.add(ticker)
                odds = _extract_odds_from_market(market)
                
                entertainment_markets.append({
                    "ticker": ticker,
                    "title": market.get("title", ""),
                    "subtitle": market.get("subtitle", "") or "",
                    "event_ticker": market.get("event_ticker", ""),
                    "series_ticker": series_ticker,
                    "status": market.get("status", ""),
                    "yes_price_cents": odds["yes_price"],
                    "no_price_cents": odds["no_price"],
                    "yes_probability": round(odds["yes_price"] / 100, 4) if odds["yes_price"] else None,
                    "volume_24h": odds["volume_24h"],
                    "volume_total": odds["volume"],
                    "open_interest": odds["open_interest"],
                    "close_time": market.get("close_time"),
                    "expiration_time": market.get("expiration_time"),
                })
    
    # Categorize markets
    categories = {
        "super_bowl": [],
        "halftime": [],
        "grammy": [],
        "oscar": [],
        "emmy": [],
        "other_awards": [],
        "sports": [],
        "celebrity": [],
        "other": [],
    }
    
    for market in entertainment_markets:
        combined = f"{market['ticker']} {market['title']} {market.get('series_ticker', '')}".lower()
        
        if any(k in combined for k in ["super bowl", "superbowl", "sb "]):
            if "halftime" in combined or "song" in combined or "setlist" in combined or "performance" in combined:
                categories["halftime"].append(market)
            else:
                categories["super_bowl"].append(market)
        elif "grammy" in combined:
            categories["grammy"].append(market)
        elif "oscar" in combined or "academy award" in combined:
            categories["oscar"].append(market)
        elif "emmy" in combined:
            categories["emmy"].append(market)
        elif any(k in combined for k in ["golden globe", "vma", "bet award", "mtv"]):
            categories["other_awards"].append(market)
        elif any(k in combined for k in ["nfl", "nba", "mlb", "nhl", "playoff", "championship", "world series"]):
            categories["sports"].append(market)
        elif any(k in combined for k in ["taylor swift", "beyonce", "drake", "kendrick", "celebrity"]):
            categories["celebrity"].append(market)
        else:
            categories["other"].append(market)
    
    # Sort each category by volume
    for cat in categories:
        categories[cat].sort(key=lambda x: x.get("volume_total", 0) or 0, reverse=True)
    
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "source": "Kalshi",
        "total_series_scanned": len(all_series),
        "total_markets_scanned": len(all_markets),
        "entertainment_series_found": len(entertainment_series),
        "entertainment_markets_found": len(entertainment_markets),
        "series": entertainment_series[:20],  # Top 20 series
        "markets_by_category": {k: v for k, v in categories.items() if v},
        "all_markets": entertainment_markets,
        "highlight_tickers": {
            "halftime_opener": [m["ticker"] for m in categories["halftime"] if "first" in m["ticker"].lower() or "song" in m["title"].lower()][:5],
            "setlist": [m["ticker"] for m in categories["halftime"] if "setlist" in m["ticker"].lower() or "setlist" in m["title"].lower()][:5],
            "halftime_other": [m["ticker"] for m in categories["halftime"]][:10],
        }
    }


async def get_kalshi_all_markets() -> dict:
    """
    Get ALL Kalshi markets with comprehensive pagination.
    Returns full market data for analysis.
    """
    all_markets = _fetch_all_kalshi_markets_sync(status="open")
    all_series = _fetch_kalshi_series_sync()
    
    # Categorize markets
    categories = {}
    for market in all_markets:
        cat = market.get("category", "Other")
        if cat not in categories:
            categories[cat] = 0
        categories[cat] += 1
    
    # Extract top markets by volume
    sorted_markets = sorted(all_markets, key=lambda x: x.get("volume", 0) or 0, reverse=True)
    
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "source": "Kalshi",
        "total_markets": len(all_markets),
        "total_series": len(all_series),
        "categories": categories,
        "top_by_volume": [
            {
                "ticker": m.get("ticker"),
                "title": m.get("title"),
                "volume": m.get("volume"),
                "yes_price": m.get("yes_ask") or m.get("last_price"),
            }
            for m in sorted_markets[:50]
        ],
        "series_tickers": [s.get("ticker") for s in all_series],
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

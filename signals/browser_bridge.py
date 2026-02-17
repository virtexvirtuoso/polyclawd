#!/usr/bin/env python3
"""
Browser Bridge — Polyclawd interface to browser-use container.

Provides structured scraping for prediction market data sources that
don't have reliable APIs:
- VegasInsider odds pages
- ESPN odds/injury pages  
- Market metadata enrichment (full descriptions, resolution criteria)
- Exchange announcement pages

Calls browser-use Docker container HTTP endpoint.
Falls back gracefully when browser-use is unavailable.
"""

import json
import logging
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)

# Browser-use container endpoint (set by Docker compose)
BROWSER_USE_URL = "http://127.0.0.1:8430"
TIMEOUT = 30


def _browser_request(url: str, selectors: Optional[Dict] = None, 
                     wait_for: Optional[str] = None, 
                     timeout: int = TIMEOUT) -> Optional[Dict]:
    """Make a request to browser-use container.
    
    Args:
        url: Page to scrape
        selectors: CSS selectors to extract {name: selector}
        wait_for: CSS selector to wait for before extraction
        timeout: Request timeout
    
    Returns:
        Dict with extracted data or None on failure
    """
    payload = json.dumps({
        "url": url,
        "selectors": selectors or {},
        "wait_for": wait_for,
        "timeout": timeout,
    }).encode()
    
    try:
        req = urllib.request.Request(
            f"{BROWSER_USE_URL}/scrape",
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "Polyclawd/2.0"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout + 5) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        logger.debug(f"Browser-use unavailable: {e}")
        return None
    except Exception as e:
        logger.warning(f"Browser request failed: {e}")
        return None


def is_available() -> bool:
    """Check if browser-use container is running."""
    try:
        req = urllib.request.Request(f"{BROWSER_USE_URL}/health")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


# ============================================================================
# Sports Odds Scraping
# ============================================================================

def scrape_vegasinsider_nfl() -> List[Dict]:
    """Scrape VegasInsider for NFL odds."""
    data = _browser_request(
        url="https://www.vegasinsider.com/nfl/odds/las-vegas/",
        selectors={
            "games": "table.odds-table tr",
            "teams": ".team-name",
            "spreads": ".spread",
            "moneylines": ".moneyline",
            "totals": ".total",
        },
        wait_for="table.odds-table"
    )
    if not data:
        return []
    
    # Parse into structured odds
    games = []
    # TODO: Parse browser-use response format once container is live
    return games


def scrape_espn_odds(sport: str = "nfl") -> List[Dict]:
    """Scrape ESPN for moneyline odds."""
    sport_urls = {
        "nfl": "https://www.espn.com/nfl/odds",
        "nba": "https://www.espn.com/nba/odds",
        "nhl": "https://www.espn.com/nhl/odds",
    }
    url = sport_urls.get(sport, sport_urls["nfl"])
    
    data = _browser_request(
        url=url,
        selectors={
            "matchups": ".odds-table__row",
            "teams": ".odds-table__team",
            "odds": ".odds-table__odds",
        },
        wait_for=".odds-table"
    )
    if not data:
        return []
    
    return []  # TODO: Parse once live


# ============================================================================
# Market Metadata Enrichment
# ============================================================================

def enrich_polymarket_metadata(slug: str) -> Optional[Dict]:
    """Get full market metadata from Polymarket page.
    
    Extracts: full description, resolution criteria, resolution source,
    end date, related markets.
    """
    data = _browser_request(
        url=f"https://polymarket.com/event/{slug}",
        selectors={
            "title": "h1",
            "description": "[data-testid='market-description']",
            "resolution": "[data-testid='resolution-source']",
            "end_date": "[data-testid='end-date']",
            "volume": "[data-testid='volume']",
            "liquidity": "[data-testid='liquidity']",
        },
        wait_for="h1"
    )
    return data


def enrich_kalshi_metadata(ticker: str) -> Optional[Dict]:
    """Get full market metadata from Kalshi page."""
    data = _browser_request(
        url=f"https://kalshi.com/markets/{ticker}",
        selectors={
            "title": "h1",
            "description": ".market-description",
            "rules": ".resolution-rules",
            "end_date": ".expiration-date",
        },
        wait_for="h1"
    )
    return data


# ============================================================================
# Cross-Platform Market Matching (Enhanced)
# ============================================================================

def get_enriched_market_data(market_id: str, platform: str, title: str) -> Dict:
    """Get enriched data for better cross-platform matching.
    
    Returns structured metadata: sport, teams, date, market_type, timeframe.
    Falls back to title parsing if browser unavailable.
    """
    metadata = {"title": title, "platform": platform, "market_id": market_id}
    
    if is_available():
        if platform == "polymarket":
            enriched = enrich_polymarket_metadata(market_id)
        elif platform == "kalshi":
            enriched = enrich_kalshi_metadata(market_id)
        else:
            enriched = None
        
        if enriched:
            metadata.update(enriched)
    
    # Always do title parsing as fallback/supplement
    metadata.update(_parse_market_title(title))
    return metadata


def _parse_market_title(title: str) -> Dict:
    """Parse market title into structured fields."""
    import re
    
    result = {
        "market_type": "unknown",
        "timeframe": "unknown",
        "asset": None,
        "threshold": None,
        "direction": None,
    }
    
    title_lower = title.lower()
    
    # Crypto markets
    if any(x in title_lower for x in ["bitcoin", "btc", "ethereum", "eth"]):
        result["market_type"] = "crypto"
        if "bitcoin" in title_lower or "btc" in title_lower:
            result["asset"] = "BTC"
        elif "ethereum" in title_lower or "eth" in title_lower:
            result["asset"] = "ETH"
    
    # Up or Down
    if "up or down" in title_lower:
        result["direction"] = "up_or_down"
    elif "above" in title_lower:
        result["direction"] = "above"
        # Extract threshold
        match = re.search(r'\$([0-9,]+)', title)
        if match:
            result["threshold"] = float(match.group(1).replace(",", ""))
    elif "reach" in title_lower:
        result["direction"] = "reach"
        match = re.search(r'\$([0-9,]+)', title)
        if match:
            result["threshold"] = float(match.group(1).replace(",", ""))
    
    # Date extraction
    date_match = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})', title_lower)
    if date_match:
        result["timeframe"] = f"{date_match.group(1)} {date_match.group(2)}"
    
    # Time extraction (intraday)
    time_match = re.search(r'(\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*et)', title_lower)
    if time_match:
        result["timeframe"] += f" {time_match.group(1)}"
    
    # AI model markets
    if any(x in title_lower for x in ["ai model", "google", "anthropic", "openai"]):
        result["market_type"] = "ai"
    
    # Sports
    if any(x in title_lower for x in ["nfl", "nba", "nhl", "mlb", "super bowl"]):
        result["market_type"] = "sports"
    
    return result


# ============================================================================
# Exchange Announcements
# ============================================================================

def scrape_binance_announcements() -> List[Dict]:
    """Scrape Binance new listing announcements."""
    data = _browser_request(
        url="https://www.binance.com/en/support/announcement/new-cryptocurrency-listing",
        selectors={
            "announcements": ".css-1ej4hfo",
            "titles": "a.css-1ej4hfo",
        },
        wait_for=".css-1ej4hfo"
    )
    if not data:
        return []
    return []  # TODO


def scrape_bybit_announcements() -> List[Dict]:
    """Scrape Bybit new listing announcements."""
    data = _browser_request(
        url="https://announcements.bybit.com/en/new-listings/",
        selectors={
            "listings": ".article-item",
            "titles": ".article-item__title",
        },
        wait_for=".article-item"
    )
    if not data:
        return []
    return []  # TODO


# ============================================================================
# API Integration
# ============================================================================

def get_status() -> Dict:
    """Get browser bridge status."""
    available = is_available()
    return {
        "browser_use_available": available,
        "endpoint": BROWSER_USE_URL,
        "capabilities": [
            "vegasinsider_odds",
            "espn_odds",
            "market_metadata",
            "exchange_announcements",
        ] if available else [],
        "fallback": "title_parsing_only",
    }


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        print(json.dumps(get_status(), indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "test":
        # Test title parsing
        tests = [
            "Bitcoin Up or Down on February 16?",
            "Will the price of Bitcoin be above $70,000 on February 15?",
            "Will Google have the best AI model at the end of February 2026?",
            "Ethereum Up or Down - February 14, 2PM ET",
        ]
        for t in tests:
            parsed = _parse_market_title(t)
            print(f"  {t[:50]}")
            print(f"    → {parsed}")
            print()
    else:
        print(json.dumps(get_status(), indent=2))

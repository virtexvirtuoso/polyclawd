"""Wikipedia opinion polling scraper for election prediction markets.

Scrapes Wikipedia polling pages, compares to Polymarket prices,
and provides confidence multipliers for the paper trading system.
"""

from loguru import logger
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

import httpx
from bs4 import BeautifulSoup


# Wikipedia polling pages by country/year
POLLING_PAGES = {
    ("hungary", 2026): "Opinion_polling_for_the_2026_Hungarian_parliamentary_election",
    ("brazil", 2026): "Opinion_polling_for_the_2026_Brazilian_presidential_election",
}

# Known incumbents: (country keyword, incumbent name, party)
INCUMBENTS = {
    "hungary": {"name": "Orbán", "party": "Fidesz", "aliases": ["orban", "orbán", "fidesz"]},
    "brazil": {"name": "Lula", "party": "PT", "aliases": ["lula", "pt", "silva"]},
    "venezuela": {"name": "Maduro", "party": "PSUV", "aliases": ["maduro", "psuv"]},
}

# Market title keywords → country mapping
MARKET_COUNTRY_MAP = {
    "hungary": "hungary",
    "hungarian": "hungary",
    "orbán": "hungary",
    "orban": "hungary",
    "fidesz": "hungary",
    "tisza": "hungary",
    "magyar": "hungary",
    "brazil": "brazil",
    "brazilian": "brazil",
    "lula": "brazil",
    "bolsonaro": "brazil",
    "venezuela": "venezuela",
    "venezuelan": "venezuela",
    "maduro": "venezuela",
}

# Special cases — no real elections
NO_DEMOCRATIC_TRANSITION = {
    "venezuela": "No democratic transition likely — Maduro is authoritarian incumbent",
}


def _detect_country(market_title: str) -> Optional[str]:
    """Detect country from market title keywords."""
    title_lower = market_title.lower()
    for keyword, country in MARKET_COUNTRY_MAP.items():
        if keyword in title_lower:
            return country
    return None


def _detect_year(market_title: str) -> int:
    """Extract election year from market title, default 2026."""
    match = re.search(r'20[2-3]\d', market_title)
    return int(match.group()) if match else 2026


def _parse_percentage(text: str) -> Optional[float]:
    """Parse a percentage string like '47.1%' or '47.1' into a float."""
    if not text:
        return None
    text = text.strip().replace('%', '').replace('−', '-').replace('–', '-')
    try:
        return float(text)
    except (ValueError, TypeError):
        return None


def _parse_date(text: str) -> Optional[datetime]:
    """Try to parse a date string from Wikipedia polling tables."""
    text = text.strip()
    for fmt in ("%d %B %Y", "%B %d, %Y", "%d %b %Y", "%b %d, %Y",
                "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    # Try extracting just month+year
    match = re.search(r'(\w+ \d{4})', text)
    if match:
        for fmt in ("%B %Y", "%b %Y"):
            try:
                return datetime.strptime(match.group(1), fmt)
            except ValueError:
                continue
    return None


def _recency_weight(poll_date: Optional[datetime]) -> float:
    """Weight polls by recency: last 30d=1.0, 30-90d=0.7, >90d=0.4."""
    if not poll_date:
        return 0.4
    days_ago = (datetime.now() - poll_date).days
    if days_ago <= 30:
        return 1.0
    elif days_ago <= 90:
        return 0.7
    else:
        return 0.4


def get_polling_data(country: str, election_year: int) -> dict:
    """Scrape Wikipedia opinion polling page for a given election.
    
    Returns:
        dict with keys: country, year, polls (list), latest_polls (dict party->%),
                        scraped_at, error (if any)
    """
    country = country.lower()
    
    # Check for non-democratic cases
    if country in NO_DEMOCRATIC_TRANSITION:
        return {
            "country": country,
            "year": election_year,
            "polls": [],
            "latest_polls": {},
            "note": NO_DEMOCRATIC_TRANSITION[country],
            "scraped_at": datetime.now().isoformat(),
            "error": None,
        }
    
    page_key = (country, election_year)
    if page_key not in POLLING_PAGES:
        return {
            "country": country,
            "year": election_year,
            "polls": [],
            "latest_polls": {},
            "scraped_at": datetime.now().isoformat(),
            "error": f"No polling page configured for {country} {election_year}",
        }
    
    page_name = POLLING_PAGES[page_key]
    url = f"https://en.wikipedia.org/wiki/{page_name}"
    
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "Polyclawd/1.0 (prediction research)"})
            resp.raise_for_status()
    except Exception as e:
        logger.error("Failed to fetch Wikipedia polling page {}: {}", url, e)
        return {
            "country": country,
            "year": election_year,
            "polls": [],
            "latest_polls": {},
            "scraped_at": datetime.now().isoformat(),
            "error": str(e),
        }
    
    return _parse_polling_html(resp.text, country, election_year)


def _parse_polling_html(html: str, country: str, election_year: int) -> dict:
    """Parse polling tables from Wikipedia HTML."""
    soup = BeautifulSoup(html, "html.parser")
    polls = []
    
    # Find all wikitables
    tables = soup.find_all("table", class_="wikitable")
    
    for table in tables:
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        
        # Get header row to find party columns
        header_cells = rows[0].find_all(["th", "td"])
        headers = [cell.get_text(strip=True) for cell in header_cells]
        
        if len(headers) < 3:
            continue
        
        # Look for date-like column and percentage columns
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) < 3:
                continue
            
            cell_texts = [c.get_text(strip=True) for c in cells]
            
            # Try to find a date in the first few cells
            poll_date = None
            for ct in cell_texts[:3]:
                poll_date = _parse_date(ct)
                if poll_date:
                    break
            
            # Try to find percentages
            party_data = {}
            for i, (header, cell_text) in enumerate(zip(headers, cell_texts)):
                pct = _parse_percentage(cell_text)
                if pct is not None and 0 < pct < 100 and header:
                    party_data[header] = pct
            
            if party_data and len(party_data) >= 2:
                # Try to extract pollster (usually first or second cell)
                pollster = cell_texts[0] if cell_texts else "Unknown"
                polls.append({
                    "date": poll_date.isoformat() if poll_date else None,
                    "date_obj": poll_date,
                    "pollster": pollster,
                    "parties": party_data,
                    "recency_weight": _recency_weight(poll_date),
                })
    
    # Sort by date, most recent first
    polls.sort(key=lambda p: p.get("date") or "", reverse=True)
    
    # Build latest polls (weighted average of recent polls)
    latest_polls = {}
    if polls:
        total_weight = 0
        for poll in polls[:10]:  # Use up to 10 most recent
            w = poll["recency_weight"]
            total_weight += w
            for party, pct in poll["parties"].items():
                if party not in latest_polls:
                    latest_polls[party] = 0
                latest_polls[party] += pct * w
        
        if total_weight > 0:
            latest_polls = {k: round(v / total_weight, 1) for k, v in latest_polls.items()}
    
    # Clean up — remove date_obj (not serializable)
    for p in polls:
        p.pop("date_obj", None)
    
    return {
        "country": country,
        "year": election_year,
        "polls": polls[:20],  # Keep top 20
        "latest_polls": latest_polls,
        "poll_count": len(polls),
        "scraped_at": datetime.now().isoformat(),
        "error": None,
    }


def is_incumbent_favored(market_title: str) -> bool:
    """Check if the market is about an incumbent who has structural advantage."""
    country = _detect_country(market_title)
    if not country:
        return False
    
    if country in NO_DEMOCRATIC_TRANSITION:
        return True  # Authoritarian incumbents always "favored"
    
    return country in INCUMBENTS


def _get_incumbent_info(country: str) -> Optional[dict]:
    """Get incumbent info for a country."""
    return INCUMBENTS.get(country)


def poll_vs_market(market_title: str, market_price: float) -> dict:
    """Compare latest polls to Polymarket price for an election market.
    
    Args:
        market_title: The market question/title
        market_price: Current Polymarket YES price (0-1)
        
    Returns:
        dict with: confidence_multiplier, polling_data, divergence, reasoning
    """
    country = _detect_country(market_title)
    if not country:
        return {
            "confidence_multiplier": 1.0,
            "polling_data": None,
            "reasoning": "Could not detect country from market title",
        }
    
    # Venezuela special case
    if country in NO_DEMOCRATIC_TRANSITION:
        return {
            "confidence_multiplier": 1.2,
            "polling_data": {"note": NO_DEMOCRATIC_TRANSITION[country]},
            "reasoning": f"No democratic transition likely in {country} — incumbent strongly favored",
            "incumbency_boost": True,
        }
    
    year = _detect_year(market_title)
    polling_data = get_polling_data(country, year)
    
    if polling_data.get("error") or not polling_data.get("latest_polls"):
        # No polling data — neutral
        multiplier = 1.0
        if is_incumbent_favored(market_title):
            multiplier *= 1.15  # Incumbency advantage
        return {
            "confidence_multiplier": multiplier,
            "polling_data": polling_data,
            "reasoning": "No polling data available" + (" — incumbency boost applied" if multiplier > 1.0 else ""),
            "incumbency_boost": multiplier > 1.0,
        }
    
    # Determine if polls favor incumbent or challenger
    incumbent = _get_incumbent_info(country)
    if not incumbent:
        return {
            "confidence_multiplier": 1.0,
            "polling_data": polling_data,
            "reasoning": "No incumbent info configured",
        }
    
    # Find incumbent's poll % in latest polls
    incumbent_pct = None
    challenger_max_pct = 0
    latest = polling_data["latest_polls"]
    
    for party, pct in latest.items():
        party_lower = party.lower()
        is_incumbent_party = any(alias in party_lower for alias in incumbent["aliases"])
        if is_incumbent_party:
            incumbent_pct = pct
        else:
            challenger_max_pct = max(challenger_max_pct, pct)
    
    multiplier = 1.0
    reasoning_parts = []
    
    if incumbent_pct is not None:
        if incumbent_pct > challenger_max_pct:
            # Polls favor incumbent — if we're betting NO on challenger, boost
            multiplier *= 1.2
            reasoning_parts.append(f"Polls favor incumbent ({incumbent['name']}: {incumbent_pct}% vs challenger: {challenger_max_pct}%)")
        else:
            # Challenger leading in polls — reduce confidence in NO bet
            multiplier *= 0.7
            reasoning_parts.append(f"Challenger leading in polls ({challenger_max_pct}% vs {incumbent['name']}: {incumbent_pct}%)")
    
    # Incumbency advantage
    if is_incumbent_favored(market_title):
        multiplier *= 1.15
        reasoning_parts.append("Incumbency advantage applied (1.15x)")
    
    return {
        "confidence_multiplier": round(multiplier, 3),
        "polling_data": polling_data,
        "incumbent_pct": incumbent_pct,
        "challenger_max_pct": challenger_max_pct,
        "reasoning": "; ".join(reasoning_parts) if reasoning_parts else "Neutral",
        "incumbency_boost": is_incumbent_favored(market_title),
    }


def scan_election_markets(signals: list) -> list:
    """Enrich election-related signals with polling data.
    
    Scans signal titles for election keywords and adds polling_data field.
    """
    election_keywords = ["election", "president", "prime minister", "parliament",
                         "win", "elected", "vote", "ruling party"]
    # Also match known country/candidate keywords
    country_keywords = list(MARKET_COUNTRY_MAP.keys())
    
    enriched = []
    for signal in signals:
        title = signal.get("title", "") or signal.get("question", "") or ""
        title_lower = title.lower()
        
        is_election = (
            any(kw in title_lower for kw in election_keywords) and
            any(kw in title_lower for kw in country_keywords)
        )
        
        if is_election:
            price = signal.get("price", signal.get("yes_price", 0.5))
            if isinstance(price, str):
                try:
                    price = float(price)
                except (ValueError, TypeError):
                    price = 0.5
            
            poll_result = poll_vs_market(title, price)
            signal["polling_data"] = poll_result
            signal["election_signal"] = True
            logger.info("Election signal enriched: {} → multiplier={}",
                       title[:60], poll_result["confidence_multiplier"])
        else:
            signal["election_signal"] = False
        
        enriched.append(signal)
    
    return enriched

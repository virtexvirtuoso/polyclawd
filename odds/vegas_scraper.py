"""
VegasInsider Scraper for Soccer Futures
Scrapes odds from VegasInsider and compares with Polymarket
"""

import requests
import asyncio
from concurrent.futures import ThreadPoolExecutor
import re
from dataclasses import dataclass
from typing import Optional
import json
from datetime import datetime, timedelta
import os

CACHE_FILE = os.path.join(os.path.dirname(__file__), "vegas_cache.json")
CACHE_TTL_HOURS = 12  # Refresh every 12 hours

@dataclass
class VegasOdds:
    team: str
    american_odds: int
    implied_prob: float
    source: str
    league: str
    updated: str

def american_to_prob(odds: int) -> float:
    """Convert American odds to implied probability"""
    if odds < 0:
        return abs(odds) / (abs(odds) + 100)
    else:
        return 100 / (odds + 100)

def _scrape_vegasinsider_sync() -> dict:
    """Synchronous scrape of VegasInsider"""
    results = {
        "epl": [],
        "ucl": [],
        "world_cup": [],
        "bundesliga": [],
        "la_liga": []
    }
    
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    
    # Main futures page
    try:
        resp = requests.get(
            "https://www.vegasinsider.com/soccer/odds/futures/",
            headers=headers,
            timeout=30
        )
        if resp.status_code == 200:
            text = resp.text
            now = datetime.utcnow().isoformat()
            
            # Parse EPL odds
            epl_section = re.search(r'Premier League.*?(?=Bundesliga|La Liga|Serie A|World Cup|$)', text, re.DOTALL | re.IGNORECASE)
            if epl_section:
                odds_matches = re.findall(r'([A-Za-z\s]+)\s*\[([+-]\d+)\]', epl_section.group())
                for team, odds in odds_matches[:10]:
                    team = team.strip()
                    if team and len(team) > 2:
                        american = int(odds)
                        results["epl"].append(VegasOdds(
                            team=team,
                            american_odds=american,
                            implied_prob=american_to_prob(american),
                            source="VegasInsider",
                            league="EPL",
                            updated=now
                        ))
            
            # Parse World Cup odds
            wc_section = re.search(r'World Cup.*?(?=Premier|Bundesliga|La Liga|Serie A|Champions|$)', text, re.DOTALL | re.IGNORECASE)
            if wc_section:
                odds_matches = re.findall(r'([A-Za-z\s]+)\s*\[([+-]\d+)\]', wc_section.group())
                for team, odds in odds_matches[:15]:
                    team = team.strip()
                    if team and len(team) > 2:
                        american = int(odds)
                        results["world_cup"].append(VegasOdds(
                            team=team,
                            american_odds=american,
                            implied_prob=american_to_prob(american),
                            source="VegasInsider",
                            league="World Cup 2026",
                            updated=now
                        ))
                        
    except Exception as e:
        print(f"Error scraping VegasInsider futures: {e}")
    
    # UCL specific page
    try:
        resp = requests.get(
            "https://www.vegasinsider.com/soccer/odds/champions-league/",
            headers=headers,
            timeout=30
        )
        if resp.status_code == 200:
            text = resp.text
            now = datetime.utcnow().isoformat()
            
            # Look for current odds section
            odds_matches = re.findall(r'([A-Za-z\s]+)\s*\[([+-]\d+)\]', text)
            ucl_teams = []
            for team, odds in odds_matches:
                team = team.strip()
                # Filter to known UCL teams
                if any(t in team for t in ['Arsenal', 'Bayern', 'City', 'Barcelona', 'PSG', 
                                            'Liverpool', 'Madrid', 'Chelsea', 'Inter', 
                                            'Tottenham', 'Newcastle', 'Atletico', 'Dortmund']):
                    american = int(odds)
                    ucl_teams.append(VegasOdds(
                        team=team,
                        american_odds=american,
                        implied_prob=american_to_prob(american),
                        source="VegasInsider",
                        league="UCL",
                        updated=now
                    ))
            
            if ucl_teams:
                results["ucl"] = ucl_teams[:12]
                
    except Exception as e:
        print(f"Error scraping UCL page: {e}")
    
    return results

async def scrape_vegasinsider_soccer() -> dict[str, list[VegasOdds]]:
    """Async wrapper for VegasInsider scrape"""
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as executor:
        return await loop.run_in_executor(executor, _scrape_vegasinsider_sync)

def load_cache() -> Optional[dict]:
    """Load cached odds if still valid"""
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, 'r') as f:
                cache = json.load(f)
            cached_time = datetime.fromisoformat(cache.get("timestamp", "2000-01-01"))
            if datetime.utcnow() - cached_time < timedelta(hours=CACHE_TTL_HOURS):
                return cache.get("data")
    except Exception:
        pass
    return None

def save_cache(data: dict):
    """Save odds to cache"""
    try:
        cache = {
            "timestamp": datetime.utcnow().isoformat(),
            "data": data
        }
        with open(CACHE_FILE, 'w') as f:
            json.dump(cache, f, default=lambda x: x.__dict__ if hasattr(x, '__dict__') else str(x))
    except Exception as e:
        print(f"Error saving cache: {e}")

async def get_vegas_odds(force_refresh: bool = False) -> dict[str, list[VegasOdds]]:
    """Get Vegas odds, using cache if available"""
    if not force_refresh:
        cached = load_cache()
        if cached:
            # Convert back to VegasOdds objects
            result = {}
            for league, odds_list in cached.items():
                result[league] = [
                    VegasOdds(**o) if isinstance(o, dict) else o 
                    for o in odds_list
                ]
            return result
    
    # Scrape fresh data
    data = await scrape_vegasinsider_soccer()
    save_cache(data)
    return data

# Hardcoded fallback data (from VegasInsider/BetMGM Feb 7, 2026)
FALLBACK_ODDS = {
    "epl": [
        VegasOdds("Arsenal", -550, 0.846, "BetMGM", "EPL", "2026-02-07"),
        VegasOdds("Manchester City", 450, 0.182, "BetMGM", "EPL", "2026-02-07"),
        VegasOdds("Aston Villa", 3300, 0.029, "BetMGM", "EPL", "2026-02-07"),
        VegasOdds("Manchester United", 5000, 0.020, "BetMGM", "EPL", "2026-02-07"),
        VegasOdds("Liverpool", 10000, 0.010, "BetMGM", "EPL", "2026-02-07"),
    ],
    "ucl": [
        VegasOdds("Arsenal", 350, 0.222, "VegasInsider", "UCL", "2026-02-07"),
        VegasOdds("Bayern Munich", 450, 0.182, "VegasInsider", "UCL", "2026-02-07"),
        VegasOdds("Manchester City", 700, 0.125, "VegasInsider", "UCL", "2026-02-07"),
        VegasOdds("Barcelona", 700, 0.125, "VegasInsider", "UCL", "2026-02-07"),
        VegasOdds("Paris Saint-Germain", 800, 0.111, "VegasInsider", "UCL", "2026-02-07"),
        VegasOdds("Liverpool", 1000, 0.091, "VegasInsider", "UCL", "2026-02-07"),
        VegasOdds("Real Madrid", 1200, 0.077, "VegasInsider", "UCL", "2026-02-07"),
        VegasOdds("Chelsea", 1800, 0.053, "VegasInsider", "UCL", "2026-02-07"),
        VegasOdds("Inter Milan", 2800, 0.034, "VegasInsider", "UCL", "2026-02-07"),
        VegasOdds("Tottenham", 3300, 0.029, "VegasInsider", "UCL", "2026-02-07"),
    ],
    "world_cup": [
        VegasOdds("Spain", 550, 0.154, "VegasInsider", "World Cup 2026", "2026-02-07"),
        VegasOdds("Brazil", 600, 0.143, "VegasInsider", "World Cup 2026", "2026-02-07"),
        VegasOdds("France", 650, 0.133, "VegasInsider", "World Cup 2026", "2026-02-07"),
        VegasOdds("England", 700, 0.125, "VegasInsider", "World Cup 2026", "2026-02-07"),
        VegasOdds("Argentina", 800, 0.111, "VegasInsider", "World Cup 2026", "2026-02-07"),
        VegasOdds("Germany", 1200, 0.077, "VegasInsider", "World Cup 2026", "2026-02-07"),
    ],
    "bundesliga": [
        VegasOdds("Bayern Munich", -324, 0.764, "VegasInsider", "Bundesliga", "2026-02-07"),
        VegasOdds("Bayer Leverkusen", 700, 0.125, "VegasInsider", "Bundesliga", "2026-02-07"),
        VegasOdds("Borussia Dortmund", 1000, 0.091, "VegasInsider", "Bundesliga", "2026-02-07"),
    ],
    "la_liga": [
        VegasOdds("Real Madrid", -138, 0.58, "VegasInsider", "La Liga", "2026-02-07"),
        VegasOdds("Barcelona", 120, 0.455, "VegasInsider", "La Liga", "2026-02-07"),
        VegasOdds("Atletico Madrid", 800, 0.111, "VegasInsider", "La Liga", "2026-02-07"),
    ],
}

async def get_vegas_odds_with_fallback() -> dict[str, list[VegasOdds]]:
    """Get Vegas odds with fallback to hardcoded data"""
    try:
        data = await get_vegas_odds()
        # Check if we got meaningful data
        if data and any(len(v) > 0 for v in data.values()):
            return data
    except Exception as e:
        print(f"Error getting Vegas odds: {e}")
    
    return FALLBACK_ODDS


if __name__ == "__main__":
    async def test():
        print("Fetching Vegas odds...")
        odds = await get_vegas_odds_with_fallback()
        
        for league, teams in odds.items():
            print(f"\n=== {league.upper()} ===")
            for o in teams[:5]:
                print(f"  {o.team}: {o.american_odds:+d} ({o.implied_prob:.1%})")
    
    asyncio.run(test())

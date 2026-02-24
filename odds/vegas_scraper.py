"""
VegasInsider Scraper for Soccer & NFL Futures
Scrapes odds from VegasInsider and compares with Polymarket
"""

import requests
import asyncio

# Resilient fetch wrapper
try:
    from api.services.resilient_fetch import resilient_call
    HAS_RESILIENT = True
except ImportError:
    HAS_RESILIENT = False
from concurrent.futures import ThreadPoolExecutor
import re
from dataclasses import dataclass
from typing import Optional, Dict, List
import json
from datetime import datetime, timedelta
import os

CACHE_FILE = os.path.join(os.path.dirname(__file__), "vegas_cache.json")
NFL_CACHE_FILE = os.path.join(os.path.dirname(__file__), "vegas_nfl_cache.json")
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
        def _fetch_soccer():
            r = requests.get("https://www.vegasinsider.com/soccer/odds/futures/", headers=headers, timeout=30)
            if r.status_code != 200:
                raise RuntimeError(f"VegasInsider soccer returned {r.status_code}")
            return r
        resp = resilient_call("vegas", _fetch_soccer, retries=2, backoff_base=2.0) if HAS_RESILIENT else requests.get("https://www.vegasinsider.com/soccer/odds/futures/", headers=headers, timeout=30)
        if resp and resp.status_code == 200:
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

# ============================================================================
# NFL Futures Scraping
# ============================================================================

# NFL team name normalization
NFL_TEAMS = {
    "seahawks": "Seattle Seahawks",
    "patriots": "New England Patriots",
    "rams": "Los Angeles Rams",
    "bills": "Buffalo Bills",
    "ravens": "Baltimore Ravens",
    "eagles": "Philadelphia Eagles",
    "chargers": "Los Angeles Chargers",
    "lions": "Detroit Lions",
    "packers": "Green Bay Packers",
    "49ers": "San Francisco 49ers",
    "chiefs": "Kansas City Chiefs",
    "cowboys": "Dallas Cowboys",
    "dolphins": "Miami Dolphins",
    "jets": "New York Jets",
    "broncos": "Denver Broncos",
    "bengals": "Cincinnati Bengals",
    "steelers": "Pittsburgh Steelers",
    "browns": "Cleveland Browns",
    "texans": "Houston Texans",
    "colts": "Indianapolis Colts",
    "jaguars": "Jacksonville Jaguars",
    "titans": "Tennessee Titans",
    "raiders": "Las Vegas Raiders",
    "bears": "Chicago Bears",
    "vikings": "Minnesota Vikings",
    "saints": "New Orleans Saints",
    "falcons": "Atlanta Falcons",
    "buccaneers": "Tampa Bay Buccaneers",
    "panthers": "Carolina Panthers",
    "cardinals": "Arizona Cardinals",
    "giants": "New York Giants",
    "commanders": "Washington Commanders",
}


def _normalize_nfl_team(name: str) -> str:
    """Normalize NFL team name to full name"""
    name_lower = name.lower().strip()
    for short, full in NFL_TEAMS.items():
        if short in name_lower or full.lower() in name_lower:
            return full
    return name.strip()


def _scrape_vegasinsider_nfl_sync() -> Dict[str, List[VegasOdds]]:
    """Synchronous scrape of VegasInsider NFL Futures"""
    results = {
        "super_bowl": [],
        "afc_winner": [],
        "nfc_winner": [],
    }
    
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    now = datetime.utcnow().isoformat()
    
    # Super Bowl futures page
    try:
        resp = requests.get(
            "https://www.vegasinsider.com/nfl/odds/futures/",
            headers=headers,
            timeout=30
        )
        if resp.status_code == 200:
            text = resp.text
            
            # Parse odds patterns like "+900" or "-235" with team names
            # Looking for patterns like "Seahawks" followed by odds
            team_odds_pattern = r'\[(/nfl/teams/([a-z]+)/)\].*?\[([+-]\d+)'
            
            # Simpler pattern - look for team references and nearby odds
            matches = re.findall(r'/nfl/teams/([a-z]+)/.*?\[([+-]\d+)', text, re.IGNORECASE)
            
            seen_teams = set()
            for team_slug, odds in matches[:30]:  # Limit to top 30
                team_name = _normalize_nfl_team(team_slug)
                if team_name in seen_teams:
                    continue
                seen_teams.add(team_name)
                
                try:
                    american = int(odds)
                    results["super_bowl"].append(VegasOdds(
                        team=team_name,
                        american_odds=american,
                        implied_prob=american_to_prob(american),
                        source="VegasInsider",
                        league="NFL Super Bowl",
                        updated=now
                    ))
                except ValueError:
                    pass
                    
    except Exception as e:
        print(f"Error scraping VegasInsider NFL futures: {e}")
    
    # AFC Championship page
    try:
        resp = requests.get(
            "https://www.vegasinsider.com/nfl/odds/afc-championship/",
            headers=headers,
            timeout=30
        )
        if resp.status_code == 200:
            text = resp.text
            matches = re.findall(r'/nfl/teams/([a-z]+)/.*?\[([+-]\d+)', text, re.IGNORECASE)
            
            seen_teams = set()
            for team_slug, odds in matches[:16]:
                team_name = _normalize_nfl_team(team_slug)
                if team_name in seen_teams:
                    continue
                seen_teams.add(team_name)
                
                try:
                    american = int(odds)
                    results["afc_winner"].append(VegasOdds(
                        team=team_name,
                        american_odds=american,
                        implied_prob=american_to_prob(american),
                        source="VegasInsider",
                        league="NFL AFC",
                        updated=now
                    ))
                except ValueError:
                    pass
                    
    except Exception as e:
        print(f"Error scraping VegasInsider AFC odds: {e}")
    
    # NFC Championship page
    try:
        resp = requests.get(
            "https://www.vegasinsider.com/nfl/odds/nfc-championship/",
            headers=headers,
            timeout=30
        )
        if resp.status_code == 200:
            text = resp.text
            matches = re.findall(r'/nfl/teams/([a-z]+)/.*?\[([+-]\d+)', text, re.IGNORECASE)
            
            seen_teams = set()
            for team_slug, odds in matches[:16]:
                team_name = _normalize_nfl_team(team_slug)
                if team_name in seen_teams:
                    continue
                seen_teams.add(team_name)
                
                try:
                    american = int(odds)
                    results["nfc_winner"].append(VegasOdds(
                        team=team_name,
                        american_odds=american,
                        implied_prob=american_to_prob(american),
                        source="VegasInsider",
                        league="NFL NFC",
                        updated=now
                    ))
                except ValueError:
                    pass
                    
    except Exception as e:
        print(f"Error scraping VegasInsider NFC odds: {e}")
    
    return results


async def scrape_vegasinsider_nfl() -> Dict[str, List[VegasOdds]]:
    """Async wrapper for VegasInsider NFL scrape"""
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as executor:
        return await loop.run_in_executor(executor, _scrape_vegasinsider_nfl_sync)


def load_nfl_cache() -> Optional[dict]:
    """Load cached NFL odds if still valid"""
    try:
        if os.path.exists(NFL_CACHE_FILE):
            with open(NFL_CACHE_FILE, 'r') as f:
                cache = json.load(f)
            cached_time = datetime.fromisoformat(cache.get("timestamp", "2000-01-01"))
            if datetime.utcnow() - cached_time < timedelta(hours=CACHE_TTL_HOURS):
                return cache.get("data")
    except Exception:
        pass
    return None


def save_nfl_cache(data: dict):
    """Save NFL odds to cache"""
    try:
        cache = {
            "timestamp": datetime.utcnow().isoformat(),
            "data": data
        }
        with open(NFL_CACHE_FILE, 'w') as f:
            json.dump(cache, f, default=lambda x: x.__dict__ if hasattr(x, '__dict__') else str(x))
    except Exception as e:
        print(f"Error saving NFL cache: {e}")


async def get_nfl_vegas_odds(force_refresh: bool = False) -> Dict[str, List[VegasOdds]]:
    """Get NFL Vegas odds, using cache if available"""
    if not force_refresh:
        cached = load_nfl_cache()
        if cached:
            result = {}
            for league, odds_list in cached.items():
                result[league] = [
                    VegasOdds(**o) if isinstance(o, dict) else o 
                    for o in odds_list
                ]
            return result
    
    # Scrape fresh data
    data = await scrape_vegasinsider_nfl()
    save_nfl_cache(data)
    return data


# Hardcoded NFL fallback data (from VegasInsider Feb 8, 2026)
NFL_FALLBACK_ODDS = {
    "super_bowl": [
        VegasOdds("Seattle Seahawks", -235, 0.701, "VegasInsider", "NFL Super Bowl", "2026-02-08"),
        VegasOdds("New England Patriots", 195, 0.339, "VegasInsider", "NFL Super Bowl", "2026-02-08"),
        VegasOdds("Los Angeles Rams", 900, 0.100, "VegasInsider", "NFL Super Bowl", "2026-02-08"),
        VegasOdds("Buffalo Bills", 1100, 0.083, "VegasInsider", "NFL Super Bowl", "2026-02-08"),
        VegasOdds("Baltimore Ravens", 1200, 0.077, "VegasInsider", "NFL Super Bowl", "2026-02-08"),
        VegasOdds("Philadelphia Eagles", 1200, 0.077, "VegasInsider", "NFL Super Bowl", "2026-02-08"),
        VegasOdds("Los Angeles Chargers", 1500, 0.063, "VegasInsider", "NFL Super Bowl", "2026-02-08"),
        VegasOdds("Detroit Lions", 1400, 0.067, "VegasInsider", "NFL Super Bowl", "2026-02-08"),
        VegasOdds("Green Bay Packers", 1400, 0.067, "VegasInsider", "NFL Super Bowl", "2026-02-08"),
    ],
    "afc_winner": [
        VegasOdds("New England Patriots", -150, 0.600, "VegasInsider", "NFL AFC", "2026-02-08"),
        VegasOdds("Buffalo Bills", 400, 0.200, "VegasInsider", "NFL AFC", "2026-02-08"),
        VegasOdds("Baltimore Ravens", 600, 0.143, "VegasInsider", "NFL AFC", "2026-02-08"),
        VegasOdds("Kansas City Chiefs", 800, 0.111, "VegasInsider", "NFL AFC", "2026-02-08"),
        VegasOdds("Cincinnati Bengals", 1200, 0.077, "VegasInsider", "NFL AFC", "2026-02-08"),
    ],
    "nfc_winner": [
        VegasOdds("Seattle Seahawks", -300, 0.750, "VegasInsider", "NFL NFC", "2026-02-08"),
        VegasOdds("Los Angeles Rams", 500, 0.167, "VegasInsider", "NFL NFC", "2026-02-08"),
        VegasOdds("Philadelphia Eagles", 800, 0.111, "VegasInsider", "NFL NFC", "2026-02-08"),
        VegasOdds("Detroit Lions", 1000, 0.091, "VegasInsider", "NFL NFC", "2026-02-08"),
        VegasOdds("Green Bay Packers", 1200, 0.077, "VegasInsider", "NFL NFC", "2026-02-08"),
    ],
}


async def get_nfl_odds_with_fallback() -> Dict[str, List[VegasOdds]]:
    """Get NFL Vegas odds with fallback to hardcoded data"""
    try:
        data = await get_nfl_vegas_odds()
        if data and any(len(v) > 0 for v in data.values()):
            return data
    except Exception as e:
        print(f"Error getting NFL Vegas odds: {e}")
    
    return NFL_FALLBACK_ODDS


# ============================================================================
# Soccer Fallback Data
# ============================================================================

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


# ============================================================================
# NBA Futures Scraping
# ============================================================================

NBA_TEAMS = {
    "celtics": "Boston Celtics",
    "nets": "Brooklyn Nets",
    "knicks": "New York Knicks",
    "76ers": "Philadelphia 76ers",
    "raptors": "Toronto Raptors",
    "bulls": "Chicago Bulls",
    "cavaliers": "Cleveland Cavaliers",
    "pistons": "Detroit Pistons",
    "pacers": "Indiana Pacers",
    "bucks": "Milwaukee Bucks",
    "hawks": "Atlanta Hawks",
    "hornets": "Charlotte Hornets",
    "heat": "Miami Heat",
    "magic": "Orlando Magic",
    "wizards": "Washington Wizards",
    "nuggets": "Denver Nuggets",
    "timberwolves": "Minnesota Timberwolves",
    "thunder": "Oklahoma City Thunder",
    "blazers": "Portland Trail Blazers",
    "jazz": "Utah Jazz",
    "warriors": "Golden State Warriors",
    "clippers": "Los Angeles Clippers",
    "lakers": "Los Angeles Lakers",
    "suns": "Phoenix Suns",
    "kings": "Sacramento Kings",
    "mavericks": "Dallas Mavericks",
    "rockets": "Houston Rockets",
    "grizzlies": "Memphis Grizzlies",
    "pelicans": "New Orleans Pelicans",
    "spurs": "San Antonio Spurs",
}


def _normalize_nba_team(name: str) -> str:
    """Normalize NBA team name to full name"""
    name_lower = name.lower().strip()
    for short, full in NBA_TEAMS.items():
        if short in name_lower or full.lower() in name_lower:
            return full
    return name.strip()


def _scrape_vegasinsider_nba_sync() -> Dict[str, List[VegasOdds]]:
    """Synchronous scrape of VegasInsider NBA Futures"""
    results = {
        "nba_champion": [],
        "eastern_conf": [],
        "western_conf": [],
    }
    
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    now = datetime.utcnow().isoformat()
    
    # NBA Championship page
    try:
        resp = requests.get(
            "https://www.vegasinsider.com/nba/odds/futures/",
            headers=headers,
            timeout=30
        )
        if resp.status_code == 200:
            text = resp.text
            matches = re.findall(r'/nba/teams/([a-z-]+)/.*?\[([+-]\d+)', text, re.IGNORECASE)
            
            seen_teams = set()
            for team_slug, odds in matches[:30]:
                team_name = _normalize_nba_team(team_slug.replace("-", " "))
                if team_name in seen_teams:
                    continue
                seen_teams.add(team_name)
                
                try:
                    american = int(odds)
                    results["nba_champion"].append(VegasOdds(
                        team=team_name,
                        american_odds=american,
                        implied_prob=american_to_prob(american),
                        source="VegasInsider",
                        league="NBA Championship",
                        updated=now
                    ))
                except ValueError:
                    pass
                    
    except Exception as e:
        print(f"Error scraping VegasInsider NBA futures: {e}")
    
    return results


async def scrape_vegasinsider_nba() -> Dict[str, List[VegasOdds]]:
    """Async wrapper for VegasInsider NBA scrape"""
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as executor:
        return await loop.run_in_executor(executor, _scrape_vegasinsider_nba_sync)


# NBA Fallback data
NBA_FALLBACK_ODDS = {
    "nba_champion": [
        VegasOdds("Boston Celtics", 280, 0.263, "VegasInsider", "NBA Championship", "2026-02-08"),
        VegasOdds("Oklahoma City Thunder", 350, 0.222, "VegasInsider", "NBA Championship", "2026-02-08"),
        VegasOdds("Denver Nuggets", 600, 0.143, "VegasInsider", "NBA Championship", "2026-02-08"),
        VegasOdds("Cleveland Cavaliers", 700, 0.125, "VegasInsider", "NBA Championship", "2026-02-08"),
        VegasOdds("Milwaukee Bucks", 900, 0.100, "VegasInsider", "NBA Championship", "2026-02-08"),
        VegasOdds("Los Angeles Lakers", 1200, 0.077, "VegasInsider", "NBA Championship", "2026-02-08"),
        VegasOdds("Phoenix Suns", 1500, 0.063, "VegasInsider", "NBA Championship", "2026-02-08"),
        VegasOdds("Golden State Warriors", 2000, 0.048, "VegasInsider", "NBA Championship", "2026-02-08"),
    ],
}


async def get_nba_vegas_odds(force_refresh: bool = False) -> Dict[str, List[VegasOdds]]:
    """Get NBA Vegas odds with fallback"""
    try:
        data = await scrape_vegasinsider_nba()
        if data and any(len(v) > 0 for v in data.values()):
            return data
    except Exception as e:
        print(f"Error getting NBA Vegas odds: {e}")
    return NBA_FALLBACK_ODDS


# ============================================================================
# MLB Futures Scraping  
# ============================================================================

MLB_TEAMS = {
    "yankees": "New York Yankees",
    "red-sox": "Boston Red Sox",
    "dodgers": "Los Angeles Dodgers",
    "braves": "Atlanta Braves",
    "astros": "Houston Astros",
    "phillies": "Philadelphia Phillies",
    "padres": "San Diego Padres",
    "mets": "New York Mets",
    "cubs": "Chicago Cubs",
    "cardinals": "St. Louis Cardinals",
}


def _scrape_vegasinsider_mlb_sync() -> Dict[str, List[VegasOdds]]:
    """Synchronous scrape of VegasInsider MLB Futures"""
    results = {"world_series": []}
    
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    now = datetime.utcnow().isoformat()
    
    try:
        resp = requests.get(
            "https://www.vegasinsider.com/mlb/odds/futures/",
            headers=headers,
            timeout=30
        )
        if resp.status_code == 200:
            text = resp.text
            matches = re.findall(r'/mlb/teams/([a-z-]+)/.*?\[([+-]\d+)', text, re.IGNORECASE)
            
            seen_teams = set()
            for team_slug, odds in matches[:30]:
                team_name = team_slug.replace("-", " ").title()
                if team_name in seen_teams:
                    continue
                seen_teams.add(team_name)
                
                try:
                    american = int(odds)
                    results["world_series"].append(VegasOdds(
                        team=team_name,
                        american_odds=american,
                        implied_prob=american_to_prob(american),
                        source="VegasInsider",
                        league="MLB World Series",
                        updated=now
                    ))
                except ValueError:
                    pass
                    
    except Exception as e:
        print(f"Error scraping VegasInsider MLB futures: {e}")
    
    return results


async def scrape_vegasinsider_mlb() -> Dict[str, List[VegasOdds]]:
    """Async wrapper for VegasInsider MLB scrape"""
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as executor:
        return await loop.run_in_executor(executor, _scrape_vegasinsider_mlb_sync)


# ============================================================================
# NHL Futures Scraping
# ============================================================================

def _scrape_vegasinsider_nhl_sync() -> Dict[str, List[VegasOdds]]:
    """Synchronous scrape of VegasInsider NHL Futures"""
    results = {"stanley_cup": []}
    
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    now = datetime.utcnow().isoformat()
    
    try:
        resp = requests.get(
            "https://www.vegasinsider.com/nhl/odds/futures/",
            headers=headers,
            timeout=30
        )
        if resp.status_code == 200:
            text = resp.text
            matches = re.findall(r'/nhl/teams/([a-z-]+)/.*?\[([+-]\d+)', text, re.IGNORECASE)
            
            seen_teams = set()
            for team_slug, odds in matches[:30]:
                team_name = team_slug.replace("-", " ").title()
                if team_name in seen_teams:
                    continue
                seen_teams.add(team_name)
                
                try:
                    american = int(odds)
                    results["stanley_cup"].append(VegasOdds(
                        team=team_name,
                        american_odds=american,
                        implied_prob=american_to_prob(american),
                        source="VegasInsider",
                        league="NHL Stanley Cup",
                        updated=now
                    ))
                except ValueError:
                    pass
                    
    except Exception as e:
        print(f"Error scraping VegasInsider NHL futures: {e}")
    
    return results


async def scrape_vegasinsider_nhl() -> Dict[str, List[VegasOdds]]:
    """Async wrapper for VegasInsider NHL scrape"""
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as executor:
        return await loop.run_in_executor(executor, _scrape_vegasinsider_nhl_sync)


async def get_all_vegas_futures() -> Dict:
    """
    Get all Vegas futures across sports.
    
    Returns combined dict with all leagues.
    """
    soccer = await get_vegas_odds_with_fallback()
    nfl = await get_nfl_odds_with_fallback()
    nba = await get_nba_vegas_odds()
    
    return {
        "soccer": soccer,
        "nfl": nfl,
        "nba": nba,
        "timestamp": datetime.utcnow().isoformat(),
    }


if __name__ == "__main__":
    async def test():
        print("Fetching Vegas odds...")
        odds = await get_vegas_odds_with_fallback()
        
        for league, teams in odds.items():
            print(f"\n=== {league.upper()} ===")
            for o in teams[:5]:
                print(f"  {o.team}: {o.american_odds:+d} ({o.implied_prob:.1%})")
        
        print("\n" + "="*50)
        print("\nFetching NBA futures...")
        nba = await get_nba_vegas_odds()
        for team in nba.get("nba_champion", [])[:5]:
            print(f"  {team.team}: {team.american_odds:+d}")
    
    asyncio.run(test())

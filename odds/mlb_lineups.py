"""
MLB Stats API lineup fetcher — official API, free, no key required.

Used to gate player prop signals so we only signal on confirmed starters.
Lineups are posted ~60-90 min before first pitch at statsapi.mlb.com.

API: https://statsapi.mlb.com/api/v1/schedule?sportId=1&hydrate=lineups,probablePitcher&date=YYYY-MM-DD
"""

import json
import time
import urllib.request
from datetime import date
from typing import Dict, List, Optional

from loguru import logger

MLB_STATS_API = "https://statsapi.mlb.com/api/v1"
CACHE_TTL = 300  # 5 minutes

# {date_str: {"data": [game_dicts], "fetched_at": float}}
_lineup_cache: Dict[str, Dict] = {}


def _fetch_schedule_sync(date_str: str) -> List[Dict]:
    """Fetch MLB schedule with lineups. Returns [] on error."""
    url = (
        f"{MLB_STATS_API}/schedule"
        f"?sportId=1&hydrate=lineups,probablePitcher&date={date_str}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        games: List[Dict] = []
        for date_entry in data.get("dates", []):
            games.extend(date_entry.get("games", []))
        return games
    except Exception as e:
        logger.warning(f"mlb_lineups: schedule fetch failed for {date_str}: {e}")
        return []


def get_scheduled_games(date_str: Optional[str] = None) -> List[Dict]:
    """
    Return MLB games for a date (default: today UTC). Cached for CACHE_TTL seconds.
    """
    if date_str is None:
        date_str = date.today().isoformat()

    cached = _lineup_cache.get(date_str)
    if cached and time.time() - cached["fetched_at"] < CACHE_TTL:
        return cached["data"]

    games = _fetch_schedule_sync(date_str)
    _lineup_cache[date_str] = {"data": games, "fetched_at": time.time()}
    return games


def get_starting_lineup(game_pk: int, date_str: Optional[str] = None) -> Dict:
    """
    Return starting lineup for a game.

    Returns:
        {
            "home": {"batting_order": ["Player Name", ...], "starting_pitcher": "Name"},
            "away": {"batting_order": ["Player Name", ...], "starting_pitcher": "Name"},
        }
    Returns {} if the game is not found or lineups are not yet posted.
    """
    for game in get_scheduled_games(date_str):
        if game.get("gamePk") != game_pk:
            continue

        result: Dict = {}
        lineups = game.get("lineups", {})
        for side in ("home", "away"):
            players = lineups.get(f"{side}Players", [])
            batting_order = [
                p.get("person", {}).get("fullName", "")
                for p in players
                if p.get("person", {}).get("fullName")
            ]
            probable = (
                game.get("teams", {})
                .get(side, {})
                .get("probablePitcher", {})
            )
            result[side] = {
                "batting_order": batting_order,
                "starting_pitcher": probable.get("fullName", ""),
            }
        return result

    return {}


def is_player_starting(
    player_name: str, game_pk: int, date_str: Optional[str] = None
) -> bool:
    """
    True if player is in the confirmed starting lineup.
    Returns False (conservatively suppress) if lineups not yet posted.
    """
    lineup = get_starting_lineup(game_pk, date_str)
    if not lineup:
        return False

    player_lower = player_name.lower()
    for side_data in lineup.values():
        if any(n.lower() == player_lower for n in side_data.get("batting_order", [])):
            return True
        if side_data.get("starting_pitcher", "").lower() == player_lower:
            return True

    return False


def get_game_pk_for_teams(
    home_team: str, away_team: str, date_str: Optional[str] = None
) -> Optional[int]:
    """Find gamePk for a home/away matchup. Returns None if not found."""
    for game in get_scheduled_games(date_str):
        teams = game.get("teams", {})
        h = teams.get("home", {}).get("team", {}).get("name", "")
        a = teams.get("away", {}).get("team", {}).get("name", "")
        if home_team.lower() in h.lower() and away_team.lower() in a.lower():
            return game.get("gamePk")
    return None


if __name__ == "__main__":
    today = date.today().isoformat()
    print(f"Fetching MLB schedule for {today}...")
    games = get_scheduled_games(today)
    print(f"Found {len(games)} games")
    for g in games[:5]:
        teams = g.get("teams", {})
        home = teams.get("home", {}).get("team", {}).get("name", "")
        away = teams.get("away", {}).get("team", {}).get("name", "")
        gp = g.get("gamePk")
        print(f"  gamePk={gp}: {away} @ {home}")

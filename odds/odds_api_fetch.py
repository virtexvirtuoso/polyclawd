"""
Generic The Odds API v4 fetch helpers for the new sports edge engines.

Kept as a NEW module (rather than editing odds/the_odds_api.py) so the soccer/UFC
work doesn't collide with in-flight changes there. Mirrors the_odds_api.py's
patterns: ODDS_API_KEY from env, resilient fetch + credit-header tracking when
available, graceful [] / {} on missing key or error.

Sharp-book reference: every call defaults to bookmakers="pinnacle", which The
Odds API treats as overriding `regions` and bills as a single credit unit. If a
sport doesn't carry Pinnacle (verify via the Phase-0 probe), pass a fallback
sharp set or widen regions explicitly.

Endpoints used:
  GET /v4/sports/{key}/odds                        — featured markets (h2h/spreads/totals/outrights)
  GET /v4/sports/{key}/events/{id}/odds            — per-event props/alternates
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List

import asyncio

from loguru import logger

try:
    from api.services.resilient_fetch import _resilient_urlopen

    HAS_RESILIENT = True
except ImportError:  # pragma: no cover
    HAS_RESILIENT = False
    _resilient_urlopen = None

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SOURCE_NAME = "the_odds_api"

# Sport keys (verified shapes; live availability confirmed by the Phase-0 probe).
SOCCER_MATCH_KEYS: Dict[str, str] = {
    "worldcup": "soccer_fifa_world_cup",  # per-match h2h (3-way) during the tournament
    "epl": "soccer_epl",
    "ucl": "soccer_uefa_champs_league",
    "laliga": "soccer_spain_la_liga",
    "bundesliga": "soccer_germany_bundesliga",
    "mls": "soccer_usa_mls",
}
# Outrights live under a DEDICATED *_winner key — NOT the match key. (api-expert BLOCKER fix)
SOCCER_OUTRIGHT_KEYS: Dict[str, str] = {
    "worldcup": "soccer_fifa_world_cup_winner",
}
MMA_SPORT_KEYS: Dict[str, str] = {"ufc": "mma_mixed_martial_arts"}


def _get_api_key():
    return os.getenv("ODDS_API_KEY") or None


def upcoming_window(hours: int) -> tuple:
    """(commenceTimeFrom, commenceTimeTo) ISO8601 strings for the next `hours`.
    Used to pull only soon-to-start games — smaller payloads, fewer matches to
    enrich, and avoids re-pulling the whole season's future fixtures."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc).replace(microsecond=0)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return now.strftime(fmt), (now + timedelta(hours=hours)).strftime(fmt)


def _build_url(
    sport_key: str,
    markets: str,
    regions: str,
    bookmakers: str,
    event_id: str = "",
    commence_from: str = "",
    commence_to: str = "",
) -> str:
    params = {"apiKey": _get_api_key(), "markets": markets, "oddsFormat": "american", "regions": regions}
    if bookmakers:
        params["bookmakers"] = bookmakers
    if commence_from:
        params["commenceTimeFrom"] = commence_from
    if commence_to:
        params["commenceTimeTo"] = commence_to
    path = f"/sports/{sport_key}/events/{event_id}/odds" if event_id else f"/sports/{sport_key}/odds"
    return f"{ODDS_API_BASE}{path}?{urllib.parse.urlencode(params)}"


def _fetch_sync(url: str, timeout: int = 10):
    if HAS_RESILIENT and _resilient_urlopen is not None:
        return _resilient_urlopen(SOURCE_NAME, url, timeout=timeout)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:  # pragma: no cover
        logger.warning(f"odds_api_fetch failed: {url.split('?')[0]} — {e}")
        return None


async def get_games_with_markets(
    sport_key: str,
    markets: str = "h2h",
    regions: str = "eu",
    bookmakers: str = "pinnacle",
    commence_from: str = "",
    commence_to: str = "",
) -> List[Dict]:
    """Featured-market odds for a sport key. [] if no key / no games / error.

    Cost = markets × regions; `bookmakers` overrides regions to 1 unit (10 books
    = 1 region). Pass commence_from/to (see upcoming_window) to pull only games
    in a time window."""
    if not _get_api_key():
        logger.warning(f"odds_api_fetch: ODDS_API_KEY unset — {sport_key} empty")
        return []
    url = _build_url(sport_key, markets, regions, bookmakers, commence_from=commence_from, commence_to=commence_to)
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as pool:
        data = await loop.run_in_executor(pool, _fetch_sync, url)
    return data if isinstance(data, list) else []


async def get_event_markets(
    sport_key: str, event_id: str, markets: str, regions: str = "us", bookmakers: str = "pinnacle"
) -> Dict:
    """Per-event odds (props/alternates). {} if no key / error.
    Credit cost = unique-markets-returned × regions (or 1 unit when bookmakers set)."""
    if not _get_api_key():
        return {}
    url = _build_url(sport_key, markets, regions, bookmakers, event_id=event_id)
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as pool:
        data = await loop.run_in_executor(pool, _fetch_sync, url)
    return data if isinstance(data, dict) else {}


# Sharpest outright sources first. Betfair Exchange leads for tournament winners
# (Pinnacle does NOT carry World Cup outright winner — confirmed via live probe).
OUTRIGHT_SHARP_PREFERENCE = ("betfair_ex_eu", "betfair_ex_uk", "pinnacle", "williamhill")


def extract_outright_field(raw: List[Dict]) -> List[Dict]:
    """Pull the outrights outcomes [{name, price}, ...] from a /odds outrights payload,
    preferring the sharpest book that carries the market (Betfair Exchange), falling
    back to whatever book is present."""
    by_book: Dict[str, List[Dict]] = {}
    for ev in raw or []:
        for bk in ev.get("bookmakers", []):
            for mk in bk.get("markets", []):
                if mk.get("key") == "outrights":
                    by_book[bk.get("key", "")] = mk.get("outcomes", [])
    for pref in OUTRIGHT_SHARP_PREFERENCE:
        if pref in by_book:
            return by_book[pref]
    return next(iter(by_book.values()), [])

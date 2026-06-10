"""
odds_api_cache.py — Shared TTL cache for Odds API responses.

WHY THIS EXISTS
---------------
Multiple modules (baseball_edge, pm_props_scanner, wc_edge_scanner, player_profile,
three-way scanner) independently call the Odds API. Each call costs credits.
Game lines (h2h/totals) don't change faster than every 5-10 minutes; Pinnacle
updates are batched. Fetching the same endpoint twice in a 20-min window wastes
credits with no new information.

This module provides a single process-level cache with configurable TTL.
Any module that imports this will share the same cached data for the process
lifetime, avoiding duplicate calls within a scan window.

CREDIT BUDGET (reference)
--------------------------
100K credits/month (~$30 plan). At current usage:
  - 30-min MLB scan (h2h+totals): 2 cr × 48 runs = 96 cr/day = 2,880/month
  - Props scan (15 events × 1 cr): 15 cr per run
  - Three-way scan (shares MLB fetch): 0 additional cr
  - Manual player_profile: ~1-3 cr per run

Rule: always use bookmakers=pinnacle (1 unit per market type) not regions=us.
Rule: /events list is FREE — use it to check if games exist before spending credits.
Rule: per-event props cost 0 when Pinnacle hasn't posted lines yet (early morning).
Rule: bundle markets in one call (h2h,totals = 2cr, not two 1cr calls).

USAGE
-----
    from odds.odds_api_cache import get_mlb_game_lines, get_mlb_events, get_prop_lines

    # Free — event IDs and timestamps only (0 credits)
    events = get_mlb_events()

    # 2 credits, cached 20 min
    games = get_mlb_game_lines()          # h2h + totals, Pinnacle

    # 1 credit per event WITH active lines, cached 30 min
    # Only burns credits if Pinnacle has props (typically <3h before game)
    props = get_prop_lines(event_id, markets="pitcher_strikeouts,batter_home_runs")
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    from loguru import logger
except ImportError:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# ── Credit-optimal defaults ───────────────────────────────────────────────────
# Using bookmakers= instead of regions= bills per-market-type, not per-region.
# Pinnacle only is sufficient — it's the sharpest book and sufficient for devig.
DEFAULT_SHARP_BOOKS = "pinnacle"

# TTL settings (seconds)
TTL_GAME_LINES  = 20 * 60   # 20 min — Pinnacle updates lines every 5-10 min; 20 is fine
TTL_EVENTS_LIST = 60 * 60   # 60 min — event list rarely changes intraday
TTL_PROP_LINES  = 30 * 60   # 30 min — prop lines stable once posted
TTL_FUTURES     = 4 * 60 * 60  # 4 hours — futures move slowly

# Credit tracking
_credits_used_this_session = 0
_last_remaining = None


# ── In-process cache ─────────────────────────────────────────────────────────
_cache: Dict[str, Tuple[float, Any]] = {}  # key → (expires_at, data)


def _get(key: str) -> Optional[Any]:
    entry = _cache.get(key)
    if entry and time.time() < entry[0]:
        return entry[1]
    return None


def _set(key: str, data: Any, ttl: int) -> None:
    _cache[key] = (time.time() + ttl, data)


def cache_stats() -> Dict[str, Any]:
    """Return current cache state for debugging."""
    now = time.time()
    live = {k: round(v[0] - now, 0) for k, v in _cache.items() if v[0] > now}
    return {
        "live_entries": len(live),
        "ttls_remaining_s": live,
        "credits_this_session": _credits_used_this_session,
        "last_remaining": _last_remaining,
    }


def clear_cache() -> None:
    """Force-clear all cached entries. Use when you need fresh data."""
    _cache.clear()


# ── HTTP fetch ────────────────────────────────────────────────────────────────
def _fetch(url: str, timeout: int = 15) -> Optional[Any]:
    """Fetch a URL, track credit headers, return parsed JSON or None on error."""
    global _credits_used_this_session, _last_remaining
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            cost = int(resp.headers.get("x-requests-last", 0) or 0)
            remaining = resp.headers.get("x-requests-remaining")
            _credits_used_this_session += cost
            if remaining:
                _last_remaining = int(remaining)
            if cost > 0:
                logger.debug(f"odds_api_cache: {cost}cr used | {remaining} remaining | {url.split('?')[0].split('/')[-1]}")
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.warning(f"odds_api_cache fetch error: {e}")
        return None


def _api_key() -> Optional[str]:
    return os.getenv("ODDS_API_KEY") or None


def _upcoming_window(hours: int = 36) -> Tuple[str, str]:
    """Return (from, to) ISO strings for the next N hours."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return now.strftime(fmt), (now + timedelta(hours=hours)).strftime(fmt)


# ── Public API ────────────────────────────────────────────────────────────────

def get_mlb_events() -> List[Dict]:
    """
    Fetch today's + tomorrow's MLB event IDs and times. **FREE (0 credits).**
    Use to check if games exist before spending credits on lines/props.
    Cached 60 min.
    """
    key = "mlb_events"
    cached = _get(key)
    if cached is not None:
        return cached

    api_key = _api_key()
    if not api_key:
        return []

    from_ts, to_ts = _upcoming_window(hours=36)
    params = urllib.parse.urlencode({
        "apiKey": api_key,
        "commenceTimeFrom": from_ts,
        "commenceTimeTo": to_ts,
    })
    data = _fetch(f"{ODDS_API_BASE}/sports/baseball_mlb/events?{params}")
    result = data if isinstance(data, list) else []
    _set(key, result, TTL_EVENTS_LIST)
    return result


def get_mlb_game_lines(
    markets: str = "h2h,totals",
    bookmakers: str = DEFAULT_SHARP_BOOKS,
) -> List[Dict]:
    """
    Fetch MLB game lines (h2h + totals) from Pinnacle.
    Cost: 2 credits (h2h=1 + totals=1, bookmakers= billing).
    Cached 20 min.

    To add spreads: markets="h2h,totals,spreads" → 3 credits.
    To add DK as backup: bookmakers="pinnacle,draftkings" → same cost.
    """
    cache_key = f"mlb_lines:{markets}:{bookmakers}"
    cached = _get(cache_key)
    if cached is not None:
        logger.debug(f"odds_api_cache: HIT mlb_lines ({len(cached)} games)")
        return cached

    api_key = _api_key()
    if not api_key:
        return []

    from_ts, to_ts = _upcoming_window(hours=36)
    params = urllib.parse.urlencode({
        "apiKey": api_key,
        "bookmakers": bookmakers,
        "markets": markets,
        "oddsFormat": "american",
        "commenceTimeFrom": from_ts,
        "commenceTimeTo": to_ts,
    })
    data = _fetch(f"{ODDS_API_BASE}/sports/baseball_mlb/odds?{params}")
    result = data if isinstance(data, list) else []
    _set(cache_key, result, TTL_GAME_LINES)
    logger.debug(f"odds_api_cache: MISS mlb_lines → fetched {len(result)} games")
    return result


def get_prop_lines(
    event_id: str,
    markets: str = "pitcher_strikeouts,batter_home_runs",
    bookmakers: str = DEFAULT_SHARP_BOOKS,
) -> Dict:
    """
    Fetch player prop lines for a single event.
    Cost: 0 credits if Pinnacle hasn't posted lines yet (early morning).
         ~1 credit once lines are active (typically 2-3h before game).
    Bundle all desired prop markets in one call to keep cost at 1 credit.
    Cached 30 min.
    """
    cache_key = f"props:{event_id}:{markets}:{bookmakers}"
    cached = _get(cache_key)
    if cached is not None:
        return cached

    api_key = _api_key()
    if not api_key:
        return {}

    params = urllib.parse.urlencode({
        "apiKey": api_key,
        "bookmakers": bookmakers,
        "markets": markets,
        "oddsFormat": "american",
    })
    data = _fetch(f"{ODDS_API_BASE}/sports/baseball_mlb/events/{event_id}/odds?{params}")
    result = data if isinstance(data, dict) else {}
    _set(cache_key, result, TTL_PROP_LINES)
    return result


def get_soccer_lines(
    sport_key: str = "soccer_fifa_world_cup",
    markets: str = "h2h",
    bookmakers: str = DEFAULT_SHARP_BOOKS,
) -> List[Dict]:
    """
    Fetch soccer game lines. Cost: 1 credit (h2h only with Pinnacle).
    Cached 20 min.
    """
    cache_key = f"soccer:{sport_key}:{markets}:{bookmakers}"
    cached = _get(cache_key)
    if cached is not None:
        return cached

    api_key = _api_key()
    if not api_key:
        return []

    from_ts, to_ts = _upcoming_window(hours=48)
    params = urllib.parse.urlencode({
        "apiKey": api_key,
        "bookmakers": bookmakers,
        "markets": markets,
        "oddsFormat": "american",
        "commenceTimeFrom": from_ts,
        "commenceTimeTo": to_ts,
    })
    data = _fetch(f"{ODDS_API_BASE}/sports/{sport_key}/odds?{params}")
    result = data if isinstance(data, list) else []
    _set(cache_key, result, TTL_GAME_LINES)
    return result


def get_all_mlb_prop_lines(
    event_ids: Optional[List[str]] = None,
    markets: str = "pitcher_strikeouts,batter_home_runs,batter_hits,batter_rbis,batter_total_bases",
) -> Dict[str, Dict]:
    """
    Fetch all MLB prop lines for today's games in one pass.
    Returns {event_id: odds_dict}.

    EFFICIENCY: 0 credits until Pinnacle posts lines (~2-3h pre-game).
    Once active: 1 credit per event (all markets bundled in one call).
    With 15 games = 15 credits. Compare: per-market calls = 5 × 15 = 75 credits.

    Calls get_mlb_events() for free if event_ids not provided.
    """
    if event_ids is None:
        event_ids = [e["id"] for e in get_mlb_events()]

    results = {}
    for eid in event_ids:
        data = get_prop_lines(eid, markets=markets)
        if data:
            results[eid] = data
    return results


# ── Credit budget reporting ───────────────────────────────────────────────────

def budget_report() -> str:
    """Quick credit budget summary."""
    stats = cache_stats()
    remaining = stats["last_remaining"] or "unknown"
    per_day_est = (stats["credits_this_session"] / max(1, len(_cache))) * 48  # rough 30-min estimate
    lines = [
        f"Credits this session: {stats['credits_this_session']}",
        f"Credits remaining: {remaining}",
        f"Cache entries live: {stats['live_entries']}",
        f"Est. monthly burn (30-min cron): ~{int(per_day_est * 30):,} credits",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    # Quick test + credit cost audit
    print("\n── Odds API Cache Test ──\n")

    events = get_mlb_events()
    print(f"Events (free): {len(events)} games")

    lines = get_mlb_game_lines()
    print(f"Game lines (2cr): {len(lines)} games")

    # Second call should be a cache HIT (0 credits)
    lines2 = get_mlb_game_lines()
    print(f"Game lines (cached, 0cr): {len(lines2)} games")

    print(f"\n{budget_report()}")
    print(f"\n{cache_stats()}")

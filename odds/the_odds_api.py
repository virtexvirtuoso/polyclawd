"""
The Odds API client — replaces VegasInsider scraping for soccer edges.

Status: SCAFFOLDING (2026-05-15). Activatable by setting ODDS_API_KEY env var.
Until activated, all entry points return {"error": "ODDS_API_KEY not configured"}
so consumers can detect-and-degrade rather than 500.

Vendor evaluation: see ENSEMBLE_AUDIT_2026-05-15_05_Odds-API-Replacement.md
- The Odds API ($30/mo / 20K credits) chosen for: soccer + 4 US majors + tennis/MMA
  under one schema, API-key query-param auth, US VPS OK, operating since 2017.
- Free tier: 500 credits/mo (enough for shadow validation).
- Quota visible in response header `x-requests-remaining`.

Health tracking: routed through _resilient_urlopen("the_odds_api", url) so
source_health.record_success/failure get called automatically.

To activate:
    1. Sign up at https://the-odds-api.com/, copy key
    2. ssh vps 'sudo systemctl set-environment ODDS_API_KEY=<key>'
    3. ssh vps 'sudo systemctl restart polyclawd-api'
    4. Update api/routes/markets.py /vegas/soccer + league endpoints to import
       from this module instead of soccer_edge.get_soccer_edge_summary
    5. Watch source_health table for the_odds_api row + Discord alerts
"""

import os
import json
import time
import asyncio
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from loguru import logger

try:
    from api.services.resilient_fetch import _resilient_urlopen

    HAS_RESILIENT = True
except ImportError:
    HAS_RESILIENT = False
    _resilient_urlopen = None

# Soccer edge detection moved to odds/soccer_match_edge.py + soccer_futures_edge.py
# (shared sports_edge_common core). This module now only provides baseball game
# odds + credit tracking + the health probe.


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SOURCE_NAME = "the_odds_api"  # for source_health tracking

# The Odds API sport key for MLB regular season game odds
BASEBALL_SPORT_KEYS: Dict[str, str] = {
    "mlb": "baseball_mlb",
}


# ---------------------------------------------------------------------------
# Credit budget tracking
# ---------------------------------------------------------------------------

import time as _time_mod

_CREDIT_BUDGET = {"remaining": None, "used": None, "last_check": 0, "alerted_low": False}
CREDIT_LOW_WATERMARK = 5000  # Alert when remaining credits drop below this


def _track_credits_from_response(resp) -> None:
    """Extract credit usage from response headers if present."""
    global _CREDIT_BUDGET
    try:
        remaining = resp.headers.get("x-requests-remaining")
        used = resp.headers.get("x-requests-used")
        if remaining is not None:
            _CREDIT_BUDGET["remaining"] = int(remaining)
        if used is not None:
            _CREDIT_BUDGET["used"] = int(used)
        _CREDIT_BUDGET["last_check"] = time.time()
        if _CREDIT_BUDGET["remaining"] is not None and _CREDIT_BUDGET["remaining"] < CREDIT_LOW_WATERMARK:
            if not _CREDIT_BUDGET["alerted_low"]:
                logger.warning(
                    f"Odds API credit budget low: {_CREDIT_BUDGET['remaining']} remaining (watermark: {CREDIT_LOW_WATERMARK})"
                )
                _CREDIT_BUDGET["alerted_low"] = True
    except (ValueError, TypeError, AttributeError):
        pass


def refresh_credit_balance() -> dict:
    """Call Odds API /v4/sports (free endpoint) to read real credit headers."""
    global _CREDIT_BUDGET
    api_key = _get_api_key()
    if not api_key:
        logger.warning("refresh_credit_balance: ODDS_API_KEY not set")
        return get_credit_status()
    import urllib.request

    url = f"{ODDS_API_BASE}/sports?apiKey={api_key}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            remaining = resp.headers.get("x-requests-remaining")
            used = resp.headers.get("x-requests-used")
            if remaining is not None:
                _CREDIT_BUDGET["remaining"] = int(remaining)
            if used is not None:
                _CREDIT_BUDGET["used"] = int(used)
            _CREDIT_BUDGET["last_check"] = time.time()
            r_int = int(remaining) if remaining is not None else None
            if r_int is not None and r_int < CREDIT_LOW_WATERMARK:
                if not _CREDIT_BUDGET["alerted_low"]:
                    logger.warning(f"Credits low: {r_int} remaining")
                    _CREDIT_BUDGET["alerted_low"] = True
            if r_int is not None and r_int > CREDIT_LOW_WATERMARK:
                _CREDIT_BUDGET["alerted_low"] = False
            logger.info(f"Credits: {used}/{r_int or '?'} used")
            return get_credit_status()
    except Exception as e:
        logger.warning(f"refresh_credit_balance failed: {e}")
        return get_credit_status()


# Simple per-day call counter (estimate, since resilient_fetch returns parsed data not headers)
_CALL_COUNTER = {"date": "", "count": 0}


def _count_call():
    """Increment call counter, reset at UTC day boundary."""
    import datetime

    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    if _CALL_COUNTER["date"] != today:
        _CALL_COUNTER["date"] = today
        _CALL_COUNTER["count"] = 0
    _CALL_COUNTER["count"] += 1


def get_credit_status() -> dict:
    """Return current Odds API credit usage for dashboards."""
    global _CREDIT_BUDGET
    return {
        "remaining": _CREDIT_BUDGET.get("remaining"),
        "used": _CREDIT_BUDGET.get("used"),
        "budget": 20000,
        "last_check": _CREDIT_BUDGET.get("last_check"),
        "estimated_today": _CALL_COUNTER.get("count", 0),  # approx calls today
        "alerted": _CREDIT_BUDGET.get("alerted_low", False),
    }



# ---------------------------------------------------------------------------
# Key resolution + auth
# ---------------------------------------------------------------------------


def _get_api_key() -> Optional[str]:
    """Read ODDS_API_KEY from env. Returns None if unset — caller must handle."""
    return os.getenv("ODDS_API_KEY") or None


# ---------------------------------------------------------------------------
# American odds <-> implied prob
# ---------------------------------------------------------------------------


def _american_to_implied_prob(odds: int) -> float:
    """Standard moneyline → implied probability conversion."""
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


# ---------------------------------------------------------------------------
# Single-league fetch (sync, called from thread pool)
# ---------------------------------------------------------------------------


def _fetch_league_sync(api_key: str, sport_key: str, timeout: int = 10) -> List[Dict]:
    """Fetch raw odds for one sport key from The Odds API. Returns parsed list or []."""
    import urllib.request  # must precede urllib.parse.urlencode call

    params = {
        "apiKey": api_key,
        "regions": "us,uk,eu",
        "markets": "h2h",
        "oddsFormat": "american",
    }
    url = f"{ODDS_API_BASE}/sports/{sport_key}/odds?{urllib.parse.urlencode(params)}"

    if HAS_RESILIENT and _resilient_urlopen is not None:
        # Routes through retries + circuit breaker + source_health tracking
        data = _resilient_urlopen(SOURCE_NAME, url, timeout=timeout)
        _count_call()
        return data if isinstance(data, list) else []

    # Fallback path (resilient_fetch unavailable — should not normally happen)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # Track credit usage from response headers
            _track_credits_from_response(resp)
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.warning(f"the_odds_api fetch failed for {sport_key}: {e}")
        return []


# ---------------------------------------------------------------------------
# Health probe (optional — can be wired into polyclawd-scheduler)
# ---------------------------------------------------------------------------


async def get_baseball_games_with_odds() -> List[Dict]:
    """
    Fetch today's MLB game odds (h2h only) from The Odds API.
    Returns [] if ODDS_API_KEY is not set or no games today.
    """
    api_key = _get_api_key()
    if not api_key:
        logger.warning("the_odds_api: ODDS_API_KEY not set — returning empty baseball data")
        return []

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as pool:
        raw = await loop.run_in_executor(pool, _fetch_league_sync, api_key, BASEBALL_SPORT_KEYS["mlb"])

    return raw if isinstance(raw, list) else []


async def get_baseball_games_with_all_markets() -> List[Dict]:
    """
    Fetch today's MLB game odds including h2h, spreads, and totals.
    Same API call but with all three market types.
    Returns [] if ODDS_API_KEY is not set or no games today.
    """
    import urllib.request  # must precede urllib.parse.urlencode call

    api_key = _get_api_key()
    if not api_key:
        logger.warning("the_odds_api: ODDS_API_KEY not set — returning empty baseball data")
        return []

    sport_key = BASEBALL_SPORT_KEYS["mlb"]
    params = {
        "apiKey": api_key,
        "regions": "us,uk,eu",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "american",
    }
    url = f"{ODDS_API_BASE}/sports/{sport_key}/odds?{urllib.parse.urlencode(params)}"

    if HAS_RESILIENT and _resilient_urlopen is not None:
        data = _resilient_urlopen(SOURCE_NAME, url, timeout=10)
        _count_call()
        return data if isinstance(data, list) else []

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            _track_credits_from_response(resp)
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.warning(f"the_odds_api baseball all-markets fetch failed: {e}")
        return []


def health_probe(timeout: int = 5) -> Tuple[bool, str]:
    """
    Cheap probe for source_health: hit /v4/sports (lists available sports).
    Returns (ok, detail). Does NOT consume a credit (sports listing is free).

    Use from a scheduled task to keep `the_odds_api` row in source_health fresh.
    """
    api_key = _get_api_key()
    if not api_key:
        return False, "ODDS_API_KEY not configured"

    url = f"{ODDS_API_BASE}/sports?apiKey={api_key}"
    if HAS_RESILIENT and _resilient_urlopen is not None:
        try:
            data = _resilient_urlopen(SOURCE_NAME, url, timeout=timeout)
            return (isinstance(data, list) and len(data) > 0), f"{len(data) if isinstance(data, list) else 0} sports"
        except Exception as e:
            return False, str(e)

    import urllib.request

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return True, f"{len(data) if isinstance(data, list) else 0} sports"
    except Exception as e:
        return False, str(e)

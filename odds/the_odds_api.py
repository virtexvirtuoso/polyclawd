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

try:
    from .client import devig_multiway
except ImportError:
    from client import devig_multiway

# ── Soccer helpers (relocated from the retired soccer_edge.py, 2026-06-02).
#    Clean 3-tuple match_team contract that find_soccer_edges() expects. ──
import requests as _requests


@dataclass
class SoccerEdge:
    team: str
    league: str
    vegas_prob: float
    vegas_odds: int
    polymarket_price: float
    edge_pct: float
    direction: str
    poly_market_id: Optional[str] = None


POLYMARKET_SEARCHES = {
    "epl": "Premier League Winner",
    "ucl": "Champions League Winner",
    "world_cup": "World Cup Winner",
    "la_liga": "La Liga Winner",
    "bundesliga": "Bundesliga Winner",
}

TEAM_ALIASES = {
    "Man City": ["Manchester City", "Man City"],
    "Manchester City": ["Manchester City", "Man City"],
    "Man Utd": ["Manchester United", "Man United", "Man Utd"],
    "PSG": ["Paris Saint-Germain", "PSG"],
    "Paris Saint-Germain": ["Paris Saint-Germain", "PSG"],
    "Bayern Munich": ["Bayern Munich", "Bayern"],
    "Spurs": ["Tottenham", "Spurs"],
    "Tottenham": ["Tottenham", "Spurs"],
    "Inter Milan": ["Inter", "Inter Milan"],
    "Inter": ["Inter", "Inter Milan"],
    "Atletico Madrid": ["Atletico Madrid", "Atletico Madrid", "Atletico"],
    "Dortmund": ["Dortmund", "Borussia Dortmund"],
    "Borussia Dortmund": ["Dortmund", "Borussia Dortmund"],
    "Leverkusen": ["Bayer Leverkusen", "Leverkusen"],
    "Bayer Leverkusen": ["Bayer Leverkusen", "Leverkusen"],
}


def normalize_team(team: str) -> List[str]:
    team = team.strip()
    if team in TEAM_ALIASES:
        return TEAM_ALIASES[team]
    return [team]


def _fetch_polymarket_soccer_sync() -> list:
    try:
        resp = _requests.get(
            "https://gamma-api.polymarket.com/events",
            params={"closed": "false", "limit": "200"}, timeout=30,
        )
        return resp.json()
    except Exception as e:
        logger.warning(f"polymarket soccer fetch failed: {e}")
        return []


async def get_polymarket_soccer_markets() -> Dict[str, Dict[str, Tuple[float, str]]]:
    """{league: {team: (price, market_id)}} — clean 2-tuple values."""
    results: Dict[str, Dict[str, Tuple[float, str]]] = {}
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as executor:
        events = await loop.run_in_executor(executor, _fetch_polymarket_soccer_sync)
    for search_key, search_term in POLYMARKET_SEARCHES.items():
        results[search_key] = {}
        for event in events:
            if search_term.lower() in event.get("title", "").lower():
                for market in event.get("markets", []):
                    question = market.get("question", "")
                    price = market.get("bestAsk", 0)
                    market_id = market.get("id", "")
                    if "Will " in question and " win " in question:
                        team = question.split("Will ")[1].split(" win ")[0].strip()
                        if price and price < 1:
                            results[search_key][team] = (float(price), market_id)
                break
    return results


def match_team(vegas_team: str, poly_teams: Dict[str, Tuple[float, str]]) -> Optional[Tuple[str, float, str]]:
    """Returns (matched_name, price, market_id) or None — 3-tuple contract."""
    for var in normalize_team(vegas_team):
        for poly_team, (price, market_id) in poly_teams.items():
            if var.lower() == poly_team.lower():
                return (poly_team, price, market_id)
            if var.lower() in poly_team.lower() or poly_team.lower() in var.lower():
                return (poly_team, price, market_id)
    return None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SOURCE_NAME = "the_odds_api"  # for source_health tracking

# The Odds API sport keys for soccer leagues we care about.
# Maps internal league name → API sport_key (verified against
# https://the-odds-api.com/sports-odds-data/sports-apis.html)
SOCCER_SPORT_KEYS: Dict[str, str] = {
    "epl": "soccer_epl",
    "ucl": "soccer_uefa_champs_league",
    "laliga": "soccer_spain_la_liga",
    "bundesliga": "soccer_germany_bundesliga",
    "worldcup": "soccer_fifa_world_cup",
}

# The Odds API sport key for MLB regular season game odds
BASEBALL_SPORT_KEYS: Dict[str, str] = {
    "mlb": "baseball_mlb",
}


# ---------------------------------------------------------------------------
# Credit budget tracking
# ---------------------------------------------------------------------------

import time as _time_mod

_CREDIT_BUDGET = {"remaining": None, "used": None, "last_check": 0, "alerted_low": False}
# Alert when remaining drops below 20% of the 100K LIVE plan (was 100, sized for
# the dead 500-credit free tier). Sourced from rate_limiter so there's one knob.
try:
    from odds.rate_limiter import LOW_CREDIT_WATERMARK as CREDIT_LOW_WATERMARK
except Exception:  # pragma: no cover
    CREDIT_LOW_WATERMARK = 20_000

# Seed from the persisted real balance so a fresh process knows the balance
# before its first fetch (survives restarts).
try:
    from odds.rate_limiter import read_real_remaining as _read_real_remaining

    _seed = _read_real_remaining()
    if _seed is not None:
        _CREDIT_BUDGET["remaining"] = _seed
except Exception:  # pragma: no cover
    pass


def _record_credits(remaining, used) -> None:
    """Single sink for live credit headers: update in-memory budget, persist the
    real balance to disk (survives restarts), and fire a latched Discord alert
    below the low-credit watermark. Never raises."""
    global _CREDIT_BUDGET
    try:
        r_int = int(remaining) if remaining is not None else None
        u_int = int(used) if used is not None else None
        if r_int is not None:
            _CREDIT_BUDGET["remaining"] = r_int
        if u_int is not None:
            _CREDIT_BUDGET["used"] = u_int
        _CREDIT_BUDGET["last_check"] = time.time()

        # Persist the authoritative balance to disk.
        try:
            from odds.rate_limiter import persist_real_remaining

            persist_real_remaining(r_int, u_int)
        except Exception as e:  # pragma: no cover
            logger.debug(f"persist real remaining skipped: {e}")

        # Latched low-credit Discord alert.
        if r_int is not None and r_int < CREDIT_LOW_WATERMARK:
            if not _CREDIT_BUDGET["alerted_low"]:
                logger.warning(f"Odds API credits low: {r_int} remaining (watermark {CREDIT_LOW_WATERMARK})")
                try:
                    from signals.discord_alerts import alert_credits_low

                    alert_credits_low(r_int, CREDIT_LOW_WATERMARK, used=u_int)
                except Exception as e:  # pragma: no cover
                    logger.debug(f"low-credit Discord alert skipped: {e}")
                _CREDIT_BUDGET["alerted_low"] = True
        elif r_int is not None and r_int >= CREDIT_LOW_WATERMARK:
            _CREDIT_BUDGET["alerted_low"] = False  # re-arm after recovery
    except (ValueError, TypeError, AttributeError):
        pass


def _track_credits_from_response(resp) -> None:
    """Extract credit usage from response headers if present."""
    try:
        _record_credits(resp.headers.get("x-requests-remaining"), resp.headers.get("x-requests-used"))
    except (AttributeError, TypeError):
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
            _record_credits(remaining, used)  # persist + latched low-credit alert
            logger.info(f"Credits: {used}/{remaining or '?'} used")
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
        "budget": 100000,  # LIVE plan size (100K/mo)
        "last_check": _CREDIT_BUDGET.get("last_check"),
        "estimated_today": _CALL_COUNTER.get("count", 0),  # approx calls today
        "alerted": _CREDIT_BUDGET.get("alerted_low", False),
    }


# ---------------------------------------------------------------------------
# Lightweight VegasOdds-shaped object (avoid importing vegas_scraper)
# ---------------------------------------------------------------------------

@dataclass
class TheOddsApiOdds:
    """Mirrors VegasOdds shape so downstream consumers don't need to change."""
    team: str
    american_odds: int
    implied_prob: float


# ---------------------------------------------------------------------------
# Key resolution + auth
# ---------------------------------------------------------------------------

def _get_api_key() -> Optional[str]:
    """Read ODDS_API_KEY from env. Returns None if unset — caller must handle."""
    return os.getenv("ODDS_API_KEY") or None


def _get_baseball_api_key() -> Optional[str]:
    """
    Baseball-dedicated key. Reads ODDS_API_KEY_2 first, falls back to ODDS_API_KEY.
    This lets baseball consume its own credit budget without draining the shared pool.
    """
    return os.getenv("ODDS_API_KEY_2") or os.getenv("ODDS_API_KEY") or None


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
    try:
        from odds.rate_limiter import can_make_call

        _ok, _why = can_make_call("normal")
        if not _ok:
            logger.warning(f"the_odds_api: credit gate ({sport_key}) — {_why}")
            return []
    except Exception:  # pragma: no cover — never let gating break a fetch
        pass
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
# Aggregate fetch across all soccer leagues
# ---------------------------------------------------------------------------

async def get_the_odds_api_soccer_odds() -> Dict[str, List[TheOddsApiOdds]]:
    """
    Fetch h2h odds across all configured soccer leagues.
    Returns {league_name: [TheOddsApiOdds, ...]} — matches the shape that
    soccer_edge.find_soccer_edges() expects from get_vegas_odds_with_fallback().

    If no API key: returns {} with a logged warning. Caller can detect-and-degrade.
    If a league has no upcoming games: that league is omitted (empty list filtered).
    """
    api_key = _get_api_key()
    if not api_key:
        logger.warning("the_odds_api: ODDS_API_KEY not set — returning empty data")
        return {}

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=len(SOCCER_SPORT_KEYS)) as pool:
        tasks = [
            loop.run_in_executor(pool, _fetch_league_sync, api_key, sport_key)
            for sport_key in SOCCER_SPORT_KEYS.values()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    out: Dict[str, List[TheOddsApiOdds]] = {}
    for league_name, raw in zip(SOCCER_SPORT_KEYS.keys(), results):
        if isinstance(raw, Exception):
            logger.warning(f"the_odds_api: {league_name} fetch raised: {raw}")
            continue
        if not raw:
            continue

        # Average implied prob per team across ALL books (was best-of-all, which
        # cherry-picked the most favorable line per team, deflating the overround
        # and manufacturing phantom edges). Display odds = last seen. This path is
        # deprecated — superseded by the consensus engines at /api/soccer/*.
        prob_sum: Dict[str, float] = {}
        prob_cnt: Dict[str, int] = {}
        team_to_odds: Dict[str, int] = {}
        for event in raw:
            for bookmaker in event.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    if market.get("key") != "h2h":
                        continue
                    for outcome in market.get("outcomes", []):
                        team = outcome.get("name")
                        price = outcome.get("price")
                        if team is None or price is None:
                            continue
                        prob_sum[team] = prob_sum.get(team, 0.0) + _american_to_implied_prob(int(price))
                        prob_cnt[team] = prob_cnt.get(team, 0) + 1
                        team_to_odds[team] = int(price)

        odds_list = [
            TheOddsApiOdds(
                team=team,
                american_odds=team_to_odds[team],
                implied_prob=prob_sum[team] / prob_cnt[team],
            )
            for team in prob_sum
        ]
        if odds_list:
            out[league_name] = odds_list

    return out


# ---------------------------------------------------------------------------
# Edge detection — mirrors soccer_edge.find_soccer_edges() contract
# ---------------------------------------------------------------------------

async def find_soccer_edges(min_edge: float = 0.01) -> List[SoccerEdge]:
    """
    Drop-in replacement for odds.soccer_edge.find_soccer_edges using The Odds API
    as the odds source. Same return type, same min_edge semantics.

    Returns [] if ODDS_API_KEY is not set — consumers should treat that as
    "no soccer data available, skip" rather than 500.
    """
    edges: List[SoccerEdge] = []

    odds_data = await get_the_odds_api_soccer_odds()
    if not odds_data:
        return edges

    poly_data = await get_polymarket_soccer_markets()

    for league, odds_list in odds_data.items():
        poly_markets = poly_data.get(league, {})
        if not poly_markets or not odds_list:
            continue

        # Devig to remove bookmaker margin
        raw_probs = [o.implied_prob for o in odds_list]
        devigged_probs = devig_multiway(raw_probs)
        team_to_devigged = {
            odds_list[i].team: devigged_probs[i]
            for i in range(len(odds_list))
        }

        for o in odds_list:
            match = match_team(o.team, poly_markets)
            if not match:
                continue
            poly_team, poly_price, market_id = match
            true_prob = team_to_devigged.get(o.team, o.implied_prob)
            edge = true_prob - poly_price

            if abs(edge) >= min_edge:
                edges.append(SoccerEdge(
                    team=o.team,
                    league=league.upper(),
                    vegas_prob=true_prob,
                    vegas_odds=o.american_odds,
                    polymarket_price=poly_price,
                    edge_pct=edge,
                    direction="BUY" if edge > 0 else "SELL",
                    poly_market_id=market_id,
                ))

    edges.sort(key=lambda e: abs(e.edge_pct), reverse=True)
    return edges


async def get_soccer_edge_summary() -> Dict:
    """
    Drop-in replacement for odds.soccer_edge.get_soccer_edge_summary.
    Same response shape so routes can swap import without changing JSON contract.
    """
    api_key = _get_api_key()
    if not api_key:
        return {
            "error": "ODDS_API_KEY not configured",
            "hint": "Set the env var on polyclawd-api and restart. See odds/the_odds_api.py docstring.",
            "edges": [],
            "total_edges": 0,
            "source": SOURCE_NAME,
        }

    edges = await find_soccer_edges(min_edge=0.01)

    leagues_summary: Dict[str, Dict] = {}
    for e in edges:
        if e.league not in leagues_summary:
            leagues_summary[e.league] = {"total": 0, "buy": 0, "sell": 0, "best_edge": 0.0}
        leagues_summary[e.league]["total"] += 1
        leagues_summary[e.league][e.direction.lower()] += 1
        if abs(e.edge_pct) > abs(leagues_summary[e.league]["best_edge"]):
            leagues_summary[e.league]["best_edge"] = e.edge_pct

    return {
        "source": SOURCE_NAME,
        "total_edges": len(edges),
        "leagues": leagues_summary,
        "edges": [
            {
                "team": e.team,
                "league": e.league,
                "vegas_prob": round(e.vegas_prob, 4),
                "vegas_odds": e.vegas_odds,
                "polymarket_price": round(e.polymarket_price, 4),
                "edge_pct": round(e.edge_pct * 100, 2),
                "direction": e.direction,
                "market_id": e.poly_market_id,
            }
            for e in edges
        ],
    }


# ---------------------------------------------------------------------------
# Health probe (optional — can be wired into polyclawd-scheduler)
# ---------------------------------------------------------------------------

async def get_baseball_games_with_odds() -> List[Dict]:
    """
    Fetch today's MLB game odds (h2h only) from The Odds API.
    Returns [] if ODDS_API_KEY is not set or no games today.
    """
    api_key = _get_baseball_api_key()
    if not api_key:
        logger.warning("the_odds_api: ODDS_API_KEY not set — returning empty baseball data")
        return []

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as pool:
        raw = await loop.run_in_executor(
            pool, _fetch_league_sync, api_key, BASEBALL_SPORT_KEYS["mlb"]
        )

    return raw if isinstance(raw, list) else []


async def get_baseball_games_with_all_markets() -> List[Dict]:
    """
    Fetch today's MLB game odds including h2h, spreads, and totals.
    Same API call but with all three market types.
    Returns [] if ODDS_API_KEY_2 is not set or no games today.
    """
    import urllib.request  # must precede urllib.parse.urlencode call
    api_key = _get_baseball_api_key()
    if not api_key:
        logger.warning("the_odds_api: ODDS_API_KEY not set — returning empty baseball data")
        return []

    sport_key = BASEBALL_SPORT_KEYS["mlb"]
    try:
        from odds.rate_limiter import can_make_call

        _ok, _why = can_make_call("normal")
        if not _ok:
            logger.warning(f"the_odds_api: credit gate ({sport_key}) — {_why}")
            return []
    except Exception:  # pragma: no cover — never let gating break a fetch
        pass

    # bookmakers= overrides regions and bills by the number of regions the listed
    # books SPAN (not 1 flat unit): the sharp set spans us/uk/eu -> 6 credits vs 9
    # for regions=us,uk,eu. Returns exactly the books the weighted consensus uses;
    # the ~37 soft books we drop all carry weight 0, so edges are unchanged.
    try:
        from . import sports_edge_common as _sec
    except ImportError:  # pragma: no cover
        import sports_edge_common as _sec

    params = {
        "apiKey": api_key,
        "bookmakers": _sec.CONSENSUS_BOOKMAKERS,
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

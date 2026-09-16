# MLB Baseball Edge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingest live MLB game odds from The Odds API, compare to Polymarket game markets, and expose `/api/baseball/edge` with shadow-trade-ready signals.

**Architecture:** `odds/baseball_edge.py` (new) fetches The Odds API `baseball_mlb` h2h per-game data and Polymarket events tagged `baseball`; it deviGs bookmaker odds and computes edge vs Polymarket game moneyline prices. `odds/mlb_lineups.py` (new) wraps the free MLB Stats API for starting lineup confirmation. Route and cache wire both into the existing API surface.

**Tech Stack:** Python 3.11, asyncio/ThreadPoolExecutor, `requests`, FastAPI, The Odds API v4, Polymarket Gamma API, MLB Stats API (free)

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `odds/baseball_edge.py` | **Create** | Per-game edge detection: team matching, devig, Polymarket price extraction |
| `odds/mlb_lineups.py` | **Create** | MLB Stats API lineup fetcher with TTL cache |
| `odds/the_odds_api.py` | **Modify** | Add `BASEBALL_SPORT_KEYS` + `get_baseball_games_with_odds()` |
| `api/routes/markets.py` | **Modify** | Add `GET /api/baseball/edge` endpoint |
| `api/edge_cache.py` | **Modify** | Add `fetch_baseball_edges()`, wire into `refresh_edge_cache()` |
| `tests/unit/test_baseball_edge.py` | **Create** | Unit tests for game matching, devig, price extraction |

---

## Task 1: Add baseball support to `odds/the_odds_api.py`

**Files:**
- Modify: `odds/the_odds_api.py`

- [ ] **Step 1: Add `BASEBALL_SPORT_KEYS` constant after `SOCCER_SPORT_KEYS`**

Open `odds/the_odds_api.py`. After the `SOCCER_SPORT_KEYS` dict (around line 65), add:

```python
# The Odds API sport key for MLB regular season game odds
BASEBALL_SPORT_KEYS: Dict[str, str] = {
    "mlb": "baseball_mlb",
}
```

- [ ] **Step 2: Add `get_baseball_games_with_odds()` at the end of the file (before the health probe)**

Add after the `get_soccer_edge_summary()` function and before `health_probe()`:

```python
async def get_baseball_games_with_odds() -> List[Dict]:
    """
    Fetch today's MLB game odds from The Odds API.
    Returns the raw Odds API event list (one dict per game):
      [{"id": ..., "home_team": "...", "away_team": "...",
        "commence_time": "2026-05-30T18:10:00Z",
        "bookmakers": [{"key": "draftkings", "markets": [{"key": "h2h",
          "outcomes": [{"name": "Cubs", "price": -130}, ...]}]}]}, ...]

    Returns [] if ODDS_API_KEY is not set or no games today.
    """
    api_key = _get_api_key()
    if not api_key:
        logger.warning("the_odds_api: ODDS_API_KEY not set — returning empty baseball data")
        return []

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as pool:
        raw = await loop.run_in_executor(
            pool, _fetch_league_sync, api_key, BASEBALL_SPORT_KEYS["mlb"]
        )

    return raw if isinstance(raw, list) else []
```

- [ ] **Step 3: Verify the file parses correctly**

```bash
cd ~/Desktop/polyclawd
python3 -c "from odds.the_odds_api import get_baseball_games_with_odds, BASEBALL_SPORT_KEYS; print('OK', BASEBALL_SPORT_KEYS)"
```

Expected output: `OK {'mlb': 'baseball_mlb'}`

- [ ] **Step 4: Commit**

```bash
cd ~/Desktop/polyclawd
git add odds/the_odds_api.py
git commit -m "feat: add get_baseball_games_with_odds to the_odds_api"
```

---

## Task 2: Create `odds/baseball_edge.py`

**Files:**
- Create: `odds/baseball_edge.py`

- [ ] **Step 1: Write the failing test first**

Create `tests/unit/test_baseball_edge.py`:

```python
"""Unit tests for MLB baseball edge detection."""
import json
import pytest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from odds.baseball_edge import (
    _team_in_title,
    _find_matching_event,
    _extract_moneyline_prices,
    _devig_two_way,
    _best_odds_per_team,
    MLBEdge,
)


# ---------------------------------------------------------------------------
# _team_in_title
# ---------------------------------------------------------------------------

def test_team_in_title_exact():
    assert _team_in_title("Chicago Cubs", "Chicago Cubs vs. St. Louis Cardinals") is True

def test_team_in_title_alias():
    assert _team_in_title("Chicago Cubs", "Cubs vs. Cardinals") is True

def test_team_in_title_no_match():
    assert _team_in_title("Chicago Cubs", "Yankees vs. Red Sox") is False

def test_team_in_title_partial_overlap_rejected():
    # "Cardinals" should not match "Arizona Diamondbacks"
    assert _team_in_title("St. Louis Cardinals", "Arizona Diamondbacks vs. Seattle Mariners") is False


# ---------------------------------------------------------------------------
# _find_matching_event
# ---------------------------------------------------------------------------

SAMPLE_POLY_EVENTS = [
    {"id": "e1", "title": "Philadelphia Phillies vs. Los Angeles Dodgers",
     "markets": [{"question": "Philadelphia Phillies vs. Los Angeles Dodgers",
                  "outcomePrices": '["0.375", "0.625"]', "bestAsk": 0.38, "id": "m1"}]},
    {"id": "e2", "title": "New York Yankees vs. Athletics",
     "markets": [{"question": "New York Yankees vs. Athletics",
                  "outcomePrices": '["0.6", "0.4"]', "bestAsk": 0.61, "id": "m2"}]},
]

def test_find_matching_event_found():
    event = _find_matching_event("Los Angeles Dodgers", "Philadelphia Phillies", SAMPLE_POLY_EVENTS)
    assert event is not None
    assert event["id"] == "e1"

def test_find_matching_event_alias():
    # "Oakland Athletics" from Odds API should match "Athletics" in Polymarket title
    event = _find_matching_event("Oakland Athletics", "New York Yankees", SAMPLE_POLY_EVENTS)
    assert event is not None
    assert event["id"] == "e2"

def test_find_matching_event_not_found():
    event = _find_matching_event("Boston Red Sox", "Tampa Bay Rays", SAMPLE_POLY_EVENTS)
    assert event is None

def test_find_matching_event_skips_non_game_events():
    non_game_events = [{"id": "e3", "title": "MLB World Series Champion 2026", "markets": []}]
    event = _find_matching_event("Los Angeles Dodgers", "Philadelphia Phillies", non_game_events)
    assert event is None


# ---------------------------------------------------------------------------
# _extract_moneyline_prices
# ---------------------------------------------------------------------------

def test_extract_moneyline_prices_home_is_first():
    # Title: "Philadelphia Phillies vs. Los Angeles Dodgers"
    # Phillies listed first → outcomePrices[0] = Phillies price
    event = {
        "title": "Philadelphia Phillies vs. Los Angeles Dodgers",
        "markets": [
            {"question": "Philadelphia Phillies vs. Los Angeles Dodgers",
             "outcomePrices": '["0.375", "0.625"]', "id": "m1"}
        ]
    }
    # home=Dodgers, away=Phillies (Phillies listed first in title)
    result = _extract_moneyline_prices(event, home_team="Los Angeles Dodgers", away_team="Philadelphia Phillies")
    assert result is not None
    home_price, away_price, market_id = result
    # Dodgers listed second → outcomePrices[1] = 0.625
    assert abs(home_price - 0.625) < 0.001
    # Phillies listed first → outcomePrices[0] = 0.375
    assert abs(away_price - 0.375) < 0.001
    assert market_id == "m1"

def test_extract_moneyline_prices_no_market():
    event = {
        "title": "Cubs vs. Cardinals",
        "markets": [
            {"question": "Will there be a run in the first inning?",
             "outcomePrices": '["0.5", "0.5"]', "id": "m2"}
        ]
    }
    result = _extract_moneyline_prices(event, "Cardinals", "Cubs")
    assert result is None

def test_extract_moneyline_prices_zero_rejected():
    event = {
        "title": "Cubs vs. Cardinals",
        "markets": [
            {"question": "Cubs vs. Cardinals",
             "outcomePrices": '["0", "1"]', "id": "m3"}
        ]
    }
    result = _extract_moneyline_prices(event, "Cardinals", "Cubs")
    assert result is None


# ---------------------------------------------------------------------------
# _devig_two_way
# ---------------------------------------------------------------------------

def test_devig_two_way_favorite_underdog():
    # Dodgers -150, Phillies +130
    home_p, away_p = _devig_two_way(-150, 130)
    assert abs(home_p + away_p - 1.0) < 0.001
    assert home_p > away_p  # favorite has higher true prob

def test_devig_two_way_even_odds():
    home_p, away_p = _devig_two_way(-110, -110)
    assert abs(home_p - 0.5) < 0.01
    assert abs(away_p - 0.5) < 0.01


# ---------------------------------------------------------------------------
# _best_odds_per_team
# ---------------------------------------------------------------------------

def test_best_odds_per_team_selects_best_line():
    game = {
        "home_team": "Los Angeles Dodgers",
        "away_team": "Philadelphia Phillies",
        "bookmakers": [
            {"key": "draftkings", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Los Angeles Dodgers", "price": -150},
                {"name": "Philadelphia Phillies", "price": 130},
            ]}]},
            {"key": "fanduel", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Los Angeles Dodgers", "price": -145},  # better for Dodgers bettor
                {"name": "Philadelphia Phillies", "price": 125},
            ]}]},
        ]
    }
    best = _best_odds_per_team(game)
    # Best Dodgers line: -145 (lower implied prob, better payout)
    assert best["Los Angeles Dodgers"] == -145
    # Best Phillies line: +130 (lower implied prob, better payout)
    assert best["Philadelphia Phillies"] == 130
```

- [ ] **Step 2: Run the test to verify it fails (file doesn't exist yet)**

```bash
cd ~/Desktop/polyclawd
python3 -m pytest tests/unit/test_baseball_edge.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'odds.baseball_edge'`

- [ ] **Step 3: Create `odds/baseball_edge.py`**

Create the file at `odds/baseball_edge.py`:

```python
"""
MLB Baseball Edge — The Odds API × Polymarket game moneylines

Compares devigged bookmaker odds for MLB game moneylines to Polymarket prices.
The Odds API provides per-game h2h data; Polymarket has game events tagged baseball.

Key difference from soccer_edge.py: markets are per-game (not season futures).
Each game event has exactly one moneyline market where question == event title.

Polymarket game market structure (from Gamma API, tag_slug=baseball):
  Event title: "Philadelphia Phillies vs. Los Angeles Dodgers"
  Moneyline market question: (same as title)
  outcomePrices: ["<first_team_price>", "<second_team_price>"]
  → outcomePrices[0] = P(first-named team wins) as YES price

Usage:
    from baseball_edge import get_baseball_edge_summary
    summary = await get_baseball_edge_summary()
"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests
from loguru import logger

try:
    from .the_odds_api import get_baseball_games_with_odds, _american_to_implied_prob
    from .client import devig_multiway
except ImportError:
    from the_odds_api import get_baseball_games_with_odds, _american_to_implied_prob
    from client import devig_multiway

POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"
DEFAULT_MIN_EDGE = 0.05  # 5%

# Canonical Odds API team name → Polymarket title variants
MLB_TEAM_ALIASES: Dict[str, List[str]] = {
    "Arizona Diamondbacks": ["Arizona Diamondbacks", "Diamondbacks", "D-backs", "AZ"],
    "Atlanta Braves": ["Atlanta Braves", "Braves"],
    "Baltimore Orioles": ["Baltimore Orioles", "Orioles"],
    "Boston Red Sox": ["Boston Red Sox", "Red Sox"],
    "Chicago Cubs": ["Chicago Cubs", "Cubs"],
    "Chicago White Sox": ["Chicago White Sox", "White Sox"],
    "Cincinnati Reds": ["Cincinnati Reds", "Reds"],
    "Cleveland Guardians": ["Cleveland Guardians", "Guardians"],
    "Colorado Rockies": ["Colorado Rockies", "Rockies"],
    "Detroit Tigers": ["Detroit Tigers", "Tigers"],
    "Houston Astros": ["Houston Astros", "Astros"],
    "Kansas City Royals": ["Kansas City Royals", "Royals"],
    "Los Angeles Angels": ["Los Angeles Angels", "Angels"],
    "Los Angeles Dodgers": ["Los Angeles Dodgers", "Dodgers"],
    "Miami Marlins": ["Miami Marlins", "Marlins"],
    "Milwaukee Brewers": ["Milwaukee Brewers", "Brewers"],
    "Minnesota Twins": ["Minnesota Twins", "Twins"],
    "New York Mets": ["New York Mets", "Mets"],
    "New York Yankees": ["New York Yankees", "Yankees"],
    "Oakland Athletics": ["Oakland Athletics", "Athletics", "A's"],
    "Philadelphia Phillies": ["Philadelphia Phillies", "Phillies"],
    "Pittsburgh Pirates": ["Pittsburgh Pirates", "Pirates"],
    "San Diego Padres": ["San Diego Padres", "Padres"],
    "San Francisco Giants": ["San Francisco Giants", "Giants"],
    "Seattle Mariners": ["Seattle Mariners", "Mariners"],
    "St. Louis Cardinals": ["St. Louis Cardinals", "Cardinals"],
    "Tampa Bay Rays": ["Tampa Bay Rays", "Rays"],
    "Texas Rangers": ["Texas Rangers", "Rangers"],
    "Toronto Blue Jays": ["Toronto Blue Jays", "Blue Jays"],
    "Washington Nationals": ["Washington Nationals", "Nationals"],
}


@dataclass
class MLBEdge:
    game_title: str       # "Philadelphia Phillies vs. Los Angeles Dodgers"
    home_team: str
    away_team: str
    bet_team: str         # team this edge is for
    market_type: str      # "moneyline"
    odds_api_prob: float  # devigged bookmaker probability (0-1)
    american_odds: int
    polymarket_price: float  # Polymarket YES price (0-1)
    edge_pct: float       # odds_api_prob - polymarket_price (signed)
    direction: str        # "BUY" or "SELL"
    commence_time: str
    poly_market_id: Optional[str] = None
    poly_event_id: Optional[str] = None


def _team_in_title(team: str, title: str) -> bool:
    """Check if a team name or any alias appears in a Polymarket event title."""
    title_lower = title.lower()
    aliases = MLB_TEAM_ALIASES.get(team, [team])
    return any(alias.lower() in title_lower for alias in aliases)


def _fetch_polymarket_baseball_sync() -> List[Dict]:
    """Synchronous fetch of Polymarket baseball events (tag_slug=baseball)."""
    try:
        resp = requests.get(
            f"{POLYMARKET_GAMMA}/events",
            params={"closed": "false", "tag_slug": "baseball", "limit": "100"},
            timeout=30,
        )
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"Polymarket baseball fetch failed: {e}")
        return []


async def get_polymarket_baseball_events() -> List[Dict]:
    """Async wrapper for Polymarket baseball event fetch."""
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as pool:
        return await loop.run_in_executor(pool, _fetch_polymarket_baseball_sync)


def _find_matching_event(
    home_team: str, away_team: str, poly_events: List[Dict]
) -> Optional[Dict]:
    """
    Find the Polymarket event for a specific game.
    Titles follow "[Team A] vs. [Team B]" — both teams must appear in the title.
    Skips non-game events (no " vs. " in title).
    """
    for event in poly_events:
        title = event.get("title", "")
        if " vs. " not in title:
            continue
        if _team_in_title(home_team, title) and _team_in_title(away_team, title):
            return event
    return None


def _extract_moneyline_prices(
    event: Dict, home_team: str, away_team: str
) -> Optional[Tuple[float, float, str]]:
    """
    Extract (home_price, away_price, market_id) from a game event's moneyline market.

    The moneyline market has question == event title.
    outcomePrices[0] = first team in title; outcomePrices[1] = second team.
    Rejects prices of 0 (stale/settled markets).
    Returns None if no valid moneyline market found.
    """
    title = event.get("title", "")
    for market in event.get("markets", []):
        if market.get("question", "") != title:
            continue

        prices_raw = market.get("outcomePrices", "[]")
        if isinstance(prices_raw, str):
            try:
                prices = json.loads(prices_raw)
            except (json.JSONDecodeError, ValueError):
                continue
        else:
            prices = prices_raw

        if len(prices) < 2:
            continue

        try:
            price0 = float(prices[0])
            price1 = float(prices[1])
        except (ValueError, TypeError):
            continue

        if price0 <= 0 or price1 <= 0:
            continue

        # Determine which price belongs to home vs away
        # Title format: "[first_team] vs. [second_team]"
        first_team_fragment = title.split(" vs. ")[0].strip()
        first_is_home = _team_in_title(home_team, first_team_fragment)

        if first_is_home:
            return price0, price1, market.get("id", "")
        else:
            return price1, price0, market.get("id", "")

    return None


def _devig_two_way(odds_a: int, odds_b: int) -> Tuple[float, float]:
    """
    Remove bookmaker vig from a two-outcome market.
    Returns (true_prob_a, true_prob_b) that sum to exactly 1.0.
    """
    p_a = _american_to_implied_prob(odds_a)
    p_b = _american_to_implied_prob(odds_b)
    total = p_a + p_b
    return p_a / total, p_b / total


def _best_odds_per_team(game: Dict) -> Dict[str, int]:
    """
    Find best available h2h American odds per team across all bookmakers in a game.
    "Best" = lowest implied probability (best payout for the bettor).
    """
    best: Dict[str, int] = {}
    for bookmaker in game.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market.get("key") != "h2h":
                continue
            for outcome in market.get("outcomes", []):
                name = outcome.get("name")
                price = outcome.get("price")
                if name is None or price is None:
                    continue
                price = int(price)
                existing = best.get(name)
                if existing is None or _american_to_implied_prob(price) < _american_to_implied_prob(existing):
                    best[name] = price
    return best


async def find_baseball_edges(min_edge: float = DEFAULT_MIN_EDGE) -> List[MLBEdge]:
    """
    Compute edges between devigged bookmaker moneylines and Polymarket game prices.

    Flow:
      1. Fetch Odds API MLB game list (home_team, away_team, bookmakers)
      2. Fetch Polymarket baseball events (tag_slug=baseball)
      3. Match each game to its Polymarket event by team name
      4. Extract moneyline prices from both sources
      5. Devig bookmaker odds, compare to Polymarket price
      6. Return signals where |edge| >= min_edge, sorted by |edge| desc

    Returns [] if ODDS_API_KEY not set or no games today.
    """
    edges: List[MLBEdge] = []

    # Parallel fetch
    odds_games, poly_events = await asyncio.gather(
        get_baseball_games_with_odds(),
        get_polymarket_baseball_events(),
    )

    if not odds_games:
        logger.warning("baseball_edge: no Odds API game data (key missing or no games today)")
        return edges

    game_events = [e for e in poly_events if " vs. " in e.get("title", "")]
    logger.info(
        f"baseball_edge: {len(odds_games)} Odds API games, "
        f"{len(game_events)} Polymarket game events"
    )

    for game in odds_games:
        home_team = game.get("home_team", "")
        away_team = game.get("away_team", "")
        commence_time = game.get("commence_time", "")

        if not home_team or not away_team:
            continue

        event = _find_matching_event(home_team, away_team, game_events)
        if not event:
            logger.debug(f"baseball_edge: no Polymarket match for {away_team} @ {home_team}")
            continue

        team_odds = _best_odds_per_team(game)
        home_odds = team_odds.get(home_team)
        away_odds = team_odds.get(away_team)
        if not home_odds or not away_odds:
            continue

        home_true_prob, away_true_prob = _devig_two_way(home_odds, away_odds)

        prices = _extract_moneyline_prices(event, home_team, away_team)
        if not prices:
            logger.debug(
                f"baseball_edge: no moneyline market for '{event.get('title')}'"
            )
            continue

        home_poly_price, away_poly_price, market_id = prices

        for team, true_prob, poly_price, american_odds in [
            (home_team, home_true_prob, home_poly_price, home_odds),
            (away_team, away_true_prob, away_poly_price, away_odds),
        ]:
            edge = true_prob - poly_price
            if abs(edge) >= min_edge:
                edges.append(MLBEdge(
                    game_title=event.get("title", f"{away_team} vs. {home_team}"),
                    home_team=home_team,
                    away_team=away_team,
                    bet_team=team,
                    market_type="moneyline",
                    odds_api_prob=true_prob,
                    american_odds=american_odds,
                    polymarket_price=poly_price,
                    edge_pct=edge,
                    direction="BUY" if edge > 0 else "SELL",
                    commence_time=commence_time,
                    poly_market_id=market_id,
                    poly_event_id=event.get("id"),
                ))

    edges.sort(key=lambda e: abs(e.edge_pct), reverse=True)
    return edges


async def get_baseball_edge_summary() -> Dict:
    """
    MLB edge summary for `/api/baseball/edge` response.
    Mirrors get_soccer_edge_summary() shape.
    """
    edges = await find_baseball_edges()

    return {
        "source": "the_odds_api_baseball",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_edges": len(edges),
        "edges": [
            {
                "game": e.game_title,
                "team": e.bet_team,
                "market_type": e.market_type,
                "odds_api_prob": round(e.odds_api_prob * 100, 1),
                "american_odds": f"{e.american_odds:+d}",
                "polymarket_price": round(e.polymarket_price * 100, 1),
                "edge_pct": round(e.edge_pct * 100, 1),
                "direction": e.direction,
                "commence_time": e.commence_time,
                "market_id": e.poly_market_id,
                "event_id": e.poly_event_id,
            }
            for e in edges
        ],
        "top_opportunities": [
            {
                "game": e.game_title,
                "team": e.bet_team,
                "edge": f"{e.edge_pct * 100:+.1f}%",
                "action": f"{e.direction} at {e.polymarket_price * 100:.0f}¢",
            }
            for e in edges[:5]
        ],
    }


if __name__ == "__main__":
    async def _test():
        summary = await get_baseball_edge_summary()
        print(f"Found {summary['total_edges']} edges")
        for e in summary["edges"][:10]:
            sign = "+" if e["edge_pct"] > 0 else ""
            print(
                f"  {e['team']} ({e['game']}): "
                f"Odds {e['odds_api_prob']}% vs Poly {e['polymarket_price']}¢ "
                f"→ {sign}{e['edge_pct']}% {e['direction']}"
            )

    asyncio.run(_test())
```

- [ ] **Step 4: Run tests — all must pass**

```bash
cd ~/Desktop/polyclawd
python3 -m pytest tests/unit/test_baseball_edge.py -v
```

Expected: All tests PASS (no Odds API call needed — tests use fixtures)

- [ ] **Step 5: Commit**

```bash
git add odds/baseball_edge.py tests/unit/test_baseball_edge.py
git commit -m "feat: add baseball_edge.py with Odds API × Polymarket game matching"
```

---

## Task 3: Create `odds/mlb_lineups.py`

**Files:**
- Create: `odds/mlb_lineups.py`

- [ ] **Step 1: Create the file**

```python
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
```

- [ ] **Step 2: Verify MLB Stats API is live and the module imports**

```bash
cd ~/Desktop/polyclawd
python3 odds/mlb_lineups.py
```

Expected: Prints today's games (e.g. `Found N games`, with matchups)

- [ ] **Step 3: Commit**

```bash
git add odds/mlb_lineups.py
git commit -m "feat: add mlb_lineups.py - MLB Stats API lineup fetcher"
```

---

## Task 4: Add `/api/baseball/edge` endpoint

**Files:**
- Modify: `api/routes/markets.py`

- [ ] **Step 1: Add the endpoint after the `/vegas/worldcup` route (around line 771)**

Find this block in `api/routes/markets.py`:

```python
@router.get("/vegas/worldcup")
```

Add the following AFTER the worldcup route's `return` statement (after `return await handle_edge_request("worldcup", _get_league_edges("world_cup", min_edge))`):

```python

# ----------------------------------------------------------------------------
# MLB Baseball Edge
# ----------------------------------------------------------------------------

@router.get("/baseball/edge")
async def get_baseball_edge(min_edge: float = Query(default=0.05, ge=0, le=1)):
    """MLB moneyline edges: devigged The Odds API vs Polymarket game markets.

    Data sources:
      - The Odds API baseball_mlb h2h (requires ODDS_API_KEY env var)
      - Polymarket Gamma API tag_slug=baseball game events

    Edge = devigged bookmaker probability - Polymarket bestAsk (YES price).
    Only returns |edge| >= min_edge (default 5%).

    Returns:
      {source, timestamp, total_edges, edges: [...], top_opportunities: [...]}
    """
    async def _get_baseball():
        import sys
        odds_path = _get_odds_modules_path()
        if odds_path not in sys.path:
            sys.path.insert(0, odds_path)
        from baseball_edge import get_baseball_edge_summary
        return await get_baseball_edge_summary()

    return await handle_edge_request("baseball", _get_baseball())
```

- [ ] **Step 2: Verify the route is registered (imports clean)**

```bash
cd ~/Desktop/polyclawd
python3 -c "from api.routes.markets import router; routes = [r.path for r in router.routes]; assert '/baseball/edge' in routes, routes; print('Route registered OK')"
```

Expected: `Route registered OK`

- [ ] **Step 3: Start the local dev server and hit the endpoint**

```bash
cd ~/Desktop/polyclawd
uvicorn api.main:app --port 8421 --reload &
sleep 3
curl -s http://localhost:8421/api/baseball/edge | python3 -m json.tool | head -30
```

Expected: JSON with `source`, `total_edges`, `edges` keys. `total_edges` may be 0 if Odds API key has no credits yet — that's fine. Should NOT 500 or 503.

Kill the dev server after: `pkill -f "uvicorn api.main:app --port 8421"`

- [ ] **Step 4: Commit**

```bash
git add api/routes/markets.py
git commit -m "feat: add /api/baseball/edge endpoint"
```

---

## Task 5: Wire baseball into edge cache

**Files:**
- Modify: `api/edge_cache.py`

- [ ] **Step 1: Add `fetch_baseball_edges()` function**

In `api/edge_cache.py`, add this function after `fetch_soccer_edges()`:

```python
def fetch_baseball_edges() -> List[Dict]:
    """Fetch MLB baseball edge signals from /api/baseball/edge."""
    signals = []
    try:
        resp = urllib.request.urlopen(
            urllib.request.Request(
                "http://localhost:8420/api/baseball/edge?min_edge=0.05",
                headers={"User-Agent": "EdgeCache/1.0"},
            ),
            timeout=25,
        )
        data = json.loads(resp.read().decode())
        for edge in data.get("edges", [])[:5]:
            edge_pct = edge.get("edge_pct", 0)
            if abs(edge_pct) >= 5:
                side = "YES" if edge_pct > 0 else "NO"
                signals.append({
                    "source": "baseball_edge",
                    "platform": "polymarket",
                    "market": f"{edge.get('team', '')} - {edge.get('game', '')}",
                    "market_id": edge.get("market_id"),
                    "side": side,
                    "confidence": min(65, abs(edge_pct) * 2.5),
                    "value": abs(edge_pct),
                    "reasoning": (
                        f"Odds API {edge.get('odds_api_prob', 0):.0f}% "
                        f"({edge.get('american_odds', '')}) "
                        f"vs Poly {edge.get('polymarket_price', 0):.0f}¢ "
                        f"({edge_pct:+.1f}% edge)"
                    ),
                    "price": edge.get("polymarket_price", 50) / 100,
                })
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        logger.debug(f"Baseball edge fetch failed (expected if service down): {e}")
    except Exception as e:
        logger.exception(f"Unexpected error fetching baseball edges: {e}")
    return signals
```

- [ ] **Step 2: Wire into `refresh_edge_cache()`**

In `refresh_edge_cache()`, add `fetch_baseball_edges()` after `fetch_soccer_edges()`:

Find:
```python
    all_signals.extend(fetch_soccer_edges())
    all_signals.extend(fetch_manifold_edges())
```

Replace with:
```python
    all_signals.extend(fetch_soccer_edges())
    all_signals.extend(fetch_baseball_edges())
    all_signals.extend(fetch_manifold_edges())
```

- [ ] **Step 3: Verify import**

```bash
cd ~/Desktop/polyclawd
python3 -c "from api.edge_cache import fetch_baseball_edges; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add api/edge_cache.py
git commit -m "feat: wire fetch_baseball_edges into edge cache"
```

---

## Task 6: Deploy to VPS

**Files:**
- Deploy to VPS at `/var/www/virtuosocrypto.com/polyclawd/`

- [ ] **Step 1: Push local commits**

```bash
cd ~/Desktop/polyclawd
git push origin chore/cleanup-2026-05-29
```

- [ ] **Step 2: Copy new and modified files to VPS**

```bash
scp odds/baseball_edge.py vps:/var/www/virtuosocrypto.com/polyclawd/odds/
scp odds/mlb_lineups.py vps:/var/www/virtuosocrypto.com/polyclawd/odds/
scp odds/the_odds_api.py vps:/var/www/virtuosocrypto.com/polyclawd/odds/
scp api/routes/markets.py vps:/var/www/virtuosocrypto.com/polyclawd/api/routes/
scp api/edge_cache.py vps:/var/www/virtuosocrypto.com/polyclawd/api/
```

- [ ] **Step 3: Restart the service**

```bash
ssh vps "sudo systemctl restart polyclawd-api"
sleep 5
ssh vps "sudo systemctl status polyclawd-api --no-pager | head -10"
```

Expected: `active (running)`

- [ ] **Step 4: Verify the new endpoint is live**

```bash
curl -s https://virtuosocrypto.com/polyclawd/api/baseball/edge | python3 -m json.tool | head -20
```

Expected: JSON with `source: "the_odds_api_baseball"` and `total_edges` (may be 0 until Odds API credits are active). Should NOT return 500 or 503.

- [ ] **Step 5: Confirm ODDS_API_KEY is set on VPS (required for live signals)**

```bash
ssh vps "sudo systemctl show polyclawd-api --property=Environment | grep ODDS_API_KEY | head -c 50"
```

If empty: `ssh vps 'sudo systemctl set-environment ODDS_API_KEY=<your_key> && sudo systemctl restart polyclawd-api'`

- [ ] **Step 6: Smoke-test MLB Stats API endpoint**

```bash
ssh vps "cd /var/www/virtuosocrypto.com/polyclawd && venv/bin/python3 odds/mlb_lineups.py"
```

Expected: Today's game list printed with gamePks.

---

## Validation: First Signal Day

Once Odds API key is active ($30/mo upgraded), run this to confirm end-to-end:

```bash
# Hit the live endpoint
curl -s https://virtuosocrypto.com/polyclawd/api/baseball/edge | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f'Edges: {d[\"total_edges\"]}')
for e in d.get(\"top_opportunities\", [])[:3]:
    print(f'  {e[\"team\"]} ({e[\"game\"]}): {e[\"edge\"]} → {e[\"action\"]}')
"
```

If `total_edges > 0` and edges look reasonable (not 0% poly prices, not 99% odds), shadow logging is the next step (shadow_tracker integration is tracked separately).

---

## Self-Review Checklist

- [x] **Spec coverage**: Odds ingestion ✓, Polymarket scanning ✓, edge computation ✓, lineup fetcher ✓, endpoint ✓, edge cache ✓
- [x] **No placeholders**: All code blocks are complete and runnable
- [x] **Type consistency**: `MLBEdge` used throughout, `_american_to_implied_prob` imported from `the_odds_api`, `devig_multiway` imported from `client` (but not used — `_devig_two_way` handles two-way markets directly, which is more precise)
- [x] **Field names**: `odds_api_prob`, `polymarket_price`, `edge_pct` consistent across `MLBEdge`, `get_baseball_edge_summary()`, and `fetch_baseball_edges()` in edge_cache
- [x] **Out of scope confirmed not included**: Player prop lineup guard is present as `mlb_lineups.py` stub (callable) but not wired into baseball_edge.py moneyline logic — correct per scope (guard needed only for player props, not game lines)

"""
MLB player-props scanner for the dashboard Player Props tab (baseball.html).

Serves GET /api/baseball/props. Fetches batter_*/pitcher_* prop markets from The
Odds API event-odds endpoint and shapes them into the JSON contract that
renderProps() in static/baseball.html expects:

    {
      "source": "the_odds_api_mlb_props",
      "timestamp": "<ISO>",
      "credit_remaining": <int|null>,
      "games": [
        {
          "away_team": str, "home_team": str,
          "away_pitcher": str, "home_pitcher": str,   # best-effort (MLB StatsAPI)
          "commence_time": "<ISO>",
          "props": {
            "batter_home_runs": [
              {"player","book","line","over_odds","over_ip","under_odds","under_ip"},
              ...
            ],
            ...
          }
        }, ...
      ]
    }

Cost discipline (the reason the standalone reconcile script existed but this
endpoint never did): MLB props use batter_*/pitcher_* keys (NOT player_*, which
are NFL/NBA and return 422). Each event-odds call is billed (markets-with-data x
regions), but passing `bookmakers=` collapses the region dimension to 1 unit. We
also (a) only fetch games in the next PROP_WINDOW_HOURS, (b) cap at MAX_GAMES,
(c) cache the whole result for CACHE_TTL_S, and (d) hard-stop below CREDIT_FLOOR.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from loguru import logger

# Dual import: works whether this module is loaded as `odds.mlb_props` (package)
# or flat after a sys.path insert (matches the soccer/UFC engine precedent).
try:
    from .odds_api_fetch import get_event_markets, get_games_with_markets, upcoming_window
except ImportError:  # pragma: no cover
    from odds_api_fetch import get_event_markets, get_games_with_markets, upcoming_window

try:
    try:
        from .the_odds_api import get_credit_status
    except ImportError:  # pragma: no cover
        from the_odds_api import get_credit_status
except Exception:  # pragma: no cover - keep scanner importable in isolation

    def get_credit_status() -> dict:
        return {"remaining": None}


SOURCE = "the_odds_api_mlb_props"
MLB_KEY = "baseball_mlb"

# Only the 5 markets baseball.html actually renders (marketOrder). Fewer markets
# = fewer billed units per event.
PROP_MARKETS: List[str] = [
    "batter_home_runs",
    "pitcher_strikeouts",
    "batter_hits",
    "batter_rbis",
    "batter_total_bases",
]

# Sharp books we trust, mapped to The Odds API bookmaker keys. Passing this list
# as `bookmakers=` bills the event-odds call as a single region unit.
SHARP_BOOK_KEYS = "pinnacle,fanduel,draftkings,betmgm,williamhill_us"
# API bookmaker key -> display title used in the UI badge.
BOOK_TITLES = {
    "pinnacle": "Pinnacle",
    "fanduel": "FanDuel",
    "draftkings": "DraftKings",
    "betmgm": "BetMGM",
    "williamhill_us": "Caesars",
}

PROP_WINDOW_HOURS = 30  # next full slate — in the evening, tonight's games have
#                         started, so 14h shows nothing; 30h surfaces tomorrow's
#                         upcoming games (books post props ~day-of). Empty-prop
#                         games are filtered out, so the extra reach is ~free.
MAX_GAMES = 12  # cap event-odds calls per refresh (one full MLB slate)
CACHE_TTL_S = 600  # 10 min — dashboard re-loads reuse this, no new credits
CREDIT_FLOOR = 300  # below this remaining, do not spend on props

_CACHE: Dict[str, object] = {"ts": 0.0, "data": None}


def _american_to_ip(price: int) -> float:
    """American odds -> implied probability percent (1 decimal)."""
    try:
        o = int(price)
    except (TypeError, ValueError):
        return 0.0
    ip = (100.0 / (o + 100.0)) if o > 0 else (abs(o) / (abs(o) + 100.0))
    return round(ip * 100, 1)


def _fmt_american(price) -> str:
    try:
        o = int(price)
    except (TypeError, ValueError):
        return "—"
    return f"+{o}" if o > 0 else str(o)


def _parse_event_props(event: Dict) -> Dict[str, List[Dict]]:
    """Turn one event-odds payload into {market_key: [prop rows]} for sharp books.

    Outcomes for a prop market arrive as Over/Under pairs per player, with the
    player's name in `description`. We pair them by (book, description).
    """
    out: Dict[str, List[Dict]] = {}
    for bk in event.get("bookmakers", []):
        title = BOOK_TITLES.get(bk.get("key", ""), bk.get("title", bk.get("key", "")))
        for market in bk.get("markets", []):
            mkt = market.get("key")
            if mkt not in PROP_MARKETS:
                continue
            outcomes = market.get("outcomes", []) or []
            # Group Over/Under by player description.
            by_player: Dict[str, Dict[str, Dict]] = {}
            for o in outcomes:
                player = o.get("description") or o.get("name") or "?"
                side = (o.get("name") or "").lower()  # "over" / "under"
                by_player.setdefault(player, {})[side] = o
            rows = out.setdefault(mkt, [])
            for player, sides in by_player.items():
                over = sides.get("over")
                under = sides.get("under")
                ref = over or under
                if not ref:
                    continue
                rows.append(
                    {
                        "player": player,
                        "book": title,
                        "line": ref.get("point", "—"),
                        "over_odds": _fmt_american(over.get("price")) if over else "—",
                        "over_ip": _american_to_ip(over.get("price")) if over else 0.0,
                        "under_odds": _fmt_american(under.get("price")) if under else "—",
                        "under_ip": _american_to_ip(under.get("price")) if under else 0.0,
                    }
                )
    # Stable sort within each market: book then player.
    for mkt in out:
        out[mkt].sort(key=lambda r: (r["book"], r["player"]))
    return out


def _probable_pitchers() -> Dict[str, str]:
    """Best-effort {lower-team-name-word: pitcher} from MLB StatsAPI (free, no
    Odds credit). Returns {} on any failure — pitchers default to TBD in the UI."""
    import json
    import urllib.request

    pitchers: Dict[str, str] = {}
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&hydrate=probablePitcher&date={today}"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        for d in data.get("dates", []):
            for g in d.get("games", []):
                for side in ("home", "away"):
                    team = g.get("teams", {}).get(side, {})
                    name = (team.get("team", {}).get("name", "") or "").lower()
                    pitcher = team.get("probablePitcher", {}).get("fullName", "")
                    if name and pitcher:
                        # index by each significant word so odds-team names match
                        for w in name.split():
                            if len(w) > 3:
                                pitchers[w] = pitcher
    except Exception as e:  # pragma: no cover
        logger.debug(f"mlb_props: probable-pitcher enrich skipped — {e}")
    return pitchers


def _match_pitcher(team_name: str, pitchers: Dict[str, str]) -> str:
    for w in (team_name or "").lower().split():
        if len(w) > 3 and w in pitchers:
            return pitchers[w]
    return "TBD"


async def get_mlb_props(force: bool = False) -> Dict:
    """Return the dashboard props payload, cached for CACHE_TTL_S.

    Never raises — returns an empty `games` list (and a `note`) on any problem so
    the dashboard degrades quietly.
    """
    now = time.time()
    if not force and _CACHE["data"] is not None and (now - float(_CACHE["ts"])) < CACHE_TTL_S:
        return _CACHE["data"]  # type: ignore[return-value]

    ts_iso = datetime.now(timezone.utc).isoformat()
    remaining = get_credit_status().get("remaining")
    # Populate the credit balance if nothing has read it yet this process. The
    # /sports endpoint is free, so this makes the floor guard + credit_remaining
    # field reliable without spending a credit.
    if remaining is None:
        try:
            try:
                from .the_odds_api import refresh_credit_balance
            except ImportError:  # pragma: no cover
                from the_odds_api import refresh_credit_balance
            remaining = refresh_credit_balance().get("remaining")
        except Exception as e:  # pragma: no cover
            logger.debug(f"mlb_props: credit refresh skipped — {e}")

    # Credit floor guard — serve stale cache if we have it, else empty.
    if isinstance(remaining, int) and remaining < CREDIT_FLOOR:
        logger.warning(f"mlb_props: credit floor hit ({remaining} < {CREDIT_FLOOR}) — skipping fetch")
        if _CACHE["data"] is not None:
            return _CACHE["data"]  # type: ignore[return-value]
        return {
            "source": SOURCE,
            "timestamp": ts_iso,
            "credit_remaining": remaining,
            "games": [],
            "note": "credit floor reached — props paused",
        }

    cf, ct = upcoming_window(PROP_WINDOW_HOURS)
    # 1 credit: list upcoming games (event ids + teams + commence) via sharp book.
    events = await get_games_with_markets(
        MLB_KEY,
        markets="h2h",
        regions="us",
        bookmakers="pinnacle",
        commence_from=cf,
        commence_to=ct,
    )
    events = sorted(events, key=lambda g: g.get("commence_time", ""))[:MAX_GAMES]

    pitchers = _probable_pitchers()
    games: List[Dict] = []
    for ev in events:
        eid = ev.get("id")
        if not eid:
            continue
        payload = await get_event_markets(
            MLB_KEY,
            eid,
            markets=",".join(PROP_MARKETS),
            regions="us",
            bookmakers=SHARP_BOOK_KEYS,
        )
        props = _parse_event_props(payload) if payload else {}
        # Skip games no book has posted props for yet (keeps the tab clean).
        if not any(props.get(m) for m in PROP_MARKETS):
            continue
        away = ev.get("away_team", "Away")
        home = ev.get("home_team", "Home")
        games.append(
            {
                "away_team": away,
                "home_team": home,
                "away_pitcher": _match_pitcher(away, pitchers),
                "home_pitcher": _match_pitcher(home, pitchers),
                "commence_time": ev.get("commence_time", ""),
                "props": props,
            }
        )

    result = {
        "source": SOURCE,
        "timestamp": ts_iso,
        "credit_remaining": get_credit_status().get("remaining"),
        "games": games,
    }
    _CACHE["data"] = result
    _CACHE["ts"] = now
    return result

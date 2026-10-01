"""
Pinnacle guest-API adapter — free sharp-book lines for MLB props + game lines.

Replaces The Odds API (key dead 2026-09-30) as the sharp anchor for the prop
scout / dashboard / alert pipeline. Undocumented endpoints, verified live
2026-09-30:

  GET https://guest.api.arcadia.pinnacle.com/0.1/leagues/{id}/matchups
      → all league matchups: type "matchup" (games) and "special" (props and
        futures; player props carry parent.id = game id, Over/Under participants)
  GET https://guest.api.arcadia.pinnacle.com/0.1/leagues/{id}/markets/straight
      → ALL prices (game lines + specials) in ONE unauthenticated bulk call

No auth on the bulk endpoints (verified: no header / garbage key / real key all
200). Per-matchup endpoints DO require a valid X-API-Key — avoided entirely.
Rate limits: none observed at 10 rapid calls; we make 2 calls per 10-min cache
window. Coverage: batter HR / TB, pitcher K / ER / hits allowed / pitching outs.
No batter hits / RBI (Pinnacle does not post those as specials).

Output contract = odds/mlb_props.get_mlb_props() exactly, so the scout,
dashboard, and alert pipeline consume it unchanged. Kill switch:
POLYCLAWD_PROP_SOURCE=odds_api reverts the seam without a redeploy.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from loguru import logger

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

SOURCE = "pinnacle_guest_api"
BASE_URL = "https://guest.api.arcadia.pinnacle.com/0.1"
MLB_LEAGUE_ID = 246  # verified: sport Baseball=3, league MLB=246
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
TIMEOUT_S = 10
CACHE_TTL_S = 600  # 10 min — matches mlb_props / scout cache TTLs

# Description suffix → contract market key. Longest/most-specific first.
# Exact endswith match — prevents "Exact Total Runs" / "Total Runs Range" /
# team-total specials from matching a player-prop suffix.
_SUFFIX_MAP: List[Tuple[str, str]] = [
    ("Total Pitching Outs", "pitcher_outs"),
    ("Total Hits Allowed", "pitcher_hits_allowed"),
    ("Total Earned Runs", "pitcher_earned_runs"),
    ("Total Strikeouts", "pitcher_strikeouts"),
    ("Total Home Runs", "batter_home_runs"),
    ("Total Bases", "batter_total_bases"),
]

_CACHE: Dict[str, object] = {"ts": 0.0, "data": None}


def _get(url: str):
    """GET one guest-API URL → parsed JSON, or None on any failure. Never raises."""
    if requests is None:  # pragma: no cover
        return None
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT_S)
        if resp.status_code != 200:
            logger.warning(f"pinnacle_fetch: {url.rsplit('/', 1)[-1]} → HTTP {resp.status_code}")
            return None
        return resp.json()
    except Exception as e:
        logger.warning(f"pinnacle_fetch: fetch failed — {e}")
        return None


def parse_special_description(desc) -> Tuple[Optional[str], Optional[str]]:
    """'<Player> Total <Stat>' → (player, market_key); (None, None) if not a
    player prop (futures, team totals, odd/even, ranges, margins, 1st-half)."""
    if not isinstance(desc, str):
        return (None, None)
    d = desc.strip()
    for suffix, key in _SUFFIX_MAP:
        if d.endswith(suffix):
            player = d[: -len(suffix)].strip()
            if player:
                return (player, key)
            return (None, None)
    return (None, None)


def build_props_payload(
    matchups,
    straight,
    probable_pitchers: Optional[Dict[str, str]] = None,
    now: Optional[str] = None,
) -> Dict:
    """Turn guest-API payloads into the mlb_props contract. Never raises.

    matchups: /leagues/{id}/matchups list (games + specials)
    straight: /leagues/{id}/markets/straight list (all priced rows)
    probable_pitchers: {lower-team-word: pitcher name} for display fields
    now: ISO timestamp for the 30h window filter (tests); None = real clock
    """
    from odds.mlb_props import PROP_WINDOW_HOURS, _american_to_ip, _fmt_american, _match_pitcher

    probable_pitchers = probable_pitchers or {}
    ts = now or datetime.now(timezone.utc).isoformat()
    payload: Dict = {"source": SOURCE, "timestamp": ts, "credit_remaining": None, "games": []}

    try:
        now_dt = (
            datetime.now(timezone.utc)
            if now is None
            else datetime.fromisoformat(str(now).replace("Z", "+00:00"))
        )
    except ValueError:
        now_dt = datetime.now(timezone.utc)
    window_end = now_dt + timedelta(hours=PROP_WINDOW_HOURS)

    # Games in window, keyed by id
    games: Dict[int, Dict] = {}
    for m in matchups or []:
        if not isinstance(m, dict) or m.get("type") != "matchup":
            continue
        try:
            start = datetime.fromisoformat(str(m.get("startTime", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if not (now_dt <= start <= window_end):
            continue  # already started or beyond the 30h window
        parts = {}
        for p in m.get("participants") or []:
            if isinstance(p, dict) and p.get("alignment") in ("home", "away") and p.get("name"):
                parts[p["alignment"]] = p["name"]
        if "home" in parts and "away" in parts:
            games[m["id"]] = {
                "away_team": parts["away"],
                "home_team": parts["home"],
                "away_pitcher": "TBD",
                "home_pitcher": "TBD",
                "commence_time": m.get("startTime", ""),
                "props": {},
            }
    if not games:
        return payload

    prices_by_mu: Dict[int, List[Dict]] = {}
    for row in straight or []:
        if isinstance(row, dict) and isinstance(row.get("matchupId"), int):
            prices_by_mu.setdefault(row["matchupId"], []).append(row)

    # Player-prop specials attached to an in-window game
    for m in matchups or []:
        if not isinstance(m, dict) or m.get("type") != "special":
            continue
        parent = m.get("parent")
        parent_id = parent.get("id") if isinstance(parent, dict) else None
        if parent_id not in games:
            continue
        player, market_key = parse_special_description((m.get("special") or {}).get("description"))
        if not player or not market_key:
            continue
        name_by_pid = {
            p.get("id"): p.get("name")
            for p in (m.get("participants") or [])
            if isinstance(p, dict)
        }
        for row in prices_by_mu.get(m["id"]) or []:
            prices = row.get("prices") or []
            over = under = None
            for pr in prices:
                if not isinstance(pr, dict) or pr.get("price") is None:
                    continue
                side = name_by_pid.get(pr.get("participantId"))
                if side == "Over" and over is None:
                    over = pr
                elif side == "Under" and under is None:
                    under = pr
            if over is None or under is None:
                continue  # unpriced or one-sided — skip
            try:
                line = float(over.get("points", 0.5))
            except (TypeError, ValueError):
                line = 0.5
            games[parent_id]["props"].setdefault(market_key, []).append(
                {
                    "player": player,
                    "book": "Pinnacle",
                    "line": line,
                    "over_odds": _fmt_american(over.get("price")),
                    "over_ip": _american_to_ip(over.get("price")),
                    "under_odds": _fmt_american(under.get("price")),
                    "under_ip": _american_to_ip(under.get("price")),
                }
            )
            break  # one priced row per special is all we need

    out_games = [g for g in games.values() if g["props"]]
    for g in out_games:
        g["away_pitcher"] = _match_pitcher(g["away_team"], probable_pitchers)
        g["home_pitcher"] = _match_pitcher(g["home_team"], probable_pitchers)
    out_games.sort(key=lambda g: g.get("commence_time", ""))
    payload["games"] = out_games
    return payload


def get_pinnacle_props(force: bool = False) -> Dict:
    """Fetch + build the props payload from Pinnacle's guest API. Never raises.

    2 bulk HTTP calls per CACHE_TTL_S window; empty payload (with `note`) on
    any failure so the seam can fall back to the Odds API path."""
    now = time.time()
    if (
        not force
        and _CACHE["data"] is not None
        and (now - float(_CACHE["ts"])) < CACHE_TTL_S
    ):
        return _CACHE["data"]  # type: ignore[return-value]

    try:
        matchups = _get(f"{BASE_URL}/leagues/{MLB_LEAGUE_ID}/matchups")
        straight = _get(f"{BASE_URL}/leagues/{MLB_LEAGUE_ID}/markets/straight")
        if not isinstance(matchups, list) or not isinstance(straight, list):
            raise ValueError("guest api returned no payload")
        from odds.mlb_props import _probable_pitchers

        payload = build_props_payload(matchups, straight, _probable_pitchers())
    except Exception as e:
        logger.warning(f"pinnacle_fetch: build failed — {e}")
        return {
            "source": SOURCE,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "credit_remaining": None,
            "games": [],
            "note": f"pinnacle fetch failed: {e}",
        }

    _CACHE["data"] = payload
    _CACHE["ts"] = now
    return payload
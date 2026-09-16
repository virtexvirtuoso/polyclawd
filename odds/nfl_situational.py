#!/usr/bin/env python3
"""nfl_situational.py — NFL situational overlay (Phase 3).

Adds situational factors on top of the Elo team-strength overlay:
  - Rest days (short week penalty)
  - QB availability (ESPN injury feed — QB is ~50% of team value)
  - Weather (temperature via weather_ensemble; wind/rain deferred)

Each factor returns a signed adjustment to the team's win probability. The
adjustments are combined into a single `situational_edge_pct` that the edge
engine can add to the Elo-implied prob (or use as a confidence modifier).

Data sources (all ESPN, no new keys):
  - /scoreboard?dates=...  → game dates for rest-day computation
  - /injuries              → QB availability
  - weather_ensemble       → temperature

Cached aggressively; never hammered per-edge.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests
from loguru import logger

ESPN_API = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
UA = {"User-Agent": "Polyclawd/1.0"}

# ── NFL team → home stadium city (for weather) ───────────────────────
TEAM_CITY: Dict[str, str] = {
    "Arizona Cardinals": "glendale",
    "Atlanta Falcons": "atlanta",
    "Baltimore Ravens": "baltimore",
    "Buffalo Bills": "orchard park",
    "Carolina Panthers": "charlotte",
    "Chicago Bears": "chicago",
    "Cincinnati Bengals": "cincinnati",
    "Cleveland Browns": "cleveland",
    "Dallas Cowboys": "arlington",
    "Denver Broncos": "denver",
    "Detroit Lions": "detroit",
    "Green Bay Packers": "green bay",
    "Houston Texans": "houston",
    "Indianapolis Colts": "indianapolis",
    "Jacksonville Jaguars": "jacksonville",
    "Kansas City Chiefs": "kansas city",
    "Las Vegas Raiders": "las vegas",
    "Los Angeles Chargers": "inglewood",
    "Los Angeles Rams": "inglewood",
    "Miami Dolphins": "miami",
    "Minnesota Vikings": "minneapolis",
    "New England Patriots": "foxborough",
    "New Orleans Saints": "new orleans",
    "New York Giants": "east rutherford",
    "New York Jets": "east rutherford",
    "Philadelphia Eagles": "philadelphia",
    "Pittsburgh Steelers": "pittsburgh",
    "San Francisco 49ers": "santa clara",
    "Seattle Seahawks": "seattle",
    "Tampa Bay Buccaneers": "tampa",
    "Tennessee Titans": "nashville",
    "Washington Commanders": "landover",
}

# ── Tuning ───────────────────────────────────────────────────────
REST_DAY_PENALTY_ELO = 12.0   # short week (< 6 days rest) penalty
QB_OUT_ELO = 50.0             # starting QB out → big swing
QB_DOUBTFUL_ELO = 25.0
COLD_TEMP_ELO = 8.0           # < 40°F → slight underdog boost (low totals)

_CACHE: Dict = {}
_CACHE_TTL_SECONDS = 3600


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _fetch_scoreboard(date_str: str) -> List[Dict]:
    """Fetch games for a date: {home, away, date, home_score, away_score}."""
    try:
        resp = requests.get(
            f"{ESPN_API}/scoreboard", headers=UA,
            params={"dates": date_str}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"ESPN scoreboard fetch {date_str} failed: {e}")
        return []
    games = []
    for event in data.get("events", []):
        comp = event.get("competitions", [{}])[0]
        comps = comp.get("competitors", [])
        if len(comps) != 2:
            continue
        home = next((c for c in comps if c.get("homeAway") == "home"), None)
        away = next((c for c in comps if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        games.append({
            "date": event.get("date", ""),
            "home": home["team"]["displayName"],
            "away": away["team"]["displayName"],
        })
    return games


def _last_game_dates() -> Dict[str, str]:
    """Map team → ISO date of its most recent game (last 14 days)."""
    cached = _CACHE.get("last_games")
    if cached and (time.time() - _CACHE.get("ts", 0)) < _CACHE_TTL_SECONDS:
        return cached
    last: Dict[str, str] = {}
    now = _now_utc()
    for i in range(14):
        d = (now - timedelta(days=i)).strftime("%Y%m%d")
        for g in _fetch_scoreboard(d):
            for team in (g["home"], g["away"]):
                # Keep the most recent (first found going back in time)
                if team not in last:
                    last[team] = g["date"]
    _CACHE["last_games"] = last
    _CACHE["ts"] = time.time()
    return last


def rest_days(team: str, game_date: str,
              last_games: Optional[Dict[str, str]] = None) -> Optional[float]:
    """Days of rest for `team` before `game_date`. None if unknown."""
    last_games = last_games or _last_game_dates()
    last = last_games.get(team)
    if not last:
        return None
    try:
        gd = datetime.fromisoformat(game_date.replace("Z", "+00:00"))
        ld = datetime.fromisoformat(last.replace("Z", "+00:00"))
        return (gd - ld).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return None


def _fetch_injuries() -> List[Dict]:
    """Fetch NFL injury report from ESPN."""
    try:
        resp = requests.get(f"{ESPN_API}/injuries", headers=UA, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"ESPN injuries fetch failed: {e}")
        return []
    injuries = []
    for team in data.get("athletes", []):
        team_name = team.get("team", {}).get("displayName", "Unknown")
        for athlete in team.get("injuries", []):
            player = athlete.get("athlete", {})
            injury = athlete.get("injuries", [{}])[0] if athlete.get("injuries") else {}
            status = injury.get("status", "Unknown")
            pos = player.get("position", {}).get("abbreviation", "")
            if pos == "QB" and status in ("Out", "Doubtful", "Questionable"):
                injuries.append({
                    "player": player.get("displayName", "Unknown"),
                    "team": team_name,
                    "status": status,
                })
    return injuries


def weather_adjustment(city: str, game_date: str) -> Optional[float]:
    """Temperature-based Elo adjustment. None if weather unavailable."""
    try:
        from odds import weather_ensemble as we
        fc = we.get_ensemble_forecast(city, game_date[:10])
        if not fc:
            return None
        high = fc.get("ensemble", {}).get("high_mean_f")
        if high is None:
            return None
        # Cold (< 40°F) → slight underdog boost (low-scoring, more variance)
        if high < 40:
            return COLD_TEMP_ELO
        return 0.0
    except Exception as e:
        logger.debug(f"Weather adjustment failed ({city}): {e}")
        return None


def situational(home_team: str, away_team: str, game_date: str,
                home_city: Optional[str] = None) -> Dict:
    """Compute situational adjustments for a matchup.

    Returns per-team Elo adjustments + a summary. All adjustments are signed
    so a positive value means the team is stronger than raw Elo suggests.
    """
    last_games = _last_game_dates()

    # Rest days
    home_rest = rest_days(home_team, game_date, last_games)
    away_rest = rest_days(away_team, game_date, last_games)

    home_adj = 0.0
    away_adj = 0.0

    # Short-week penalty (only if we know the rest days)
    if home_rest is not None and home_rest < 6:
        home_adj -= REST_DAY_PENALTY_ELO
    if away_rest is not None and away_rest < 6:
        away_adj -= REST_DAY_PENALTY_ELO

    # QB availability
    home_qb = qb_injury(home_team)
    away_qb = qb_injury(away_team)
    if home_qb:
        home_adj -= QB_OUT_ELO if home_qb["status"] == "Out" else QB_DOUBTFUL_ELO
    if away_qb:
        away_adj -= QB_OUT_ELO if away_qb["status"] == "Out" else QB_DOUBTFUL_ELO

    # Weather (home team venue) — only if game is within forecast window
    # (~14 days). Far-future dates 400 the forecast API and trip circuit
    # breakers; skip them.
    if home_city and game_date:
        try:
            gd = datetime.fromisoformat(game_date.replace("Z", "+00:00"))
            if 0 <= (gd - _now_utc()).total_seconds() / 86400.0 <= 14:
                w = weather_adjustment(home_city, game_date)
                if w:
                    home_adj += w  # cold boosts home underdog slightly
        except (ValueError, TypeError):
            pass

    return {
        "home_team": home_team,
        "away_team": away_team,
        "home_rest_days": round(home_rest, 1) if home_rest is not None else None,
        "away_rest_days": round(away_rest, 1) if away_rest is not None else None,
        "home_qb": home_qb,
        "away_qb": away_qb,
        "home_adj_elo": round(home_adj, 1),
        "away_adj_elo": round(away_adj, 1),
        "situational_edge_pct": round((home_adj - away_adj) / 400.0, 4),
    }


def qb_injury(team_name: str) -> Optional[Dict]:
    """QB availability for a team. None if no QB injury."""
    injuries = _CACHE.get("qb_injuries")
    if injuries is None:
        injuries = _fetch_injuries()
        _CACHE["qb_injuries"] = injuries
    for inj in injuries:
        if inj["team"] == team_name:
            return inj
    return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        print(json.dumps(situational(sys.argv[1], sys.argv[2],
                                     datetime.now(timezone.utc).isoformat()),
                         indent=2))

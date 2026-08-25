#!/usr/bin/env python3
"""nfl_strength.py — NFL team-strength overlay (Elo power rating).

Phase 1 of the NFL Team-Strength Overlay spec
(02-Projects/Polyclawd/Strategy/Sports-Props/NFL-Team-Strength-Overlay-Spec-2026-08-22.md).

Builds a lightweight per-team Elo power rating from ESPN season results, then
produces an implied win probability for any matchup. This is the *confirmation
layer* for the devigged moneyline edge engine — it answers "does the devigged
edge align with team strength, or is it market noise?"

Data source: ESPN (free, no key, already wired in espn_odds.py).
  - /scoreboard?dates=YYYYMMDD  → final scores for a date (back-fillable)
  - Iterate over the season's date range to train Elo.

No new API keys. ESPN calls are cached aggressively (refresh <= hourly).

Elo mechanics (NFL-tuned):
  - HFA constant ~55 Elo (home advantage)
  - K-factor ~25 (NFL is low-scoring / high-variance → lower than chess's 32)
  - Seed 1500; burn-in ~4 games before ratings are trusted
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests
from loguru import logger

ESPN_API = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
UA = {"User-Agent": "Polyclawd/1.0"}

# ── Elo tuning constants ─────────────────────────────────────────
START_RATING = 1500.0
HFA = 55.0          # home-field advantage in Elo points
K_FACTOR = 25.0     # NFL: low-scoring, high-variance → lower K than chess
MIN_GAMES_FOR_CONFIDENCE = 4   # burn-in before ratings are trusted

# Cache: ratings are expensive-ish to rebuild (season backfill). Cache per
# process + a short TTL so we don't hammer ESPN on every edge computation.
_CACHE: Dict = {}
_CACHE_TTL_SECONDS = 3600  # refresh ≤ hourly per spec


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def expected(home_r: float, away_r: float) -> float:
    """Expected win prob for home team given ratings (with HFA)."""
    return 1.0 / (1.0 + 10 ** ((away_r - home_r + HFA) / 400.0))


def _update(r: float, exp: float, won: bool) -> float:
    """Standard Elo update. won=True → the team won the game."""
    return r + K_FACTOR * ((1.0 if won else 0.0) - exp)


def _fetch_scoreboard(date_from: str, date_to: str) -> List[Dict]:
    """Fetch final games in a date range (YYYYMMDD-YYYYMMDD).

    ESPN supports a date-range param that returns up to ~100 events per call,
    so a full NFL season collapses to a handful of monthly calls instead of
    ~350 daily calls. Returns list of {home, away, home_score, away_score}.
    """
    try:
        resp = requests.get(
            f"{ESPN_API}/scoreboard",
            headers=UA,
            params={"dates": f"{date_from}-{date_to}"},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"ESPN scoreboard fetch {date_from}-{date_to} failed: {e}")
        return []

    games = []
    for event in data.get("events", []):
        comp = event.get("competitions", [{}])[0]
        state = comp.get("status", {}).get("type", {}).get("name", "")
        if state != "STATUS_FINAL":
            continue  # only completed games train Elo
        comps = comp.get("competitors", [])
        if len(comps) != 2:
            continue
        home = next((c for c in comps if c.get("homeAway") == "home"), None)
        away = next((c for c in comps if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        try:
            games.append({
                "home": home["team"]["displayName"],
                "away": away["team"]["displayName"],
                "home_score": int(home.get("score", 0)),
                "away_score": int(away.get("score", 0)),
            })
        except (KeyError, ValueError, TypeError):
            continue
    return games


def _season_chunks(season_start: Optional[str] = None) -> List[Tuple[str, str]]:
    """(from, to) date-range chunks covering the season, newest-last.

    Chunk by ~1 month so each ESPN call stays under the ~100-event cap.
    Defaults to the current NFL season (Sep 1 → today). Pass season_start
    (e.g. "20250901") to train on a specific season.
    """
    if season_start:
        start = datetime.strptime(season_start, "%Y%m%d").replace(tzinfo=timezone.utc)
    else:
        start = datetime(_now_utc().year, 9, 1, tzinfo=timezone.utc)
    end = _now_utc()
    if end < start:
        return []
    chunks = []
    d = start
    while d <= end:
        chunk_end = min(d + timedelta(days=30), end)
        chunks.append((d.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")))
        d = chunk_end + timedelta(days=1)
    return chunks


def build_ratings(season_start: Optional[str] = None,
                  progress: bool = False) -> Dict[str, float]:
    """Train Elo ratings over a season of ESPN final results.

    Returns {team_display_name: rating}. Teams with no games keep START_RATING.
    """
    ratings: Dict[str, float] = {}
    chunks = _season_chunks(season_start)
    n = len(chunks)
    for i, (dfrom, dto) in enumerate(chunks):
        if progress:
            logger.info(f"Elo training {i + 1}/{n} ({dfrom}-{dto})")
        for g in _fetch_scoreboard(dfrom, dto):
            home, away = g["home"], g["away"]
            hr = ratings.get(home, START_RATING)
            ar = ratings.get(away, START_RATING)
            exp_home = expected(hr, ar)
            home_won = g["home_score"] > g["away_score"]
            ratings[home] = _update(hr, exp_home, home_won)
            ratings[away] = _update(ar, 1.0 - exp_home, not home_won)
    return ratings


def get_ratings(season_start: Optional[str] = None,
                force: bool = False) -> Dict[str, float]:
    """Cached ratings. Refreshes at most hourly unless force=True."""
    now = time.time()
    if not force and _CACHE.get("ratings") and (now - _CACHE.get("ts", 0)) < _CACHE_TTL_SECONDS:
        return _CACHE["ratings"]
    ratings = build_ratings(season_start=season_start)
    _CACHE["ratings"] = ratings
    _CACHE["ts"] = now
    return ratings


def matchup_prob(home_team: str, away_team: str,
                 ratings: Optional[Dict[str, float]] = None) -> Tuple[float, float]:
    """Implied win prob for (home, away) from Elo. Missing teams → 1500 neutral."""
    ratings = ratings or get_ratings()
    hr = ratings.get(home_team, START_RATING)
    ar = ratings.get(away_team, START_RATING)
    ph = expected(hr, ar)
    return ph, 1.0 - ph


def _games_for(ratings: Dict[str, float], team: str) -> int:
    """Approx games played = |rating - START| / (K * 0.5) — rough proxy."""
    return int(abs(ratings.get(team, START_RATING) - START_RATING) / (K_FACTOR * 0.5) + 0.5)


def strength(book_home_prob: float, home_team: str, away_team: str,
             ratings: Optional[Dict[str, float]] = None) -> Dict:
    """Compare devigged book prob vs Elo-implied prob for the home team.

    Returns:
      elo_home, elo_away       — raw ratings
      strength_home_prob       — Elo-implied home win prob
      strength_edge_pct        — (elo_home_prob - book_home_prob), signed
      strength_agree           — True if Elo and book point the same direction
      strength_confidence      — 0..1, ramps up after MIN_GAMES of data
    """
    ratings = ratings or get_ratings()
    hr = ratings.get(home_team, START_RATING)
    ar = ratings.get(away_team, START_RATING)
    elo_home_prob = expected(hr, ar)

    # Confidence: min games played across both teams (data maturity proxy)
    min_games = min(_games_for(ratings, home_team), _games_for(ratings, away_team))
    confidence = min(1.0, min_games / MIN_GAMES_FOR_CONFIDENCE)

    book_home = float(book_home_prob)

    # No data (preseason / no season games yet) → fully neutral. Return None
    # for every overlay field so consumers see blank, not a fake number.
    if confidence <= 0.0:
        return {
            "elo_home": None,
            "elo_away": None,
            "strength_home_prob": None,
            "strength_edge_pct": None,
            "strength_agree": None,
            "strength_confidence": 0.0,
        }

    agree = (elo_home_prob >= 0.5 and book_home >= 0.5) or \
            (elo_home_prob < 0.5 and book_home < 0.5)

    return {
        "elo_home": round(hr, 1),
        "elo_away": round(ar, 1),
        "strength_home_prob": round(elo_home_prob, 4),
        "strength_edge_pct": round(elo_home_prob - book_home, 4),
        "strength_agree": agree,
        "strength_confidence": round(confidence, 3),
    }


# ── Standalone test ──────────────────────────────────────────────────────
if __name__ == "__main__":
    ratings = get_ratings(force=True)
    print(f"Trained {len(ratings)} teams")
    for team, r in sorted(ratings.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {team:24s} {r:7.1f}")
    if len(sys.argv) >= 3:
        h, a = sys.argv[1], sys.argv[2]
        print(json.dumps(strength(0.5, h, a, ratings), indent=2))

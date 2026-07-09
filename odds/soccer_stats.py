#!/usr/bin/env python3
"""soccer_stats.py — Soccer player/team stats from ESPN free API.

Provides goal-scoring rates for scorer edge enrichment.
No API key needed. Rate limit: be polite (cache results, don't hammer).

Data available:
  - Tournament goal leaders (goals, matches, goals_per_match)
  - Tournament assist leaders
  - Team roster with position data

No xG (FBref is behind Cloudflare, paid APIs need keys).
Actual goal rates > xG for scorer props anyway (xG ≠ finishing quality).
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
CACHE_TTL_SECONDS = 3600  # 1h — tournament stats don't change mid-match

# ESPN league slugs for competitions we track
LEAGUE_SLUGS = {
    "soccer_fifa_world_cup": "fifa.world",
    "soccer_epl": "eng.1",
    "soccer_uefa_champs_league": "uefa.champions",
    "soccer_spain_la_liga": "esp.1",
    "soccer_germany_bundesliga": "ger.1",
    "soccer_italy_serie_a": "ita.1",
    "soccer_france_ligue_one": "fra.1",
}

STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"
CACHE_DB = STORAGE_DIR / "soccer_stats_cache.db"


def _canonical(name: str) -> str:
    name = unicodedata.normalize("NFKD", name or "")
    name = "".join(c for c in name if not unicodedata.combining(c)).lower().strip()
    name = re.sub(r"[^\w\s]", "", name)
    parts = name.split()
    return parts[1] if len(parts) == 2 and len(parts[0]) == 1 else name


def _init_cache():
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(CACHE_DB))
    con.execute("""CREATE TABLE IF NOT EXISTS player_goals (
        player_canonical TEXT,
        player_display TEXT,
        sport_key TEXT,
        goals INTEGER,
        assists INTEGER,
        matches INTEGER,
        goals_per_match REAL,
        team_name TEXT,
        fetched_at TEXT,
        PRIMARY KEY (player_canonical, sport_key)
    )""")
    con.commit()
    return con


def _parse_display_value(display: str) -> dict:
    """Parse 'Matches: 2, Goals: 3' or 'Matches: 2, Assists: 3' into dict."""
    out = {}
    for part in display.split(","):
        part = part.strip()
        if ":" in part:
            k, v = part.split(":", 1)
            try:
                out[k.strip().lower()] = int(v.strip())
            except ValueError:
                pass
    return out


def fetch_scorers(sport_key: str, force: bool = False) -> list[dict]:
    """Fetch goal + assist leaders for a competition from ESPN.

    Returns list of dicts with keys:
        player_canonical, player_display, goals, assists, matches,
        goals_per_match, team_name
    """
    slug = LEAGUE_SLUGS.get(sport_key)
    if not slug:
        return []

    con = _init_cache()

    # Check cache freshness
    if not force:
        row = con.execute(
            "SELECT fetched_at FROM player_goals WHERE sport_key=? ORDER BY fetched_at DESC LIMIT 1",
            (sport_key,),
        ).fetchone()
        if row:
            try:
                fetched = datetime.fromisoformat(row[0])
                age = (datetime.now(timezone.utc) - fetched).total_seconds()
                if age < CACHE_TTL_SECONDS:
                    # Return from cache
                    rows = con.execute(
                        "SELECT player_canonical, player_display, goals, assists, matches, goals_per_match, team_name "
                        "FROM player_goals WHERE sport_key=?",
                        (sport_key,),
                    ).fetchall()
                    con.close()
                    return [
                        {
                            "player_canonical": r[0],
                            "player_display": r[1],
                            "goals": r[2],
                            "assists": r[3],
                            "matches": r[4],
                            "goals_per_match": r[5],
                            "team_name": r[6],
                        }
                        for r in rows
                    ]
            except (ValueError, TypeError):
                pass

    # Fetch from ESPN
    url = f"{ESPN_BASE}/{slug}/statistics"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        con.close()
        return [{"error": str(e)}]

    now_iso = datetime.now(timezone.utc).isoformat()

    # Parse goals + assists into a unified player dict
    players: dict[str, dict] = {}  # canonical -> stats

    for cat in data.get("stats", []):
        cat_name = cat.get("name", "")
        for leader in cat.get("leaders", []):
            ath = leader.get("athlete", {})
            display_name = ath.get("displayName", "")
            canonical = _canonical(display_name)
            if not canonical:
                continue

            team = leader.get("team", {})
            parsed = _parse_display_value(leader.get("displayValue", ""))

            if canonical not in players:
                players[canonical] = {
                    "player_canonical": canonical,
                    "player_display": display_name,
                    "goals": 0,
                    "assists": 0,
                    "matches": parsed.get("matches", 0),
                    "team_name": team.get("displayName", ""),
                }

            p = players[canonical]
            if "matches" in parsed and parsed["matches"] > p["matches"]:
                p["matches"] = parsed["matches"]

            if cat_name == "goalsLeaders":
                p["goals"] = parsed.get("goals", int(leader.get("value", 0)))
            elif cat_name == "assistsLeaders":
                p["assists"] = parsed.get("assists", int(leader.get("value", 0)))

    # Compute goals per match
    result = []
    for p in players.values():
        p["goals_per_match"] = round(p["goals"] / max(p["matches"], 1), 3)
        result.append(p)

        # Upsert cache
        con.execute(
            """INSERT OR REPLACE INTO player_goals
               (player_canonical, player_display, sport_key, goals, assists, matches,
                goals_per_match, team_name, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                p["player_canonical"],
                p["player_display"],
                sport_key,
                p["goals"],
                p["assists"],
                p["matches"],
                p["goals_per_match"],
                p["team_name"],
                now_iso,
            ),
        )

    con.commit()
    con.close()
    return sorted(result, key=lambda x: x["goals"], reverse=True)


def lookup_player(player_canonical: str, sport_key: str) -> Optional[dict]:
    """Look up a single player's stats from cache (no fetch)."""
    try:
        con = sqlite3.connect(str(CACHE_DB))
        row = con.execute(
            "SELECT player_canonical, player_display, goals, assists, matches, goals_per_match, team_name "
            "FROM player_goals WHERE player_canonical=? AND sport_key=?",
            (player_canonical, sport_key),
        ).fetchone()
        con.close()
        if row:
            return {
                "player_canonical": row[0],
                "player_display": row[1],
                "goals": row[2],
                "assists": row[3],
                "matches": row[4],
                "goals_per_match": row[5],
                "team_name": row[6],
            }
    except Exception:
        pass
    return None


def enrich_scorer_edge(edge, sport_key: str) -> Optional[dict]:
    """Given a ScorerEdge, look up the player's stats and return enrichment dict
    suitable for edge_enrichment table.

    Returns None if no stats found.
    """
    player_canon = getattr(edge, "player", None)
    if not player_canon:
        return None

    stats = lookup_player(player_canon, sport_key)
    if not stats:
        # Try fetching fresh data
        fetch_scorers(sport_key)
        stats = lookup_player(player_canon, sport_key)

    if not stats:
        return None

    gpg = stats["goals_per_match"]
    consensus_fair = getattr(edge, "consensus_fair", None)

    # Stats confirmation: if goals_per_match > consensus fair prob, stats AGREE
    # that this player scores more than the market implies
    stats_agrees = None
    if consensus_fair is not None and gpg > 0:
        # GPG is goals per match, not probability. But in a single-match context,
        # GPG > implied_prob suggests the player scores more than the market thinks.
        # This is a rough proxy — e.g., GPG=0.5 vs implied 30% → stats say player
        # scores every other game, market says 30%. Stats agree with the edge.
        stats_agrees = gpg > consensus_fair

    return {
        "goals": stats["goals"],
        "assists": stats["assists"],
        "matches": stats["matches"],
        "goals_per_match": gpg,
        "team": stats["team_name"],
        "stats_agrees": stats_agrees,
        "source": "espn_tournament_stats",
    }


if __name__ == "__main__":
    import json

    scorers = fetch_scorers("soccer_fifa_world_cup")
    print(f"World Cup 2026 — {len(scorers)} scorers/assisters:")
    for p in scorers[:15]:
        print(
            f"  {p['player_display']:25s}  {p['goals']}G {p['assists']}A  "
            f"{p['matches']}M  {p['goals_per_match']:.2f} GPG  ({p['team_name']})"
        )

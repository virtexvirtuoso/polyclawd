#!/usr/bin/env python3
"""ufc_stats.py — UFC fighter stats from ESPN free API + Sherdog fallback.

Provides win records, finish rates, and fight history for UFC edge enrichment.
No API key needed. ESPN gives basic records; Sherdog (if accessible) gives
detailed stats (sig strikes, takedowns).

For Phase 2c enrichment: does the fighter's record support or contradict
the market implied probability?
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc"
CACHE_TTL_SECONDS = 86400  # 24h — fighter records don't change mid-event

STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"
CACHE_DB = STORAGE_DIR / "ufc_stats_cache.db"


def _canonical(name: str) -> str:
    name = unicodedata.normalize("NFKD", name or "")
    name = "".join(c for c in name if not unicodedata.combining(c)).lower().strip()
    name = re.sub(r"[^\w\s]", "", name)
    return name


def _init_cache():
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(CACHE_DB), timeout=15)
    con.execute("PRAGMA busy_timeout=8000")
    con.execute("""CREATE TABLE IF NOT EXISTS fighter_records (
        fighter_canonical TEXT PRIMARY KEY,
        fighter_display TEXT,
        wins INTEGER,
        losses INTEGER,
        draws INTEGER,
        win_pct REAL,
        ko_wins INTEGER,
        sub_wins INTEGER,
        dec_wins INTEGER,
        finish_rate REAL,
        team TEXT,
        fetched_at TEXT
    )""")
    con.commit()
    return con


def _parse_record(record_str: str) -> dict:
    """Parse '17-2-0' into {wins, losses, draws}."""
    parts = record_str.strip().split("-")
    try:
        w = int(parts[0]) if len(parts) > 0 else 0
        l = int(parts[1]) if len(parts) > 1 else 0
        d = int(parts[2]) if len(parts) > 2 else 0
        total = max(w + l + d, 1)
        return {"wins": w, "losses": l, "draws": d, "win_pct": round(w / total, 3)}
    except (ValueError, IndexError):
        return {"wins": 0, "losses": 0, "draws": 0, "win_pct": 0.0}


def fetch_event_fighters(force: bool = False) -> list[dict]:
    """Fetch fighter records from the current/upcoming UFC event via ESPN.

    Returns list of dicts with keys:
        fighter_canonical, fighter_display, wins, losses, draws, win_pct, team
    """
    con = _init_cache()

    # Check cache freshness
    if not force:
        row = con.execute(
            "SELECT fetched_at FROM fighter_records ORDER BY fetched_at DESC LIMIT 1",
        ).fetchone()
        if row:
            try:
                fetched = datetime.fromisoformat(row[0])
                age = (datetime.now(timezone.utc) - fetched).total_seconds()
                if age < CACHE_TTL_SECONDS:
                    rows = con.execute(
                        "SELECT fighter_canonical, fighter_display, wins, losses, draws, "
                        "win_pct, ko_wins, sub_wins, dec_wins, finish_rate, team "
                        "FROM fighter_records"
                    ).fetchall()
                    con.close()
                    return [
                        {
                            "fighter_canonical": r[0],
                            "fighter_display": r[1],
                            "wins": r[2], "losses": r[3], "draws": r[4],
                            "win_pct": r[5],
                            "ko_wins": r[6], "sub_wins": r[7], "dec_wins": r[8],
                            "finish_rate": r[9], "team": r[10],
                        }
                        for r in rows
                    ]
            except (ValueError, TypeError):
                pass

    # Fetch from ESPN scoreboard (current/upcoming events)
    result = []
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        r = requests.get(f"{ESPN_BASE}/scoreboard", timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        con.close()
        return [{"error": str(e)}]

    for event in data.get("events", []):
        for comp in event.get("competitions", []):
            for competitor in comp.get("competitors", []):
                athlete = competitor.get("athlete", {})
                name = athlete.get("displayName", "")
                if not name:
                    continue

                records = competitor.get("records", [])
                rec_str = records[0].get("summary", "0-0-0") if records else "0-0-0"
                parsed = _parse_record(rec_str)

                canonical = _canonical(name)
                entry = {
                    "fighter_canonical": canonical,
                    "fighter_display": name,
                    "wins": parsed["wins"],
                    "losses": parsed["losses"],
                    "draws": parsed["draws"],
                    "win_pct": parsed["win_pct"],
                    "ko_wins": 0,  # ESPN doesn't give breakdown
                    "sub_wins": 0,
                    "dec_wins": 0,
                    "finish_rate": 0.0,
                    "team": "",
                }
                result.append(entry)

                # Cache
                con.execute(
                    """INSERT OR REPLACE INTO fighter_records
                       (fighter_canonical, fighter_display, wins, losses, draws,
                        win_pct, ko_wins, sub_wins, dec_wins, finish_rate, team, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (canonical, name, parsed["wins"], parsed["losses"], parsed["draws"],
                     parsed["win_pct"], 0, 0, 0, 0.0, "", now_iso),
                )

    con.commit()
    con.close()
    return result


def lookup_fighter(fighter_canonical: str) -> Optional[dict]:
    """Look up a fighter's stats from cache."""
    try:
        con = sqlite3.connect(str(CACHE_DB), timeout=15)
        con.execute("PRAGMA busy_timeout=8000")
        row = con.execute(
            "SELECT fighter_canonical, fighter_display, wins, losses, draws, "
            "win_pct, ko_wins, sub_wins, dec_wins, finish_rate, team "
            "FROM fighter_records WHERE fighter_canonical=?",
            (fighter_canonical,),
        ).fetchone()
        con.close()
        if row:
            return {
                "fighter_canonical": row[0],
                "fighter_display": row[1],
                "wins": row[2], "losses": row[3], "draws": row[4],
                "win_pct": row[5],
                "ko_wins": row[6], "sub_wins": row[7], "dec_wins": row[8],
                "finish_rate": row[9], "team": row[10],
            }
    except Exception:
        pass
    return None


def enrich_ufc_edge(edge, _sport_key: str = "ufc") -> Optional[dict]:
    """Given a UFC Edge, look up the fighter's record and return enrichment dict.

    Compares win rate vs market implied probability:
    - If fighter's win% >> market implied → stats support the edge
    - If fighter's win% << market implied → stats contradict
    """
    participant = getattr(edge, "participant", None)
    if not participant:
        return None

    canonical = _canonical(participant)
    stats = lookup_fighter(canonical)
    if not stats:
        # Try fetching
        fetch_event_fighters()
        stats = lookup_fighter(canonical)

    if not stats:
        return None

    win_pct = stats["win_pct"]
    book_prob = getattr(edge, "book_prob", None)

    stats_agrees = None
    if book_prob is not None and win_pct > 0:
        direction = getattr(edge, "direction", "BUY")
        if direction == "BUY":
            # We're buying YES on this fighter — stats agree if win% > book prob
            stats_agrees = win_pct > book_prob
        else:
            # We're selling — stats agree if win% < book prob
            stats_agrees = win_pct < book_prob

    return {
        "wins": stats["wins"],
        "losses": stats["losses"],
        "draws": stats["draws"],
        "win_pct": win_pct,
        "finish_rate": stats["finish_rate"],
        "stats_agrees": stats_agrees,
        "source": "espn_fighter_record",
    }


if __name__ == "__main__":
    fighters = fetch_event_fighters()
    print(f"UFC — {len(fighters)} fighters from upcoming events:")
    for f in fighters:
        if "error" in f:
            print(f"  Error: {f['error']}")
            continue
        print(
            f"  {f['fighter_display']:25s}  {f['wins']}-{f['losses']}-{f['draws']}  "
            f"Win%: {f['win_pct']:.1%}"
        )

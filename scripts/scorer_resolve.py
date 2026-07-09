#!/usr/bin/env python3
"""
scorer_resolve.py — resolve goalscorer props against actual match results.

Sources: ESPN scoreboard (free, no API key) → match summary → goal scorers.
Matches player names from our DB against ESPN's scoring summary players.
Fuzzy match on canon() form to handle accent/diacritic discrepancies.

Modes:
  --db DB          SQLite DB to read/write (must have scorer_snapshot table)
  --batch-days N   Fetch ESPN scoreboard for last N days and resolve all props
  --auto           Auto-resolve matches that appear FINAL on ESPN and are un-resolved
  --list-missing   Show unresolved matches
  --stats          Summary of resolution coverage + calibration buckets

Run:    python3 scripts/scorer_resolve.py --db storage/scorer_clv_corrected.db --batch-days 30
Cron:   python3 scripts/scorer_resolve.py --db storage/scorer_clv_corrected.db --auto
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import unicodedata
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

ESPN_SOCCER = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"


def canon(name: str) -> str:
    """NFKD-normalized, lowercase, diacritics-removed canonical form."""
    name = unicodedata.normalize("NFKD", name or "")
    name = "".join(c for c in name if not unicodedata.combining(c)).lower().strip()
    name = re.sub(r"[^\w\s\-']", "", name)
    name = re.sub(r"\s+", " ", name)  # collapse multiple spaces
    # Country name normalizations for ESPN-vs-Odds-API mismatches
    name = name.replace("congo dr", "dr congo")
    name = name.replace("czechia", "czech republic")
    name = name.replace("turkiye", "turkey")
    name = name.replace("türkiye", "turkey")
    name = name.replace("bosnia-herzegovina", "bosnia")
    name = name.replace("bosnia & herzegovina", "bosnia")
    name = name.replace("bosnia herzegovina", "bosnia")
    name = name.replace("united states", "usa")
    return name.strip()


def _fetch(url: str) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": "polyclawd/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def espn_events_from_date(days_back: int) -> list[dict]:
    """Fetch ESPN scoreboard, paginating through startIndex for each day."""
    all_events = []
    for d in range(days_back, -1, -1):
        day = (datetime.now(timezone.utc) - timedelta(days=d)).strftime("%Y%m%d")
        # ESPN paginates at 300 events per call; WC wont exceed that per day
        url = f"{ESPN_SOCCER}?dates={day}&limit=300"
        try:
            data = _fetch(url)
            events = data.get("events", [])
            all_events.extend(events)
            if events:
                print(f"  [espn] {day}: {len(events)} events", flush=True)
        except Exception as e:
            print(f"  [espn] {day}: error — {e}", flush=True)
    return all_events


def extract_scorers(espn_event: dict) -> dict[str, list[dict]]:
    """
    From an ESPN event JSON, extract:
      home_team, away_team, home_score, away_score, status
      scorers: [{player, team, minute, type}]

    Returns dict with keys: home_team, away_team, home_score, away_score,
    status, scorers (list), event_date.
    """
    comp = espn_event.get("competitions", [{}])[0]
    status = comp.get("status", {}).get("type", {}).get("name", "")
    is_final = "STATUS_FINAL" in status or "STATUS_FULL_TIME" in status
    
    teams = {}
    for team in comp.get("competitors", []):
        side = team.get("homeAway", "")
        teams[side] = {
            "name": team.get("team", {}).get("displayName", team.get("team", {}).get("name", "")),
            "short": team.get("team", {}).get("shortDisplayName", ""),
            "abbrev": team.get("team", {}).get("abbreviation", ""),
            "score": int(team.get("score", 0)) if team.get("score") is not None else 0,
        }

    scorers = []
    # Walk through the scoring summaries
    for summary in comp.get("scoringSummaries", []):
        for item in summary:
            player = (item.get("athlete", {}) or {}).get("displayName", "")
            team_name = (item.get("team", {}) or {}).get("displayName", "")
            minute = item.get("minute", 0)
            stype = item.get("type", "")
            if not player:
                continue
            scorers.append({
                "player": player,
                "player_canon": canon(player),
                "team": team_name,
                "minute": minute,
                "type": stype,
            })

    # Also check the detailed scoring plays section
    for detail in comp.get("details", []):
        plays = detail.get("athletesInvolved", []) if isinstance(detail, dict) else []
        if not plays and isinstance(detail, list):
            plays = detail
        for play in (plays if isinstance(plays, list) else [plays]):
            if not isinstance(play, dict):
                continue
            pname = play.get("displayName", "") or play.get("shortName", "")
            tid = play.get("team", {}).get("displayName", "") if isinstance(play.get("team"), dict) else ""
            if not pname or canon(pname) in {s["player_canon"] for s in scorers}:
                continue
            # Check if this is a goal
            desc = detail.get("description", "") if isinstance(detail, dict) else ""
            if "goal" in desc.lower() or not desc:
                scorers.append({
                    "player": pname,
                    "player_canon": canon(pname),
                    "team": tid,
                    "minute": 0,
                    "type": "goal",
                })

    home_name = teams.get("home", {}).get("name", "")
    away_name = teams.get("away", {}).get("name", "")

    return {
        "home_team": home_name,
        "away_team": away_name,
        "home_score": teams.get("home", {}).get("score", 0),
        "away_score": teams.get("away", {}).get("score", 0),
        "status": "final" if is_final else "live" if "IN_PROGRESS" in status else "scheduled",
        "scorers": scorers,
        "event_date": espn_event.get("date", ""),
    }


def match_event_id(db, espn_result: dict) -> str | None:
    """
    Try to match an ESPN result to an event_id in our DB.
    Uses team name matching.
    """
    ht = canon(espn_result["home_team"])
    at = canon(espn_result["away_team"])

    cur = db.execute("""
        SELECT DISTINCT event_id, event_title, commence_time
        FROM scorer_snapshot
        ORDER BY commence_time DESC
    """)
    
    candidates = []
    for eid, title, ct in cur.fetchall():
        parts = title.split(" vs ")
        if len(parts) != 2:
            continue
        db_home, db_away = canon(parts[0]), canon(parts[1])
        # Try exact, then substring match
        if (ht == db_home and at == db_away) or (at == db_home and ht == db_away):
            candidates.append((eid, title, ct))
    
    if not candidates:
        # Fallback: try reverse order
        for eid, title, ct in db.execute("""
            SELECT DISTINCT event_id, event_title, commence_time
            FROM scorer_snapshot ORDER BY commence_time DESC
        """):
            parts = title.split(" vs ")
            if len(parts) != 2:
                continue
            db_home, db_away = canon(parts[0]), canon(parts[1])
            # Check if home team partial match
            if ht[:8] in db_home or ht[:8] in db_away:
                candidates.append((eid, title, ct))
    
    if not candidates:
        return None
    # Return most recent match that fits
    candidates.sort(key=lambda x: x[2], reverse=True)
    return candidates[0][0]


def resolve_db(db_path: str, days_back: int = 14) -> dict:
    """Main resolution: fetch ESPN data, match to DB, write scored column."""
    db = sqlite3.connect(db_path)
    
    # Add scored column if missing
    try:
        db.execute("ALTER TABLE scorer_snapshot ADD COLUMN scored INTEGER")
    except sqlite3.OperationalError:
        pass  # already exists
    try:
        db.execute("ALTER TABLE scorer_snapshot ADD COLUMN resolved INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE scorer_snapshot ADD COLUMN match_score_home INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE scorer_snapshot ADD COLUMN match_score_away INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE scorer_snapshot ADD COLUMN resolved_at TEXT")
    except sqlite3.OperationalError:
        pass
    db.commit()
    
    # Get all distinct unscored event_ids
    resolved_count = db.execute(
        "SELECT COUNT(DISTINCT event_id) FROM scorer_snapshot WHERE resolved = 1"
    ).fetchone()[0]
    total_count = db.execute(
        "SELECT COUNT(DISTINCT event_id) FROM scorer_snapshot"
    ).fetchone()[0]
    
    print(f"Matches: {resolved_count}/{total_count} already resolved", flush=True)
    
    unscored = db.execute("""
        SELECT DISTINCT event_id, event_title, commence_time
        FROM scorer_snapshot
        WHERE resolved IS NULL OR resolved = 0
        ORDER BY commence_time DESC
    """).fetchall()
    
    print(f"Matches to resolve: {len(unscored)}", flush=True)
    
    # Fetch ESPN events — expanded window to cover all matches
    espn_events = espn_events_from_date(days_back)
    print(f"ESPN events fetched: {len(espn_events)}", flush=True)
    
    stats = {"matched": 0, "unmatched": 0, "players_scored": 0, "props_resolved": 0}
    
    for espn_ev in espn_events:
        result = extract_scorers(espn_ev)
        if result["status"] != "final":
            continue
        
        eid = match_event_id(db, result)
        if eid is None:
            stats["unmatched"] += 1
            continue
        
        # Get all players in this match from our DB
        players = db.execute(
            "SELECT DISTINCT player, player_raw FROM scorer_snapshot WHERE event_id = ?",
            (eid,)
        ).fetchall()
        
        # Canonicalize scorer names
        scorer_canons = set(s["player_canon"] for s in result["scorers"])
        
        scored_count = 0
        total_in_match = 0
        for player_canon, player_raw in players:
            # Check if this player scored
            pc = canon(player_canon)
            did_score = 1 if pc in scorer_canons else 0
            
            # Also check partial matches for name variations
            if not did_score:
                # Check if any scorer's name is a partial match
                for sc in scorer_canons:
                    # Try first+last name splice
                    parts = pc.split()
                    sc_parts = sc.split()
                    if len(parts) >= 2 and len(sc_parts) >= 2:
                        # Last name match often survives transliteration
                        if parts[-1] == sc_parts[-1] and len(parts[-1]) > 3:
                            did_score = 1
                            break
            
            db.execute("""
                UPDATE scorer_snapshot
                SET scored = ?, resolved = 1,
                    match_score_home = ?, match_score_away = ?,
                    resolved_at = datetime('now')
                WHERE event_id = ? AND player = ?
            """, (did_score, result["home_score"], result["away_score"], eid, player_canon))
            total_in_match += 1
            if did_score:
                scored_count += 1
        
        db.commit()
        stats["matched"] += 1
        stats["players_scored"] += scored_count
        stats["props_resolved"] += total_in_match
        
        print(f"  [resolve] {result['home_team']} {result['home_score']}-{result['away_score']} "
              f"{result['away_team']} — {scored_count}/{total_in_match} scored, "
              f"scorers: {', '.join(s['player'] for s in result['scorers'][:5])}", flush=True)
    
    # Also try to match any remaining unscored matches by brute-force name search
    remaining = db.execute("""
        SELECT DISTINCT s.event_id, s.event_title, s.commence_time
        FROM scorer_snapshot s
        WHERE s.resolved IS NULL OR s.resolved = 0
        ORDER BY s.commence_time DESC
        LIMIT 30
    """).fetchall()
    
    if remaining:
        print(f"\n⚠️ {len(remaining)} matches still unresolved:", flush=True)
        for eid, title, ct in remaining[:10]:
            print(f"  {title[:45]:45s} | {ct[:16]}", flush=True)
    
    db.close()
    return stats


def stats(db_path: str):
    """Print calibration stats."""
    db = sqlite3.connect(db_path)
    cur = db.execute("""
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN resolved = 1 THEN 1 ELSE 0 END) as resolved,
            SUM(CASE WHEN scored = 1 THEN 1 ELSE 0 END) as scored,
            COUNT(DISTINCT event_id) as matches
        FROM scorer_snapshot
    """)
    total, resolved, scored, matches = cur.fetchone()
    
    print(f"=== SCORER RESOLUTION STATS ===")
    print(f"Total props: {total}")
    print(f"Resolved:    {resolved} ({100*resolved/total:.0f}%)" if total else "Resolved: 0")
    print(f"Players scored: {scored}")
    print(f"Unique matches: {matches}")
    print()
    
    if resolved:
        # By fair-value bucket (all resolved props)
        cur = db.execute("""
            SELECT 
                CASE 
                    WHEN consensus_fair < 0.02 THEN '0-2%'
                    WHEN consensus_fair < 0.05 THEN '2-5%'
                    WHEN consensus_fair < 0.10 THEN '5-10%'
                    WHEN consensus_fair < 0.15 THEN '10-15%'
                    WHEN consensus_fair < 0.25 THEN '15-25%'
                    ELSE '25%+'
                END as bucket,
                COUNT(*) as n,
                SUM(scored) as goals
            FROM scorer_snapshot
            WHERE resolved = 1
            GROUP BY bucket
            ORDER BY MIN(consensus_fair)
        """)
        print("Scoring rate by fair-value bucket:")
        for r in cur.fetchall():
            print(f"  {r[0]:>8s}: {r[2]}/{r[1]} = {100*r[2]/r[1]:.1f}%")
        print()
        
        # By edge bucket (makes this a calibration curve)
        cur = db.execute("""
            SELECT 
                CASE 
                    WHEN edge_pct < 0 THEN '<0pp'
                    WHEN edge_pct < 3 THEN '0-3pp'
                    WHEN edge_pct < 5 THEN '3-5pp'
                    WHEN edge_pct < 8 THEN '5-8pp'
                    WHEN edge_pct < 12 THEN '8-12pp'
                    ELSE '12pp+'
                END as bucket,
                COUNT(*) as n,
                SUM(scored) as goals
            FROM scorer_snapshot
            WHERE resolved = 1
            GROUP BY bucket
            ORDER BY MIN(edge_pct)
        """)
        print("Scoring rate by edge bucket (calibration curve):")
        for r in cur.fetchall():
            print(f"  {r[0]:>8s}: {r[2]}/{r[1]} = {100*r[2]/r[1]:.1f}%")
    
    db.close()


def list_missing(db_path: str):
    db = sqlite3.connect(db_path)
    cur = db.execute("""
        SELECT DISTINCT s.event_id, s.event_title, s.commence_time,
               (SELECT COUNT(*) FROM scorer_snapshot s2 WHERE s2.event_id = s.event_id) as n
        FROM scorer_snapshot s
        WHERE s.resolved IS NULL OR s.resolved = 0
        ORDER BY s.commence_time DESC
    """)
    rows = cur.fetchall()
    print(f"Unresolved matches: {len(rows)}")
    for eid, title, ct, n in rows:
        print(f"  {title[:45]:45s} | {ct[:16]} | {n:3d} props")
    db.close()


def main():
    ap = argparse.ArgumentParser(description="Resolve goalscorer props from ESPN")
    ap.add_argument("--db", default="storage/scorer_clv_corrected.db")
    ap.add_argument("--batch-days", type=int, default=0,
                    help="Fetch ESPN for N days back and resolve")
    ap.add_argument("--auto", action="store_true",
                    help="Auto-resolve all currently unresolved matches")
    ap.add_argument("--list-missing", action="store_true")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()
    
    if args.list_missing:
        list_missing(args.db)
    elif args.stats:
        stats(args.db)
    elif args.auto:
        # Determine days needed from DB
        cur = sqlite3.connect(args.db).execute(
            "SELECT MIN(commence_time), MAX(commence_time) FROM scorer_snapshot"
        ).fetchone()
        if cur and cur[0]:
            min_dt = datetime.fromisoformat(cur[0].replace("Z", "+00:00"))
            days = (datetime.now(timezone.utc) - min_dt).days + 2
            print(f"Auto-resolve: fetching {days} days of ESPN data", flush=True)
            s = resolve_db(args.db, days_back=days)
            print(f"\nDone: {s['matched']} matches resolved, "
                  f"{s['players_scored']} scoring events, "
                  f"{s['props_resolved']} props updated", flush=True)
            stats(args.db)
        else:
            print("No data in DB")
    elif args.batch_days:
        s = resolve_db(args.db, days_back=args.batch_days)
        print(f"\nDone: {s['matched']} matches resolved, "
              f"{s['players_scored']} scoring events, "
              f"{s['props_resolved']} props updated", flush=True)
    else:
        stats(args.db)


if __name__ == "__main__":
    main()
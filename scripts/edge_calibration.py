#!/usr/bin/env python3
"""
edge_calibration.py — Sport-agnostic win-rate-by-edge calibration engine.

Scores every logged edge signal (gap_pp + direction + teams + timestamp)
against the FINAL result for that sport, stores outcomes durably, and reports
a win-rate-by-edge-bucket curve per sport. Self-updates as signals accumulate.

WHY (2026-08-23):
  MLB PM_GAP_PP was raised 6->15pp because 6-15pp gaps were coin-flips (~51%).
  That was validated by mlb_edge_calibration.py. This generalizes the same
  calibration to every sport we trade edges on, so each sport's threshold is
  learned from its own data instead of guessed.

SPORT CONFIGS:
  Each sport defines:
    - signal_sql:   query returning (fired_at, home, away, outcome, gap_pp,
                    direction) rows to score
    - result_fn:    callable(date) -> {team_name: won_bool}
    - team_key:     how to normalize team names for result matching
    - has_draw:     whether 3-way markets exist (draw outcomes skipped)

RUN:  python3 scripts/edge_calibration.py [--sport SPORT] [--days N] [--report]
  --sport   only score one sport (default: all)
  --days N  only score rows from last N days (default: all)
  --report  print curves only (no scoring)

Cron: daily 06:00 ET — self-updates as signals accumulate.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "storage", "shadow_trades.db")

# Edge buckets (pp of |gap|)
BUCKETS = [(0, 6), (6, 8), (8, 10), (10, 12), (12, 15), (15, 20), (20, 100)]


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS edge_calibration (
            sport        TEXT,
            game_id      TEXT,
            team         TEXT,
            gap_pp       REAL,
            direction    TEXT,
            won          INTEGER,
            fired_at     TEXT,
            scored_at    TEXT,
            PRIMARY KEY (sport, game_id, team, fired_at)
        )
    """)
    conn.commit()


# ── Result sources ──────────────────────────────────────────────────────────

def _mlb_results(date_str: str) -> dict:
    url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date_str}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except Exception as ex:
        print(f"[calib] MLB fetch {date_str} failed: {ex}", flush=True)
        return {}
    out = {}
    for date in data.get("dates", []):
        for g in date.get("games", []):
            if g.get("status", {}).get("detailedState") != "Final":
                continue
            t = g.get("teams", {})
            away = t.get("away", {}).get("team", {}).get("name", "")
            home = t.get("home", {}).get("team", {}).get("name", "")
            as_, hs = t.get("away", {}).get("score"), t.get("home", {}).get("score")
            if as_ is None or hs is None:
                continue
            out[away] = as_ > hs
            out[home] = hs > as_
    return out


def _espn_results(league: str, date_str: str) -> dict:
    """Generic ESPN scoreboard result fetch. league = e.g. 'soccer.fifa.world'."""
    url = f"https://site.api.espn.com/apis/site/v2/sports/{league}/scoreboard?dates={date_str}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except Exception as ex:
        print(f"[calib] ESPN {league} fetch {date_str} failed: {ex}", flush=True)
        return {}
    out = {}
    for e in data.get("events", []):
        comp = e.get("competitions", [{}])[0]
        status = comp.get("status", {}).get("type", {}).get("detail", "")
        if status not in ("FT", "Final", "Full Time"):
            continue
        for c in comp.get("competitors", []):
            name = c.get("team", {}).get("displayName", "")
            score = c.get("score")
            if name and score is not None:
                out[name] = int(score) > 0  # placeholder; refined per sport
    return out


def _ufc_results(date_str: str) -> dict:
    """UFC results via ESPN — winner flag on competitors."""
    url = f"https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard?dates={date_str}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except Exception as ex:
        print(f"[calib] UFC fetch {date_str} failed: {ex}", flush=True)
        return {}
    out = {}
    for e in data.get("events", []):
        comp = e.get("competitions", [{}])[0]
        status = comp.get("status", {}).get("type", {}).get("detail", "")
        if status != "Final":
            continue
        for c in comp.get("competitors", []):
            name = c.get("athlete", {}).get("displayName", "")
            winner = c.get("winner")
            if name and winner is not None:
                out[name] = bool(winner)
    return out


# ── Sport configs ────────────────────────────────────────────────────────────

SPORTS = {
    "MLB": {
        "signal_sql": """
            SELECT fired_at, home_team, away_team, outcome, gap_pp, trade_signal
            FROM mlb_odds_moved_log
            WHERE trade_signal IS NOT NULL
        """,
        "result_fn": _mlb_results,
        "has_draw": False,
    },
    "UFC": {
        "signal_sql": """
            SELECT ts as fired_at, fighter_a as home_team, fighter_b as away_team,
                   fighter_a as outcome, (pin_a - pm_a)*100 as gap_pp,
                   CASE WHEN pin_a > pm_a THEN 'BUY' ELSE 'SELL' END as trade_signal
            FROM ufc_line_snap
            WHERE pm_a IS NOT NULL AND pm_a != ''
        """,
        "result_fn": _ufc_results,
        "has_draw": False,
    },
}


def _game_date_et(fired_at: str) -> str:
    try:
        dt = datetime.fromisoformat(fired_at.replace("Z", "+00:00"))
    except Exception:
        return ""
    return dt.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


def score_sport(conn: sqlite3.Connection, sport: str, days: int | None = None) -> int:
    cfg = SPORTS[sport]
    _ensure_schema(conn)
    where = ""
    params = []
    if days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        where = "AND fired_at >= ?"
        params.append(cutoff)

    rows = conn.execute(cfg["signal_sql"] + where, params).fetchall()
    # Group by ET date to batch result fetches
    by_date: dict = {}
    for r in rows:
        et = _game_date_et(r["fired_at"])
        if not et:
            continue
        by_date.setdefault(et, set()).add((r["home_team"], r["away_team"]))

    scored = 0
    for et_date, games in by_date.items():
        results = cfg["result_fn"](et_date)
        if not results:
            continue
        for home, away in games:
            sport_rows = conn.execute(cfg["signal_sql"] + " AND home_team=? AND away_team=?", (home, away)).fetchall()
            for r in sport_rows:
                team = r["outcome"]
                if team not in results:
                    continue
                won = 1 if results[team] else 0
                conn.execute("""
                    INSERT OR REPLACE INTO edge_calibration
                      (sport, game_id, team, gap_pp, direction, won, fired_at, scored_at)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (sport, f"{home}_{away}", team, r["gap_pp"], r["trade_signal"],
                      won, r["fired_at"], datetime.now(timezone.utc).isoformat()))
                scored += 1
    conn.commit()
    return scored


def report(conn: sqlite3.Connection, sport: str | None = None) -> None:
    sports = [sport] if sport else list(SPORTS.keys())
    for s in sports:
        print(f"\n=== {s} Edge Calibration: win rate by |gap| bucket ===")
        print(f"{'bucket':<10} {'n':>5} {'wins':>5} {'win_rate':>9}")
        print("-" * 34)
        total_n = 0
        for lo, hi in BUCKETS:
            rows = conn.execute("""
                SELECT COUNT(*) as n, SUM(won) as wins
                FROM edge_calibration
                WHERE sport=? AND abs(gap_pp) >= ? AND abs(gap_pp) < ?
            """, (s, lo, hi)).fetchone()
            n = rows["n"] or 0
            wins = rows["wins"] or 0
            total_n += n
            wr = (wins / n * 100) if n else 0
            print(f"{lo}-{hi:<5} {n:>5} {wins:>5} {wr:>8.1f}%")
        print("-" * 34)
        all_rows = conn.execute(
            "SELECT COUNT(*) as n, SUM(won) as wins FROM edge_calibration WHERE sport=?",
            (s,)).fetchone()
        n = all_rows["n"] or 0
        wins = all_rows["wins"] or 0
        if n:
            print(f"Overall: {wins/n*100:.1f}%  (n={n})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", choices=list(SPORTS.keys()), default=None)
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    conn = _db()
    sports = [args.sport] if args.sport else list(SPORTS.keys())
    if not args.report:
        for s in sports:
            scored = score_sport(conn, s, args.days)
            print(f"[calib] {s}: scored {scored} new rows", flush=True)
    report(conn, args.sport)
    conn.close()


if __name__ == "__main__":
    main()

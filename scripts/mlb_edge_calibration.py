#!/usr/bin/env python3
"""
mlb_edge_calibration.py — Durable win-rate-by-edge calibration for MLB live alerts.

Scores every row in mlb_odds_moved_log against the FINAL game result (MLB
StatsAPI) and stores the outcome durably, so we can learn the empirically
optimal PM_GAP_PP threshold instead of guessing.

WHY THIS EXISTS (2026-08-23):
  PM_GAP_PP was raised 6pp -> 15pp because 6-15pp gaps fired on PM-lag/spread
  noise, not confirmed edges. But 15pp was a guess based on 2 documented real
  signals (16pp, 19pp). This job turns that guess into a calibrated curve by
  scoring every historical alert against the actual game result.

DATA FLOW:
  mlb_odds_moved_log (gap_pp, trade_signal, home_team, away_team, fired_at)
    -> MLB StatsAPI final score (per game-day)
    -> mlb_edge_calibration (durable: game, team, gap_pp, direction, won)
    -> win-rate-by-edge-bucket report

RUN:  python3 scripts/mlb_edge_calibration.py [--days N] [--report]
  --days N   only score rows from the last N days (default: all)
  --report   print the win-rate-by-edge-bucket table (no scoring)

Cron: daily 06:00 ET (after all games final) — self-updates as alerts accumulate.
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
MLB_API = "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date}"

# Edge buckets for the report (pp of |gap|)
BUCKETS = [(0, 6), (6, 8), (8, 10), (10, 12), (12, 15), (15, 20), (20, 100)]


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mlb_edge_calibration (
            game_id      TEXT,
            team         TEXT,
            gap_pp       REAL,
            direction    TEXT,      -- BUY / SELL
            poly_price   REAL,
            won          INTEGER,   -- 1 = team won, 0 = lost
            fired_at     TEXT,
            scored_at    TEXT,
            PRIMARY KEY (game_id, team, fired_at)
        )
    """)
    conn.commit()


def _fetch_mlb_results(date_str: str) -> dict:
    """Fetch final scores for one ET date. Returns {team_name: won_bool}."""
    url = MLB_API.format(date=date_str)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except Exception as ex:
        print(f"[calib] MLB fetch failed for {date_str}: {ex}", flush=True)
        return {}
    results = {}
    for date in data.get("dates", []):
        for g in date.get("games", []):
            status = g.get("status", {}).get("detailedState", "")
            if status != "Final":
                continue
            teams = g.get("teams", {})
            away = teams.get("away", {}).get("team", {}).get("name", "")
            home = teams.get("home", {}).get("team", {}).get("name", "")
            away_s = teams.get("away", {}).get("score")
            home_s = teams.get("home", {}).get("score")
            if away_s is None or home_s is None:
                continue
            results[away] = away_s > home_s
            results[home] = home_s > away_s
    return results


def _game_date_et(fired_at: str) -> str:
    """Convert a UTC fired_at to the ET game date (MLB dates games by ET)."""
    try:
        dt = datetime.fromisoformat(fired_at.replace("Z", "+00:00"))
    except Exception:
        return ""
    return dt.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


def score_rows(conn: sqlite3.Connection, days: int | None = None) -> int:
    """Score unscored mlb_odds_moved_log rows against final results."""
    _ensure_schema(conn)
    # Find rows not yet scored (no matching calibration row)
    where = ""
    params = []
    if days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        where = "AND fired_at >= ?"
        params.append(cutoff)

    rows = conn.execute(f"""
        SELECT DISTINCT home_team, away_team, substr(fired_at,1,10) as day
        FROM mlb_odds_moved_log
        WHERE trade_signal IS NOT NULL {where}
    """, params).fetchall()

    scored = 0
    # Group by ET game date to batch MLB API calls
    by_date: dict = {}
    for r in rows:
        et_date = _game_date_et(r["day"] + "T12:00:00+00:00")
        if not et_date:
            continue
        by_date.setdefault(et_date, set()).add((r["home_team"], r["away_team"]))

    for et_date, games in by_date.items():
        results = _fetch_mlb_results(et_date)
        if not results:
            continue
        for home, away in games:
            # Score all log rows for this game
            log_rows = conn.execute("""
                SELECT id, fired_at, home_team, away_team, outcome, gap_pp,
                       poly_price, trade_signal
                FROM mlb_odds_moved_log
                WHERE home_team=? AND away_team=? AND trade_signal IS NOT NULL
            """, (home, away)).fetchall()
            for lr in log_rows:
                team = lr["outcome"]
                if team not in results:
                    continue
                won = 1 if results[team] else 0
                direction = lr["trade_signal"]
                conn.execute("""
                    INSERT OR REPLACE INTO mlb_edge_calibration
                      (game_id, team, gap_pp, direction, poly_price, won, fired_at, scored_at)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (
                    f"{home}_{away}", team, lr["gap_pp"], direction,
                    lr["poly_price"], won, lr["fired_at"],
                    datetime.now(timezone.utc).isoformat(),
                ))
                scored += 1
    conn.commit()
    return scored


def report(conn: sqlite3.Connection) -> None:
    """Print win-rate-by-edge-bucket table."""
    print("\n=== MLB Edge Calibration: win rate by |gap| bucket ===")
    print(f"{'bucket':<10} {'n':>5} {'wins':>5} {'win_rate':>9}")
    print("-" * 34)
    total_n = 0
    for lo, hi in BUCKETS:
        rows = conn.execute("""
            SELECT COUNT(*) as n, SUM(won) as wins
            FROM mlb_edge_calibration
            WHERE abs(gap_pp) >= ? AND abs(gap_pp) < ?
        """, (lo, hi)).fetchone()
        n = rows["n"] or 0
        wins = rows["wins"] or 0
        total_n += n
        wr = (wins / n * 100) if n else 0
        print(f"{lo}-{hi:<5} {n:>5} {wins:>5} {wr:>8.1f}%")
    print("-" * 34)
    print(f"{'TOTAL':<10} {total_n:>5}")
    # Overall
    all_rows = conn.execute("SELECT COUNT(*) as n, SUM(won) as wins FROM mlb_edge_calibration").fetchone()
    n = all_rows["n"] or 0
    wins = all_rows["wins"] or 0
    if n:
        print(f"Overall win rate: {wins/n*100:.1f}%  (n={n})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="only score last N days")
    ap.add_argument("--report", action="store_true", help="print report only")
    args = ap.parse_args()

    conn = _db()
    if not args.report:
        scored = score_rows(conn, args.days)
        print(f"[calib] scored {scored} new rows", flush=True)
    report(conn)
    conn.close()


if __name__ == "__main__":
    main()

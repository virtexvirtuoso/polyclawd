#!/usr/bin/env python3
"""
Kalshi MLB Prop Resolver — resolves shadow trades with strategy='kalshi_edge'.

Uses MLB Stats API (statsapi.mlb.com) to fetch actual K/HR stats post-game
and update shadow_trades with outcome=YES/NO and pnl.

Run after games complete (next morning is safest) or on-demand via CLI:
  python3 signals/kalshi_prop_resolver.py [--date 2026-06-06] [--dry-run]
"""

import json
import re
import sqlite3
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from loguru import logger

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "storage" / "shadow_trades.db"
MLB_API = "https://statsapi.mlb.com/api/v1"

# Ticker regex: KXMLB{KS|HR}-{YYMONDDHHMMAWAYOME}-{TEAM}{INITIAL}{LASTNAME}{JERSEY}-{LINE}
# Team codes can be 2-3 chars (AZ, TB, SD vs BAL, TOR, MIA).
# We capture the full game code and parse it separately to avoid greedy ambiguity.
_TICKER_RE = re.compile(
    r"^KXMLB(KS|HR)-(\d{2}\w{3}\d{6}\w{4,6})-([A-Z]{2,3})([A-Z]?)([A-Z]+)(\d+)-(\d+)$"
)

# Known 2-char MLB team codes on Kalshi
_TWO_CHAR_TEAMS = {"AZ", "TB", "SD", "SF", "KC", "NY", "LA"}


def _fetch_json(url: str, timeout: int = 15) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug(f"fetch failed: {url[:80]} — {e}")
        return None


def _parse_ticker(ticker: str) -> Optional[Dict]:
    """Parse Kalshi prop ticker into components. Returns None if no match."""
    m = _TICKER_RE.match(ticker)
    if not m:
        return None
    prop_type, game_code, team, initial, last_name, jersey, line = m.groups()

    # Extract date from game_code: YYMONDD... e.g. 26JUN06...
    # First 7 chars = YY+MON+DD, next 4 = HHMM, rest = away+home
    # We don't need exact away/home — resolution searches all final games
    game_date_str = game_code[:7]  # e.g. 26JUN06
    try:
        game_date = datetime.strptime("20" + game_date_str, "%Y%b%d").date().isoformat()
    except ValueError:
        game_date = None

    return {
        "prop_type": prop_type,       # KS or HR
        "game_code": game_code,       # full game code
        "game_date": game_date,       # YYYY-MM-DD
        "team": team,                 # player's team (2-3 chars)
        "initial": initial,           # first initial
        "last_name": last_name.lower(),
        "jersey": int(jersey),
        "line": int(line),            # Kalshi N+ line
    }


def _get_schedule(game_date: str) -> List[Dict]:
    """Fetch MLB schedule for a date (YYYY-MM-DD). Returns list of game dicts."""
    url = f"{MLB_API}/schedule?sportId=1&date={game_date}&hydrate=team"
    data = _fetch_json(url)
    if not data:
        return []
    games = []
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            games.append({
                "gamePk": g["gamePk"],
                "away": g["teams"]["away"]["team"].get("abbreviation", "").upper(),
                "home": g["teams"]["home"]["team"].get("abbreviation", "").upper(),
                "status": g.get("status", {}).get("abstractGameState", ""),
            })
    return games


def _get_boxscore(game_pk: int) -> Optional[Dict]:
    """Fetch boxscore for a game. Returns raw boxscore dict."""
    url = f"{MLB_API}/game/{game_pk}/boxscore"
    return _fetch_json(url)


def _find_player_stats(boxscore: Dict, last_name: str, prop_type: str) -> Optional[int]:
    """
    Search boxscore for a player by last name and return their K or HR stat.
    prop_type: 'KS' → pitching strikeouts, 'HR' → batting homeRuns.
    Returns the integer stat value, or None if player not found.
    """
    lname = last_name.lower()

    for side in ("away", "home"):
        team_box = boxscore.get("teams", {}).get(side, {})

        if prop_type == "KS":
            # Pitcher stats
            pitchers = team_box.get("pitchers", [])
            players = team_box.get("players", {})
            for pid in pitchers:
                player = players.get(f"ID{pid}", {})
                full_name = player.get("person", {}).get("fullName", "").lower()
                if lname in full_name.split()[-1]:  # match last name
                    ks = player.get("stats", {}).get("pitching", {}).get("strikeOuts")
                    if ks is not None:
                        logger.debug(f"  Found pitcher {full_name}: {ks} Ks")
                        return int(ks)

        elif prop_type == "HR":
            # Batter stats
            batters = team_box.get("batters", [])
            players = team_box.get("players", {})
            for pid in batters:
                player = players.get(f"ID{pid}", {})
                full_name = player.get("person", {}).get("fullName", "").lower()
                if lname in full_name.split()[-1]:
                    hr = player.get("stats", {}).get("batting", {}).get("homeRuns")
                    if hr is not None:
                        logger.debug(f"  Found batter {full_name}: {hr} HRs")
                        return int(hr)

    return None


def _resolve_trade(trade: Dict, schedule: List[Dict], dry_run: bool = False) -> Optional[Dict]:
    """
    Resolve a single Kalshi prop shadow trade. Returns resolution dict or None.
    Searches all final games on the date for the player — avoids team code parsing issues.
    """
    ticker = trade["market_id"]
    parsed = _parse_ticker(ticker)
    if not parsed:
        logger.warning(f"  Could not parse ticker: {ticker}")
        return None

    last_name = parsed["last_name"]
    prop_type = parsed["prop_type"]
    line = parsed["line"]

    # Search all final games for this player
    final_games = [g for g in schedule if g["status"] in ("Final", "Completed")]
    if not final_games:
        live = [g for g in schedule if g["status"] == "Live"]
        if live:
            logger.info(f"  Games still live for {ticker} — retry later")
        else:
            logger.info(f"  No final games found for {ticker}")
        return None

    stat = None
    for game in final_games:
        boxscore = _get_boxscore(game["gamePk"])
        if not boxscore:
            continue
        found = _find_player_stats(boxscore, last_name, prop_type)
        if found is not None:
            stat = found
            break
        time.sleep(0.1)

    if stat is None:
        # Check if any games still live (player might be in an unfinished game)
        live = [g for g in schedule if g["status"] == "Live"]
        if live:
            logger.info(f"  {last_name.upper()} not found in final games; {len(live)} game(s) still live — retry later")
        else:
            logger.warning(f"  Player not found in any final game: {last_name.upper()} ({ticker})")
        return None

    hit = stat >= line
    outcome = "YES" if hit else "NO"
    entry_price = trade.get("entry_price", 0.5)
    pnl = round((1.0 - entry_price) if hit else (-entry_price), 4)

    prop_label = f"{line}+{'K' if prop_type == 'KS' else 'HR'}"
    logger.info(
        f"  RESOLVED {ticker[:50]} | {last_name.upper()} {prop_label} "
        f"actual={stat} → {outcome} | pnl={pnl:+.4f}"
    )

    return {
        "id": trade["id"],
        "outcome": outcome,
        "pnl": pnl,
        "actual_stat": stat,
        "line": line,
    }


def run_resolver(game_date: str = None, dry_run: bool = False) -> Dict:
    """
    Main entry point. Resolves all unresolved Kalshi prop shadow trades for a date.
    game_date: YYYY-MM-DD, defaults to yesterday (most games completed).
    """
    if game_date is None:
        game_date = (date.today() - timedelta(days=0)).isoformat()

    logger.info(f"kalshi_prop_resolver: resolving for {game_date} (dry_run={dry_run})")

    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

    # Get unresolved Kalshi prop trades for this date
    rows = conn.execute("""
        SELECT id, market_id, market, entry_price, side, snapshot_date
        FROM shadow_trades
        WHERE resolved = 0
          AND strategy = 'kalshi_edge'
          AND category = 'mlb_props'
          AND snapshot_date = ?
        ORDER BY id ASC
    """, (game_date,)).fetchall()

    trades = [dict(r) for r in rows]
    logger.info(f"  {len(trades)} unresolved Kalshi prop trades for {game_date}")

    if not trades:
        conn.close()
        return {"resolved": 0, "skipped": 0, "game_date": game_date}

    schedule = _get_schedule(game_date)
    logger.info(f"  {len(schedule)} games in MLB schedule for {game_date}")

    resolved_count = 0
    skipped_count = 0
    results = []

    for trade in trades:
        result = _resolve_trade(trade, schedule, dry_run=dry_run)
        if result is None:
            skipped_count += 1
            time.sleep(0.2)
            continue

        if not dry_run:
            conn.execute("""
                UPDATE shadow_trades
                SET resolved = 1,
                    resolved_at = ?,
                    outcome = ?,
                    pnl = ?,
                    exit_price = ?,
                    close_reason = ?
                WHERE id = ?
            """, (
                datetime.now(timezone.utc).isoformat(),
                result["outcome"],
                result["pnl"],
                1.0 if result["outcome"] == "YES" else 0.0,
                f"stat={result['actual_stat']} line={result['line']}",
                result["id"],
            ))
            resolved_count += 1
        else:
            logger.info(f"  DRY RUN: would resolve id={result['id']} → {result['outcome']}")
            resolved_count += 1

        results.append(result)
        time.sleep(0.15)

    if not dry_run:
        conn.commit()

    conn.close()

    # Summary
    wins = [r for r in results if r["outcome"] == "YES"]
    losses = [r for r in results if r["outcome"] == "NO"]
    total_pnl = sum(r["pnl"] for r in results)

    logger.info(
        f"kalshi_prop_resolver: {resolved_count} resolved "
        f"({len(wins)}W/{len(losses)}L, pnl={total_pnl:+.4f}), "
        f"{skipped_count} skipped"
    )

    return {
        "resolved": resolved_count,
        "skipped": skipped_count,
        "wins": len(wins),
        "losses": len(losses),
        "total_pnl": round(total_pnl, 4),
        "game_date": game_date,
        "details": results,
    }


def print_unresolved(game_date: str = None):
    """Print current unresolved Kalshi prop trades for review."""
    if game_date is None:
        game_date = date.today().isoformat()
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, market_id, market, entry_price, snapshot_date
        FROM shadow_trades
        WHERE resolved = 0 AND strategy = 'kalshi_edge' AND category = 'mlb_props'
          AND snapshot_date = ?
        ORDER BY id
    """, (game_date,)).fetchall()
    conn.close()
    print(f"\nUnresolved Kalshi prop shadow trades for {game_date}: {len(rows)}")
    for r in rows:
        print(f"  [{r['id']}] {r['market_id']}")
        print(f"       {r['market']} | entry={r['entry_price']:.2f}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Resolve Kalshi prop shadow trades")
    parser.add_argument("--date", help="Game date YYYY-MM-DD (default: today)")
    parser.add_argument("--dry-run", action="store_true", help="Print resolutions without writing")
    parser.add_argument("--list", action="store_true", help="List unresolved trades and exit")
    args = parser.parse_args()

    if args.list:
        print_unresolved(args.date)
        sys.exit(0)

    result = run_resolver(game_date=args.date, dry_run=args.dry_run)
    print(f"\nDate: {result['game_date']}")
    print(f"Resolved: {result['resolved']} ({result.get('wins',0)}W / {result.get('losses',0)}L)")
    print(f"Skipped:  {result['skipped']}")
    print(f"Total PnL: {result.get('total_pnl', 0):+.4f}")
    if result.get("details"):
        print("\nDetails:")
        for d in result["details"]:
            print(f"  [{d['id']}] line={d['line']} actual={d['actual_stat']} → {d['outcome']} pnl={d['pnl']:+.4f}")

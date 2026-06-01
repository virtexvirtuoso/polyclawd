#!/usr/bin/env python3
"""
Baseball Game Resolution Watcher — Phase 1 of closed-loop calibration.

Checks Polymarket Gamma API for resolved baseball events, matches them to
shadow trades by market_id, records prediction-vs-outcome in
baseball_forecast_log table, and updates shadow_trade rows.

Runs alongside shadow_tracker.py resolve in the 5-min watchdog cycle.
Rate-limit aware: max 1 Polymarket call per 5 seconds.
"""

import json
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Dict, List, Optional, Any
import re

from loguru import logger

# Paths (mirrors shadow_tracker.py)
BASE_DIR = Path(__file__).parent.parent
STORAGE_DIR = BASE_DIR / "storage"
DB_PATH = STORAGE_DIR / "shadow_trades.db"

# Polymarket APIs
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Rate limiting: 1 call per 5 seconds
RATE_DELAY = 5.0


# ─── DB Init ─────────────────────────────────────────────────────────


def get_db() -> sqlite3.Connection:
    """Get SQLite connection (WAL mode, shared with shadow_tracker)."""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _init_tables(conn)
    return conn


def _init_tables(conn: sqlite3.Connection):
    """Create baseball_forecast_log table if not exists."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS baseball_forecast_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id TEXT,
            team TEXT,
            opponent TEXT,
            game_date TEXT,
            odds_api_prob REAL,
            poly_price REAL,
            edge_pct REAL,
            direction TEXT,
            actual_outcome TEXT,
            predicted_correct INTEGER,
            american_odds INTEGER,
            books_count INTEGER,
            shadow_trade_id INTEGER,
            recorded_at TEXT,
            UNIQUE(game_id, team)
        );
    """)
    conn.commit()


# ─── Polymarket API Calls ────────────────────────────────────────────


def _fetch_json(url: str, params: dict = None, timeout: int = 10) -> Optional[Any]:
    """Fetch JSON from a URL with User-Agent. Returns None on failure."""
    try:
        full_url = url
        if params:
            import urllib.parse
            full_url = url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(full_url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug(f"baseball_resolver fetch failed: {url[:60]} - {e}")
        return None


def _get_resolved_baseball_events() -> List[Dict]:
    """Fetch resolved baseball events from Polymarket Gamma API.

    Polls for closed events tagged baseball, limited to recent 50.
    Filters to only game events (contain ' vs. ' in title).
    Returns empty list on any failure (never crashes).
    """
    data = _fetch_json(
        f"{GAMMA_API}/events",
        params={"closed": "true", "tag_slug": "baseball", "limit": "50"},
        timeout=15,
    )
    if not isinstance(data, list):
        return []
    return [e for e in data if " vs. " in (e.get("title", "") or "")]


def _get_moneyline_outcome(event: Dict) -> Optional[str]:
    """Extract the winning outcome from a resolved game event's moneyline market.

    Returns 'YES' or 'NO' (market perspective: outcomePrices[0] = YES = first team).
    Returns None if still open or ambiguous.
    """
    title = event.get("title", "")
    for market in event.get("markets", []):
        if market.get("question", "") != title:
            continue
        prices_raw = market.get("outcomePrices", "[]")
        if isinstance(prices_raw, str):
            try:
                prices = json.loads(prices_raw)
            except (json.JSONDecodeError, ValueError):
                continue
        else:
            prices = prices_raw
        if len(prices) < 2:
            continue
        try:
            price0 = float(prices[0])
            price1 = float(prices[1])
        except (ValueError, TypeError):
            continue

        # Check via CLOB for definitive resolution first
        market_id = market.get("id", "")
        if market_id:
            clob_result = _check_clob_resolution(market_id)
            if clob_result:
                return clob_result
        # Fallback: price-based heuristic
        if price0 >= 0.99 or price1 <= 0.01:
            return "YES"
        if price1 >= 0.99 or price0 <= 0.01:
            return "NO"
        return None
    return None


def _check_clob_resolution(condition_id: str) -> Optional[str]:
    """Check CLOB API for definitive resolution of a condition.
    Returns 'YES', 'NO', or None if unresolved.
    """
    data = _fetch_json(f"{CLOB_API}/markets/{condition_id}", timeout=10)
    if not data:
        return None
    if data.get("closed") or data.get("resolved"):
        tokens = data.get("tokens", [])
        for token in tokens:
            if token.get("winner") is True:
                outcome = (token.get("outcome") or "").upper()
                if outcome in ("YES", "NO"):
                    return outcome
                return "YES" if token == tokens[0] else "NO"
        for token in tokens:
            if token.get("outcome") == "Yes" and float(token.get("price", 0)) > 0.9:
                return "YES"
            elif token.get("outcome") == "No" and float(token.get("price", 0)) > 0.9:
                return "NO"
    return None


# ─── Shadow Trade Matching ───────────────────────────────────────────


def _get_unresolved_baseball_trades(conn: sqlite3.Connection) -> List[Dict]:
    """Get all unresolved shadow trades with strategy=baseball_moneyline."""
    rows = conn.execute("""
        SELECT id, market_id, side, entry_price, market, category, reasoning
        FROM shadow_trades
        WHERE resolved = 0
          AND (strategy = 'baseball_moneyline'
               OR (category = 'baseball' AND side IN ('YES','NO')))
        ORDER BY timestamp ASC
    """).fetchall()
    return [dict(r) for r in rows]


# ─── Resolution Logic ────────────────────────────────────────────────


def _find_matching_event(
    shadow_trade: Dict, event_by_market_id: Dict, resolved_events: List[Dict]
) -> Optional[Dict]:
    """Find the resolved Polymarket event matching a shadow trade.
    Matches by market_id first, then by team name in title (fallback).
    """
    trade_market_id = shadow_trade.get("market_id", "")

    # Try exact market_id match first
    if trade_market_id and trade_market_id in event_by_market_id:
        return event_by_market_id[trade_market_id]

    # Fallback: match by team name
    trade_market = shadow_trade.get("market", "").lower()
    for event in resolved_events:
        title = event.get("title", "").lower()
        for fragment in trade_market.replace(" vs. ", "|").split("|"):
            fragment = fragment.split(" — ")[0].strip()
            if len(fragment) > 5 and fragment in title:
                return event
    return None


def _extract_teams_from_title(title: str) -> tuple:
    """Extract (team_a, team_b) from a game title."""
    if not title:
        return ("", "")
    parts = title.split(" vs. ")
    if len(parts) == 2:
        return (parts[0].strip(), parts[1].strip())
    return ("", "")


# ─── Main Resolution Scan ────────────────────────────────────────────


def scan_resolved_baseball_games(batch_size: int = 20) -> Dict[str, Any]:
    """Main entry point. Checks for resolved baseball games, matches to
    shadow trades, logs forecast data, updates shadow_trade rows.
    """
    conn = get_db()
    result = {"resolved": 0, "forecast_logged": 0, "skipped": 0, "errors": 0}

    # 1. Get unresolved baseball shadow trades
    trades = _get_unresolved_baseball_trades(conn)
    if not trades:
        conn.close()
        return {**result, "note": "No unresolved baseball trades"}

    # 2. Fetch resolved baseball events from Polymarket
    resolved_events = _get_resolved_baseball_events()
    if not resolved_events:
        conn.close()
        return {**result, "note": "No resolved events from Polymarket"}

    # Build event lookup by market_id for fast matching
    event_by_market_id = {}
    for event in resolved_events:
        for market in event.get("markets", []):
            mid = market.get("id", "")
            if mid:
                event_by_market_id[mid] = event

    processed = 0
    for trade in trades[:batch_size]:
        market_id = trade.get("market_id", "")
        if not market_id:
            continue

        event = _find_matching_event(trade, event_by_market_id, resolved_events)
        if not event:
            result["skipped"] += 1
            continue

        outcome = _get_moneyline_outcome(event)
        if outcome is None:
            result["skipped"] += 1
            continue

        trade_side = trade.get("side", "")
        is_correct = 1 if trade_side == outcome else 0

        # Parse details from reasoning
        reasoning = trade.get("reasoning", "")
        edge_pct = 0.0
        edge_match = re.search(r'\(([+-]\d+\.?\d*)% edge\)', reasoning)
        if edge_match:
            edge_pct = float(edge_match.group(1))

        prob_match = re.search(r'Odds API (\d+\.?\d*)%', reasoning)
        odds_api_prob = float(prob_match.group(1)) / 100.0 if prob_match else 0.0

        price_match = re.search(r'Poly (\d+\.?\d*)¢', reasoning)
        poly_price = float(price_match.group(1)) / 100.0 if price_match else trade.get("entry_price", 0.5)

        # Extract teams
        event_title = event.get("title", "")
        teams_a, teams_b = _extract_teams_from_title(event_title)
        market_str = trade.get("market", "")
        bet_team = ""
        if " — " in market_str:
            bet_team = market_str.split(" — ")[1].replace("Moneyline", "").strip()
        opponent = teams_b if bet_team in teams_a or (teams_a and bet_team and teams_a.startswith(bet_team.split()[-1])) else teams_a

        game_date = date.today().isoformat()

        # 3. Log to baseball_forecast_log
        try:
            conn.execute("""
                INSERT OR IGNORE INTO baseball_forecast_log
                (game_id, team, opponent, game_date, odds_api_prob, poly_price,
                 edge_pct, direction, actual_outcome, predicted_correct,
                 american_odds, books_count, shadow_trade_id, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                market_id[:20],
                bet_team or "unknown",
                opponent or "unknown",
                game_date,
                odds_api_prob,
                poly_price,
                edge_pct,
                trade.get("side", ""),
                outcome,
                is_correct,
                0,
                0,
                trade["id"],
                datetime.now(timezone.utc).isoformat(),
            ))
            result["forecast_logged"] += 1
        except Exception as e:
            logger.warning(f"forecast_log insert failed: {e}")
            result["errors"] += 1
            continue

        # 4. Update shadow_trade row
        entry_price = trade.get("entry_price", 0.5)
        if outcome == "YES":
            pnl = (1.0 - entry_price) if trade_side == "YES" else -entry_price
        else:
            pnl = -entry_price if trade_side == "YES" else entry_price

        try:
            conn.execute("""
                UPDATE shadow_trades
                SET resolved = 1,
                    resolved_at = ?,
                    outcome = ?,
                    pnl = ?,
                    exit_price = ?
                WHERE id = ?
            """, (
                datetime.now(timezone.utc).isoformat(),
                outcome,
                round(pnl, 4),
                1.0 if is_correct else 0.0,
                trade["id"],
            ))
            result["resolved"] += 1
        except Exception as e:
            logger.warning(f"shadow_trade update failed: {e}")
            result["errors"] += 1

        processed += 1
        if processed < len(trades[:batch_size]):
            time.sleep(RATE_DELAY)

    conn.commit()
    conn.close()

    if result["resolved"] > 0:
        logger.info(
            f"baseball_resolver: {result['resolved']} resolved, "
            f"{result['forecast_logged']} logged, "
            f"{result['skipped']} skipped, {result['errors']} errors"
        )

    return result


# ─── CLI ─────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import logging as builtin_logging
    builtin_logging.basicConfig(level=builtin_logging.INFO, format="%(message)s")
    result = scan_resolved_baseball_games()
    print(f"Resolved: {result['resolved']}")
    print(f"Forecast logged: {result['forecast_logged']}")
    print(f"Skipped: {result['skipped']}")
    print(f"Errors: {result['errors']}")
    if result.get("note"):
        print(f"Note: {result['note']}")
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

# Rate limiting: 0.5s between CLOB calls (public endpoint, no auth required)
RATE_DELAY = 0.5


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

    # Migration: ensure strategy column exists on shadow_trades
    try:
        conn.execute("ALTER TABLE shadow_trades ADD COLUMN strategy TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists


# ─── Polymarket API Calls ────────────────────────────────────────────


def _fetch_json(url: str, params: dict = None, timeout: int = 10) -> Optional[Any]:
    """Fetch JSON from a URL with User-Agent. Returns None on failure."""
    try:
        import urllib.parse
        import urllib.request
        full_url = url
        if params:
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
    try:
        rows = conn.execute("""
            SELECT id, market_id, side, entry_price, market, category, reasoning
            FROM shadow_trades
            WHERE resolved = 0
              AND (strategy = 'baseball_moneyline'
                   OR (category = 'baseball' AND side IN ('YES','NO')))
            ORDER BY timestamp ASC
        """).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        # shadow_trades table may not exist yet (first run, fresh DB)
        return []


# ─── Resolution Logic ────────────────────────────────────────────────


def _find_matching_event(
    shadow_trade: Dict, event_by_market_id: Dict, resolved_events: List[Dict]
) -> Optional[Dict]:
    """Find the resolved Polymarket event matching a shadow trade.
    Matches by market_id first, then by team name in title (fallback).
    """
    trade_market_id = shadow_trade.get("market_id", "")

    # Extract base market_id (strip __Team_markettype suffix from new format)
    base_market_id = trade_market_id.split("__")[0] if "__" in trade_market_id else trade_market_id

    # Try exact market_id match first (both raw and base)
    if trade_market_id and trade_market_id in event_by_market_id:
        return event_by_market_id[trade_market_id]
    if base_market_id and base_market_id != trade_market_id and base_market_id in event_by_market_id:
        return event_by_market_id[base_market_id]

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


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def _names_match(a: str, b: str) -> bool:
    """Loose team/outcome match: exact, containment, or shared last token
    ('Rockies' ~ 'Colorado Rockies'). Exact for 'Over'/'Under'."""
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    return na.split()[-1] == nb.split()[-1]


def _parse_bet(market_str: str):
    """('<bet_label>', '<market_type>') from the shadow 'market' text
    '<title> — <bet_label> <Moneyline|Spread|Total>'. bet_label is a team
    name (ml/spread) or 'Over'/'Under' (total)."""
    s = market_str or ""
    part = s.split(" — ")[-1].strip() if " — " in s else s
    m = re.search(r"\s+(Moneyline|Spread|Total)$", part)
    mt = m.group(1).lower() if m else "moneyline"
    label = re.sub(r"\s+(Moneyline|Spread|Total)$", "", part).strip()
    return label, mt


def score_baseball_trade(market_str: str, side: str, entry_price: float, winner_name: str):
    """Pure scorer. side 'YES'=BUY, 'NO'=SELL. winner_name = the winning Polymarket
    outcome (team or Over/Under) of the trade's OWN market. Returns (is_correct, pnl).

    Win = the trade's chosen outcome won (BUY) / did NOT win (SELL). pnl is the
    binary settle (1=win, 0=loss) minus the price actually paid for the held token.
    """
    bet_label, _mt = _parse_bet(market_str)
    is_buy = (side or "YES").upper() == "YES"
    chosen_won = _names_match(winner_name, bet_label)
    bet_won = chosen_won if is_buy else (not chosen_won)
    p = entry_price if entry_price is not None else 0.5
    eff_entry = p if is_buy else (1.0 - p)        # price of the token actually held
    pnl = (1.0 - eff_entry) if bet_won else -eff_entry
    return (1 if bet_won else 0), round(pnl, 4)


def _clob_winner(market_id: str):
    """(winner_name, winner_index) for a closed/resolved market, else None (open)."""
    data = _fetch_json(f"{CLOB_API}/markets/{market_id}", timeout=10)
    if not data or not (data.get("closed") or data.get("resolved")):
        return None
    tokens = data.get("tokens", [])
    for i, t in enumerate(tokens):
        if t.get("winner") is True:
            return (t.get("outcome") or "", i)
    for i, t in enumerate(tokens):
        try:
            if float(t.get("price", 0)) > 0.9:
                return (t.get("outcome") or "", i)
        except (TypeError, ValueError):
            pass
    return None


def scan_resolved_baseball_games(batch_size: int = 200) -> Dict[str, Any]:
    """Resolve unresolved baseball shadow trades (moneyline/spread/total) against
    EACH trade's OWN Polymarket market via CLOB. No game-winner heuristic, no
    event-matching, no head-of-line batch cap (every trade is attempted each run;
    still-open markets are simply retried next cycle, never blocking newer rows).
    """
    conn = get_db()
    result = {"resolved": 0, "forecast_logged": 0, "skipped": 0, "errors": 0}

    trades = _get_unresolved_baseball_trades(conn)
    if not trades:
        conn.close()
        return {**result, "note": "No unresolved baseball trades"}

    for trade in trades[:batch_size]:
        market_id = trade.get("market_id", "")
        if not market_id or not market_id.startswith("0x"):
            result["skipped"] += 1
            continue

        won = _clob_winner(market_id)
        if won is None:
            result["skipped"] += 1            # market still open
            time.sleep(RATE_DELAY)
            continue
        winner_name, winner_idx = won

        market_str = trade.get("market", "")
        side = trade.get("side", "YES")
        entry_price = trade.get("entry_price", 0.5)
        is_correct, pnl = score_baseball_trade(market_str, side, entry_price, winner_name)
        outcome_yesno = "YES" if winner_idx == 0 else "NO"
        bet_label, _mt = _parse_bet(market_str)

        reasoning = trade.get("reasoning", "")
        edge_pct = 0.0
        m = re.search(r"\(([+-]\d+\.?\d*)% edge\)", reasoning)
        if m:
            edge_pct = float(m.group(1))
        m = re.search(r"Odds API (\d+\.?\d*)%", reasoning)
        odds_api_prob = float(m.group(1)) / 100.0 if m else 0.0
        m = re.search(r"Poly (\d+\.?\d*)", reasoning)
        poly_price = float(m.group(1)) / 100.0 if m else entry_price

        try:
            conn.execute("""
                INSERT OR IGNORE INTO baseball_forecast_log
                (game_id, team, opponent, game_date, odds_api_prob, poly_price,
                 edge_pct, direction, actual_outcome, predicted_correct,
                 american_odds, books_count, shadow_trade_id, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                market_id[:20], bet_label or "unknown", winner_name or "unknown",
                date.today().isoformat(), odds_api_prob, poly_price, edge_pct,
                side, winner_name, is_correct, 0, 0, trade["id"],
                datetime.now(timezone.utc).isoformat(),
            ))
            result["forecast_logged"] += 1
        except Exception as e:
            logger.warning(f"forecast_log insert failed: {e}")

        try:
            conn.execute("""
                UPDATE shadow_trades
                SET resolved = 1, resolved_at = ?, outcome = ?, pnl = ?, exit_price = ?
                WHERE id = ?
            """, (
                datetime.now(timezone.utc).isoformat(), outcome_yesno,
                pnl, 1.0 if is_correct else 0.0, trade["id"],
            ))
            result["resolved"] += 1
        except Exception as e:
            logger.warning(f"shadow_trade update failed: {e}")
            result["errors"] += 1

        time.sleep(RATE_DELAY)

    conn.commit()
    conn.close()
    if result["resolved"] > 0:
        logger.info(
            f"baseball_resolver: {result['resolved']} resolved, "
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
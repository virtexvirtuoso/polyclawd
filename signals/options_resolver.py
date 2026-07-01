#!/usr/bin/env python3
"""
Options-Implied Resolution Watcher — Phase 1 of closed-loop calibration.

After weekly Polymarket options markets resolve (Fridays 4PM ET),
checks the actual outcome against our N(d2) implied probability,
logs prediction vs outcome to options_forecast_log, and auto-resolves
shadow trades.

z-score semantics:
  z < 0 → implied_prob < poly_price → Poly undervalued → BUY YES
  z > 0 → implied_prob > poly_price → Poly overvalued → BUY NO (= SELL)
So predicted_correct = (side == actual_outcome) where side comes from z.

Runs every 30min in scheduler alongside signal scan.
"""

import json
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Dict, List, Optional, Any

from loguru import logger

# Paths
BASE_DIR = Path(__file__).parent.parent
STORAGE_DIR = BASE_DIR / "storage"
HOME_DIR = Path.home()
OPTIONS_DB = Path(
    __import__("os").environ.get(
        "OPTIONS_DB",
        str(HOME_DIR / "polyclawd-data" / "options_implied.db"),
    )
)
SHADOW_DB = STORAGE_DIR / "shadow_trades.db"

# Polymarket APIs
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
UA = {"User-Agent": "Mozilla/5.0 polyclawd-options-resolver"}

# Tracked tickers (same as options_implied.py)
NAMES = ["NVDA", "META", "MSFT", "AAPL", "AMZN"]

# Rate limiting
RATE_DELAY = 2.0

# ─── DB Init ─────────────────────────────────────────────────────────


def get_options_db() -> sqlite3.Connection:
    """Get connection to options_implied.db."""
    OPTIONS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(OPTIONS_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _init_tables(conn)
    return conn


def get_shadow_db() -> sqlite3.Connection:
    """Get connection to shadow_trades.db."""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SHADOW_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init_tables(conn: sqlite3.Connection):
    """Create options_forecast_log table if not exists."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS options_forecast_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            expiry TEXT,
            strike REAL,
            market_type TEXT,
            implied_prob REAL,
            poly_price REAL,
            spread_pp REAL,
            z_score REAL,
            poly_market_id TEXT,
            actual_outcome TEXT,
            predicted_side TEXT,
            predicted_correct INTEGER,
            resolved_at TEXT,
            recorded_at TEXT,
            UNIQUE(poly_market_id)
        );
    """)
    conn.commit()


# ─── Polymarket API ──────────────────────────────────────────────────


def _fetch_json(url: str, timeout: int = 10) -> Optional[Any]:
    """Fetch JSON. Returns None on any failure."""
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug(f"options_resolver fetch failed: {url[:60]} - {e}")
        return None


def _get_resolved_close_events(ticker: str) -> List[Dict]:
    """Fetch resolved Polymarket events matching a ticker's close markets.
    Uses public-search endpoint with events_status=closed.
    Returns list of event dicts, or [] on failure.
    """
    data = _fetch_json(
        f"{GAMMA_API}/public-search?"
        + urllib.parse.urlencode({
            "q": f"{ticker} close",
            "limit_per_type": 20,
            "events_status": "closed",
        }),
        timeout=15,
    )
    if isinstance(data, dict):
        events = data.get("events", [])
    elif isinstance(data, list):
        events = data
    else:
        return []

    # Filter to events that actually match our ticker
    ticker_lower = ticker.lower()
    return [
        e
        for e in events
        if ticker_lower in e.get("slug", "").lower()
        and ("week" in e.get("slug", "").lower() or "close" in e.get("slug", "").lower())
    ]


def _check_clob_resolution(condition_id: str) -> Optional[str]:
    """Check CLOB API for definitive resolution.
    Returns 'YES', 'NO', or None if still open.
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


def _get_market_outcome_from_event(event: Dict) -> Dict[str, str]:
    """Get resolution outcome per market in a closed event.
    Returns {market_conditionId: 'YES'/'NO'/None}.
    Uses Gamma's outcomePrices first, falls back to CLOB.
    """
    results = {}
    for market in event.get("markets", []):
        condition_id = market.get("conditionId", "") or market.get("id", "")
        if not condition_id:
            continue

        # Try CLOB first (authoritative)
        clob_result = _check_clob_resolution(condition_id)
        if clob_result:
            results[condition_id] = clob_result
            continue

        # Fallback: Gamma outcomePrices heuristic
        prices_raw = market.get("outcomePrices", "[]")
        if isinstance(prices_raw, str):
            try:
                prices = json.loads(prices_raw)
            except (json.JSONDecodeError, ValueError):
                continue
        else:
            prices = prices_raw
        if len(prices) >= 2:
            try:
                p0 = float(prices[0])
                p1 = float(prices[1])
            except (ValueError, TypeError):
                continue
            if p0 >= 0.99:
                results[condition_id] = "YES"
            elif p1 >= 0.99:
                results[condition_id] = "NO"

    return results


# ─── Resolution Matching ─────────────────────────────────────────────


def _get_unresolved_options_implied_rows(conn) -> List[Dict]:
    """Get rows from options_implied table with poly_market_id that haven't
    been resolved yet (not in options_forecast_log)."""
    rows = conn.execute("""
        SELECT o.date, o.poly_market_id, o.ticker, o.expiry, o.strike,
               o.market_type, o.poly_price, o.implied_prob, o.spread_pp,
               o.underlying, o.iv
        FROM options_implied o
        LEFT JOIN options_forecast_log f ON o.poly_market_id = f.poly_market_id
        WHERE f.poly_market_id IS NULL
          AND o.poly_market_id IS NOT NULL
          AND o.poly_market_id != ''
        ORDER BY o.date DESC
        LIMIT 200
    """).fetchall()
    return [dict(r) for r in rows]


def _get_matching_shadow_trade(conn, poly_market_id: str) -> Optional[Dict]:
    """Find an unresolved shadow trade with matching market_id."""
    row = conn.execute(
        "SELECT id, side, entry_price, confidence FROM shadow_trades "
        "WHERE resolved = 0 AND market_id = ? AND strategy = 'options_implied'",
        (poly_market_id,),
    ).fetchone()
    return dict(row) if row else None


def _determine_predicted_side(z_score: float) -> str:
    """z < 0 → implied < poly → market underpriced → BUY YES
    z > 0 → implied > poly → market overpriced → SELL (= BUY NO)"""
    return "YES" if z_score < 0 else "NO"


# ─── Main Resolution Scan ────────────────────────────────────────────


def scan_resolved_options_markets() -> Dict[str, Any]:
    """Main entry point. Checks all tracked tickers for resolved markets,
    logs forecast accuracy, resolves shadow trades.

    Returns summary dict with counts.
    """
    result = {"resolved": 0, "forecast_logged": 0, "skipped": 0, "errors": 0}

    # 1. Get unresolved rows from options_implied.db
    try:
        oconn = get_options_db()
    except Exception as e:
        return {**result, "note": f"Can't open options DB: {e}"}

    rows = _get_unresolved_options_implied_rows(oconn)
    if not rows:
        oconn.close()
        return {**result, "note": "No unresolved options rows"}

    # Build market_id lookup
    unresolved_markets = {r["poly_market_id"]: r for r in rows}

    # 2. Fetch resolved events per ticker
    event_market_outcomes = {}  # conditionId -> outcome
    all_events = []
    for ticker in NAMES:
        events = _get_resolved_close_events(ticker)
        all_events.extend(events)
        for event in events:
            outcomes = _get_market_outcome_from_event(event)
            for cid, outcome in outcomes.items():
                if outcome:
                    event_market_outcomes[cid] = outcome
        time.sleep(RATE_DELAY)  # rate limit per ticker

    if not event_market_outcomes:
        oconn.close()
        return {**result, "note": "No resolved events from Polymarket"}

    # 3. Match and process
    sconn = get_shadow_db()

    for poly_market_id, row in unresolved_markets.items():
        outcome = event_market_outcomes.get(poly_market_id)
        if not outcome:
            continue

        shadow = _get_matching_shadow_trade(sconn, poly_market_id)
        predicted_side = None
        if shadow:
            predicted_side = shadow["side"]
        else:
            # Fallback: if no shadow trade, infer from spread sign
            result["skipped"] += 1
            continue

        is_correct = 1 if predicted_side == outcome else 0

        # 4. Log to options_forecast_log
        try:
            oconn.execute("""
                INSERT OR IGNORE INTO options_forecast_log
                (ticker, expiry, strike, market_type, implied_prob, poly_price,
                 spread_pp, z_score, poly_market_id, actual_outcome,
                 predicted_side, predicted_correct, resolved_at, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                row.get("ticker", ""),
                row.get("expiry", ""),
                row.get("strike", 0),
                row.get("market_type", ""),
                row.get("implied_prob", 0),
                row.get("poly_price", 0),
                row.get("spread_pp", 0),
                row.get("spread_pp", 0) / 3.0,  # approx z (SD_FLOOR=0.5, z=spread/0.5/2 -> rough)
                poly_market_id,
                outcome,
                predicted_side,
                is_correct,
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ))
            result["forecast_logged"] += 1
        except Exception as e:
            logger.warning(f"forecast_log insert failed: {e}")
            result["errors"] += 1
            continue

        # 5. Update shadow trade if one exists
        if shadow:
            entry_price = shadow.get("entry_price", 0.5)
            if outcome == "YES":
                pnl = (1.0 - entry_price) if predicted_side == "YES" else -entry_price
            else:
                pnl = -entry_price if predicted_side == "YES" else entry_price

            try:
                sconn.execute("""
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
                    shadow["id"],
                ))
                result["resolved"] += 1
            except Exception as e:
                logger.warning(f"shadow_trade update failed: {e}")
                result["errors"] += 1

    oconn.commit()
    oconn.close()
    sconn.commit()
    sconn.close()

    if result["resolved"] > 0 or result["forecast_logged"] > 0:
        logger.info(
            f"options_resolver: {result['resolved']} resolved, "
            f"{result['forecast_logged']} logged, "
            f"{result['skipped']} skipped, {result['errors']} errors"
        )

    return result


# ─── Accuracy Stats ──────────────────────────────────────────────────


def get_options_accuracy_summary() -> Dict[str, Any]:
    """Return accuracy stats by ticker, market_type, and z-score bucket
    from options_forecast_log. Returns empty state if no records."""
    try:
        conn = get_options_db()
    except Exception:
        return {"total": 0, "by_ticker": {}, "by_market_type": {}, "by_z_bucket": {}}

    # Total
    total_row = conn.execute("""
        SELECT COUNT(*) as total,
               SUM(predicted_correct) as correct,
               ROUND(AVG(CASE WHEN predicted_correct = 1 THEN 1.0 ELSE 0.0 END) * 100, 1) as accuracy
        FROM options_forecast_log
    """).fetchone()
    total = total_row["total"] if total_row else 0

    if not total:
        conn.close()
        return {"total": 0, "by_ticker": {}, "by_market_type": {}, "by_z_bucket": {}}

    # By ticker
    by_ticker = {}
    for r in conn.execute("""
        SELECT ticker, COUNT(*) as total,
               SUM(predicted_correct) as correct,
               ROUND(AVG(CASE WHEN predicted_correct = 1 THEN 1.0 ELSE 0.0 END) * 100, 1) as accuracy
        FROM options_forecast_log
        GROUP BY ticker
        ORDER BY total DESC
    """).fetchall():
        by_ticker[r["ticker"]] = {
            "total": r["total"], "correct": r["correct"], "accuracy": r["accuracy"]
        }

    # By market_type
    by_market_type = {}
    for r in conn.execute("""
        SELECT market_type, COUNT(*) as total,
               SUM(predicted_correct) as correct,
               ROUND(AVG(CASE WHEN predicted_correct = 1 THEN 1.0 ELSE 0.0 END) * 100, 1) as accuracy
        FROM options_forecast_log
        GROUP BY market_type
        ORDER BY total DESC
    """).fetchall():
        by_market_type[r["market_type"]] = {
            "total": r["total"], "correct": r["correct"], "accuracy": r["accuracy"]
        }

    # Recent resolved
    recent = []
    for r in conn.execute("""
        SELECT ticker, expiry, strike, market_type, implied_prob, poly_price,
               spread_pp, actual_outcome, predicted_side, predicted_correct, resolved_at
        FROM options_forecast_log
        ORDER BY resolved_at DESC
        LIMIT 20
    """).fetchall():
        recent.append({
            "ticker": r["ticker"],
            "expiry": r["expiry"],
            "strike": r["strike"],
            "market_type": r["market_type"],
            "implied_prob": round(r["implied_prob"] * 100, 1) if r["implied_prob"] else None,
            "poly_price": round(r["poly_price"] * 100, 1) if r["poly_price"] else None,
            "spread_pp": round(r["spread_pp"], 2) if r["spread_pp"] else None,
            "actual_outcome": r["actual_outcome"],
            "predicted_side": r["predicted_side"],
            "correct": bool(r["predicted_correct"]),
        })

    conn.close()
    return {
        "total": total,
        "accuracy": round(total_row["correct"] / total * 100, 1) if total > 0 else 0,
        "by_ticker": by_ticker,
        "by_market_type": by_market_type,
        "recent": recent,
        "collection": {"resolved": total, "target": 30, "pct": min(100, round(total / 30 * 100, 1))},
    }


# ─── CLI ─────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import logging as builtin_logging
    builtin_logging.basicConfig(level=builtin_logging.INFO, format="%(message)s")
    result = scan_resolved_options_markets()
    print(f"Resolved: {result['resolved']}")
    print(f"Forecast logged: {result['forecast_logged']}")
    print(f"Skipped: {result['skipped']}")
    print(f"Errors: {result['errors']}")
    if result.get("note"):
        print(f"Note: {result['note']}")
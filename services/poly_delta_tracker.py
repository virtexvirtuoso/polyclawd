"""
Poly Delta Tracker — adverse selection detection for Polymarket fills.

After each PM fill in shadow_trades, snapshots market mid price at:
  +60s  → poly_delta_60
  +300s → poly_delta_300

delta = mid_at_T - entry_price
  positive = market moved in our direction (good fill)
  negative = adverse selection (faster bots filled at better prices)

Surfaces in daily summary as avg_poly_delta by signal source.
Called from scheduler tick_5min — lightweight, only processes fills < 20min old.
"""

import json
import sqlite3
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from loguru import logger
from config.polymarket_urls import GAMMA_API, CLOB_API  # polyproxy: central URL config

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "storage" / "shadow_trades.db"

def _ensure_columns():
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute("PRAGMA journal_mode=WAL")
    existing = {row[1] for row in conn.execute("PRAGMA table_info(shadow_trades)")}
    for col in ("poly_delta_60", "poly_delta_300"):
        if col not in existing:
            conn.execute(f"ALTER TABLE shadow_trades ADD COLUMN {col} REAL")
    conn.commit()
    conn.close()

def _fetch_json(url: str, timeout: int = 8) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug("poly_delta fetch failed {}: {}", url, e)
        return None

def _get_mid_price(market_id: str, side: str) -> Optional[float]:
    """Fetch PM CLOB mid for a market_id (hex condition_id) + side."""
    # Gamma rejects /markets/{condition_id} (path) with 422; the correct lookup
    # is the condition_ids query param, which returns a LIST (matches the working
    # poly_executable_edge path). Fixed 2026-06-20 — was 0/331 populated.
    resp = _fetch_json(f"{GAMMA_API}/markets?condition_ids={market_id}")
    if not resp:
        return None
    market = resp[0] if isinstance(resp, list) else resp
    if not market:
        return None

    clob_token_ids = market.get("clobTokenIds", "[]")
    outcomes = market.get("outcomes", "[]")
    if isinstance(clob_token_ids, str):
        clob_token_ids = json.loads(clob_token_ids)
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)

    token_id = None
    side_upper = (side or "YES").upper()
    for i, outcome in enumerate(outcomes):
        if outcome.upper() == side_upper and i < len(clob_token_ids):
            token_id = clob_token_ids[i]
            break
    if not token_id:
        # No outcome matched `side` (e.g. named-outcome market + side "NO").
        # Guessing token[0] here measured the WRONG outcome — refuse instead.
        logger.debug("poly_delta: side {} matches no outcome for {}", side, market_id)
        return None

    book = _fetch_json(f"{CLOB_API}/book?token_id={token_id}")
    if not book:
        return None

    bids = sorted([float(b["price"]) for b in book.get("bids", [])], reverse=True)
    asks = sorted([float(a["price"]) for a in book.get("asks", [])])
    if not bids or not asks:
        return None
    return round((bids[0] + asks[0]) / 2, 4)

def run_once():
    """Process pending poly delta snapshots. Called every 5 minutes."""
    _ensure_columns()

    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute("PRAGMA journal_mode=WAL")
    # Cutoffs computed in Python so both comparison sides share the ISO-T
    # format. Comparing against sqlite datetime() (space-separated) could
    # never match same-day rows — deltas were only written by a once-nightly
    # sweep just after UTC midnight, with real lag anywhere from 1min to 24h.
    now = datetime.now(timezone.utc)
    cut_60 = (now - timedelta(seconds=60)).isoformat()
    floor_60 = (now - timedelta(minutes=10)).isoformat()
    cut_300 = (now - timedelta(seconds=300)).isoformat()
    floor_300 = (now - timedelta(minutes=20)).isoformat()

    # Fills needing delta_60: filled 60s–10min ago, delta_60 not set
    rows_60 = conn.execute(
        """
        SELECT id, market_id, side, entry_price
        FROM shadow_trades
        WHERE platform = 'polymarket'
          AND resolved = 0
          AND entry_price IS NOT NULL
          AND poly_delta_60 IS NULL
          AND timestamp <= ?
          AND timestamp >= ?
    """,
        (cut_60, floor_60),
    ).fetchall()

    # Fills needing delta_300: filled 5–20min ago, delta_300 not set (delta_60 already captured)
    rows_300 = conn.execute(
        """
        SELECT id, market_id, side, entry_price
        FROM shadow_trades
        WHERE platform = 'polymarket'
          AND resolved = 0
          AND entry_price IS NOT NULL
          AND poly_delta_300 IS NULL
          AND poly_delta_60 IS NOT NULL
          AND timestamp <= ?
          AND timestamp >= ?
    """,
        (cut_300, floor_300),
    ).fetchall()

    conn.close()

    updated_60, updated_300 = 0, 0

    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute("PRAGMA journal_mode=WAL")

    for row_id, market_id, side, entry_price in rows_60:
        mid = _get_mid_price(market_id, side)
        if mid is None:
            continue
        delta = round(mid - entry_price, 4)
        conn.execute(
            "UPDATE shadow_trades SET poly_delta_60 = ? WHERE id = ?",
            (delta, row_id),
        )
        updated_60 += 1

    for row_id, market_id, side, entry_price in rows_300:
        mid = _get_mid_price(market_id, side)
        if mid is None:
            continue
        delta = round(mid - entry_price, 4)
        conn.execute(
            "UPDATE shadow_trades SET poly_delta_300 = ? WHERE id = ?",
            (delta, row_id),
        )
        updated_300 += 1

    conn.commit()
    conn.close()

    if updated_60 or updated_300:
        logger.info(
            "poly_delta_tracker: delta_60={} delta_300={} updated",
            updated_60,
            updated_300,
        )

    return {"updated_60": updated_60, "updated_300": updated_300}

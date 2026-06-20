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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "storage" / "shadow_trades.db"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


def _ensure_columns():
    conn = sqlite3.connect(str(DB_PATH))
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
    market = _fetch_json(f"{GAMMA_API}/markets/{market_id}")
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
    if token_id is None and clob_token_ids:
        token_id = clob_token_ids[0]
    if not token_id:
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

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    now_iso = datetime.now(timezone.utc).isoformat()

    # Fills needing delta_60: filled 60s–10min ago, delta_60 not set
    rows_60 = conn.execute("""
        SELECT id, market_id, side, entry_price
        FROM shadow_trades
        WHERE platform = 'polymarket'
          AND resolved = 0
          AND poly_delta_60 IS NULL
          AND timestamp <= datetime(?, '-60 seconds')
          AND timestamp >= datetime(?, '-10 minutes')
    """, (now_iso, now_iso)).fetchall()

    # Fills needing delta_300: filled 5–20min ago, delta_300 not set (delta_60 already captured)
    rows_300 = conn.execute("""
        SELECT id, market_id, side, entry_price
        FROM shadow_trades
        WHERE platform = 'polymarket'
          AND resolved = 0
          AND poly_delta_300 IS NULL
          AND poly_delta_60 IS NOT NULL
          AND timestamp <= datetime(?, '-300 seconds')
          AND timestamp >= datetime(?, '-20 minutes')
    """, (now_iso, now_iso)).fetchall()

    conn.close()

    updated_60, updated_300 = 0, 0

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")

    for row_id, market_id, side, entry_price in rows_60:
        mid = _get_mid_price(market_id, side)
        if mid is None:
            continue
        delta = round(mid - (entry_price or 0.5), 4)
        conn.execute(
            "UPDATE shadow_trades SET poly_delta_60 = ? WHERE id = ?",
            (delta, row_id),
        )
        updated_60 += 1

    for row_id, market_id, side, entry_price in rows_300:
        mid = _get_mid_price(market_id, side)
        if mid is None:
            continue
        delta = round(mid - (entry_price or 0.5), 4)
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
            updated_60, updated_300,
        )

    return {"updated_60": updated_60, "updated_300": updated_300}

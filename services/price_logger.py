"""
Position Price Logger — Phase 0 of Adaptive Exit System

Logs current market prices for all open positions every tick.
Pure data collection — no exits, no alerts. Just building the dataset
for learning optimal exit curves per strategy.

Table: position_price_log
  - position_id (FK → paper_positions.id)
  - timestamp (ISO 8601 UTC)
  - market_price (YES token price from Polymarket/Kalshi)
  - edge_current (re-estimated edge at snapshot time, nullable for Phase 0)

Called from scheduler tick_5min().
"""

import sqlite3
import time
import urllib.request
import json
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from loguru import logger

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "storage" / "shadow_trades.db"

CLOB_API = "https://clob.polymarket.com"
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"


def _ensure_table():
    """Create position_price_log table if it doesn't exist."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS position_price_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            market_price REAL,
            edge_current REAL,
            FOREIGN KEY (position_id) REFERENCES paper_positions(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_ppl_position_ts
        ON position_price_log(position_id, timestamp)
    """)
    conn.commit()
    conn.close()


def _fetch_url(url: str, timeout: int = 10):
    """Fetch JSON from URL."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug("Price fetch failed for {}: {}", url, e)
        return None


def _fetch_position_price(pos: dict) -> tuple:
    """Fetch current YES token price for a position. Returns (position_id, price)."""
    market_id = pos["market_id"]
    platform = pos.get("platform") or "kalshi"
    pos_id = pos["id"]

    if platform == "polymarket" or market_id.startswith("0x"):
        data = _fetch_url(f"{CLOB_API}/markets/{market_id}")
        if data:
            tokens = data.get("tokens", [])
            if tokens:
                return (pos_id, float(tokens[0].get("price", 0)))
    else:
        data = _fetch_url(f"{KALSHI_API}/markets/{market_id}")
        if data:
            market = data.get("market", data)
            cp = market.get("last_price")
            if cp and cp > 1:
                cp = cp / 100
            return (pos_id, cp)

    return (pos_id, None)


def log_position_prices():
    """
    Fetch and log current prices for all open positions.
    Called every 5 minutes from scheduler.
    """
    _ensure_table()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, market_id, platform, side, entry_price, strategy "
        "FROM paper_positions WHERE status = 'open'"
    ).fetchall()
    conn.close()

    if not rows:
        return 0

    positions = [dict(r) for r in rows]
    now = datetime.now(timezone.utc).isoformat()

    # Parallel price fetch
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_fetch_position_price, positions))

    # Batch insert
    inserts = []
    for pos_id, price in results:
        if price is not None:
            inserts.append((pos_id, now, round(price, 6), None))

    if inserts:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executemany(
            "INSERT INTO position_price_log (position_id, timestamp, market_price, edge_current) "
            "VALUES (?, ?, ?, ?)",
            inserts,
        )
        conn.commit()
        conn.close()

    logger.info("Price logger: logged {}/{} open positions", len(inserts), len(positions))
    return len(inserts)


if __name__ == "__main__":
    n = log_position_prices()
    print(f"Logged {n} position prices")

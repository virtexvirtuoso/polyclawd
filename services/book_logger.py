"""
Position Orderbook Logger — Phase 0 of Adaptive Stop System (orderbook arm)

Logs orderbook microstructure for every open Polymarket position every 5 min.
Pure data collection — no exits, no trading impact. Builds the dataset that
will power orderbook-aware stop logic and microstructure backtests.

Schema: position_book_log
  - position_id      → paper_positions.id
  - timestamp        ISO 8601 UTC
  - token_id         CLOB token id of the ADVERSE side (the side that, if
                     bought, hurts our position). For a NO holder this is
                     the YES token; for a YES holder it's the NO token.
  - best_bid, best_ask, spread, microprice
  - bid_depth_3pp, ask_depth_3pp     USD depth within 3pp of top
  - bid_depth_5pp, ask_depth_5pp
  - l1_bid_size, l1_ask_size         contracts at top
  - imbalance_3pp                    (bid - ask) / (bid + ask) within 3pp
  - fetch_latency_ms

Called from scheduler tick_5min(). Kalshi positions are skipped (Polymarket-
only for now).
"""

import json
import sqlite3
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "storage" / "shadow_trades.db"
CLOB_API = "https://clob.polymarket.com"


def _ensure_table():
    """Create position_book_log table if it doesn't exist."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS position_book_log (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id      INTEGER NOT NULL,
            timestamp        TEXT    NOT NULL,
            token_id         TEXT,
            best_bid         REAL,
            best_ask         REAL,
            spread           REAL,
            microprice       REAL,
            bid_depth_3pp    REAL,
            ask_depth_3pp    REAL,
            bid_depth_5pp    REAL,
            ask_depth_5pp    REAL,
            l1_bid_size      REAL,
            l1_ask_size      REAL,
            imbalance_3pp    REAL,
            fetch_latency_ms INTEGER,
            FOREIGN KEY (position_id) REFERENCES paper_positions(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pbl_position_ts
        ON position_book_log(position_id, timestamp)
    """)
    conn.commit()
    conn.close()


def _fetch_url(url: str, timeout: int = 10):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug("Book fetch failed for {}: {}", url, e)
        return None


def _depth_within_pp(levels, top_price: float, pp: float) -> float:
    """Sum price*size for levels within `pp` percentage points of `top_price`.
    `levels` should already be sorted best-first (bids descending, asks ascending).
    """
    cutoff = top_price - pp / 100.0 if levels and top_price >= 0.5 else top_price + pp / 100.0
    # Bids: include price >= top - pp; Asks: include price <= top + pp
    # The caller passes the side's own top; we detect direction from levels[0] vs levels[-1].
    if len(levels) >= 2 and levels[0]["price"] > levels[1]["price"]:
        # bids (descending)
        return sum(float(l["price"]) * float(l["size"])
                   for l in levels if float(l["price"]) >= top_price - pp / 100.0)
    # asks (ascending) or single-level
    return sum(float(l["price"]) * float(l["size"])
               for l in levels if float(l["price"]) <= top_price + pp / 100.0)


def _snapshot_book(pos: dict) -> dict | None:
    """Fetch + summarize the adverse-side book for one position.
    Returns a dict ready for INSERT, or None on failure.
    """
    market_id = pos["market_id"]
    side = (pos.get("side") or "").upper()
    platform = (pos.get("platform") or "kalshi").lower()
    if platform != "polymarket" and not market_id.startswith("0x"):
        return None  # Kalshi has a different book API; skip for now

    # Step 1: get market metadata to find adverse-side token_id
    market = _fetch_url(f"{CLOB_API}/markets/{market_id}")
    if not market:
        return None
    tokens = {(t.get("outcome") or "").lower(): t.get("token_id")
              for t in market.get("tokens", [])}
    adverse_outcome = "yes" if side == "NO" else "no"
    token_id = tokens.get(adverse_outcome)
    if not token_id:
        return None

    # Step 2: fetch the adverse-side book
    t0 = time.time()
    book = _fetch_url(f"{CLOB_API}/book?token_id={token_id}")
    latency_ms = int((time.time() - t0) * 1000)
    if not book:
        return None

    # Polymarket /book returns bids ascending and asks descending (worst-first).
    # Normalize to best-first.
    bids = sorted(book.get("bids", []), key=lambda b: -float(b["price"]))
    asks = sorted(book.get("asks", []), key=lambda a: float(a["price"]))
    if not bids or not asks:
        return None

    best_bid = float(bids[0]["price"])
    best_ask = float(asks[0]["price"])
    l1_bid_sz = float(bids[0]["size"])
    l1_ask_sz = float(asks[0]["size"])
    spread = best_ask - best_bid
    midprice = (best_bid + best_ask) / 2.0
    # Stoikov microprice: size-weighted top-of-book toward the heavier side
    if l1_bid_sz + l1_ask_sz > 0:
        microprice = (best_bid * l1_ask_sz + best_ask * l1_bid_sz) / (l1_bid_sz + l1_ask_sz)
    else:
        microprice = midprice

    bid_3 = sum(float(b["price"]) * float(b["size"])
                for b in bids if float(b["price"]) >= best_bid - 0.03)
    ask_3 = sum(float(a["price"]) * float(a["size"])
                for a in asks if float(a["price"]) <= best_ask + 0.03)
    bid_5 = sum(float(b["price"]) * float(b["size"])
                for b in bids if float(b["price"]) >= best_bid - 0.05)
    ask_5 = sum(float(a["price"]) * float(a["size"])
                for a in asks if float(a["price"]) <= best_ask + 0.05)
    total_3 = bid_3 + ask_3
    imbalance_3 = (bid_3 - ask_3) / total_3 if total_3 > 0 else 0.0

    return {
        "position_id":      pos["id"],
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "token_id":         token_id,
        "best_bid":         round(best_bid, 4),
        "best_ask":         round(best_ask, 4),
        "spread":           round(spread, 4),
        "microprice":       round(microprice, 4),
        "bid_depth_3pp":    round(bid_3, 2),
        "ask_depth_3pp":    round(ask_3, 2),
        "bid_depth_5pp":    round(bid_5, 2),
        "ask_depth_5pp":    round(ask_5, 2),
        "l1_bid_size":      round(l1_bid_sz, 2),
        "l1_ask_size":      round(l1_ask_sz, 2),
        "imbalance_3pp":    round(imbalance_3, 4),
        "fetch_latency_ms": latency_ms,
    }


def log_position_books():
    """Snapshot adverse-side orderbook for every open Polymarket position."""
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
    with ThreadPoolExecutor(max_workers=4) as pool:
        snapshots = list(pool.map(_snapshot_book, positions))

    rows_to_insert = [s for s in snapshots if s is not None]
    if rows_to_insert:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        cols = ("position_id, timestamp, token_id, best_bid, best_ask, spread, "
                "microprice, bid_depth_3pp, ask_depth_3pp, bid_depth_5pp, "
                "ask_depth_5pp, l1_bid_size, l1_ask_size, imbalance_3pp, "
                "fetch_latency_ms")
        placeholders = ", ".join(["?"] * 15)
        conn.executemany(
            f"INSERT INTO position_book_log ({cols}) VALUES ({placeholders})",
            [(s["position_id"], s["timestamp"], s["token_id"], s["best_bid"],
              s["best_ask"], s["spread"], s["microprice"], s["bid_depth_3pp"],
              s["ask_depth_3pp"], s["bid_depth_5pp"], s["ask_depth_5pp"],
              s["l1_bid_size"], s["l1_ask_size"], s["imbalance_3pp"],
              s["fetch_latency_ms"]) for s in rows_to_insert],
        )
        conn.commit()
        conn.close()

    logger.info("Book logger: snapshotted {}/{} open positions",
                len(rows_to_insert), len(positions))
    return len(rows_to_insert)


if __name__ == "__main__":
    n = log_position_books()
    print(f"Logged {n} position books")

"""
CLV Snapshot Service — T-10min closing line value capture.

Snapshots the Polymarket CLOB mid price at T-10min before market resolution.
This is the industry-standard CLV window (per r/algobetting consensus):
  "The whole point of CLV is where the market settles once all information
   is priced in. Within 5-10 minutes of tip is ideal." — u/Delicious_Pipe_1326

Earlier snapshots (hours before) underestimate edge because late sharp action
dominates the true closing line.

Columns added to paper_positions:
  clv_price          REAL  — PM mid at T-10min
  clv_snapshot_at    TEXT  — ISO UTC timestamp when snapshot taken
  clv_edge_vs_close  REAL  — entry_price - clv_price
                             positive = better entry than close (CLV positive)
                             negative = worse entry than close (CLV negative)

Called from scheduler tick_5min. Only snapshots open PM positions where
the market resolves within the next 30 minutes (and hasn't been snapshotted).
"""
import json
from db import connect as db_connect
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

# Snapshot window: between T-30min and T-5min before resolution
WINDOW_EARLY_S = 1800   # 30 min before
WINDOW_LATE_S  = 300    # 5 min before (don't snapshot too close — market may be locked)


def _ensure_columns():
    conn = db_connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    existing = {row[1] for row in conn.execute("PRAGMA table_info(paper_positions)")}
    for col, typ in [
        ("clv_price", "REAL"),
        ("clv_snapshot_at", "TEXT"),
        ("clv_edge_vs_close", "REAL"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE paper_positions ADD COLUMN {col} {typ}")
    conn.commit()
    conn.close()


def _fetch_json(url: str, timeout: int = 8) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug("clv_snapshot fetch failed {}: {}", url, e)
        return None


def _get_resolution_dt(market_slug: str) -> Optional[datetime]:
    """Fetch end_date_iso from Gamma API for a market slug."""
    if not market_slug:
        return None
    data = _fetch_json(f"{GAMMA_API}/markets?slug={market_slug}&limit=1")
    if not data or not isinstance(data, list):
        return None
    market = data[0] if data else {}
    end_date = market.get("endDateIso") or market.get("end_date_iso")
    if not end_date:
        return None
    try:
        dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _get_mid_price(market_id: str, side: str) -> Optional[float]:
    """Fetch PM CLOB mid for market_id (hex condition_id) + side."""
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
    """Snapshot CLV for open PM positions approaching resolution."""
    _ensure_columns()

    conn = db_connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    rows = conn.execute("""
        SELECT id, market_id, market_slug, side, entry_price
        FROM paper_positions
        WHERE status = 'open'
          AND platform = 'polymarket'
          AND clv_snapshot_at IS NULL
          AND market_slug IS NOT NULL
          AND market_slug != ''
    """).fetchall()
    conn.close()

    if not rows:
        return {"checked": 0, "snapshotted": 0}

    now = datetime.now(timezone.utc)
    snapshotted = 0

    conn = db_connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")

    for pos_id, market_id, market_slug, side, entry_price in rows:
        resolution_dt = _get_resolution_dt(market_slug)
        if resolution_dt is None:
            continue

        time_to_close = (resolution_dt - now).total_seconds()

        # Only snapshot inside the window: T-30min to T-5min
        if not (WINDOW_LATE_S <= time_to_close <= WINDOW_EARLY_S):
            continue

        mid = _get_mid_price(market_id, side)
        if mid is None:
            logger.warning("clv_snapshot: no mid for {} ({})", market_slug, market_id)
            continue

        clv_edge = round((entry_price or 0.5) - mid, 4)
        snapshot_ts = now.isoformat()

        conn.execute("""
            UPDATE paper_positions
            SET clv_price = ?, clv_snapshot_at = ?, clv_edge_vs_close = ?
            WHERE id = ?
        """, (mid, snapshot_ts, clv_edge, pos_id))

        direction = "ahead" if clv_edge > 0 else "behind"
        logger.info(
            "CLV snapshot pos={} {} {} | entry={:.3f} clv={:.3f} edge={:+.3f}pp ({}) T-{:.0f}min",
            pos_id, market_slug[:40], side,
            entry_price, mid, clv_edge * 100, direction,
            time_to_close / 60,
        )
        snapshotted += 1

    conn.commit()
    conn.close()

    return {"checked": len(rows), "snapshotted": snapshotted}

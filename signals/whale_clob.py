#!/usr/bin/env python3
"""
Polymarket CLOB order flow — detect large resting orders from known whale wallets.

Uses REST polling (every 60s) instead of websockets for reliability:
- Fetch order book for top-20 whale-active markets via CLOB REST API
- Check for large resting orders (>$5K) from known whale wallets
- Store alerts in whale_meta.db as whale_intent alerts

CLI:
    python3 signals/whale_clob.py --scan        # single scan
    python3 signals/whale_clob.py --summary     # recent alerts
"""

import argparse
import json
import logging
import sqlite3
import sys
import os
import time
import urllib.request
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
META_DB_PATH = BASE_DIR / "storage" / "whale_meta.db"
CLOB_REST_URL = "https://clob.polymarket.com/orderbook"
CLOB_MARKETS_URL = "https://clob.polymarket.com/markets"

# Thresholds
MIN_RESTING_USD = 5000.0  # $5K minimum to be a "large" resting order
MAX_MARKETS_PER_SCAN = 20  # top N markets by whale activity
ALERT_COOLDOWN_S = 3600  # 1 hour per market per wallet


def get_meta_db(path: Optional[Path] = None) -> sqlite3.Connection:
    db_path = Path(path) if path else META_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS whale_intent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            market TEXT NOT NULL,
            token_id TEXT,
            side TEXT NOT NULL,
            price REAL,
            size_usd REAL,
            total_usd REAL,
            is_whale INTEGER DEFAULT 0,
            ts REAL,
            updated REAL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clob_scan_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )""")
    conn.commit()
    return conn


def _fetch_json(url: str, timeout: int = 20):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        logger.debug("Fetch failed: %s %s", url, e)
        return None


def _get_whale_wallets(meta) -> dict:
    """Return dict of wallet -> name for all smart wallets."""
    return {r["wallet"]: r["name"] or r["wallet"][:10]
            for r in meta.execute(
                "SELECT wallet, name FROM pm_wallets WHERE smart=1"
            ).fetchall()}


def _get_whale_active_markets(meta) -> list:
    """Get top markets by whale activity from whale_follows."""
    rows = meta.execute("""
        SELECT market, COUNT(*) as whale_count
        FROM whale_follows
        WHERE wallet IS NOT NULL AND wallet != ''
        GROUP BY market
        ORDER BY whale_count DESC
        LIMIT ?
    """, (MAX_MARKETS_PER_SCAN,)).fetchall()
    return [r["market"] for r in rows]


def _resolve_token_id(market_slug: str) -> Optional[str]:
    """Resolve a Polymarket slug to a CLOB token ID via Gamma API."""
    # Gamma API is the reliable way to resolve slugs to token IDs
    gamma_url = f"https://gamma-api.polymarket.com/markets?slug={market_slug}"
    gamma_data = _fetch_json(gamma_url)
    if gamma_data:
        # Gamma can return a list (multiple) or a dict (single market)
        if isinstance(gamma_data, dict):
            gamma_data = [gamma_data]
        if isinstance(gamma_data, list) and len(gamma_data) > 0:
            first = gamma_data[0]
            if isinstance(first, dict):
                # clobTokenIds can be a dict {"yes": "0x...", "no": "0x..."}
                # or a list ["yes_token", "no_token"]
                token_ids = first.get("clobTokenIds", {})
                if isinstance(token_ids, dict):
                    for key in ("yes", "no", "0", "1"):
                        if key in token_ids:
                            return token_ids[key]
                    return list(token_ids.values())[0]
                elif isinstance(token_ids, list) and len(token_ids) > 0:
                    return token_ids[0]

    return None


def _get_orderbook(token_id: str) -> Optional[dict]:
    """Fetch order book for a CLOB token ID."""
    return _fetch_json(f"{CLOB_REST_URL}?market={token_id}")


def scan_orderbooks(meta: sqlite3.Connection) -> dict:
    """Scan order books for top whale-active markets, detect large resting orders.

    Returns dict with scan results.
    """
    now = time.time()
    whale_wallets = _get_whale_wallets(meta)
    whale_addresses = set(whale_wallets.keys())

    markets = _get_whale_active_markets(meta)
    if not markets:
        logger.info("CLOB scan: no whale-active markets found")
        return {"scanned": 0, "alerts": 0, "large_orders": 0}

    alerts = 0
    large_orders = 0
    scanned = 0

    for market_slug in markets:
        token_id = _resolve_token_id(market_slug)
        if not token_id:
            logger.debug("CLOB: no token_id for %s", market_slug)
            continue

        ob = _get_orderbook(token_id)
        if not ob:
            continue

        scanned += 1
        bids = ob.get("bids", [])
        asks = ob.get("asks", [])

        # Check bids (buy orders)
        for order in bids:
            price = float(order.get("price", 0))
            size = float(order.get("size", 0))
            owner = order.get("owner", "")
            total_usd = price * size

            if total_usd < MIN_RESTING_USD:
                continue

            is_whale = 1 if owner in whale_addresses else 0
            large_orders += 1

            # Check cooldown
            existing = meta.execute(
                "SELECT id FROM whale_intent WHERE wallet=? AND market=? AND side='buy' AND ts > ?",
                (owner, market_slug, now - ALERT_COOLDOWN_S)
            ).fetchone()
            if existing:
                continue

            meta.execute(
                "INSERT INTO whale_intent (wallet, market, token_id, side, price, size_usd, total_usd, is_whale, ts, updated)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (owner, market_slug, token_id, "buy", price, size, total_usd, is_whale, now, now))
            alerts += 1

        # Check asks (sell orders)
        for order in asks:
            price = float(order.get("price", 0))
            size = float(order.get("size", 0))
            owner = order.get("owner", "")
            total_usd = price * size

            if total_usd < MIN_RESTING_USD:
                continue

            is_whale = 1 if owner in whale_addresses else 0
            large_orders += 1

            existing = meta.execute(
                "SELECT id FROM whale_intent WHERE wallet=? AND market=? AND side='sell' AND ts > ?",
                (owner, market_slug, now - ALERT_COOLDOWN_S)
            ).fetchone()
            if existing:
                continue

            meta.execute(
                "INSERT INTO whale_intent (wallet, market, token_id, side, price, size_usd, total_usd, is_whale, ts, updated)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (owner, market_slug, token_id, "sell", price, size, total_usd, is_whale, now, now))
            alerts += 1

    meta.commit()
    return {
        "scanned": scanned,
        "alerts": alerts,
        "large_orders": large_orders,
        "markets_available": len(markets),
    }


def summary(meta: sqlite3.Connection) -> str:
    """Print recent whale intent alerts."""
    lines = ["CLOB whale intent alerts (last 20):"]
    rows = meta.execute(
        "SELECT * FROM whale_intent ORDER BY ts DESC LIMIT 20"
    ).fetchall()
    if not rows:
        lines.append("  No alerts yet. Run --scan first.")
    for r in rows:
        wallet_label = r["wallet"][:12]
        whale_tag = " 🐋" if r["is_whale"] else ""
        lines.append(
            f"  {wallet_label}{whale_tag} | {str(r['market'])[:30]:30s} | "
            f"{r['side']:4s} | {r['price']:.4f} | ${r['total_usd']:>8,.0f} | "
            f"{time.strftime('%H:%M:%S', time.localtime(r['ts']))}"
        )

    # Stats
    total = meta.execute("SELECT COUNT(*) FROM whale_intent").fetchone()[0]
    whale_only = meta.execute("SELECT COUNT(*) FROM whale_intent WHERE is_whale=1").fetchone()[0]
    lines.append(f"\nTotal alerts: {total} ({whale_only} from known whales)")
    return "\n".join(lines)


def run_scan() -> dict:
    """Scheduler entry point. Writes /tmp/whale_clob_last.json when known-whale alerts fire."""
    import json as _json
    meta = get_meta_db()
    try:
        result = scan_orderbooks(meta)
        # Export most recent known-whale alert markets for CLOB×scanner fusion
        if result["alerts"] > 0:
            rows = meta.execute(
                "SELECT market, side, price, size_usd, ts FROM whale_intent"
                " WHERE is_whale=1 ORDER BY ts DESC LIMIT 10"
            ).fetchall()
            if rows:
                fired = [
                    {"market": r[0], "side": r[1], "price": r[2], "size_usd": r[3], "ts": r[4]}
                    for r in rows
                ]
                try:
                    with open("/tmp/whale_clob_last.json", "w") as f:
                        _json.dump({"ts": fired[0]["ts"], "fired": fired}, f)
                except Exception:
                    pass
        return result
    finally:
        meta.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--scan", action="store_true")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    meta = get_meta_db()
    if args.scan:
        result = scan_orderbooks(meta)
        print(f"CLOB scan: {result['scanned']} markets, {result['alerts']} new alerts, {result['large_orders']} large orders")
    if args.summary:
        print(summary(meta))
    meta.close()

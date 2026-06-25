"""
Whale hot rescan — refreshes prices for recent CRITICAL/HIGH markets every 60s.
Called from scheduler.py tick_clob loop.

Reads: whale_scanner.db (whale_alerts with CRITICAL/HIGH in last 2h)
Writes: Updates payload.best_bid/best_ask in whale_alerts
Side effect: If live price is >0.90 or <0.10, adds "_resolved_live" flag

This is a lightweight pass — ~1 API call per hot market, <200ms each.
"""
import sqlite3
import json
import time
import logging
import requests

logger = logging.getLogger("whale_hot_rescan")

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
HOT_WINDOW_S = 7200  # 2 hours
MAX_HOT_MARKETS = 15

def _fetch_kalshi_book(ticker: str):
    try:
        r = requests.get(f"{KALSHI_API}/markets/{ticker}/orderbook", timeout=5)
        if r.ok:
            d = r.json()
            fp = d.get("orderbook_fp", {})
            yes_bids = fp.get("yes", [])
            if yes_bids:
                # First element is best bid [price, quantity]
                return yes_bids[0][0] / 100.0 if yes_bids[0][0] > 1 else yes_bids[0][0]
    except Exception:
        pass
    return None

def _fetch_pm_book(slug: str):
    try:
        g = requests.get(f"https://gamma-api.polymarket.com/markets?slug={slug}&limit=1", timeout=5)
        if not g.ok:
            return None
        markets = g.json()
        if not markets:
            return None
        token = json.loads(markets[0].get("clobTokenIds", "[]"))[0]
        r = requests.get(f"https://clob.polymarket.com/book?token_id={token}", timeout=5)
        if r.ok:
            d = r.json()
            bids = d.get("bids", [])
            if bids:
                return float(bids[0]["price"])
    except Exception:
        pass
    return None

def run_hot_rescan():
    """Refresh prices for recent CRITICAL/HIGH markets. Returns stats dict."""
    from pathlib import Path
    db_path = Path(__file__).resolve().parent.parent / "storage" / "whale_scanner.db"
    if not db_path.exists():
        return {"hot": 0, "refreshed": 0, "resolved": 0}

    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    now = time.time()
    cutoff = now - HOT_WINDOW_S

    rows = conn.execute(
        "SELECT id, market, platform, payload FROM whale_alerts "
        "WHERE severity IN ('CRITICAL', 'HIGH') AND ts > ? "
        "ORDER BY ts DESC LIMIT ?",
        (cutoff, MAX_HOT_MARKETS)
    ).fetchall()

    # Deduplicate by market
    seen = set()
    hot = []
    for r in rows:
        if r["market"] not in seen:
            seen.add(r["market"])
            hot.append(r)

    stats = {"hot": len(hot), "refreshed": 0, "resolved": 0}

    for r in hot:
        platform = r["platform"]
        market = r["market"]

        # Fetch live price
        if platform == "kalshi":
            live_bid = _fetch_kalshi_book(market)
        elif platform == "polymarket":
            live_bid = _fetch_pm_book(market)
        else:
            continue

        if live_bid is None:
            continue

        stats["refreshed"] += 1

        # Update payload with live price
        try:
            payload = json.loads(r["payload"] or "{}")
        except json.JSONDecodeError:
            payload = {}

        old_bid = payload.get("best_bid")
        payload["best_bid"] = live_bid
        payload["_live_refresh_ts"] = now

        # Mark as resolved if price is decided
        if live_bid > 0.90 or live_bid < 0.10:
            payload["_resolved_live"] = True
            stats["resolved"] += 1
            if old_bid is not None:
                logger.info(
                    "Hot rescan: %s moved %s→%s (RESOLVED)",
                    market[:40], f"{old_bid:.2f}" if old_bid else "?", f"{live_bid:.2f}"
                )

        try:
            conn.execute(
                "UPDATE whale_alerts SET payload = ? WHERE id = ?",
                (json.dumps(payload), r["id"])
            )
        except sqlite3.OperationalError:
            pass  # skip if DB locked, retry next cycle

    try:
        conn.commit()
    except sqlite3.OperationalError:
        pass
    conn.close()

    if stats["refreshed"]:
        logger.info("Hot rescan: %d hot, %d refreshed, %d resolved", stats["hot"], stats["refreshed"], stats["resolved"])

    return stats

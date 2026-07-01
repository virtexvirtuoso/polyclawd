#!/usr/bin/env python3
"""
Cross-platform arb scanner — logs arb opportunities for performance tracking.
No Telegram alerts. Data stored in shadow_trades.db for later analysis.

Called by scheduler every 30min.
"""

import os, sys, json, sqlite3, time
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signals.cross_platform_arb import scan_cross_platform_arb

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "storage", "shadow_trades.db")
MIN_NET_EDGE = 2.0  # pp
MAX_LOG = 10


def _init_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arb_opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            kalshi_title TEXT,
            kalshi_id TEXT,
            kalshi_price REAL,
            kalshi_volume REAL,
            poly_title TEXT,
            poly_id TEXT,
            poly_price REAL,
            poly_volume REAL,
            poly_slug TEXT,
            spread_pp REAL,
            net_edge_pp REAL,
            similarity REAL,
            direction TEXT,
            resolved INTEGER DEFAULT 0,
            resolution_price REAL,
            resolved_at REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_arb_ts ON arb_opportunities(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_arb_resolved ON arb_opportunities(resolved)")


def log_arb_opportunities():
    """Scan for arb opportunities and log them to DB. No Telegram alerts."""
    result = scan_cross_platform_arb()
    arbs = result.get("arbs", [])

    if not arbs:
        print("No arb opportunities found")
        return

    qualified = [a for a in arbs if a["net_edge_pp"] >= MIN_NET_EDGE]
    if not qualified:
        print(f"No arbs above {MIN_NET_EDGE}pp threshold")
        return

    now = time.time()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    _init_table(conn)

    logged = 0
    for arb in qualified[:MAX_LOG]:
        conn.execute(
            "INSERT INTO arb_opportunities "
            "(ts, kalshi_title, kalshi_id, kalshi_price, kalshi_volume, "
            " poly_title, poly_id, poly_price, poly_volume, poly_slug, "
            " spread_pp, net_edge_pp, similarity, direction) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now,
             arb.get("kalshi_title", "")[:100],
             arb.get("kalshi_id", ""),
             arb.get("kalshi_price"),
             arb.get("kalshi_volume"),
             arb.get("poly_title", "")[:100],
             arb.get("poly_id", ""),
             arb.get("poly_price"),
             arb.get("poly_volume"),
             arb.get("poly_slug", ""),
             arb.get("spread_pp"),
             arb.get("net_edge_pp"),
             arb.get("similarity"),
             arb.get("direction", "")[:200])
        )
        logged += 1

    conn.commit()
    conn.close()
    print(f"Logged {logged} arb opportunities (net edge >= {MIN_NET_EDGE}pp)")


if __name__ == "__main__":
    log_arb_opportunities()

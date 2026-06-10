#!/usr/bin/env python3
"""
Baseball Odds Snapshot — 15-min book vs Polymarket price capture.
Creates time-series data for lag analysis: does Poly converge to books as game approaches?

Table: baseball_odds_snapshots
  id, game_id, game_title, team, market_type, point_value,
  book_prob, poly_price, edge_pct, mins_to_start, captured_at

Run via cron every 15 min during MLB season.
"""

import asyncio
import sqlite3
import sys
import os
from datetime import datetime, timezone

BASE_DIR = '/var/www/virtuosocrypto.com/polyclawd'
DB_PATH = os.path.join(BASE_DIR, 'storage', 'shadow_trades.db')
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS baseball_odds_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id TEXT,
            game_title TEXT,
            team TEXT,
            market_type TEXT,
            point_value REAL,
            book_prob REAL,
            poly_price REAL,
            edge_pct REAL,
            mins_to_start INTEGER,
            captured_at TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_odds_snap_game
        ON baseball_odds_snapshots(game_id, team, market_type)
    """)
    conn.commit()


async def capture_snapshot():
    from odds.baseball_edge import find_baseball_edges
    edges = await find_baseball_edges(min_edge=0.0)

    conn = sqlite3.connect(DB_PATH)
    ensure_table(conn)

    now = datetime.now(timezone.utc)
    inserted = 0

    for e in edges:
        try:
            ct = datetime.fromisoformat(e.commence_time.replace('Z', '+00:00'))
            mins = max(0, int((ct - now).total_seconds() / 60))
        except:
            mins = -1

        conn.execute("""
            INSERT INTO baseball_odds_snapshots
            (game_id, game_title, team, market_type, point_value,
             book_prob, poly_price, edge_pct, mins_to_start, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            e.poly_market_id or e.game_title,
            e.game_title,
            e.bet_team,
            e.market_type,
            e.point_value,
            round(e.odds_api_prob, 4),
            round(e.polymarket_price, 4),
            round(e.edge_pct, 4),
            mins,
            now.isoformat(),
        ))
        inserted += 1

    conn.commit()
    conn.close()
    print(f"baseball_odds_snapshot: {inserted} rows at {now.isoformat()}")


if __name__ == '__main__':
    asyncio.run(capture_snapshot())
#!/usr/bin/env python3
"""
ufc_event_discovery.py — UFC event discovery service.

Polls the Odds API events list (FREE — 0 credits) every 6 hours to discover
upcoming fight cards. Stores results in the ufc_events SQLite table.

The active scanner (ufc_edge_cron.py) reads from this table to know when to
run paid scans.

Usage:
  python3 odds/ufc_event_discovery.py              # discover + store
  python3 odds/ufc_event_discovery.py --dry         # print only, no store
"""

import os, sys, json, requests, sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_DIR, "storage", "shadow_trades.db")
ODDS_API = "https://api.the-odds-api.com/v4"

def _load_env():
    env_path = os.path.join(PROJECT_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()

_load_env()
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
DRY_RUN = "--dry" in sys.argv


def _get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ufc_events (
            event_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            commence_time TEXT NOT NULL,
            fighters TEXT NOT NULL,
            status TEXT DEFAULT 'upcoming',
            last_scanned TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    return conn


def discover_events() -> list[dict]:
    """Fetch upcoming UFC events from Odds API (FREE — 0 credits)."""
    r = requests.get(
        f"{ODDS_API}/sports/mma_mixed_martial_arts/events",
        params={"apiKey": ODDS_API_KEY},
        timeout=15,
    )
    if r.status_code != 200:
        print(f"  ⚠ Odds API error: {r.status_code} {r.text[:200]}")
        return []

    events = r.json()
    now = datetime.now(timezone.utc)
    discovered = []

    for e in events:
        eid = e.get("id", "")
        home = e.get("home_team", "")
        away = e.get("away_team", "")
        ct = e.get("commence_time", "")

        if not eid or not home or not away or not ct:
            continue

        try:
            dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        except:
            continue

        # Skip past events
        if dt < now - timedelta(hours=2):
            continue

        # Determine status
        if dt <= now + timedelta(hours=6):
            status = "active"
        elif dt <= now:
            status = "completed"
        else:
            status = "upcoming"

        discovered.append({
            "event_id": eid,
            "title": f"{home} vs {away}",
            "commence_time": ct,
            "fighters": json.dumps([home, away]),
            "status": status,
            "last_scanned": now.isoformat(),
        })

    return discovered


def store_events(conn: sqlite3.Connection, events: list[dict]):
    """Insert or update events in the database."""
    for e in events:
        conn.execute("""
            INSERT INTO ufc_events (event_id, title, commence_time, fighters, status, last_scanned)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                title=excluded.title,
                commence_time=excluded.commence_time,
                fighters=excluded.fighters,
                status=excluded.status,
                last_scanned=excluded.last_scanned
        """, (
            e["event_id"], e["title"], e["commence_time"],
            e["fighters"], e["status"], e["last_scanned"],
        ))
    conn.commit()


def display(events: list[dict]):
    """Print discovered events."""
    now = datetime.now(timezone.utc)
    print(f"\n{'═' * 60}")
    print(f"  UFC EVENT DISCOVERY — {now.strftime('%H:%M UTC')}")
    print(f"{'═' * 60}")

    if not events:
        print("\n  No upcoming events found.")
        return

    # Group by status
    active = [e for e in events if e["status"] == "active"]
    upcoming = [e for e in events if e["status"] == "upcoming"]

    if active:
        print(f"\n  🔴 ACTIVE (within 6h):")
        for e in active:
            dt = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
            print(f"     {e['title']:45s} | {dt.strftime('%b %d %H:%M UTC')}")

    if upcoming:
        # Group by card date
        cards = {}
        for e in upcoming:
            dt = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
            date_key = dt.strftime("%b %d")
            if date_key not in cards:
                cards[date_key] = []
            cards[date_key].append(e)

        print(f"\n  📅 UPCOMING CARDS:")
        for date_key in sorted(cards.keys()):
            print(f"     {date_key}:")
            for e in cards[date_key]:
                dt = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
                print(f"       {e['title']:45s} | {dt.strftime('%H:%M UTC')}")

    print(f"\n{'─' * 60}")
    print(f"  Total: {len(events)} events ({len(active)} active, {len(upcoming)} upcoming)")
    print(f"{'═' * 60}\n")


def main():
    print("\n  Discovering UFC events...")
    events = discover_events()

    if not events:
        print("  No events found.")
        return

    display(events)

    if not DRY_RUN:
        conn = _get_db()
        store_events(conn, events)
        conn.close()
        print(f"  ✅ Stored {len(events)} events in database.")
    else:
        print(f"  (dry run — not stored)")


if __name__ == "__main__":
    main()

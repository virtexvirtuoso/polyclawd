#!/usr/bin/env python3
"""
ufc_edge_cron.py — UFC edge scan cron entry point.

Called by services/scheduler.py on a 15-minute tick during active fight windows.
Self-gating: skips entirely if no events within KICKOFF_WINDOW_HOURS (0 credits).

Flow:
  1. Read ufc_events table for events within KICKOFF_WINDOW_HOURS
  2. If none: skip (0 credits)
  3. If events found: run ufc_edge_scanner, log results, alert if actionable

Usage (scheduler):
  from signals.ufc_edge_cron import run_ufc_edge_scan
  await run_ufc_edge_scan()

Usage (standalone):
  python3 signals/ufc_edge_cron.py
  python3 signals/ufc_edge_cron.py --dry
"""

import os, sys, json, requests, sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

DB_PATH = os.path.join(PROJECT_DIR, "storage", "shadow_trades.db")
KICKOFF_WINDOW_HOURS = 6
MIN_EDGE_PP = 4.0
COOLDOWN_MINUTES = 30
MAX_ALERTS_PER_SCAN = 3

DRY_RUN = "--dry" in sys.argv

# ── Telegram Alerting ──────────────────────────────────────────────

def _send_telegram(message: str) -> bool:
    """Send a Telegram alert via OpenClaw gateway or direct Bot API."""
    import subprocess, urllib.request, urllib.parse

    # Try OpenClaw CLI first
    try:
        target = "468298295"
        cmd = ["openclaw", "message", "send", "--channel", "telegram",
               "--target", target, "--message", message]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: direct Bot API
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return False
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "468298295")
    fields = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    payload = urllib.parse.urlencode(fields).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=payload)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode()).get("ok", False)
    except Exception:
        return False


def _send_ufc_alert(edge) -> bool:
    """Format and send a Telegram alert for an actionable UFC edge."""
    if edge.edge_type == "pm_vs_pinnacle":
        parts = []
        if edge.polymarket:
            parts.append(f"PM {edge.polymarket.price:.1%}")
        if edge.kalshi:
            parts.append(f"KA {edge.kalshi.price:.1%}")
        if edge.pinnacle:
            parts.append(f"PIN {edge.pinnacle.price:.1%}")
        prices = " | ".join(parts)
        msg = (
            f"⚡ UFC EDGE — {edge.fight}\n"
            f"{edge.fighter}\n"
            f"{prices}\n"
            f"Edge: {edge.edge_pp:+.1f}pp → {edge.direction}"
        )
        if edge.movement and edge.movement.delta_15m is not None:
            msg += f"\nMovement: {edge.movement.delta_15m:+.1f}pp (15m)"

    elif edge.edge_type == "pm_vs_kalshi":
        msg = (
            f"⚡ PM-KALSHI ARB — {edge.fight}\n"
            f"{edge.fighter}\n"
            f"PM: {edge.polymarket.price:.1%} | KA: {edge.kalshi.price:.1%}\n"
            f"Gap: {edge.edge_pp:.1f}pp → {edge.direction}"
        )

    elif edge.edge_type == "soft_vs_pinnacle":
        msg = (
            f"⚡ STALE LINE — {edge.fight}\n"
            f"{edge.fighter}\n"
            f"{edge.soft_book.platform}: {edge.soft_book.price:.1%} | PIN: {edge.pinnacle.price:.1%}\n"
            f"Overpriced by {edge.edge_pp:.1f}pp → {edge.direction}"
        )
    else:
        return False

    return _send_telegram(msg)


# ── Resolution Tracking ─────────────────────────────────────────────

def _init_resolution_table(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ufc_resolutions (
            event_id TEXT NOT NULL,
            fighter TEXT NOT NULL,
            platform TEXT NOT NULL,
            entry_price REAL NOT NULL,
            exit_price REAL,
            brier_score REAL,
            pnl_pct REAL,
            resolved_at TEXT,
            PRIMARY KEY (event_id, fighter, platform)
        );
    """)


def _resolve_completed_fights() -> int:
    """Check for completed fights and record Brier scores.
    Uses Polymarket Gamma API to check if a fight market has resolved."""
    conn = _get_db()
    _init_resolution_table(conn)

    now = datetime.now(timezone.utc)
    resolved_count = 0

    # Get events that are past their commence time + 2h buffer
    cutoff = (now - timedelta(hours=2)).isoformat()
    past_events = conn.execute("""
        SELECT event_id, title, commence_time
        FROM ufc_events
        WHERE commence_time < ? AND status != 'completed'
    """, (cutoff,)).fetchall()

    for eid, title, ct in past_events:
        # Check Polymarket Gamma for resolution
        r = requests.get(f"{GAMMA_API}/events?tag_slug=ufc&closed=false&limit=50", timeout=10)
        if r.status_code != 200:
            continue

        # Find this event in Gamma
        for ev in r.json():
            if ev.get("title", "") != title:
                continue
            for m in ev.get("markets", []):
                if m.get("question") != title:
                    continue
                winner = m.get("winner")
                if winner is None:
                    continue  # Not resolved yet

                # Market resolved — record outcomes
                prices = m.get("outcomePrices", "[]")
                try:
                    p = json.loads(prices)
                except:
                    continue

                # Get snapshots for this event
                snapshots = conn.execute("""
                    SELECT fighter, platform, price
                    FROM ufc_price_snapshots
                    WHERE event_id = ?
                    GROUP BY fighter, platform
                    HAVING scan_time = MAX(scan_time)
                """, (eid,)).fetchall()

                for fighter, platform, entry_price in snapshots:
                    # Determine if this fighter won
                    fighters_in_title = [f for f in FIGHTER_ALIASES if f.lower() in title.lower()]
                    fighters_in_title.sort(key=lambda f: title.lower().index(f.lower()))

                    result = 0.0
                    if len(fighters_in_title) >= 2:
                        winner_idx = int(winner)
                        if winner_idx < len(fighters_in_title):
                            winner_name = fighters_in_title[winner_idx]
                            if fighter == winner_name:
                                result = 1.0

                    exit_price = result  # 1.0 if won, 0.0 if lost
                    brier = (entry_price - result) ** 2
                    pnl_pct = (exit_price - entry_price) / entry_price if entry_price > 0 else 0

                    conn.execute("""
                        INSERT OR REPLACE INTO ufc_resolutions
                        (event_id, fighter, platform, entry_price, exit_price, brier_score, pnl_pct, resolved_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (eid, fighter, platform, entry_price, exit_price, brier, pnl_pct, now.isoformat()))
                    resolved_count += 1

                # Mark event as completed
                conn.execute("UPDATE ufc_events SET status='completed' WHERE event_id=?", (eid,))
                break
            break

    conn.commit()
    conn.close()
    return resolved_count


def _get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_active_events(conn: sqlite3.Connection) -> list[dict]:
    """Get events within KICKOFF_WINDOW_HOURS of kickoff."""
    now = datetime.now(timezone.utc)
    cutoff = (now + timedelta(hours=KICKOFF_WINDOW_HOURS)).isoformat()

    rows = conn.execute("""
        SELECT event_id, title, commence_time, fighters, status
        FROM ufc_events
        WHERE commence_time <= ? AND status != 'completed'
        ORDER BY commence_time ASC
    """, (cutoff,)).fetchall()

    events = []
    for row in rows:
        eid, title, ct, fighters_json, status = row
        try:
            dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        except:
            continue

        # Skip past events
        if dt < now - timedelta(hours=2):
            conn.execute("UPDATE ufc_events SET status='completed' WHERE event_id=?", (eid,))
            conn.commit()
            continue

        events.append({
            "event_id": eid,
            "title": title,
            "commence_time": ct,
            "fighters": json.loads(fighters_json) if fighters_json else [],
            "status": status,
        })

    return events


def run_ufc_edge_scan() -> dict:
    """
    Main entry point for scheduler.
    Returns summary dict: {scanned, edges_found, alerts_sent, credits_used}
    """
    conn = _get_db()
    events = get_active_events(conn)
    conn.close()

    if not events:
        return {"scanned": False, "edges_found": 0, "alerts_sent": 0, "credits_used": 0}

    # Import scanner (lazy — only when needed)
    from odds.ufc_edge_scanner import (
        get_polymarket_fights, get_kalshi_fights,
        get_pinnacle_odds, get_all_book_odds,
        compute_edges, display, save_price_snapshot,
        TONIGHT_FIGHTS, _get_db as scanner_db,
    )

    # Run scan
    poly = get_polymarket_fights()
    kalshi = get_kalshi_fights()

    sconn = scanner_db()
    edges = compute_edges(poly, kalshi, sconn)
    sconn.close()

    # Display
    display(edges)

    # Alert if actionable
    alerts_sent = 0
    actionable = [e for e in edges if e.tradeable]
    for e in actionable[:MAX_ALERTS_PER_SCAN]:
        _send_ufc_alert(e)
        alerts_sent += 1

    # Resolve completed fights
    resolutions = _resolve_completed_fights()

    return {
        "scanned": True,
        "edges_found": len(edges),
        "alerts_sent": alerts_sent,
        "resolved": resolutions,
        "credits_used": len(TONIGHT_FIGHTS) + 1,
    }


def main():
    print(f"\n  UFC Edge Cron — {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
    print(f"  Window: {KICKOFF_WINDOW_HOURS}h before kickoff")
    print(f"  Min edge: {MIN_EDGE_PP}pp")
    print()

    conn = _get_db()
    events = get_active_events(conn)
    conn.close()

    if not events:
        print("  No active events in window. Skipping (0 credits).")
        return

    print(f"  Active events: {len(events)}")
    for e in events:
        dt = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
        print(f"    {e['title']:45s} | {dt.strftime('%H:%M UTC')}")

    print()
    result = run_ufc_edge_scan()
    print(f"\n  Result: scanned={result['scanned']}, edges={result['edges_found']}, "
          f"alerts={result['alerts_sent']}, credits={result['credits_used']}")


if __name__ == "__main__":
    main()

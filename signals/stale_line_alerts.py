#!/usr/bin/env python3
"""
stale_line_alerts.py — Generic stale line detection pipeline.

Detects when soft sportsbooks (DraftKings, FanDuel, BetMGM, Caesars, BetRivers)
have prices that diverge significantly from Pinnacle (the sharp reference).

Sport-agnostic — any sport scanner can call it.

Usage:
  python3 signals/stale_line_alerts.py              # scan all active sports
  python3 signals/stale_line_alerts.py --sport ufc   # UFC only
  python3 signals/stale_line_alerts.py --dry         # no alerts
"""

import os, sys, json, time, requests, sqlite3
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import Optional

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from db import connect as db_connect  # noqa: E402

ODDS_API = "https://api.the-odds-api.com/v4"
DB_PATH = os.path.join(PROJECT_DIR, "storage", "shadow_trades.db")


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

SOFT_BOOKS = ["draftkings", "fanduel", "betmgm", "caesars", "betrivers"]
MIN_EDGE_PP = 4.0
COOLDOWN_MINUTES = 120  # must exceed the 30-min tick cadence or it never gates (15 was a no-op: persistent lines re-alerted every scan)
EDGE_CHANGE_PP = 3.0  # re-alert inside cooldown if edge moves >= this
MAX_ALERTS_PER_SCAN = 5
NEAR_WINDOW_HOURS = 12  # only spend credits on events kicking off within this window

ACTIVE_SPORTS = [
    "mma_mixed_martial_arts",
    "soccer_fifa_world_cup",
    "baseball_mlb",
]


@dataclass
class StaleLine:
    sport: str
    event_title: str
    event_id: str
    commence_time: str
    fighter: str
    soft_book: str
    soft_price: float
    pinnacle_price: float
    edge_pp: float
    direction: str
    tradeable: bool = False


def _send_alert(message: str) -> bool:
    """Deliver via the fleet-canonical helper (CLI-first, HTTP fallback, token
    from the service EnvironmentFile — never hardcoded). parse_mode=None sends
    plain text, dodging the Markdown-400 trap on stray % / _ in book names."""
    from scripts.openclaw_alerts import alert_openclaw

    return alert_openclaw(message, channel="telegram", parse_mode=None)


def _cooldown_conn() -> sqlite3.Connection:
    conn = db_connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stale_line_alert_log (
            event_id   TEXT NOT NULL,
            soft_book  TEXT NOT NULL,
            outcome    TEXT NOT NULL,
            last_ts    REAL NOT NULL,
            last_edge  REAL NOT NULL,
            PRIMARY KEY (event_id, soft_book, outcome)
        )
        """
    )
    conn.commit()
    return conn


def _should_alert(conn: sqlite3.Connection, line: "StaleLine", now_ts: float) -> bool:
    """Fire if first sighting, cooldown elapsed, or edge moved >= EDGE_CHANGE_PP."""
    row = conn.execute(
        "SELECT last_ts, last_edge FROM stale_line_alert_log WHERE event_id=? AND soft_book=? AND outcome=?",
        (line.event_id, line.soft_book, line.fighter),
    ).fetchone()
    if row is None:
        return True
    last_ts, last_edge = row
    if (now_ts - last_ts) >= COOLDOWN_MINUTES * 60:
        return True
    if abs(line.edge_pp - last_edge) >= EDGE_CHANGE_PP:
        return True
    return False


def _record_alert(conn: sqlite3.Connection, line: "StaleLine", now_ts: float) -> None:
    conn.execute(
        """
        INSERT INTO stale_line_alert_log (event_id, soft_book, outcome, last_ts, last_edge)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(event_id, soft_book, outcome)
        DO UPDATE SET last_ts=excluded.last_ts, last_edge=excluded.last_edge
        """,
        (line.event_id, line.soft_book, line.fighter, now_ts, line.edge_pp),
    )
    conn.commit()


def scan_sport(sport_key: str) -> list[StaleLine]:
    lines = []
    r = requests.get(
        f"{ODDS_API}/sports/{sport_key}/events",
        params={"apiKey": ODDS_API_KEY},
        timeout=10,
    )
    if r.status_code != 200:
        print(f"  WARN {sport_key}: events error {r.status_code}")
        return lines

    events = r.json()
    now = datetime.now(timezone.utc)

    for e in events:
        eid = e.get("id", "")
        ct = e.get("commence_time", "")
        home = e.get("home_team", "")
        away = e.get("away_team", "")
        if not eid or not home or not away:
            continue
        try:
            dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        except:
            continue
        if dt < now - timedelta(hours=1):
            continue
        # Forward self-gate: don't spend a credit on events still days out.
        # Stale soft-book lines matter near kickoff; this keeps idle scans free.
        if dt > now + timedelta(hours=NEAR_WINDOW_HOURS):
            continue

        r2 = requests.get(
            f"{ODDS_API}/sports/{sport_key}/events/{eid}/odds",
            params={"apiKey": ODDS_API_KEY, "regions": "us", "markets": "h2h", "oddsFormat": "decimal"},
            timeout=10,
        )
        if r2.status_code != 200:
            continue

        data = r2.json()
        title = data.get("title", f"{home} vs {away}")

        pinnacle = None
        for bm in data.get("bookmakers", []):
            if bm.get("key") == "pinnacle":
                for m in bm.get("markets", []):
                    outcomes = m.get("outcomes", [])
                    if len(outcomes) >= 2:
                        o1, o2 = outcomes[0], outcomes[1]
                        imp1, imp2 = 1 / o1["price"], 1 / o2["price"]
                        total = imp1 + imp2
                        pinnacle = {
                            o1["name"]: imp1 / total,
                            o2["name"]: imp2 / total,
                        }
                break

        if not pinnacle:
            continue

        for bm in data.get("bookmakers", []):
            bk = bm.get("key", "")
            if bk == "pinnacle" or bk not in SOFT_BOOKS:
                continue
            for m in bm.get("markets", []):
                for o in m.get("outcomes", []):
                    name = o["name"]
                    if name not in pinnacle:
                        continue
                    soft_imp = 1 / o["price"]
                    edge_pp = (pinnacle[name] - soft_imp) * 100
                    direction = "BUY" if edge_pp > 0 else "SELL"
                    if abs(edge_pp) >= MIN_EDGE_PP:
                        lines.append(
                            StaleLine(
                                sport=sport_key,
                                event_title=title,
                                event_id=eid,
                                commence_time=ct,
                                fighter=name,
                                soft_book=bk,
                                soft_price=soft_imp,
                                pinnacle_price=pinnacle[name],
                                edge_pp=round(edge_pp, 1),
                                direction=direction,
                                tradeable=True,
                            )
                        )
    return lines


def display(lines: list[StaleLine]):
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    print(f"\n{'=' * 70}")
    print(f"  STALE LINE SCAN — {now}")
    print(f"{'=' * 70}")
    if not lines:
        print("\n  No stale lines found.")
        return
    for l in lines:
        icon = "BOLT" if l.tradeable else "  "
        print(f"\n  {icon} [{l.sport}] {l.event_title[:40]:40s}")
        print(f"     {l.fighter:25s} | {l.soft_book:15s}: {l.soft_price:.1%} vs PIN: {l.pinnacle_price:.1%}")
        print(f"     Edge: {l.edge_pp:+.1f}pp -> {l.direction}")
    actionable = [l for l in lines if l.tradeable]
    print(f"\n{'-' * 70}")
    print(f"  Total: {len(lines)} stale lines  |  Actionable (>={MIN_EDGE_PP}pp): {len(actionable)}")
    print(f"{'=' * 70}\n")


def run_stale_line_scan(sport_key=None) -> dict:
    sports = [sport_key] if sport_key else ACTIVE_SPORTS
    all_lines = []
    for sk in sports:
        all_lines.extend(scan_sport(sk))

    # Strongest edges first so the per-scan cap keeps the most actionable alerts.
    candidates = sorted(
        [l for l in all_lines if l.tradeable],
        key=lambda l: abs(l.edge_pp),
        reverse=True,
    )

    alerts_sent = 0
    suppressed = 0
    if not DRY_RUN:
        now_ts = time.time()
        conn = _cooldown_conn()
        try:
            for l in candidates:
                if alerts_sent >= MAX_ALERTS_PER_SCAN:
                    suppressed += 1
                    continue
                if not _should_alert(conn, l, now_ts):
                    suppressed += 1
                    continue
                msg = (
                    f"BOLT STALE LINE — {l.event_title}\n"
                    f"{l.fighter}\n"
                    f"{l.soft_book}: {l.soft_price:.1%} vs Pinnacle: {l.pinnacle_price:.1%}\n"
                    f"Edge: {l.edge_pp:+.1f}pp -> {l.direction}"
                )
                if _send_alert(msg):
                    _record_alert(conn, l, now_ts)
                    alerts_sent += 1
        finally:
            conn.close()

    display(all_lines)
    return {
        "scanned_sports": len(sports),
        "lines_found": len(all_lines),
        "alerts_sent": alerts_sent,
        "suppressed": suppressed,
    }


def main():
    print(f"\n  Stale Line Scanner — {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
    print(f"  Sports: {', '.join(ACTIVE_SPORTS)}")
    print(f"  Min edge: {MIN_EDGE_PP}pp\n")
    result = run_stale_line_scan()
    print(f"  Result: {result['scanned_sports']} sports, {result['lines_found']} lines, {result['alerts_sent']} alerts")


if __name__ == "__main__":
    main()

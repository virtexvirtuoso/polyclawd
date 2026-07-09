#!/usr/bin/env python3
"""
ufc_live_monitor.py — Live-fight alert monitor for UFC events.

Fires 3 alert types every 5 min during active fight cards:
  1. ROUND TRIGGER   — ESPN round/status change → fresh Pinnacle odds + PM CLOB comparison
  2. LINE DRIFT      — Pinnacle ML moves > LINE_DRIFT_PP since last snapshot
  3. EDGE INVERSION  — open shadow trade → close + alert if edge gone

ESPN MMA endpoint gives per-fight status: "R1, 2:09", "R2, 5:00", "Final", etc.
UFC has no live "score" — we trigger on round transitions + fight conclusions.

Cron: */5 * * * * (scheduler tick_5min, self-gating to card hours)
State: storage/shadow_trades.db (auto-migrated tables)
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
import time
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))
from odds.monitor_gate import gated_fetch_json, LIVE_BOOKS

from scripts.alert_formatter import send_telegram

# ── Config ────────────────────────────────────────────────────────────────────
ESPN_MMA       = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"
POLY_EVENTS    = "https://gamma-api.polymarket.com/events"
ODDS_API_BASE  = "https://api.the-odds-api.com/v4/sports/mma_mixed_martial_arts/odds/"
CLOB_BOOK      = "https://clob.polymarket.com/book"

ODDS_API_KEY   = os.environ.get("ODDS_API_KEY", "")
LINE_DRIFT_PP  = 10.0    # pp shift to fire drift alert (UFC swings hard between rounds)
WHALE_SIZE     = 50000   # CLOB book wall threshold
WHALE_DEDUP_S  = 1800    # suppress re-alert for same wall within 30 min
EDGE_FLOOR     = 0.02    # close shadow trade if edge drops below 2pp
PM_GAP_PP      = 8.0     # min pp gap between PM and Vegas to flag in alerts

DB_PATH  = BASE_DIR / "storage" / "shadow_trades.db"
MC_HOST, MC_PORT = "localhost", 11211

_EXECUTOR = ThreadPoolExecutor(max_workers=6)

# Fighter name aliases for matching PM ↔ ESPN
ALIASES: Dict[str, List[str]] = {
    "izzy": ["israel adesanya", "adesanya"],
    "israel adesanya": ["izzy", "adesanya"],
    "bones": ["jon jones"],
    "jon jones": ["bones"],
    "ngannou": ["francis ngannou"],
    "francis ngannou": ["ngannou"],
}


# ── DB ────────────────────────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ufc_fight_snap (
            fight_id    TEXT PRIMARY KEY,
            fighter_a   TEXT,
            fighter_b   TEXT,
            detail      TEXT,
            round       INTEGER DEFAULT 0,
            winner      TEXT,
            ts          TEXT
        );
        CREATE TABLE IF NOT EXISTS ufc_line_snap (
            fight_id    TEXT PRIMARY KEY,
            fighter_a   TEXT,
            fighter_b   TEXT,
            pin_a       REAL,
            pin_b       REAL,
            pm_a        REAL,
            pm_b        REAL,
            ts          TEXT
        );
        CREATE TABLE IF NOT EXISTS ufc_whale_seen (
            fight_id    TEXT,
            side        TEXT,
            ts          INTEGER,
            PRIMARY KEY (fight_id, side, ts)
        );
    """)
    conn.commit()


# ── HTTP ──────────────────────────────────────────────────────────────────────
def _get(url: str, params: Optional[dict] = None, timeout: int = 12) -> Optional[object]:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"[ufc_monitor] GET {url[:70]} → {e}", flush=True)
        return None


# ── Memcached ─────────────────────────────────────────────────────────────────
def _mc_get(key: str, timeout: float = 0.4) -> Optional[bytes]:
    try:
        s = socket.create_connection((MC_HOST, MC_PORT), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(b"get " + key.encode() + b"\r\n")
        buf = b""
        while b"END\r\n" not in buf and len(buf) < 500_000:
            chunk = s.recv(8192)
            if not chunk:
                break
            buf += chunk
        try:
            s.close()
        except Exception:
            pass
        if not buf.startswith(b"VALUE"):
            return None
        _, _, rest = buf.partition(b"\r\n")
        return rest.split(b"\r\nEND\r\n", 1)[0]
    except Exception:
        return None


def _mc_set(key: str, value: str, exptime: int = 600, timeout: float = 0.4) -> None:
    try:
        payload = value.encode()
        cmd = f"set {key} 0 {exptime} {len(payload)}\r\n".encode() + payload + b"\r\n"
        s = socket.create_connection((MC_HOST, MC_PORT), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(cmd)
        s.recv(64)
        s.close()
    except Exception:
        pass


def mc_register_tokens(token_ids: List[str]) -> None:
    raw = _mc_get("poly:ws:registered")
    cur: set = set()
    if raw:
        try:
            cur = set(json.loads(raw))
        except Exception:
            pass
    cur.update(token_ids)
    merged = list(cur)[:500]
    _mc_set("poly:ws:registered", json.dumps(merged), exptime=600)


# ── Name matching ─────────────────────────────────────────────────────────────
def _nmatch(a: str, b: str) -> bool:
    a, b = a.lower().strip(), b.lower().strip()
    if a == b or a in b or b in a:
        return True
    for alias in ALIASES.get(a, []):
        if alias in b or b in alias:
            return True
    return False


def _fight_id(fighter_a: str, fighter_b: str) -> str:
    """Deterministic ID: sorted lowercase names joined with vs."""
    parts = sorted([fighter_a.lower().strip(), fighter_b.lower().strip()])
    return f"{parts[0]}_vs_{parts[1]}"


# ── ESPN ──────────────────────────────────────────────────────────────────────
def fetch_espn_fights() -> List[Dict]:
    """Fetch all fights from today's UFC card via ESPN scoreboard."""
    data = _get(ESPN_MMA)
    if not data:
        return []

    fights = []
    for event in data.get("events", []):
        event_name = event.get("name", "")
        for comp in event.get("competitions", []):
            status = comp.get("status", {})
            state = status.get("type", {}).get("state", "")
            detail = status.get("type", {}).get("detail", "")
            period = status.get("period", 0)

            competitors = comp.get("competitors", [])
            if len(competitors) < 2:
                continue

            fighter_a = competitors[0].get("athlete", {}).get("displayName", "")
            fighter_b = competitors[1].get("athlete", {}).get("displayName", "")
            winner_a = competitors[0].get("winner", False)
            winner_b = competitors[1].get("winner", False)

            winner = ""
            if winner_a:
                winner = fighter_a
            elif winner_b:
                winner = fighter_b

            fights.append({
                "event_name": event_name,
                "fighter_a": fighter_a,
                "fighter_b": fighter_b,
                "status": state,       # "pre", "in", "post"
                "detail": detail,      # "R1, 2:09", "Final", "Round 2"
                "round": period,
                "winner": winner,
                "fight_id": _fight_id(fighter_a, fighter_b),
            })

    return fights


# ── Odds API (live consensus) ─────────────────────────────────────────────────
def fetch_pinnacle(fighter_a: str, fighter_b: str) -> Optional[Dict[str, float]]:
    """Fetch devigged probabilities from live-updated books consensus.

    Uses ALL books with staleness filter (>10min behind freshest = excluded).
    Prevents pre-game snapshot bug where one book freezes during in-play.
    """
    if not ODDS_API_KEY:
        return None

    data = gated_fetch_json(ODDS_API_BASE, {
        "apiKey": ODDS_API_KEY,
        "bookmakers": LIVE_BOOKS,
        "markets": "h2h",
        "oddsFormat": "decimal",
    })
    if not data or not isinstance(data, list):
        return None

    for event in data:
        home = event.get("home_team", "")
        away = event.get("away_team", "")
        if not ((_nmatch(fighter_a, home) or _nmatch(fighter_a, away)) and
                (_nmatch(fighter_b, home) or _nmatch(fighter_b, away))):
            continue

        book_probs = []
        for bm in event.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt.get("key") != "h2h":
                    continue
                valid = [o for o in mkt.get("outcomes", []) if o.get("price", 0) and o["price"] > 1.0]
                if len(valid) < 2:
                    continue
                raw = {o["name"]: 1.0 / o["price"] for o in valid}
                total = sum(raw.values())
                if total < 0.5:
                    continue
                devigged = {k: v / total for k, v in raw.items()}
                upd = mkt.get("last_update", bm.get("last_update", ""))
                book_probs.append((upd, devigged))

        if not book_probs:
            return None

        book_probs.sort(key=lambda x: x[0], reverse=True)
        from datetime import timedelta
        try:
            fresh_dt = datetime.fromisoformat(book_probs[0][0].replace("Z", "+00:00"))
            cutoff = fresh_dt - timedelta(minutes=10)
            live_books = [
                (ts, probs) for ts, probs in book_probs
                if datetime.fromisoformat(ts.replace("Z", "+00:00")) >= cutoff
            ]
        except (ValueError, TypeError):
            live_books = book_probs

        if not live_books:
            live_books = book_probs[:1]

        # Consensus average
        all_outcomes = set()
        for _, probs in live_books:
            all_outcomes.update(probs.keys())
        consensus = {}
        for outcome in all_outcomes:
            vals = [probs.get(outcome, 0) for _, probs in live_books if outcome in probs]
            consensus[outcome] = sum(vals) / len(vals) if vals else 0
        total = sum(consensus.values())
        if total < 0.1:
            return None
        consensus = {k: v / total for k, v in consensus.items()}

        # Map to fighter_a/fighter_b format
        result = {}
        for name, prob in consensus.items():
            if _nmatch(fighter_a, name):
                result["a"] = prob
            elif _nmatch(fighter_b, name):
                result["b"] = prob
        if "a" in result and "b" in result:
            return result

    return None


# ── Polymarket ────────────────────────────────────────────────────────────────
def fetch_poly_event(fighter_a: str, fighter_b: str) -> Optional[Dict]:
    """Find Polymarket event for a fight via Gamma API."""
    data = _get(POLY_EVENTS, {
        "tag_slug": "ufc",
        "active": "true",
        "limit": 100,
    })
    if not data or not isinstance(data, list):
        return None

    for ev in data:
        title = ev.get("title", "").lower()
        if _nmatch(fighter_a, title) and _nmatch(fighter_b, title):
            return ev

    # UFC has many pages — try offset pagination
    for offset in [100, 200]:
        data = _get(POLY_EVENTS, {
            "tag_slug": "ufc",
            "active": "true",
            "limit": 100,
            "offset": offset,
        })
        if not data or not isinstance(data, list):
            break
        for ev in data:
            title = ev.get("title", "").lower()
            if _nmatch(fighter_a, title) and _nmatch(fighter_b, title):
                return ev

    return None


def refresh_clob_prices(tokens: Dict) -> Dict:
    """Replace Gamma-cached prices with live CLOB mid. Adds liquid flag (bid>2%, ask<98%)."""
    updated = {}
    for label, token_data in tokens.items():
        tid = token_data[0]
        gamma_price = token_data[1]
        book = fetch_book(tid)
        if not book:
            updated[label] = (tid, gamma_price, True)
            continue
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        best_bid = float(bids[0]["price"]) if bids else 0.0
        best_ask = float(asks[0]["price"]) if asks else 1.0
        if best_bid > 0.02 and best_ask < 0.98 and best_ask > best_bid:
            clob_mid = (best_bid + best_ask) / 2
            liquid = any(
                abs(float(o["price"]) - clob_mid) <= 0.15
                for o in (bids + asks)
            )
            updated[label] = (tid, clob_mid, liquid)
        else:
            updated[label] = (tid, gamma_price, False)
    return updated


def extract_tokens(ev: Dict, fighter_a: str, fighter_b: str) -> Dict[str, Tuple[str, float]]:
    """Extract {label: (token_id, current_price)} from PM event.

    Labels: "a" (fighter_a wins) and "b" (fighter_b wins).
    """
    tokens: Dict[str, Tuple[str, float]] = {}
    for m in ev.get("markets", []):
        q = m.get("question", "").lower()
        if "win" not in q and "vs" not in q:
            continue

        prices = m.get("outcomePrices", [])
        tids = m.get("clobTokenIds", [])
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except Exception:
                continue
        if isinstance(tids, str):
            try:
                tids = json.loads(tids)
            except Exception:
                continue
        if len(prices) < 2 or len(tids) < 2:
            continue

        # For UFC fight markets: YES (index 0) = first-named fighter wins
        # But we need to match by name
        outcomes = m.get("outcomes", [])
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except Exception:
                outcomes = []

        if outcomes and len(outcomes) >= 2:
            for i, name in enumerate(outcomes):
                if i >= len(tids) or i >= len(prices):
                    continue
                try:
                    price = float(prices[i])
                except (ValueError, TypeError):
                    continue
                if _nmatch(fighter_a, name):
                    tokens["a"] = (tids[i], price)
                elif _nmatch(fighter_b, name):
                    tokens["b"] = (tids[i], price)
        else:
            # Fallback: YES=first in question, NO=second
            try:
                yes_price = float(prices[0])
                no_price = float(prices[1])
            except (ValueError, TypeError):
                continue
            # Figure out who is first-named in question
            fa_pos = q.find(fighter_a.lower().split()[-1])
            fb_pos = q.find(fighter_b.lower().split()[-1])
            if fa_pos >= 0 and fb_pos >= 0:
                if fa_pos < fb_pos:
                    tokens["a"] = (tids[0], yes_price)
                    tokens["b"] = (tids[1], no_price)
                else:
                    tokens["b"] = (tids[0], yes_price)
                    tokens["a"] = (tids[1], no_price)

        if len(tokens) >= 2:
            break

    return tokens


def fetch_book(token_id: str) -> Optional[Dict]:
    data = _get(CLOB_BOOK, {"token_id": token_id})
    return data


def find_whale_wall(book: Dict, current_mid: Optional[float] = None,
                    threshold: float = WHALE_SIZE) -> Optional[Dict]:
    """Check CLOB book for single large order (whale wall)."""
    for side_key in ("bids", "asks"):
        orders = book.get(side_key, [])
        for order in orders:
            try:
                sz = float(order.get("size", 0))
            except (ValueError, TypeError):
                continue
            if sz >= threshold:
                try:
                    px = float(order.get("price", 0))
                except (ValueError, TypeError):
                    px = 0
                return {
                    "side": "bid" if side_key == "bids" else "ask",
                    "price": px,
                    "size": sz,
                }
    return None


# ── Alert checks ─────────────────────────────────────────────────────────────

def check_round_trigger(conn: sqlite3.Connection, fight: Dict,
                        pin: Optional[Dict], ev: Optional[Dict],
                        tokens: Dict[str, Tuple[str, float]]) -> None:
    """Fire alert when round changes or fight ends (equivalent of goal trigger)."""
    fid = fight["fight_id"]
    fa, fb = fight["fighter_a"], fight["fighter_b"]
    detail = fight["detail"]
    current_round = fight["round"]
    winner = fight["winner"]
    now_iso = datetime.now(timezone.utc).isoformat()

    prev = conn.execute(
        "SELECT detail, round, winner FROM ufc_fight_snap WHERE fight_id=?", (fid,)
    ).fetchone()

    prev_round = prev["round"] if prev else 0
    prev_detail = prev["detail"] if prev else ""
    prev_winner = prev["winner"] if prev else ""

    # Determine if state changed
    round_changed = current_round != prev_round
    fight_ended = winner and not prev_winner
    detail_changed = detail != prev_detail

    # Update snapshot regardless
    conn.execute("""
        INSERT OR REPLACE INTO ufc_fight_snap
          (fight_id, fighter_a, fighter_b, detail, round, winner, ts)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (fid, fa, fb, detail, current_round, winner, now_iso))
    conn.commit()

    if not (round_changed or fight_ended):
        return  # No state change — skip alert

    # Build alert
    if fight_ended:
        event_text = f"🏆 <b>FIGHT OVER</b> — {winner} wins!"
        event_detail = detail
    else:
        event_text = f"🔔 <b>Round {current_round}</b> started"
        event_detail = detail

    # Build price comparison table
    lines = [
        f"🥊 <b>UFC ROUND TRIGGER</b>  —  {fa} vs {fb}\n",
        f"{event_text}\n",
        f"<code>{event_detail}</code>\n",
    ]

    if pin:
        lines.append(f"\n<b>Pinnacle (devigged):</b>")
        lines.append(f"  {fa}: {pin['a']:.1%}  |  {fb}: {pin['b']:.1%}")

    if tokens:
        pm_a = tokens.get("a", (None, None))[1]
        pm_b = tokens.get("b", (None, None))[1]
        if pm_a is not None and pm_b is not None:
            lines.append(f"\n<b>Polymarket:</b>")
            lines.append(f"  {fa}: {pm_a:.1%}  |  {fb}: {pm_b:.1%}")

            # Gap analysis
            if pin:
                gap_a = abs((pm_a or 0) - pin.get("a", 0)) * 100
                gap_b = abs((pm_b or 0) - pin.get("b", 0)) * 100
                max_gap = max(gap_a, gap_b)
                if max_gap >= PM_GAP_PP:
                    cheaper = fa if (pm_a or 0) < pin.get("a", 0) else fb
                    lines.append(f"\n⚡ {max_gap:.1f}pp gap — {cheaper} cheaper on PM")

    # CLOB depth
    for label, name in [("a", fa), ("b", fb)]:
        if label in tokens:
            tid = tokens[label][0]
            # Whale walls disabled — resting orders, not executed trades.
            # Actual trade flow alerts come from sport_whale_trades.py.
            pass

    msg = "\n".join(lines)
    send_telegram(msg)
    print(f"[ufc_monitor] Round trigger: {fa} vs {fb} — {event_detail}", flush=True)


def _market_settling(tokens: Dict) -> bool:
    if not tokens:
        return False
    prices = [t[1] for t in tokens.values()]
    return any(p >= 0.98 for p in prices) or all(p <= 0.02 for p in prices)


def check_line_drift(conn: sqlite3.Connection, fight: Dict,
                     pin: Optional[Dict],
                     tokens: Dict[str, Tuple[str, float]]) -> None:
    """Fire alert when Pinnacle line moves > LINE_DRIFT_PP since last snapshot."""
    fid = fight["fight_id"]
    fa, fb = fight["fighter_a"], fight["fighter_b"]
    now_iso = datetime.now(timezone.utc).isoformat()

    if not pin:
        return
    if _market_settling(tokens):
        return

    prev = conn.execute(
        "SELECT pin_a, pin_b, pm_a, pm_b FROM ufc_line_snap WHERE fight_id=?", (fid,)
    ).fetchone()

    pm_a = tokens.get("a", (None, None))[1]
    pm_b = tokens.get("b", (None, None))[1]

    # Always update the snapshot
    conn.execute("""
        INSERT OR REPLACE INTO ufc_line_snap
          (fight_id, fighter_a, fighter_b, pin_a, pin_b, pm_a, pm_b, ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (fid, fa, fb, pin["a"], pin["b"], pm_a, pm_b, now_iso))
    conn.commit()

    if not prev:
        return  # First snapshot — baseline only

    prev_a = prev["pin_a"]
    drift_a = (pin["a"] - prev_a) * 100 if prev_a else 0
    drift_b = (pin["b"] - (prev["pin_b"] or 0)) * 100 if prev["pin_b"] else 0

    if abs(drift_a) < LINE_DRIFT_PP and abs(drift_b) < LINE_DRIFT_PP:
        return

    # Determine who moved more
    if abs(drift_a) >= abs(drift_b):
        mover, drift_pp = fa, drift_a
        direction = "↑" if drift_a > 0 else "↓"
    else:
        mover, drift_pp = fb, drift_b
        direction = "↑" if drift_b > 0 else "↓"

    lines = [
        f"📉 <b>UFC LINE DRIFT</b>  —  {fa} vs {fb}\n",
        f"Pinnacle shifted <b>{direction} {abs(drift_pp):.1f}pp</b> on {mover}\n",
        f"  {fa}: {prev_a:.1%} → {pin['a']:.1%}",
        f"  {fb}: {prev['pin_b']:.1%} → {pin['b']:.1%}",
    ]

    if pm_a is not None:
        gap = abs((pm_a or 0) - pin["a"]) * 100
        if gap >= PM_GAP_PP:
            lines.append(f"\n⚡ PM still at {pm_a:.1%} — {gap:.1f}pp gap")

    msg = "\n".join(lines)
    send_telegram(msg)
    print(f"[ufc_monitor] Drift: {mover} {direction}{abs(drift_pp):.1f}pp", flush=True)


def check_edge_inversion(conn: sqlite3.Connection, fight: Dict,
                         tokens: Dict[str, Tuple[str, float]],
                         pin: Optional[Dict]) -> None:
    """Check if any open UFC shadow trade has lost its edge."""
    if not pin or not tokens:
        return

    fid = fight["fight_id"]
    fa, fb = fight["fighter_a"], fight["fighter_b"]

    # Look for open shadow trades matching this fight
    rows = conn.execute("""
        SELECT id, market_title, side, entry_price, confidence
        FROM shadow_trades
        WHERE resolved = 0
          AND category = 'ufc'
          AND (market_title LIKE ? OR market_title LIKE ?)
    """, (f"%{fa}%", f"%{fb}%")).fetchall()

    for row in rows:
        side = row["side"]
        entry = row["entry_price"] / 100.0 if row["entry_price"] else 0

        # Determine current fair value
        if _nmatch(fa, row["market_title"]):
            fair = pin.get("a", 0.5)
        elif _nmatch(fb, row["market_title"]):
            fair = pin.get("b", 0.5)
        else:
            continue

        if side == "YES":
            edge = fair - entry
        else:
            edge = (1 - fair) - (1 - entry)

        if edge < EDGE_FLOOR:
            msg = (
                f"⚠️ <b>UFC EDGE INVERSION</b>  —  {fa} vs {fb}\n\n"
                f"Shadow trade #{row['id']} lost edge:\n"
                f"  Entry: {entry:.0%} {side}  |  Fair now: {fair:.1%}\n"
                f"  Edge: {edge*100:+.1f}pp (below {EDGE_FLOOR*100:.0f}pp floor)\n"
                f"\nConsider closing."
            )
            send_telegram(msg)
            print(f"[ufc_monitor] Edge inversion: trade #{row['id']}", flush=True)


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    conn = get_db()
    migrate(conn)

    fights = fetch_espn_fights()
    active = [f for f in fights if f["status"] == "in"]

    if not active:
        print("[ufc_monitor] No active fights.", flush=True)
        conn.close()
        return

    print(f"[ufc_monitor] {len(active)} active fight(s)", flush=True)

    for fight in active:
        fa, fb = fight["fighter_a"], fight["fighter_b"]
        print(f"[ufc_monitor] → {fa} vs {fb}  ({fight['detail']})", flush=True)

        # Fetch Pinnacle + PM in parallel
        f_pin = _EXECUTOR.submit(fetch_pinnacle, fa, fb)
        f_ev  = _EXECUTOR.submit(fetch_poly_event, fa, fb)

        try:
            pin = f_pin.result(timeout=15)
        except Exception as e:
            print(f"[ufc_monitor] fetch_pinnacle failed: {e}", flush=True)
            pin = None
        try:
            ev = f_ev.result(timeout=15)
        except Exception as e:
            print(f"[ufc_monitor] fetch_poly_event failed: {e}", flush=True)
            ev = None

        tokens = extract_tokens(ev, fa, fb) if ev else {}
        sdk_source = False

        # SDK fallback: aec-ufc-{f1_abbr}-{f2_abbr}-{date} (binary, YES=f1 wins)
        if not tokens:
            try:
                from scripts.pm_sdk_utils import fetch_pm_sdk_ufc
                tokens = fetch_pm_sdk_ufc(fa, fb, pin=pin)
                sdk_source = bool(tokens)
                if sdk_source:
                    print(f"[ufc_monitor] SDK fallback: {fa} vs {fb}", flush=True)
            except Exception as _sdk_e:
                print(f"[ufc_monitor] SDK fallback error: {_sdk_e}", flush=True)

        if tokens and not sdk_source:
            # SDK tokens use slugs, not CLOB token IDs — skip CLOB refresh for them
            mc_register_tokens([tokens[lbl][0] for lbl in tokens])
            tokens = refresh_clob_prices(tokens)

        check_round_trigger(conn, fight, pin, ev, tokens)
        check_line_drift(conn, fight, pin, tokens)
        check_edge_inversion(conn, fight, tokens, pin)

    conn.close()


if __name__ == "__main__":
    main()

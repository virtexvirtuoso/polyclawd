#!/usr/bin/env python3
"""
Whale alert outcome labeling — closes the loop the /qa report flagged as
Finding #0: alerts are logged but never scored against what happened next.

For every whale alert (all severities — LOW is the shadow data), record the
price at alert time and the whale's direction, then backfill price at +1h,
+6h, and the resolution result. From that, per-fingerprint precision becomes
a query instead of a guess.

Direction convention: +1 = whale long YES (Kalshi) / outcomes[0] (PM),
-1 = whale short. Inferred from taker flow tags first, resting-wall side
second; None when ambiguous (alert still gets price trajectory).

Writes to storage/whale_meta.db (separate from whale_scanner.db — the
scanner holds long write transactions during book phases). Reads alerts
from whale_scanner.db read-only.

CLI:
    python3 signals/whale_outcomes.py --run        # ingest + backfill pass
    python3 signals/whale_outcomes.py --summary    # precision by severity/platform
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
ALERTS_DB_PATH = BASE_DIR / "storage" / "whale_scanner.db"

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_API = "https://gamma-api.polymarket.com"

EPS = 0.005            # |move| below this = no-move, correctness stays NULL
H1, H6 = 3600, 6 * 3600
BACKFILL_CAP = 300     # price lookups per run (batched, so cheap)
GIVE_UP_AFTER = 35 * 24 * 3600   # stop chasing resolution after 35 days


def get_meta_db(path: Optional[Path] = None) -> sqlite3.Connection:
    db_path = Path(path) if path else META_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS whale_outcomes (
            alert_id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            platform TEXT NOT NULL,
            market TEXT NOT NULL,
            severity TEXT, score INTEGER, reasons TEXT,
            condition_id TEXT,
            direction INTEGER,            -- +1 long YES/outcomes[0], -1 short, NULL ambiguous
            price_at_alert REAL,
            price_1h REAL, price_6h REAL,
            result TEXT,                  -- yes|no|outcomes[0]|outcomes[1] when resolved
            correct_1h INTEGER, correct_6h INTEGER, correct_res INTEGER,
            done INTEGER DEFAULT 0,       -- 1 = nothing left to backfill
            updated REAL
        )""")
    conn.commit()
    return conn


def _fetch_json(url: str, timeout: int = 20):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


# ── Direction inference ─────────────────────────────────────────────────────

def direction_from_alert(platform: str, reasons: str, payload: dict) -> Optional[int]:
    """Whale direction in YES/outcomes[0] terms. Flow tags outrank wall side
    (executed conviction beats resting orders)."""
    if platform == "kalshi":
        if "taker_YES" in reasons:
            return 1
        if "taker_NO" in reasons:
            return -1
        if "level_jump_bid" in reasons:
            return 1     # fresh YES-side bid wall = long support
        if "level_jump_ask" in reasons:
            return -1    # fresh ask wall = supply
        return None

    # Polymarket: flow_desc like "BUY Indiana Fever $1,234 (95%)" — needs the
    # market's outcome list (passed in payload at labeling time) to map to a sign.
    desc = payload.get("flow_desc") or ""
    outcomes = payload.get("_outcomes") or []
    if not desc or len(outcomes) < 2:
        return None
    side = "BUY" if desc.startswith("BUY ") else "SELL" if desc.startswith("SELL ") else None
    if not side:
        return None
    rest = desc.split(" ", 1)[1]
    o0, o1 = outcomes[0], outcomes[1]
    target = 0 if rest.startswith(o0[:18]) else 1 if rest.startswith(o1[:18]) else None
    if target is None:
        return None
    sign = 1 if target == 0 else -1
    return sign if side == "BUY" else -sign


def _correct(direction: Optional[int], p0: Optional[float],
             p1: Optional[float]) -> Optional[int]:
    if direction is None or p0 is None or p1 is None:
        return None
    move = p1 - p0
    if abs(move) < EPS:
        return None
    return 1 if (move > 0) == (direction > 0) else 0


# ── Price/result fetchers ───────────────────────────────────────────────────

def kalshi_lookup(tickers: list) -> dict:
    """ticker -> {mid, result}. result is 'yes'/'no' once settled, else ''."""
    out = {}
    for i in range(0, len(tickers), 100):
        d = _fetch_json(f"{KALSHI_API}/markets?limit=100&tickers={','.join(tickers[i:i+100])}")
        for m in (d or {}).get("markets", []):
            try:
                bid = float(m.get("yes_bid_dollars") or 0)
                ask = float(m.get("yes_ask_dollars") or 0)
                mid = (bid + ask) / 2 if (bid or ask) else None
            except (TypeError, ValueError):
                mid = None
            out[m["ticker"]] = {"mid": mid, "result": m.get("result") or ""}
    return out


def pm_lookup(slugs: list) -> dict:
    """slug -> {mid, result, outcomes}. result = winning outcome name when
    resolved (outcomePrices pins to 1), else ''."""
    out = {}
    for slug in slugs:
        d = _fetch_json(f"{GAMMA_API}/markets?slug={slug}&limit=1")
        if not d:
            continue
        m = d[0]
        try:
            outcomes = json.loads(m.get("outcomes") or "[]")
            prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
        except (ValueError, TypeError):
            outcomes, prices = [], []
        mid = m.get("lastTradePrice")
        result = ""
        if m.get("closed") and prices and max(prices) > 0.99:
            result = outcomes[prices.index(max(prices))] if outcomes else "resolved"
        out[slug] = {"mid": mid, "result": result, "outcomes": outcomes}
    return out


# ── Pipeline ────────────────────────────────────────────────────────────────

def ingest_new_alerts(meta: sqlite3.Connection,
                      alerts_db_path: Optional[Path] = None) -> int:
    """Copy alerts not yet tracked into whale_outcomes with their at-alert
    price and (Kalshi) direction. PM direction resolves on first backfill."""
    src = sqlite3.connect(f"file:{alerts_db_path or ALERTS_DB_PATH}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    last = meta.execute("SELECT COALESCE(MAX(alert_id),0) FROM whale_outcomes").fetchone()[0]
    rows = src.execute("SELECT * FROM whale_alerts WHERE id > ? ORDER BY id LIMIT 2000",
                       (last,)).fetchall()
    src.close()

    n = 0
    for r in rows:
        try:
            p = json.loads(r["payload"] or "{}")
        except json.JSONDecodeError:
            p = {}
        if r["platform"] == "kalshi":
            bid, ask = p.get("best_bid"), p.get("best_ask")
            p0 = (bid + ask) / 2 if (bid is not None and ask is not None and (bid or ask)) else None
            if p0 is None:
                p0 = p.get("last_yes_price")
            direction = direction_from_alert("kalshi", r["reasons"] or "", p)
        else:
            p0 = p.get("current_price")
            direction = None   # needs gamma outcomes; resolved on first backfill
        meta.execute(
            "INSERT OR IGNORE INTO whale_outcomes"
            " (alert_id, ts, platform, market, severity, score, reasons,"
            "  condition_id, direction, price_at_alert, updated)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (r["id"], r["ts"], r["platform"], r["market"], r["severity"],
             r["score"], r["reasons"], p.get("condition_id"), direction, p0, time.time()))
        n += 1
    meta.commit()
    return n


def backfill(meta: sqlite3.Connection) -> dict:
    """Fill price_1h / price_6h / result for rows that are due."""
    now = time.time()
    due = meta.execute(
        "SELECT * FROM whale_outcomes WHERE done=0 AND ("
        " (price_1h IS NULL AND ts <= ?) OR"
        " (price_6h IS NULL AND ts <= ?) OR"
        " (result IS NULL OR result = ''))"
        " AND ts <= ? ORDER BY ts LIMIT ?",
        (now - H1, now - H6, now - H1, BACKFILL_CAP)).fetchall()
    if not due:
        return {"due": 0}

    k_tickers = sorted({r["market"] for r in due if r["platform"] == "kalshi"})
    p_slugs = sorted({r["market"] for r in due if r["platform"] == "polymarket"})
    k = kalshi_lookup(k_tickers) if k_tickers else {}
    pm = pm_lookup(p_slugs[:80]) if p_slugs else {}   # gamma is 1 call/slug; cap

    filled = resolved = 0
    for r in due:
        info = (k if r["platform"] == "kalshi" else pm).get(r["market"])
        if not info:
            if now - r["ts"] > GIVE_UP_AFTER:
                meta.execute("UPDATE whale_outcomes SET done=1, updated=? WHERE alert_id=?",
                             (now, r["alert_id"]))
            continue

        direction = r["direction"]
        if r["platform"] == "polymarket" and direction is None and info.get("outcomes"):
            try:
                payload = json.loads(sqlite3.connect(
                    f"file:{ALERTS_DB_PATH}?mode=ro", uri=True).execute(
                    "SELECT payload FROM whale_alerts WHERE id=?",
                    (r["alert_id"],)).fetchone()[0] or "{}")
            except Exception:
                payload = {}
            payload["_outcomes"] = info["outcomes"]
            direction = direction_from_alert("polymarket", r["reasons"] or "", payload)

        sets, vals = ["updated=?", "direction=?"], [now, direction]
        mid = info.get("mid")
        if r["price_1h"] is None and now - r["ts"] >= H1 and mid is not None:
            sets += ["price_1h=?", "correct_1h=?"]
            vals += [mid, _correct(direction, r["price_at_alert"], mid)]
        if r["price_6h"] is None and now - r["ts"] >= H6 and mid is not None:
            sets += ["price_6h=?", "correct_6h=?"]
            vals += [mid, _correct(direction, r["price_at_alert"], mid)]

        result = info.get("result") or ""
        if result:
            win = None
            if direction is not None:
                if r["platform"] == "kalshi":
                    win = 1 if (result == "yes") == (direction > 0) else 0
                elif info.get("outcomes"):
                    won0 = result == info["outcomes"][0]
                    win = 1 if won0 == (direction > 0) else 0
            sets += ["result=?", "correct_res=?", "done=1"]
            vals += [result, win]
            resolved += 1
        elif now - r["ts"] > GIVE_UP_AFTER:
            sets += ["done=1"]

        vals.append(r["alert_id"])
        meta.execute(f"UPDATE whale_outcomes SET {', '.join(sets)} WHERE alert_id=?", vals)
        filled += 1
    meta.commit()
    return {"due": len(due), "filled": filled, "resolved": resolved}


def run_pass(meta: Optional[sqlite3.Connection] = None) -> dict:
    """Scheduler entry point: ingest new alerts, backfill due ones."""
    conn = meta or get_meta_db()
    try:
        n = ingest_new_alerts(conn)
        stats = backfill(conn)
        stats["ingested"] = n
        return stats
    finally:
        if meta is None:
            conn.close()


def precision_summary(conn) -> str:
    lines = ["whale-alert precision (direction-known alerts only):"]
    q = """SELECT platform, severity,
                  COUNT(*) n,
                  SUM(CASE WHEN correct_1h IS NOT NULL THEN 1 ELSE 0 END) n1,
                  AVG(correct_1h) p1,
                  SUM(CASE WHEN correct_res IS NOT NULL THEN 1 ELSE 0 END) nr,
                  AVG(correct_res) pr
           FROM whale_outcomes WHERE direction IS NOT NULL
           GROUP BY platform, severity ORDER BY platform, severity"""
    for r in conn.execute(q):
        p1 = f"{r['p1']:.0%}" if r["p1"] is not None else "—"
        pr = f"{r['pr']:.0%}" if r["pr"] is not None else "—"
        lines.append(f"  {r['platform']:10s} {r['severity']:8s} n={r['n']:<5d}"
                     f" 1h-precision={p1} (n={r['n1']})  resolution={pr} (n={r['nr']})")
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    conn = get_meta_db()
    if args.run:
        print(run_pass(conn))
    print(precision_summary(conn))
    conn.close()

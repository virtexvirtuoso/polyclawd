"""Whale Shark dashboard routes — read-only views over whale_scanner.db
(alerts, sweep state) and whale_meta.db (outcome labels, wallet ledger).

Handlers are sync `def` on purpose: they do file I/O and FastAPI runs them
in the threadpool; an `async def` doing sqlite would block the event loop
(see the 2026-06 polyclawd-api hang postmortem).
"""

import json
import logging
import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, Query
from typing import Optional

logger = logging.getLogger(__name__)

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parents[2]
ALERTS_DB = BASE_DIR / "storage" / "whale_scanner.db"
META_DB = BASE_DIR / "storage" / "whale_meta.db"


def _ro(path: Path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


@router.get("/whale/alerts")
def whale_alerts(
    severity: str = Query("all"),
    platform: str = Query("all"),
    hours: int = Query(24, le=168),
    limit: int = Query(60, le=200),
):
    """Recent whale alerts, newest first, payload fields inlined."""
    try:
        conn = _ro(ALERTS_DB)
    except sqlite3.OperationalError:
        return {"alerts": [], "counts": {}}
    q = "SELECT * FROM whale_alerts WHERE ts > ?"
    args = [time.time() - hours * 3600]
    if severity.upper() in ("CRITICAL", "HIGH", "LOW"):
        q += " AND severity = ?"
        args.append(severity.upper())
    if platform.lower() in ("kalshi", "polymarket"):
        q += " AND platform = ?"
        args.append(platform.lower())
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)

    alerts = []
    for r in conn.execute(q, args):
        try:
            p = json.loads(r["payload"] or "{}")
        except json.JSONDecodeError:
            p = {}
        alerts.append(
            {
                "id": r["id"],
                "ts": r["ts"],
                "platform": r["platform"],
                "market": r["market"],
                "severity": r["severity"],
                "score": r["score"],
                "reasons": r["reasons"] or "",
                "title": p.get("title") or "",
                "sub_title": p.get("sub_title") or "",
                "close_time": p.get("close_time") or "",
                "best_bid": p.get("best_bid"),
                "best_ask": p.get("best_ask"),
                "current_price": p.get("current_price"),
                "bid_depth": p.get("bid_depth"),
                "ask_depth": p.get("ask_depth"),
                "flow_yes": p.get("flow_yes"),
                "flow_no": p.get("flow_no"),
                "flow_dollars": p.get("flow_dollars"),
                "flow_desc": p.get("flow_desc") or "",
                "top_wallet_name": p.get("top_wallet_name") or "",
                "top_wallet_usd": p.get("top_wallet_usd"),
                "smart": "smart_wallet" in (r["reasons"] or ""),
            }
        )
    counts = {
        r["severity"]: r["n"]
        for r in conn.execute(
            "SELECT severity, COUNT(*) n FROM whale_alerts WHERE ts > ? GROUP BY severity",
            (time.time() - hours * 3600,),
        )
    }
    conn.close()
    return {"alerts": alerts, "counts": counts}


@router.get("/whale/stats")
def whale_stats():
    """Scan health + outcome precision for the dashboard header panels."""
    out = {"health": {}, "precision": [], "counts_24h": {}}
    now = time.time()
    try:
        conn = _ro(ALERTS_DB)
        last_snap = conn.execute("SELECT MAX(ts) FROM whale_snapshots").fetchone()[0]
        out["health"] = {
            "last_scan_age_s": round(now - last_snap) if last_snap else None,
            "markets_tracked": {
                r["platform"]: r["n"]
                for r in conn.execute("SELECT platform, COUNT(*) n FROM market_state GROUP BY platform")
            },
            "snapshots_48h": conn.execute("SELECT COUNT(*) FROM whale_snapshots").fetchone()[0],
            "alerts_24h": conn.execute("SELECT COUNT(*) FROM whale_alerts WHERE ts > ?", (now - 86400,)).fetchone()[0],
        }
        out["counts_24h"] = {
            r["severity"]: r["n"]
            for r in conn.execute(
                "SELECT severity, COUNT(*) n FROM whale_alerts WHERE ts > ? GROUP BY severity", (now - 86400,)
            )
        }
        conn.close()
    except sqlite3.OperationalError:
        pass

    try:
        meta = _ro(META_DB)
        for r in meta.execute(
            """SELECT platform, severity, COUNT(*) n,
                          SUM(CASE WHEN correct_1h IS NOT NULL THEN 1 ELSE 0 END) n1,
                          AVG(correct_1h) p1,
                          SUM(CASE WHEN correct_res IS NOT NULL THEN 1 ELSE 0 END) nr,
                          AVG(correct_res) pr
                   FROM whale_outcomes WHERE direction IS NOT NULL
                   GROUP BY platform, severity"""
        ):
            out["precision"].append(
                {
                    "platform": r["platform"],
                    "severity": r["severity"],
                    "n": r["n"],
                    "n_1h": r["n1"],
                    "p_1h": round(r["p1"], 3) if r["p1"] is not None else None,
                    "n_res": r["nr"],
                    "p_res": round(r["pr"], 3) if r["pr"] is not None else None,
                }
            )
        out["health"]["outcomes_tracked"] = meta.execute("SELECT COUNT(*) FROM whale_outcomes").fetchone()[0]
        meta.close()
    except sqlite3.OperationalError:
        pass
    return out


@router.get("/whale/follows")
def whale_follows(limit: int = Query(40, le=200)):
    """Paper whale-follow strategy: open positions, closed round-trips,
    cell matrix (archetype x INFO tercile), equity curve. Read-only."""
    out = {
        "open": [],
        "closed": [],
        "cells": [],
        "equity": [],
        "totals": {"n": 0, "open": 0, "closed": 0, "pnl_net": 0.0, "win_rate": None},
    }
    try:
        meta = _ro(META_DB)
        meta.execute("SELECT 1 FROM whale_follows LIMIT 1")
    except sqlite3.OperationalError:
        return out

    cols = (
        "follow_id, ts_alert, ts_entry, platform, market, archetype,"
        " info_score, direction, entry_px, target_px, stop_px, exit_policy,"
        " ts_exit, exit_px, exit_reason, pnl_net, result, done"
    )
    for r in meta.execute(f"SELECT {cols} FROM whale_follows WHERE done=0 ORDER BY ts_entry DESC LIMIT ?", (limit,)):
        out["open"].append(dict(r))
    for r in meta.execute(f"SELECT {cols} FROM whale_follows WHERE done=1 ORDER BY ts_exit DESC LIMIT ?", (limit,)):
        out["closed"].append(dict(r))

    for r in meta.execute(
        """SELECT archetype,
                      CASE WHEN info_score >= 0.7 THEN 'hi'
                           WHEN info_score >= 0.6 THEN 'mid' ELSE 'lo' END tercile,
                      COUNT(*) n, SUM(done) closed,
                      ROUND(SUM(pnl_net), 2) pnl,
                      ROUND(AVG(CASE WHEN done=1 AND pnl_net IS NOT NULL
                                THEN (pnl_net > 0) END), 3) wr
               FROM whale_follows GROUP BY archetype, tercile"""
    ):
        out["cells"].append(dict(r))

    eq = 0.0
    for r in meta.execute(
        "SELECT ts_exit, pnl_net FROM whale_follows WHERE done=1 AND pnl_net IS NOT NULL ORDER BY ts_exit"
    ):
        eq += r["pnl_net"]
        out["equity"].append({"ts": r["ts_exit"], "eq": round(eq, 2)})

    t = meta.execute(
        """SELECT COUNT(*) n, SUM(done=0) o, SUM(done=1) c,
                  ROUND(SUM(pnl_net), 2) pnl,
                  ROUND(AVG(CASE WHEN done=1 AND pnl_net IS NOT NULL
                            THEN (pnl_net > 0) END), 3) wr
           FROM whale_follows"""
    ).fetchone()
    out["totals"] = {
        "n": t["n"],
        "open": t["o"] or 0,
        "closed": t["c"] or 0,
        "pnl_net": t["pnl"] or 0.0,
        "win_rate": t["wr"],
    }

    # Entry funnel: today's counters + 7-day sum (written by bump_funnel).
    import datetime as _dt
    today = _dt.datetime.now(_dt.timezone.utc).date()
    funnel_today, funnel_7d = {}, {}
    try:
        kv_rows = meta.execute("SELECT key, value FROM follower_kv"
                               " WHERE key LIKE 'funnel:%'").fetchall()
    except sqlite3.OperationalError:
        kv_rows = []
    for r in kv_rows:
        try:
            day = _dt.date.fromisoformat(r["key"][7:])
            vals = json.loads(r["value"] or "{}")
        except (ValueError, json.JSONDecodeError):
            continue
        age = (today - day).days
        if age < 0 or age > 6:
            continue
        for k, v in vals.items():
            funnel_7d[k] = funnel_7d.get(k, 0) + v
            if age == 0:
                funnel_today[k] = funnel_today.get(k, 0) + v
    out["funnel"] = {"today": funnel_today, "d7": funnel_7d}

    # Decay grid (the K1 evidence): mean direction-signed move per horizon,
    # by archetype plus an 'all' row. n per horizon so the UI can grey low-N.
    horizons = (("5m", "px_5m"), ("15m", "px_15m"), ("30m", "px_30m"),
                ("1h", "px_1h"), ("4h", "px_4h"))
    sel = ", ".join(
        f"COUNT({c}) n_{h}, ROUND(AVG(direction*({c} - alert_mid)), 4) d_{h}"
        for h, c in horizons)
    out["decay"] = []
    for grp in ("archetype", "'all'"):
        for r in meta.execute(
                f"SELECT {grp} arch, {sel} FROM whale_follows"
                f" WHERE alert_mid IS NOT NULL GROUP BY {grp}"):
            row = {"archetype": r["arch"], "points": []}
            for h, _ in horizons:
                row["points"].append({"h": h, "n": r[f"n_{h}"], "d": r[f"d_{h}"]})
            out["decay"].append(row)

    meta.close()
    return out


@router.get("/whale/wallets")
def whale_wallets(limit: int = Query(20, le=100)):
    """Wallet ledger: smart wallets first, by realized PnL."""
    try:
        meta = _ro(META_DB)
    except sqlite3.OperationalError:
        return {"wallets": [], "smart_count": 0, "tracked": 0, "queued": 0}
    wallets = [
        dict(r)
        for r in meta.execute(
            "SELECT wallet, name, closed_positions, wins, win_rate, realized_pnl,"
            " smart, last_seen FROM pm_wallets"
            " ORDER BY smart DESC, realized_pnl DESC LIMIT ?",
            (limit,),
        )
    ]
    smart = meta.execute("SELECT COUNT(*) FROM pm_wallets WHERE smart=1").fetchone()[0]
    tracked = meta.execute("SELECT COUNT(*) FROM pm_wallets").fetchone()[0]
    queued = meta.execute("SELECT COUNT(*) FROM pm_wallet_seen").fetchone()[0]
    meta.close()
    return {"wallets": wallets, "smart_count": smart, "tracked": tracked, "queued": queued}


# ── Live order book (for the dashboard's per-alert depth visualization) ─────
_BOOK_CACHE: dict = {}  # market -> (ts, payload); 10s TTL, protects upstreams
_BOOK_TTL = 10.0


def _fetch_json(url: str, timeout: int = 8):
    import urllib.request

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


@router.get("/whale/book")
def whale_book(platform: str = Query(...), market: str = Query(...)):
    """Live depth ladder for one market. Kalshi: yes/no complement via the
    scanner's parser. Polymarket: gamma slug -> clobTokenIds[0] -> CLOB book."""
    now = time.time()
    cached = _BOOK_CACHE.get(market)
    if cached and now - cached[0] < _BOOK_TTL:
        return cached[1]

    bids, asks = [], []
    if platform == "kalshi":
        from signals.whale_scanner import parse_kalshi_book

        d = _fetch_json(f"https://api.elections.kalshi.com/trade-api/v2/markets/{market}/orderbook")
        if d and d.get("orderbook_fp"):
            b, a = parse_kalshi_book(d["orderbook_fp"])
            bids, asks = b[:12], a[:12]
    elif platform == "polymarket":
        g = _fetch_json(f"https://gamma-api.polymarket.com/markets?slug={market}&limit=1")
        token = None
        if g:
            try:
                token = json.loads(g[0].get("clobTokenIds") or "[]")[0]
            except (ValueError, IndexError, TypeError):
                token = None
        if token:
            d = _fetch_json(f"https://clob.polymarket.com/book?token_id={token}")
            if d:
                bids = sorted(
                    ((float(x["price"]), float(x["size"])) for x in d.get("bids") or []), key=lambda v: -v[0]
                )[:12]
                asks = sorted(((float(x["price"]), float(x["size"])) for x in d.get("asks") or []), key=lambda v: v[0])[
                    :12
                ]

    payload = {"market": market, "platform": platform, "ts": now, "bids": bids, "asks": asks}
    if bids or asks:
        _BOOK_CACHE[market] = (now, payload)
        if len(_BOOK_CACHE) > 300:
            _BOOK_CACHE.clear()
    return payload


# ── Ranked alert feed ───────────────────────────────────────────────────────

_WEIGHTS = {
    "flow_size": 0.30,
    "wallet_reputation": 0.25,
    "spread": 0.15,
    "urgency": 0.15,
    "archetype_bonus": 0.15,
}

def _score_alert(row):
    score = 0.0
    components = {}

    fd = row["flow_dollars"] or 0
    fs = 1.0 if fd >= 100000 else 0.8 if fd >= 50000 else 0.6 if fd >= 25000 else 0.4 if fd >= 10000 else 0.2
    score += fs * _WEIGHTS["flow_size"]
    components["flow_size"] = round(fs, 2)

    wr = row["wallet_win_rate"]
    if wr is not None:
        ws = 1.0 if wr >= 0.65 else 0.7 if wr >= 0.55 else 0.4 if wr >= 0.45 else 0.2
    else:
        ws = 0.3
    score += ws * _WEIGHTS["wallet_reputation"]
    components["wallet_rep"] = round(ws, 2)

    sp = row["spread_bps"]
    if sp is not None and sp > 0:
        ss = 1.0 if sp < 20 else 0.7 if sp < 50 else 0.4 if sp < 100 else 0.2 if sp < 200 else 0.1
    else:
        ss = 0.5
    score += ss * _WEIGHTS["spread"]
    components["spread"] = round(ss, 2)

    htr = row["hours_to_resolve"]
    if htr is not None and htr > 0:
        us = 1.0 if htr < 2 else 0.8 if htr < 6 else 0.5 if htr < 24 else 0.3 if htr < 72 else 0.1
    else:
        us = 0.3
    score += us * _WEIGHTS["urgency"]
    components["urgency"] = round(us, 2)

    arch_bonus = {"weather": 1.0, "election": 0.5, "deadline_binary": 0.5, "other": 0.5, "index": 0.3, "sports": 0.15}
    ab = arch_bonus.get(row["market_archetype"] or "", 0.3)
    score += ab * _WEIGHTS["archetype_bonus"]
    components["archetype"] = round(ab, 2)

    return round(score, 3), components


@router.get("/whale/top")
def whale_top(limit: int = Query(10, ge=1, le=50),
              min_score: float = Query(0, ge=0, le=1),
              severity: Optional[str] = Query(None),
              platform: Optional[str] = Query(None)):
    """Top-ranked whale alerts by composite score. Auto-learns from resolutions."""
    conn = sqlite3.connect(str(META_DB), timeout=5)
    conn.row_factory = sqlite3.Row

    base_where = "done = 0 AND flow_dollars > 0 AND severity IN ('CRITICAL','HIGH','LOW')"
    params = []
    extra = ""
    if severity:
        extra += " AND severity = ?"
        params.append(severity.upper())
    if platform:
        extra += " AND platform = ?"
        params.append(platform.lower())
    query = "SELECT * FROM whale_outcomes WHERE " + base_where + extra + " ORDER BY alert_id DESC LIMIT 500"
    rows = conn.execute(query, params).fetchall()

    seen = {}
    for r in rows:
        htr = r["hours_to_resolve"]
        if htr is not None and htr < 0:
            continue
        s, comps = _score_alert(r)
        if s < min_score:
            continue
        w = r["top_wallet"] or ""
        wallet_short = w[:10] + "..." if len(w) > 16 else w
        # Cost-adjusted EV: wallet_win_rate - entry_price - half_spread
        # direction=-1 means whale bet NO, so entry price is (1 - YES_price)
        _wr = r["wallet_win_rate"]
        _price = r["price_at_alert"]
        _dir = r["direction"]
        _sp = r["spread_bps"]
        _half_sp = (_sp / 2 / 10000) if (_sp and _sp > 0) else 0.005
        if _wr is not None and _price is not None:
            _entry = (1.0 - _price) if _dir == -1 else _price
            ev_net = round(_wr - _entry - _half_sp, 3)
        else:
            ev_net = None

        entry = {
            "market": r["market"],
            "platform": r["platform"],
            "severity": r["severity"],
            "score": s,
            "price": r["price_at_alert"],
            "flow_dollars": r["flow_dollars"],
            "wallet": wallet_short,
            "wallet_win_rate": r["wallet_win_rate"],
            "wallet_n": r["wallet_n"],
            "spread_bps": r["spread_bps"],
            "hours_to_resolve": r["hours_to_resolve"],
            "archetype": r["market_archetype"],
            "ev_net": ev_net,
            "url": ("https://polymarket.com/market/" + r["market"]) if r["platform"] == "polymarket" else ("https://kalshi.com/markets/" + r["market"].split("-")[0]),
        }
        if r["market"] not in seen or s > seen[r["market"]]["score"]:
            seen[r["market"]] = entry

    alerts = sorted(seen.values(), key=lambda x: x["score"], reverse=True)[:limit]
    for i, a in enumerate(alerts):
        a["rank"] = i + 1

    conn.close()
    return {"count": len(alerts), "alerts": alerts, "weights": _WEIGHTS}


@router.get("/whale/precision")
def whale_precision():
    """Precision by severity, platform, archetype, and flow size."""
    conn = sqlite3.connect(str(META_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    results = {}

    for label, col in [("by_severity", "severity"), ("by_platform", "platform"), ("by_archetype", "market_archetype")]:
        rows = conn.execute(
            "SELECT %s, COUNT(*) as total, SUM(CASE WHEN correct_res IS NOT NULL THEN 1 ELSE 0 END) as resolved, "
            "ROUND(AVG(CASE WHEN correct_res IS NOT NULL THEN correct_res END), 3) as precision "
            "FROM whale_outcomes WHERE direction IS NOT NULL AND %s IS NOT NULL "
            "GROUP BY %s ORDER BY COUNT(*) DESC" % (col, col, col)
        ).fetchall()
        results[label] = [dict(r) for r in rows]

    rows = conn.execute(
        "SELECT CASE WHEN flow_dollars < 5000 THEN 'u5k' WHEN flow_dollars < 25000 THEN 'f5k25k' "
        "WHEN flow_dollars < 100000 THEN 'f25k100k' ELSE 'o100k' END as bucket, "
        "COUNT(*) as total, SUM(CASE WHEN correct_res IS NOT NULL THEN 1 ELSE 0 END) as resolved, "
        "ROUND(AVG(CASE WHEN correct_res IS NOT NULL THEN correct_res END), 3) as precision "
        "FROM whale_outcomes WHERE direction IS NOT NULL AND flow_dollars IS NOT NULL "
        "GROUP BY 1 ORDER BY MIN(flow_dollars)"
    ).fetchall()
    results["by_flow_size"] = [dict(r) for r in rows]

    conn.close()
    return results

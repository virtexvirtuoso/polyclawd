#!/usr/bin/env python3
"""
Smart Wallet Entry/Exit Alert — shadow-first.
=============================================

Fires a dedicated alert when a graduated smart wallet accumulates >= $500 of
BUY (Entry) or SELL (Exit) flow in a single market inside a TRUE rolling 4h
window, independent of the anonymous whale-flow pipeline.

Shadow-first posture (this build):
  * Every fire writes a row to `smart_wallet_shadows` with `price_at_alert`.
  * Telegram delivery is OFF unless env SMART_WALLET_ALERT_SEND=1, so we collect
    >= 50 resolved shadows (scope Gate 2) and confirm the +47%/68% follow-PnL
    holds at live alert-time prices BEFORE any real-money follow.

Design decisions baked in from the 2026-06-23 follow-through backtest:
  * The accumulator stores TIMESTAMPED FILLS and re-sums the 4h window each
    update — NOT a running total. A running total can never age out partial old
    flow ("4h rolling" would be a lie); a wallet dribbling $50/3h59m would
    accumulate forever. See research/smart_wallet_followthru_SPEC.md.
  * Resolution of shadows (resolve_shadows) must use the COMPLETE rule — no
    single PM positions field encodes win/loss (realizedPnl misses unredeemed
    winners; redeemable means resolved-not-won; curPrice collapses post-
    resolution). v1 resolver is a placeholder that wants a market-settlement
    source (CLOB /markets/{cid} or on-chain UMA); see resolve_shadows().

Scanner hook (one guarded call) lives in signals/whale_scanner.py.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Callable, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
# Canonical project shadow DB (NOT the stray 0-byte repo-root file) — matches
# signals/shadow_tracker.py and services/scheduler.py.
SHADOW_DB = BASE_DIR / "storage" / "shadow_trades.db"

# --- alert parameters (validated by the follow-through backtest) -----------
ACCUM_WINDOW = 4 * 3600  # 4h rolling window
THRESHOLD = 500.0  # $ cumulative that triggers an alert
REFIRE_MULT = 2.0  # re-alert when cumulative doubles
MIN_ALERT_MARKET_VOL = 100_000.0
NEAR_SETTLED_HI = 0.90  # suppress when held outcome priced >= this
NEAR_SETTLED_LO = 0.10  # ...or <= this (no edge near resolution)

# Telegram delivery is opt-in (shadow-first). Logging always happens.
_SEND_ENABLED = os.environ.get("SMART_WALLET_ALERT_SEND", "0") == "1"


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def init_accum(conn) -> None:
    """Rolling-window accumulator (lives in whale_meta.db). One row per
    (wallet, market, direction); `fills_json` is the pruned 4h fill list."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS smart_wallet_accum (
            wallet       TEXT,
            market       TEXT,
            direction    TEXT,
            fills_json   TEXT,
            total_usd    REAL,
            num_fills    INTEGER,
            first_seen   INTEGER,
            last_seen    INTEGER,
            alert_fired  INTEGER DEFAULT 0,
            fired_total  REAL DEFAULT 0,
            PRIMARY KEY (wallet, market, direction)
        )""")
    conn.commit()


def init_shadows(conn) -> None:
    """Shadow trade log (lives in shadow_trades.db). One row per fired alert."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS smart_wallet_shadows (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet         TEXT,
            market         TEXT,
            title          TEXT,
            direction      TEXT,
            outcome        TEXT,
            outcome_index  INTEGER,
            price_at_alert REAL,
            cumulative_usd REAL,
            num_fills      INTEGER,
            alert_type     TEXT,
            ts_alert       INTEGER,
            resolved       INTEGER DEFAULT 0,
            outcome_result TEXT,
            closing_price  REAL,
            clv            REAL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sw_shadows_open ON smart_wallet_shadows(resolved, ts_alert)")
    conn.commit()


# --------------------------------------------------------------------------- #
# Accumulator (true rolling window)
# --------------------------------------------------------------------------- #
def _accumulate(conn, wallet: str, market: str, direction: str, usd: float, price: float, now: int):
    """Add a fill, prune the 4h window, persist. Returns
    (total_usd, num_fills, alert_fired, fired_total) AFTER the update."""
    row = conn.execute(
        "SELECT fills_json, alert_fired, fired_total, first_seen "
        "FROM smart_wallet_accum WHERE wallet=? AND market=? AND direction=?",
        (wallet, market, direction),
    ).fetchone()
    if row:
        fills = json.loads(row["fills_json"] or "[]")
        alert_fired = row["alert_fired"] or 0
        fired_total = row["fired_total"] or 0.0
        first_seen = row["first_seen"] or now
    else:
        fills, alert_fired, fired_total, first_seen = [], 0, 0.0, now

    fills.append([int(now), float(usd), float(price)])
    cutoff = now - ACCUM_WINDOW
    fills = [f for f in fills if f[0] >= cutoff]
    total = sum(f[1] for f in fills)

    # If the last fire fell out of the window, the dedup period is over —
    # allow a fresh accumulation to fire again.
    if alert_fired and alert_fired < cutoff:
        alert_fired, fired_total = 0, 0.0

    conn.execute(
        "INSERT INTO smart_wallet_accum "
        "(wallet, market, direction, fills_json, total_usd, num_fills, "
        " first_seen, last_seen, alert_fired, fired_total) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(wallet, market, direction) DO UPDATE SET "
        " fills_json=excluded.fills_json, total_usd=excluded.total_usd, "
        " num_fills=excluded.num_fills, last_seen=excluded.last_seen, "
        " alert_fired=excluded.alert_fired, fired_total=excluded.fired_total",
        (
            wallet,
            market,
            direction,
            json.dumps(fills),
            total,
            len(fills),
            first_seen,
            int(now),
            alert_fired,
            fired_total,
        ),
    )
    return total, len(fills), alert_fired, fired_total


def _decide(total: float, alert_fired: int, fired_total: float) -> Optional[str]:
    if alert_fired == 0 and total >= THRESHOLD:
        return "first"
    if alert_fired > 0 and total >= fired_total * REFIRE_MULT:
        return "refire"
    return None


def _mark_fired(conn, wallet, market, direction, now: int, total: float) -> None:
    conn.execute(
        "UPDATE smart_wallet_accum SET alert_fired=?, fired_total=? WHERE wallet=? AND market=? AND direction=?",
        (int(now), float(total), wallet, market, direction),
    )


def _gates_suppress(m: dict) -> bool:
    """True = suppress this alert. Applied BEFORE marking fired, so a gated
    crossing can still fire later when conditions clear."""
    vol = m.get("volume") or 0.0
    price = m.get("price")
    if vol < MIN_ALERT_MARKET_VOL:
        return True
    if price is not None and (price >= NEAR_SETTLED_HI or price <= NEAR_SETTLED_LO):
        return True
    return False


# --------------------------------------------------------------------------- #
# Main entry
# --------------------------------------------------------------------------- #
def check_and_fire(
    meta_conn,
    shadow_conn,
    smart_fills: list,
    meta_for: Callable[[str], dict],
    *,
    send: Optional[bool] = None,
    now: Optional[int] = None,
) -> list:
    """Process this cycle's smart-wallet fills; fire + shadow-log crossings.

    smart_fills: list of dicts {wallet, market, direction, usd, price,
                 outcome, outcome_index, name, title}
    meta_for(market) -> {volume, price, title, close_time}
    """
    if now is None:
        now = int(time.time())
    if send is None:
        send = _SEND_ENABLED
    fired = []
    for f in smart_fills:
        total, nfills, af, ft = _accumulate(
            meta_conn, f["wallet"], f["market"], f["direction"], f["usd"], f["price"], now
        )
        kind = _decide(total, af, ft)
        if not kind:
            continue
        m = meta_for(f["market"]) or {}
        if _gates_suppress(m):
            continue
        _mark_fired(meta_conn, f["wallet"], f["market"], f["direction"], now, total)
        alert_type = "refire" if kind == "refire" else "entry" if f["direction"] == "BUY" else "exit"
        rec = {
            "wallet": f["wallet"],
            "name": f.get("name") or f["wallet"][:8],
            "market": f["market"],
            "title": f.get("title") or m.get("title") or f["market"],
            "direction": f["direction"],
            "outcome": f.get("outcome") or "",
            "outcome_index": f.get("outcome_index"),
            "price_at_alert": float(f["price"]),
            "cumulative_usd": round(total, 2),
            "num_fills": nfills,
            "alert_type": alert_type,
            "ts_alert": int(now),
            "market_volume": m.get("volume"),
            "close_time": m.get("close_time"),
        }
        _log_shadow(shadow_conn, rec)
        if send:
            try:
                from scripts.alert_formatter import send_telegram

                send_telegram(_format_alert(rec))
            except Exception:  # noqa: BLE001 - delivery must never break the scan
                pass
        fired.append(rec)
    meta_conn.commit()
    shadow_conn.commit()
    return fired


def _log_shadow(conn, rec: dict) -> None:
    conn.execute(
        "INSERT INTO smart_wallet_shadows "
        "(wallet, market, title, direction, outcome, outcome_index, "
        " price_at_alert, cumulative_usd, num_fills, alert_type, ts_alert) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            rec["wallet"],
            rec["market"],
            rec["title"],
            rec["direction"],
            rec["outcome"],
            rec["outcome_index"],
            rec["price_at_alert"],
            rec["cumulative_usd"],
            rec["num_fills"],
            rec["alert_type"],
            rec["ts_alert"],
        ),
    )


def _format_alert(rec: dict) -> str:
    head = {
        "entry": "🧠 Smart Wallet Entry",
        "exit": "🧠 Smart Wallet Exit ⚠️",
        "refire": "🧠 Smart Wallet — Adding",
    }.get(rec["alert_type"], "🧠 Smart Wallet")
    cents = rec["price_at_alert"] * 100
    side = "YES" if rec["direction"] == "BUY" else "SELL YES"
    return (
        f"{head}\n\n"
        f"{rec['name']}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{rec['title']}\n"
        f"{'Accumulated' if rec['direction'] == 'BUY' else 'Exited'} "
        f"${rec['cumulative_usd']:,.0f} → {side} @ ~{cents:.1f}¢ "
        f"({rec['num_fills']} fills)\n"
        f"[shadow] logged for calibration"
    )


# --------------------------------------------------------------------------- #
# Scanner integration: aggregate this cycle's trades -> smart fills
# --------------------------------------------------------------------------- #
def fills_from_trades(trades: list, smart: dict) -> list:
    """Collapse raw PM trades into per-(wallet, market, side) fills, restricted
    to graduated smart wallets. price = $-weighted avg of the cycle's fills."""
    agg = {}
    for t in trades:
        w = t.get("proxyWallet")
        if not w or w not in smart:
            continue
        cid = t.get("conditionId")
        side = (t.get("side") or "").upper()
        if not cid or side not in ("BUY", "SELL"):
            continue
        size = t.get("size") or 0
        price = t.get("price") or 0
        usd = size * price
        if usd <= 0:
            continue
        k = (w, cid, side)
        a = agg.setdefault(
            k,
            {
                "usd": 0.0,
                "pxw": 0.0,
                "outcome": t.get("outcome") or "",
                "outcome_index": t.get("outcomeIndex"),
                "title": (t.get("title") or "")[:80],
                "name": smart[w].get("name") or t.get("name") or w[:8],
            },
        )
        a["usd"] += usd
        a["pxw"] += usd * price
    out = []
    for (w, cid, side), a in agg.items():
        px = a["pxw"] / a["usd"] if a["usd"] else 0.0
        out.append(
            {
                "wallet": w,
                "market": cid,
                "direction": side,
                "usd": a["usd"],
                "price": px,
                "outcome": a["outcome"],
                "outcome_index": a["outcome_index"],
                "name": a["name"],
                "title": a["title"],
            }
        )
    return out


def run_from_scan(meta_conn, shadow_conn, trades: list, gamma: dict, smart: dict) -> list:
    """Called once per PM sweep from whale_scanner. gamma maps cid -> market
    metadata (volumeNum, liquidityNum, last_price, question, endDate)."""
    fills = fills_from_trades(trades, smart)
    if not fills:
        return []

    def meta_for(cid):
        g = gamma.get(cid) or {}
        return {
            "volume": g.get("volumeNum") or 0.0,
            "price": g.get("last_price"),
            "title": (g.get("question") or "")[:80],
            "close_time": g.get("endDate") or "",
        }

    return check_and_fire(meta_conn, shadow_conn, fills, meta_for)


def scanner_hook(meta_conn, trades: list, gamma: dict, smart: dict) -> list:
    """One-call entry for whale_scanner: ensures tables exist, opens the shadow
    DB, runs the sweep, and never raises into the scan loop."""
    try:
        init_accum(meta_conn)
        sconn = sqlite3.connect(str(SHADOW_DB))
        sconn.row_factory = sqlite3.Row
        try:
            init_shadows(sconn)
            return run_from_scan(meta_conn, sconn, trades, gamma, smart)
        finally:
            sconn.close()
    except Exception:  # noqa: BLE001 - the alert must never break the scan
        return []


# --------------------------------------------------------------------------- #
# Shadow resolution (cron)
# --------------------------------------------------------------------------- #
def resolve_shadows(shadow_conn, settle: Callable[[dict], Optional[float]]) -> int:
    """Backfill outcome + CLV for resolved shadows.

    settle(shadow_row) -> the held outcome's settled value (1.0 won / 0.0 lost)
    or None if not yet resolved. Returns the number newly resolved.
    """
    rows = shadow_conn.execute(
        "SELECT id, wallet, market, outcome_index, price_at_alert FROM smart_wallet_shadows WHERE resolved=0"
    ).fetchall()
    n = 0
    for r in rows:
        settled = settle(dict(r))
        if settled is None:
            continue
        clv = settled - (r["price_at_alert"] or 0.0)
        shadow_conn.execute(
            "UPDATE smart_wallet_shadows SET resolved=1, outcome_result=?, closing_price=?, clv=? WHERE id=?",
            ("WIN" if settled > 0.5 else "LOSS", settled, clv, r["id"]),
        )
        n += 1
    shadow_conn.commit()
    return n


def settle_via_wallet_positions(row: dict) -> Optional[float]:
    """COMPLETE-RULE settlement from the alerting wallet's own position for the
    market (the wallet bought it, so it still holds the row). Validated in the
    2026-06-23 follow-through backtest. No single PM field works alone:
      realizedPnl>0 -> redeemed winner; realizedPnl<0 -> sold at a loss;
      currentValue>=50% of cost (resolved) -> unredeemed winner;
      currentValue<1% (resolved) -> collapsed loser; else still open -> None.
    UPGRADE PATH: a fully survivorship-free run wants market-level settlement
    (CLOB /markets/{cid} resolution or on-chain UMA) instead of the wallet's
    surviving position; Gamma condition_ids misses ~40% of older markets.
    """
    import urllib.request

    wallet, cid, oidx = row.get("wallet"), row.get("market"), row.get("outcome_index")
    if not wallet or not cid:
        return None
    try:
        req = urllib.request.Request(
            f"https://data-api.polymarket.com/positions?user={wallet}&limit=500",
            headers={"User-Agent": "Polyclawd/2.0"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            positions = json.loads(resp.read().decode())
    except Exception:  # noqa: BLE001
        return None
    now = time.time()
    for p in positions if isinstance(positions, list) else positions.get("data", []):
        if p.get("conditionId") != cid or p.get("outcomeIndex") != oidx:
            continue
        iv = float(p.get("initialValue") or 0)
        cv = float(p.get("currentValue") or 0)
        rp = float(p.get("realizedPnl") or 0)
        end = p.get("endDate")
        ended = False
        if end:
            try:
                from datetime import datetime

                ended = datetime.fromisoformat(str(end).replace("Z", "+00:00")).timestamp() < now
            except Exception:  # noqa: BLE001
                ended = False
        resolved = bool(p.get("redeemable")) or ended or abs(rp) > 0.01
        if not resolved or iv <= 0:
            return None
        if rp > 0.01:
            return 1.0
        if rp < -0.01:
            return 0.0
        if cv >= 0.5 * iv:
            return 1.0
        if cv < 0.01 * max(iv, 1):
            return 0.0
        return None
    return None  # position no longer present -> can't resolve from wallet


def main() -> None:
    """Cron entry: resolve outstanding shadows and print a calibration summary
    (scope Gate 2: >=50 resolved before any real-money follow)."""
    conn = sqlite3.connect(str(SHADOW_DB))
    conn.row_factory = sqlite3.Row
    init_shadows(conn)
    n = resolve_shadows(conn, settle_via_wallet_positions)
    row = conn.execute(
        "SELECT COUNT(*) c, "
        " SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END) res, "
        " SUM(CASE WHEN resolved=1 AND outcome_result='WIN' THEN 1 ELSE 0 END) wins, "
        " AVG(CASE WHEN resolved=1 THEN clv END) mean_clv "
        "FROM smart_wallet_shadows"
    ).fetchone()
    res = row["res"] or 0
    wr = (row["wins"] / res) if res else 0.0
    print(
        f"smart-wallet shadows: {row['c']} logged, {res} resolved "
        f"(+{n} this run), WR@alert={wr:.1%}, mean CLV={row['mean_clv'] or 0:+.3f}"
    )
    print("Gate 2 (>=50 resolved): " + ("MET" if res >= 50 else f"{res}/50"))
    conn.close()


if __name__ == "__main__":
    main()

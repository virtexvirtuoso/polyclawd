#!/usr/bin/env python3
"""Wallet allowlist — rolling nightly selector + forward-test report (2026-09-21).

Design locked 2026-09-21 BEFORE any forward data existed (pre-registered gate;
vault: 02-Projects/Polyclawd/Strategy/Wallet-Allowlist-Forward-Test-2026-09-21.md):

  - SELECT (nightly cron 02:45 UTC): rank wallets by PnL per $1 staked over the
    trailing 30 days of resolved BUY shadows (price 0.01-0.60, near_settled=0,
    n >= 10, PnL/$ > 0); top 8 become the allowlist. Appends a dated snapshot
    to storage/wallet_allowlist_snapshots.json and overwrites
    storage/wallet_allowlist_current.json.
  - PRODUCER (scripts/smart_wallet_alert.py): entry/refire alerts from
    allowlisted wallets dispatch at TIER_BATCH (15-min batched pages) instead
    of the tier-3 digest, via page_tier_for(). Missing or >48h-stale allowlist
    file => digest-only (fail-safe to status quo). Pages are visibility only —
    the live book stays dead until the forward gate passes.
  - REPORT: forward-test metrics — allowlist-followed vs blanket-follow per $1
    since the first snapshot, per-wallet forward table, refire slice, and the
    pre-registered gate verdict:
      allowlist beats blanket by >= 3pp per $1, over >= 21 days, with >= 100
      allowlist-followed resolved trades => revive live book at $25 sizing,
      allowlist-only. Otherwise stay paper.

PnL per $1 staked: WIN -> (1-price)/price, LOSS -> -1.

Usage:
    venv/bin/python3 scripts/wallet_allowlist.py select
    venv/bin/python3 scripts/wallet_allowlist.py report
    venv/bin/python3 scripts/wallet_allowlist.py current
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from bisect import bisect_right
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

WINDOW_DAYS = 30
MIN_N = 10
TOP_N = 8
MAX_AGE_H = 48.0
PAGE_TYPES = ("entry", "refire")
GATE_MIN_EDGE_PP = 3.0
GATE_MIN_DAYS = 21
GATE_MIN_TRADES = 100


def _db_path() -> Path:
    return Path(os.environ.get("POLYCLAWD_ALLOWLIST_DB") or BASE / "storage" / "shadow_trades.db")


def _snapshots_path() -> Path:
    return Path(os.environ.get("POLYCLAWD_ALLOWLIST_SNAPSHOTS") or BASE / "storage" / "wallet_allowlist_snapshots.json")


def _current_path() -> Path:
    return Path(os.environ.get("POLYCLAWD_ALLOWLIST_CURRENT") or BASE / "storage" / "wallet_allowlist_current.json")


def _pnl_per_dollar(price, result):
    if not price or price <= 0:
        return None
    return ((1.0 - price) / price) if result == "WIN" else -1.0


def _connect():
    con = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _eligible_rows(con, since_ts):
    """Live-eligible resolved BUY shadows (the slice the live book would trade)."""
    return con.execute(
        "SELECT wallet, ts_alert, price_at_alert, outcome_result, alert_type "
        "FROM smart_wallet_shadows "
        "WHERE resolved=1 AND direction='BUY' AND near_settled=0 "
        "AND price_at_alert > 0.01 AND price_at_alert < 0.60 AND ts_alert >= ?",
        (since_ts,),
    ).fetchall()


def _wallet_stats(rows):
    by = {}
    for r in rows:
        v = _pnl_per_dollar(r["price_at_alert"], r["outcome_result"])
        if v is None:
            continue
        by.setdefault(r["wallet"], []).append(v)
    out = {}
    for w, vs in by.items():
        out[w] = {
            "n": len(vs),
            "pnl_per_dollar": round(sum(vs) / len(vs), 4),
            "wr": round(sum(1 for v in vs if v > 0) / len(vs), 4),
        }
    return out


def select(window_days: int = WINDOW_DAYS, min_n: int = MIN_N, top_n: int = TOP_N) -> dict:
    """Rank the trailing window, write the dated snapshot + current copy."""
    now = time.time()
    con = _connect()
    rows = _eligible_rows(con, now - window_days * 86400)
    con.close()
    stats = _wallet_stats(rows)
    qualifying = {w: s for w, s in stats.items() if s["n"] >= min_n and s["pnl_per_dollar"] > 0}
    ranked = sorted(qualifying.items(), key=lambda kv: (-kv[1]["pnl_per_dollar"], -kv[1]["n"]))[:top_n]
    snap = {
        "date": time.strftime("%Y-%m-%d", time.gmtime(now)),
        "ts": int(now),
        "window_days": window_days,
        "min_n": min_n,
        "top_n": top_n,
        "n_eligible_rows": len(rows),
        "n_qualifying": len(qualifying),
        "wallets": {w: qualifying[w] for w, _ in ranked},
    }
    try:
        snaps = json.loads(_snapshots_path().read_text())
        if not isinstance(snaps, dict) or not isinstance(snaps.get("snapshots"), list):
            snaps = {"snapshots": []}
    except (json.JSONDecodeError, OSError):
        snaps = {"snapshots": []}
    snaps["snapshots"] = [s for s in snaps["snapshots"] if s.get("date") != snap["date"]]
    snaps["snapshots"].append(snap)
    snaps["snapshots"].sort(key=lambda s: s.get("ts", 0))
    _snapshots_path().write_text(json.dumps(snaps, indent=1))
    _current_path().write_text(json.dumps(snap, indent=1))
    print(
        f"allowlist {snap['date']}: {len(ranked)} wallets selected "
        f"(of {len(qualifying)} qualifying, {len(rows)} eligible rows in window)"
    )
    for w, s in ranked:
        print(f"  {w[:10]}… n={s['n']:>4}  pnl/${s['pnl_per_dollar']:+.1%}  wr={s['wr']:.0%}")
    return snap


def current_allowlist(max_age_h: float = MAX_AGE_H) -> set:
    """Lowercased wallets from a fresh current snapshot; empty set = digest-only
    fail-safe (missing file, corrupt JSON, or older than max_age_h)."""
    try:
        snap = json.loads(_current_path().read_text())
    except (json.JSONDecodeError, OSError):
        return set()
    ts = snap.get("ts", 0) if isinstance(snap, dict) else 0
    if not ts or time.time() - ts > max_age_h * 3600:
        return set()
    wallets = snap.get("wallets") or {}
    return {str(w).lower() for w in wallets}


def page_tier_for(wallet, alert_type) -> int:
    """Dispatch tier for one wallet_moves alert: TIER_BATCH for allowlisted
    wallets' entry/refire, else TIER_DIGEST. Any failure => digest (status quo)."""
    try:
        from signals.alert_dispatch import TIER_BATCH, TIER_DIGEST

        if alert_type in PAGE_TYPES and wallet and str(wallet).lower() in current_allowlist():
            return TIER_BATCH
        return TIER_DIGEST
    except Exception:
        return 3  # tier map: wallet_moves=3 (digest) — fail-safe


def _allowlisted_at(snaps, ts_alert) -> set:
    """Wallets in the latest snapshot as of ts_alert (nightly cadence)."""
    times = [s.get("ts", 0) for s in snaps]
    idx = bisect_right(times, ts_alert) - 1
    if idx < 0:
        return set()
    return {str(w).lower() for w in (snaps[idx].get("wallets") or {})}


def report() -> None:
    """Forward-test scoreboard + pre-registered gate verdict."""
    try:
        snaps = json.loads(_snapshots_path().read_text())["snapshots"]
    except (json.JSONDecodeError, OSError, KeyError):
        print("no snapshots yet — run select first")
        return
    if not snaps:
        print("no snapshots yet — run select first")
        return
    first_ts = snaps[0]["ts"]
    days = (time.time() - first_ts) / 86400.0
    con = _connect()
    rows = _eligible_rows(con, first_ts)
    con.close()
    al_num = al_den = 0.0
    al_n = 0
    bl_num = bl_den = 0.0
    bl_n = 0
    rf_num = rf_den = 0.0
    rf_n = 0
    per_wallet = {}
    for r in rows:
        p = r["price_at_alert"]
        v = _pnl_per_dollar(p, r["outcome_result"])
        if v is None:
            continue
        stake = p  # $1 staked at price p = 1/p shares; PnL$ = v * p
        bl_num += v * stake
        bl_den += stake
        bl_n += 1
        if r["wallet"].lower() in _allowlisted_at(snaps, r["ts_alert"]):
            al_num += v * stake
            al_den += stake
            al_n += 1
            pw = per_wallet.setdefault(r["wallet"], [0, 0.0, 0.0])
            pw[0] += 1
            pw[1] += v * stake
            pw[2] += stake
            if r["alert_type"] == "refire":
                rf_num += v * stake
                rf_den += stake
                rf_n += 1
    al = al_num / al_den if al_den else 0.0
    bl = bl_num / bl_den if bl_den else 0.0
    print(f"forward window: {days:.1f} days (since {time.strftime('%Y-%m-%d', time.gmtime(first_ts))})")
    print(f"allowlist-followed: n={al_n}  {al:+.1%} per $1")
    print(f"blanket baseline:   n={bl_n}  {bl:+.1%} per $1")
    print(f"edge: {(al - bl) * 100:+.1f}pp")
    if rf_n:
        print(f"allowlisted refires: n={rf_n}  {rf_num / rf_den:+.1%} per $1")
    print("per-wallet forward:")
    for w, (n, num, den) in sorted(per_wallet.items(), key=lambda kv: -kv[1][1]):
        print(f"  {w[:10]}… n={n:>4}  {num / den:+.1%} per $1" if den else f"  {w[:10]}… n={n}")
    gate_edge = (al - bl) * 100 >= GATE_MIN_EDGE_PP
    gate_days = days >= GATE_MIN_DAYS
    gate_n = al_n >= GATE_MIN_TRADES
    verdict = "PASS" if (gate_edge and gate_days and gate_n) else "NOT YET"
    print(
        f"gate [{verdict}]: edge>= {GATE_MIN_EDGE_PP:.0f}pp {gate_edge} | "
        f"days>= {GATE_MIN_DAYS} {gate_days} | trades>= {GATE_MIN_TRADES} {gate_n}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("select", help="rank trailing window, write snapshots (nightly cron)")
    sub.add_parser("report", help="forward-test metrics + gate verdict")
    sub.add_parser("current", help="print the current allowlist")
    args = ap.parse_args()
    if args.cmd == "select":
        select()
    elif args.cmd == "report":
        report()
    else:
        wallets = current_allowlist()
        if not wallets:
            print("(empty or stale — digest-only fail-safe active)")
        for w in sorted(wallets):
            print(w)


if __name__ == "__main__":
    main()

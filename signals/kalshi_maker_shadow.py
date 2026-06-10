#!/usr/bin/env python3
"""
Kalshi Maker-Fill Shadow — knob 2 dry-run (2026-06-10 optimization plan).

Answers: if we had RESTED orders instead of paying the spread (taker), would we
have been filled, and at what P&L? Reads the nightly fade scans
(data/kalshi_fade_scans.jsonl), reconstructs hypothetical resting quotes for
every tier-classified candidate, and settles fills from the PUBLIC trade tape.

HONESTY RULES (the §8 MM-shadow died of optimistic fills — never repeat it):
  - JOIN level (rest at current best bid): filled only on STRICT trade-through
    (a trade printed at a price strictly better-for-the-counterparty than our
    level — i.e. our bid must have been swept first). Queue position unknowable
    -> trades AT our level do NOT count.
  - IMPROVE level (best bid + 1c): we are top of book; trades AT or through the
    level count, but this is flagged optimistic-if-competed in output.
  - Resting window: candidate scan time -> 23:15 local (cancel before the 00Z
    model run lands ~23:30 ET — the pick-off shield).
  - Unfilled is a first-class result and is REPORTED (fill rate matters as much
    as P&L; a maker strategy that never fills earns nothing).

No trading, no DB writes — appends data/kalshi_maker_shadow.jsonl + prints.

Usage: python -m signals.kalshi_maker_shadow [--date YYYY-MM-DD] (default: scans
from the last 24h; run it the morning after an entry window.)
"""

import argparse
import json
import math
import time
import urllib.request
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from signals.kalshi_weather_fade import (
    KALSHI_API, SERIES, STRATEGY_NO, STRATEGY_YES, kalshi_fee,  # noqa: F401
)

UA = {"User-Agent": "polyclawd-kalshi-maker-shadow/1.0"}
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCANS_PATH = PROJECT_ROOT / "data" / "kalshi_fade_scans.jsonl"
OUT_PATH = PROJECT_ROOT / "data" / "kalshi_maker_shadow.jsonl"

BET_NO, BET_YES = 100.0, 50.0
CANCEL_LOCAL = (23, 15)  # cancel resting orders before the 00Z model drop


def _jget(url, timeout=20, retries=3):
    for a in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return json.load(urllib.request.urlopen(req, timeout=timeout))
        except Exception:
            if a == retries - 1:
                return None
            time.sleep(0.8 * (a + 1))
    return None


def fetch_trades(ticker: str, start: datetime, end: datetime) -> list:
    """All public trades for ticker in [start, end] (cursor-paginated)."""
    out, cursor = [], ""
    while True:
        url = (f"{KALSHI_API}/markets/trades?ticker={ticker}&limit=1000"
               f"&min_ts={int(start.timestamp())}&max_ts={int(end.timestamp())}")
        if cursor:
            url += f"&cursor={cursor}"
        d = _jget(url)
        if not d:
            break
        out += d.get("trades", [])
        cursor = d.get("cursor") or ""
        if not cursor:
            break
        time.sleep(0.1)
    return out


def market_result(ticker: str):
    d = _jget(f"{KALSHI_API}/markets/{ticker}")
    m = (d or {}).get("market", d or {})
    r = (m.get("result") or "").strip().lower()
    return r if r in ("yes", "no") else None


def simulate_resting(cand: dict, scan_ts: datetime) -> list:
    """Two hypothetical resting orders (join / improve) for one candidate."""
    tier = cand.get("tier")
    if tier == STRATEGY_NO:
        side, budget = "NO", BET_NO
        best_bid = cand.get("no_bid")
    elif tier == STRATEGY_YES:
        side, budget = "YES", BET_YES
        best_bid = cand.get("yes_bid")
    else:
        return []
    if best_bid is None or not (0.01 <= best_bid <= 0.97):
        return []

    tz = None
    for s, (city, tz_name) in SERIES.items():
        if cand.get("series") == s:
            tz = ZoneInfo(tz_name)
            break
    if tz is None:
        return []
    local = scan_ts.astimezone(tz)
    cancel = local.replace(hour=CANCEL_LOCAL[0], minute=CANCEL_LOCAL[1],
                           second=0, microsecond=0)
    if cancel <= local:
        cancel += timedelta(days=1)

    orders = []
    for mode, level in (("join", round(best_bid, 2)),
                        ("improve", round(best_bid + 0.01, 2))):
        if level > 0.98:
            continue
        contracts = math.floor(budget / level)
        if contracts < 1:
            continue
        orders.append(dict(
            ticker=cand["ticker"], series=cand.get("series"),
            city=cand.get("city"), tier=tier, side=side, mode=mode,
            level=level, contracts=contracts,
            rest_from=scan_ts.isoformat(),
            rest_until=cancel.astimezone(timezone.utc).isoformat(),
            taker_exec=cand.get("exec_price"), mid=cand.get("mid"),
        ))
    return orders


def judge_fill(order: dict, trades: list) -> dict:
    """Strict trade-through fill judgment from the public tape."""
    side, level, mode = order["side"], order["level"], order["mode"]
    price_key = "no_price_dollars" if side == "NO" else "yes_price_dollars"
    through = at_level = 0.0
    for t in trades:
        try:
            p = float(t.get(price_key))
            c = float(t.get("count_fp") or t.get("count") or 0)
        except (TypeError, ValueError):
            continue
        if p < level:
            through += c
        elif abs(p - level) < 0.005:
            at_level += c
    if mode == "join":
        filled_qty = through  # strict: only strictly-through volume
    else:  # improve: we are best bid; at-level prints count (flag optimism)
        filled_qty = through + at_level
    filled = min(order["contracts"], int(filled_qty))
    return dict(filled_contracts=filled,
                fill_rate=round(filled / order["contracts"], 3),
                tape_through=through, tape_at_level=at_level,
                n_trades=len(trades))


def settle(order: dict, fill: dict, result: str) -> dict:
    c = fill["filled_contracts"]
    q = order["level"]
    if c == 0 or result is None:
        return dict(pnl=0.0, outcome=result or "unresolved")
    won = (result.upper() == order["side"])
    pnl = c * (1 - q) if won else -c * q  # maker fee = 0
    return dict(pnl=round(pnl, 2), outcome="won" if won else "lost")


def run(since_hours: int = 24, target_date: str = None) -> dict:
    if not SCANS_PATH.exists():
        print("no scan log yet")
        return {}
    rows = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    for line in open(SCANS_PATH):
        try:
            s = json.loads(line)
        except ValueError:
            continue
        ts = datetime.fromisoformat(s["generated_at"])
        if target_date:
            if not s["generated_at"].startswith(target_date):
                continue
        elif ts < cutoff:
            continue
        for c in s.get("candidates", []):
            if c.get("tier") and c.get("action") in ("entered", "skipped", "dry_run"):
                rows.append((ts, c))

    # dedupe per ticker (first scan wins — earliest resting order)
    seen, orders = set(), []
    for ts, c in sorted(rows, key=lambda r: r[0]):
        if c["ticker"] in seen:
            continue
        seen.add(c["ticker"])
        orders += simulate_resting(c, ts)
    print(f"candidates: {len(seen)}, resting orders simulated: {len(orders)}")

    results = []
    for o in orders:
        trades = fetch_trades(
            o["ticker"],
            datetime.fromisoformat(o["rest_from"]),
            datetime.fromisoformat(o["rest_until"]))
        time.sleep(0.1)
        fill = judge_fill(o, trades)
        res = market_result(o["ticker"]) if fill["filled_contracts"] else None
        results.append({**o, **fill, **settle(o, fill, res)})

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    with open(OUT_PATH, "a") as f:
        for r in results:
            f.write(json.dumps({"evaluated_at": stamp, **r}) + "\n")

    summary = {}
    for mode in ("join", "improve"):
        sub = [r for r in results if r["mode"] == mode]
        filled = [r for r in sub if r["filled_contracts"] > 0]
        staked = sum(r["filled_contracts"] * r["level"] for r in filled)
        pnl = sum(r["pnl"] for r in filled if r["outcome"] in ("won", "lost"))
        summary[mode] = dict(
            orders=len(sub), filled=len(filled),
            fill_rate=round(len(filled) / len(sub), 3) if sub else None,
            staked=round(staked, 2), pnl=round(pnl, 2),
            ev_per_dollar=round(pnl / staked, 4) if staked else None)
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="evaluate scans from this UTC date (YYYY-MM-DD)")
    ap.add_argument("--hours", type=int, default=24)
    args = ap.parse_args()
    run(since_hours=args.hours, target_date=args.date)

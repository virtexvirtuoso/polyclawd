#!/usr/bin/env python3
"""
Polymarket Maker Shadow — knob 7 (does the Kalshi tail edge work on PM?).

PM weather longshots carry a smaller (+1.6pt, OOS-verified) overpricing that is
provably DEAD as a taker (PM's ~7pt taker drag exceeds it) but untested as a
MAKER (makers pay no taker fee). This module collects that evidence with zero
capital, mirroring signals/kalshi_maker_shadow.py:

  - EVENING RECORDER (scheduler, in each city's local 19:30-20:30 window):
    snapshot tomorrow's PM weather brackets, classify the same tiers
    (longshot NO: mid<0.15; favorite YES: 0.50-0.70), and record hypothetical
    resting orders at join (current best bid) and improve (+1c) levels.
    Writes data/pm_maker_shadow_quotes.jsonl. No positions, no DB.
  - MORNING EVALUATOR (manual/cron): judge fills from the PUBLIC trade tape
    (data-api.polymarket.com/trades) and settle vs Gamma outcomePrices.

HONESTY RULES (same as Kalshi; the old §8 MM shadow died of optimistic fills):
  - join fills require STRICT trade-through; improve counts at-level prints
    (flagged optimistic-if-competed).
  - PM CLOB complementary matching: a resting NO bid at q can be filled by a
    taker SELLing No at <q OR a taker BUYing Yes at >(1-q) (pair minting).
    Both legs counted, strictness per mode.
  - Resting window ends 23:15 local (pre-00Z-model pick-off shield).
  - Unfilled rate is reported as prominently as P&L.

Usage:
  recorder:  called by scheduler task (no-op outside windows), or
             python -m signals.pm_maker_shadow record --force-window
  evaluator: python -m signals.pm_maker_shadow evaluate [--date YYYY-MM-DD]
"""

import argparse
import json
import logging
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
QUOTES_PATH = PROJECT_ROOT / "data" / "pm_maker_shadow_quotes.jsonl"
OUT_PATH = PROJECT_ROOT / "data" / "pm_maker_shadow.jsonl"

GAMMA = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
UA = {"User-Agent": "polyclawd-pm-maker-shadow/1.0"}

SYNOPTIC_BLACKOUT_MIN = 10  # blindspots 2026-06-10: named sniper bots (DSM Bot,
# 6-Hour Bot, OMO) pick off stale resting orders at scheduled NWS releases. The
# 6-hourly synoptic METAR times (00/06/12/18Z) fall INSIDE evening rest windows
# for some timezones. Model the order as CANCELED +/-10min around each release
# (conservative both ways: no sniper losses counted, no lucky fills either).


def in_release_blackout(ts: float) -> bool:
    """True if a unix timestamp falls within +/-SYNOPTIC_BLACKOUT_MIN minutes
    of a 6-hourly synoptic time (00/06/12/18 UTC)."""
    minutes_into_6h = (ts % 21600) / 60.0
    return minutes_into_6h <= SYNOPTIC_BLACKOUT_MIN or minutes_into_6h >= 360 - SYNOPTIC_BLACKOUT_MIN


LONGSHOT_MAX_YES = 0.15
FAVORITE_MIN, FAVORITE_MAX = 0.50, 0.70
BET_NO, BET_YES = 100.0, 50.0
WINDOW_START, WINDOW_END = (19, 30), (20, 30)
CANCEL_LOCAL = (23, 15)
MON_NAME = ["", "january", "february", "march", "april", "may", "june", "july",
            "august", "september", "october", "november", "december"]

# PM slug city -> IANA tz (same 26-city universe as the §10 audit)
PM_CITIES = {
    "nyc": "America/New_York", "philadelphia": "America/New_York",
    "miami": "America/New_York", "atlanta": "America/New_York",
    "boston": "America/New_York", "toronto": "America/New_York",
    "washington": "America/New_York",
    "chicago": "America/Chicago", "austin": "America/Chicago",
    "houston": "America/Chicago", "dallas": "America/Chicago",
    "denver": "America/Denver", "phoenix": "America/Phoenix",
    "los-angeles": "America/Los_Angeles", "seattle": "America/Los_Angeles",
    "san-francisco": "America/Los_Angeles", "san-diego": "America/Los_Angeles",
    "london": "Europe/London", "paris": "Europe/Paris", "berlin": "Europe/Berlin",
    "tokyo": "Asia/Tokyo", "seoul": "Asia/Seoul", "sydney": "Australia/Sydney",
    "wellington": "Pacific/Auckland", "sao-paulo": "America/Sao_Paulo",
    "buenos-aires": "America/Argentina/Buenos_Aires",
}


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


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def cities_in_window(now_utc: datetime, force: bool = False) -> list:
    out = []
    for city, tz_name in PM_CITIES.items():
        local = now_utc.astimezone(ZoneInfo(tz_name))
        hm = (local.hour, local.minute)
        if force or (WINDOW_START <= hm <= WINDOW_END):
            out.append((city, tz_name, local.date() + timedelta(days=1)))
    return out


def event_slug(city: str, d) -> str:
    return f"highest-temperature-in-{city}-on-{MON_NAME[d.month]}-{d.day}-{d.year}"


def classify(mid: float):
    if mid is not None and 0 < mid < LONGSHOT_MAX_YES:
        return "pm_fade_longshot_no", "NO", BET_NO
    if mid is not None and FAVORITE_MIN <= mid <= FAVORITE_MAX:
        return "pm_fade_favorite_yes", "YES", BET_YES
    return None, None, None


def record_evening(now: datetime = None, force_window: bool = False) -> dict:
    """Snapshot tomorrow's PM brackets for in-window cities; record resting
    orders. Safe to call any time — no-op outside windows."""
    now = now or datetime.now(timezone.utc)
    targets = cities_in_window(now, force=force_window)
    recorded = []
    for city, tz_name, target in targets:
        ev = _jget(f"{GAMMA}/events?slug={urllib.parse.quote(event_slug(city, target))}")
        time.sleep(0.1)
        if not ev:
            continue
        local = now.astimezone(ZoneInfo(tz_name))
        cancel = local.replace(hour=CANCEL_LOCAL[0], minute=CANCEL_LOCAL[1],
                               second=0, microsecond=0)
        if cancel <= local:
            cancel += timedelta(days=1)
        for m in ev[0].get("markets", []):
            yes_bid, yes_ask = _f(m.get("bestBid")), _f(m.get("bestAsk"))
            if yes_bid is None or yes_ask is None or yes_ask <= yes_bid:
                continue
            mid = (yes_bid + yes_ask) / 2
            tier, side, budget = classify(mid)
            if not tier:
                continue
            # resting bid level on the side we BUY:
            #   NO  -> NO-token best bid = 1 - yes_ask
            #   YES -> YES-token best bid = yes_bid
            join = round((1 - yes_ask) if side == "NO" else yes_bid, 2)
            for mode, level in (("join", join), ("improve", round(join + 0.01, 2))):
                if not (0.01 <= level <= 0.98):
                    continue
                contracts = math.floor(budget / level)
                if contracts < 1:
                    continue
                recorded.append(dict(
                    ts=now.isoformat(), city=city, tier=tier, side=side,
                    mode=mode, level=level, contracts=contracts,
                    condition_id=m.get("conditionId"),
                    question=(m.get("question") or "")[:90],
                    yes_bid=yes_bid, yes_ask=yes_ask, mid=round(mid, 4),
                    event_date=str(target),
                    rest_until=cancel.astimezone(timezone.utc).isoformat(),
                ))
    if recorded:
        QUOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(QUOTES_PATH, "a") as f:
            for r in recorded:
                f.write(json.dumps(r) + "\n")
        logger.info("pm-maker-shadow: recorded %d resting orders (%d cities)",
                    len(recorded), len(targets))
    return {"cities_in_window": len(targets), "orders_recorded": len(recorded)}


def fetch_trades(condition_id: str) -> list:
    """Public trade tape for one market (most recent first; weather brackets
    are low-volume so one page generally covers an evening)."""
    out, offset = [], 0
    while offset <= 2000:
        d = _jget(f"{DATA_API}/trades?market={condition_id}&limit=500&offset={offset}")
        if not d or not isinstance(d, list):
            break
        out += d
        if len(d) < 500:
            break
        offset += 500
        time.sleep(0.1)
    return out


def judge_fill(order: dict, trades: list) -> dict:
    """Fill from tape, honoring PM complementary matching.

    Resting BUY bid at `level` on side S is hit by:
      leg A: taker SELL of S at price < level (strict; <= for improve)
      leg B: taker BUY of the complement at price > 1-level (strict; >= improve)
    """
    side, level, mode = order["side"], order["level"], order["mode"]
    comp = "Yes" if side == "NO" else "No"
    t0 = datetime.fromisoformat(order["ts"]).timestamp()
    t1 = datetime.fromisoformat(order["rest_until"]).timestamp()
    qty = 0.0
    n_window = 0
    for t in trades:
        ts = t.get("timestamp") or 0
        if not (t0 <= ts <= t1):
            continue
        if in_release_blackout(ts):
            continue  # order modeled as canceled around scheduled NWS releases
        n_window += 1
        price, size = _f(t.get("price")), _f(t.get("size")) or 0
        outc, tside = (t.get("outcome") or ""), (t.get("side") or "").upper()
        if price is None:
            continue
        if outc.upper() == side and tside == "SELL":
            if price < level or (mode == "improve" and abs(price - level) < 0.005):
                qty += size
        elif outc == comp and tside == "BUY":
            thr = 1 - level
            if price > thr or (mode == "improve" and abs(price - thr) < 0.005):
                qty += size
    filled = min(order["contracts"], int(qty))
    return dict(filled_contracts=filled,
                fill_rate=round(filled / order["contracts"], 3),
                tape_qty=qty, trades_in_window=n_window)


def market_outcome(condition_id: str):
    """Settled YES/NO from Gamma by conditionId. None if unresolved."""
    d = _jget(f"{GAMMA}/markets?condition_ids={condition_id}")
    if not d:
        return None
    m = d[0] if isinstance(d, list) and d else None
    if not m:
        return None
    try:
        op = m.get("outcomePrices")
        op = json.loads(op) if isinstance(op, str) else op
    except (TypeError, ValueError):
        return None
    if op == ["1", "0"]:
        return "YES"
    if op == ["0", "1"]:
        return "NO"
    return None


def evaluate(target_date: str = None, since_hours: int = 24) -> dict:
    if not QUOTES_PATH.exists():
        print("no recorded quotes yet")
        return {}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    orders = []
    for line in open(QUOTES_PATH):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if target_date:
            if not o["ts"].startswith(target_date):
                continue
        elif datetime.fromisoformat(o["ts"]) < cutoff:
            continue
        orders.append(o)
    print(f"resting orders to evaluate: {len(orders)}")

    tape_cache, outcome_cache, results = {}, {}, []
    for o in orders:
        cid = o["condition_id"]
        if cid not in tape_cache:
            tape_cache[cid] = fetch_trades(cid)
            time.sleep(0.1)
        fill = judge_fill(o, tape_cache[cid])
        outcome = None
        if fill["filled_contracts"]:
            if cid not in outcome_cache:
                outcome_cache[cid] = market_outcome(cid)
                time.sleep(0.1)
            outcome = outcome_cache[cid]
        c, q = fill["filled_contracts"], o["level"]
        if c and outcome:
            won = outcome == o["side"]
            pnl, res = (c * (1 - q) if won else -c * q), ("won" if won else "lost")
        else:
            pnl, res = 0.0, ("unresolved" if c else "unfilled")
        results.append({**o, **fill, "outcome": res, "pnl": round(pnl, 2)})

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
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["record", "evaluate"])
    ap.add_argument("--force-window", action="store_true")
    ap.add_argument("--date")
    ap.add_argument("--hours", type=int, default=24)
    a = ap.parse_args()
    if a.cmd == "record":
        print(json.dumps(record_evening(force_window=a.force_window), indent=2))
    else:
        # Default to yesterday (settled) if --date not specified
        from datetime import timezone as _tz, timedelta as _td
        date = a.date or (datetime.now(_tz.utc) - _td(days=1)).strftime('%Y-%m-%d')
        evaluate(target_date=date, since_hours=a.hours)

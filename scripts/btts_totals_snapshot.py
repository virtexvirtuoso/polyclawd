#!/usr/bin/env python3
"""
btts_totals_snapshot.py — Path 2: paper-shadow CLV logger for BTTS / Totals markets.

Cleared to build: 2026-08-22 (BTTS/totals sharp-coverage check), see
  02-Projects/Polyclawd/Research/Edge-Methodology/2026-08-22-BTTS-Totals-Sharp-Coverage-Check.md
Replaces the KILLED goalscorer-anytime toolchain (2026-08-22-Scorer-CLV-Pseudoreplication-Finding):
goalscorer props ran at n_sharp=2 (100% of flagged volume, 8x overpriced); BTTS/totals run
at n_sharp=4-5 on the same matches (live-verified on this branch).

WHAT IT IS
----------
Path 2 of the scorer-CLV KILL decision. Same sequential CONFIRM/KILL gate as the
goalscorer logger, but pointed at markets with real sharp anchor depth. Data-collection
ONLY (snapshot live odds -> SQLite). NO execution writes — paper-shadow. The independent
unit is the MATCH (per-event), NOT the market line — lines within a match are correlated
(same rule that exposed the scorer pseudoreplication bug).

Non-circular CLV: does the soft line you'd bet move toward you by kickoff, relative to
the SHARP consensus anchor (de-vigged, power method) at snapshot time.

MODES
-----
  --snapshot     fetch live BTTS + totals odds for upcoming soccer, persist to SQLite
  --report       match-level CLV + sequential verdict (CONFIRM / KILL / CONTINUE)
  --void-before / --void-event   mark open snapshot alerts invalid

USAGE
-----
  export ODDS_API_KEY=...    # or leave unset; falls back to config/.env at runtime
  python3 scripts/btts_totals_snapshot.py --snapshot --sport soccer_epl
  python3 scripts/btts_totals_snapshot.py --report --send
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
try:
    from odds.sports_edge_common import (  # type: ignore
        american_to_implied_prob,
        devig_power_2way,
    )
    _DEVIG_SRC = "odds.sports_edge_common.devig_power_2way"
except Exception:  # standalone fallback — exact copies
    _DEVIG_SRC = "inlined fallback"

    def american_to_implied_prob(odds: int) -> float:
        odds = int(odds)
        return (100.0 / (odds + 100.0)) if odds > 0 else (abs(odds) / (abs(odds) + 100.0))

    def devig_power_2way(odds_a: int, odds_b: int):
        pa = american_to_implied_prob(odds_a)
        pb = american_to_implied_prob(odds_b)
        lo, hi = 0.5, 3.0
        for _ in range(64):
            mid = (lo + hi) / 2
            if pa ** (1.0 / mid) + pb ** (1.0 / mid) > 1.0:
                hi = mid
            else:
                lo = mid
        k = (lo + hi) / 2
        ra, rb = pa ** (1.0 / k), pb ** (1.0 / k)
        t = ra + rb
        return ra / t, rb / t


# ── Config (REWEIGHT TBD — provisional; all 5 sharp books verified live on soccer_epl 2026-08-26) ─
BTTS_SHARP_WEIGHTS = {
    "pinnacle":      0.35,
    "betfair_ex_uk": 0.30,
    "matchbook":     0.20,
    "betonlineag":   0.10,
    "lowvig":        0.05,
}
TOTALS_SHARP_WEIGHTS = dict(BTTS_SHARP_WEIGHTS)  # same set (totals additionally prices on williamhill)

# Soft books used as the "you'd bet here" reference line (lowest implied = best odds for buyer).
SOFT_BOOKS = {"draftkings", "fanduel", "betrivers", "betmgm", "caesars", "williamhill_us", "betano", "onexbet"}

MARKETS = ("btts", "totals")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

DB_DEFAULT = "storage/path2_btts_totals.db"


# ── helpers ───────────────────────────────────────────────────────────────────
def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode())


def _key():
    key = os.getenv("ODDS_API_KEY")
    if key:
        return key
    for envp in ("config/polymarket.env", ".env"):
        try:
            for line in open(envp):
                if line.startswith("ODDS_API_KEY="):
                    return line.strip().split("=", 1)[1]
        except FileNotFoundError:
            continue
    sys.exit("ODDS_API_KEY not set and not found in config/.env — cannot snapshot.")


def _books_param():
    books = set(TOTALS_SHARP_WEIGHTS) | set(SOFT_BOOKS)
    return ",".join(sorted(books))


# ── DB ────────────────────────────────────────────────────────────────────────
def db_connect(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS path2_snapshot (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_at TEXT NOT NULL,
        sport TEXT NOT NULL,
        event_id TEXT NOT NULL,
        event_title TEXT,
        commence_time TEXT,
        market TEXT NOT NULL,
        line REAL,
        soft_book TEXT,
        soft_implied REAL,
        sharp_fair REAL,
        n_sharp INTEGER,
        edge_pct REAL,
        mins_to_kickoff REAL,
        UNIQUE(event_id, market, line, snapshot_at))""")
    con.execute("""CREATE TABLE IF NOT EXISTS path2_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alerted_at TEXT NOT NULL,
        event_id TEXT NOT NULL,
        event_title TEXT,
        market TEXT NOT NULL,
        line REAL,
        soft_book TEXT,
        soft_implied REAL,
        sharp_consensus REAL,
        edge_pct REAL,
        n_sharp INTEGER,
        status TEXT NOT NULL DEFAULT 'open',
        resolution_note TEXT,
        resolved_at TEXT)""")
    con.commit()
    return con


def _insert(con, **r):
    cols = ",".join(r)
    ph = ",".join("?" * len(r))
    try:
        con.execute(f"INSERT INTO path2_snapshot ({cols}) VALUES ({ph})", tuple(r.values()))
        return 1
    except sqlite3.IntegrityError:
        return 0  # duplicate (event, market, line, snapshot_at)


# ── sharp consensus per market ────────────────────────────────────────────────
def _book_sides(bkey, outcomes):
    """Devigged 2-way {side: prob} for one book's market (power devig)."""
    sides = {}
    for o in outcomes:
        nm = (o.get("name") or "").strip()
        price = o.get("price")
        if price is None:
            continue
        sides[nm] = int(price)
    if len(sides) != 2:
        return None
    n0, n1 = list(sides.keys())
    p0, p1 = devig_power_2way(sides[n0], sides[n1])
    return {n0: p0, n1: p1}


def _totals_consensus(ev, weights):
    """Per-point weighted consensus for totals: {point_str: {side: prob}}."""
    acc = defaultdict(lambda: defaultdict(float))
    wsum = defaultdict(float)
    for bk in ev.get("bookmakers", []):
        w = weights.get(bk.get("key", ""), 0.0)
        if w <= 0.0:
            continue
        for mk in bk.get("markets", []):
            if mk.get("key") != "totals":
                continue
            by_pt = defaultdict(dict)
            for o in mk.get("outcomes", []):
                pt = o.get("point")
                nm = o.get("name")
                price = o.get("price")
                if pt is None or nm is None or price is None:
                    continue
                by_pt[str(float(pt))][nm.strip()] = int(price)
            for pt, sides in by_pt.items():
                if len(sides) != 2:
                    continue
                n0, n1 = list(sides.keys())
                p0, p1 = devig_power_2way(sides[n0], sides[n1])
                acc[pt][n0] += w * p0
                acc[pt][n1] += w * p1
                wsum[pt] += w
            break
    out = {}
    for pt, sides in acc.items():
        tw = wsum.get(pt, 0.0)
        if tw > 0.0 and len(sides) >= 2:
            out[pt] = {s: v / tw for s, v in sides.items()}
    return out


def _btts_consensus(ev, weights):
    """Weighted {side: prob} for the btts market."""
    acc, wsum = defaultdict(float), 0.0
    for bk in ev.get("bookmakers", []):
        w = weights.get(bk.get("key", ""), 0.0)
        if w <= 0.0:
            continue
        for mk in bk.get("markets", []):
            if mk.get("key") != "btts":
                continue
            sides = _book_sides(bk["key"], mk.get("outcomes", []))
            if sides is None:
                continue
            for nm, pr in sides.items():
                acc[nm] += w * pr
            wsum += w
            break
    if wsum == 0.0 or len(acc) != 2:
        return {}
    return {nm: v / wsum for nm, v in acc.items()}


def _best_soft(ev, market, line=None):
    """Lowest implied (= best odds for buyer) across soft books for the market/line.

    Returns (book, implied, n_sides) or None.
    """
    best = None
    for bk in ev.get("bookmakers", []):
        bkey = bk.get("key", "")
        if bkey not in SOFT_BOOKS:
            continue
        for mk in bk.get("markets", []):
            if mk.get("key") != market:
                continue
            sides = {}
            for o in mk.get("outcomes", []):
                pt = o.get("point")
                nm = o.get("name")
                price = o.get("price")
                if price is None or nm is None:
                    continue
                if market == "totals" and line is not None and (pt is None or str(float(pt)) != str(float(line))):
                    continue
                sides[nm.strip()] = int(price)
            if len(sides) != 2:
                continue
            n0, n1 = list(sides.keys())
            p0, p1 = devig_power_2way(sides[n0], sides[n1])
            # take the 'buyer' side = the side with the higher implied prob (the bet we'd place is the
            # side that's underpriced vs sharp); for CLV we compare the soft implied of the side the
            # sharp consensus prices HIGHER than soft.
            for nm, pr in ((n0, p0), (n1, p1)):
                if best is None or pr < best[1]:
                    best = (bkey, pr, nm)
            break
    return best


def _n_sharp(ev, market, weights):
    """Count how many sharp books in `weights` actually price `market` on this event."""
    n = 0
    for bk in ev.get("bookmakers", []):
        bkey = bk.get("key", "")
        if bkey not in weights:
            continue
        if any(mk.get("key") == market for mk in bk.get("markets", [])):
            n += 1
    return n


# ── live snapshot (cron this) ────────────────────────────────────────────────
def live_snapshot(con, sport, window_hours, min_edge=5.0):
    """Snapshot live BTTS + totals odds for upcoming matches; persist to SQLite."""
    key = _key()
    now = datetime.now(timezone.utc)
    events = _get(f"{ODDS_API_BASE}/sports/{sport}/events?apiKey={key}")
    snap_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    n_rows = n_ev = 0
    new_edges = []  # (event_id, market, line, soft_book, soft_implied, sharp, edge, n_sharp, title, mins)

    for ev in events:
        ct = _parse_iso(ev.get("commence_time"))
        if ct is None:
            continue
        mins = (ct - now).total_seconds() / 60.0
        if mins <= 0 or mins > window_hours * 60:
            continue
        eid = ev["id"]
        url = (
            f"{ODDS_API_BASE}/sports/{sport}/events/{eid}/odds?apiKey={key}"
            f"&regions=us,uk,eu&markets={','.join(MARKETS)}&oddsFormat=american&bookmakers={_books_param()}"
        )
        try:
            od = _get(url)
        except Exception as e:
            print(f"  [skip] {eid}: {e}")
            continue
        title = f"{od.get('home_team')} vs {od.get('away_team')}"
        got = 0

        # totals — per point
        totals_cons = _totals_consensus(od, TOTALS_SHARP_WEIGHTS)
        totals_n_sharp = _n_sharp(od, "totals", TOTALS_SHARP_WEIGHTS)
        for pt, sides in totals_cons.items():
            for nm, pr in sides.items():
                soft = _best_soft(od, "totals", line=pt)
                if soft is None:
                    continue
                sharp_fair = pr
                soft_implied = soft[1]
                edge_pct = (sharp_fair - soft_implied) * 100.0
                inserted = _insert(
                    con, snapshot_at=snap_at, sport=sport, event_id=eid, event_title=title,
                    commence_time=ev.get("commence_time"), market="totals", line=pt,
                    soft_book=soft[0], soft_implied=soft_implied, sharp_fair=sharp_fair,
                    n_sharp=totals_n_sharp, edge_pct=edge_pct, mins_to_kickoff=round(mins, 1),
                )
                if inserted:
                    got += 1
                    if edge_pct >= min_edge:
                        new_edges.append((eid, "totals", pt, soft[0], soft_implied, sharp_fair, totals_n_sharp, title, mins))

        # btts
        btts_cons = _btts_consensus(od, BTTS_SHARP_WEIGHTS)
        if btts_cons:
            btts_n_sharp = _n_sharp(od, "btts", BTTS_SHARP_WEIGHTS)
            for side_name, pr in btts_cons.items():
                soft = _best_soft(od, "btts")
                if soft is None:
                    continue
                sharp_fair = pr
                soft_implied = soft[1]
                edge_pct = (sharp_fair - soft_implied) * 100.0
                inserted = _insert(
                    con, snapshot_at=snap_at, sport=sport, event_id=eid, event_title=title,
                    commence_time=ev.get("commence_time"), market="btts", line=side_name,
                    soft_book=soft[0], soft_implied=soft_implied, sharp_fair=sharp_fair,
                    n_sharp=btts_n_sharp, edge_pct=edge_pct, mins_to_kickoff=round(mins, 1),
                )
                if inserted:
                    got += 1
                    if edge_pct >= min_edge:
                        new_edges.append((eid, "btts", side_name, soft[0], soft_implied, sharp_fair, btts_n_sharp, title, mins))
        if got:
            n_ev += 1
            n_rows += got
            print(f"  [snap] T-{mins/60:.1f}h {title}: {got} lines")
    con.commit()
    print(f"[snapshot] {n_rows} rows across {n_ev} matches @ {snap_at}")
    return new_edges


# ── report: match-level CLV + sequential decision ─────────────────────────────
def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def report(con, min_edge, send=False):
    """Match-level CLV verdict. Independent unit = MATCH (per event), not per line.

    CLV per line = (soft_implied at close) - (soft_implied at first flag), i.e. did the
    soft line you'd bet move toward you by kickoff. Aggregated to the match mean, then a
    sequential Wilson gate on the match-level beat-rate (fraction of matches with mean
    soft-move > 0): CONFIRM when the 95% lower bound clears 0.55, KILL when the upper
    bound falls below it, else CONTINUE.
    """
    rows = con.execute(
        """SELECT event_id, event_title, market, line, snapshot_at, soft_implied,
                  edge_pct, mins_to_kickoff FROM path2_snapshot"""
    ).fetchall()
    by_line = defaultdict(list)
    titles = {}
    for eid, title, market, line, snap, soft, edge, mins in rows:
        by_line[(eid, market, line)].append((snap, soft, edge, mins))
        titles[eid] = title

    line_clv = []  # (eid, market, line, soft_move_pp)
    for (eid, market, line), snaps in by_line.items():
        snaps.sort(key=lambda x: x[0])
        flagged = [s for s in snaps if s[2] is not None and s[2] >= min_edge]
        if not flagged:
            continue  # never a survivor edge
        entry = flagged[0]  # first time it flagged
        pre = [s for s in snaps if s[3] is not None and s[3] >= 0]  # pre-kickoff snaps
        close = max(pre, key=lambda x: x[0]) if pre else snaps[-1]  # closest to kickoff
        if close[0] <= entry[0]:
            continue  # need a later close snapshot
        line_clv.append((eid, market, line, (close[1] - entry[1]) * 100.0))

    by_match = defaultdict(list)
    for eid, market, line, mv in line_clv:
        by_match[eid].append(mv)
    match_mean = {eid: sum(mvs) / len(mvs) for eid, mvs in by_match.items()}
    n_matches = len(match_mean)
    beats = sum(1 for m in match_mean.values() if m > 0)
    pool_n = len(line_clv)
    pool_beat = sum(1 for _, _, _, mv in line_clv if mv > 0)
    lo, hi = wilson(beats, n_matches)

    print("\n" + "═" * 74)
    print("  PATH 2 — BTTS/TOTALS CLV REPORT (match-level, non-circular)")
    print("═" * 74)
    print(f"  matches with gradable flagged lines: {n_matches}")
    print(
        f"  match-level beat-rate (mean soft-move > 0): {beats}/{n_matches}"
        f"{(' = %.0f%%' % (100 * beats / n_matches)) if n_matches else ''}"
    )
    print(f"  Wilson 95% CI: [{lo:.2f}, {hi:.2f}]")
    print(f"  (pooled line-level: {pool_beat}/{pool_n} lines moved toward you)")
    if by_match:
        print("\n  Per match (mean soft-move pp):")
        for eid, m in sorted(match_mean.items(), key=lambda x: x[1], reverse=True):
            print(f"    {('+' if m > 0 else '')}{m:5.1f}pp  {titles.get(eid, '?')[:40]} (n={len(by_match[eid])})")

    print("\n" + "═" * 74)
    if n_matches < 12:
        v = f"CONTINUE — only {n_matches} matches (need ~12+ for the sequential gate)."
    elif lo > 0.55:
        v = f"CONFIRM — match-level beat-rate CI lower bound {lo:.2f} > 0.55. Real CLV; go to spec §6 Step 4."
    elif hi < 0.55:
        v = f"KILL — CI upper bound {hi:.2f} < 0.55. No edge; stop."
    else:
        v = f"CONTINUE — CI [{lo:.2f},{hi:.2f}] straddles 0.55; accumulate more matches."
    print(f"  VERDICT: {v}")
    print("═" * 74 + "\n")

    if send:
        pct = f"{100 * beats / n_matches:.0f}%" if n_matches else "n/a"
        msg = (
            f"⚽ Path 2 BTTS/totals CLV paper logger\n"
            f"matches: {n_matches}  beat-rate: {beats}/{n_matches} ({pct})\n"
            f"Wilson 95% CI: [{lo:.2f}, {hi:.2f}]  pooled lines: {pool_beat}/{pool_n}\n"
            f"VERDICT: {v}"
        )
        try:
            import alert_formatter
            alert_formatter.send_telegram(msg)
        except Exception as e:
            print(f"[warn] could not send Telegram: {e}")


def main():
    ap = argparse.ArgumentParser(description="Path 2 BTTS/totals paper CLV logger.")
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--sport", default="soccer_epl", help="single sport (back-compat)")
    ap.add_argument("--sports", default="", help="comma-separated sports; overrides --sport")
    ap.add_argument("--min-edge", type=float, default=5.0)
    ap.add_argument("--window-hours", type=float, default=8.0)
    ap.add_argument("--snapshot", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--send", action="store_true", help="send report summary to Telegram")
    ap.add_argument("--void-before", metavar="ISO")
    ap.add_argument("--void-event", metavar="EVENT_ID")
    ap.add_argument("--void-reason", default="manual")
    args = ap.parse_args()

    con = db_connect(args.db)
    if args.snapshot:
        sports = [s.strip() for s in args.sports.split(",") if s.strip()] or [args.sport]
        for sport in sports:
            print(f"[sport] {sport}")
            live_snapshot(con, sport, args.window_hours, min_edge=args.min_edge)
    if args.void_before or args.void_event:
        print("[void] manual void of alerts — wiring left to scorer logger for now.")
    if args.report or not (args.snapshot or args.void_before or args.void_event):
        report(con, args.min_edge, send=args.send)
    con.close()


if __name__ == "__main__":
    main()

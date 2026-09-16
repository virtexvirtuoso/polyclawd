#!/usr/bin/env python3
"""
scorer_paper_logger.py — paper-shadow CLV logger for goalscorer props.

Spec: 02-Projects/Polyclawd/Development/prop-edge-system-spec.md §6 Step 1.

WHAT IT IS
----------
The data-collection mechanism, built BEFORE the go/no-go gate (the logger is how
you get the data). It snapshots live `player_goal_scorer_anytime` props for
upcoming soccer matches, persists them to SQLite, and at report time computes the
NON-CIRCULAR CLV metric (does the soft line you'd bet move toward you by kickoff)
aggregated to the MATCH level — the independent unit, since props within a match
are correlated.

The decision is SEQUENTIAL, not a fixed N: CONFIRM when the Wilson 95% lower bound
of the match-level beat-rate clears 0.55; KILL when the upper bound falls below it;
else keep accumulating. A large effect (the 10-match pilot showed ~84%) resolves in
~15-25 matches.

MODES
-----
  --seed-historical --entry-dir D --close-dir D   ingest the 10 pilot WC matches
  --snapshot                                       fetch live props now (cron this)
  --report                                         match-level CLV + sequential verdict

  python3 scripts/scorer_paper_logger.py --db storage/scorer_clv.db --seed-historical \
      --entry-dir /tmp/phase0_data --close-dir /tmp/phase0_close
  python3 scripts/scorer_paper_logger.py --db storage/scorer_clv.db --snapshot   # cron ~/30min
  python3 scripts/scorer_paper_logger.py --db storage/scorer_clv.db --report
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import sqlite3
import sys
import unicodedata
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import phase0_prop_falsification as P  # noqa: E402  (consensus anchor, parsing, fetch helpers)
import alert_formatter

SCORER_MARKET = "player_goal_scorer_anytime"

# [OQ2 / spec §3.2] De-bias the Betfair-weighted consensus YES with a flat haircut.
# The raw consensus carries a ~+1.2pp positive bias (it never removes the YES-side
# vig); goalscorer vig is empirically ~4.4% and FLAT across buckets (n=48 Pinnacle
# two-way), so a flat factor de-vigs as well as Shin without a NO side. PROVISIONAL —
# refit on resolution data. Applies to the fair-value anchor ONLY; soft price stays raw.
GOALSCORER_YES_HAIRCUT = 0.958


# ── helpers ───────────────────────────────────────────────────────────────────
def canon(name: str) -> str:
    name = unicodedata.normalize("NFKD", name or "")
    name = "".join(c for c in name if not unicodedata.combining(c)).lower().strip()
    name = re.sub(r"[^\w\s]", "", name)
    parts = name.split()
    return parts[1] if len(parts) == 2 and len(parts[0]) == 1 else name


def best_soft_raw(books: dict):
    best = None
    for bkey, sides in books.items():
        if bkey not in P.SOFT_BOOKS:
            continue
        y = sides.get("yes")
        if y is None:
            continue
        if best is None or y < best[1]:
            best = (bkey, y)
    return best


def scorer_props(ev: dict):
    """Yield (player_canon, player_raw, consensus_fair, soft_book, soft_implied, n_sharp)."""
    if isinstance(ev, dict) and "data" in ev and "bookmakers" not in ev:
        ev = ev["data"]
    _, bd = P.parse_event(ev, "auto")
    for (player, mkt), books in bd.items():
        if mkt != SCORER_MARKET:
            continue
        cons = P.new_sharp_yes(books)
        soft = best_soft_raw(books)
        if cons is None or soft is None:
            continue
        cons *= GOALSCORER_YES_HAIRCUT  # [OQ2] de-bias fair-value anchor (soft stays raw)
        n_sharp = sum(1 for b in P.SOCCER_PROP_SHARP_WEIGHTS if b in books)
        yield canon(player), player, cons, soft[0], soft[1], n_sharp


def db_connect(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS scorer_snapshot (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_at TEXT NOT NULL, source TEXT NOT NULL,
        event_id TEXT NOT NULL, event_title TEXT, commence_time TEXT,
        player TEXT NOT NULL, player_raw TEXT,
        consensus_fair REAL, best_soft_book TEXT, best_soft_implied REAL,
        edge_pct REAL, n_sharp INTEGER, mins_to_kickoff REAL,
        UNIQUE(event_id, player, snapshot_at))""")
    con.execute("""CREATE TABLE IF NOT EXISTS scorer_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alerted_at TEXT NOT NULL,
        event_id TEXT NOT NULL,
        event_title TEXT,
        player TEXT NOT NULL,
        player_raw TEXT,
        soft_book TEXT,
        book_odds_american TEXT,
        edge_pp REAL,
        consensus_fair REAL,
        soft_implied REAL,
        stake REAL,
        n_legs INTEGER,
        status TEXT NOT NULL DEFAULT 'open',
        resolution_note TEXT,
        resolved_at TEXT)""")
    con.commit()
    return con


def _insert(con, **r):
    cols = ",".join(r)
    ph = ",".join("?" * len(r))
    try:
        con.execute(f"INSERT INTO scorer_snapshot ({cols}) VALUES ({ph})", tuple(r.values()))
        return 1
    except sqlite3.IntegrityError:
        return 0  # duplicate (event,player,snapshot_at)


# ── seed from the 10 pilot historical snapshots ───────────────────────────────
def seed_historical(con, entry_dir, close_dir):
    total = 0
    for src, d, mins in [("seed-entry", entry_dir, 240.0), ("seed-close", close_dir, 5.0)]:
        for fp in sorted(glob.glob(os.path.join(d, "*.json"))):
            ev = json.load(open(fp))
            if isinstance(ev, dict) and "data" in ev and "bookmakers" not in ev:
                ev = ev["data"]
            eid = ev.get("id") or os.path.splitext(os.path.basename(fp))[0]
            ct = ev.get("commence_time")
            title = f"{ev.get('home_team')} vs {ev.get('away_team')}"
            ctd = P._parse_iso(ct)
            snap_at = (ctd - timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%SZ") if ctd else src
            for pc, praw, cons, sb, si, ns in scorer_props(ev):
                total += _insert(
                    con,
                    snapshot_at=snap_at,
                    source=src,
                    event_id=eid,
                    event_title=title,
                    commence_time=ct,
                    player=pc,
                    player_raw=praw,
                    consensus_fair=cons,
                    best_soft_book=sb,
                    best_soft_implied=si,
                    edge_pct=(cons - si) * 100.0,
                    n_sharp=ns,
                    mins_to_kickoff=mins,
                )
    con.commit()
    print(f"[seed] inserted {total} scorer snapshot rows from pilot matches")


# ── live snapshot (cron this) ─────────────────────────────────────────────────
def live_snapshot(con, sport, window_hours, min_edge=5.0, bankroll=10_000.0):
    """Snapshot live props; fire Step-4 sizing alerts for newly-detected edges.

    Uses send_telegram from scripts.alert_formatter (already imported above).
    Fires exactly once per (event_id, player) crossing min_edge for the first time.
    Sizing: half-Kelly × 1/√n match-cluster haircut, capped at $200 and 3% bankroll.
    """
    key = os.getenv("ODDS_API_KEY")
    if not key:
        sys.exit("ODDS_API_KEY not set — cannot snapshot live.")
    now = datetime.now(timezone.utc)
    books = ",".join(list(P.SOCCER_PROP_SHARP_WEIGHTS) + sorted(P.SOFT_BOOKS))
    events = P._get(f"{P.ODDS_API_BASE}/sports/{sport}/events?apiKey={key}")
    snap_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    n_rows = n_ev = 0

    # Accumulate new edge detections per match before alerting (need n for haircut).
    new_edges: dict = {}

    for ev in events:
        ct = P._parse_iso(ev.get("commence_time"))
        if ct is None:
            continue
        mins = (ct - now).total_seconds() / 60.0
        if mins <= 0 or mins > window_hours * 60:  # skip in-play & far-future
            continue
        eid = ev["id"]
        url = (
            f"{P.ODDS_API_BASE}/sports/{sport}/events/{eid}/odds?apiKey={key}"
            f"&regions=us,uk,eu&markets={SCORER_MARKET}&oddsFormat=american&bookmakers={books}"
        )
        try:
            od = P._get(url)
        except Exception as e:
            print(f"  [skip] {eid}: {e}")
            continue
        title = f"{od.get('home_team')} vs {od.get('away_team')}"
        got = 0
        for pc, praw, cons, sb, si, ns in scorer_props(od):
            edge_pct = (cons - si) * 100.0
            inserted = _insert(
                con,
                snapshot_at=snap_at,
                source="live",
                event_id=eid,
                event_title=title,
                commence_time=ev.get("commence_time"),
                player=pc,
                player_raw=praw,
                consensus_fair=cons,
                best_soft_book=sb,
                best_soft_implied=si,
                edge_pct=edge_pct,
                n_sharp=ns,
                mins_to_kickoff=round(mins, 1),
            )
            if inserted:
                got += 1
                _betfair_sanity = (cons < 3.0 * si) if si > 0 else False
                _min_implied_ok = si >= 0.10
                if edge_pct >= min_edge and _min_implied_ok and _betfair_sanity:
                    prior = con.execute(
                        "SELECT COUNT(*) FROM scorer_snapshot "
                        "WHERE event_id=? AND player=? AND edge_pct>=? AND snapshot_at<?",
                        (eid, pc, min_edge, snap_at),
                    ).fetchone()[0]
                    if prior == 0:
                        if eid not in new_edges:
                            new_edges[eid] = []
                        new_edges[eid].append((praw, cons, sb, si, ns, title, mins))
        if got:
            n_ev += 1
            n_rows += got
            print(f"  [snap] T-{mins / 60:.1f}h {title}: {got} scorer props")
    con.commit()
    print(f"[snapshot] {n_rows} rows across {n_ev} matches @ {snap_at}")

    # ── Step 4: sizing alerts ─────────────────────────────────────────────────
    for eid, candidates in new_edges.items():
        n = len(candidates)
        haircut = 1.0 / math.sqrt(n)
        for praw, cons, sb, si, ns, title, mins in candidates:
            b = (1.0 / si) - 1.0
            p, q = cons, 1.0 - cons
            f_half = max(0.0, (b * p - q) / b * 0.5 * haircut)
            stake = max(0.0, round(min(f_half * bankroll, 200.0, 0.03 * bankroll)))
            edge_pp = (cons - si) * 100.0
            if si <= 0 or si >= 1:
                amer_str = "n/a"
            elif si < 0.5:
                amer_str = f"+{round(100 * (1 / si - 1))}"
            else:
                amer_str = str(round(-100 * si / (1 - si)))
            cap_label = " [book cap]" if stake == 200.0 else (" [match cap]" if stake == 0.03 * bankroll else "")
            alert = "\n".join([
                f"\u26bd [PAPER] {praw} anytime scorer",
                title,
                f"{sb} {amer_str} | Edge +{edge_pp:.1f}pp | T-{mins / 60:.1f}h",
                f"Stake: ${stake:.0f}{cap_label} (half-Kelly, {n}-leg cluster, {haircut:.2f}x haircut)",
                f"Fair: {cons * 100:.1f}% | Sharp books: {ns}",
            ])
            print(f"[alert] {praw} edge={edge_pp:.1f}pp stake=${stake:.0f} n={n}")
            alert_formatter.send_telegram(alert)
            con.execute(
                """INSERT INTO scorer_alerts
                   (alerted_at, event_id, event_title, player, player_raw,
                    soft_book, book_odds_american, edge_pp, consensus_fair,
                    soft_implied, stake, n_legs)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (snap_at, eid, title, canon(praw), praw,
                 sb, amer_str, edge_pp, cons, si, stake, n),
            )
            con.commit()


# ── alert ledger management ───────────────────────────────────────────────────
def void_alerts(con, event_id=None, before=None, reason="manual"):
    """Mark scorer_alerts rows as invalid. Filters by event_id and/or alerted_at < before."""
    where = ["status='open'"]
    vals: list = [reason, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")]
    if event_id:
        where.append("event_id=?")
        vals.append(event_id)
    if before:
        where.append("alerted_at < ?")
        vals.append(before)
    clause = " AND ".join(where)
    n = con.execute(
        f"UPDATE scorer_alerts SET status='invalid', resolution_note=?, resolved_at=? WHERE {clause}",
        tuple(vals),
    ).rowcount
    con.commit()
    print(f"[void] {n} alerts marked invalid: {reason}")
    return n


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
    rows = con.execute("""SELECT event_id, event_title, player, snapshot_at, best_soft_implied,
                                 edge_pct, mins_to_kickoff FROM scorer_snapshot""").fetchall()
    by_prop = defaultdict(list)
    titles = {}
    for eid, title, player, snap, soft, edge, mins in rows:
        by_prop[(eid, player)].append((snap, soft, edge, mins))
        titles[eid] = title

    prop_clv = []  # (eid, player, soft_move_pp)
    for (eid, player), snaps in by_prop.items():
        snaps.sort(key=lambda x: x[0])  # by snapshot_at
        flagged = [s for s in snaps if s[2] is not None and s[2] >= min_edge]
        if not flagged:
            continue  # never a survivor edge
        entry = flagged[0]  # first time it flagged
        pre = [s for s in snaps if s[3] is not None and s[3] >= 0]  # pre-kickoff snaps
        close = max(pre, key=lambda x: x[0]) if pre else snaps[-1]  # closest to kickoff
        if close[0] <= entry[0]:
            continue  # need a later close snapshot
        prop_clv.append((eid, player, (close[1] - entry[1]) * 100.0))

    by_match = defaultdict(list)
    for eid, player, mv in prop_clv:
        by_match[eid].append(mv)
    match_mean = {eid: sum(mvs) / len(mvs) for eid, mvs in by_match.items()}
    n_matches = len(match_mean)
    beats = sum(1 for m in match_mean.values() if m > 0)
    pool_n = len(prop_clv)
    pool_beat = sum(1 for _, _, mv in prop_clv if mv > 0)
    lo, hi = wilson(beats, n_matches)

    print("\n" + "═" * 74)
    print("  SCORER PAPER LOGGER — CLV REPORT (match-level, non-circular)")
    print("═" * 74)
    print(f"  matches with gradable flagged props: {n_matches}")
    print(
        f"  match-level beat-rate (mean soft-move > 0): {beats}/{n_matches}"
        f"{(' = %.0f%%' % (100 * beats / n_matches)) if n_matches else ''}"
    )
    print(f"  Wilson 95% CI: [{lo:.2f}, {hi:.2f}]")
    print(f"  (pooled prop-level: {pool_beat}/{pool_n} props moved toward you)")
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
            f"⚽ Scorer CLV paper logger\n"
            f"matches: {n_matches}  beat-rate: {beats}/{n_matches} ({pct})\n"
            f"Wilson 95% CI: [{lo:.2f}, {hi:.2f}]  pooled props: {pool_beat}/{pool_n}\n"
            f"VERDICT: {v}"
        )
        alert_formatter.send_telegram(msg)


def main():
    ap = argparse.ArgumentParser(description="Goalscorer paper CLV logger (spec §6 Step 1).")
    ap.add_argument("--db", default="storage/scorer_clv.db")
    ap.add_argument("--sport", default="soccer_fifa_world_cup")
    ap.add_argument("--min-edge", type=float, default=5.0)
    ap.add_argument("--window-hours", type=float, default=8.0, help="live snapshot kickoff window")
    ap.add_argument("--seed-historical", action="store_true")
    ap.add_argument("--entry-dir", default="/tmp/phase0_data")
    ap.add_argument("--close-dir", default="/tmp/phase0_close")
    ap.add_argument("--snapshot", action="store_true")
    ap.add_argument("--bankroll", type=float, default=10_000.0, help="bankroll in USD for Step-4 sizing")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--send", action="store_true", help="send report summary to Telegram")
    ap.add_argument("--void-before", metavar="ISO", help="Mark all open alerts before this timestamp as invalid")
    ap.add_argument("--void-event", metavar="EVENT_ID", help="Mark all open alerts for this event_id as invalid")
    ap.add_argument("--void-reason", default="manual", help="Reason string for voided alerts")
    args = ap.parse_args()

    con = db_connect(args.db)
    if args.seed_historical:
        seed_historical(con, args.entry_dir, args.close_dir)
    if args.snapshot:
        live_snapshot(con, args.sport, args.window_hours, min_edge=args.min_edge, bankroll=args.bankroll)
    if args.void_before or args.void_event:
        void_alerts(con, event_id=args.void_event, before=args.void_before, reason=args.void_reason)
    if args.report or not (args.seed_historical or args.snapshot or args.void_before or args.void_event):
        report(con, args.min_edge, send=args.send)
    con.close()


if __name__ == "__main__":
    main()

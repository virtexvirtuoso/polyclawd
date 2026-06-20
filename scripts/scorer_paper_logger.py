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

The decision is SEQUENTIAL and NOW CONTROL-CORRECTED (2026-06-18). The old gate
compared the selected match-level beat-rate to a 50% coin flip and CONFIRMed when
the Wilson lower bound cleared 0.55. That null was wrong: soft goalscorer lines
drift up before kickoff for ~63% of ALL props (picked or not), so selected props
beat 50% mechanically with zero skill. A matched-control re-analysis showed the
within-match difference (selected vs same-match unpicked props) is a coin flip
(12/24, p=1.0) — no edge. See scripts/scorer_clv_control.py.

The corrected gate tests the WITHIN-MATCH PAIRED delta: in each match, does the
mean move of selected props exceed the mean move of that match's control (never-
flagged) props? CONFIRM when the Wilson 95% lower bound of the delta>0 rate clears
0.50; KILL when its upper bound falls below 0.50; else accumulate. The old
selected-vs-50% beat-rate is still printed for continuity but no longer decides.

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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.alert_formatter import send_telegram  # noqa: E402

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
def live_snapshot(con, sport, window_hours):
    key = os.getenv("ODDS_API_KEY")
    if not key:
        sys.exit("ODDS_API_KEY not set — cannot snapshot live.")
    now = datetime.now(timezone.utc)
    books = ",".join(list(P.SOCCER_PROP_SHARP_WEIGHTS) + sorted(P.SOFT_BOOKS))
    events = P._get(f"{P.ODDS_API_BASE}/sports/{sport}/events?apiKey={key}")
    snap_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    n_rows = n_ev = 0
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
            got += _insert(
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
                edge_pct=(cons - si) * 100.0,
                n_sharp=ns,
                mins_to_kickoff=round(mins, 1),
            )
        if got:
            n_ev += 1
            n_rows += got
            print(f"  [snap] T-{mins / 60:.1f}h {title}: {got} scorer props")
    con.commit()
    print(f"[snapshot] {n_rows} rows across {n_ev} matches @ {snap_at}")


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

    sel_live = []  # (eid, mv)   selected, LIVE anchor (entry=first flag) — headline continuity
    sel_unif = defaultdict(list)  # eid -> [mv]  selected, uniform anchor (entry=earliest)
    ctl_unif = defaultdict(list)  # eid -> [mv]  control (never flagged), uniform anchor
    for (eid, player), snaps in by_prop.items():
        snaps.sort(key=lambda x: x[0])  # by snapshot_at
        if len(snaps) < 2:
            continue
        pre = [s for s in snaps if s[3] is not None and s[3] >= 0]  # pre-kickoff snaps
        ever = any(s[2] is not None and s[2] >= min_edge for s in snaps)

        # uniform anchor (entry = earliest snapshot) — applies to EVERY prop, matched windows
        e, c = snaps[0], (max(pre, key=lambda x: x[0]) if pre else snaps[-1])
        if c[0] > e[0] and e[1] is not None and c[1] is not None:
            (sel_unif if ever else ctl_unif)[eid].append((c[1] - e[1]) * 100.0)

        # live anchor (entry = first flagged snapshot) — reproduces the old selected metric
        if ever:
            flagged = [s for s in snaps if s[2] is not None and s[2] >= min_edge]
            en, cl = flagged[0], (max(pre, key=lambda x: x[0]) if pre else snaps[-1])
            if cl[0] > en[0] and en[1] is not None and cl[1] is not None:
                sel_live.append((eid, (cl[1] - en[1]) * 100.0))

    # headline (uncorrected) selected vs 50% — kept for continuity only
    by_match = defaultdict(list)
    for eid, mv in sel_live:
        by_match[eid].append(mv)
    match_mean = {eid: sum(v) / len(v) for eid, v in by_match.items()}
    n_matches = len(match_mean)
    beats = sum(1 for m in match_mean.values() if m > 0)
    pool_n = len(sel_live)
    pool_beat = sum(1 for _, mv in sel_live if mv > 0)
    lo, hi = wilson(beats, n_matches)

    # honest baseline: ALL props, uniform anchor (the real null, not 50%)
    all_unif = [mv for v in sel_unif.values() for mv in v] + [mv for v in ctl_unif.values() for mv in v]
    base_pos = sum(1 for mv in all_unif if mv > 0)
    base_n = len(all_unif)

    # DECISIVE: within-match paired delta = mean(selected) - mean(control).
    # Sign-test convention — DROP ties (delta≈0), else a perfect tie miscounts as a
    # loss and fires a spurious KILL (caught by /qa 2026-06-18).
    EPS = 1e-9
    sel_mean = {eid: sum(v) / len(v) for eid, v in sel_unif.items()}
    ctl_mean = {eid: sum(v) / len(v) for eid, v in ctl_unif.items()}
    all_deltas = [sel_mean[eid] - ctl_mean[eid] for eid in sel_mean if eid in ctl_mean]
    paired = [d for d in all_deltas if abs(d) > EPS]
    n_pair = len(paired)
    pair_pos = sum(1 for d in paired if d > 0)
    plo, phi = wilson(pair_pos, n_pair)
    mean_delta = (sum(paired) / n_pair) if n_pair else 0.0

    print("\n" + "═" * 74)
    print("  SCORER PAPER LOGGER — CLV REPORT (control-corrected gate)")
    print("═" * 74)
    print(
        f"  [headline, NOT decisive] selected vs 50%: {beats}/{n_matches} matches"
        f"{(' = %.0f%%' % (100 * beats / n_matches)) if n_matches else ''}, "
        f"Wilson [{lo:.2f}, {hi:.2f}]  (pooled props {pool_beat}/{pool_n})"
    )
    if base_n:
        print(
            f"  [honest baseline]  ALL props rose: {base_pos}/{base_n} = "
            f"{100 * base_pos / base_n:.0f}%  <- the real null, not 50%"
        )
    print(f"\n  [DECISIVE] within-match  delta = mean(selected) - mean(control):")
    print(f"    matches with both groups: {n_pair}")
    if n_pair:
        print(
            f"    selected beat control: {pair_pos}/{n_pair} = {100 * pair_pos / n_pair:.0f}%"
            f"   Wilson95 [{plo:.2f}, {phi:.2f}]   mean delta {mean_delta:+.2f}pp"
        )

    print("\n" + "═" * 74)
    if n_pair < 12:
        v = f"CONTINUE — only {n_pair} matches have both selected & control props (need ~12+)."
    elif plo > 0.50:
        v = (
            f"CONFIRM — selected beat same-match control {pair_pos}/{n_pair}, "
            f"CI lower bound {plo:.2f} > 0.50. Real edge net of drift; go to spec §6 Step 4."
        )
    elif phi < 0.50:
        v = f"KILL — selected do NOT beat control (CI upper {phi:.2f} < 0.50). Edge is drift/reversion."
    else:
        v = f"CONTINUE — paired CI [{plo:.2f},{phi:.2f}] straddles 0.50; accumulate more matches."
    print(f"  VERDICT: {v}")
    print("═" * 74 + "\n")

    if send:
        bpct = f"{100 * base_pos / base_n:.0f}%" if base_n else "n/a"
        ppct = f"{100 * pair_pos / n_pair:.0f}%" if n_pair else "n/a"
        msg = (
            f"⚽ Scorer CLV paper logger (control-corrected)\n"
            f"headline (vs 50%): {beats}/{n_matches} matches — NOT decisive\n"
            f"honest baseline: all props rose {bpct}\n"
            f"DECISIVE within-match: selected beat control {pair_pos}/{n_pair} ({ppct}), "
            f"CI [{plo:.2f}, {phi:.2f}], delta {mean_delta:+.2f}pp\n"
            f"VERDICT: {v}"
        )
        send_telegram(msg)


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
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--send", action="store_true", help="send report summary to Telegram")
    args = ap.parse_args()

    con = db_connect(args.db)
    if args.seed_historical:
        seed_historical(con, args.entry_dir, args.close_dir)
    if args.snapshot:
        live_snapshot(con, args.sport, args.window_hours)
    if args.report or not (args.seed_historical or args.snapshot):
        report(con, args.min_edge, send=args.send)
    con.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
scorer_clv_control.py — matched-control re-analysis of the scorer CLV logger.

READ-ONLY. Answers the question the plain-English doc flags as outstanding:
is the 83% match-level beat-rate real skill, or an artifact of (a) mean-reversion
of transiently-low soft lines and (b) market-wide pre-kickoff drift (public/steam
on favorites)?

It re-reads the SAME scorer_snapshot table the live report uses, but instead of
grading ONLY props with edge_pct >= min_edge, it grades the whole population and
splits it into:

  SELECTED      props that flagged (edge_pct >= min_edge) — the current methodology
  CONTROL       props in the same matches that NEVER flagged
  UNCONDITIONAL all gradable props — the honest null that should replace 50%

Decisive test: WITHIN-MATCH PAIRED difference. For every match that has both
selected and control props, delta = mean(selected move) - mean(control move).
A delta>0 beat-rate well above 50% (CI lower bound clear) = real differential
skill net of match-wide drift. delta ~ 0 = the 83% is drift/reversion.

Usage:
    python3 scripts/scorer_clv_control.py --db /tmp/scorer_clv_2026-06-18.db
    python3 scripts/scorer_clv_control.py --db ... --min-edge 5.0
"""
from __future__ import annotations

import argparse
import math
import sqlite3
from collections import defaultdict


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def sign_test_p(k, n):
    """Two-sided exact binomial p-value vs p=0.5 (sign test). stdlib only."""
    if n == 0:
        return 1.0
    # P(X<=min(k,n-k)) * 2, X~Bin(n,0.5)
    m = min(k, n - k)
    tail = sum(math.comb(n, i) for i in range(m + 1)) / (2.0 ** n)
    return min(1.0, 2.0 * tail)


def load_props(con, min_edge):
    """
    Returns dict keyed (event_id, player) -> dict with:
      title, selected (bool, ever flagged),
      mv_live   : move using live anchor (entry = first flagged snapshot)  [SELECTED only]
      mv_unif   : move using uniform anchor (entry = earliest snapshot)    [ALL props]
    Only props with a valid later close snapshot are kept.
    """
    rows = con.execute(
        """SELECT event_id, event_title, player, snapshot_at,
                  best_soft_implied, edge_pct, mins_to_kickoff
           FROM scorer_snapshot"""
    ).fetchall()

    by_prop = defaultdict(list)
    titles = {}
    for eid, title, player, snap, soft, edge, mins in rows:
        by_prop[(eid, player)].append((snap, soft, edge, mins))
        titles[eid] = title

    out = {}
    for key, snaps in by_prop.items():
        snaps.sort(key=lambda x: x[0])  # by snapshot_at (ISO sorts lexically)
        if len(snaps) < 2:
            continue
        ever_flagged = any(s[2] is not None and s[2] >= min_edge for s in snaps)
        pre = [s for s in snaps if s[3] is not None and s[3] >= 0]  # pre-kickoff

        rec = {"title": titles[key[0]], "selected": ever_flagged,
               "mv_live": None, "mv_unif": None}

        # --- uniform anchor (entry = earliest snapshot) — applies to EVERY prop ---
        entry_u = snaps[0]
        close_u = max(pre, key=lambda x: x[0]) if pre else snaps[-1]
        if close_u[0] > entry_u[0] and entry_u[1] is not None and close_u[1] is not None:
            rec["mv_unif"] = (close_u[1] - entry_u[1]) * 100.0

        # --- live anchor (entry = first flagged snapshot) — reproduces report() ---
        if ever_flagged:
            flagged = [s for s in snaps if s[2] is not None and s[2] >= min_edge]
            entry = flagged[0]
            close = max(pre, key=lambda x: x[0]) if pre else snaps[-1]
            if close[0] > entry[0] and entry[1] is not None and close[1] is not None:
                rec["mv_live"] = (close[1] - entry[1]) * 100.0

        out[key] = rec
    return out


def match_beatrate(prop_moves):
    """prop_moves: dict event_id -> list of move pp. Returns (beats, n, lo, hi, means)."""
    means = {eid: sum(v) / len(v) for eid, v in prop_moves.items() if v}
    n = len(means)
    beats = sum(1 for m in means.values() if m > 0)
    lo, hi = wilson(beats, n)
    return beats, n, lo, hi, means


def collect(props, field, selected=None):
    """event_id -> [moves] for props matching the selected filter and a non-null field."""
    out = defaultdict(list)
    for (eid, _player), rec in props.items():
        if selected is not None and rec["selected"] != selected:
            continue
        if rec[field] is not None:
            out[eid].append(rec[field])
    return out


def hr():
    print("═" * 74)


def main():
    ap = argparse.ArgumentParser(description="Matched-control re-analysis of scorer CLV.")
    ap.add_argument("--db", required=True)
    ap.add_argument("--min-edge", type=float, default=5.0)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    props = load_props(con, args.min_edge)
    con.close()

    # ---------------------------------------------------------------- A. reproduce live (provenance gate)
    sel_live = collect(props, "mv_live", selected=True)
    b, n, lo, hi, _ = match_beatrate(sel_live)
    hr(); print("  A. SELECTED, live anchor (reproduces report() — provenance check)"); hr()
    pool = [m for v in sel_live.values() for m in v]
    print(f"  match-level: {b}/{n} = {100*b/n:.0f}%   Wilson95 [{lo:.2f}, {hi:.2f}]")
    print(f"  pooled props: {sum(1 for m in pool if m>0)}/{len(pool)} moved toward you")
    print(f"  >>> should match the doc's 19/23 = 83%, CI [0.63,0.93]\n")

    # ---------------------------------------------------------------- B. the honest null (unconditional)
    uncond = collect(props, "mv_unif")  # all props, uniform anchor
    b, n, lo, hi, _ = match_beatrate(uncond)
    upool = [m for v in uncond.values() for m in v]
    upos = sum(1 for m in upool if m > 0)
    hr(); print("  B. UNCONDITIONAL baseline (ALL props, uniform anchor) — the real null"); hr()
    print(f"  match-level rise-rate: {b}/{n} = {100*b/n:.0f}%   Wilson95 [{lo:.2f}, {hi:.2f}]")
    print(f"  pooled props that rose: {upos}/{len(upool)} = {100*upos/len(upool):.0f}%")
    print(f"  mean prop drift: {sum(upool)/len(upool):+.2f}pp")
    print(f"  >>> if this is >>50%, the '13 points above random' claim is inflated\n")

    # ---------------------------------------------------------------- C. selected vs control, uniform anchor
    sel_u = collect(props, "mv_unif", selected=True)
    ctl_u = collect(props, "mv_unif", selected=False)
    bs, ns, los, his, sel_means = match_beatrate(sel_u)
    bc, nc, loc, hic, ctl_means = match_beatrate(ctl_u)
    hr(); print("  C. SELECTED vs CONTROL (uniform anchor, identical windows)"); hr()
    print(f"  SELECTED match rise-rate: {bs}/{ns} = {100*bs/ns:.0f}%   [{los:.2f}, {his:.2f}]")
    print(f"  CONTROL  match rise-rate: {bc}/{nc} = {100*bc/nc:.0f}%   [{loc:.2f}, {hic:.2f}]")
    spool = [m for v in sel_u.values() for m in v]
    cpool = [m for v in ctl_u.values() for m in v]
    print(f"  mean move  SELECTED: {sum(spool)/len(spool):+.2f}pp   "
          f"CONTROL: {sum(cpool)/len(cpool):+.2f}pp" if cpool else "  (no control props)")
    print()

    # ---------------------------------------------------------------- D. THE DECISIVE TEST: within-match paired
    hr(); print("  D. WITHIN-MATCH PAIRED  delta = mean(SELECTED) - mean(CONTROL)  [DECISIVE]"); hr()
    paired = []
    for eid in sel_means:
        if eid in ctl_means:
            paired.append((eid, sel_means[eid] - ctl_means[eid]))
    npair = len(paired)
    if npair == 0:
        print("  No matches contain BOTH selected and control props — cannot pair.")
        print("  (Fall back to the B vs A comparison above.)")
    else:
        pos = sum(1 for _, d in paired if d > 0)
        lo, hi = wilson(pos, npair)
        meand = sum(d for _, d in paired) / npair
        p = sign_test_p(pos, npair)
        print(f"  matches with both groups: {npair}")
        print(f"  delta>0 (selected rose MORE than control): {pos}/{npair} = {100*pos/npair:.0f}%")
        print(f"  Wilson95 [{lo:.2f}, {hi:.2f}]   mean delta: {meand:+.2f}pp   sign-test p={p:.3f}")
        print()
        print("  VERDICT:")
        if lo > 0.50 and p < 0.05:
            print(f"  ✅ REAL — selected props beat their own match's control "
                  f"({100*pos/npair:.0f}%, p={p:.3f}). Edge survives drift.")
        elif hi < 0.50:
            print(f"  ❌ ARTIFACT — selected do NOT beat control. The 83% is drift/reversion.")
        else:
            print(f"  ⚠️ INCONCLUSIVE — paired CI [{lo:.2f},{hi:.2f}] straddles 0.50 "
                  f"(n={npair} too small). Accumulate more matches with both groups.")
    hr()


if __name__ == "__main__":
    main()

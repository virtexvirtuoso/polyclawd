#!/usr/bin/env python3
"""
scorer_backfill.py — backfill corrected weights on resolved historical data,
then build calibration curve: does edge predict actual scoring?

This reads the old DB (scorer_clv.db) which has 74 resolved matches with
actual goal outcomes, but with WRONG consensus weights. We need to re-fetch
the raw Odds API data and recompute consensus_fair with corrected weights.

Since we can't re-fetch historical matches from Odds API (they're over),
we recompute from the stored best_soft_implied (raw soft price) and use
the archived phase0 pilot data if available.

MODES:
  --db DB          Old DB with resolved props
  --calibrate      Print calibration curve from existing data
  --compare        Show old-weights vs corrected-weights deltas
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


def calibrate(db_path: str):
    db = sqlite3.connect(db_path)
    
    # Check what we have
    r = db.execute("""
        SELECT COUNT(*), SUM(resolved), SUM(scored)
        FROM scorer_snapshot
    """).fetchone()
    total, resolved, scored = r
    print(f"Total props: {total}, Resolved: {resolved}, Scored: {scored}")
    print()
    
    # Only use resolved props
    if not resolved:
        print("No resolved props to analyze")
        return
    
    # 1. Scoring rate by edge bucket (with current/old weights)
    cur = db.execute("""
        SELECT 
            CASE 
                WHEN edge_pct < -3 THEN '< -3pp'
                WHEN edge_pct < 0 THEN '-3-0pp'
                WHEN edge_pct < 3 THEN '0-3pp'
                WHEN edge_pct < 5 THEN '3-5pp'
                WHEN edge_pct < 8 THEN '5-8pp'
                WHEN edge_pct < 12 THEN '8-12pp'
                ELSE '12pp+'
            END as bucket,
            COUNT(*) as n,
            SUM(scored) as goals
        FROM scorer_snapshot
        WHERE resolved = 1
        GROUP BY bucket
        ORDER BY MIN(edge_pct)
    """)
    rows = cur.fetchall()
    
    print("=== CALIBRATION CURVE (edge bucket -> scoring rate) ===")
    print(f"  {'Bucket':>10s} | {'N':>6s} {'Goals':>6s} {'Rate':>6s} {'CI':>14s}")
    print(f"  {'-'*10}-+-{'-'*6}-{'-'*6}-{'-'*6}-{'-'*14}")
    
    prev_rate = 50.0
    for r in rows:
        bucket, n, goals = r
        rate = 100 * goals / n if n else 0
        lo, hi = wilson(goals, n)
        delta = rate - prev_rate
        flag = ""
        if n >= 20:
            if lo > prev_rate:
                flag = " 📈"
            elif hi < prev_rate:
                flag = " 📉"
        print(f"  {bucket:>10s} | {n:>6d} {goals:>6d} {rate:>5.1f}% [{lo:>.2f},{hi:>.2f}]{flag}")
        prev_rate = rate
    
    # 2. Scoring rate by fair-value bucket
    cur = db.execute("""
        SELECT 
            CASE 
                WHEN consensus_fair < 0.03 THEN '0-3% (def)'
                WHEN consensus_fair < 0.06 THEN '3-6% (DM)'
                WHEN consensus_fair < 0.10 THEN '6-10% (mid)'
                WHEN consensus_fair < 0.15 THEN '10-15% (att)'
                WHEN consensus_fair < 0.25 THEN '15-25% (fwd)'
                ELSE '25%+ (star)'
            END as bucket,
            COUNT(*) as n,
            SUM(scored) as goals
        FROM scorer_snapshot
        WHERE resolved = 1
        GROUP BY bucket
        ORDER BY MIN(consensus_fair)
    """)
    print()
    print("=== SCORING RATE BY FAIR-VALUE BUCKET ===")
    for r in cur.fetchall():
        bucket, n, goals = r
        rate = 100 * goals / n if n else 0
        print(f"  {bucket:>12s}: {goals}/{n} = {rate:.1f}%")
    
    # 3. The key question: do flagged props (edge >= 5pp) score more often?
    cur = db.execute("""
        SELECT 
            CASE WHEN edge_pct >= 5.0 THEN 'FLAGGED' ELSE 'NOT FLAGGED' END as flagged,
            COUNT(*) as n,
            SUM(scored) as goals
        FROM scorer_snapshot
        WHERE resolved = 1
        GROUP BY flagged
    """)
    print()
    print("=== FLAGGED vs NOT FLAGGED (edge >= 5pp) ===")
    for r in cur.fetchall():
        flag, n, goals = r
        rate = 100 * goals / n if n else 0
        print(f"  {flag:15s}: {goals}/{n} = {rate:.1f}%")
    
    # 4. Calibration by flagged + fair value
    cur = db.execute("""
        SELECT 
            CASE WHEN edge_pct >= 5.0 AND consensus_fair >= 0.05 THEN 'FLAGGED+quality'
                 WHEN edge_pct >= 5.0 THEN 'FLAGGED+noise'
                 ELSE 'NOT FLAGGED'
            END as bucket,
            COUNT(*) as n,
            SUM(scored) as goals
        FROM scorer_snapshot
        WHERE resolved = 1
        GROUP BY bucket
        ORDER BY bucket
    """)
    print()
    print("=== FLAGGED FAIR-VALUE SPLIT ===")
    for r in cur.fetchall():
        bucket, n, goals = r
        rate = 100 * goals / n if n else 0
        print(f"  {bucket:20s}: {goals}/{n} = {rate:.1f}%")
    
    # 5. Average edge and scoring rate by n_sharp (how many sharp books present)
    cur = db.execute("""
        SELECT 
            CASE 
                WHEN n_sharp = 1 THEN '1 sharp'
                WHEN n_sharp = 2 THEN '2 sharp'
                WHEN n_sharp >= 3 THEN '3+ sharp'
            END as bucket,
            COUNT(*) as n,
            SUM(scored) as goals,
            AVG(edge_pct) as avg_edge,
            AVG(consensus_fair*100) as avg_fair
        FROM scorer_snapshot
        WHERE resolved = 1
        GROUP BY bucket
        ORDER BY n_sharp
    """)
    print()
    print("=== BY SHARP BOOK PRESENCE ===")
    for r in cur.fetchall():
        bucket, n, goals, avg_e, avg_f = r
        rate = 100 * goals / n if n else 0
        print(f"  {bucket:12s}: {goals}/{n} = {rate:.1f}%  avg_edge={avg_e:+.1f}pp  avg_fair={avg_f:.1f}%")
    
    # 6. Predictive value: ROC-style — how does raising min_edge change precision?
    print()
    print("=== EDGE THRESHOLD SWEEP ===")
    for thresh in [0, 2, 3, 4, 5, 6, 8, 10, 12]:
        cur = db.execute("""
            SELECT COUNT(*), SUM(scored) FROM scorer_snapshot
            WHERE resolved = 1 AND edge_pct >= ?
        """, (thresh,))
        n, goals = cur.fetchone()
        rate = 100 * goals / n if n else 0
        # Also get total flagged
        cur2 = db.execute("SELECT COUNT(*) FROM scorer_snapshot WHERE resolved = 1")
        total = cur2.fetchone()[0]
        pct_of_pop = 100 * n / total if total else 0
        print(f"  edge >= {thresh:2d}pp: {goals:>4d}/{n:<5d} = {rate:5.1f}%  ({pct_of_pop:.1f}% of pop)")
    
    # 7. Player-level: how many unique scorers
    cur = db.execute("""
        SELECT COUNT(DISTINCT player) FROM scorer_snapshot WHERE scored = 1 AND resolved = 1
    """)
    unique_scorers = cur.fetchone()[0]
    cur = db.execute("""
        SELECT COUNT(DISTINCT player) FROM scorer_snapshot WHERE resolved = 1
    """)
    unique_players = cur.fetchone()[0]
    print(f"\nUnique scorers: {unique_scorers}/{unique_players} = {100*unique_scorers/unique_players:.1f}%")
    
    db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="storage/scorer_clv.db")
    ap.add_argument("--calibrate", action="store_true")
    args = ap.parse_args()
    calibrate(args.db)
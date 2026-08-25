#!/usr/bin/env python3
"""VPIN pre-registered verdict study — full-table, kill/keep gate.

Pre-registered 2026-07-10 (see vault: Polyclawd/Development/
VPIN-Restoration-2026-07-08.md). Do NOT tune these gates after the fact.

  H1: On Polymarket markets, high VPIN (>= 0.7) with directional flow
      (buy_pct > 55 or < 45) predicts the sign of the 1h price move.

  PRIMARY metric: directional accuracy on high-VPIN events, EXCLUDING
  zero-move snapshots (sticky Polymarket prices make 0-moves ambiguous;
  the zero-move fraction is reported separately).

  Gates (all must hold for PASS):
    G1  n_high (non-zero-move) >= 500
    G2  accuracy > 55.0%
    G3  one-sided binomial p vs 50% < 0.05

  Verdict: PASS -> "SIGNAL" (eligible for aggregation wiring, with review)
           n < 500 -> "INSUFFICIENT_DATA" (keep accumulating)
           else -> "INFORMATIONAL_ONLY" (do not wire into aggregation)

Unlike signals/vpin.py:backtest_vpin_accuracy (rolling last-2000-rows
health readout), this reads the ENTIRE snapshots table.

Usage:
  venv/bin/python3 scripts/vpin_verdict_study.py [--send]
"""
import argparse
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "storage" / "vpin_snapshots.db"

VPIN_HIGH = 0.7
VPIN_LOW = 0.4
BUY_UP, BUY_DOWN = 55.0, 45.0
GATE_N, GATE_ACC, GATE_P = 500, 55.0, 0.05


def binom_p_one_sided(k: int, n: int) -> float:
    """P(X >= k) for X ~ Bin(n, 0.5), normal approx w/ continuity corr."""
    if n == 0:
        return 1.0
    mu, sd = n * 0.5, math.sqrt(n * 0.25)
    z = (k - 0.5 - mu) / sd
    return 0.5 * math.erfc(z / math.sqrt(2))


def bucket_stats(events):
    """events: list of (matched: bool). Returns dict of stats."""
    n = len(events)
    k = sum(events)
    acc = (k / n * 100) if n else 0.0
    return {"n": n, "correct": k, "accuracy": acc, "p": binom_p_one_sided(k, n)}


def run_study():
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        """SELECT vpin, buy_pct, price_at_snap, price_1h_later
           FROM vpin_snapshots WHERE price_1h_later IS NOT NULL"""
    ).fetchall()
    total_rows = conn.execute("SELECT COUNT(*) FROM vpin_snapshots").fetchone()[0]
    span = conn.execute("SELECT MIN(ts), MAX(ts) FROM vpin_snapshots").fetchone()
    conn.close()

    high, medium, high_zero = [], [], 0
    for vpin, buy_pct, p0, p1 in rows:
        if vpin is None or buy_pct is None or p0 is None or p1 is None:
            continue
        if buy_pct > BUY_UP:
            expected_up = True
        elif buy_pct < BUY_DOWN:
            expected_up = False
        else:
            continue  # neutral flow — no directional prediction
        move = p1 - p0
        if move == 0:
            if vpin >= VPIN_HIGH:
                high_zero += 1
            continue  # primary metric excludes zero-moves
        matched = (move > 0) == expected_up
        if vpin >= VPIN_HIGH:
            high.append(matched)
        elif vpin >= VPIN_LOW:
            medium.append(matched)

    hs, ms = bucket_stats(high), bucket_stats(medium)
    zero_frac = high_zero / (hs["n"] + high_zero) * 100 if (hs["n"] + high_zero) else 0.0

    g1 = hs["n"] >= GATE_N
    g2 = hs["accuracy"] > GATE_ACC
    g3 = hs["p"] < GATE_P
    if not g1:
        verdict = "INSUFFICIENT_DATA"
    elif g2 and g3:
        verdict = "SIGNAL"
    else:
        verdict = "INFORMATIONAL_ONLY"

    fmt_ts = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%m-%d %H:%M") if t else "?"
    lines = [
        "*VPIN Verdict Study* (pre-registered 2026-07-10)",
        f"Data: {len(rows):,} outcome rows of {total_rows:,} snapshots "
        f"({fmt_ts(span[0])} -> {fmt_ts(span[1])} UTC)",
        "",
        f"High VPIN (>={VPIN_HIGH}, directional, non-zero move):",
        f"  n={hs['n']:,}  acc={hs['accuracy']:.1f}%  p={hs['p']:.4f}",
        f"  zero-move fraction: {zero_frac:.1f}% ({high_zero:,} events)",
        f"Medium VPIN ({VPIN_LOW}-{VPIN_HIGH}): n={ms['n']:,}  acc={ms['accuracy']:.1f}%",
        "",
        f"Gates: n>={GATE_N} [{'PASS' if g1 else 'FAIL'}]  "
        f"acc>{GATE_ACC}% [{'PASS' if g2 else 'FAIL'}]  "
        f"p<{GATE_P} [{'PASS' if g3 else 'FAIL'}]",
        f"*VERDICT: {verdict}*",
    ]
    if verdict == "INFORMATIONAL_ONLY":
        lines.append("Action: do NOT wire VPIN into aggregation; keep as telemetry or retire.")
    elif verdict == "SIGNAL":
        lines.append("Action: eligible for aggregation wiring — review with Mr. V first.")
    else:
        lines.append(f"Action: keep accumulating (~{max(0, GATE_N - hs['n']):,} more high events needed).")
    return "\n".join(lines), verdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="send report via Telegram")
    args = ap.parse_args()

    report, verdict = run_study()
    print(report)

    if args.send:
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        from openclaw_alerts import alert_openclaw
        ok = alert_openclaw(report, parse_mode="Markdown")
        print(f"telegram delivery: {'OK' if ok else 'FAILED'}")
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    main()

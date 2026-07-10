#!/usr/bin/env python3
"""
Shin devig benchmark — pre-registered test of H1:
    Shin-devigged probabilities beat proportional devig on longshot calibration.

Standalone research harness. Does NOT import or modify any Polyclawd prod code.
The devig implementations below are byte-faithful ports of the canonical core in
`odds/sports_edge_common.py` (devig_multiway = proportional, devig_power,
devig_shin), re-expressed to accept implied-probability vectors directly so the
benchmark is data-source agnostic.

Data: football-data.co.uk season CSVs — real closing 3-way (Home/Draw/Away)
odds from Pinnacle (PSCH/PSCD/PSCA) with settled full-time results (FTR). 3-way
soccer is exactly where favorite-longshot distortion lives and where the
literature (Koning & Zijm 2022; Cain/Law/Peel 2003; Hegarty & Whelan 2023)
predicts Shin should beat proportional normalization.

Evaluation is a PAIRED comparison: identical events, identical outcomes, only the
devig method differs. Metrics: multiclass log-loss + Brier overall, plus a
one-vs-rest calibration table bucketed by predicted probability (longshot focus:
<0.10, 0.10-0.20, 0.20-0.40, 0.40-0.60, >0.60). Significance via Wilcoxon
signed-rank on per-match log-loss deltas + bootstrap 95% CI.

Usage:
    python3 shin_devig_benchmark.py --data-dir <dir of football-data CSVs>
If --data-dir is omitted it looks for CSVs next to this file under ./fd_data/.
"""
from __future__ import annotations

import argparse
import csv
import glob
import math
import os
import random
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

random.seed(42)

# Pre-registered buckets (edges chosen before seeing results).
BUCKETS: List[Tuple[float, float, str]] = [
    (0.00, 0.10, "<0.10"),
    (0.10, 0.20, "0.10-0.20"),
    (0.20, 0.40, "0.20-0.40"),
    (0.40, 0.60, "0.40-0.60"),
    (0.60, 1.01, ">0.60"),
]
EPS = 1e-15


# ─────────────────────────────────────────────────────────────────────
# Devig implementations — faithful ports of odds/sports_edge_common.py
# ─────────────────────────────────────────────────────────────────────
def devig_proportional(implied: Sequence[float]) -> List[float]:
    """Port of devig_multiway: normalize to sum 1 (multiplicative/proportional)."""
    t = sum(implied)
    return [p / t for p in implied] if t > 0 else list(implied)


def devig_power(implied: Sequence[float], iters: int = 64) -> List[float]:
    """Port of devig_power: find k s.t. sum(p_i^(1/k)) = 1 (Strumbelj 2014)."""
    if not implied or sum(implied) <= 0:
        return list(implied)
    lo, hi = 0.5, 3.0
    for _ in range(iters):
        mid = (lo + hi) / 2
        if sum(p ** (1.0 / mid) for p in implied) > 1.0:
            hi = mid
        else:
            lo = mid
    k = (lo + hi) / 2
    raw = [p ** (1.0 / k) for p in implied]
    t = sum(raw)
    return [x / t for x in raw] if t > 0 else raw


def devig_shin(implied: Sequence[float], iters: int = 50) -> List[float]:
    """Port of devig_shin: Newton-solve for insider proportion z (Shin 1992/93)."""
    s = sum(implied)
    if s <= 0 or len(implied) < 2:
        return [p / s for p in implied] if s > 0 else list(implied)
    n = len(implied)
    z = 0.0
    for _ in range(iters):
        roots = [(z * z + 4 * (1 - z) * (p * p) / s) ** 0.5 for p in implied]
        f = sum(roots) - 2.0 - z * (n - 2.0)
        df = sum(((2 * z - 4 * (p * p) / s) / (2 * r) if r > 0 else 0.0)
                 for p, r in zip(implied, roots)) - (n - 2.0)
        if abs(df) < 1e-12:
            break
        z = min(0.999, max(0.0, z - f / df))
    true = [(((z * z + 4 * (1 - z) * (p * p) / s) ** 0.5) - z) / (2 * (1 - z)) if z < 1 else p / s
            for p in implied]
    t = sum(true)
    return [x / t for x in true] if t > 0 else [p / s for p in implied]


METHODS = {
    "proportional": devig_proportional,
    "power": devig_power,
    "shin": devig_shin,
}


# ─────────────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────────────
def _f(x: Optional[str]) -> Optional[float]:
    try:
        v = float(x)
        return v if v > 1.0 else None  # decimal odds must be > 1.0
    except (TypeError, ValueError):
        return None


def load_matches(data_dir: str) -> Tuple[List[Tuple[List[float], int]], Dict[str, int]]:
    """Return [(implied[H,D,A], winner_idx)], plus a per-source count.

    Primary odds: Pinnacle closing (PSCH/PSCD/PSCA). Fallback per row to the
    market average closing (AvgCH/D/A) then Bet365 closing (B365C*) then opening
    B365 (B365H/D/A) so older seasons without Pinnacle closing still contribute.
    Winner index from FTR (H=0, D=1, A=2).
    """
    rows: List[Tuple[List[float], int]] = []
    src_count: Dict[str, int] = defaultdict(int)
    ftr_map = {"H": 0, "D": 1, "A": 2}
    files = sorted(glob.glob(os.path.join(data_dir, "*.csv")))
    for path in files:
        try:
            with open(path, newline="", encoding="latin-1") as fh:
                for r in csv.DictReader(fh):
                    ftr = (r.get("FTR") or "").strip().upper()
                    if ftr not in ftr_map:
                        continue
                    trip = src = None
                    for cols, name in (
                        (("PSCH", "PSCD", "PSCA"), "pinnacle_close"),
                        (("AvgCH", "AvgCD", "AvgCA"), "market_avg_close"),
                        (("B365CH", "B365CD", "B365CA"), "b365_close"),
                        (("B365H", "B365D", "B365A"), "b365_open"),
                    ):
                        vals = [_f(r.get(c)) for c in cols]
                        if all(v is not None for v in vals):
                            trip, src = vals, name
                            break
                    if trip is None:
                        continue
                    implied = [1.0 / o for o in trip]  # decimal → implied prob (with vig)
                    rows.append((implied, ftr_map[ftr]))
                    src_count[src] += 1
        except Exception:
            continue
    return rows, dict(src_count)


# ─────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────
def match_logloss(probs: Sequence[float], winner: int) -> float:
    return -math.log(max(probs[winner], EPS))


def match_brier(probs: Sequence[float], winner: int) -> float:
    return sum((p - (1.0 if i == winner else 0.0)) ** 2 for i, p in enumerate(probs))


def bucket_of(p: float) -> str:
    for lo, hi, lab in BUCKETS:
        if lo <= p < hi:
            return lab
    return ">0.60"


def wilcoxon_p(deltas: Sequence[float]) -> float:
    """Two-sided Wilcoxon signed-rank p-value via normal approximation."""
    nz = [d for d in deltas if d != 0.0]
    n = len(nz)
    if n < 10:
        return float("nan")
    ranks = sorted(range(n), key=lambda i: abs(nz[i]))
    rank_val = [0.0] * n
    i = 0
    srt = sorted(range(n), key=lambda k: abs(nz[k]))
    j = 0
    while j < n:
        k = j
        while k + 1 < n and abs(nz[srt[k + 1]]) == abs(nz[srt[j]]):
            k += 1
        avg = (j + 1 + k + 1) / 2.0
        for m in range(j, k + 1):
            rank_val[srt[m]] = avg
        j = k + 1
    w_pos = sum(rank_val[i] for i in range(n) if nz[i] > 0)
    w_neg = sum(rank_val[i] for i in range(n) if nz[i] < 0)
    w = min(w_pos, w_neg)
    mean = n * (n + 1) / 4.0
    sd = math.sqrt(n * (n + 1) * (2 * n + 1) / 24.0)
    if sd == 0:
        return float("nan")
    z = (w - mean) / sd
    return 2.0 * 0.5 * math.erfc(abs(z) / math.sqrt(2))


def bootstrap_ci(deltas: Sequence[float], n_boot: int = 5000) -> Tuple[float, float]:
    n = len(deltas)
    means = []
    for _ in range(n_boot):
        s = 0.0
        for _ in range(n):
            s += deltas[random.randrange(n)]
        means.append(s / n)
    means.sort()
    return means[int(0.025 * n_boot)], means[int(0.975 * n_boot)]


# ─────────────────────────────────────────────────────────────────────
# Benchmark
# ─────────────────────────────────────────────────────────────────────
def run(data_dir: str) -> None:
    matches, src_count = load_matches(data_dir)
    N = len(matches)
    print("=" * 78)
    print("SHIN DEVIG BENCHMARK — 3-way soccer (H/D/A), closing odds + settled results")
    print("=" * 78)
    print(f"Data dir : {data_dir}")
    print(f"N matches: {N}   (each = 3 one-vs-rest outcome observations)")
    print("Source odds used (per-row priority):")
    for k, v in sorted(src_count.items(), key=lambda kv: -kv[1]):
        print(f"    {k:18s} {v}")
    if N < 100:
        print("\n[WARN] insufficient sample — results not reliable.")
        return

    # Per-method aggregate + per-match series (paired) + calibration accumulators.
    probs_by_method: Dict[str, List[List[float]]] = {m: [] for m in METHODS}
    ll_series: Dict[str, List[float]] = {m: [] for m in METHODS}
    br_series: Dict[str, List[float]] = {m: [] for m in METHODS}
    # calibration: method -> bucket -> [n, sum_pred, sum_realized]
    calib: Dict[str, Dict[str, List[float]]] = {
        m: {lab: [0.0, 0.0, 0.0] for _, _, lab in BUCKETS} for m in METHODS
    }

    for implied, winner in matches:
        for m, fn in METHODS.items():
            p = fn(implied)
            probs_by_method[m].append(p)
            ll_series[m].append(match_logloss(p, winner))
            br_series[m].append(match_brier(p, winner))
            for i, pi in enumerate(p):  # one-vs-rest calibration
                lab = bucket_of(pi)
                c = calib[m][lab]
                c[0] += 1
                c[1] += pi
                c[2] += 1.0 if i == winner else 0.0

    # ── Headline table ──
    print("\n" + "-" * 78)
    print("OVERALL (lower = better)")
    print(f"{'method':14s} {'log-loss':>12s} {'Brier':>12s}")
    for m in METHODS:
        ll = sum(ll_series[m]) / N
        br = sum(br_series[m]) / N
        print(f"{m:14s} {ll:12.5f} {br:12.5f}")

    # ── Calibration by predicted-prob bucket (longshot focus) ──
    print("\n" + "-" * 78)
    print("CALIBRATION — one-vs-rest, per predicted-probability bucket")
    print("(pred = mean predicted prob in bucket; real = realized frequency; N = obs)")
    print(f"{'bucket':11s} " + "".join(f"| {m[:10]:>22s} " for m in METHODS))
    print(f"{'':11s} " + "".join(f"| {'pred':>6s} {'real':>6s} {'N':>7s} " for _ in METHODS))
    for _, _, lab in BUCKETS:
        line = f"{lab:11s} "
        for m in METHODS:
            n, sp, sr = calib[m][lab]
            if n > 0:
                line += f"| {sp/n:6.3f} {sr/n:6.3f} {int(n):7d} "
            else:
                line += f"| {'--':>6s} {'--':>6s} {0:7d} "
        print(line)

    # ── Paired significance: Shin vs proportional (primary H1 contrast) ──
    print("\n" + "-" * 78)
    print("PAIRED TEST — Shin vs proportional (per-match log-loss delta = shin - prop)")
    deltas = [s - p for s, p in zip(ll_series["shin"], ll_series["proportional"])]
    mean_d = sum(deltas) / N
    lo, hi = bootstrap_ci(deltas)
    pval = wilcoxon_p(deltas)
    print(f"mean delta log-loss : {mean_d:+.6f}   (negative ⇒ Shin better)")
    print(f"bootstrap 95% CI    : [{lo:+.6f}, {hi:+.6f}]")
    print(f"Wilcoxon p-value    : {pval:.4f}")

    # Longshot-only paired contrast (<0.10 predicted by proportional).
    ls_shin, ls_prop = [], []
    for implied, winner in matches:
        pp = devig_proportional(implied)
        ps = devig_shin(implied)
        for i in range(3):
            if pp[i] < 0.10:  # proportional-longshot outcomes
                ls_prop.append((pp[i] - (1.0 if i == winner else 0.0)) ** 2)
                ls_shin.append((ps[i] - (1.0 if i == winner else 0.0)) ** 2)
    if ls_prop:
        ls_delta = [s - p for s, p in zip(ls_shin, ls_prop)]
        md = sum(ls_delta) / len(ls_delta)
        lo2, hi2 = bootstrap_ci(ls_delta)
        print(f"\nLongshot-only (<0.10 proportional-pred) Brier delta = shin - prop")
        print(f"  N obs             : {len(ls_delta)}")
        print(f"  mean Brier delta  : {md:+.6f}   (negative ⇒ Shin better on longshots)")
        print(f"  bootstrap 95% CI  : [{lo2:+.6f}, {hi2:+.6f}]")

    # ── Verdict ──
    print("\n" + "=" * 78)
    ci_excludes_zero = hi < 0 or lo > 0
    if mean_d < 0 and hi < 0:
        verdict = "SUPPORTED — Shin lower log-loss, 95% CI excludes zero"
    elif mean_d > 0 and lo > 0:
        verdict = "REFUTED — proportional lower log-loss, 95% CI excludes zero"
    else:
        verdict = "INSUFFICIENT — difference not distinguishable from noise (CI spans zero)"
    print(f"H1 VERDICT: {verdict}")
    print("=" * 78)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    default_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fd_data")
    ap.add_argument("--data-dir", default=default_dir,
                    help="Directory of football-data.co.uk season CSVs")
    args = ap.parse_args()
    run(args.data_dir)

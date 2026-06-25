# signals/calibration_core.py
"""Pure, dependency-light calibration math. No DB, no I/O — unit-testable."""

from typing import List, Dict, Tuple


def reliability_table(preds: List[float], outcomes: List[int], n_bins: int = 10) -> List[Dict]:
    """Bin predictions into [0,1] and report mean predicted vs actual win rate per bin."""
    assert len(preds) == len(outcomes)
    bins = [[] for _ in range(n_bins)]
    for p, y in zip(preds, outcomes):
        idx = min(int(p * n_bins), n_bins - 1)
        bins[idx].append((p, y))
    out = []
    for i, b in enumerate(bins):
        if not b:
            continue
        mp = sum(p for p, _ in b) / len(b)
        ac = sum(y for _, y in b) / len(b)
        out.append({"bin": i, "n": len(b), "mean_pred": round(mp, 4), "actual": round(ac, 4)})
    return out


def expected_calibration_error(preds: List[float], outcomes: List[int], n_bins: int = 10) -> float:
    """Sample-weighted average |mean_pred - actual| across bins. 0 = perfect."""
    tbl = reliability_table(preds, outcomes, n_bins)
    total = len(preds)
    if total == 0:
        return 0.0
    return round(sum(b["n"] / total * abs(b["mean_pred"] - b["actual"]) for b in tbl), 4)


import numpy as np


def time_forward_split(rows: List[Dict], key: str, train_frac: float = 0.7):
    """Sort by time, split chronologically. Train is strictly older than test (no leakage).

    key must name a TRUSTWORTHY chronological field; on live shadow_trades use "timestamp"
    (entry time), NOT "resolved_at" (bulk-stamped/corrupt).
    """
    s = sorted(rows, key=lambda r: r.get(key) or "")
    cut = int(len(s) * train_frac)
    return s[:cut], s[cut:]


def fit_isotonic(raw_scores: List[float], outcomes: List[int]) -> Dict:
    """Pool-adjacent-violators isotonic regression. Returns a {x:[], y:[]} step model."""
    order = np.argsort(raw_scores, kind="stable")
    x = np.asarray(raw_scores, float)[order]
    y = np.asarray(outcomes, float)[order]
    # Pre-pool tied x values into their mean outcome before PAV so weights stay clean.
    ux, inv = np.unique(x, return_inverse=True)
    uy = np.array([y[inv == k].mean() for k in range(len(ux))])
    # PAV on deduplicated (x, y) — weights are all 1 here (equal group sizes not needed;
    # monotonicity on means is what matters for interpolation).
    w = np.ones(len(uy))
    yhat = uy.copy()
    i = 0
    while i < len(yhat) - 1:
        if yhat[i] > yhat[i + 1]:
            new = (yhat[i] * w[i] + yhat[i + 1] * w[i + 1]) / (w[i] + w[i + 1])
            yhat[i] = yhat[i + 1] = new
            w[i] = w[i + 1] = w[i] + w[i + 1]
            i = max(i - 1, 0)
        else:
            i += 1
    return {"x": ux.tolist(), "y": yhat.tolist()}


def apply_isotonic(model: Dict, raw: float) -> float:
    """Interpolate the calibrated probability for a raw score."""
    x, y = model["x"], model["y"]
    if not x:
        return raw
    return float(np.interp(raw, x, y))


def brier_score(preds: List[float], outcomes: List[int]) -> float:
    """Mean squared error of probabilistic forecasts. 0 = perfect. Proper scoring rule."""
    assert len(preds) == len(outcomes)
    n = len(preds)
    if n == 0:
        return 0.0
    return sum((p - y) ** 2 for p, y in zip(preds, outcomes)) / n


def brier_skill_score(preds: List[float], outcomes: List[int], baseline: List[float]) -> float:
    """1 - brier(preds)/brier(baseline). >0 = better than baseline (e.g. the market).
    Returns 0.0 if the baseline is itself perfect (no skill room)."""
    assert len(preds) == len(outcomes) == len(baseline)
    b_model = brier_score(preds, outcomes)
    b_base = brier_score(baseline, outcomes)
    if b_base == 0.0:
        return 0.0
    return round(1.0 - b_model / b_base, 4)


def bootstrap_ece_ci(
    preds: List[float], outcomes: List[int], n_boot: int = 2000, seed: int = 0, n_bins: int = 10
) -> Tuple[float, float]:
    """Percentile bootstrap 95% CI for ECE. Resamples (pred, outcome) pairs with
    replacement n_boot times. Deterministic given seed."""
    if not preds:
        return (0.0, 0.0)
    rng = np.random.default_rng(seed)
    n = len(preds)
    idx_pool = np.arange(n)
    eces = []
    pa = np.asarray(preds, float)
    ya = np.asarray(outcomes, int)
    for _ in range(n_boot):
        idx = rng.choice(idx_pool, size=n, replace=True)
        eces.append(expected_calibration_error(pa[idx].tolist(), ya[idx].tolist(), n_bins))
    lo, hi = np.percentile(eces, [2.5, 97.5])
    return (round(float(lo), 4), round(float(hi), 4))

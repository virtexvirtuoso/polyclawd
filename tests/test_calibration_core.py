# tests/test_calibration_core.py
import math
from signals.calibration_core import reliability_table, expected_calibration_error


def test_perfectly_calibrated_has_zero_ece():
    # 100 preds at p=0.7 that win exactly 70% → ECE ~ 0
    preds = [0.7] * 100
    outcomes = [1] * 70 + [0] * 30
    ece = expected_calibration_error(preds, outcomes, n_bins=10)
    assert ece < 0.02


def test_overconfident_has_high_ece():
    # claims 0.6 but only wins 25% → large ECE
    preds = [0.6] * 100
    outcomes = [1] * 25 + [0] * 75
    ece = expected_calibration_error(preds, outcomes, n_bins=10)
    assert ece > 0.3


def test_reliability_table_bins_and_counts():
    preds = [0.1, 0.15, 0.85, 0.9]
    outcomes = [0, 0, 1, 1]
    tbl = reliability_table(preds, outcomes, n_bins=10)
    assert sum(b["n"] for b in tbl) == 4
    assert all("mean_pred" in b and "actual" in b for b in tbl)


from signals.calibration_core import time_forward_split, fit_isotonic, apply_isotonic


def test_time_forward_split_no_leakage():
    rows = [{"resolved_at": f"2026-06-{d:02d}", "x": d} for d in range(1, 11)]
    train, test = time_forward_split(rows, key="resolved_at", train_frac=0.7)
    assert len(train) == 7 and len(test) == 3
    assert max(r["x"] for r in train) < min(r["x"] for r in test)  # strictly earlier


def test_isotonic_monotone_and_shrinks_overconfidence():
    # raw scores 0.6 that actually win 25% → calibrated should map ~0.6 down toward ~0.25
    raw = [0.6] * 40
    won = [1] * 10 + [0] * 30
    model = fit_isotonic(raw, won)
    assert apply_isotonic(model, 0.6) < 0.45
    # monotonic: higher raw never maps lower
    assert apply_isotonic(model, 0.9) >= apply_isotonic(model, 0.3)

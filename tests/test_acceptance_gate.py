# tests/test_acceptance_gate.py
import json, pytest
from signals.calibration_core import time_forward_split, expected_calibration_error
from signals.empirical_confidence import calibrated_confidence_oos

FIX = "tests/fixtures/resolved_trades_2026-06-16.json"
MIN_TEST = 30  # need enough resolved trades for a real verdict
ECE_MAX = 0.10  # claimed vs realized must agree within ~10pp avg


def test_confidence_is_calibrated_enough_to_trade():
    rows = json.load(open(FIX))
    train, test = time_forward_split(rows, key="resolved_at", train_frac=0.7)
    if len(test) < MIN_TEST:
        pytest.skip(f"only {len(test)} OOS trades — NOT cleared to trade; keep paper-only")
    preds = [calibrated_confidence_oos(r["title"], r["side"], float(r["price"]), train) / 100 for r in test]
    outcomes = [int(r["won"]) for r in test]
    ece = expected_calibration_error(preds, outcomes)
    assert ece <= ECE_MAX, f"ECE {ece} > {ECE_MAX} — confidence still miscalibrated, stay paper-only"

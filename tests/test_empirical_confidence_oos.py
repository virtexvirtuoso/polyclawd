# tests/test_empirical_confidence_oos.py
"""Out-of-sample calibration test for the empirical confidence engine.

Gate philosophy (mirrors the Task 6 go-live gate): with <10 of the bot's OWN
resolved trades in an archetype, the OOS path is identical to the borrowed Becker
prior, so calibration cannot be validated — SKIP and stay paper-only. Once any
archetype has >=10 own train samples, assert the OOS-calibrated confidence is at
least as well-calibrated (ECE) as the legacy in-sample path.
"""
import json
from collections import Counter

import pytest

from signals.calibration_core import time_forward_split, expected_calibration_error
from signals.empirical_confidence import (
    calibrated_confidence_oos,
    calculate_empirical_confidence,
    classify_archetype,
)

FIX = "tests/fixtures/resolved_trades_2026-06-16.json"
MIN_OWN_ARCH = 10  # OOS path only diverges from the borrowed Becker prior past this


def test_oos_calibration_at_least_as_good_as_legacy_when_data_sufficient():
    rows = json.load(open(FIX))
    train, test = time_forward_split(rows, key="resolved_at", train_frac=0.7)
    if len(test) < 8:
        pytest.skip("not enough resolved trades yet for OOS test")

    arch_counts = Counter(classify_archetype(t["title"]) for t in train)
    max_own = max(arch_counts.values(), default=0)
    if max_own < MIN_OWN_ARCH:
        pytest.skip(
            f"no archetype has >={MIN_OWN_ARCH} of the bot's own train samples "
            f"(max={max_own}) — OOS path == Becker fallback; calibration not yet "
            f"validatable, stays paper-only"
        )

    outcomes = [int(r["won"]) for r in test]
    oos = [calibrated_confidence_oos(r["title"], r["side"], float(r["price"]), train) / 100.0
           for r in test]
    legacy = [calculate_empirical_confidence(r["title"], r["side"], float(r["price"]))["confidence"]
              for r in test]
    oos_ece = expected_calibration_error(oos, outcomes)
    legacy_ece = expected_calibration_error(legacy, outcomes)
    assert oos_ece <= legacy_ece, f"OOS ECE {oos_ece} worse than legacy {legacy_ece}"

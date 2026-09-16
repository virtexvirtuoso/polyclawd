# tests/test_calibration_scoring.py
from signals.calibration_core import brier_score, brier_skill_score, bootstrap_ece_ci


def test_brier_perfect_is_zero():
    assert brier_score([1.0, 0.0, 1.0], [1, 0, 1]) == 0.0


def test_brier_known_value():
    # one pred 0.8 on a win (0.04) and 0.3 on a loss (0.09) -> mean 0.065
    assert abs(brier_score([0.8, 0.3], [1, 0]) - 0.065) < 1e-9


def test_brier_skill_positive_when_better_than_baseline():
    preds = [0.9, 0.1, 0.8, 0.2]
    outcomes = [1, 0, 1, 0]
    baseline = [0.5, 0.5, 0.5, 0.5]  # uninformative market
    bss = brier_skill_score(preds, outcomes, baseline)
    assert bss > 0.5  # much better than 0.25 baseline brier


def test_brier_skill_zero_when_equal_to_baseline():
    preds = [0.5, 0.5]
    outcomes = [1, 0]
    assert brier_skill_score(preds, outcomes, [0.5, 0.5]) == 0.0


def test_bootstrap_ci_brackets_point_estimate_and_is_ordered():
    from signals.calibration_core import expected_calibration_error

    preds = [0.1] * 20 + [0.9] * 20
    outcomes = [0] * 20 + [1] * 20  # well calibrated
    lo, hi = bootstrap_ece_ci(preds, outcomes, n_boot=500, seed=0)
    point = expected_calibration_error(preds, outcomes)
    assert 0.0 <= lo <= point <= hi <= 1.0


def test_brier_rejects_length_mismatch():
    import pytest

    with pytest.raises(AssertionError):
        brier_score([0.5, 0.5], [1])


def test_resolution_logger_uses_shared_brier(monkeypatch, tmp_path):
    # get_auto_scorecard must compute the SAME brier as calibration_core.brier_score
    import signals.resolution_logger as rl
    from signals.calibration_core import brier_score

    recs = [{"mc_prob": 0.8, "won": True}, {"mc_prob": 0.3, "won": False}] * 12  # n=24 >=20
    monkeypatch.setattr(rl, "load_auto_resolutions", lambda strategy: recs)
    card = rl.get_auto_scorecard("tweet_count_mc")
    preds = [r["mc_prob"] for r in recs]
    outs = [int(r["won"]) for r in recs]
    assert abs(card["brier"] - brier_score(preds, outs)) < 1e-9

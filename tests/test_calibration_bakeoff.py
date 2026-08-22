# tests/test_calibration_bakeoff.py
from scripts.recalibrate import run_bakeoff, PROMOTE_MARGIN, MIN_TEST


def _rows(n, win_rate, conf):
    # rows the bakeoff consumes: title/side/price/won/timestamp
    return [
        {
            "title": "Team A vs Team B winner",
            "side": "YES",
            "price": conf,
            "won": 1 if i < int(n * win_rate) else 0,
            "timestamp": f"2026-06-{1 + i % 28:02d}T00:00:00",
        }
        for i in range(n)
    ]


def test_no_op_below_min_test():
    rows = _rows(20, 0.5, 0.5)  # <30 test after split
    res = run_bakeoff(rows, champion={"version": 1, "maps": {}, "meta": {}})
    assert res["decision"] == "skip_insufficient"


def test_keeps_champion_when_challenger_not_better():
    rows = _rows(200, 0.5, 0.5)  # nothing to learn -> no skill gain
    res = run_bakeoff(rows, champion={"version": 1, "maps": {}, "meta": {}})
    assert res["decision"] in ("keep", "skip_insufficient")
    assert res["promoted"] is False


def test_promotes_when_challenger_beats_a_bad_champion():
    # The market is overconfident (price 0.9) but realized win-rate is 0.4, so a
    # calibrated challenger beats the market (positive skill). The incumbent champion
    # carries a STALE/bad map that forces ~0.9 -> worse than market. Challenger should
    # beat champion by a margin and promote (version bumps).
    from signals.empirical_confidence import classify_archetype

    rows = _rows(200, 0.4, 0.9)
    arch = classify_archetype(rows[0]["title"])
    bad_champion = {
        "version": 2,
        "maps": {arch: {"x": [0.0, 1.0], "y": [0.9, 0.9]}},  # forces ~0.9
        "meta": {},
    }
    res = run_bakeoff(rows, bad_champion)
    assert res["promoted"] is True
    assert res["challenger_skill"] > res["champion_skill"]
    assert res["margin"] >= PROMOTE_MARGIN
    assert res["new_champion"]["version"] == 3
    assert res["new_champion"]["maps"]


def test_decision_record_has_required_fields():
    rows = _rows(200, 0.4, 0.9)
    res = run_bakeoff(rows, champion={"version": 1, "maps": {}, "meta": {}})
    for k in ("decision", "promoted", "n_test", "champion_skill", "challenger_skill", "ece_ci", "margin"):
        assert k in res


def test_hysteresis_blocks_promotion_during_cooldown():
    # A challenger that WOULD beat a bad champion is still blocked while in cooldown.
    from signals.empirical_confidence import classify_archetype

    rows = _rows(200, 0.4, 0.9)
    arch = classify_archetype(rows[0]["title"])
    bad_champion = {"version": 2, "maps": {arch: {"x": [0.0, 1.0], "y": [0.9, 0.9]}}, "meta": {}}
    res = run_bakeoff(rows, bad_champion, runs_since_promo=0)  # 0 < COOLDOWN_RUNS
    assert res["promoted"] is False
    assert res["decision"] == "keep"
    assert res["challenger_skill"] > res["champion_skill"]  # would win if not for cooldown

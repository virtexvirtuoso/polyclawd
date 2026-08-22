# tests/test_inference_champion.py
from signals.empirical_confidence import apply_calibration
from signals.calibration_champion import IDENTITY_CHAMPION


def test_identity_champion_preserves_legacy_behavior(monkeypatch):
    # With identity champion, apply_calibration output == pre-champion soft-cap output.
    monkeypatch.setattr("signals.empirical_confidence._active_champion",
                        lambda: IDENTITY_CHAMPION)
    # 0.25 is below the "other" cap (0.30) -> passthrough, unchanged
    assert apply_calibration(0.25, "other") == 0.25


def test_champion_map_shifts_confidence(monkeypatch):
    champ = {"version": 1,
             "maps": {"other": {"x": [0.0, 1.0], "y": [0.0, 0.5]}},  # halve everything
             "meta": {}}
    monkeypatch.setattr("signals.empirical_confidence._active_champion", lambda: champ)
    # 0.25 is below "other" cap (0.30) -> soft-cap passthrough -> isotonic interp on (0->0, 1->0.5) = 0.125
    assert abs(apply_calibration(0.25, "other") - 0.125) < 1e-9


def test_identity_champion_preserves_legacy_above_cap(monkeypatch):
    monkeypatch.setattr("signals.empirical_confidence._active_champion",
                        lambda: IDENTITY_CHAMPION)
    # 0.87 > "other" cap (0.30) -> legacy soft-cap: 0.30 + (0.57 * 0.15) = 0.3855
    assert abs(apply_calibration(0.87, "other") - 0.3855) < 1e-9

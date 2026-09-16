# tests/test_calibration_champion.py
import json
from signals.calibration_champion import (
    load_champion,
    save_champion,
    apply_champion,
    IDENTITY_CHAMPION,
)


def test_missing_file_returns_identity(tmp_path):
    champ = load_champion(path=tmp_path / "nope.json")
    assert champ["version"] == 0
    assert champ["maps"] == {}


def test_corrupt_file_returns_identity_no_crash(tmp_path):
    p = tmp_path / "champ.json"
    p.write_text("{ this is not json")
    champ = load_champion(path=p)  # must NOT raise
    assert champ == IDENTITY_CHAMPION


def test_save_then_load_roundtrips(tmp_path):
    p = tmp_path / "champ.json"
    model = {"version": 3, "maps": {"sports_winner": {"x": [0.3, 0.6], "y": [0.4, 0.7]}}, "meta": {"n_test": 42}}
    save_champion(model, path=p)
    assert load_champion(path=p) == model


def test_apply_identity_is_passthrough():
    assert apply_champion(0.55, "other", champion=IDENTITY_CHAMPION) == 0.55


def test_apply_known_archetype_interpolates():
    champ = {"version": 1, "maps": {"sports_winner": {"x": [0.2, 0.8], "y": [0.3, 0.6]}}, "meta": {}}
    # raw 0.5 -> interp between (0.2->0.3) and (0.8->0.6) = 0.45
    assert abs(apply_champion(0.5, "sports_winner", champion=champ) - 0.45) < 1e-9


def test_apply_unknown_archetype_passthrough():
    champ = {"version": 1, "maps": {"sports_winner": {"x": [0.2, 0.8], "y": [0.3, 0.6]}}, "meta": {}}
    assert apply_champion(0.5, "weather_ensemble", champion=champ) == 0.5


def test_apply_corrupt_submodel_passthrough():
    # sub-model is the wrong type / missing keys -> must NOT raise, returns raw
    bad1 = {"version": 1, "maps": {"sports_winner": "not-a-dict"}, "meta": {}}
    bad2 = {"version": 1, "maps": {"sports_winner": {"z": [0.2]}}, "meta": {}}
    assert apply_champion(0.5, "sports_winner", champion=bad1) == 0.5
    assert apply_champion(0.5, "sports_winner", champion=bad2) == 0.5


def test_load_does_not_mutate_identity_singleton(tmp_path):
    from signals.calibration_champion import IDENTITY_CHAMPION

    champ = load_champion(path=tmp_path / "nope.json")
    champ["maps"]["x"] = {"x": [0.0], "y": [1.0]}  # mutate the returned copy
    assert IDENTITY_CHAMPION["maps"] == {}  # singleton untouched


def test_append_ledger_never_raises_on_bad_input(tmp_path):
    from signals.calibration_champion import append_ledger

    p = tmp_path / "ledger.jsonl"
    append_ledger({"data": object()}, path=p)  # non-serializable, must not raise

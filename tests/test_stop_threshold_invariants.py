"""Audit #16: a per-strategy stop threshold at or above UNIVERSAL_MAX_LOSS_PCT is dead.

The universal stop is checked first, on the same loss figure, with `continue`, so a
threshold that is not strictly below it can never fire (it was true of the 0.50 / 0.70
STOP_CONFIG caps and of the 0.40 urgent weather threshold, now deleted). This test
reads every loss threshold the module still has and requires each to be strictly
below it. Adding a threshold means adding it to EXPECTED on purpose.
"""

import re
from pathlib import Path

import services.stop_evaluator as se

EXPECTED = {
    "URGENT_STOP_CONFIG[default]",
    "URGENT_STOP_CONFIG[tweet_count_mc]",
    "POST_LOCK_CONFIG[weather]",
    "PRE_RESOLVE_WARN_LOSS_PCT",
}


def _loss_thresholds():
    found = {}
    for table in ("STOP_CONFIG", "URGENT_STOP_CONFIG", "POST_LOCK_CONFIG"):
        for strategy, cfg in getattr(se, table).items():
            if "max_loss_pct" in cfg:
                found[f"{table}[{strategy}]"] = cfg["max_loss_pct"]
    found["PRE_RESOLVE_WARN_LOSS_PCT"] = se.PRE_RESOLVE_WARN_LOSS_PCT
    return found


def test_the_thresholds_the_module_has_are_the_ones_we_know():
    assert set(_loss_thresholds()) == EXPECTED


def test_every_per_strategy_threshold_is_strictly_below_the_universal_stop():
    for name, value in _loss_thresholds().items():
        assert value < se.UNIVERSAL_MAX_LOSS_PCT, (
            f"{name}={value} is not below UNIVERSAL_MAX_LOSS_PCT={se.UNIVERSAL_MAX_LOSS_PCT}: "
            "the universal stop would fire first and this threshold could never fire")


def test_post_lock_weather_loss_default_is_strictly_below_the_universal_stop():
    """The paper loops read the post-lock weather loss from engine state with an
    inline literal default (no constant is exposed), once in each loop. Read both
    literals from the source so the check follows the code, and require each to
    be below the universal stop; the two must agree."""
    src = Path(se.__file__).read_text()
    defaults = re.findall(
        r'engine_state\.get\("post_lock_weather_max_loss",\s*([0-9.]+)\)', src)
    assert len(defaults) == 2, defaults       # one per paper loop
    assert len(set(defaults)) == 1, defaults
    for value in map(float, defaults):
        assert value < se.UNIVERSAL_MAX_LOSS_PCT, (
            f"post_lock_weather_max_loss default {value} is not below "
            f"UNIVERSAL_MAX_LOSS_PCT={se.UNIVERSAL_MAX_LOSS_PCT}")

"""Calibration/coherence fixes for the tweet-count MC model.

Reproduces three defects found 2026-06-25 (Paper-To-Live review):

  Fix 1  bracket-width mismatch: the MC binned in hardcoded 20-wide brackets
         ("40-59") while many markets use 25-wide brackets ("40-64"), so
         mc_probs.get("40-64", 0) ALWAYS missed -> 0.
  Fix 2  0-as-certainty inversion: a missing/out-of-support bracket read 0,
         then mc_no = 1 - 0 = 1.0 ("100% certain NO"), booking edge == market
         price. The doc's headline "+45.5% edge" was just yes_price * 100.
  Fix 3  0.95 confidence stamped on those out-of-support artifacts.

Run: venv/bin/python -m pytest tests/test_tweet_count_calibration.py -v --noconftest
"""

from signals.tweet_count_scanner import bracket_probability, decide_signal, MIN_EDGE_PCT


# --- Fix 1: probability is computed against the market's ACTUAL bracket bounds ---


def test_25_wide_bracket_is_scored_not_missed():
    # Simulated final counts clustered around 50 (a 2-day window, ~30/day model).
    totals = [40, 45, 50, 55, 64, 70]
    # The OLD code keyed on width-20 brackets and returned 0 for "40-64".
    # Correct: 5 of 6 totals (40,45,50,55,64) fall in [40,64]; 70 is excluded.
    p = bracket_probability(totals, "40-64")
    assert p == 5 / 6


def test_open_ended_bracket_bounds():
    totals = [500, 560, 590, 620]
    # "580+" => count >= 580 => 590,620 => 2/4
    assert bracket_probability(totals, "580+") == 2 / 4


# --- Fix 2: out-of-support => "no opinion" (None), NEVER 0/certainty ---


def test_out_of_support_above_envelope_returns_none():
    totals = [40, 50, 60, 70, 79]  # model thinks 40-79
    assert bracket_probability(totals, "300-319") is None  # not 0.0


def test_out_of_support_below_envelope_returns_none():
    totals = [40, 50, 60, 70, 79]
    assert bracket_probability(totals, "0-19") is None


def test_in_support_gap_is_zero_not_none():
    # Bracket sits INSIDE the simulated envelope but no sim landed in it.
    # That is a genuine ~0 we can act on (legit NO bet), so 0.0 not None.
    totals = [10, 10, 90, 90]  # envelope [10, 90]
    assert bracket_probability(totals, "40-59") == 0.0


# --- Fix 2/3: the decision layer never fabricates certainty or confidence ---


def test_out_of_support_yields_no_signal():
    # This is the exact doc artifact: market 0.46, model has no opinion.
    # Must return None (skip), NOT a NO bet with edge == 46.
    assert decide_signal(None, 0.46) is None


def test_model_agreeing_with_market_yields_no_signal():
    # In-support but model ~ market => edge below threshold => skip.
    assert decide_signal(0.45, 0.46) is None


def test_genuine_edge_surfaces_with_honest_confidence():
    # Model has support mass: mc_yes=0.10 vs market 0.40 -> NO is underpriced.
    sig = decide_signal(0.10, 0.40)
    assert sig is not None
    assert sig["side"] == "NO"
    assert sig["edge_pct"] == round((0.90 - 0.60) * 100, 1)  # +30.0
    # confidence reflects our_prob (mc_no=0.90), and is never a blind 0.95 default
    assert sig["confidence"] == 0.90

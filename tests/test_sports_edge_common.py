"""Unit tests for the generic sport-edge core (odds/sports_edge_common.py).

Pure functions only — no network. Run from app root:
    venv/bin/python -m pytest tests/test_sports_edge_common.py -v
"""
import pytest

from odds import sports_edge_common as sec


# ── American odds / devig ────────────────────────────────────────────
def test_american_to_implied_prob():
    assert sec.american_to_implied_prob(-200) == pytest.approx(0.6667, abs=1e-3)
    assert sec.american_to_implied_prob(150) == pytest.approx(0.40, abs=1e-3)


def test_devig_two_way_sums_to_one_favorite_higher():
    a, b = sec.devig_two_way(-200, 150)
    assert a + b == pytest.approx(1.0, abs=1e-9)
    assert a > b


def test_devig_multiway_normalizes():
    out = sec.devig_multiway([0.5, 0.3, 0.3])  # overround 1.1
    assert sum(out) == pytest.approx(1.0, abs=1e-9)


def test_devig_shin_sums_to_one():
    legs = sec.devig_shin([0.667, 0.40])
    assert sum(legs) == pytest.approx(1.0, abs=1e-9)


def test_devig_shin_favors_favorite_more_than_proportional():
    # Two-way: book implied 0.667 / 0.40 (overround). Shin should push the
    # favorite HIGHER than simple proportional normalization.
    implied = [0.667, 0.40]
    prop_fav = implied[0] / sum(implied)
    shin_fav = sec.devig_shin(implied)[0]
    assert shin_fav > prop_fav


def test_devig_shin_three_way_favorite_realistic():
    # -300 / +280 / +600  → implied 0.75 / 0.263 / 0.143 (overround ~1.156)
    implied = [sec.american_to_implied_prob(x) for x in (-300, 280, 600)]
    legs = sec.devig_shin(implied)
    assert sum(legs) == pytest.approx(1.0, abs=1e-9)
    # proportional would give ~0.649; Shin should keep the favorite clearly higher
    assert legs[0] > 0.66


# ── String / matching helpers ────────────────────────────────────────
def test_strip_trailing_date():
    assert sec.strip_trailing_date("Will Houston Dynamo win on 2026-03-07?") == "Will Houston Dynamo win"
    assert sec.strip_trailing_date("Will A vs B end in a draw?") == "Will A vs B end in a draw?"


def test_norm_strips_diacritics():
    assert sec._norm("Türkiye") == "turkiye"
    assert sec._norm("Édgar Cháirez") == "edgar chairez"


def test_match_event_by_participants_with_unicode_alias():
    events = [{"title": "Türkiye vs. Portugal",
               "markets": [{"question": "Will Turkey win on 2026-06-20?"}]}]
    aliases = {"Turkey": ["Turkey", "Türkiye"]}
    ev = sec.match_event_by_participants(["Turkey", "Portugal"], events, aliases)
    assert ev is not None


def test_match_event_returns_none_when_absent():
    events = [{"title": "Spain vs. France", "markets": []}]
    assert sec.match_event_by_participants(["Brazil", "Argentina"], events, {}) is None


def test_outcome_index_for():
    m = {"outcomes": '["Yes", "No"]'}
    assert sec.outcome_index_for(m, "Yes") == 0
    assert sec.outcome_index_for(m, "No") == 1
    fight = {"outcomes": ["Alex Perez", "Sumudaerji"]}
    assert sec.outcome_index_for(fight, "Sumudaerji") == 1
    assert sec.outcome_index_for({"outcomes": "[]"}, "anything") == 0  # default


# ── Sharp-book line selection ────────────────────────────────────────
def test_sharp_odds_prefers_pinnacle_over_soft_book():
    game = {"bookmakers": [
        {"key": "draftkings", "markets": [{"key": "h2h", "outcomes": [
            {"name": "A", "price": -150}, {"name": "B", "price": 130}]}]},
        {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
            {"name": "A", "price": -170}, {"name": "B", "price": 150}]}]},
    ]}
    odds = sec.sharp_odds_per_outcome(game, "h2h")
    assert odds == {"A": -170, "B": 150}  # pinnacle, not draftkings


def test_sharp_odds_falls_back_when_no_sharp_book():
    game = {"bookmakers": [
        {"key": "betonline", "markets": [{"key": "h2h", "outcomes": [
            {"name": "A", "price": -150}, {"name": "B", "price": 130}]}]},
    ]}
    odds = sec.sharp_odds_per_outcome(game, "h2h")
    assert odds == {"A": -150, "B": 130}


# ── Misc guards ──────────────────────────────────────────────────────
def test_valid_price_bounds():
    assert sec.VALID_PRICE(0.5)
    assert not sec.VALID_PRICE(0.0)
    assert not sec.VALID_PRICE(0.99)
    assert not sec.VALID_PRICE(0.01)


def test_is_stale_event():
    assert sec.is_stale_event("", 30) is True
    assert sec.is_stale_event("2020-01-01T00:00:00Z", 30) is True   # long past
    assert sec.is_stale_event("2030-01-01T00:00:00Z", 30) is False  # far future

"""Offline unit tests for the new soccer/UFC engine compute logic.

Pure functions only (no network). Synthetic payloads mirror real Polymarket Gamma
+ The Odds API structures probed 2026-06-02. Run:
    venv/bin/python -m pytest tests/test_sports_engines.py -v --noconftest
"""
import pytest

from odds import soccer_futures_edge as sfe
from odds import soccer_match_edge as sme
from odds import ufc_edge as ufc


# ── Soccer futures ───────────────────────────────────────────────────
def test_futures_maps_winner_market_and_signs_edge():
    poly = [{"id": "ev1", "title": "World Cup Winner", "markets": [
        {"question": "Will Brazil win the 2026 FIFA World Cup?",
         "outcomePrices": '["0.12", "0.88"]', "conditionId": "c1"},
        {"question": "Will France win the 2026 FIFA World Cup?",
         "outcomePrices": '["0.15", "0.85"]', "conditionId": "c2"},
    ]}]
    field = [{"name": "Brazil", "price": 450}, {"name": "France", "price": 500},
             {"name": "England", "price": 600}, {"name": "Spain", "price": 650}]
    edges = sfe.edges_from_field("World Cup Winner", field, poly, sfe.WC_CFG, min_edge=0.0)
    brazil = next((e for e in edges if e.participant == "Brazil"), None)
    assert brazil is not None
    assert brazil.poly_market_id == "c1"
    assert brazil.poly_price == pytest.approx(0.12)
    assert brazil.market_type == "outright"


def test_futures_rejects_resolved_price():
    poly = [{"id": "e", "title": "World Cup Winner", "markets": [
        {"question": "Will Brazil win the 2026 FIFA World Cup?",
         "outcomePrices": '["0.99", "0.01"]', "conditionId": "c1"}]}]
    field = [{"name": "Brazil", "price": -200}, {"name": "France", "price": 500}]
    edges = sfe.edges_from_field("World Cup Winner", field, poly, sfe.WC_CFG, min_edge=0.0)
    assert edges == []   # 0.99 fails VALID_PRICE


# ── Soccer per-match 3-way ───────────────────────────────────────────
def _match_game():
    return {"home_team": "New England Revolution", "away_team": "Houston Dynamo",
            "commence_time": "2030-03-07T00:00:00Z",
            "bookmakers": [{"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
                {"name": "New England Revolution", "price": 120},
                {"name": "Houston Dynamo", "price": 210},
                {"name": "Draw", "price": 230}]}]}]}


def _match_event():
    return {"id": "e", "title": "New England Revolution vs. Houston Dynamo", "markets": [
        {"question": "Will New England Revolution win on 2030-03-07?",
         "outcomes": '["Yes","No"]', "outcomePrices": '["0.40","0.60"]', "conditionId": "h"},
        {"question": "Will New England Revolution vs. Houston Dynamo end in a draw?",
         "outcomes": '["Yes","No"]', "outcomePrices": '["0.25","0.75"]', "conditionId": "d"},
        {"question": "Will Houston Dynamo win on 2030-03-07?",
         "outcomes": '["Yes","No"]', "outcomePrices": '["0.28","0.72"]', "conditionId": "a"}]}


def test_devig_three_way_sums_to_one_with_draw():
    outs = [{"name": "New England Revolution", "price": 120},
            {"name": "Houston Dynamo", "price": 210}, {"name": "Draw", "price": 230}]
    legs = sme.devig_three_way(outs)
    assert sum(legs.values()) == pytest.approx(1.0, abs=1e-9)
    assert "Draw" in legs


def test_map_legs_resolves_three_binaries():
    legs = sme.map_legs(_match_event(), "New England Revolution", "Houston Dynamo")
    assert legs["home"][0] == "h" and legs["draw"][0] == "d" and legs["away"][0] == "a"
    assert legs["home"][2] == 0  # YES is outcome index 0


def test_compute_match_edges_emits_legs_with_outcome_index():
    edges = sme.compute_match_edges(_match_game(), _match_event(), min_edge=0.0)
    types = {e.market_type for e in edges}
    assert {"home", "away", "draw"} & types
    for e in edges:
        assert hasattr(e, "_oi")
        assert e.book_prob > 0 and 0 <= e.poly_price <= 1


# ── UFC ──────────────────────────────────────────────────────────────
def _fight():
    return {"commence_time": "2030-01-01T00:00:00Z",
            "bookmakers": [{"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Alex Perez", "price": -150},
                {"name": "Sumudaerji", "price": 130}]}]}]}


def _fight_event():
    return {"id": "e", "title": "UFC Fight Night: Alex Perez vs. Sumudaerji (Flyweight, Main Card)",
            "markets": [
                {"question": "UFC Fight Night: Alex Perez vs. Sumudaerji (Flyweight, Main Card)",
                 "outcomes": '["Alex Perez","Sumudaerji"]', "outcomePrices": '["0.62","0.38"]', "conditionId": "c"},
                {"question": "Will Alex Perez win by KO or TKO?",
                 "outcomes": '["Yes","No"]', "outcomePrices": '["0.30","0.70"]', "conditionId": "p"}]}


def test_ml_market_maps_fighter_to_outcome_index():
    idx, cid, price = ufc.ml_market(_fight_event(), "Sumudaerji")
    assert idx == 1 and cid == "c" and price == pytest.approx(0.38)


def test_compute_ufc_edges_ml_and_prop_listing():
    edges = ufc.compute_ufc_edges(_fight(), _fight_event(), min_edge=0.03)
    ml = [e for e in edges if e.market_type == "moneyline"]
    props = [e for e in edges if e.market_type == "prop"]
    assert ml, "expected at least one moneyline edge"
    assert all(hasattr(e, "_oi") for e in ml)
    assert props and props[0].no_api_line is True and props[0].direction == "REVIEW"


# ── Outright field sharp-book preference (live-probe fix) ────────────
def test_extract_outright_prefers_betfair_exchange():
    from odds.odds_api_fetch import extract_outright_field
    raw = [{"bookmakers": [
        {"key": "draftkings", "markets": [{"key": "outrights",
            "outcomes": [{"name": "Brazil", "price": 800}]}]},
        {"key": "betfair_ex_eu", "markets": [{"key": "outrights",
            "outcomes": [{"name": "Brazil", "price": 850}, {"name": "France", "price": 500}]}]},
    ]}]
    field = extract_outright_field(raw)
    # Betfair exchange (sharper) preferred over draftkings
    assert len(field) == 2 and field[0]["name"] == "Brazil" and field[0]["price"] == 850


# ── Cutover regression: legacy soccer_edge fully removed ─────────────
def test_legacy_soccer_edge_decoupled():
    import importlib.util
    import odds
    import odds.the_odds_api as t
    assert importlib.util.find_spec("odds.soccer_edge") is None, "soccer_edge should be deleted"
    assert not hasattr(t, "get_soccer_edge_summary"), "the_odds_api soccer funcs should be gone"
    assert not hasattr(t, "find_soccer_edges")
    assert hasattr(t, "get_baseball_games_with_all_markets"), "baseball funcs must remain"

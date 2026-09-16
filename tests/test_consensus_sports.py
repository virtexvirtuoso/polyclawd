"""Tests for weighted consensus devig in sports_edge_common (soccer 3-way + UFC 2-way).

Run: venv/bin/python -m pytest tests/test_consensus_sports.py -v --noconftest
"""
from odds import sports_edge_common as sec


def _two_book_2way():
    return {"bookmakers": [
        {"key": "draftkings", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Fighter A", "price": -150}, {"name": "Fighter B", "price": 130}]}]},
        {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Fighter A", "price": -160}, {"name": "Fighter B", "price": 140}]}]},
    ]}


def _two_book_3way():
    return {"bookmakers": [
        {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Home", "price": 150}, {"name": "Draw", "price": 250}, {"name": "Away", "price": 200}]}]},
        {"key": "draftkings", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Home", "price": 140}, {"name": "Draw", "price": 240}, {"name": "Away", "price": 190}]}]},
    ]}


def test_consensus_2way_blends():
    con = sec.consensus_devig_2way(_two_book_2way())
    assert len(con) == 2
    assert abs(sum(con.values()) - 1.0) < 0.001
    # Pinnacle weight 0.35 > DK 0.20, so result should lean toward Pinnacle's devig
    pinn_a = sec.american_to_implied_prob(-160) / (sec.american_to_implied_prob(-160) + sec.american_to_implied_prob(140))
    dk_a = sec.american_to_implied_prob(-150) / (sec.american_to_implied_prob(-150) + sec.american_to_implied_prob(130))
    # Consensus should be between the two
    assert min(dk_a, pinn_a) <= con["Fighter A"] <= max(dk_a, pinn_a)


def test_consensus_2way_empty():
    assert sec.consensus_devig_2way({"bookmakers": []}) == {}


def test_consensus_2way_unknown_book():
    game = {"bookmakers": [
        {"key": "unknown_book", "markets": [{"key": "h2h", "outcomes": [
            {"name": "A", "price": -110}, {"name": "B", "price": -110}]}]},
    ]}
    assert sec.consensus_devig_2way(game) == {}


def test_consensus_3way_blends():
    con = sec.consensus_devig_3way(_two_book_3way())
    assert len(con) == 3
    assert abs(sum(con.values()) - 1.0) < 0.001
    # All three outcomes should be present
    assert "Home" in con and "Draw" in con and "Away" in con


def test_consensus_3way_empty():
    assert sec.consensus_devig_3way({"bookmakers": []}) == {}


def test_consensus_3way_needs_three_outcomes():
    game = {"bookmakers": [
        {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Home", "price": 150}, {"name": "Away", "price": 200}]}]},
    ]}
    assert sec.consensus_devig_3way(game) == {}


def test_consensus_best_odds_single_book():
    best = sec.consensus_best_odds(_two_book_2way())
    # Pinnacle has weight 0.35 > DK 0.20
    assert best == {"Fighter A": -160, "Fighter B": 140}


def test_consensus_bookmakers_string():
    # Verify the comma-separated bookmaker string includes our weighted books
    assert "pinnacle" in sec.CONSENSUS_BOOKMAKERS
    assert "draftkings" in sec.CONSENSUS_BOOKMAKERS
    assert "betfair_ex_eu" in sec.CONSENSUS_BOOKMAKERS

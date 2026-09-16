"""Test suite for weighted consensus devig in baseball_edge.

Replaces the old Phase R sharp-devig flag tests. The consensus approach devigs
per-book then weight-averages, eliminating the best-of-all vig collapse bug.

Run: venv/bin/python -m pytest tests/test_baseball_phase_r.py -v --noconftest
"""
from odds import baseball_edge as be


def _two_book_game():
    """DK (soft) + Pinnacle (sharp). Both have valid h2h lines that DISAGREE."""
    return {"bookmakers": [
        {"key": "draftkings", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Yankees", "price": -150}, {"name": "Red Sox", "price": 140}]}]},
        {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Yankees", "price": -170}, {"name": "Red Sox", "price": 150}]}]},
    ]}


def _three_book_game():
    """DK + Pinnacle + FanDuel. Mixed sharpness."""
    return {"bookmakers": [
        {"key": "draftkings", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Yankees", "price": -150}, {"name": "Red Sox", "price": 140}]}]},
        {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Yankees", "price": -170}, {"name": "Red Sox", "price": 150}]}]},
        {"key": "fanduel", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Yankees", "price": -155}, {"name": "Red Sox", "price": 135}]}]},
    ]}


def test_consensus_two_books():
    """Two books -> weight-blended average, between the two books' devigged values."""
    consensus = be._consensus_devig(_two_book_game())
    # DK: Yankees raw 0.600, Red Sox 0.417, vig 1.0167 -> devig 0.5902 / 0.4098
    # Pinn: Yankees raw 0.630, Red Sox 0.400, vig 1.0296 -> devig 0.6115 / 0.3885
    # Weights: pinnacle 0.35, draftkings 0.20 -> total 0.55
    expected_y = (0.35 * 0.6115 + 0.20 * 0.5902) / 0.55
    assert abs(consensus["Yankees"] - expected_y) < 0.01
    assert abs(consensus["Red Sox"] - (1.0 - expected_y)) < 0.01
    assert abs(consensus["Yankees"] + consensus["Red Sox"] - 1.0) < 0.001


def test_consensus_three_books():
    """Three books -> weighted average within the books' range."""
    consensus = be._consensus_devig(_three_book_game())
    y_probs = [0.5902, 0.6115]  # DK and Pinn (FD -155/135 falls between)
    assert min(y_probs) <= consensus["Yankees"] <= max(y_probs)
    assert abs(consensus["Yankees"] + consensus["Red Sox"] - 1.0) < 0.001


def test_no_books_returns_empty():
    """No bookmakers -> empty dict."""
    assert be._consensus_devig({"bookmakers": []}) == {}


def test_one_book_fallback():
    """Single weighted book -> uses its devigged line directly."""
    game = {"bookmakers": [
        {"key": "bovada", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Yanks", "price": -120}, {"name": "Sox", "price": 100}]}]},
    ]}
    consensus = be._consensus_devig(game)
    raw = be._american_to_implied_prob(-120) + be._american_to_implied_prob(100)
    expected = be._american_to_implied_prob(-120) / raw
    assert abs(consensus.get("Yanks", 0) - expected) < 0.001
    assert abs(consensus.get("Sox", 0) - (1.0 - expected)) < 0.001


def test_missing_team_skips_book():
    """Book with only one team's odds -> skipped (no partial data)."""
    game = {"bookmakers": [
        {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Yankees", "price": -170}]}]},
    ]}
    assert be._consensus_devig(game) == {}


def test_unknown_book_weight_is_zero():
    """Unknown book key -> weight 0 -> ignored -> insufficient data -> empty."""
    game = {"bookmakers": [
        {"key": "some_unknown_book", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Yankees", "price": -150}, {"name": "Red Sox", "price": 140}]}]},
    ]}
    assert be._consensus_devig(game) == {}


def test_consensus_blends_not_echoes_sharp():
    """The consensus must BLEND books, not echo the sharp book alone.

    The old single-sharp-book path returned Pinnacle's devig verbatim. The
    consensus must differ by a measurable amount because DraftKings is mixed in.
    """
    consensus = be._consensus_devig(_two_book_game())
    pinn_raw = be._american_to_implied_prob(-170) + be._american_to_implied_prob(150)
    pinn_only = be._american_to_implied_prob(-170) / pinn_raw
    assert abs(consensus["Yankees"] - pinn_only) > 0.003


def test_no_vig_collapse():
    """The best-of-all BUG cherry-picked the lowest implied prob per team across
    books, driving the cross-book overround to <=1.0. Per-book devig removes each
    book's vig individually before blending — consensus sums to 1.0 by construction
    while the old bug's raw overround was <1.0.
    """
    game = _two_book_game()
    # Reproduce OLD best-of-all: min implied prob per team
    raw = {}
    for bk in game["bookmakers"]:
        for o in bk["markets"][0]["outcomes"]:
            p = be._american_to_implied_prob(o["price"])
            raw[o["name"]] = min(raw.get(o["name"], 9.9), p)
    best_of_all_total = sum(raw.values())
    assert best_of_all_total <= 1.0 + 1e-9  # vig collapsed — phantom-edge source
    # Consensus is a proper distribution
    consensus = be._consensus_devig(game)
    assert abs(sum(consensus.values()) - 1.0) < 1e-6


def test_best_odds_per_team_same_book():
    """_best_odds_per_team now returns odds from a single book (highest-weighted),
    not cherry-picked across books."""
    game = _two_book_game()
    best = be._best_odds_per_team(game)
    # Pinnacle has weight 0.35 > DK 0.20, so both teams should come from Pinnacle
    assert best == {"Yankees": -170, "Red Sox": 150}

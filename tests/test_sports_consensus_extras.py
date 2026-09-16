"""Point-keyed consensus helpers for spreads & totals (sports_edge_common).

These back the baseball spread/total edge paths (and any future NBA/NFL spreads).
Run: venv/bin/python -m pytest tests/test_sports_consensus_extras.py -v --noconftest
"""

from odds import sports_edge_common as sec


# ── SPREADS ──────────────────────────────────────────────────────────────

def _two_book_spread():
    """Pinnacle (0.35) + DraftKings (0.20), both at ±1.5, disagreeing."""
    return {"bookmakers": [
        {"key": "pinnacle", "markets": [{"key": "spreads", "outcomes": [
            {"name": "Yankees", "price": -200, "point": 1.5},
            {"name": "Red Sox", "price": 170, "point": -1.5}]}]},
        {"key": "draftkings", "markets": [{"key": "spreads", "outcomes": [
            {"name": "Yankees", "price": -180, "point": 1.5},
            {"name": "Red Sox", "price": 160, "point": -1.5}]}]},
    ]}


def test_consensus_devig_spreads_blends_same_point():
    out = sec.consensus_devig_spreads(_two_book_spread())
    assert set(out.keys()) == {1.5}
    pa, _ = sec.devig_two_way(-200, 170)   # pinnacle Yankees cover prob
    da, _ = sec.devig_two_way(-180, 160)   # draftkings Yankees cover prob
    exp = (0.35 * pa + 0.20 * da) / 0.55
    assert abs(out[1.5]["Yankees"] - exp) < 1e-9
    assert abs(sum(out[1.5].values()) - 1.0) < 1e-9


def test_consensus_best_spread_odds_picks_highest_weighted():
    out = sec.consensus_best_spread_odds(_two_book_spread())
    assert out[1.5]["Yankees"] == (-200, 1.5)   # pinnacle, signed point preserved
    assert out[1.5]["Red Sox"] == (170, -1.5)


def test_spreads_single_sided_book_excluded():
    """A book quoting only one side of a |point| contributes nothing — no
    cross-book pairing. Consensus falls to the book with both sides."""
    game = {"bookmakers": [
        {"key": "pinnacle", "markets": [{"key": "spreads", "outcomes": [
            {"name": "Yankees", "price": -200, "point": 1.5}]}]},          # one side
        {"key": "draftkings", "markets": [{"key": "spreads", "outcomes": [
            {"name": "Yankees", "price": -180, "point": 1.5},
            {"name": "Red Sox", "price": 160, "point": -1.5}]}]},
    ]}
    out = sec.consensus_devig_spreads(game)
    da, _ = sec.devig_two_way(-180, 160)
    assert abs(out[1.5]["Yankees"] - da) < 1e-9                            # DK only
    assert sec.consensus_best_spread_odds(game)[1.5]["Yankees"] == (-180, 1.5)


def test_spreads_unknown_book_ignored():
    game = {"bookmakers": [{"key": "nobody", "markets": [{"key": "spreads", "outcomes": [
        {"name": "Yankees", "price": -200, "point": 1.5},
        {"name": "Red Sox", "price": 170, "point": -1.5}]}]}]}
    assert sec.consensus_devig_spreads(game) == {}
    assert sec.consensus_best_spread_odds(game) == {}


# ── TOTALS ───────────────────────────────────────────────────────────────

def _two_book_total():
    return {"bookmakers": [
        {"key": "pinnacle", "markets": [{"key": "totals", "outcomes": [
            {"name": "Over", "price": -110, "point": 8.5},
            {"name": "Under", "price": -110, "point": 8.5}]}]},
        {"key": "draftkings", "markets": [{"key": "totals", "outcomes": [
            {"name": "Over", "price": -105, "point": 8.5},
            {"name": "Under", "price": -115, "point": 8.5}]}]},
    ]}


def test_consensus_devig_totals_blends_same_point():
    out = sec.consensus_devig_totals(_two_book_total())
    assert set(out.keys()) == {8.5}
    pa, _ = sec.devig_two_way(-110, -110)
    da, _ = sec.devig_two_way(-105, -115)
    exp = (0.35 * pa + 0.20 * da) / 0.55
    assert abs(out[8.5]["Over"] - exp) < 1e-9
    assert abs(sum(out[8.5].values()) - 1.0) < 1e-9
    assert sec.consensus_best_total_odds(_two_book_total())[8.5] == (-110, -110)


def test_totals_distinct_points_not_blended():
    """Over 8.5 and Over 9.0 are different bets — never averaged together."""
    game = {"bookmakers": [
        {"key": "pinnacle", "markets": [{"key": "totals", "outcomes": [
            {"name": "Over", "price": -110, "point": 8.5},
            {"name": "Under", "price": -110, "point": 8.5}]}]},
        {"key": "draftkings", "markets": [{"key": "totals", "outcomes": [
            {"name": "Over", "price": -110, "point": 9.0},
            {"name": "Under", "price": -110, "point": 9.0}]}]},
    ]}
    out = sec.consensus_devig_totals(game)
    assert set(out.keys()) == {8.5, 9.0}


def test_totals_missing_side_skips_point():
    game = {"bookmakers": [
        {"key": "pinnacle", "markets": [{"key": "totals", "outcomes": [
            {"name": "Over", "price": -110, "point": 8.5}]}]},
    ]}
    assert sec.consensus_devig_totals(game) == {}

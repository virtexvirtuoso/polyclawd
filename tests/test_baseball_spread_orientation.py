"""Sign-aware Polymarket runline matching in _extract_spread_prices.

Polymarket lists runlines only as "Spread: <team> (-N)". The favorite's bet maps
to its own market; the underdog's +N bet maps to the NO side of the OPPONENT's
-N market. Matching on |point| alone (old bug) compared a +1.5 underdog against
the team's own -1.5 market — the opposite bet — inventing phantom edges.

Run: venv/bin/python -m pytest tests/test_baseball_spread_orientation.py -v --noconftest
"""

from odds import baseball_edge as be


def _event():
    # Both runline markets exist (favorite-named and underdog-named), as Polymarket
    # actually returns them. Padres-named market listed FIRST to exercise skip logic.
    return {"markets": [
        {"question": "Spread: Padres (-1.5)", "outcomes": '["Padres", "Phillies"]',
         "outcomePrices": '["0.235", "0.765"]', "conditionId": "PADRES_MINUS"},
        {"question": "Spread: Phillies (-1.5)", "outcomes": '["Phillies", "Padres"]',
         "outcomePrices": '["0.495", "0.505"]', "conditionId": "PHILLIES_MINUS"},
    ]}


def test_favorite_uses_own_negative_market():
    r = be._extract_spread_prices(_event(), "Phillies", -1.5)   # Phillies -1.5
    assert r is not None
    assert abs(r[0] - 0.495) < 1e-9                 # P(Phillies cover -1.5)
    assert r[2] == "PHILLIES_MINUS"


def test_underdog_uses_opponent_no_side():
    r = be._extract_spread_prices(_event(), "Padres", 1.5)      # Padres +1.5
    assert r is not None
    assert abs(r[0] - 0.505) < 1e-9                 # = NO of Phillies (-1.5)
    assert r[2] == "PHILLIES_MINUS"                 # opponent's market


def test_underdog_not_matched_to_own_negative_market():
    """The OLD bug returned Padres (-1.5) YES = 0.235 (win by 2+). Must NOT."""
    r = be._extract_spread_prices(_event(), "Padres", 1.5)
    assert abs(r[0] - 0.235) > 0.2


def test_complement_holds():
    """Favorite cover + underdog cover sum to 1 (no push on .5 lines)."""
    fav = be._extract_spread_prices(_event(), "Phillies", -1.5)[0]
    dog = be._extract_spread_prices(_event(), "Padres", 1.5)[0]
    assert abs(fav + dog - 1.0) < 1e-9


def test_no_matching_point_returns_none():
    assert be._extract_spread_prices(_event(), "Padres", 3.5) is None

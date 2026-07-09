"""Manifold leading-indicator -> shadow signal mapping (#3).

When Manifold's prob diverges from Polymarket's, we log a directional bet on the
RESOLVABLE Polymarket market (buy the side Manifold implies is underpriced),
tagged strategy="manifold_lead". Requires a polymarket condition_id.
"""

from services.manifold_shadow import _edge_to_signal


def _edge(**kw):
    base = {
        "polymarket_id": "0xCID", "polymarket_title": "Will X happen?",
        "polymarket_price": 40.0, "manifold_prob": 55.0,
        "edge_pct": 15.0, "direction": "YES",
    }
    base.update(kw)
    return base


def test_yes_edge_buys_yes_at_poly_price():
    s = _edge_to_signal(_edge())
    assert s["market_id"] == "0xCID"
    assert s["platform"] == "polymarket"
    assert s["side"] == "YES"
    assert s["price"] == 0.40          # poly YES price
    assert s["strategy"] == "manifold_lead"


def test_no_edge_buys_no_at_complement_price():
    s = _edge_to_signal(_edge(direction="NO", edge_pct=-12.0, polymarket_price=70.0))
    assert s["side"] == "NO"
    assert s["price"] == 0.30          # 1 - 0.70 (cost of the NO token)


def test_confidence_scales_with_abs_edge_and_caps():
    assert _edge_to_signal(_edge(edge_pct=0.0))["confidence"] == 0.5
    assert _edge_to_signal(_edge(edge_pct=999.0))["confidence"] <= 0.95

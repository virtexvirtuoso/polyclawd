"""Cross-platform arb -> directional buy-leg shadow signal mapping (#2).

Directional-leg convention (chosen 2026-06-20): log the underpriced YES we'd
buy on `buy_platform`, NOT a locked 2-leg spread. Resolution + poly_delta (when
the buy leg is Polymarket) then accrue via the existing shadow pipeline.
"""

from signals.cross_platform_arb import _arb_to_shadow_signal


def _opp(**kw):
    base = {
        "buy_platform": "polymarket", "poly_id": "0xPOLY", "kalshi_id": "KXKID",
        "poly_title": "Poly Title", "kalshi_title": "Kalshi Title",
        "buy_price": 42.0, "spread_pp": 5.0, "net_edge_pp": 3.2,
        "sell_platform": "kalshi", "min_volume": 1200,
    }
    base.update(kw)
    return base


def test_polymarket_buy_leg_maps_to_poly_market():
    s = _arb_to_shadow_signal(_opp())
    assert s["market_id"] == "0xPOLY"
    assert s["platform"] == "polymarket"
    assert s["market"] == "Poly Title"
    assert s["side"] == "YES"
    assert s["price"] == 0.42          # buy_price is stored x100
    assert s["strategy"] == "cross_platform_arb"
    assert s["volume"] == 1200


def test_kalshi_buy_leg_maps_to_kalshi_market():
    s = _arb_to_shadow_signal(_opp(buy_platform="kalshi", sell_platform="polymarket", buy_price=38.0))
    assert s["market_id"] == "KXKID"
    assert s["platform"] == "kalshi"
    assert s["market"] == "Kalshi Title"
    assert s["price"] == 0.38


def test_confidence_scales_with_net_edge_and_is_capped():
    assert _arb_to_shadow_signal(_opp(net_edge_pp=0.0))["confidence"] == 0.5
    assert _arb_to_shadow_signal(_opp(net_edge_pp=999.0))["confidence"] <= 0.95

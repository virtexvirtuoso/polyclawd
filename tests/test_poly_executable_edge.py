"""Tests for the shared executable-edge enrichment (poly_executable_edge).

Order books are monkeypatched so these run offline and deterministically.
Run from app root:  pytest tests/test_poly_executable_edge.py
"""
import pytest
from odds import polymarket_clob as clob
from odds import poly_executable_edge as pee


def _book(asks, bids):
    return clob.OrderBook(
        market_id="m", token_id="t", outcome="Yes",
        bids=[clob.OrderBookLevel(p, s) for p, s in bids],
        asks=[clob.OrderBookLevel(p, s) for p, s in asks],
        spread=round((asks[0][0] - bids[0][0]), 4),
        mid_price=round((asks[0][0] + bids[0][0]) / 2, 4),
        timestamp="t",
    )


def test_healthy_book_positive_edge(monkeypatch):
    # Deep book at 0.50; model says 0.55 -> real edge ~+0.05, tradeable.
    monkeypatch.setattr(clob, "get_orderbook",
                        lambda tid: _book(asks=[(0.50, 5000)], bids=[(0.49, 5000)]))
    r = pee.executable_edge(0.55, "YES", token_id="t", target_usd=100)
    assert r["available"] is True
    assert abs(r["executable_price"] - 0.50) < 1e-6
    assert r["executable_edge"] == pytest.approx(0.05, abs=1e-3)
    assert r["tradeable"] is True


def test_midpoint_edge_dies_after_slippage(monkeypatch):
    # THE point: looks like a buy vs a 0.52 last price, but the book's asks
    # start at 0.55 -> executable edge is NEGATIVE -> not tradeable.
    monkeypatch.setattr(clob, "get_orderbook",
                        lambda tid: _book(asks=[(0.55, 5000)], bids=[(0.50, 5000)]))
    r = pee.executable_edge(0.52, "YES", token_id="t", target_usd=100)
    assert r["available"] is True
    assert r["executable_edge"] < 0
    assert r["tradeable"] is False


def test_wide_spread_not_tradeable(monkeypatch):
    monkeypatch.setattr(clob, "get_orderbook",
                        lambda tid: _book(asks=[(0.70, 5000)], bids=[(0.40, 5000)]))
    r = pee.executable_edge(0.90, "YES", token_id="t", max_spread=0.05)
    assert r["available"] is True            # book existed
    assert r["tradeable"] is False           # spread too wide to act
    assert r["reason"].startswith("skip:wide_spread")


def test_thin_book_not_tradeable(monkeypatch):
    # Only $5 of depth; min_usd=15 -> not tradeable, but book was available.
    monkeypatch.setattr(clob, "get_orderbook",
                        lambda tid: _book(asks=[(0.50, 10)], bids=[(0.49, 10)]))
    r = pee.executable_edge(0.60, "YES", token_id="t", target_usd=100, min_usd=15)
    assert r["available"] is True
    assert r["tradeable"] is False
    assert r["reason"].startswith("skip:thin_book")


def test_no_book_falls_back_gracefully(monkeypatch):
    monkeypatch.setattr(clob, "get_orderbook", lambda tid: None)
    r = pee.executable_edge(0.60, "YES", token_id="t")
    assert r["available"] is False           # caller keeps its midpoint edge
    assert r["executable_edge"] is None
    assert r["tradeable"] is False


def test_price_move_computes_deltas(monkeypatch):
    # hourly closes over 6h: 0.40 -> ... -> 0.50 (last); prev hour 0.48
    hist = [{"close": c} for c in [0.40, 0.42, 0.45, 0.47, 0.48, 0.50]]
    monkeypatch.setattr(clob, "get_price_history", lambda tid, **k: hist)
    r = pee.poly_price_move(token_id="t")
    assert r["available"] is True
    assert r["last"] == 0.50
    assert r["move_1h_pp"] == pytest.approx(2.0, abs=1e-6)   # (0.50-0.48)*100
    assert r["move_6h_pp"] == pytest.approx(10.0, abs=1e-6)  # (0.50-0.40)*100
    assert r["n_points"] == 6


def test_price_move_no_history(monkeypatch):
    monkeypatch.setattr(clob, "get_price_history", lambda tid, **k: [])
    r = pee.poly_price_move(token_id="t")
    assert r["available"] is False

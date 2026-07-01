"""Offline unit tests for goalscorer prop POSITION SIZING (odds/scorer_sizing.py).

Pure / deterministic — no network, no API credits, PAPER-mode math only.
Run from app root:
    venv/bin/python -m pytest tests/test_scorer_sizing.py -q --noconftest

Spec coverage (prop-edge-system-spec.md §6 Step 4 + blindspots B14/B17):
  1. two correlated same-match edges are sized as a cluster (each ~1/sqrt(2)
     haircut), NOT summed as two independent Kelly bets             [B14]
  2. per-match exposure cap binds (3 big legs in one match → cap)   [B14]
  3. per-book stake cap binds: final = min(kelly, cap)              [B17]
  4. daily cap binds across matches (whole-slate scale-down)
  5. non-tradeable edges get nothing
  6. a single uncorrelated edge sizes to plain fractional Kelly under the caps
"""

import math

import pytest

from odds.scorer_edge import ScorerEdge
from odds.scorer_sizing import (
    SizingConfig,
    SizedBet,
    size_slate,
    _raw_kelly_fraction,
    _to_decimal_odds,
)


# ── helpers ───────────────────────────────────────────────────────────────────
def _edge(
    event_title: str,
    player: str,
    consensus_fair: float,
    best_soft_price: float,
    tradeable: bool = True,
    edge_pct: float = 6.0,
    best_soft_book: str = "draftkings",
) -> ScorerEdge:
    """A minimal sized-ready ScorerEdge. Only the fields the sizer reads matter:
    event_title (cluster key), player, consensus_fair (p), best_soft_price
    (→ decimal odds), tradeable."""
    return ScorerEdge(
        sport_key="soccer_fifa_world_cup",
        event_title=event_title,
        commence_time="2026-06-20T13:00:00Z",
        player=player,
        consensus_fair=consensus_fair,
        best_soft_book=best_soft_book,
        best_soft_price=best_soft_price,
        best_soft_implied=1.0 / _to_decimal_odds(best_soft_price),
        edge_pct=edge_pct,
        player_confirmed_starting=tradeable or None,
        tradeable=tradeable,
    )


def _expected_kelly_stake(p, price, kelly_fraction, bankroll, haircut=1.0):
    d = _to_decimal_odds(price)
    f = _raw_kelly_fraction(p, d)
    return f * kelly_fraction * bankroll * haircut


# ── primitives ──────────────────────────────────────────────────────────────────
def test_decimal_odds_conversion():
    assert _to_decimal_odds(150) == pytest.approx(2.5)  # +150 american
    assert _to_decimal_odds(-200) == pytest.approx(1.5)  # -200 american
    assert _to_decimal_odds(2.5) == pytest.approx(2.5)  # decimal passthrough
    assert _to_decimal_odds(None) is None
    assert _to_decimal_odds(1.0) is None  # invalid decimal


def test_raw_kelly_clamps_negative_to_zero():
    # fair below the break-even implied prob → negative edge → f=0, never staked.
    d = 2.0  # implied 0.50
    assert _raw_kelly_fraction(0.40, d) == 0.0
    # positive edge → positive f.
    assert _raw_kelly_fraction(0.60, d) > 0.0


# ── (6) single uncorrelated edge → plain fractional Kelly under the caps ─────────
def test_single_edge_plain_fractional_kelly():
    cfg = SizingConfig(
        bankroll=10_000.0,
        kelly_fraction=0.5,
        per_match_cap_pct=1.0,  # relax the match cap so Kelly is the binding one
        per_book_max_stake=10_000.0,  # relax the per-book cap
        daily_cap_pct=1.0,  # relax the daily cap
    )
    # p=0.40 fair, price +200 (decimal 3.0, implied 0.333) → real edge.
    edge = _edge("Brazil vs Spain", "haaland", consensus_fair=0.40, best_soft_price=200)
    out = size_slate([edge], cfg)

    assert len(out) == 1
    bet = out[0]
    # n=1 → haircut is exactly 1.0 (recovers plain Kelly).
    assert bet.correlation_haircut == pytest.approx(1.0)
    expected = _expected_kelly_stake(0.40, 200, 0.5, 10_000.0)
    assert bet.stake == pytest.approx(expected)
    assert bet.binding_constraint == "kelly"
    # sanity: f = (2*0.4 - 0.6)/2 = 0.10 ; half-Kelly stake = 0.05 * 10k = 500.
    assert bet.raw_kelly_fraction == pytest.approx(0.10)
    assert bet.stake == pytest.approx(500.0)


# ── (1) two correlated same-match edges sized as a cluster, NOT summed ───────────
def test_two_correlated_legs_get_sqrt_n_haircut():
    cfg = SizingConfig(
        bankroll=10_000.0,
        kelly_fraction=0.5,
        per_match_cap_pct=1.0,  # relax match cap → isolate the haircut effect
        per_book_max_stake=10_000.0,  # relax per-book cap
        daily_cap_pct=1.0,
    )
    e1 = _edge("Brazil vs Spain", "haaland", 0.40, 200)
    e2 = _edge("Brazil vs Spain", "mbappe", 0.40, 200)
    out = size_slate([e1, e2], cfg)

    assert len(out) == 2
    haircut = 1.0 / math.sqrt(2)
    for bet in out:
        assert bet.correlation_haircut == pytest.approx(haircut)
        expected = _expected_kelly_stake(0.40, 200, 0.5, 10_000.0, haircut=haircut)
        assert bet.stake == pytest.approx(expected)
        assert bet.binding_constraint == "kelly_haircut"

    # KEY B14 ASSERTION: the cluster is NOT sized as 2 independent full-Kelly bets.
    independent_total = 2 * _expected_kelly_stake(0.40, 200, 0.5, 10_000.0)
    cluster_total = sum(b.stake for b in out)
    assert cluster_total < independent_total
    # specifically: cluster total == independent_per_leg * 2 * (1/sqrt2) == *sqrt2.
    one_leg = _expected_kelly_stake(0.40, 200, 0.5, 10_000.0)
    assert cluster_total == pytest.approx(one_leg * math.sqrt(2))


# ── (2) per-match cap binds: 3 big legs in one match scaled to the cap ───────────
def test_per_match_cap_binds():
    cfg = SizingConfig(
        bankroll=10_000.0,
        kelly_fraction=0.5,
        per_match_cap_pct=0.03,  # cap = $300 for the whole fixture
        per_book_max_stake=10_000.0,  # relax per-book so the MATCH cap is the binder
        daily_cap_pct=1.0,
    )
    # 3 strong edges on one match; even after the 1/sqrt(3) haircut they'd exceed $300.
    e1 = _edge("Brazil vs Spain", "haaland", 0.55, 200)
    e2 = _edge("Brazil vs Spain", "mbappe", 0.55, 200)
    e3 = _edge("Brazil vs Spain", "vinicius", 0.55, 200)
    out = size_slate([e1, e2, e3], cfg)

    assert len(out) == 3
    match_total = sum(b.stake for b in out)
    assert match_total == pytest.approx(0.03 * 10_000.0)  # exactly the cap, $300
    assert all(b.binding_constraint == "per_match_cap" for b in out)
    # identical legs → equal split, $100 each.
    for b in out:
        assert b.stake == pytest.approx(100.0)


# ── (3) per-book stake cap binds: final = min(kelly, cap) ─────────────────────────
def test_per_book_cap_binds():
    cfg = SizingConfig(
        bankroll=10_000.0,
        kelly_fraction=0.5,
        per_match_cap_pct=1.0,  # relax match cap
        per_book_max_stake=200.0,  # the binding ceiling (B17)
        daily_cap_pct=1.0,
    )
    # Big single-leg Kelly: p=0.55, +200 → f=(2*.55-.45)/2=0.325, half-Kelly stake
    # = 0.1625*10k = $1625, far above the $200 per-book cap.
    edge = _edge("Brazil vs Spain", "haaland", 0.55, 200)
    out = size_slate([edge], cfg)

    assert len(out) == 1
    bet = out[0]
    assert bet.stake == pytest.approx(200.0)  # min(kelly=1625, cap=200) → 200
    assert bet.binding_constraint == "per_book_cap"
    # the un-capped fractional Kelly is recorded for audit and is much larger.
    assert bet.fractional_kelly_stake == pytest.approx(1625.0)


# ── (4) daily cap binds across matches ────────────────────────────────────────────
def test_daily_cap_binds_across_matches():
    cfg = SizingConfig(
        bankroll=10_000.0,
        kelly_fraction=0.5,
        per_match_cap_pct=1.0,  # relax match cap
        per_book_max_stake=200.0,  # each leg caps at $200
        daily_cap_pct=0.05,  # daily cap = $500 total
    )
    # 5 single-leg matches, each capped at $200 → pre-daily total $1000 > $500.
    edges = [
        _edge(f"Match {i} home vs away", f"player{i}", 0.55, 200) for i in range(5)
    ]
    out = size_slate(edges, cfg)

    assert len(out) == 5
    slate_total = sum(b.stake for b in out)
    assert slate_total == pytest.approx(0.05 * 10_000.0)  # exactly $500
    assert all(b.binding_constraint == "daily_cap" for b in out)
    # proportional scale-down of equal $200 legs → $100 each.
    for b in out:
        assert b.stake == pytest.approx(100.0)


# ── (5) non-tradeable edges get nothing ──────────────────────────────────────────
def test_non_tradeable_edges_skipped():
    cfg = SizingConfig()
    tradeable = _edge("Brazil vs Spain", "haaland", 0.40, 200, tradeable=True)
    speculative = _edge("Brazil vs Spain", "mbappe", 0.40, 200, tradeable=False)
    out = size_slate([tradeable, speculative], cfg)

    assert len(out) == 1
    assert out[0].player == "haaland"
    # an all-speculative slate sizes to nothing.
    assert size_slate([speculative], cfg) == []


def test_empty_slate():
    assert size_slate([], SizingConfig()) == []

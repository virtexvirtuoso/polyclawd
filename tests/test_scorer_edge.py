"""Offline unit tests for the goalscorer prop edge engine (odds/scorer_edge.py).

Pure functions only — no network, no API credits. Synthetic fixtures mirror The
Odds API /events/{id}/odds shape (oddsFormat=american, YES-only scorer outcomes).
Run from app root:
    venv/bin/python -m pytest tests/test_scorer_edge.py -q --noconftest

Spec coverage (prop-edge-system-spec.md §3 + §4):
  1. consensus is Betfair-weighted, not Pinnacle-only
  2. the 0.958 haircut hits the fair anchor but NOT the soft price
  3. edge = consensus_fair - soft_raw
  4. in-play/started events are skipped
  5. Pinnacle-absent still produces a consensus from Betfair
  6. YES-only data (no NO outcome) is handled
  7. lineup gate: confirmed False -> not tradeable; None -> shown but not tradeable
"""

from datetime import datetime, timedelta, timezone

import pytest

from odds import scorer_edge as se


# ── helpers ───────────────────────────────────────────────────────────────────
def _scorer_market(player_prices: dict) -> dict:
    """A player_goal_scorer_anytime market for one book. `player_prices` maps
    player display name -> american YES price. YES-only (no NO outcome)."""
    return {
        "key": "player_goal_scorer_anytime",
        "outcomes": [{"name": "Yes", "description": player, "price": price} for player, price in player_prices.items()],
    }


def _event(commence: str, books: dict) -> dict:
    """The-Odds-API event-odds shape. `books` maps book_key -> {player: price}."""
    return {
        "id": "evt-1",
        "home_team": "Brazil",
        "away_team": "Spain",
        "commence_time": commence,
        "bookmakers": [{"key": bkey, "markets": [_scorer_market(pp)]} for bkey, pp in books.items()],
    }


# Fixed "now" so the kickoff/tradeable logic is deterministic.
NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


def _iso(hours_from_now: float) -> str:
    return (NOW + timedelta(hours=hours_from_now)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cfg(**kw):
    base = dict(sport_key="soccer_fifa_world_cup", min_edge_pp=5.0)
    base.update(kw)
    return se.ScorerSportConfig(**base)


# ── price / canon primitives ──────────────────────────────────────────────────
def test_american_to_implied_prob():
    assert se.american_to_implied_prob(-200) == pytest.approx(0.6667, abs=1e-3)
    assert se.american_to_implied_prob(150) == pytest.approx(0.40, abs=1e-3)


def test_to_implied_auto_detects_decimal():
    # abs < 100 → decimal path: 1/2.5 = 0.40
    assert se.to_implied(2.5) == pytest.approx(0.40, abs=1e-9)
    # american path
    assert se.to_implied(-200) == pytest.approx(0.6667, abs=1e-3)
    assert se.to_implied(None) is None


def test_canonical_player_strips_accents_and_initials():
    assert se._canonical_player("Vinícius Júnior") == "vinicius junior"
    assert se._canonical_player("E. Haaland") == "haaland"
    assert se._canonical_player("Kylian Mbappé") == "kylian mbappe"


# ── (1) consensus is Betfair-weighted, not Pinnacle-only ──────────────────────
def test_consensus_is_betfair_weighted_not_pinnacle():
    # Betfair (0.40+0.40 weight) at +233 (~0.30); Pinnacle (0.15) at +100 (0.50).
    # Betfair-weighted mean ≈ 0.332. A Pinnacle-only anchor would read 0.50 —
    # the assertions below prove the engine uses the former, not the latter.
    ev = _event(
        _iso(4),
        {
            "betfair_ex_uk": {"Haaland": 233},  # ~0.300
            "betfair_ex_eu": {"Haaland": 233},
            "pinnacle": {"Haaland": 100},  # 0.50
            "draftkings": {"Haaland": 233},
        },
    )
    edges = se.find_scorer_edges([ev], _cfg(min_edge_pp=-100.0), now=NOW)
    e = next(x for x in edges if x.player == "haaland")
    bf_imp = se.american_to_implied_prob(233)
    pin_imp = se.american_to_implied_prob(100)
    expected_raw = (0.40 * bf_imp + 0.40 * bf_imp + 0.15 * pin_imp) / 0.95
    expected_fair = expected_raw * se.GOALSCORER_YES_HAIRCUT
    assert e.consensus_fair == pytest.approx(expected_fair, abs=1e-6)
    # NOT the Pinnacle-only value
    assert e.consensus_fair != pytest.approx(pin_imp * se.GOALSCORER_YES_HAIRCUT, abs=1e-3)


# ── (2) haircut applies to fair only, NOT the soft price ──────────────────────
def test_haircut_applies_to_fair_not_soft():
    ev = _event(
        _iso(4),
        {
            "betfair_ex_uk": {"Haaland": -110},
            "betfair_ex_eu": {"Haaland": -110},
            "draftkings": {"Haaland": 120},
        },
    )
    edges = se.find_scorer_edges([ev], _cfg(min_edge_pp=-100.0), now=NOW)
    e = next(x for x in edges if x.player == "haaland")
    raw_consensus = se.american_to_implied_prob(-110)  # only Betfair present
    soft_raw = se.american_to_implied_prob(120)
    # fair is haircut
    assert e.consensus_fair == pytest.approx(raw_consensus * se.GOALSCORER_YES_HAIRCUT, abs=1e-9)
    assert e.consensus_fair < raw_consensus  # strictly reduced by haircut
    # soft stays RAW (no haircut)
    assert e.best_soft_implied == pytest.approx(soft_raw, abs=1e-9)


# ── (3) edge = consensus_fair - soft_raw ──────────────────────────────────────
def test_edge_is_fair_minus_soft_raw():
    ev = _event(
        _iso(4),
        {
            "betfair_ex_uk": {"Haaland": -150},
            "betfair_ex_eu": {"Haaland": -150},
            "draftkings": {"Haaland": 200},
        },
    )
    edges = se.find_scorer_edges([ev], _cfg(min_edge_pp=-100.0), now=NOW)
    e = next(x for x in edges if x.player == "haaland")
    fair = se.american_to_implied_prob(-150) * se.GOALSCORER_YES_HAIRCUT
    soft = se.american_to_implied_prob(200)
    assert e.edge_pct == pytest.approx((fair - soft) * 100.0, abs=1e-9)


def test_min_edge_threshold_filters():
    # Tight prices → small/negative edge, filtered out at default min_edge_pp=5.0.
    ev = _event(
        _iso(4),
        {
            "betfair_ex_uk": {"Haaland": 100},
            "betfair_ex_eu": {"Haaland": 100},
            "draftkings": {"Haaland": 100},
        },
    )
    assert se.find_scorer_edges([ev], _cfg(), now=NOW) == []


# ── (4) in-play / started events are skipped ──────────────────────────────────
def test_started_event_is_skipped():
    started = _event(
        _iso(-1.0),  # kicked off an hour ago
        {
            "betfair_ex_uk": {"Haaland": -150},
            "betfair_ex_eu": {"Haaland": -150},
            "draftkings": {"Haaland": 200},
        },
    )
    assert se.find_scorer_edges([started], _cfg(min_edge_pp=-100.0), now=NOW) == []


def test_within_buffer_is_skipped():
    soon = _event(
        _iso(0.05),  # 3 minutes out
        {
            "betfair_ex_uk": {"Haaland": -150},
            "betfair_ex_eu": {"Haaland": -150},
            "draftkings": {"Haaland": 200},
        },
    )
    out = se.find_scorer_edges([soon], _cfg(min_edge_pp=-100.0), now=NOW, started_buffer_minutes=5.0)
    assert out == []


# ── (5) Pinnacle-absent still produces a consensus from Betfair ───────────────
def test_pinnacle_absent_still_yields_consensus():
    ev = _event(
        _iso(4),
        {
            "betfair_ex_uk": {"Haaland": -150},
            "betfair_ex_eu": {"Haaland": -150},
            # no pinnacle, no williamhill
            "draftkings": {"Haaland": 200},
        },
    )
    edges = se.find_scorer_edges([ev], _cfg(min_edge_pp=-100.0), now=NOW)
    e = next(x for x in edges if x.player == "haaland")
    # consensus = Betfair-only mean, renormalized over present weights
    assert e.consensus_fair == pytest.approx(se.american_to_implied_prob(-150) * se.GOALSCORER_YES_HAIRCUT, abs=1e-9)


def test_no_sharp_book_skips_player():
    # Only a soft book present → no consensus anchor → skip.
    ev = _event(_iso(4), {"draftkings": {"Haaland": 200}})
    assert se.find_scorer_edges([ev], _cfg(min_edge_pp=-100.0), now=NOW) == []


# ── (6) YES-only data (no NO outcome) is handled ──────────────────────────────
def test_yes_only_data_handled_and_no_side_ignored():
    # Mix a stray NO outcome into the market; it must be ignored (no two-way devig).
    ev = {
        "id": "evt-yo",
        "home_team": "Brazil",
        "away_team": "Spain",
        "commence_time": _iso(4),
        "bookmakers": [
            {
                "key": "betfair_ex_uk",
                "markets": [
                    {
                        "key": "player_goal_scorer_anytime",
                        "outcomes": [
                            {"name": "Yes", "description": "Haaland", "price": -150},
                            # a malformed/NO row that must be skipped:
                            {"name": "No", "description": "Haaland", "price": 120},
                        ],
                    }
                ],
            },
            {
                "key": "betfair_ex_eu",
                "markets": [_scorer_market({"Haaland": -150})],
            },
            {"key": "draftkings", "markets": [_scorer_market({"Haaland": 200})]},
        ],
    }
    edges = se.find_scorer_edges([ev], _cfg(min_edge_pp=-100.0), now=NOW)
    e = next(x for x in edges if x.player == "haaland")
    # Consensus uses the RAW YES implied only — the NO row is ignored (no devig).
    assert e.consensus_fair == pytest.approx(se.american_to_implied_prob(-150) * se.GOALSCORER_YES_HAIRCUT, abs=1e-9)


def test_player_in_name_field_without_description():
    # Some feeds put the player directly in `name` with no side label.
    ev = {
        "id": "evt-nn",
        "home_team": "Brazil",
        "away_team": "Spain",
        "commence_time": _iso(4),
        "bookmakers": [
            {
                "key": "betfair_ex_uk",
                "markets": [
                    {
                        "key": "player_goal_scorer_anytime",
                        "outcomes": [{"name": "Haaland", "price": -150}],
                    }
                ],
            },
            {
                "key": "betfair_ex_eu",
                "markets": [{"key": "player_goal_scorer_anytime", "outcomes": [{"name": "Haaland", "price": -150}]}],
            },
            {
                "key": "draftkings",
                "markets": [{"key": "player_goal_scorer_anytime", "outcomes": [{"name": "Haaland", "price": 200}]}],
            },
        ],
    }
    edges = se.find_scorer_edges([ev], _cfg(min_edge_pp=-100.0), now=NOW)
    assert any(e.player == "haaland" for e in edges)


# ── (7) lineup gate ───────────────────────────────────────────────────────────
def _edge_event():
    return _event(
        _iso(0.5),  # 30 min out → inside the 1.0h tradeable window
        {
            "betfair_ex_uk": {"Haaland": -150},
            "betfair_ex_eu": {"Haaland": -150},
            "draftkings": {"Haaland": 200},
        },
    )


def test_lineup_confirmed_false_not_tradeable():
    edges = se.find_scorer_edges(
        [_edge_event()],
        _cfg(min_edge_pp=-100.0),
        lineup_checker=lambda p, ev: False,
        now=NOW,
    )
    e = next(x for x in edges if x.player == "haaland")
    assert e.player_confirmed_starting is False
    assert e.tradeable is False  # confirmed benched → never tradeable


def test_lineup_none_shown_but_not_tradeable():
    # Default stub returns None (unchecked) → edge is shown, marked speculative.
    edges = se.find_scorer_edges([_edge_event()], _cfg(min_edge_pp=-100.0), now=NOW)
    e = next(x for x in edges if x.player == "haaland")
    assert e.player_confirmed_starting is None
    assert e.tradeable is False  # spec §4.3: unchecked is not tradeable


def test_lineup_confirmed_true_inside_window_is_tradeable():
    edges = se.find_scorer_edges(
        [_edge_event()],
        _cfg(min_edge_pp=-100.0),
        lineup_checker=lambda p, ev: True,
        now=NOW,
    )
    e = next(x for x in edges if x.player == "haaland")
    assert e.player_confirmed_starting is True
    assert e.tradeable is True


def test_confirmed_true_outside_window_not_tradeable():
    # Confirmed starting but 4h out (> 1.0h tradeable window) → speculative.
    far = _event(
        _iso(4.0),
        {
            "betfair_ex_uk": {"Haaland": -150},
            "betfair_ex_eu": {"Haaland": -150},
            "draftkings": {"Haaland": 200},
        },
    )
    edges = se.find_scorer_edges([far], _cfg(min_edge_pp=-100.0), lineup_checker=lambda p, ev: True, now=NOW)
    e = next(x for x in edges if x.player == "haaland")
    assert e.player_confirmed_starting is True
    assert e.tradeable is False


# ── CLV fields default to None (filled by the close snapshot, not here) ───────
def test_clv_fields_default_none():
    edges = se.find_scorer_edges([_edge_event()], _cfg(min_edge_pp=-100.0), now=NOW)
    e = edges[0]
    assert e.soft_close_implied is None and e.clv_soft_move_pp is None

"""End-to-end integration test for the goalscorer PAPER pipeline.

Proves the validated modules compose: synthetic event-odds → find_scorer_edges
→ size_slate → record_positions → resolve_open_positions (injected resolver) →
portfolio P&L. No network, no real money. Run with --noconftest (the repo
conftest imports the full FastAPI app).
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from odds.scorer_edge import ScorerSportConfig, find_scorer_edges  # noqa: E402
from odds.scorer_sizing import SizingConfig, size_slate  # noqa: E402
from signals import scorer_paper_portfolio as pp  # noqa: E402
from signals import scorer_resolution as sr  # noqa: E402

NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
KICKOFF = (NOW + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")  # in-window, not started


def _event():
    """One WC match, YES-only goalscorer props. Striker A has a real edge
    (Betfair-weighted consensus ~0.46 → haircut ~0.44 vs DraftKings ~0.33)."""

    def yes(player, price):
        return {"name": "Yes", "description": player, "price": price}

    return {
        "id": "evt1",
        "home_team": "Brazil",
        "away_team": "Morocco",
        "commence_time": KICKOFF,
        "bookmakers": [
            {
                "key": "betfair_ex_uk",
                "markets": [
                    {"key": "player_goal_scorer_anytime", "outcomes": [yes("Striker A", 120), yes("Striker B", 400)]}
                ],
            },
            {
                "key": "pinnacle",
                "markets": [
                    {"key": "player_goal_scorer_anytime", "outcomes": [yes("Striker A", 110), yes("Striker B", 380)]}
                ],
            },
            {
                "key": "draftkings",
                "markets": [
                    {"key": "player_goal_scorer_anytime", "outcomes": [yes("Striker A", 200), yes("Striker B", 410)]}
                ],
            },
        ],
    }


def test_scan_to_record_to_resolve():
    edges = find_scorer_edges(
        [_event()], ScorerSportConfig(sport_key="soccer_fifa_world_cup"), lineup_checker=lambda *a, **k: True, now=NOW
    )
    # Striker A flags an edge; with a confirmed lineup inside the 1h window it's tradeable.
    tradeable = [e for e in edges if e.tradeable]
    assert tradeable, "expected at least one tradeable edge (Striker A)"
    assert any(e.player == "striker a" or e.player == "Striker A" or "striker a" in e.player.lower() for e in tradeable)

    sized = size_slate(edges, SizingConfig(bankroll=10000.0))
    assert sized and all(s.stake > 0 for s in sized)

    db = pp.db_connect(":memory:")
    n = pp.record_positions(sized, db)
    assert n == len(sized)

    # inject a resolver that says Striker A scored → won
    def resolver(pos):
        return sr.ResolveState.YES

    settled = pp.resolve_open_positions(db, resolver)
    assert settled == n

    rep = pp.portfolio_report(db)
    assert rep["settled"] == n
    assert rep["won"] == n
    assert rep["paper_pnl"] > 0  # winning paper bets → positive P&L


def test_no_tradeable_when_lineup_unchecked():
    # default lineup_checker=None → unchecked → not tradeable → nothing sized
    edges = find_scorer_edges([_event()], ScorerSportConfig(sport_key="soccer_fifa_world_cup"), now=NOW)
    assert all(not e.tradeable for e in edges)
    sized = size_slate(edges, SizingConfig(bankroll=10000.0))
    assert sized == []

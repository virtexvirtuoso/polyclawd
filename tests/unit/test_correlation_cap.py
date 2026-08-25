"""Tests for tiered portfolio correlation cap (2026-04-23 redesign)."""
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add signals/ to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "signals"))

from paper_portfolio import (
    _check_correlation_cap,
    _init_tables,
    get_correlation_status,
    CORRELATION_GROUPS,
    MAX_PER_GROUP,
    SOFT_CAP,
    HARD_CAP,
    STRONG_EDGE_PP,
    DISPLACE_BUFFER_PP,
)


@pytest.fixture
def mem_db():
    """In-memory SQLite DB with paper_positions table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _init_tables(conn)
    return conn


def _insert_open(conn, archetype, n=1, edge_pct=10.0):
    for i in range(n):
        conn.execute(
            "INSERT INTO paper_positions "
            "(opened_at, market_id, market_title, side, entry_price, bet_size, "
            " status, archetype, edge_pct) "
            "VALUES (datetime('now'), ?, ?, 'NO', 0.5, 100, 'open', ?, ?)",
            (f"mkt-{archetype}-{i}", f"Test {archetype} {i}", archetype, edge_pct),
        )
    conn.commit()


class TestTieredCap:
    def test_empty_portfolio_allows(self, mem_db):
        decision, info, _ = _check_correlation_cap("weather", mem_db, new_edge_pp=0)
        assert decision == "allow"
        assert info is None

    def test_below_soft_cap_allows(self, mem_db):
        _insert_open(mem_db, "weather", n=SOFT_CAP - 1, edge_pct=20)
        decision, info, _ = _check_correlation_cap("weather", mem_db, new_edge_pp=10)
        assert decision == "allow"
        assert info is None

    def test_at_soft_cap_weak_signal_blocks(self, mem_db):
        # 10 open at 20pp edge each, new signal at 8pp is weak → block
        _insert_open(mem_db, "weather", n=SOFT_CAP, edge_pct=20)
        decision, info, reason = _check_correlation_cap("weather", mem_db, new_edge_pp=8)
        assert decision == "block"
        assert "insufficient" in reason.lower() or "cap" in reason.lower()

    def test_at_soft_cap_strong_uniform_book_allows_over_cap(self, mem_db):
        # All open positions strong, new signal strong → allow as 11th
        _insert_open(mem_db, "weather", n=SOFT_CAP, edge_pct=20)
        decision, info, reason = _check_correlation_cap(
            "weather", mem_db, new_edge_pp=STRONG_EDGE_PP + 2
        )
        assert decision == "allow"
        assert info is None
        assert reason is not None and "Over-soft-cap" in reason

    def test_at_soft_cap_strong_signal_but_weak_book_displaces(self, mem_db):
        # Book has one weak position (5pp) — new strong signal displaces it
        _insert_open(mem_db, "weather", n=SOFT_CAP - 1, edge_pct=20)
        _insert_open(mem_db, "weather", n=1, edge_pct=5.0)  # the weak one
        decision, info, reason = _check_correlation_cap(
            "weather", mem_db, new_edge_pp=STRONG_EDGE_PP + 5
        )
        assert decision == "displace"
        assert info is not None
        assert info["edge_pct"] == 5.0

    def test_displacement_requires_buffer(self, mem_db):
        # Weakest is 10pp, new is 12pp — inside 5pp buffer → block not displace
        _insert_open(mem_db, "weather", n=SOFT_CAP, edge_pct=10)
        decision, info, reason = _check_correlation_cap(
            "weather", mem_db, new_edge_pp=12
        )
        assert decision == "block"

    def test_hard_cap_blocks_even_with_strong_edge(self, mem_db):
        # At hard cap (11), even strongest possible signal blocks
        _insert_open(mem_db, "weather", n=HARD_CAP, edge_pct=50)
        decision, info, reason = _check_correlation_cap(
            "weather", mem_db, new_edge_pp=50
        )
        assert decision == "block"
        assert "Hard cap" in reason

    def test_different_group_unaffected(self, mem_db):
        _insert_open(mem_db, "weather", n=SOFT_CAP, edge_pct=20)
        decision, info, _ = _check_correlation_cap("sports_winner", mem_db, new_edge_pp=10)
        assert decision == "allow"

    def test_closed_positions_dont_count(self, mem_db):
        _insert_open(mem_db, "weather", n=SOFT_CAP, edge_pct=20)
        mem_db.execute(
            "UPDATE paper_positions SET status='won' WHERE market_id='mkt-weather-0'"
        )
        mem_db.commit()
        decision, info, _ = _check_correlation_cap("weather", mem_db, new_edge_pp=10)
        assert decision == "allow"  # only 9 open, below soft cap

    def test_displaced_positions_dont_count(self, mem_db):
        """Displaced positions are treated like closed — they free their slot."""
        _insert_open(mem_db, "weather", n=SOFT_CAP, edge_pct=20)
        mem_db.execute(
            "UPDATE paper_positions SET status='displaced' "
            "WHERE market_id='mkt-weather-0'"
        )
        mem_db.commit()
        decision, info, _ = _check_correlation_cap("weather", mem_db, new_edge_pp=10)
        assert decision == "allow"

    def test_unknown_archetype_maps_to_other(self, mem_db):
        decision, info, _ = _check_correlation_cap(
            "totally_unknown", mem_db, new_edge_pp=0
        )
        assert decision == "allow"

    def test_all_archetypes_have_groups(self):
        """Every known archetype should map to a group."""
        known = [
            "daily_updown", "intraday_updown", "parlay", "price_above",
            "price_range", "directional", "financial_price", "ai_model",
            "geopolitical", "election", "sports_single_game", "game_total",
            "entertainment", "social_count", "deadline_binary",
            "sports_winner", "weather", "other",
        ]
        for arch in known:
            assert arch in CORRELATION_GROUPS, f"{arch} missing from CORRELATION_GROUPS"

    def test_constants_sane(self):
        assert SOFT_CAP <= HARD_CAP
        assert STRONG_EDGE_PP > 0
        assert DISPLACE_BUFFER_PP > 0
        assert MAX_PER_GROUP == SOFT_CAP  # backward-compat alias


class TestCorrelationStatus:
    @patch("paper_portfolio._get_db")
    def test_status_shows_groups(self, mock_db, mem_db):
        mock_db.return_value = mem_db
        _insert_open(mem_db, "price_above", 2, edge_pct=15)
        _insert_open(mem_db, "sports_winner", 1, edge_pct=15)

        status = get_correlation_status()
        assert status["crypto"]["count"] == 2
        assert status["sports"]["count"] == 1
        assert status["finance"]["count"] == 0

    @patch("paper_portfolio._get_db")
    def test_status_full_flag_at_soft_cap(self, mock_db, mem_db):
        """`full` reflects the soft cap (behavior preserved from v1 for the UI)."""
        mock_db.return_value = mem_db
        _insert_open(mem_db, "weather", n=SOFT_CAP, edge_pct=15)
        status = get_correlation_status()
        assert status["weather"]["count"] == SOFT_CAP
        assert status["weather"]["full"] is True

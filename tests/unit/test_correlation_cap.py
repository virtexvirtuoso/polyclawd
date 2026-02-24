"""Tests for portfolio correlation cap."""
import sqlite3
import sys
import os
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
)


@pytest.fixture
def mem_db():
    """In-memory SQLite DB with paper_positions table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _init_tables(conn)
    return conn


def _insert_open(conn, archetype, n=1):
    for i in range(n):
        conn.execute(
            "INSERT INTO paper_positions (opened_at, market_id, market_title, side, entry_price, bet_size, status, archetype) "
            "VALUES (datetime('now'), ?, ?, 'NO', 0.5, 100, 'open', ?)",
            (f"mkt-{archetype}-{i}", f"Test {archetype} {i}", archetype),
        )
    conn.commit()


class TestCorrelationCap:
    def test_empty_portfolio_allows(self, mem_db):
        result = _check_correlation_cap("price_above", mem_db)
        assert result is None

    def test_below_cap_allows(self, mem_db):
        _insert_open(mem_db, "price_above", 2)
        result = _check_correlation_cap("daily_updown", mem_db)
        assert result is None  # 2 crypto, cap is 3

    def test_at_cap_blocks(self, mem_db):
        _insert_open(mem_db, "price_above", 2)
        _insert_open(mem_db, "daily_updown", 1)
        result = _check_correlation_cap("directional", mem_db)
        assert result is not None
        assert "crypto" in result
        assert "3/3" in result

    def test_different_group_unaffected(self, mem_db):
        _insert_open(mem_db, "price_above", 3)  # crypto full
        result = _check_correlation_cap("sports_winner", mem_db)
        assert result is None  # sports is empty

    def test_closed_positions_dont_count(self, mem_db):
        _insert_open(mem_db, "price_above", 3)
        # Close one
        mem_db.execute(
            "UPDATE paper_positions SET status='won' WHERE market_id='mkt-price_above-0'"
        )
        mem_db.commit()
        result = _check_correlation_cap("directional", mem_db)
        assert result is None  # only 2 open crypto now

    def test_unknown_archetype_maps_to_other(self, mem_db):
        result = _check_correlation_cap("totally_unknown", mem_db)
        assert result is None

    def test_all_archetypes_have_groups(self):
        """Every archetype from classify_archetype should map to a group."""
        known = [
            "daily_updown", "intraday_updown", "parlay", "price_above",
            "price_range", "directional", "financial_price", "ai_model",
            "geopolitical", "election", "sports_single_game", "game_total",
            "entertainment", "social_count", "deadline_binary",
            "sports_winner", "weather", "other",
        ]
        for arch in known:
            assert arch in CORRELATION_GROUPS, f"{arch} missing from CORRELATION_GROUPS"


class TestCorrelationStatus:
    @patch("paper_portfolio._get_db")
    def test_status_shows_groups(self, mock_db, mem_db):
        mock_db.return_value = mem_db
        _insert_open(mem_db, "price_above", 2)
        _insert_open(mem_db, "sports_winner", 1)

        status = get_correlation_status()
        assert status["crypto"]["count"] == 2
        assert status["crypto"]["full"] is False
        assert status["sports"]["count"] == 1
        assert status["finance"]["count"] == 0

    @patch("paper_portfolio._get_db")
    def test_status_full_flag(self, mock_db, mem_db):
        mock_db.return_value = mem_db
        _insert_open(mem_db, "price_above", 3)

        status = get_correlation_status()
        assert status["crypto"]["full"] is True

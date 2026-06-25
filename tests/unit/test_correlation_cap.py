"""Tests for portfolio correlation + entity concentration caps.

Rewritten 2026-06-25: the earlier tiered soft/hard displacement-cap design
(SOFT_CAP/HARD_CAP/STRONG_EDGE_PP/DISPLACE_BUFFER_PP) was removed in favor of a
flat per-group cap (MAX_PER_GROUP) plus entity-level guards (ENTITY_GROUPS).
These tests assert the CURRENT behavior of paper_portfolio.py.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

# Add signals/ to path (repo is not yet an installable package)
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "signals"))

from paper_portfolio import (  # noqa: E402
    _check_correlation_cap,
    _check_entity_concentration,
    _init_tables,
    get_correlation_status,
    CORRELATION_GROUPS,
    ENTITY_GROUPS,
    MAX_PER_GROUP,
)


@pytest.fixture
def mem_db():
    """In-memory SQLite DB with the paper_positions schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _init_tables(conn)
    yield conn
    conn.close()


def _insert(conn, archetype="price_above", title="Test market", n=1, status="open"):
    for i in range(n):
        conn.execute(
            "INSERT INTO paper_positions "
            "(opened_at, market_id, market_title, side, entry_price, bet_size, "
            " edge_pct, status, archetype) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("2026-06-25T00:00:00Z", f"mkt-{archetype}-{i}", f"{title} {i}", "yes", 0.5, 10.0, 10.0, status, archetype),
        )
    conn.commit()


# ── Correlation group cap ──────────────────────────────────────────────


def test_under_cap_allows(mem_db):
    _insert(mem_db, "price_above", n=MAX_PER_GROUP - 1)
    assert _check_correlation_cap("price_above", mem_db) is None


def test_at_cap_blocks(mem_db):
    _insert(mem_db, "price_above", n=MAX_PER_GROUP)
    reason = _check_correlation_cap("price_above", mem_db)
    assert reason is not None
    assert "crypto" in reason  # price_above maps to the "crypto" group


def test_siblings_share_one_group(mem_db):
    # price_above / price_range / daily_updown all map to "crypto"
    _insert(mem_db, "price_above", n=4)
    _insert(mem_db, "price_range", n=3)
    _insert(mem_db, "daily_updown", n=3)  # 10 total in the crypto group
    # crypto_price is also "crypto" → group is full
    assert _check_correlation_cap("crypto_price", mem_db) is not None


def test_other_group_unaffected(mem_db):
    _insert(mem_db, "price_above", n=MAX_PER_GROUP)  # crypto full
    assert _check_correlation_cap("weather", mem_db) is None  # different group


def test_closed_positions_dont_count(mem_db):
    _insert(mem_db, "price_above", n=MAX_PER_GROUP, status="closed")
    assert _check_correlation_cap("price_above", mem_db) is None


# ── Entity concentration guard ─────────────────────────────────────────


def test_entity_under_cap_allows(mem_db):
    _insert(mem_db, "geopolitical", title="Iran strike", n=ENTITY_GROUPS["iran"] - 1)
    assert _check_entity_concentration("Iran nuclear deal", mem_db) is None


def test_entity_at_cap_blocks(mem_db):
    _insert(mem_db, "geopolitical", title="Iran strike", n=ENTITY_GROUPS["iran"])
    reason = _check_entity_concentration("Iran sanctions vote", mem_db)
    assert reason is not None
    assert "iran" in reason.lower()


def test_entity_caps_are_independent(mem_db):
    _insert(mem_db, "geopolitical", title="Iran strike", n=ENTITY_GROUPS["iran"])
    # Only iran is at cap; an unrelated entity is still allowed.
    assert _check_entity_concentration("Trump indictment odds", mem_db) is None


# ── Status helper ──────────────────────────────────────────────────────


def test_get_correlation_status(mem_db, monkeypatch):
    _insert(mem_db, "price_above", n=2)
    monkeypatch.setattr("paper_portfolio._get_db", lambda: mem_db)
    status = get_correlation_status()
    assert status["crypto"]["count"] == 2
    assert status["crypto"]["max"] == MAX_PER_GROUP
    assert status["crypto"]["full"] is False
    # CORRELATION_GROUPS is the source of truth for group names
    assert set(status) == set(CORRELATION_GROUPS.values())

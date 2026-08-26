"""Unit tests for _load_resolved_trades in signals.empirical_confidence.

This is the second consumer of shadow_trades (the archetype win-rate table
feeding confidence scoring); signals.source_win_rates is the first. Both read
the same table, so both must apply the same outcome vocabulary:

  shadow_trades.side    : 'YES' | 'NO' | 'PASS'
  shadow_trades.outcome : 'YES' | 'NO' | 'VOID' | NULL

Selecting on `resolved = 1` alone pulls in NULL-outcome, VOID and PASS-side
rows. Each then evaluates `side == outcome` to False and is scored as a LOSS,
depressing every archetype bucket. On production (2026-08-26) that was 34 of
367 rows: 39.8% shadow win rate reported vs 43.8% actual.
"""

import sqlite3

import pytest

import signals.empirical_confidence as ec


SHADOW_DDL = """
CREATE TABLE shadow_trades (
    market TEXT, side TEXT, entry_price REAL, outcome TEXT,
    platform TEXT, resolved INTEGER
)
"""
PAPER_DDL = """
CREATE TABLE paper_positions (
    market_title TEXT, side TEXT, entry_price REAL, status TEXT, platform TEXT
)
"""


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "shadow_trades.db"
    conn = sqlite3.connect(path)
    conn.execute(SHADOW_DDL)
    conn.execute(PAPER_DDL)
    conn.executemany(
        "INSERT INTO shadow_trades VALUES (?,?,?,?,?,?)",
        [
            # scoreable
            ("Mkt A", "YES", 0.40, "YES", "kalshi", 1),  # win
            ("Mkt B", "NO", 0.30, "NO", "kalshi", 1),  # win (NO side, NO result)
            ("Mkt C", "YES", 0.50, "NO", "kalshi", 1),  # loss
            # NOT scoreable — must be dropped, not counted as losses
            ("Mkt D", "YES", 0.40, None, "kalshi", 1),  # outcome never written
            ("Mkt E", "YES", 0.40, "VOID", "kalshi", 1),  # market voided
            ("Mkt F", "PASS", 0.40, "YES", "kalshi", 1),  # non-directional
            # not resolved yet
            ("Mkt G", "YES", 0.40, "YES", "kalshi", 0),
        ],
    )
    conn.executemany(
        "INSERT INTO paper_positions VALUES (?,?,?,?,?)",
        [
            ("Mkt H", "YES", 0.45, "won", "polymarket"),
            ("Mkt I", "NO", 0.55, "lost", "polymarket"),
            ("Mkt J", "YES", 0.45, "stopped", "polymarket"),  # excluded
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(ec, "DB_PATH", path)
    return path


def _shadow(trades):
    return [t for t in trades if t["platform"] == "kalshi"]


def test_unscoreable_shadow_rows_are_dropped_not_counted_as_losses(db):
    """NULL / VOID / PASS rows must not appear at all."""
    shadow = _shadow(ec._load_resolved_trades())
    assert len(shadow) == 3
    assert {t["title"] for t in shadow} == {"Mkt A", "Mkt B", "Mkt C"}


def test_shadow_win_rate_not_depressed_by_unscoreable_rows(db):
    """2 of 3 scoreable = 0.667. Counting the 3 junk rows would give 2/6."""
    shadow = _shadow(ec._load_resolved_trades())
    assert sum(t["won"] for t in shadow) == 2
    assert sum(t["won"] for t in shadow) / len(shadow) == pytest.approx(2 / 3)


def test_no_side_win_is_scored_as_a_win(db):
    """A NO-side trade resolving NO is a win."""
    shadow = _shadow(ec._load_resolved_trades())
    mkt_b = next(t for t in shadow if t["title"] == "Mkt B")
    assert mkt_b["won"] is True


def test_unresolved_rows_excluded(db):
    assert "Mkt G" not in {t["title"] for t in ec._load_resolved_trades()}


def test_paper_arm_unaffected(db):
    """The paper_positions arm keeps its own 'won'/'lost' vocabulary."""
    paper = [t for t in ec._load_resolved_trades() if t["platform"] == "polymarket"]
    assert len(paper) == 2  # 'stopped' excluded
    assert sum(t["won"] for t in paper) == 1

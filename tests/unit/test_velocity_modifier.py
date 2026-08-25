"""Tests for Strategy 1: Score velocity modifier."""
import sqlite3
import time
import pytest
from signals.alpha_score_tracker import (
    init_db,
    score_velocity_modifier,
    _get_conn,
)


@pytest.fixture
def test_db(tmp_path):
    """Create a temporary test database."""
    db = str(tmp_path / "test_velocity.db")
    init_db(db)
    return db


def _insert_snapshots(db_path, symbol, scores, interval_secs=1800):
    """Insert synthetic alpha snapshots for testing."""
    conn = _get_conn(db_path)
    now = time.time()
    for i, score in enumerate(scores):
        ts = now - (len(scores) - 1 - i) * interval_secs
        conn.execute("""
            INSERT INTO alpha_snapshots
            (timestamp, symbol, confluence_score, signal_type)
            VALUES (?, ?, ?, ?)
        """, (ts, symbol, score, "bullish" if score > 50 else "bearish"))
    conn.commit()
    conn.close()


class TestScoreVelocityModifier:
    """Test velocity modifier maps score deltas to [0.7, 1.3] multiplier."""

    def test_positive_delta_increases_multiplier(self, test_db):
        """Score improving → multiplier > 1.0."""
        _insert_snapshots(test_db, "BTCUSDT", [50, 55, 60], interval_secs=1800)
        result = score_velocity_modifier("BTCUSDT", hours=2, db_path=test_db)
        assert result["multiplier"] > 1.0
        assert result["delta"] > 0

    def test_negative_delta_decreases_multiplier(self, test_db):
        """Score deteriorating → multiplier < 1.0."""
        _insert_snapshots(test_db, "BTCUSDT", [60, 55, 50], interval_secs=1800)
        result = score_velocity_modifier("BTCUSDT", hours=2, db_path=test_db)
        assert result["multiplier"] < 1.0
        assert result["delta"] < 0

    def test_zero_delta_neutral_multiplier(self, test_db):
        """No change → multiplier = 1.0."""
        _insert_snapshots(test_db, "BTCUSDT", [50, 52, 50], interval_secs=1800)
        result = score_velocity_modifier("BTCUSDT", hours=2, db_path=test_db)
        assert result["multiplier"] == 1.0
        assert result["delta"] == 0

    def test_insufficient_data_returns_neutral(self, test_db):
        """Only 1 snapshot → multiplier = 1.0, reason=insufficient_data."""
        _insert_snapshots(test_db, "BTCUSDT", [50], interval_secs=1800)
        result = score_velocity_modifier("BTCUSDT", hours=2, db_path=test_db)
        assert result["multiplier"] == 1.0
        assert result.get("reason") == "insufficient_data"

    def test_no_data_returns_neutral(self, test_db):
        """No snapshots at all → multiplier = 1.0."""
        result = score_velocity_modifier("BTCUSDT", hours=2, db_path=test_db)
        assert result["multiplier"] == 1.0
        assert result.get("reason") == "insufficient_data"

    def test_extreme_positive_clamped(self, test_db):
        """Very large delta → clamped to 1.3 max."""
        _insert_snapshots(test_db, "BTCUSDT", [10, 30, 60], interval_secs=1800)
        result = score_velocity_modifier("BTCUSDT", hours=2, db_path=test_db)
        assert result["multiplier"] <= 1.3

    def test_extreme_negative_clamped(self, test_db):
        """Very large negative delta → clamped to 0.7 min."""
        _insert_snapshots(test_db, "BTCUSDT", [60, 30, 10], interval_secs=1800)
        result = score_velocity_modifier("BTCUSDT", hours=2, db_path=test_db)
        assert result["multiplier"] >= 0.7

    def test_bounds_always_hold(self, test_db):
        """Multiplier always within [0.7, 1.3] regardless of input."""
        for scores in [[0, 100], [100, 0], [50, 50], [10, 90], [90, 10]]:
            _insert_snapshots(test_db, "ETHUSDT", scores, interval_secs=1800)
            result = score_velocity_modifier("ETHUSDT", hours=2, db_path=test_db)
            assert 0.7 <= result["multiplier"] <= 1.3

    def test_return_dict_structure(self, test_db):
        """Verify all expected keys in return dict."""
        _insert_snapshots(test_db, "BTCUSDT", [50, 55, 60], interval_secs=1800)
        result = score_velocity_modifier("BTCUSDT", hours=2, db_path=test_db)
        assert "multiplier" in result
        assert "delta" in result
        assert "symbol" in result
        assert "hours" in result
        assert result["symbol"] == "BTCUSDT"
        assert result["hours"] == 2

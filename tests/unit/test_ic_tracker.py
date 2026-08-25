"""Tests for IC (Information Coefficient) tracker."""
import time
import pytest
from signals.ic_tracker import (
    init_ic_tables,
    record_signal_prediction,
    resolve_prediction,
    calculate_ic,
    ic_report,
    _get_conn,
    _spearman_rank_correlation,
)


@pytest.fixture
def test_db(tmp_path):
    """Create a temporary test database."""
    db = str(tmp_path / "test_ic.db")
    init_ic_tables(db)
    return db


class TestRecordPrediction:
    """Test signal prediction recording."""

    def test_record_stores_to_db(self, test_db):
        """record_signal_prediction should insert a row."""
        record_signal_prediction({
            "source": "inverse_whale",
            "market_id": "market_123",
            "market": "Will BTC hit 100k?",
            "side": "YES",
            "confidence": 72.5,
            "price": 0.45,
        }, db_path=test_db)

        conn = _get_conn(test_db)
        row = conn.execute("SELECT * FROM signal_predictions WHERE market_id = 'market_123'").fetchone()
        conn.close()
        assert row is not None

    def test_record_multiple_sources(self, test_db):
        """Multiple sources for same market should all be recorded."""
        for source in ["inverse_whale", "smart_money", "volume_spike"]:
            record_signal_prediction({
                "source": source,
                "market_id": "market_456",
                "side": "YES",
                "confidence": 60,
                "price": 0.50,
            }, db_path=test_db)

        conn = _get_conn(test_db)
        count = conn.execute(
            "SELECT COUNT(*) FROM signal_predictions WHERE market_id = 'market_456'"
        ).fetchone()[0]
        conn.close()
        assert count == 3


class TestResolvePrediction:
    """Test prediction resolution."""

    def test_resolve_marks_as_resolved(self, test_db):
        """resolve_prediction should set resolved=1 and outcome."""
        record_signal_prediction({
            "source": "test", "market_id": "m1", "side": "YES",
            "confidence": 70, "price": 0.40,
        }, db_path=test_db)

        count = resolve_prediction("m1", outcome=1.0, db_path=test_db)
        assert count == 1

        conn = _get_conn(test_db)
        row = conn.execute(
            "SELECT resolved, outcome FROM signal_predictions WHERE market_id = 'm1'"
        ).fetchone()
        conn.close()
        assert row[0] == 1  # resolved
        assert row[1] == 1.0  # outcome

    def test_resolve_nonexistent_market(self, test_db):
        """Resolving a market with no predictions should return 0."""
        count = resolve_prediction("nonexistent", outcome=1.0, db_path=test_db)
        assert count == 0


class TestSpearmanCorrelation:
    """Test the Spearman rank correlation implementation."""

    def test_perfect_positive_correlation(self):
        """Identical rankings → rho ≈ 1.0."""
        x = [1, 2, 3, 4, 5]
        y = [10, 20, 30, 40, 50]
        rho = _spearman_rank_correlation(x, y)
        assert abs(rho - 1.0) < 0.01

    def test_perfect_negative_correlation(self):
        """Reversed rankings → rho ≈ -1.0."""
        x = [1, 2, 3, 4, 5]
        y = [50, 40, 30, 20, 10]
        rho = _spearman_rank_correlation(x, y)
        assert abs(rho - (-1.0)) < 0.01

    def test_no_correlation(self):
        """Unrelated data → rho near 0."""
        x = [1, 2, 3, 4, 5, 6]
        y = [3, 1, 6, 2, 5, 4]
        rho = _spearman_rank_correlation(x, y)
        assert abs(rho) < 0.5  # Should be weakly correlated at most

    def test_insufficient_data(self):
        """Less than 3 points → returns 0.0."""
        assert _spearman_rank_correlation([1, 2], [3, 4]) == 0.0
        assert _spearman_rank_correlation([], []) == 0.0

    def test_mismatched_lengths(self):
        """Different length arrays → returns 0.0."""
        assert _spearman_rank_correlation([1, 2, 3], [1, 2]) == 0.0


class TestCalculateIC:
    """Test IC calculation for signal sources."""

    def test_insufficient_data_graceful(self, test_db):
        """Less than 10 resolved predictions → graceful insufficient_data."""
        for i in range(5):
            record_signal_prediction({
                "source": "test_source", "market_id": f"m{i}",
                "side": "YES", "confidence": 50 + i * 5, "price": 0.5,
            }, db_path=test_db)
            resolve_prediction(f"m{i}", outcome=1.0, db_path=test_db)

        result = calculate_ic("test_source", window_days=30, db_path=test_db)
        assert result["status"] == "insufficient_data"
        assert result["ic_value"] is None
        assert result["sample_size"] == 5

    def test_perfect_correlation_high_ic(self, test_db):
        """When confidence perfectly predicts outcome, IC should be high."""
        # High confidence → win, low confidence → lose
        for i in range(20):
            conf = 50 + i * 2.5  # 50 to 97.5
            outcome = 1.0 if conf > 70 else 0.0
            record_signal_prediction({
                "source": "good_source", "market_id": f"perf_{i}",
                "side": "YES", "confidence": conf, "price": 0.5,
            }, db_path=test_db)
            resolve_prediction(f"perf_{i}", outcome=outcome, db_path=test_db)

        result = calculate_ic("good_source", window_days=30, db_path=test_db)
        assert result["ic_value"] is not None
        assert result["ic_value"] > 0.3  # Strong positive correlation
        assert result["status"] == "OK"

    def test_random_source_low_ic(self, test_db):
        """When confidence is unrelated to outcome, IC should be near 0."""
        import random
        random.seed(42)
        for i in range(30):
            record_signal_prediction({
                "source": "noise_source", "market_id": f"rand_{i}",
                "side": "YES", "confidence": random.uniform(30, 90), "price": 0.5,
            }, db_path=test_db)
            resolve_prediction(f"rand_{i}", outcome=random.choice([0.0, 1.0]), db_path=test_db)

        result = calculate_ic("noise_source", window_days=30, db_path=test_db)
        assert result["ic_value"] is not None
        # Random source should have IC near zero (but not exactly)
        assert abs(result["ic_value"]) < 0.4


class TestICReport:
    """Test the full IC report generation."""

    def test_report_structure(self, test_db):
        """ic_report should return expected keys."""
        result = ic_report(window_days=30, db_path=test_db)
        assert "window_days" in result
        assert "sources" in result
        assert "aggregate_ic" in result
        assert "total_resolved" in result
        assert "total_unresolved" in result
        assert "kill_list" in result
        assert "warn_list" in result
        assert "recommendations" in result
        assert "generated_at" in result

    def test_report_empty_db(self, test_db):
        """Report with no data should return gracefully."""
        result = ic_report(window_days=30, db_path=test_db)
        assert result["total_resolved"] == 0
        assert result["aggregate_ic"] is None
        assert len(result["sources"]) == 0

    def test_report_with_data(self, test_db):
        """Report with data should include per-source IC."""
        for i in range(15):
            record_signal_prediction({
                "source": "src_a", "market_id": f"a_{i}",
                "side": "YES", "confidence": 50 + i * 3, "price": 0.5,
            }, db_path=test_db)
            resolve_prediction(f"a_{i}", outcome=1.0 if i > 7 else 0.0, db_path=test_db)

        result = ic_report(window_days=30, db_path=test_db)
        assert "src_a" in result["sources"]
        assert result["sources"]["src_a"]["sample_size"] == 15
        assert result["total_resolved"] == 15

"""Unit tests for signals router - signal aggregation, whale tracking, and confidence scoring."""
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from api.routes.signals import (
    router,
    load_predictor_stats,
    save_predictor_stats,
    load_source_outcomes,
    save_source_outcomes,
    get_source_win_rate,
    record_outcome,
    load_conflict_history,
    calculate_bayesian_confidence,
    scan_volume_spikes,
    scan_resolution_timing,
    get_inverse_whale_signals,
    get_smart_money_flow,
    aggregate_all_signals,
    DATA_DIR,
    STORAGE_DIR,
)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_data_dir(tmp_path):
    """Create temporary data directory."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


@pytest.fixture
def mock_predictor_stats(tmp_path):
    """Create mock predictor stats file."""
    stats = {
        "predictors": {
            "0xabc123": {
                "name": "TestWhale",
                "accuracy": 65.0,
                "total_predictions": 20,
                "correct_predictions": 13,
                "total_profit": 1500.0
            },
            "0xdef456": {
                "name": "LosingWhale",
                "accuracy": 35.0,
                "total_predictions": 20,
                "correct_predictions": 7,
                "total_profit": -800.0
            }
        },
        "last_updated": "2026-02-01T12:00:00"
    }
    return stats


@pytest.fixture
def mock_source_outcomes():
    """Create mock source outcomes."""
    return {
        "inverse_whale": {"wins": 12, "losses": 8, "total": 20},
        "smart_money": {"wins": 15, "losses": 5, "total": 20},
        "volume_spike": {"wins": 6, "losses": 14, "total": 20},
    }


@pytest.fixture
def mock_signals():
    """Create mock signals for testing."""
    return [
        {
            "source": "inverse_whale",
            "platform": "polymarket",
            "market": "Will BTC hit $100k?",
            "side": "YES",
            "confidence": 25.0,
            "price": 0.45
        },
        {
            "source": "smart_money",
            "platform": "polymarket",
            "market": "Will BTC hit $100k?",
            "side": "YES",
            "confidence": 30.0,
            "price": 0.45
        },
        {
            "source": "volume_spike",
            "platform": "polymarket",
            "market": "Will ETH hit $5k?",
            "side": "NO",
            "confidence": 20.0,
            "price": 0.65
        }
    ]


# ============================================================================
# Test: Predictor Stats
# ============================================================================

class TestPredictorStats:
    """Tests for predictor stats loading/saving."""

    def test_load_empty_predictor_stats(self, tmp_path, monkeypatch):
        """Load predictor stats when file doesn't exist."""
        # Patch DATA_DIR to use tmp_path
        monkeypatch.setattr("api.routes.signals.DATA_DIR", tmp_path)
        monkeypatch.setattr("api.routes.signals.PREDICTOR_STATS_FILE", tmp_path / "predictor_stats.json")

        stats = load_predictor_stats()
        assert stats == {"predictors": {}, "last_updated": None}

    def test_load_existing_predictor_stats(self, tmp_path, mock_predictor_stats, monkeypatch):
        """Load predictor stats from existing file."""
        stats_file = tmp_path / "predictor_stats.json"
        stats_file.write_text(json.dumps(mock_predictor_stats))
        monkeypatch.setattr("api.routes.signals.DATA_DIR", tmp_path)
        monkeypatch.setattr("api.routes.signals.PREDICTOR_STATS_FILE", stats_file)

        stats = load_predictor_stats()
        assert len(stats["predictors"]) == 2
        assert stats["predictors"]["0xabc123"]["accuracy"] == 65.0

    def test_save_predictor_stats(self, tmp_path, mock_predictor_stats, monkeypatch):
        """Save predictor stats to file."""
        stats_file = tmp_path / "predictor_stats.json"
        monkeypatch.setattr("api.routes.signals.PREDICTOR_STATS_FILE", stats_file)

        save_predictor_stats(mock_predictor_stats)

        assert stats_file.exists()
        loaded = json.loads(stats_file.read_text())
        assert loaded["predictors"]["0xabc123"]["name"] == "TestWhale"


# ============================================================================
# Test: Source Outcomes
# ============================================================================

class TestSourceOutcomes:
    """Tests for source outcome tracking."""

    def test_load_default_source_outcomes(self, tmp_path, monkeypatch):
        """Load source outcomes creates defaults when file doesn't exist."""
        monkeypatch.setattr("api.routes.signals.DATA_DIR", tmp_path)
        monkeypatch.setattr("api.routes.signals.SOURCE_OUTCOMES_FILE", tmp_path / "source_outcomes.json")

        outcomes = load_source_outcomes()
        assert "simmer_divergence" in outcomes
        assert outcomes["simmer_divergence"]["total"] == 10

    def test_get_source_win_rate(self, tmp_path, mock_source_outcomes, monkeypatch):
        """Calculate win rate for a source."""
        outcomes_file = tmp_path / "source_outcomes.json"
        outcomes_file.write_text(json.dumps(mock_source_outcomes))
        monkeypatch.setattr("api.routes.signals.SOURCE_OUTCOMES_FILE", outcomes_file)

        win_rate = get_source_win_rate("smart_money")
        assert win_rate == 0.75  # 15/20

    def test_get_win_rate_unknown_source(self, tmp_path, mock_source_outcomes, monkeypatch):
        """Get win rate for unknown source defaults to 0.5."""
        outcomes_file = tmp_path / "source_outcomes.json"
        outcomes_file.write_text(json.dumps(mock_source_outcomes))
        monkeypatch.setattr("api.routes.signals.SOURCE_OUTCOMES_FILE", outcomes_file)

        win_rate = get_source_win_rate("unknown_source")
        assert win_rate == 0.5

    def test_record_outcome_win(self, tmp_path, mock_source_outcomes, monkeypatch):
        """Record a winning trade outcome."""
        outcomes_file = tmp_path / "source_outcomes.json"
        outcomes_file.write_text(json.dumps(mock_source_outcomes))
        monkeypatch.setattr("api.routes.signals.SOURCE_OUTCOMES_FILE", outcomes_file)

        record_outcome("smart_money", True, "Test Market")

        loaded = json.loads(outcomes_file.read_text())
        assert loaded["smart_money"]["wins"] == 16
        assert loaded["smart_money"]["total"] == 21

    def test_record_outcome_loss(self, tmp_path, mock_source_outcomes, monkeypatch):
        """Record a losing trade outcome."""
        outcomes_file = tmp_path / "source_outcomes.json"
        outcomes_file.write_text(json.dumps(mock_source_outcomes))
        monkeypatch.setattr("api.routes.signals.SOURCE_OUTCOMES_FILE", outcomes_file)

        record_outcome("smart_money", False, "Test Market")

        loaded = json.loads(outcomes_file.read_text())
        assert loaded["smart_money"]["losses"] == 6
        assert loaded["smart_money"]["total"] == 21


# ============================================================================
# Test: Bayesian Confidence
# ============================================================================

class TestBayesianConfidence:
    """Tests for Bayesian confidence scoring."""

    def test_calculate_bayesian_confidence_base(self, tmp_path, mock_source_outcomes, monkeypatch):
        """Calculate Bayesian confidence with high win rate source."""
        outcomes_file = tmp_path / "source_outcomes.json"
        outcomes_file.write_text(json.dumps(mock_source_outcomes))
        monkeypatch.setattr("api.routes.signals.SOURCE_OUTCOMES_FILE", outcomes_file)

        result = calculate_bayesian_confidence(
            raw_score=50.0,
            source="smart_money",
            market="Test Market",
            side="YES",
            all_signals=[]
        )

        assert result["base_confidence"] == 50.0
        assert result["win_rate"] == 0.75
        assert result["bayesian_multiplier"] == 1.5  # 0.75 / 0.5
        assert result["final_confidence"] > 50.0  # Boosted by high win rate

    def test_calculate_bayesian_confidence_with_agreement(self, tmp_path, mock_source_outcomes, mock_signals, monkeypatch):
        """Calculate Bayesian confidence with signal agreement."""
        outcomes_file = tmp_path / "source_outcomes.json"
        outcomes_file.write_text(json.dumps(mock_source_outcomes))
        monkeypatch.setattr("api.routes.signals.SOURCE_OUTCOMES_FILE", outcomes_file)

        result = calculate_bayesian_confidence(
            raw_score=50.0,
            source="inverse_whale",
            market="Will BTC hit $100k?",
            side="YES",
            all_signals=mock_signals
        )

        assert result["agreement_count"] == 1  # smart_money agrees
        assert result["composite_multiplier"] > 1.0


# ============================================================================
# Test: Signal Aggregation
# ============================================================================

class TestSignalAggregation:
    """Tests for signal aggregation."""

    def test_aggregate_all_signals_structure(self, tmp_path, monkeypatch):
        """Verify aggregate_all_signals returns correct structure."""
        # Mock all external dependencies
        monkeypatch.setattr("api.routes.signals.DATA_DIR", tmp_path)
        monkeypatch.setattr("api.routes.signals.SOURCE_OUTCOMES_FILE", tmp_path / "source_outcomes.json")
        monkeypatch.setattr("api.routes.signals.PREDICTOR_STATS_FILE", tmp_path / "predictor_stats.json")
        monkeypatch.setattr("api.routes.signals.WHALE_CONFIG_FILE", tmp_path / "whale_config.json")

        # Create minimal config files
        (tmp_path / "source_outcomes.json").write_text(json.dumps({}))
        (tmp_path / "predictor_stats.json").write_text(json.dumps({"predictors": {}}))
        (tmp_path / "whale_config.json").write_text(json.dumps({"whales": []}))

        result = aggregate_all_signals()

        assert "actionable_signals" in result
        assert "research_signals" in result
        assert "arb_signals" in result
        assert "total_signals" in result
        assert "sources" in result
        assert "scoring_method" in result
        assert result["scoring_method"] == "bayesian_composite"


# ============================================================================
# Test: Volume Spikes
# ============================================================================

class TestVolumeSpikes:
    """Tests for volume spike detection."""

    def test_scan_volume_spikes_with_mock(self, monkeypatch):
        """Test volume spike scanning with mocked API."""
        mock_markets = [
            {"id": "1", "question": "High Volume Market", "volume24hr": 100000, "outcomePrices": "[0.6, 0.4]", "slug": "high-vol"},
            {"id": "2", "question": "Normal Volume Market", "volume24hr": 10000, "outcomePrices": "[0.5, 0.5]", "slug": "normal-vol"},
            {"id": "3", "question": "Low Volume Market", "volume24hr": 1000, "outcomePrices": "[0.7, 0.3]", "slug": "low-vol"},
        ]

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(mock_markets).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = scan_volume_spikes(spike_threshold=1.5, use_zscore=True)

        assert "spikes" in result
        assert "mean_volume" in result
        assert "std_volume" in result
        assert result["method"] == "zscore"


# ============================================================================
# Test: Resolution Timing
# ============================================================================

class TestResolutionTiming:
    """Tests for resolution timing scanning."""

    def test_scan_resolution_timing_with_mock(self, monkeypatch):
        """Test resolution timing with mocked API."""
        # Create market with end date 12 hours from now
        from datetime import datetime, timedelta
        future_date = (datetime.now() + timedelta(hours=12)).isoformat()

        mock_markets = [
            {"id": "1", "question": "Market Ending Soon", "endDate": future_date, "outcomePrices": "[0.5, 0.5]", "volume24hr": 50000, "liquidityNum": 100000, "slug": "ending-soon"},
        ]

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(mock_markets).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = scan_resolution_timing(hours_until=48)

        assert "markets" in result
        assert "count" in result
        assert "hours_threshold" in result
        assert result["hours_threshold"] == 48


# ============================================================================
# Test: Whale Tracking
# ============================================================================

class TestWhaleTracking:
    """Tests for whale tracking functions."""

    def test_inverse_whale_no_losing_whales(self, tmp_path, monkeypatch):
        """Test inverse whale with no losing whales returns empty."""
        stats = {"predictors": {}, "last_updated": None}
        stats_file = tmp_path / "predictor_stats.json"
        stats_file.write_text(json.dumps(stats))
        monkeypatch.setattr("api.routes.signals.PREDICTOR_STATS_FILE", stats_file)
        monkeypatch.setattr("api.routes.signals.WHALE_CONFIG_FILE", tmp_path / "whale_config.json")
        (tmp_path / "whale_config.json").write_text(json.dumps({"whales": []}))

        result = get_inverse_whale_signals()

        assert result["signals"] == []
        assert "No losing whales" in result["note"]

    def test_smart_money_flow_empty(self, tmp_path, monkeypatch):
        """Test smart money flow with no whales."""
        monkeypatch.setattr("api.routes.signals.WHALE_CONFIG_FILE", tmp_path / "whale_config.json")
        monkeypatch.setattr("api.routes.signals.PREDICTOR_STATS_FILE", tmp_path / "predictor_stats.json")
        (tmp_path / "whale_config.json").write_text(json.dumps({"whales": []}))
        (tmp_path / "predictor_stats.json").write_text(json.dumps({"predictors": {}}))

        result = get_smart_money_flow()

        assert result["flows"] == []
        assert result["count"] == 0


# ============================================================================
# Test: Conflict History
# ============================================================================

class TestConflictHistory:
    """Tests for conflict history."""

    def test_load_empty_conflict_history(self, tmp_path, monkeypatch):
        """Load conflict history when file doesn't exist."""
        monkeypatch.setattr("api.routes.signals.CONFLICT_HISTORY_FILE", tmp_path / "conflict_history.json")

        history = load_conflict_history()

        assert history == {"conflicts": [], "source_vs_source": {}}

    def test_load_existing_conflict_history(self, tmp_path, monkeypatch):
        """Load conflict history from existing file."""
        history_data = {
            "conflicts": [{"market": "test", "resolved": True}],
            "source_vs_source": {"inverse_whale_vs_smart_money": {"wins": 5, "losses": 3}}
        }
        history_file = tmp_path / "conflict_history.json"
        history_file.write_text(json.dumps(history_data))
        monkeypatch.setattr("api.routes.signals.CONFLICT_HISTORY_FILE", history_file)

        history = load_conflict_history()

        assert len(history["conflicts"]) == 1
        assert "inverse_whale_vs_smart_money" in history["source_vs_source"]


# ============================================================================
# Test: Router Import
# ============================================================================

class TestRouterImport:
    """Test that router imports correctly."""

    def test_router_has_routes(self):
        """Verify router has expected number of routes."""
        assert len(router.routes) >= 15  # We have ~19 routes

    def test_router_has_signals_endpoint(self):
        """Verify /signals endpoint exists."""
        paths = [route.path for route in router.routes]
        assert "/signals" in paths

    def test_router_has_predictors_endpoint(self):
        """Verify /predictors endpoint exists."""
        paths = [route.path for route in router.routes]
        assert "/predictors" in paths

    def test_router_has_confidence_endpoints(self):
        """Verify confidence endpoints exist."""
        paths = [route.path for route in router.routes]
        assert "/confidence/sources" in paths
        assert "/confidence/record" in paths

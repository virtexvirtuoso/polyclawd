"""Unit tests for engine router - engine control, alerts, LLM, Kelly, and phase endpoints."""
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from api.routes.engine import (
    router,
    load_engine_state,
    save_engine_state,
    get_effective_min_confidence,
    decay_adaptive_boost,
    increment_adaptive_boost,
    check_drawdown_halt,
    start_engine,
    stop_engine,
    engine_evaluate_and_trade,
    load_price_alerts,
    save_price_alerts,
    check_price_alerts,
    load_recent_performance,
    calculate_dynamic_kelly,
    llm_validate_signal,
    _engine_running,
    ADAPTIVE_CONF_INCREMENT,
    ADAPTIVE_CONF_MAX,
    ADAPTIVE_CONF_DECAY_MINUTES,
    DRAWDOWN_HALT_PCT,
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
def mock_storage_dir(tmp_path):
    """Create temporary storage directory."""
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    return storage_dir


@pytest.fixture
def mock_engine_state():
    """Create mock engine state."""
    return {
        "enabled": True,
        "min_confidence": 35,
        "max_per_trade": 100,
        "max_daily_trades": 20,
        "max_position_pct": 0.05,
        "cooldown_minutes": 5,
        "trades_today": 5,
        "last_trade_time": "2026-02-07T12:00:00",
        "last_scan_time": "2026-02-07T12:30:00",
        "total_trades": 100,
        "adaptive_boost": 10,
        "last_boost_decay": "2026-02-07T12:00:00",
        "daily_pnl": 50.0,
        "drawdown_halt": False,
    }


@pytest.fixture
def mock_price_alerts():
    """Create mock price alerts."""
    return [
        {
            "id": "alert_1_1707307200",
            "market_id": "0x123abc",
            "title": "Will Bitcoin reach $100k?",
            "target_price": 0.75,
            "direction": "above",
            "current_price": 0.65,
            "note": "Test alert",
            "created_at": "2026-02-07T10:00:00"
        },
        {
            "id": "alert_2_1707310800",
            "market_id": "0x456def",
            "title": "Will it rain tomorrow?",
            "target_price": 0.30,
            "direction": "below",
            "current_price": 0.45,
            "note": None,
            "created_at": "2026-02-07T11:00:00"
        }
    ]


@pytest.fixture
def mock_recent_performance():
    """Create mock recent performance data."""
    return {
        "trades": [
            {"won": True, "timestamp": "2026-02-07T10:00:00"},
            {"won": True, "timestamp": "2026-02-07T11:00:00"},
            {"won": False, "timestamp": "2026-02-07T12:00:00"},
            {"won": True, "timestamp": "2026-02-07T13:00:00"},
            {"won": True, "timestamp": "2026-02-07T14:00:00"},
        ],
        "last_5_wins": 4,
        "last_5_total": 5,
        "last_5_win_rate": 0.8,
        "overall_wins": 4,
        "overall_total": 5,
    }


# ============================================================================
# Engine State Tests
# ============================================================================

class TestEngineState:
    """Tests for engine state management."""

    def test_load_engine_state_default(self, tmp_path):
        """Test loading engine state when no file exists."""
        with patch("api.routes.engine.ENGINE_STATE_FILE", tmp_path / "engine_state.json"):
            with patch("api.routes.engine.DATA_DIR", tmp_path):
                state = load_engine_state()

        assert "enabled" in state
        assert "min_confidence" in state
        assert "adaptive_boost" in state
        assert state["enabled"] is False

    def test_load_engine_state_existing(self, tmp_path, mock_engine_state):
        """Test loading existing engine state."""
        state_file = tmp_path / "engine_state.json"
        state_file.write_text(json.dumps(mock_engine_state))

        with patch("api.routes.engine.ENGINE_STATE_FILE", state_file):
            with patch("api.routes.engine.DATA_DIR", tmp_path):
                state = load_engine_state()

        assert state["enabled"] is True
        assert state["min_confidence"] == 35
        assert state["adaptive_boost"] == 10

    def test_save_engine_state(self, tmp_path, mock_engine_state):
        """Test saving engine state."""
        state_file = tmp_path / "engine_state.json"

        with patch("api.routes.engine.ENGINE_STATE_FILE", state_file):
            save_engine_state(mock_engine_state)

        assert state_file.exists()
        saved = json.loads(state_file.read_text())
        assert saved["enabled"] is True
        assert saved["trades_today"] == 5


class TestAdaptiveConfidence:
    """Tests for adaptive confidence management."""

    def test_get_effective_min_confidence(self, mock_engine_state):
        """Test calculating effective min confidence."""
        result = get_effective_min_confidence(mock_engine_state)
        assert result == 45  # 35 + 10 boost

    def test_get_effective_min_confidence_no_boost(self):
        """Test effective min confidence with no boost."""
        state = {"min_confidence": 40, "adaptive_boost": 0}
        result = get_effective_min_confidence(state)
        assert result == 40

    def test_increment_adaptive_boost(self):
        """Test incrementing adaptive boost."""
        state = {"adaptive_boost": 10}
        result = increment_adaptive_boost(state)
        assert result["adaptive_boost"] == 10 + ADAPTIVE_CONF_INCREMENT

    def test_increment_adaptive_boost_max(self):
        """Test adaptive boost capped at maximum."""
        state = {"adaptive_boost": ADAPTIVE_CONF_MAX - 1}
        result = increment_adaptive_boost(state)
        assert result["adaptive_boost"] == ADAPTIVE_CONF_MAX

    def test_decay_adaptive_boost(self):
        """Test decaying adaptive boost over time."""
        old_time = (datetime.now() - timedelta(minutes=ADAPTIVE_CONF_DECAY_MINUTES + 1)).isoformat()
        state = {"adaptive_boost": 15, "last_boost_decay": old_time}

        result = decay_adaptive_boost(state)
        assert result["adaptive_boost"] < 15

    def test_decay_adaptive_boost_no_decay_recent(self):
        """Test no decay when time is recent."""
        recent_time = (datetime.now() - timedelta(minutes=5)).isoformat()
        state = {"adaptive_boost": 15, "last_boost_decay": recent_time}

        result = decay_adaptive_boost(state)
        assert result["adaptive_boost"] == 15


class TestDrawdownProtection:
    """Tests for drawdown circuit breaker."""

    def test_check_drawdown_halt_no_loss(self):
        """Test no halt when no losses."""
        state = {"daily_pnl": 100}
        halt, reason = check_drawdown_halt(state, 10000)
        assert halt is False
        assert reason is None

    def test_check_drawdown_halt_small_loss(self):
        """Test no halt on small loss."""
        state = {"daily_pnl": -200}  # 2% of starting balance
        halt, reason = check_drawdown_halt(state, 9800)
        assert halt is False

    def test_check_drawdown_halt_triggered(self):
        """Test halt triggered on large loss."""
        state = {"daily_pnl": -600}  # 6% of starting balance
        halt, reason = check_drawdown_halt(state, 9400)
        assert halt is True
        assert "Drawdown halt" in reason


# ============================================================================
# Engine Control Tests
# ============================================================================

class TestEngineControl:
    """Tests for engine start/stop functionality."""

    def test_engine_evaluate_and_trade(self, tmp_path):
        """Test engine evaluation returns proper structure."""
        state_file = tmp_path / "engine_state.json"
        state_file.write_text(json.dumps({"enabled": True, "trades_today": 0}))

        with patch("api.routes.engine.ENGINE_STATE_FILE", state_file):
            with patch("api.routes.engine.DATA_DIR", tmp_path):
                result = engine_evaluate_and_trade()

        assert "action" in result
        assert "timestamp" in result
        assert "trades_today" in result

    def test_start_engine_already_running(self, tmp_path):
        """Test starting engine when already running."""
        with patch("api.routes.engine._engine_running", True):
            result = start_engine()
        assert result["status"] == "already_running"


# ============================================================================
# Price Alerts Tests
# ============================================================================

class TestPriceAlerts:
    """Tests for price alert management."""

    def test_load_price_alerts_empty(self, tmp_path):
        """Test loading alerts when none exist."""
        with patch("api.routes.engine.PRICE_ALERTS_FILE", tmp_path / "alerts.json"):
            alerts = load_price_alerts()
        assert alerts == []

    def test_load_price_alerts_existing(self, tmp_path, mock_price_alerts):
        """Test loading existing alerts."""
        alerts_file = tmp_path / "alerts.json"
        alerts_file.write_text(json.dumps(mock_price_alerts))

        with patch("api.routes.engine.PRICE_ALERTS_FILE", alerts_file):
            alerts = load_price_alerts()

        assert len(alerts) == 2
        assert alerts[0]["id"] == "alert_1_1707307200"

    def test_save_price_alerts(self, tmp_path, mock_price_alerts):
        """Test saving price alerts."""
        alerts_file = tmp_path / "alerts.json"

        with patch("api.routes.engine.PRICE_ALERTS_FILE", alerts_file):
            save_price_alerts(mock_price_alerts)

        assert alerts_file.exists()
        saved = json.loads(alerts_file.read_text())
        assert len(saved) == 2


# ============================================================================
# Kelly Criterion Tests
# ============================================================================

class TestKellyCriterion:
    """Tests for Kelly criterion sizing."""

    def test_load_recent_performance_empty(self, tmp_path):
        """Test loading performance when no file exists."""
        with patch("api.routes.engine.RECENT_TRADES_FILE", tmp_path / "trades.json"):
            perf = load_recent_performance()
        assert "trades" in perf
        assert perf["last_5_wins"] == 0

    def test_calculate_dynamic_kelly_base(self):
        """Test basic Kelly calculation."""
        with patch("api.routes.engine.load_recent_performance") as mock_perf:
            mock_perf.return_value = {"last_5_win_rate": 0.5}
            signal = {"confidence": 50, "source": "test"}
            result = calculate_dynamic_kelly(signal)

        assert "kelly_fraction" in result
        assert 0.1 <= result["kelly_fraction"] <= 0.5

    def test_calculate_dynamic_kelly_hot_streak(self):
        """Test Kelly increases on hot streak."""
        with patch("api.routes.engine.load_recent_performance") as mock_perf:
            mock_perf.return_value = {"last_5_win_rate": 0.8}
            signal = {"confidence": 70, "source": "test"}
            result = calculate_dynamic_kelly(signal)

        assert result["kelly_fraction"] > 0.25  # Should be boosted

    def test_calculate_dynamic_kelly_losing_streak(self):
        """Test Kelly decreases on losing streak."""
        with patch("api.routes.engine.load_recent_performance") as mock_perf:
            mock_perf.return_value = {"last_5_win_rate": 0.2}
            signal = {"confidence": 50, "source": "test"}
            result = calculate_dynamic_kelly(signal)

        assert result["kelly_fraction"] < 0.25  # Should be reduced


# ============================================================================
# LLM Validation Tests
# ============================================================================

class TestLLMValidation:
    """Tests for LLM signal validation."""

    def test_llm_validate_signal_disabled(self):
        """Test LLM validation when disabled."""
        with patch("api.routes.engine.LLM_VALIDATION_ENABLED", False):
            result = llm_validate_signal({"market": "test", "side": "YES"})

        assert result["adjustment"] == 0
        assert result["veto"] is False
        assert "disabled" in result["reasoning"]

    def test_llm_validate_signal_cached(self):
        """Test LLM validation uses cache."""
        cache_key = "test_market:YES"
        cached_result = {"adjustment": 5, "reasoning": "Cached", "veto": False}

        with patch("api.routes.engine.LLM_VALIDATION_ENABLED", True):
            with patch.dict(
                "api.routes.engine.LLM_VALIDATION_CACHE",
                {cache_key: {"timestamp": datetime.now(), "result": cached_result}}
            ):
                result = llm_validate_signal({"market_id": "test_market", "side": "YES"})

        assert result["adjustment"] == 5
        assert result["reasoning"] == "Cached"


# ============================================================================
# Router Integration Tests
# ============================================================================

class TestEngineRouterEndpoints:
    """Tests for engine router HTTP endpoints."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_get_engine_status(self, client, tmp_path, mock_engine_state):
        """Test GET /engine/status endpoint."""
        state_file = tmp_path / "engine_state.json"
        state_file.write_text(json.dumps(mock_engine_state))
        balance_file = tmp_path / "balance.json"
        balance_file.write_text(json.dumps({"usdc": 10000.0}))
        positions_file = tmp_path / "positions.json"
        positions_file.write_text(json.dumps([]))

        with patch("api.routes.engine.ENGINE_STATE_FILE", state_file):
            with patch("api.routes.engine.BALANCE_FILE", balance_file):
                with patch("api.routes.engine.POSITIONS_FILE", positions_file):
                    with patch("api.routes.engine.DATA_DIR", tmp_path):
                        with patch("api.routes.engine.STORAGE_DIR", tmp_path):
                            response = client.get("/engine/status")

        assert response.status_code == 200
        data = response.json()
        assert "running" in data
        assert "config" in data
        assert "adaptive" in data
        assert "protection" in data

    def test_get_engine_config(self, client, tmp_path, mock_engine_state):
        """Test GET /engine/config endpoint."""
        state_file = tmp_path / "engine_state.json"
        state_file.write_text(json.dumps(mock_engine_state))

        with patch("api.routes.engine.ENGINE_STATE_FILE", state_file):
            with patch("api.routes.engine.DATA_DIR", tmp_path):
                response = client.get("/engine/config")

        assert response.status_code == 200
        data = response.json()
        assert data["min_confidence"] == 35
        assert data["max_per_trade"] == 100

    def test_get_kelly_current(self, client, tmp_path):
        """Test GET /kelly/current endpoint."""
        trades_file = tmp_path / "trades.json"
        trades_file.write_text(json.dumps({"trades": [], "last_5_wins": 0, "last_5_total": 0}))

        with patch("api.routes.engine.RECENT_TRADES_FILE", trades_file):
            response = client.get("/kelly/current")

        assert response.status_code == 200
        data = response.json()
        assert "sample_kelly" in data
        assert "base_kelly" in data

    def test_get_alerts(self, client, tmp_path, mock_price_alerts):
        """Test GET /alerts endpoint."""
        alerts_file = tmp_path / "alerts.json"
        alerts_file.write_text(json.dumps(mock_price_alerts))

        with patch("api.routes.engine.PRICE_ALERTS_FILE", alerts_file):
            response = client.get("/alerts")

        assert response.status_code == 200
        data = response.json()
        assert "alerts" in data
        assert data["count"] == 2

    def test_get_llm_status(self, client):
        """Test GET /llm/status endpoint."""
        response = client.get("/llm/status")

        assert response.status_code == 200
        data = response.json()
        assert "enabled" in data
        assert "provider" in data
        assert "cache_size" in data


class TestPhaseEndpoints:
    """Tests for phase-related endpoints."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_get_current_phase_disabled(self, client):
        """Test GET /phase/current when phase scaling is disabled."""
        with patch("api.routes.engine.PHASE_SCALING_ENABLED", False):
            response = client.get("/phase/current")

        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is False

    def test_get_phase_history(self, client, tmp_path):
        """Test GET /phase/history endpoint."""
        state_file = tmp_path / "engine_state.json"
        state_file.write_text(json.dumps({"phase_history": []}))

        with patch("api.routes.engine.PHASE_SCALING_ENABLED", True):
            with patch("api.routes.engine.ENGINE_STATE_FILE", state_file):
                with patch("api.routes.engine.DATA_DIR", tmp_path):
                    response = client.get("/phase/history")

        assert response.status_code == 200
        data = response.json()
        assert "history" in data

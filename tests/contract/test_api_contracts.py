"""API contract tests for Polyclawd.

These tests verify that API responses conform to expected schemas,
ensuring backward compatibility as we refactor.
"""
import pytest
from decimal import Decimal
from pydantic import ValidationError

from api.models import (
    TradeRequest,
    TradeResponse,
    SignalSource,
    AggregatedSignal,
    EngineStatus,
    HealthResponse,
    ReadyResponse,
    MetricsResponse,
)


class TestBalanceContract:
    """Contract tests for balance-related models."""

    def test_trade_response_with_balance(self):
        """TradeResponse should accept Decimal balance."""
        response = TradeResponse(
            success=True,
            trade_id="trade_123",
            message="Trade executed",
            balance=Decimal("9900.50"),
        )
        assert response.balance == Decimal("9900.50")

    def test_trade_response_without_balance(self):
        """TradeResponse balance should be optional."""
        response = TradeResponse(
            success=False,
            message="Trade failed",
            error_code="INSUFFICIENT_FUNDS",
        )
        assert response.balance is None

    def test_trade_response_json_encoding(self):
        """Decimal should serialize to string in JSON."""
        response = TradeResponse(
            success=True,
            message="ok",
            balance=Decimal("100.25"),
        )
        json_dict = response.model_dump(mode="json")
        # Decimal should be converted to string
        assert json_dict["balance"] == "100.25"


class TestPositionsContract:
    """Contract tests for position-related models."""

    def test_signal_source_valid(self):
        """SignalSource should accept valid confidence values."""
        source = SignalSource(
            source="whale_tracker",
            direction="YES",
            confidence=0.75,
            reasoning="Large buy detected",
        )
        assert source.confidence == 0.75

    def test_signal_source_confidence_bounds(self):
        """SignalSource confidence must be 0-1."""
        with pytest.raises(ValidationError):
            SignalSource(
                source="test",
                direction="YES",
                confidence=1.5,  # Invalid: > 1
            )

        with pytest.raises(ValidationError):
            SignalSource(
                source="test",
                direction="NO",
                confidence=-0.1,  # Invalid: < 0
            )

    def test_aggregated_signal_valid(self):
        """AggregatedSignal should accept valid score values."""
        signal = AggregatedSignal(
            market_id="0x123abc",
            direction="YES",
            score=85.5,
            sources=[],
            conflicts=0,
        )
        assert signal.score == 85.5

    def test_aggregated_signal_score_bounds(self):
        """AggregatedSignal score must be 0-100."""
        with pytest.raises(ValidationError):
            AggregatedSignal(
                market_id="0x123",
                direction="YES",
                score=150,  # Invalid: > 100
                sources=[],
            )


class TestTradeContract:
    """Contract tests for trade request/response models."""

    def test_trade_request_valid(self):
        """TradeRequest should accept valid data."""
        request = TradeRequest(
            market_id="0x123abc",
            side="YES",
            amount=Decimal("50.00"),
            reasoning="Test trade",
        )
        assert request.side == "YES"
        assert request.amount == Decimal("50.00")

    def test_trade_request_side_literal(self):
        """TradeRequest side must be YES or NO."""
        with pytest.raises(ValidationError):
            TradeRequest(
                market_id="0x123",
                side="MAYBE",  # Invalid
                amount=Decimal("10"),
            )

    def test_trade_request_amount_bounds(self):
        """TradeRequest amount must be > 0 and <= 100."""
        # Too low
        with pytest.raises(ValidationError):
            TradeRequest(
                market_id="0x123",
                side="YES",
                amount=Decimal("0"),  # Invalid: not > 0
            )

        # Too high
        with pytest.raises(ValidationError):
            TradeRequest(
                market_id="0x123",
                side="YES",
                amount=Decimal("101"),  # Invalid: > 100
            )

    def test_trade_request_market_id_validation(self):
        """TradeRequest should block path traversal in market_id."""
        with pytest.raises(ValidationError):
            TradeRequest(
                market_id="../etc/passwd",  # Path traversal
                side="YES",
                amount=Decimal("10"),
            )

        with pytest.raises(ValidationError):
            TradeRequest(
                market_id="foo/bar",  # Contains slash
                side="YES",
                amount=Decimal("10"),
            )

    def test_trade_request_market_id_length(self):
        """TradeRequest market_id must be 1-100 chars."""
        # Empty
        with pytest.raises(ValidationError):
            TradeRequest(
                market_id="",
                side="YES",
                amount=Decimal("10"),
            )

        # Too long
        with pytest.raises(ValidationError):
            TradeRequest(
                market_id="x" * 101,
                side="YES",
                amount=Decimal("10"),
            )

    def test_trade_response_error_code(self):
        """TradeResponse should include error_code on failure."""
        response = TradeResponse(
            success=False,
            message="Rate limit exceeded",
            error_code="RATE_LIMIT",
        )
        assert response.error_code == "RATE_LIMIT"


class TestSignalsContract:
    """Contract tests for signal-related models."""

    def test_engine_status_valid(self):
        """EngineStatus should accept valid configuration."""
        status = EngineStatus(
            running=True,
            mode="paper",
            phase=3,
            daily_trades=5,
            daily_limit=10,
        )
        assert status.mode == "paper"
        assert status.phase == 3

    def test_engine_status_mode_literal(self):
        """EngineStatus mode must be paper, live, or disabled."""
        with pytest.raises(ValidationError):
            EngineStatus(
                running=True,
                mode="sandbox",  # Invalid
                phase=1,
            )

    def test_engine_status_phase_bounds(self):
        """EngineStatus phase must be 1-10."""
        with pytest.raises(ValidationError):
            EngineStatus(
                running=True,
                mode="paper",
                phase=0,  # Invalid: < 1
            )

        with pytest.raises(ValidationError):
            EngineStatus(
                running=True,
                mode="live",
                phase=11,  # Invalid: > 10
            )

    def test_aggregated_signal_with_sources(self):
        """AggregatedSignal should contain list of SignalSource."""
        sources = [
            SignalSource(source="whale", direction="YES", confidence=0.8),
            SignalSource(source="news", direction="YES", confidence=0.6),
        ]
        signal = AggregatedSignal(
            market_id="0xabc",
            direction="YES",
            score=75,
            sources=sources,
            conflicts=0,
        )
        assert len(signal.sources) == 2
        assert signal.sources[0].source == "whale"


class TestSystemEndpointsContract:
    """Contract tests for system endpoint response models."""

    def test_health_response(self):
        """HealthResponse should have required fields."""
        from datetime import datetime

        response = HealthResponse(
            status="healthy",
            timestamp=datetime.now(),
            version="2.0.0",
        )
        assert response.status == "healthy"
        assert response.version == "2.0.0"

    def test_ready_response(self):
        """ReadyResponse should indicate readiness with check details."""
        response = ReadyResponse(
            ready=True,
            checks={"storage": True, "database": True},
        )
        assert response.ready is True
        assert response.checks["storage"] is True

    def test_ready_response_not_ready(self):
        """ReadyResponse should indicate failure correctly."""
        response = ReadyResponse(
            ready=False,
            checks={"storage": True, "database": False},
        )
        assert response.ready is False

    def test_metrics_response(self):
        """MetricsResponse should have uptime and request count."""
        response = MetricsResponse(
            uptime_seconds=3600.5,
            request_count=1000,
            version="2.0.0",
        )
        assert response.uptime_seconds == 3600.5
        assert response.request_count == 1000

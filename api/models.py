"""Pydantic models for Polyclawd API.

Consolidated models with proper validation using Literal, Field, and Decimal.
"""
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Optional
import re

from pydantic import BaseModel, Field, field_validator, field_serializer


# Custom Decimal type with serialization
DecimalStr = Annotated[Decimal, Field()]


class TradeRequest(BaseModel):
    """Request model for placing a trade."""
    market_id: str = Field(..., min_length=1, max_length=100)
    side: Literal["YES", "NO"]
    amount: Decimal = Field(..., gt=0, le=100)
    reasoning: str = Field("", max_length=500)

    @field_serializer("amount")
    def serialize_amount(self, v: Decimal) -> str:
        return str(v)

    @field_validator("market_id")
    @classmethod
    def validate_market_id(cls, v: str) -> str:
        """Block path traversal characters in market_id."""
        if ".." in v or "/" in v or "\\" in v:
            raise ValueError("market_id contains invalid characters")
        # Also block null bytes and other control characters
        if re.search(r"[\x00-\x1f]", v):
            raise ValueError("market_id contains control characters")
        return v


class TradeResponse(BaseModel):
    """Response model for trade operations."""
    success: bool
    trade_id: Optional[str] = None
    message: str
    balance: Optional[Decimal] = None
    error_code: Optional[str] = None

    @field_serializer("balance", when_used="json")
    def serialize_balance(self, v: Optional[Decimal]) -> Optional[str]:
        return str(v) if v is not None else None


class SignalSource(BaseModel):
    """Individual signal source data."""
    source: str
    direction: Literal["YES", "NO"]
    confidence: float = Field(..., ge=0, le=1)
    reasoning: Optional[str] = None


class AggregatedSignal(BaseModel):
    """Aggregated signal from multiple sources."""
    market_id: str
    direction: Literal["YES", "NO"]
    score: float = Field(..., ge=0, le=100)
    sources: list[SignalSource] = Field(default_factory=list)
    conflicts: int = 0


class EngineStatus(BaseModel):
    """Trading engine status."""
    running: bool
    mode: Literal["paper", "live", "disabled"]
    phase: int = Field(..., ge=1, le=10)
    daily_trades: int = 0
    daily_limit: int = 10
    last_run: Optional[datetime] = None


# System endpoint models
class HealthResponse(BaseModel):
    """Health check response."""
    status: str = "healthy"
    timestamp: datetime
    version: str = "2.0.0"


class ReadyResponse(BaseModel):
    """Readiness check response."""
    ready: bool
    checks: dict[str, bool]


class MetricsResponse(BaseModel):
    """Basic metrics response."""
    uptime_seconds: float
    request_count: int = 0
    version: str = "2.0.0"

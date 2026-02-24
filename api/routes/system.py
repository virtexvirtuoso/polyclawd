"""System routes for health, readiness, and metrics."""
import logging
from datetime import datetime
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.util import get_remote_address

from api.deps import get_storage_service
from api.models import HealthResponse, ReadyResponse, MetricsResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# Rate limiter (will use app.state.limiter at runtime)
limiter = Limiter(key_func=get_remote_address)

# Track startup time for uptime calculation
_startup_time = datetime.now()


@router.get("/health", response_model=HealthResponse)
@limiter.limit("60/minute")
async def health(request: Request) -> HealthResponse:
    """Health check endpoint.

    Returns basic health status for load balancers and monitoring.
    """
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now(),
        version="2.0.0"
    )


@router.get("/ready", response_model=ReadyResponse)
@limiter.limit("30/minute")
async def ready(request: Request) -> ReadyResponse:
    """Readiness check endpoint.

    Verifies that required services are available:
    - Storage: Can load balance.json

    Returns 200 if all checks pass, data indicates individual check status.
    """
    checks = {}

    # Check storage availability
    try:
        storage = get_storage_service()
        await storage.load("balance.json", default={"balance": 0})
        checks["storage"] = True
    except Exception:
        checks["storage"] = False

    all_ready = all(checks.values())

    return ReadyResponse(
        ready=all_ready,
        checks=checks
    )


@router.get("/api/source-health")
@limiter.limit("30/minute")
async def source_health(request: Request):
    """Get health metrics for all data sources."""
    try:
        from api.services.source_health import get_all_source_health
        health_data = get_all_source_health()
        return JSONResponse(content={"sources": health_data})
    except Exception as e:
        logger.error(f"Source health endpoint error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})


@router.get("/metrics", response_model=MetricsResponse)
@limiter.limit("30/minute")
async def metrics(request: Request) -> MetricsResponse:
    """Basic metrics endpoint.

    Returns uptime and request count placeholder.
    """
    uptime = (datetime.now() - _startup_time).total_seconds()

    return MetricsResponse(
        uptime_seconds=uptime,
        request_count=0,  # Placeholder - would need middleware to track
        version="2.0.0"
    )

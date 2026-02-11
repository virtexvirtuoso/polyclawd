#!/usr/bin/env python3
"""
Polyclawd Trading API - FastAPI Application Factory

Paper trading + Simmer SDK live trading integration.
All endpoints are defined in api/routes/ modules.

Performance enhancements:
- #9:  Per-API connection pooling with keep-alive
- #2:  WebSocket feeds initialized at startup
- #3:  In-memory market state store
- #10: Event-driven engine wired to WebSocket events
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from api.deps import get_settings
from api.middleware import add_security_headers, global_exception_handler
from api.routes import (
    edge_scanner_router,
    engine_router,
    markets_router,
    signals_router,
    system_router,
    trading_router,
)

logger = logging.getLogger(__name__)

# Shared HTTP client for async requests
http_client: httpx.AsyncClient = None

# Rate limiter setup
limiter = Limiter(key_func=get_remote_address)


def _rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    """Custom handler for rate limit exceeded errors."""
    logger.warning(f"Rate limit exceeded for {get_remote_address(request)}: {exc.detail}")
    return JSONResponse(
        status_code=429,
        content={"error": "Rate limit exceeded", "detail": str(exc.detail)},
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - startup and shutdown.

    Initializes all performance-critical subsystems:
    - Per-API connection pools (#9)
    - WebSocket feeds (#2)
    - In-memory market state (#3, #8)
    - Event-driven engine wiring (#10)
    """
    global http_client

    # Startup
    logger.info("Starting Polyclawd Trading API v2.1.0")

    # Ensure data directories exist
    settings = get_settings()
    settings.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    settings.POLY_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Create shared HTTP client (legacy)
    http_client = httpx.AsyncClient(timeout=30.0)
    logger.info("HTTP client initialized")

    # Enhancement #9: Initialize per-API connection pools
    try:
        from api.services.http_client import api_pool
        await api_pool.initialize()
        logger.info("Per-API connection pools initialized")
    except Exception as e:
        logger.warning(f"API pool init failed (non-fatal): {e}")

    # Enhancement #2 + #10: Initialize WebSocket feeds with event-driven handler
    try:
        from api.services.ws_feeds import ws_manager, handle_polymarket_price_update
        from api.routes.engine import event_driven_handler

        ws_manager.on_event(handle_polymarket_price_update)
        ws_manager.on_event(event_driven_handler)
        await ws_manager.start_all()
        logger.info("WebSocket feeds started")
    except Exception as e:
        logger.warning(f"WebSocket init failed (non-fatal, will use polling): {e}")

    yield

    # Shutdown
    if http_client:
        await http_client.aclose()
        logger.info("HTTP client closed")

    # Close per-API pools
    try:
        from api.services.http_client import api_pool
        await api_pool.close_all()
    except Exception:
        pass

    # Stop WebSocket feeds
    try:
        from api.services.ws_feeds import ws_manager
        await ws_manager.stop_all()
    except Exception:
        pass

    logger.info("Polyclawd shutdown complete")


# Application factory
app = FastAPI(
    title="Polyclawd Trading API",
    version="2.1.0",
    lifespan=lifespan,
)

# CORS middleware with restricted settings
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)

# Security middleware
app.middleware("http")(add_security_headers)

# Global exception handler
app.exception_handler(Exception)(global_exception_handler)

# Rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ============================================================================
# Router Registration
# ============================================================================

# System routes: /health, /ready, /metrics (no prefix)
app.include_router(system_router, tags=["System"])

# Trading routes: /api/balance, /api/positions, /api/trade, /api/simmer/*, etc.
app.include_router(trading_router, prefix="/api", tags=["Trading"])

# Markets routes: /api/markets/*, /api/arb-scan, /api/vegas/*, etc.
app.include_router(markets_router, prefix="/api", tags=["Markets"])

# Signals routes: /api/signals, /api/whales/*, /api/confidence/*, etc.
app.include_router(signals_router, prefix="/api", tags=["Signals"])

# Engine routes: /api/engine/*, /api/alerts/*, /api/kelly/*, /api/phase/*, etc.
app.include_router(engine_router, prefix="/api", tags=["Engine"])

# Edge scanner routes: /api/edge/scan, /api/edge/topics
app.include_router(edge_scanner_router, tags=["Edge Scanner"])

# ============================================================================
# Static Files & Frontend
# ============================================================================

static_dir = Path(__file__).parent.parent / "static"
app.mount("/css", StaticFiles(directory=str(static_dir / "css")), name="css")
app.mount("/js", StaticFiles(directory=str(static_dir / "js")), name="js")
app.mount("/icons", StaticFiles(directory=str(static_dir / "icons")), name="icons")


@app.get("/")
async def serve_index():
    """Serve the main dashboard page."""
    return FileResponse(str(static_dir / "index.html"))


@app.get("/{page}.html")
async def serve_page(page: str):
    """Serve additional HTML pages."""
    file_path = static_dir / f"{page}.html"
    if file_path.exists():
        return FileResponse(str(file_path))
    raise HTTPException(status_code=404, detail="Page not found")


@app.get("/manifest.json")
async def serve_manifest():
    """Serve PWA manifest."""
    return FileResponse(str(static_dir / "manifest.json"))


@app.get("/sw.js")
async def serve_sw():
    """Serve service worker."""
    return FileResponse(str(static_dir / "sw.js"), media_type="application/javascript")


# ============================================================================
# Main Entry Point
# ============================================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)

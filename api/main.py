#!/usr/bin/env python3
"""
Polyclawd Trading API - FastAPI Application Factory

Paper trading + Simmer SDK live trading integration.
All endpoints are defined in api/routes/ modules.
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.deps import get_settings
from api.middleware import add_security_headers, global_exception_handler
from api.routes import (
    engine_router,
    markets_router,
    signals_router,
    system_router,
    trading_router,
)

logger = logging.getLogger(__name__)

# Shared HTTP client for async requests
http_client: httpx.AsyncClient = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - startup and shutdown."""
    global http_client

    # Startup
    logger.info("Starting Polyclawd Trading API v2.1.0")

    # Ensure data directories exist
    settings = get_settings()
    settings.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    settings.POLY_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Create shared HTTP client
    http_client = httpx.AsyncClient(timeout=30.0)
    logger.info("HTTP client initialized")

    yield

    # Shutdown
    if http_client:
        await http_client.aclose()
        logger.info("HTTP client closed")


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

# ============================================================================
# Static Files & Frontend
# ============================================================================

frontend_dir = Path(__file__).parent.parent
app.mount("/css", StaticFiles(directory=str(frontend_dir / "css")), name="css")
app.mount("/js", StaticFiles(directory=str(frontend_dir / "js")), name="js")


@app.get("/")
async def serve_index():
    """Serve the main dashboard page."""
    return FileResponse(str(frontend_dir / "index.html"))


@app.get("/{page}.html")
async def serve_page(page: str):
    """Serve additional HTML pages."""
    file_path = frontend_dir / f"{page}.html"
    if file_path.exists():
        return FileResponse(str(file_path))
    raise HTTPException(status_code=404, detail="Page not found")


# ============================================================================
# Main Entry Point
# ============================================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)

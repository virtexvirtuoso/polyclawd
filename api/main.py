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
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
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
# Visitor Tracking
# ============================================================================

@app.post("/api/visitor-log")
async def visitor_log(request: Request):
    """Log visitor access for tracking."""
    import sqlite3, json as _json
    try:
        body = await request.json()
    except Exception:
        body = {}

    ip = request.headers.get("x-real-ip", request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown"))
    entry = {
        "timestamp": body.get("timestamp", ""),
        "ip": ip,
        "page": body.get("page", ""),
        "user_agent": body.get("userAgent", ""),
        "screen_size": body.get("screenSize", ""),
        "language": body.get("language", ""),
        "referrer": body.get("referrer", ""),
    }

    db_path = Path(__file__).parent.parent / "storage" / "shadow_trades.db"
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE IF NOT EXISTS visitor_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, ip TEXT, page TEXT,
            user_agent TEXT, screen_size TEXT, language TEXT, referrer TEXT,
            logged_at TEXT DEFAULT (datetime('now'))
        )""")
        conn.execute(
            "INSERT INTO visitor_log (timestamp, ip, page, user_agent, screen_size, language, referrer) VALUES (?,?,?,?,?,?,?)",
            (entry["timestamp"], entry["ip"], entry["page"], entry["user_agent"], entry["screen_size"], entry["language"], entry["referrer"])
        )
        conn.commit()
        conn.close()
        logger.info(f"[VISITOR] {entry['ip']} → {entry['page']} ({entry['screen_size']})")

        # Discord alert
        import urllib.request
        discord_url = "https://discord.com/api/webhooks/1379097202613420163/IJXNvNxw09zXGvQe2oZZ-8TwYc91hZH4PqD6XtVEQa5fH6TpBt9hBLuTZiejUPjW9m8i"
        embed = {
            "embeds": [{
                "title": "🔐 Polyclawd Login",
                "color": 0x6c5ce7,
                "fields": [
                    {"name": "IP", "value": entry["ip"], "inline": True},
                    {"name": "Page", "value": entry["page"] or "login", "inline": True},
                    {"name": "Screen", "value": entry["screen_size"], "inline": True},
                    {"name": "User Agent", "value": (entry["user_agent"] or "unknown")[:200]},
                    {"name": "Referrer", "value": entry["referrer"] or "direct", "inline": True},
                ],
                "timestamp": entry["timestamp"] or None
            }]
        }
        try:
            req = urllib.request.Request(
                discord_url,
                data=_json.dumps(embed).encode(),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception as de:
            logger.warning(f"[VISITOR] Discord alert failed: {de}")

    except Exception as e:
        logger.error(f"[VISITOR] Failed to log: {e}")

    return {"ok": True}


@app.get("/api/visitor-log")
async def get_visitor_log(limit: int = 50):
    """Get recent visitor log entries."""
    import sqlite3
    db_path = Path(__file__).parent.parent / "storage" / "shadow_trades.db"
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE IF NOT EXISTS visitor_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, ip TEXT, page TEXT,
            user_agent TEXT, screen_size TEXT, language TEXT, referrer TEXT,
            logged_at TEXT DEFAULT (datetime('now'))
        )""")
        rows = conn.execute("SELECT * FROM visitor_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        cols = ["id", "timestamp", "ip", "page", "user_agent", "screen_size", "language", "referrer", "logged_at"]
        conn.close()
        return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        return {"error": str(e)}


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

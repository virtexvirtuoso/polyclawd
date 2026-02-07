"""Security middleware for Polyclawd API.

Provides:
- API key authentication
- Security headers (X-Content-Type-Options, X-Frame-Options, etc.)
- Global exception handler with sanitized responses
"""
from fastapi import Request, HTTPException, Header
from fastapi.responses import JSONResponse
from api.deps import get_settings
import logging

logger = logging.getLogger(__name__)


async def verify_api_key(x_api_key: str = Header(..., alias="X-API-Key")):
    """Verify API key for protected endpoints.

    If no API keys are configured, authentication is disabled (development mode).
    """
    settings = get_settings()
    if not settings.API_KEYS:
        # Development mode - no auth required
        logger.warning("No API keys configured - authentication disabled")
        return None
    if x_api_key not in settings.API_KEYS:
        logger.warning(f"Invalid API key attempt: {x_api_key[:8]}...")
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key


async def add_security_headers(request: Request, call_next):
    """Add security headers to all responses."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # Only add HSTS in production
    if "virtuosocrypto.com" in request.url.host:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


async def global_exception_handler(request: Request, exc: Exception):
    """Catch-all exception handler - never leak internal details."""
    logger.exception(f"Unhandled error on {request.url.path}: {exc}")
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "path": request.url.path}
    )

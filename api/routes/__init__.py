"""API routes package."""
from .system import router as system_router
from .trading import router as trading_router
from .markets import router as markets_router

__all__ = ["system_router", "trading_router", "markets_router"]

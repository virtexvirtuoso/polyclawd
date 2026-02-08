"""API routes package."""
from .system import router as system_router
from .trading import router as trading_router
from .markets import router as markets_router
from .signals import router as signals_router
from .engine import router as engine_router

__all__ = ["system_router", "trading_router", "markets_router", "signals_router", "engine_router"]

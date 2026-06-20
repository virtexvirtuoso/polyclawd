"""API routes package."""

# Set up module search paths once here so route handlers don't need per-call
# sys.path.insert(). signals/, odds/, scripts/ have no __init__.py and are not
# proper packages — they rely on sys.path for bare-name imports.
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.parent
for _p in [
    str(_PROJECT_ROOT / "signals"),
    str(_PROJECT_ROOT / "odds"),
    str(_PROJECT_ROOT / "scripts"),
    str(_PROJECT_ROOT / "config"),
    str(_PROJECT_ROOT),
]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from .system import router as system_router
from .trading import router as trading_router
from .markets import router as markets_router
from .signals import router as signals_router
from .engine import router as engine_router
from .edge_scanner import router as edge_scanner_router
from .live import router as live_router

__all__ = [
    "system_router",
    "trading_router",
    "markets_router",
    "signals_router",
    "engine_router",
    "edge_scanner_router",
    "live_router",
]

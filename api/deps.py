"""Shared dependencies and settings for Polyclawd API.

This module provides:
- Settings class with immutable configuration
- Singleton pattern for shared services
- Dependency injection helpers for FastAPI
"""
from pathlib import Path
from functools import lru_cache
from typing import Optional, TYPE_CHECKING
import os

if TYPE_CHECKING:
    from api.services.storage import StorageService


class Settings:
    """Application settings - immutable after startup"""
    # Storage paths
    STORAGE_DIR = Path.home() / ".openclaw" / "paper-trading"
    POLY_STORAGE_DIR = Path.home() / ".openclaw" / "paper-trading-polymarket"
    DATA_DIR = Path(__file__).parent.parent / "data"

    # Defaults
    DEFAULT_BALANCE = 10000.0

    # Security
    API_KEYS: set[str] = set()  # Loaded from env
    ALLOWED_ORIGINS: list[str] = [
        "https://virtuosocrypto.com",
        "http://localhost:8420",
    ]

    # External APIs
    GAMMA_API = "https://gamma-api.polymarket.com"
    SIMMER_API = "https://api.simmer.markets/api/sdk"

    # Rate limits
    MAX_TRADE_AMOUNT = 100.0
    TRADES_PER_MINUTE = 5


@lru_cache()
def get_settings() -> Settings:
    """Get cached Settings instance with API keys loaded from environment."""
    settings = Settings()
    # Load API keys from environment
    api_keys_str = os.getenv("POLYCLAWD_API_KEYS", "")
    if api_keys_str:
        settings.API_KEYS = set(api_keys_str.split(","))
    return settings


# Singleton storage service - NOT recreated per request
_storage_service: Optional["StorageService"] = None


def get_storage_service() -> "StorageService":
    """Returns singleton StorageService instance."""
    global _storage_service
    if _storage_service is None:
        from api.services.storage import StorageService
        _storage_service = StorageService(get_settings().STORAGE_DIR)
    return _storage_service

"""Shared dependencies and settings for Polyclawd API.

``Settings`` is a pydantic-settings ``BaseSettings``: every field keeps its
previous name and default value, and any field can now be overridden via a
``POLYCLAWD_<NAME>`` environment variable (12-factor). Behaviour is unchanged
when no such env var is set, and ``POLYCLAWD_API_KEYS`` (comma-separated)
continues to work exactly as before.
"""

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

if TYPE_CHECKING:
    from api.services.storage import StorageService

_ROOT = Path(__file__).parent.parent


class Settings(BaseSettings):
    """Application settings — loaded once and cached. Override any field via POLYCLAWD_<NAME>."""

    model_config = SettingsConfigDict(env_prefix="POLYCLAWD_", extra="ignore")

    # Storage paths
    STORAGE_DIR: Path = Path.home() / ".openclaw" / "paper-trading"
    POLY_STORAGE_DIR: Path = Path.home() / ".openclaw" / "paper-trading-polymarket"
    DATA_DIR: Path = _ROOT / "data"

    # Defaults
    DEFAULT_BALANCE: float = 10000.0

    # Security — POLYCLAWD_API_KEYS is comma-separated; empty/unset => no keys (dev mode)
    API_KEYS: Annotated[set[str], NoDecode] = set()
    ALLOWED_ORIGINS: list[str] = [
        "https://virtuosocrypto.com",
        "http://localhost:8420",
    ]

    # External APIs
    GAMMA_API: str = "https://gamma-api.polymarket.com"
    SIMMER_API: str = "https://api.simmer.markets/api/sdk"

    # Rate limits
    MAX_TRADE_AMOUNT: float = 100.0
    TRADES_PER_MINUTE: int = 5

    @field_validator("API_KEYS", mode="before")
    @classmethod
    def _parse_api_keys(cls, v):
        """Accept a comma-separated string (env) or an existing collection."""
        if isinstance(v, str):
            return {k.strip() for k in v.split(",") if k.strip()}
        return v


@lru_cache
def get_settings() -> Settings:
    """Get the cached Settings instance (environment is loaded automatically)."""
    return Settings()


# Singleton storage service - NOT recreated per request
_storage_service: Optional["StorageService"] = None


def get_storage_service() -> "StorageService":
    """Returns singleton StorageService instance."""
    global _storage_service
    if _storage_service is None:
        from api.services.storage import StorageService

        _storage_service = StorageService(get_settings().STORAGE_DIR)
    return _storage_service

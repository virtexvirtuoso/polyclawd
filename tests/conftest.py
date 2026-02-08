# Pytest configuration and fixtures for polyclawd tests
import asyncio
import sys
from pathlib import Path
from typing import AsyncGenerator, Generator

import pytest
import httpx
from fastapi.testclient import TestClient

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from api.main import app


@pytest.fixture
def tmp_storage(tmp_path: Path) -> Path:
    """Temporary storage directory for test artifacts."""
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    return storage


@pytest.fixture
def test_client() -> Generator[TestClient, None, None]:
    """FastAPI TestClient for integration tests - no external server needed."""
    with TestClient(app) as client:
        yield client


@pytest.fixture
def mock_httpx_client(test_client: TestClient) -> Generator[TestClient, None, None]:
    """Alias for test_client - maintains backward compatibility with existing tests."""
    yield test_client


@pytest.fixture
async def async_httpx_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Async httpx client for testing async HTTP interactions."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        yield client


@pytest.fixture(scope="session")
def event_loop():
    """Create an event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def api_base_url() -> str:
    """Base URL for the API - empty string since TestClient uses relative paths."""
    return ""

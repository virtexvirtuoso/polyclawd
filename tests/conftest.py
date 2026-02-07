# Pytest configuration and fixtures for polyclawd tests
import asyncio
from pathlib import Path
from typing import AsyncGenerator, Generator

import pytest
import httpx


@pytest.fixture
def tmp_storage(tmp_path: Path) -> Path:
    """Temporary storage directory for test artifacts."""
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    return storage


@pytest.fixture
def mock_httpx_client() -> Generator[httpx.Client, None, None]:
    """Mock httpx client for testing HTTP interactions."""
    with httpx.Client(timeout=10.0) as client:
        yield client


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
    """Base URL for the API under test."""
    return "http://localhost:8420"

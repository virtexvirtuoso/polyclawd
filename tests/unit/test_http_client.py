"""Unit tests for AsyncHTTPClient."""
import pytest
from api.services.http_client import AsyncHTTPClient, http_client


@pytest.mark.asyncio
async def test_singleton_instance_exists():
    """Module-level http_client should exist."""
    assert http_client is not None
    assert isinstance(http_client, AsyncHTTPClient)


@pytest.mark.asyncio
async def test_client_reuse():
    """Same client should be returned on multiple calls."""
    client = AsyncHTTPClient()
    inner1 = await client._get_client()
    inner2 = await client._get_client()
    assert inner1 is inner2
    await client.close()


@pytest.mark.asyncio
async def test_client_is_none_initially():
    """Client should be None before first use (lazy init)."""
    client = AsyncHTTPClient()
    assert client._client is None
    await client.close()


@pytest.mark.asyncio
async def test_client_created_on_first_call():
    """Client should be created on first _get_client call."""
    client = AsyncHTTPClient()
    await client._get_client()
    assert client._client is not None
    await client.close()


@pytest.mark.asyncio
async def test_close_sets_client_to_none():
    """Close should clean up the client."""
    client = AsyncHTTPClient()
    await client._get_client()  # Initialize
    assert client._client is not None
    await client.close()
    assert client._client is None


@pytest.mark.asyncio
async def test_client_recreated_after_close():
    """Client should be recreated if used after close."""
    client = AsyncHTTPClient()
    first = await client._get_client()
    await client.close()
    second = await client._get_client()
    assert first is not second
    await client.close()


@pytest.mark.asyncio
async def test_connection_limits_configured():
    """Client should have connection pooling configured."""
    client = AsyncHTTPClient(max_connections=30)
    assert client.max_connections == 30
    await client.close()


@pytest.mark.asyncio
async def test_timeout_configured():
    """Client should have timeout configured."""
    client = AsyncHTTPClient(timeout=5.0)
    assert client.timeout == 5.0
    await client.close()

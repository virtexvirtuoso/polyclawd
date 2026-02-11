"""Singleton async HTTP client with connection pooling.

Reuses TCP connections across requests for better performance.
Must call close() on application shutdown.
"""
import httpx
from typing import Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)


class AsyncHTTPClient:
    """Singleton HTTP client with connection pooling.

    Features:
    - Lazy initialization of shared httpx.AsyncClient
    - Connection pooling (max_connections=20, max_keepalive_connections=10)
    - Automatic redirect following
    - Proper error logging
    """

    def __init__(self, timeout: float = 15.0, max_connections: int = 20):
        self.timeout = timeout
        self.max_connections = max_connections
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazy initialization of shared client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                limits=httpx.Limits(
                    max_connections=self.max_connections,
                    max_keepalive_connections=10
                ),
                headers={"User-Agent": "Polyclawd/2.0"},
                follow_redirects=True
            )
            logger.info("HTTP client initialized with connection pooling")
        return self._client

    async def get(self, url: str, headers: Optional[Dict] = None) -> Dict[str, Any]:
        """GET request with shared client."""
        client = await self._get_client()
        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            logger.warning(f"HTTP {e.response.status_code} from {url}")
            raise
        except httpx.RequestError as e:
            logger.error(f"Request failed for {url}: {e}")
            raise

    async def post(self, url: str, data: Dict, headers: Optional[Dict] = None) -> Dict[str, Any]:
        """POST request with shared client."""
        client = await self._get_client()
        try:
            resp = await client.post(url, json=data, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            logger.warning(f"HTTP {e.response.status_code} from POST {url}")
            raise
        except httpx.RequestError as e:
            logger.error(f"POST request failed for {url}: {e}")
            raise

    async def close(self):
        """Close client on application shutdown - MUST be called."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("HTTP client closed")


# Singleton instance - managed by FastAPI lifespan
http_client = AsyncHTTPClient()

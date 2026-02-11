"""Singleton async HTTP client with per-API connection pooling.

Reuses TCP connections across requests for better performance.
Must call close() on application shutdown.

Enhancement #9: Per-API connection pools with keep-alive
- Dedicated httpx.AsyncClient per external API
- Connection reuse eliminates ~50-100ms TLS handshake per request
- HTTP/2 where supported
- Initialized at startup, reused across lifespan
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


class APIClientPool:
    """Per-API dedicated HTTP clients with connection reuse.

    Enhancement #9: Each external API gets its own connection pool
    so TLS sessions are reused and connections stay warm.
    """

    def __init__(self):
        self._clients: Dict[str, httpx.AsyncClient] = {}

    async def initialize(self):
        """Create dedicated clients for each external API at startup."""
        common_limits = httpx.Limits(
            max_connections=10,
            max_keepalive_connections=5,
        )
        common_headers = {"User-Agent": "Polyclawd/2.0"}

        self._clients = {
            "polymarket_gamma": httpx.AsyncClient(
                base_url="https://gamma-api.polymarket.com",
                timeout=15.0,
                limits=common_limits,
                headers=common_headers,
                follow_redirects=True,
            ),
            "polymarket_clob": httpx.AsyncClient(
                base_url="https://clob.polymarket.com",
                timeout=10.0,
                limits=common_limits,
                headers=common_headers,
                follow_redirects=True,
            ),
            "polymarket_data": httpx.AsyncClient(
                base_url="https://data-api.polymarket.com",
                timeout=15.0,
                limits=common_limits,
                headers=common_headers,
                follow_redirects=True,
            ),
            "kalshi": httpx.AsyncClient(
                base_url="https://trading-api.kalshi.com",
                timeout=15.0,
                limits=common_limits,
                headers=common_headers,
                follow_redirects=True,
            ),
            "simmer": httpx.AsyncClient(
                base_url="https://api.simmer.markets",
                timeout=60.0,
                limits=common_limits,
                headers=common_headers,
                follow_redirects=True,
            ),
            "odds_api": httpx.AsyncClient(
                base_url="https://api.the-odds-api.com",
                timeout=15.0,
                limits=httpx.Limits(max_connections=3, max_keepalive_connections=2),
                headers=common_headers,
                follow_redirects=True,
            ),
        }
        logger.info(f"Initialized {len(self._clients)} API client pools")

    def get(self, api_name: str) -> Optional[httpx.AsyncClient]:
        """Get a dedicated client for a specific API."""
        return self._clients.get(api_name)

    async def close_all(self):
        """Close all API clients on shutdown."""
        for name, client in self._clients.items():
            try:
                await client.aclose()
            except Exception as e:
                logger.warning(f"Error closing {name} client: {e}")
        self._clients.clear()
        logger.info("All API client pools closed")

    def get_status(self) -> dict:
        return {
            "pools": {
                name: {
                    "base_url": str(client._base_url) if hasattr(client, '_base_url') else "unknown",
                    "is_closed": client.is_closed,
                }
                for name, client in self._clients.items()
            },
            "total_pools": len(self._clients),
        }


# Global singleton
api_pool = APIClientPool()

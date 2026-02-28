"""
HF Enrichment Reader — Direct memcached client for Virtuoso data.

Replaces the mcporter subprocess bridge (~500ms/call) with direct
aiomcache reads (<1ms/call) for low-latency trigger evaluation.

Reads cache keys written by virtuoso-trading every ~15s.
"""

import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional

import aiomcache

logger = logging.getLogger("hf_enrichment")

# Virtuoso memcached (same host on VPS)
MEMCACHED_HOST = "localhost"
MEMCACHED_PORT = 11211

# Cache keys written by virtuoso-trading (verified from Virtuoso codebase)
# Note: analysis:market_regime is plain text, all others are JSON
SYMBOL_KEYS = [
    "confluence:score:{symbol}",
    "confluence:breakdown:{symbol}",
    "liquidations:{symbol}",
    "large_trades:{symbol}",
    "orderbook:{symbol}:snapshot",
]

GLOBAL_KEYS = [
    "analysis:market_regime",   # Plain string: "strong_bullish", "bullish", "sideways", etc.
    "analysis:signals",         # JSON dict with signals array
    "market:overview",          # JSON dict with regime, volume, etc.
    "market:tickers",           # JSON dict {symbol: {price, change, volume}}
]


class VirtuosoEnrichmentReader:
    """Reads Virtuoso's cached analysis data directly from memcached."""

    def __init__(self, host: str = MEMCACHED_HOST, port: int = MEMCACHED_PORT):
        self._host = host
        self._port = port
        self._client: Optional[aiomcache.Client] = None
        self._last_read: float = 0.0
        self._last_data: Dict[str, Any] = {}

    async def _get_client(self) -> aiomcache.Client:
        if self._client is None:
            self._client = aiomcache.Client(self._host, self._port, pool_size=2)
        return self._client

    async def _safe_get(self, client: aiomcache.Client, key: str) -> Any:
        """Read a single cache key with error handling."""
        try:
            raw = await client.get(key.encode())
            if raw is None:
                return None
            decoded = raw.decode()
            # analysis:market_regime is plain text, not JSON
            if key == "analysis:market_regime":
                return decoded
            return json.loads(decoded)
        except (json.JSONDecodeError, UnicodeDecodeError):
            try:
                return raw.decode()
            except Exception:
                return None
        except Exception as e:
            logger.debug(f"Cache read failed for {key}: {e}")
            return None

    async def read_enrichment(self, symbol: str = "BTCUSDT") -> Dict[str, Any]:
        """Read all Virtuoso enrichment data for a symbol in one batch.

        Returns dict keyed by cache key name with parsed values.
        Includes metadata about read latency and data freshness.
        """
        start = time.monotonic()
        client = await self._get_client()
        enrichment: Dict[str, Any] = {}

        # Read symbol-specific keys
        for key_template in SYMBOL_KEYS:
            key = key_template.format(symbol=symbol)
            enrichment[key] = await self._safe_get(client, key)

        # Read global keys
        for key in GLOBAL_KEYS:
            enrichment[key] = await self._safe_get(client, key)

        elapsed_ms = (time.monotonic() - start) * 1000
        enrichment["_meta"] = {
            "read_latency_ms": round(elapsed_ms, 2),
            "timestamp": time.time(),
            "symbol": symbol,
            "keys_found": sum(1 for v in enrichment.values() if v is not None and not isinstance(v, dict) or v),
        }

        self._last_read = time.time()
        self._last_data = enrichment
        return enrichment

    async def read_multi_symbol(self, symbols: list[str] = None) -> Dict[str, Dict]:
        """Read enrichment for multiple symbols."""
        if symbols is None:
            symbols = ["BTCUSDT", "ETHUSDT"]
        results = {}
        for symbol in symbols:
            results[symbol] = await self.read_enrichment(symbol)
        return results

    def get_regime(self, enrichment: Dict) -> str:
        """Extract regime classification from enrichment data."""
        regime = enrichment.get("analysis:market_regime")
        if isinstance(regime, str):
            return regime.strip().lower()
        # Fallback: market_regime field inside market:overview
        overview = enrichment.get("market:overview")
        if isinstance(overview, dict):
            return overview.get("market_regime", overview.get("regime", "unknown")).lower()
        return "unknown"

    def get_confluence_score(self, enrichment: Dict, symbol: str = "BTCUSDT") -> float:
        """Extract confluence score (0-100) from enrichment data."""
        score = enrichment.get(f"confluence:score:{symbol}")
        if isinstance(score, dict):
            # Key stores JSON: {"score": 53.8, "sentiment": "...", ...}
            return float(score.get("score", score.get("confluence_score", 50)))
        if score is not None:
            try:
                return float(score)
            except (ValueError, TypeError):
                pass
        # Fallback: try breakdown
        breakdown = enrichment.get(f"confluence:breakdown:{symbol}")
        if isinstance(breakdown, dict):
            return float(breakdown.get("overall_score", breakdown.get("score", breakdown.get("confluence_score", 50))))
        return 50.0

    def get_liquidation_zones(self, enrichment: Dict, symbol: str = "BTCUSDT") -> list:
        """Extract liquidation zones from enrichment data."""
        data = enrichment.get(f"liquidations:{symbol}")
        if isinstance(data, dict):
            return data.get("zones", data.get("clusters", []))
        if isinstance(data, list):
            return data
        return []

    def get_whale_trades(self, enrichment: Dict, symbol: str = "BTCUSDT") -> list:
        """Extract recent whale trades from enrichment data."""
        data = enrichment.get(f"large_trades:{symbol}")
        if isinstance(data, dict):
            return data.get("trades", data.get("events", []))
        if isinstance(data, list):
            return data
        return []

    def get_orderbook_depth(self, enrichment: Dict, symbol: str = "BTCUSDT") -> Dict:
        """Extract orderbook depth summary from enrichment data."""
        data = enrichment.get(f"orderbook:{symbol}:snapshot")
        if isinstance(data, dict):
            return data
        return {}

    def get_cvd_data(self, enrichment: Dict, symbol: str = "BTCUSDT") -> Dict:
        """Extract CVD data from signals or breakdown."""
        breakdown = enrichment.get(f"confluence:breakdown:{symbol}")
        if isinstance(breakdown, dict):
            # CVD is typically in the orderflow component
            orderflow = breakdown.get("orderflow", breakdown.get("order_flow", {}))
            if isinstance(orderflow, dict):
                return orderflow
        return {}

    async def close(self):
        if self._client:
            await self._client.close()
            self._client = None


# Singleton
_reader: Optional[VirtuosoEnrichmentReader] = None

def get_enrichment_reader() -> VirtuosoEnrichmentReader:
    global _reader
    if _reader is None:
        _reader = VirtuosoEnrichmentReader()
    return _reader


if __name__ == "__main__":
    async def test():
        reader = get_enrichment_reader()
        print("Reading Virtuoso enrichment data...")
        data = await reader.read_enrichment("BTCUSDT")
        meta = data.get("_meta", {})
        print(f"  Read latency: {meta.get('read_latency_ms', '?')}ms")
        print(f"  Keys found: {meta.get('keys_found', 0)}")
        print(f"  Regime: {reader.get_regime(data)}")
        print(f"  Confluence: {reader.get_confluence_score(data, 'BTCUSDT')}")
        print(f"  Liq zones: {len(reader.get_liquidation_zones(data, 'BTCUSDT'))}")
        print(f"  Whale trades: {len(reader.get_whale_trades(data, 'BTCUSDT'))}")
        await reader.close()

    asyncio.run(test())

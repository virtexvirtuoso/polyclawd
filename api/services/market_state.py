"""In-memory market state store for real-time signal processing.

Provides a centralized, thread-safe state object that gets incrementally
updated as signals arrive (via WebSocket or polling). Eliminates the need
to recompute everything from scratch each scan cycle.

Enhancements implemented:
- #3: Pre-computed Bayesian scores with incremental updates
- #6: Orderbook snapshot caching for Kelly sizing
- #8: In-process signal state (no API round-trips)
- #11: $10K volume floor pre-filter
- #13: Contested + expiring market hot watchlist
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Volume floor: markets below this are filtered out (200x error reduction)
VOLUME_FLOOR_USD = 10_000

# Contested market definition: price between 30-70%
CONTESTED_PRICE_LOW = 0.30
CONTESTED_PRICE_HIGH = 0.70

# Expiring threshold: hours until resolution
EXPIRING_HOURS = 48


@dataclass
class MarketSnapshot:
    """Point-in-time snapshot of a market's state."""
    market_id: str
    title: str = ""
    yes_price: float = 0.5
    no_price: float = 0.5
    volume_24h: float = 0.0
    liquidity: float = 0.0
    end_date: Optional[datetime] = None
    category: str = ""
    slug: str = ""
    last_updated: float = field(default_factory=time.time)

    # Orderbook snapshot (cached for Kelly sizing)
    orderbook_bids: list = field(default_factory=list)
    orderbook_asks: list = field(default_factory=list)
    orderbook_updated: float = 0.0

    @property
    def is_liquid(self) -> bool:
        """Passes the $10K volume floor pre-filter."""
        return self.volume_24h >= VOLUME_FLOOR_USD

    @property
    def is_contested(self) -> bool:
        """Price in the 30-70% uncertainty zone."""
        return CONTESTED_PRICE_LOW <= self.yes_price <= CONTESTED_PRICE_HIGH

    @property
    def hours_until_resolution(self) -> Optional[float]:
        """Hours until market resolves, or None if no end date."""
        if not self.end_date:
            return None
        delta = self.end_date - datetime.now()
        return max(0, delta.total_seconds() / 3600)

    @property
    def is_expiring_soon(self) -> bool:
        """Resolves within EXPIRING_HOURS."""
        hours = self.hours_until_resolution
        return hours is not None and 0 < hours <= EXPIRING_HOURS

    @property
    def is_high_value_target(self) -> bool:
        """Contested + expiring + liquid = highest-value target."""
        return self.is_liquid and self.is_contested and self.is_expiring_soon


@dataclass
class SignalScore:
    """Cached signal score for a market/source combination."""
    source: str
    market_id: str
    side: str
    raw_confidence: float
    bayesian_confidence: float
    category_multiplier: float = 1.0
    final_confidence: float = 0.0
    reasoning: str = ""
    timestamp: float = field(default_factory=time.time)


class MarketStateStore:
    """Central in-memory state for all market data and signals.

    Thread-safe via asyncio.Lock. All engine internals read from this
    store directly instead of making API calls to themselves.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        # Market snapshots keyed by market_id
        self._markets: dict[str, MarketSnapshot] = {}
        # Signal scores keyed by (market_id, source)
        self._signals: dict[tuple[str, str], SignalScore] = {}
        # Composite scores keyed by market_id (pre-computed)
        self._composite_scores: dict[str, float] = {}
        # Hot watchlist: contested + expiring markets
        self._hot_watchlist: set[str] = set()
        # Metrics
        self._update_count = 0
        self._last_full_refresh = 0.0

    async def update_market(self, market_id: str, **kwargs) -> MarketSnapshot:
        """Incrementally update a market's state."""
        async with self._lock:
            if market_id not in self._markets:
                self._markets[market_id] = MarketSnapshot(market_id=market_id)

            snapshot = self._markets[market_id]
            for key, value in kwargs.items():
                if hasattr(snapshot, key):
                    setattr(snapshot, key, value)
            snapshot.last_updated = time.time()

            # Update hot watchlist membership
            if snapshot.is_high_value_target:
                self._hot_watchlist.add(market_id)
            else:
                self._hot_watchlist.discard(market_id)

            self._update_count += 1
            return snapshot

    async def update_orderbook(self, market_id: str, bids: list, asks: list):
        """Update cached orderbook snapshot for a market."""
        async with self._lock:
            if market_id not in self._markets:
                self._markets[market_id] = MarketSnapshot(market_id=market_id)
            self._markets[market_id].orderbook_bids = bids
            self._markets[market_id].orderbook_asks = asks
            self._markets[market_id].orderbook_updated = time.time()

    async def update_signal(self, source: str, market_id: str, side: str,
                            raw_confidence: float, bayesian_confidence: float,
                            category_multiplier: float = 1.0,
                            reasoning: str = "") -> SignalScore:
        """Update a signal score and recompute composite."""
        final = bayesian_confidence * category_multiplier
        score = SignalScore(
            source=source,
            market_id=market_id,
            side=side,
            raw_confidence=raw_confidence,
            bayesian_confidence=bayesian_confidence,
            category_multiplier=category_multiplier,
            final_confidence=final,
            reasoning=reasoning,
        )
        async with self._lock:
            self._signals[(market_id, source)] = score
            # Recompute composite for this market
            market_signals = [
                s for k, s in self._signals.items() if k[0] == market_id
            ]
            if market_signals:
                self._composite_scores[market_id] = max(
                    s.final_confidence for s in market_signals
                )
            return score

    async def get_market(self, market_id: str) -> Optional[MarketSnapshot]:
        """Get a market snapshot."""
        async with self._lock:
            return self._markets.get(market_id)

    async def get_all_markets(self) -> dict[str, MarketSnapshot]:
        """Get all market snapshots."""
        async with self._lock:
            return dict(self._markets)

    async def get_hot_watchlist(self) -> list[MarketSnapshot]:
        """Get contested + expiring markets (highest-value targets)."""
        async with self._lock:
            return [
                self._markets[mid] for mid in self._hot_watchlist
                if mid in self._markets
            ]

    async def get_liquid_markets(self) -> list[MarketSnapshot]:
        """Get all markets passing the $10K volume floor."""
        async with self._lock:
            return [m for m in self._markets.values() if m.is_liquid]

    async def get_signals_for_market(self, market_id: str) -> list[SignalScore]:
        """Get all signal scores for a market."""
        async with self._lock:
            return [
                s for k, s in self._signals.items() if k[0] == market_id
            ]

    async def get_top_signals(self, limit: int = 20) -> list[SignalScore]:
        """Get top signals by final confidence."""
        async with self._lock:
            all_signals = list(self._signals.values())
            all_signals.sort(key=lambda s: s.final_confidence, reverse=True)
            return all_signals[:limit]

    async def get_composite_score(self, market_id: str) -> float:
        """Get pre-computed composite score for a market."""
        async with self._lock:
            return self._composite_scores.get(market_id, 0.0)

    async def get_orderbook(self, market_id: str) -> Optional[dict]:
        """Get cached orderbook for Kelly sizing (no fresh API call needed)."""
        async with self._lock:
            m = self._markets.get(market_id)
            if not m or not m.orderbook_bids:
                return None
            return {
                "bids": m.orderbook_bids,
                "asks": m.orderbook_asks,
                "age_seconds": time.time() - m.orderbook_updated,
            }

    async def bulk_update_markets(self, markets: list[dict]):
        """Bulk update from API response (used during full refresh)."""
        import json as _json
        async with self._lock:
            for m in markets:
                mid = m.get("id", "")
                if not mid:
                    continue

                yes_price = 0.5
                if m.get("outcomePrices"):
                    try:
                        prices = m["outcomePrices"]
                        if isinstance(prices, str):
                            prices = _json.loads(prices)
                        yes_price = float(prices[0])
                    except Exception:
                        pass

                end_date = None
                if m.get("endDate"):
                    try:
                        end_date = datetime.fromisoformat(
                            m["endDate"].replace("Z", "+00:00").replace("+00:00", "")
                        )
                    except Exception:
                        pass

                snapshot = self._markets.get(mid, MarketSnapshot(market_id=mid))
                snapshot.title = m.get("question", "")
                snapshot.yes_price = yes_price
                snapshot.no_price = 1.0 - yes_price
                snapshot.volume_24h = float(m.get("volume24hr", 0) or 0)
                snapshot.liquidity = float(m.get("liquidityNum", 0) or 0)
                snapshot.end_date = end_date
                snapshot.slug = m.get("slug", "")
                snapshot.last_updated = time.time()
                self._markets[mid] = snapshot

                if snapshot.is_high_value_target:
                    self._hot_watchlist.add(mid)
                else:
                    self._hot_watchlist.discard(mid)

            self._last_full_refresh = time.time()

    async def get_status(self) -> dict:
        """Get state store metrics."""
        async with self._lock:
            liquid_count = sum(1 for m in self._markets.values() if m.is_liquid)
            return {
                "total_markets": len(self._markets),
                "liquid_markets": liquid_count,
                "filtered_out": len(self._markets) - liquid_count,
                "hot_watchlist": len(self._hot_watchlist),
                "active_signals": len(self._signals),
                "update_count": self._update_count,
                "last_full_refresh": self._last_full_refresh,
            }

    async def clear(self):
        """Clear all state."""
        async with self._lock:
            self._markets.clear()
            self._signals.clear()
            self._composite_scores.clear()
            self._hot_watchlist.clear()
            self._update_count = 0


# Global singleton
market_state = MarketStateStore()

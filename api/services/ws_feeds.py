"""WebSocket feed manager for real-time market data.

Replaces 30-second polling with persistent WebSocket subscriptions
for the highest-value data streams: price changes, volume spikes,
and orderbook updates.

Enhancements implemented:
- #2: WebSocket-first for Polymarket and Kalshi price feeds
- #4: Fast path (<=5s) via WebSocket events
- #10: Event-driven engine trigger on price changes
"""
import asyncio
import json
import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Polymarket CLOB WebSocket endpoint
POLYMARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
# Kalshi WebSocket endpoint
KALSHI_WS_URL = "wss://trading-api.kalshi.com/trade-api/ws/v2"


class WebSocketFeed:
    """Manages a persistent WebSocket connection with auto-reconnect.

    On each message, invokes registered callbacks which can update
    the in-memory MarketStateStore and trigger engine evaluations.
    """

    def __init__(self, name: str, url: str, max_reconnect_delay: float = 60.0):
        self.name = name
        self.url = url
        self.max_reconnect_delay = max_reconnect_delay
        self._callbacks: list[Callable] = []
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._reconnect_delay = 1.0
        self._message_count = 0
        self._last_message_time = 0.0
        self._connected = False

    def on_message(self, callback: Callable):
        """Register a callback for incoming messages."""
        self._callbacks.append(callback)

    async def start(self):
        """Start the WebSocket connection loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._connection_loop())
        logger.info(f"WebSocket feed '{self.name}' starting")

    async def stop(self):
        """Stop the WebSocket connection."""
        self._running = False
        self._connected = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info(f"WebSocket feed '{self.name}' stopped")

    async def _connection_loop(self):
        """Main connection loop with exponential backoff reconnect."""
        try:
            import websockets
        except ImportError:
            logger.warning(
                f"websockets package not installed - {self.name} feed disabled. "
                "Install with: pip install websockets"
            )
            return

        while self._running:
            try:
                async with websockets.connect(
                    self.url,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._connected = True
                    self._reconnect_delay = 1.0
                    logger.info(f"WebSocket '{self.name}' connected to {self.url}")

                    async for raw_message in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(raw_message)
                            self._message_count += 1
                            self._last_message_time = time.time()
                            for cb in self._callbacks:
                                await cb(data)
                        except json.JSONDecodeError:
                            logger.debug(f"Non-JSON message on {self.name}: {raw_message[:100]}")
                        except Exception as e:
                            logger.error(f"Callback error on {self.name}: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                logger.warning(
                    f"WebSocket '{self.name}' disconnected: {e}. "
                    f"Reconnecting in {self._reconnect_delay:.0f}s"
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self.max_reconnect_delay
                )

    @property
    def status(self) -> dict:
        return {
            "name": self.name,
            "connected": self._connected,
            "running": self._running,
            "messages_received": self._message_count,
            "last_message": self._last_message_time,
        }


class WebSocketFeedManager:
    """Manages all WebSocket feeds and routes events to the engine.

    Provides a unified interface for:
    - Subscribing to Polymarket price/volume updates
    - Subscribing to Kalshi market updates
    - Routing events to MarketStateStore and engine triggers
    """

    def __init__(self):
        self._feeds: dict[str, WebSocketFeed] = {}
        self._event_handlers: list[Callable] = []
        self._running = False

    def register_feed(self, name: str, url: str) -> WebSocketFeed:
        """Register a new WebSocket feed."""
        feed = WebSocketFeed(name, url)
        feed.on_message(self._dispatch_event)
        self._feeds[name] = feed
        return feed

    def on_event(self, handler: Callable):
        """Register a handler called on any feed event (for engine triggers)."""
        self._event_handlers.append(handler)

    async def _dispatch_event(self, data: dict):
        """Route incoming WebSocket data to registered event handlers."""
        for handler in self._event_handlers:
            try:
                await handler(data)
            except Exception as e:
                logger.error(f"Event handler error: {e}")

    async def start_all(self):
        """Start all registered feeds."""
        self._running = True
        for feed in self._feeds.values():
            await feed.start()
        logger.info(f"Started {len(self._feeds)} WebSocket feeds")

    async def stop_all(self):
        """Stop all feeds."""
        self._running = False
        for feed in self._feeds.values():
            await feed.stop()
        logger.info("All WebSocket feeds stopped")

    def get_status(self) -> dict:
        """Get status of all feeds."""
        return {
            "feeds": {name: feed.status for name, feed in self._feeds.items()},
            "total_feeds": len(self._feeds),
            "running": self._running,
        }


async def handle_polymarket_price_update(data: dict, state_store=None):
    """Process a Polymarket WebSocket price update.

    Updates the in-memory MarketStateStore with new price data.
    This is the fast path - no full scan needed.
    """
    if state_store is None:
        from api.services.market_state import market_state
        state_store = market_state

    market_id = data.get("market") or data.get("asset_id") or data.get("condition_id")
    if not market_id:
        return

    price = data.get("price") or data.get("yes_price")
    if price is not None:
        await state_store.update_market(
            market_id,
            yes_price=float(price),
            no_price=1.0 - float(price),
        )

    # Orderbook update
    if "bids" in data or "asks" in data:
        await state_store.update_orderbook(
            market_id,
            bids=data.get("bids", []),
            asks=data.get("asks", []),
        )

    # Volume update
    volume = data.get("volume") or data.get("volume_24h")
    if volume is not None:
        await state_store.update_market(market_id, volume_24h=float(volume))


def create_default_feeds() -> WebSocketFeedManager:
    """Create feed manager with default Polymarket and Kalshi feeds."""
    manager = WebSocketFeedManager()
    manager.register_feed("polymarket", POLYMARKET_WS_URL)
    manager.register_feed("kalshi", KALSHI_WS_URL)
    return manager


# Global singleton
ws_manager = create_default_feeds()

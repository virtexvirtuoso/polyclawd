"""Priority queue for signal-driven trade execution.

When multiple signals fire simultaneously (e.g. around news events),
trades are ordered by priority rather than processed FIFO.

Enhancement #7: Priority ordering by:
  1. Confidence score (highest first)
  2. Kelly edge size (largest first)
  3. Time-to-resolution / theta (soonest first)
  4. Volume / liquidity (most liquid first)
"""
import asyncio
import heapq
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass(order=True)
class TradeCandidate:
    """A prioritized trade candidate.

    Lower priority_score = higher priority (min-heap).
    We negate the values we want to maximize.
    """
    priority_score: float
    # Non-comparable fields
    market_id: str = field(compare=False)
    side: str = field(compare=False)
    amount: float = field(compare=False)
    confidence: float = field(compare=False)
    kelly_edge: float = field(compare=False)
    hours_to_resolution: Optional[float] = field(compare=False, default=None)
    volume_24h: float = field(compare=False, default=0.0)
    source: str = field(compare=False, default="")
    reasoning: str = field(compare=False, default="")
    market_title: str = field(compare=False, default="")
    price: float = field(compare=False, default=0.5)
    created_at: float = field(compare=False, default_factory=time.time)
    metadata: dict = field(compare=False, default_factory=dict)


def compute_priority_score(
    confidence: float,
    kelly_edge: float,
    hours_to_resolution: Optional[float],
    volume_24h: float,
) -> float:
    """Compute composite priority score (lower = higher priority).

    Weights:
    - Confidence: 40% (normalized 0-100)
    - Kelly edge: 30% (edge percentage)
    - Theta (time urgency): 20% (inverse hours to resolution)
    - Liquidity: 10% (log-scaled volume)
    """
    import math

    # Negate confidence so higher confidence = lower score
    conf_component = -confidence * 0.40

    # Negate kelly edge
    edge_component = -abs(kelly_edge) * 0.30 * 10  # Scale up edge %

    # Theta: closer to resolution = higher priority
    if hours_to_resolution is not None and hours_to_resolution > 0:
        theta_component = -min(100, 100 / hours_to_resolution) * 0.20
    else:
        theta_component = 0

    # Liquidity: log-scaled volume
    if volume_24h > 0:
        liquidity_component = -min(100, math.log10(max(1, volume_24h)) * 10) * 0.10
    else:
        liquidity_component = 0

    return conf_component + edge_component + theta_component + liquidity_component


class TradePriorityQueue:
    """Async-safe priority queue for trade candidates.

    Trades are dequeued in priority order: highest confidence,
    largest Kelly edge, soonest resolution, most liquid.
    """

    def __init__(self, max_size: int = 100):
        self._heap: list[TradeCandidate] = []
        self._lock = asyncio.Lock()
        self._max_size = max_size
        self._total_enqueued = 0
        self._total_dequeued = 0

    async def enqueue(
        self,
        market_id: str,
        side: str,
        amount: float,
        confidence: float,
        kelly_edge: float = 0.0,
        hours_to_resolution: Optional[float] = None,
        volume_24h: float = 0.0,
        source: str = "",
        reasoning: str = "",
        market_title: str = "",
        price: float = 0.5,
        metadata: Optional[dict] = None,
    ) -> TradeCandidate:
        """Add a trade candidate to the priority queue."""
        priority = compute_priority_score(
            confidence, kelly_edge, hours_to_resolution, volume_24h
        )
        candidate = TradeCandidate(
            priority_score=priority,
            market_id=market_id,
            side=side,
            amount=amount,
            confidence=confidence,
            kelly_edge=kelly_edge,
            hours_to_resolution=hours_to_resolution,
            volume_24h=volume_24h,
            source=source,
            reasoning=reasoning,
            market_title=market_title,
            price=price,
            metadata=metadata or {},
        )
        async with self._lock:
            if len(self._heap) >= self._max_size:
                # Drop lowest priority item
                heapq.heappushpop(self._heap, candidate)
            else:
                heapq.heappush(self._heap, candidate)
            self._total_enqueued += 1
        return candidate

    async def dequeue(self) -> Optional[TradeCandidate]:
        """Get the highest-priority trade candidate."""
        async with self._lock:
            if self._heap:
                self._total_dequeued += 1
                return heapq.heappop(self._heap)
            return None

    async def dequeue_batch(self, max_count: int = 5) -> list[TradeCandidate]:
        """Get up to max_count highest-priority trade candidates."""
        async with self._lock:
            batch = []
            for _ in range(min(max_count, len(self._heap))):
                batch.append(heapq.heappop(self._heap))
                self._total_dequeued += 1
            return batch

    async def peek(self) -> Optional[TradeCandidate]:
        """Look at highest-priority item without removing it."""
        async with self._lock:
            return self._heap[0] if self._heap else None

    async def size(self) -> int:
        async with self._lock:
            return len(self._heap)

    async def clear(self):
        async with self._lock:
            self._heap.clear()

    async def get_status(self) -> dict:
        async with self._lock:
            return {
                "queue_size": len(self._heap),
                "max_size": self._max_size,
                "total_enqueued": self._total_enqueued,
                "total_dequeued": self._total_dequeued,
                "top_candidate": {
                    "market": self._heap[0].market_title[:50] if self._heap else None,
                    "confidence": self._heap[0].confidence if self._heap else None,
                    "priority": self._heap[0].priority_score if self._heap else None,
                } if self._heap else None,
            }


# Global singleton
trade_queue = TradePriorityQueue()

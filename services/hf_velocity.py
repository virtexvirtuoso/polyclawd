"""
HF Velocity Trackers — Rate-of-change metrics for trigger detection.

Computes derivatives that Virtuoso doesn't provide:
- Orderbook imbalance velocity (level changes every 15s, we need speed)
- CVD acceleration (second derivative of cumulative volume delta)
- Liquidation proximity (distance + velocity toward nearest cluster)

All trackers use ring buffers and are fed by the enrichment reader.
"""

import time
from collections import deque
from dataclasses import dataclass
from typing import Optional


@dataclass
class VelocityReading:
    """A single timestamped reading."""
    timestamp: float
    value: float


class ImbalanceVelocityTracker:
    """Tracks rate-of-change of orderbook bid/ask imbalance ratio.

    Imbalance ratio = bid_depth / ask_depth
    Velocity = how fast the ratio is changing (positive = bids growing faster)
    """

    def __init__(self, window_size: int = 12):
        # 12 readings at ~15s intervals = 3 min window
        self._readings: deque[VelocityReading] = deque(maxlen=window_size)

    def update(self, bid_depth: float, ask_depth: float) -> None:
        """Record a new orderbook depth reading."""
        if ask_depth <= 0:
            return
        ratio = bid_depth / ask_depth
        self._readings.append(VelocityReading(time.time(), ratio))

    @property
    def current_ratio(self) -> float:
        """Current bid/ask imbalance ratio."""
        if not self._readings:
            return 1.0
        return self._readings[-1].value

    @property
    def velocity(self) -> float:
        """Rate of change of imbalance ratio per second.

        Positive = bids strengthening relative to asks (bullish)
        Negative = asks strengthening relative to bids (bearish)
        """
        if len(self._readings) < 3:
            return 0.0
        recent = self._readings[-1]
        older = self._readings[-3]  # ~45s ago at 15s intervals
        dt = recent.timestamp - older.timestamp
        if dt <= 0:
            return 0.0
        return (recent.value - older.value) / dt

    @property
    def is_cliff(self) -> bool:
        """True if imbalance ratio > 3:1 (strong directional bias)."""
        return self.current_ratio > 3.0 or self.current_ratio < (1.0 / 3.0)

    @property
    def cliff_direction(self) -> Optional[str]:
        """Direction of the cliff (UP if bids dominate, DOWN if asks dominate)."""
        if self.current_ratio > 3.0:
            return "UP"
        if self.current_ratio < (1.0 / 3.0):
            return "DOWN"
        return None

    @property
    def samples(self) -> int:
        return len(self._readings)


class CVDAccelerationTracker:
    """Tracks first and second derivatives of Cumulative Volume Delta.

    CVD velocity = buying/selling momentum (first derivative)
    CVD acceleration = momentum change rate (second derivative)
    Positive acceleration with flat price = hidden accumulation
    """

    def __init__(self, window_size: int = 12):
        self._readings: deque[VelocityReading] = deque(maxlen=window_size)

    def update(self, cvd_level: float) -> None:
        """Record a new CVD level reading."""
        self._readings.append(VelocityReading(time.time(), cvd_level))

    @property
    def current_level(self) -> float:
        if not self._readings:
            return 0.0
        return self._readings[-1].value

    @property
    def velocity(self) -> float:
        """First derivative: CVD change per second."""
        if len(self._readings) < 2:
            return 0.0
        r = self._readings
        dt = r[-1].timestamp - r[-2].timestamp
        if dt <= 0:
            return 0.0
        return (r[-1].value - r[-2].value) / dt

    @property
    def acceleration(self) -> float:
        """Second derivative: CVD velocity change per second.

        Positive = buying pressure increasing
        Negative = selling pressure increasing
        """
        if len(self._readings) < 3:
            return 0.0
        r = self._readings
        dt1 = r[-1].timestamp - r[-2].timestamp
        dt0 = r[-2].timestamp - r[-3].timestamp
        if dt1 <= 0 or dt0 <= 0:
            return 0.0
        v1 = (r[-1].value - r[-2].value) / dt1
        v0 = (r[-2].value - r[-3].value) / dt0
        avg_dt = (r[-1].timestamp - r[-3].timestamp) / 2
        if avg_dt <= 0:
            return 0.0
        return (v1 - v0) / avg_dt

    def is_divergent(self, price_change_pct: float, threshold: float = 0.01) -> bool:
        """Check if CVD is diverging from price.

        Divergence = price flat/falling but CVD rising (or vice versa).
        This signals hidden accumulation/distribution.
        """
        vel = self.velocity
        if abs(price_change_pct) < 0.05:  # Price effectively flat
            return abs(vel) > threshold
        # Price and CVD moving in opposite directions
        if price_change_pct > 0 and vel < -threshold:
            return True  # Price up but net selling
        if price_change_pct < 0 and vel > threshold:
            return True  # Price down but net buying
        return False

    @property
    def samples(self) -> int:
        return len(self._readings)


class LiquidationProximityTracker:
    """Tracks price distance and velocity toward liquidation clusters.

    Combines Virtuoso's liquidation zone map with real-time price
    to detect when a cascade is mechanically imminent.
    """

    def __init__(self, price_window: int = 30):
        # 30 readings at ~1s from Binance ticks
        self._price_history: deque[VelocityReading] = deque(maxlen=price_window)

    def update_price(self, price: float) -> None:
        """Record a price tick."""
        self._price_history.append(VelocityReading(time.time(), price))

    @property
    def current_price(self) -> float:
        if not self._price_history:
            return 0.0
        return self._price_history[-1].value

    def nearest_cluster(self, zones: list, min_size_usd: float = 10_000_000) -> Optional[dict]:
        """Find the nearest liquidation cluster to current price.

        Args:
            zones: List of zone dicts from Virtuoso (with 'price' and 'size'/'volume' keys)
            min_size_usd: Minimum cluster size to consider (default $10M)
        """
        if not self._price_history or not zones:
            return None

        current = self.current_price
        nearest = None
        min_dist = float("inf")

        for zone in zones:
            zone_price = zone.get("price", zone.get("level", 0))
            zone_size = zone.get("size", zone.get("volume", zone.get("notional", 0)))
            if zone_price <= 0:
                continue
            if zone_size < min_size_usd:
                continue
            dist = abs(zone_price - current)
            if dist < min_dist:
                min_dist = dist
                nearest = {
                    "price": zone_price,
                    "size_usd": zone_size,
                    "distance": dist,
                    "distance_pct": (dist / current) * 100 if current > 0 else 0,
                    "direction": "UP" if zone_price > current else "DOWN",
                }

        return nearest

    def velocity_toward(self, target_price: float) -> float:
        """Price movement per second toward a target (positive = approaching).

        Uses last 5 readings (~5s window) for responsive velocity.
        """
        if len(self._price_history) < 5:
            return 0.0
        recent = self._price_history[-1]
        older = self._price_history[-5]
        dt = recent.timestamp - older.timestamp
        if dt <= 0:
            return 0.0
        price_delta = recent.value - older.value
        # Positive velocity = moving toward target
        if target_price > recent.value:
            return price_delta / dt     # Price going up = approaching target above
        else:
            return -price_delta / dt    # Price going down = approaching target below

    def eta_seconds(self, target_price: float) -> Optional[float]:
        """Estimated time to reach target at current velocity.

        Returns None if moving away from target.
        """
        vel = self.velocity_toward(target_price)
        if vel <= 0:
            return None  # Moving away or stationary
        distance = abs(target_price - self.current_price)
        return distance / vel

    def cascade_imminent(self, zones: list, max_distance_pct: float = 0.5,
                         min_cluster_usd: float = 20_000_000) -> Optional[dict]:
        """Check if a liquidation cascade is imminent.

        Conditions:
        1. Cluster within max_distance_pct of current price
        2. Price accelerating toward cluster
        3. Cluster size >= min_cluster_usd

        Returns cluster details with ETA, or None.
        """
        cluster = self.nearest_cluster(zones, min_size_usd=min_cluster_usd)
        if cluster is None:
            return None

        if cluster["distance_pct"] > max_distance_pct:
            return None

        vel = self.velocity_toward(cluster["price"])
        if vel <= 0:
            return None  # Not approaching

        eta = self.eta_seconds(cluster["price"])
        cluster["velocity"] = vel
        cluster["eta_seconds"] = eta
        cluster["approaching"] = True
        return cluster

    @property
    def samples(self) -> int:
        return len(self._price_history)


if __name__ == "__main__":
    print("Velocity Tracker Tests")
    print("=" * 40)

    # Test imbalance tracker
    imb = ImbalanceVelocityTracker(window_size=5)
    for i in range(5):
        imb.update(bid_depth=100 + i * 10, ask_depth=80)
        time.sleep(0.01)
    print(f"Imbalance ratio: {imb.current_ratio:.2f}")
    print(f"Imbalance velocity: {imb.velocity:.4f}/s")
    print(f"Is cliff: {imb.is_cliff}")

    # Test CVD tracker
    cvd = CVDAccelerationTracker(window_size=5)
    for i in range(5):
        cvd.update(cvd_level=100 + i * 5 + i * i)  # Accelerating
        time.sleep(0.01)
    print(f"\nCVD level: {cvd.current_level:.1f}")
    print(f"CVD velocity: {cvd.velocity:.2f}/s")
    print(f"CVD acceleration: {cvd.acceleration:.2f}/s^2")

    # Test liq proximity
    liq = LiquidationProximityTracker(price_window=10)
    for i in range(10):
        liq.update_price(65000 + i * 20)  # Price rising toward 65500
        time.sleep(0.01)
    zones = [{"price": 65500, "size": 50_000_000}]
    cluster = liq.cascade_imminent(zones, max_distance_pct=1.0)
    print(f"\nNearest cluster: {cluster}")

"""
Polyclawd Load Testing - Locust Configuration

Load test the Polyclawd API endpoints with realistic traffic patterns.
Focuses on fast, locally-served endpoints that don't depend on external APIs.

Usage:
    locust -f tests/load/locustfile.py --host=http://localhost:8420 --headless -u 50 -r 10 -t 5m

Performance targets:
    - 0% failure rate on local endpoints
    - p95 latency < 500ms for local endpoints
    - Sustained throughput under 50 concurrent users
"""

from locust import HttpUser, between, task


class PolyclawdUser(HttpUser):
    """Simulates a typical Polyclawd API user with weighted endpoint access.

    Weights reflect real-world usage patterns:
    - balance(10): Most checked endpoint - users constantly verify funds
    - positions(5): Second most - check open positions
    - signals(3): Medium - trading signals aggregation
    - edges(2): Lower - edge detection
    - engine(1): Rare - engine status checks

    Note: This test focuses on fast, locally-served endpoints.
    External API-dependent endpoints (vegas/edge, manifold/edge, etc.)
    are tested separately with appropriate timeouts.
    """

    wait_time = between(0.5, 2)

    # Primary endpoints - local data, fast response
    @task(10)
    def balance(self):
        """Most frequent: Check paper trading balance (local JSON)."""
        self.client.get("/api/balance")

    @task(5)
    def positions(self):
        """Frequent: Check paper trading positions (local JSON)."""
        self.client.get("/api/positions")

    @task(3)
    def signals(self):
        """Medium: Get aggregated trading signals."""
        with self.client.get("/api/signals", catch_response=True) as response:
            # Allow timeouts for external API calls
            if response.status_code in [200, 503, 504]:
                response.success()

    @task(2)
    def edges(self):
        """Lower: Get edge detection from polyrouter."""
        with self.client.get("/api/polyrouter/edge", catch_response=True) as response:
            if response.status_code in [200, 503, 504]:
                response.success()

    @task(1)
    def engine(self):
        """Rare: Check trading engine status (local state)."""
        self.client.get("/api/engine/status")

    # System endpoints - always fast
    @task(3)
    def health(self):
        """System health check - always fast."""
        self.client.get("/health")

    @task(2)
    def trades(self):
        """Trade history - local JSON data."""
        self.client.get("/api/trades")

    @task(1)
    def paper_status(self):
        """Paper trading status - local state."""
        self.client.get("/api/paper/status")

    @task(1)
    def paper_positions(self):
        """Paper trading positions - local JSON."""
        self.client.get("/api/paper/positions")

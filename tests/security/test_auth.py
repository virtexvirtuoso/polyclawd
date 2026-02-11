"""Security tests for authentication, rate limiting, and security headers."""
import os
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch


# Set up test API key before importing app
TEST_API_KEY = "test-api-key-12345"
os.environ["POLYCLAWD_API_KEYS"] = TEST_API_KEY


class TestAuthentication:
    """Tests for API key authentication."""

    @pytest.fixture
    def client(self):
        """Create test client with fresh app import."""
        # Clear cached settings so environment changes take effect
        from api.deps import get_settings
        get_settings.cache_clear()
        from api.main import app
        return TestClient(app)

    def test_trade_without_api_key_returns_401(self, client):
        """POST /api/trade without X-API-Key should return 401."""
        response = client.post(
            "/api/trade",
            json={
                "market_id": "test-market",
                "side": "YES",
                "amount": 10,
                "reasoning": "test"
            }
        )
        assert response.status_code == 401 or response.status_code == 422  # 422 if missing header validation

    def test_trade_with_invalid_api_key_returns_401(self, client):
        """POST /api/trade with invalid X-API-Key should return 401."""
        response = client.post(
            "/api/trade",
            json={
                "market_id": "test-market",
                "side": "YES",
                "amount": 10,
                "reasoning": "test"
            },
            headers={"X-API-Key": "wrong-key"}
        )
        assert response.status_code == 401

    def test_trade_with_valid_api_key_passes_auth(self, client):
        """POST /api/trade with valid X-API-Key should pass auth (may fail on market lookup)."""
        response = client.post(
            "/api/trade",
            json={
                "market_id": "test-market",
                "side": "YES",
                "amount": 10,
                "reasoning": "test"
            },
            headers={"X-API-Key": TEST_API_KEY}
        )
        # Should not be 401 (passed auth) - may be 404 (market not found) or 502 (API error)
        assert response.status_code != 401

    def test_reset_without_api_key_returns_401(self, client):
        """POST /api/reset without X-API-Key should return 401."""
        response = client.post("/api/reset")
        assert response.status_code == 401 or response.status_code == 422

    def test_reset_with_valid_api_key_works(self, client):
        """POST /api/reset with valid X-API-Key should work."""
        response = client.post(
            "/api/reset",
            headers={"X-API-Key": TEST_API_KEY}
        )
        assert response.status_code == 200

    def test_simmer_trade_without_api_key_returns_401(self, client):
        """POST /api/simmer/trade without X-API-Key should return 401."""
        response = client.post("/api/simmer/trade?market_id=test&side=YES&amount=10")
        assert response.status_code == 401 or response.status_code == 422

    def test_paper_trade_without_api_key_returns_401(self, client):
        """POST /api/paper/trade without X-API-Key should return 401."""
        response = client.post(
            "/api/paper/trade",
            params={
                "market_id": "test",
                "market_title": "Test Market",
                "side": "YES",
                "amount": 10,
                "price": 0.5
            }
        )
        assert response.status_code == 401 or response.status_code == 422

    def test_resolve_position_without_api_key_returns_401(self, client):
        """POST /api/positions/xxx/resolve without X-API-Key should return 401."""
        response = client.post("/api/positions/test-id/resolve?won=true")
        assert response.status_code == 401 or response.status_code == 422


class TestSecurityHeaders:
    """Tests for security headers."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        from api.deps import get_settings
        get_settings.cache_clear()
        from api.main import app
        return TestClient(app)

    def test_health_has_security_headers(self, client):
        """GET /health should include security headers."""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"
        assert response.headers.get("X-XSS-Protection") == "1; mode=block"

    def test_ready_has_security_headers(self, client):
        """GET /ready should include security headers."""
        response = client.get("/ready")
        assert response.status_code == 200
        assert "X-Content-Type-Options" in response.headers
        assert "X-Frame-Options" in response.headers

    def test_api_endpoint_has_security_headers(self, client):
        """API endpoints should include security headers."""
        response = client.get("/api/balance")
        assert "X-Content-Type-Options" in response.headers
        assert "X-Frame-Options" in response.headers


class TestRateLimiting:
    """Tests for rate limiting."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        from api.deps import get_settings
        get_settings.cache_clear()
        from api.main import app
        return TestClient(app)

    def test_rate_limit_trade_endpoint(self, client):
        """POST /api/trade should have rate limiting (5/minute)."""
        # Make multiple rapid requests - at some point should get 429
        responses = []
        for i in range(10):
            response = client.post(
                "/api/trade",
                json={
                    "market_id": f"test-market-{i}",
                    "side": "YES",
                    "amount": 10,
                    "reasoning": "test"
                },
                headers={"X-API-Key": TEST_API_KEY}
            )
            responses.append(response.status_code)

        # After 5 requests, should start getting 429
        assert 429 in responses, f"Expected 429 in responses but got: {responses}"

    def test_rate_limit_reset_endpoint(self, client):
        """POST /api/reset should have rate limiting (2/minute)."""
        responses = []
        for i in range(5):
            response = client.post(
                "/api/reset",
                headers={"X-API-Key": TEST_API_KEY}
            )
            responses.append(response.status_code)

        # After 2 requests, should start getting 429
        assert 429 in responses, f"Expected 429 in responses but got: {responses}"

    def test_rate_limit_engine_trigger(self, client):
        """POST /api/engine/trigger should have rate limiting (5/minute)."""
        responses = []
        for i in range(10):
            response = client.post("/api/engine/trigger")
            responses.append(response.status_code)

        # After 5 requests, should start getting 429
        assert 429 in responses, f"Expected 429 in responses but got: {responses}"


class TestAuthBypass:
    """Tests for auth bypass vulnerabilities."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        from api.deps import get_settings
        get_settings.cache_clear()
        from api.main import app
        return TestClient(app)

    def test_case_insensitive_header_bypass(self, client):
        """Test that auth can't be bypassed with case variations."""
        # Try various case combinations
        for header in ["x-api-key", "x-Api-Key", "X-api-key", "X-API-KEY"]:
            response = client.post(
                "/api/reset",
                headers={header: "wrong-key"}
            )
            assert response.status_code == 401, f"Auth bypassed with header: {header}"

    def test_empty_api_key_rejected(self, client):
        """Empty API key should be rejected."""
        response = client.post(
            "/api/reset",
            headers={"X-API-Key": ""}
        )
        # Empty key should fail (either 401 or 422)
        assert response.status_code in [401, 422]

    def test_null_api_key_rejected(self, client):
        """Null-like API keys should be rejected."""
        for key in ["null", "undefined", "None"]:
            response = client.post(
                "/api/reset",
                headers={"X-API-Key": key}
            )
            assert response.status_code == 401, f"Auth passed with key: {key}"

    def test_read_endpoints_dont_require_auth(self, client):
        """Read endpoints should work without auth."""
        read_endpoints = [
            "/api/balance",
            "/api/positions",
            "/api/trades",
            "/api/engine/status",
            "/api/engine/config",
            "/api/kelly/current",
            "/health",
            "/ready"
        ]

        for endpoint in read_endpoints:
            response = client.get(endpoint)
            # Should not be 401 (auth not required for reads)
            assert response.status_code != 401, f"Auth required for read endpoint: {endpoint}"


class TestNoDevModeBypass:
    """Test that dev mode doesn't accidentally bypass auth in production."""

    def test_auth_required_when_api_keys_configured(self):
        """When API keys are configured, auth should be required."""
        # Reset settings cache
        from api.deps import get_settings
        get_settings.cache_clear()

        # Set API key
        os.environ["POLYCLAWD_API_KEYS"] = "production-key"

        from api.main import app
        client = TestClient(app)

        response = client.post("/api/reset")
        assert response.status_code == 401 or response.status_code == 422

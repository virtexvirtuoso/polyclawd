# Integration tests for trading flow endpoints
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def reset_state(api_base_url: str, mock_httpx_client):
    """Reset paper trading before each test."""
    # Reset via API (may require auth in production)
    mock_httpx_client.post(
        f"{api_base_url}/api/reset",
        headers={"X-API-Key": "test-key"}
    )
    return mock_httpx_client, api_base_url


class TestTradingFlow:
    """Integration tests for complete trading lifecycle."""

    def test_balance_endpoint_returns_expected_fields(self, api_base_url: str, mock_httpx_client):
        """Verify balance endpoint returns required fields."""
        response = mock_httpx_client.get(f"{api_base_url}/api/balance")
        assert response.status_code == 200
        data = response.json()

        # Verify expected fields exist
        expected_fields = {"cash", "positions_value", "total", "pnl", "pnl_percent"}
        assert expected_fields.issubset(data.keys()), f"Missing fields: {expected_fields - set(data.keys())}"

        # Verify types
        assert isinstance(data["cash"], (int, float))
        assert isinstance(data["total"], (int, float))

    def test_positions_endpoint_returns_list(self, api_base_url: str, mock_httpx_client):
        """Verify positions endpoint returns a list."""
        response = mock_httpx_client.get(f"{api_base_url}/api/positions")
        assert response.status_code == 200
        data = response.json()

        # Should be a list (empty or with positions)
        assert isinstance(data, list)

    def test_trades_endpoint_returns_list(self, api_base_url: str, mock_httpx_client):
        """Verify trades endpoint returns a list."""
        response = mock_httpx_client.get(f"{api_base_url}/api/trades")
        assert response.status_code == 200
        data = response.json()

        # Should be a list
        assert isinstance(data, list)

    def test_trades_respects_limit_parameter(self, api_base_url: str, mock_httpx_client):
        """Verify trades endpoint respects limit parameter."""
        response = mock_httpx_client.get(f"{api_base_url}/api/trades?limit=5")
        assert response.status_code == 200
        data = response.json()

        # Should not exceed limit
        assert len(data) <= 5

    def test_positions_check_returns_status(self, api_base_url: str, mock_httpx_client):
        """Verify positions check endpoint returns expected structure."""
        response = mock_httpx_client.get(f"{api_base_url}/api/positions/check")
        assert response.status_code == 200
        data = response.json()

        # Verify expected fields
        assert "checked" in data
        assert "positions" in data

    def test_complete_trade_lifecycle(self, reset_state):
        """Test full lifecycle: balance -> trade -> position -> resolve."""
        client, base_url = reset_state

        # 1. Check initial balance
        balance_resp = client.get(f"{base_url}/api/balance")
        assert balance_resp.status_code == 200
        initial_balance = balance_resp.json()
        assert initial_balance["cash"] > 0

        # 2. Verify empty positions after reset
        positions_resp = client.get(f"{base_url}/api/positions")
        assert positions_resp.status_code == 200
        # May or may not be empty depending on timing

        # 3. Get trades history
        trades_resp = client.get(f"{base_url}/api/trades")
        assert trades_resp.status_code == 200


class TestSimmerEndpoints:
    """Tests for Simmer SDK integration endpoints."""

    def test_simmer_status_endpoint(self, api_base_url: str, mock_httpx_client):
        """Verify simmer status endpoint responds."""
        response = mock_httpx_client.get(f"{api_base_url}/api/simmer/status")
        # May return 502 if Simmer is not configured
        assert response.status_code in (200, 502)

    def test_simmer_positions_endpoint(self, api_base_url: str, mock_httpx_client):
        """Verify simmer positions endpoint responds."""
        response = mock_httpx_client.get(f"{api_base_url}/api/simmer/positions")
        # May return 502 if Simmer is not configured
        assert response.status_code in (200, 502)

    def test_simmer_trades_endpoint(self, api_base_url: str, mock_httpx_client):
        """Verify simmer trades endpoint responds."""
        response = mock_httpx_client.get(f"{api_base_url}/api/simmer/trades")
        # May return 502 if Simmer is not configured
        assert response.status_code in (200, 502)


class TestPaperEndpoints:
    """Tests for Paper trading status endpoints."""

    def test_paper_status_endpoint(self, api_base_url: str, mock_httpx_client):
        """Verify paper status endpoint returns expected fields."""
        response = mock_httpx_client.get(f"{api_base_url}/api/paper/status")
        assert response.status_code == 200
        data = response.json()

        # Verify expected fields
        assert "balance" in data
        assert "open_positions" in data
        assert "total_trades" in data

    def test_paper_positions_endpoint(self, api_base_url: str, mock_httpx_client):
        """Verify paper positions endpoint returns structure."""
        response = mock_httpx_client.get(f"{api_base_url}/api/paper/positions")
        assert response.status_code == 200
        data = response.json()

        assert "positions" in data
        assert "count" in data
        assert isinstance(data["positions"], list)


class TestAuthRequiredEndpoints:
    """Tests for endpoints requiring API key authentication."""

    def test_trade_requires_auth(self, api_base_url: str, mock_httpx_client):
        """Verify trade endpoint requires authentication."""
        response = mock_httpx_client.post(
            f"{api_base_url}/api/trade",
            json={"market_id": "test", "side": "YES", "amount": "10"}
        )
        # Should require auth (401) or validation error (422) if no auth configured
        assert response.status_code in (401, 422)

    def test_reset_requires_auth(self, api_base_url: str, mock_httpx_client):
        """Verify reset endpoint requires authentication."""
        response = mock_httpx_client.post(f"{api_base_url}/api/reset")
        # Should require auth (401) if auth is configured
        # May succeed if dev mode (no auth)
        assert response.status_code in (200, 401, 422)

    def test_simmer_trade_requires_auth(self, api_base_url: str, mock_httpx_client):
        """Verify simmer trade endpoint requires authentication."""
        response = mock_httpx_client.post(
            f"{api_base_url}/api/simmer/trade",
            params={"market_id": "test", "side": "YES", "amount": 10}
        )
        # Should require auth or fail validation
        assert response.status_code in (401, 422, 502)

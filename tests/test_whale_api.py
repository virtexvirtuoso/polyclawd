# Whale dashboard API routes — shape and filter regression tests.
import pytest


@pytest.mark.usefixtures("test_client")
class TestWhaleRoutes:
    def test_stats_shape(self, test_client):
        r = test_client.get("/api/whale/stats")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"health", "precision", "counts_24h"}

    def test_alerts_filters(self, test_client):
        r = test_client.get("/api/whale/alerts?severity=CRITICAL&platform=kalshi&limit=5")
        assert r.status_code == 200
        body = r.json()
        assert "alerts" in body and "counts" in body
        assert len(body["alerts"]) <= 5
        for a in body["alerts"]:
            assert a["severity"] == "CRITICAL"
            assert a["platform"] == "kalshi"

    def test_alerts_limit_cap(self, test_client):
        assert test_client.get("/api/whale/alerts?limit=9999").status_code == 422

    def test_wallets_shape(self, test_client):
        r = test_client.get("/api/whale/wallets?limit=3")
        assert r.status_code == 200
        body = r.json()
        assert {"wallets", "smart_count", "tracked", "queued"} <= set(body)
        assert len(body["wallets"]) <= 3

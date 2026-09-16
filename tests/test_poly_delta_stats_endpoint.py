"""/api/signals/poly-delta-stats contract — backs the poly_delta verdict-gate cron.

gate_open flips true once >= threshold PM fills have a poly_delta_60 reading, i.e.
when the speed-edge (S2/S5) build/kill call becomes statistically meaningful.
"""


def test_poly_delta_stats_contract():
    from fastapi.testclient import TestClient
    from api.main import app

    r = TestClient(app).get("/api/signals/poly-delta-stats")
    assert r.status_code == 200
    j = r.json()
    assert {"populated_count", "threshold", "gate_open", "by_strategy"} <= set(j)
    assert j["threshold"] == 50
    # gate is a pure function of count vs threshold
    assert j["gate_open"] == (j["populated_count"] >= j["threshold"])
    assert isinstance(j["by_strategy"], dict)

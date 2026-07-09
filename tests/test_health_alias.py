"""Health endpoint path robustness.

The root cause of the 2026-06-20 'Polyclawd unreachable' false alarm: a monitor
hit /api/health (404) while the real route was /health. Both paths must return
200 so no caller can false-alarm on a path mismatch.
"""


def test_api_health_alias_returns_200():
    from fastapi.testclient import TestClient
    from api.main import app

    r = TestClient(app).get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"


def test_root_health_still_works():
    from fastapi.testclient import TestClient
    from api.main import app

    assert TestClient(app).get("/health").status_code == 200

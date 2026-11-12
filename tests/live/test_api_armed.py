"""Task 1.10, reader 3: /api/live/portfolio and /api/live/governor report `armed`
next to `mode`.

The route handlers are plain functions: they are called directly (the TestClient
fixture is broken in this environment by an installed-library incompatibility).
They run against the temporary live DB; the balance read goes to fake_clob.
"""

import pytest

from api.routes import live as live_api


@pytest.fixture
def routes(live_db, fake_clob, shapes, monkeypatch):
    monkeypatch.setattr(live_api, "_safe_recompute", lambda conn, onchain_balance: None)

    def call(name):
        if name == "portfolio":
            fake_clob.script("get_balance_allowance", shapes.balance("25"))
            return live_api.get_live_portfolio_endpoint()
        return live_api.get_live_governor()

    return call


@pytest.mark.parametrize("route", ["portfolio", "governor"])
def test_route_reports_armed_false_under_paper(routes, mode_env, route):
    mode_env("PAPER", None, False)
    out = routes(route)
    assert out["mode"] == "PAPER"
    assert out["armed"] is False


@pytest.mark.parametrize("route", ["portfolio", "governor"])
def test_route_reports_armed_false_for_live_but_unarmed(routes, mode_env, route):
    mode_env("LIVE", "1", False)
    out = routes(route)
    assert out["mode"] == "LIVE"
    assert out["armed"] is False


@pytest.mark.parametrize("route", ["portfolio", "governor"])
def test_route_reports_armed_true_when_armed(routes, armed, route):
    out = routes(route)
    assert out["mode"] == "LIVE"
    assert out["armed"] is True

"""Simmer /api/simmer/trade must be OFF unless explicitly enabled.

/qa 2026-08-19: this route places real-money trades via Simmer managed
custody with zero RiskGovernor coupling (different custody pot than the
canary wallet) — no strategy allowlist, KILL state, daily-loss halt, or
deployed cap. It must 403 unless POLYCLAWD_SIMMER_TRADE_ENABLED is set.
"""
import api.routes.trading as trading_module

TRADE_PARAMS = {"market_id": "0xdeadbeef", "side": "yes", "amount": 10}
HEADERS = {"X-API-Key": "test-key"}


def test_simmer_trade_disabled_by_default(test_client, monkeypatch):
    monkeypatch.delenv("POLYCLAWD_SIMMER_TRADE_ENABLED", raising=False)

    resp = test_client.post("/api/simmer/trade", params=TRADE_PARAMS, headers=HEADERS)

    assert resp.status_code == 403
    assert "POLYCLAWD_SIMMER_TRADE_ENABLED" in resp.text


def test_simmer_trade_explicitly_disabled_via_zero(test_client, monkeypatch):
    monkeypatch.setenv("POLYCLAWD_SIMMER_TRADE_ENABLED", "0")

    resp = test_client.post("/api/simmer/trade", params=TRADE_PARAMS, headers=HEADERS)

    assert resp.status_code == 403
    assert "POLYCLAWD_SIMMER_TRADE_ENABLED" in resp.text


def test_simmer_trade_enabled_skips_the_gate(test_client, monkeypatch):
    """Enabled -> gate no longer fires. Route may still fail downstream
    (bogus market / no real Simmer creds) but must NOT be a 403 from our
    gate, and must not hit the real network.
    """
    monkeypatch.setenv("POLYCLAWD_SIMMER_TRADE_ENABLED", "1")
    monkeypatch.setattr(
        trading_module,
        "_simmer_request",
        lambda endpoint, method="GET", data=None: {"error": "bogus market for test"},
    )

    resp = test_client.post("/api/simmer/trade", params=TRADE_PARAMS, headers=HEADERS)

    assert resp.status_code != 403

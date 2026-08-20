"""Fail-closed egress-route guard (execution/clob_client.assert_egress_tunneled).

See the "Egress guard" section in execution/clob_client.py for the full
incident writeup. Short version: the VPS's default route egresses in
Singapore (Polymarket-blocked); only the proton-ie WireGuard tunnel reaches
Polymarket from an allowed jurisdiction. This guard refuses to submit an
order unless the kernel would actually route it through the tunnel right
now, closing the window between a tunnel IP rotation and the 5-minute
re-sync timer catching up.
"""
import pytest

import execution.clob_client as clob_client
from execution.clob_client import ClobError, assert_egress_tunneled


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Guard is env-gated; make sure no ambient env leaks between tests."""
    monkeypatch.delenv("POLYCLAWD_EGRESS_REQUIRE_SRC", raising=False)
    monkeypatch.delenv("POLYCLAWD_EGRESS_HOSTS", raising=False)


def test_guard_inactive_when_env_unset(monkeypatch):
    """No POLYCLAWD_EGRESS_REQUIRE_SRC => guard is a no-op, even if the probe
    would report a 'wrong' source IP."""
    monkeypatch.setattr(clob_client, "_egress_src_ip", lambda host, port=443: "5.223.63.4")
    assert assert_egress_tunneled() is None


def test_guard_passes_when_src_matches(monkeypatch):
    monkeypatch.setenv("POLYCLAWD_EGRESS_REQUIRE_SRC", "10.2.0.2")
    monkeypatch.setattr(clob_client, "_egress_src_ip", lambda host, port=443: "10.2.0.2")
    assert assert_egress_tunneled() is None


def test_guard_raises_when_src_differs(monkeypatch):
    monkeypatch.setenv("POLYCLAWD_EGRESS_REQUIRE_SRC", "10.2.0.2")
    monkeypatch.setattr(clob_client, "_egress_src_ip", lambda host, port=443: "5.223.63.4")
    with pytest.raises(ClobError) as exc_info:
        assert_egress_tunneled()
    msg = str(exc_info.value)
    assert "10.2.0.2" in msg
    assert "5.223.63.4" in msg


def test_guard_raises_when_route_undeterminable(monkeypatch):
    monkeypatch.setenv("POLYCLAWD_EGRESS_REQUIRE_SRC", "10.2.0.2")
    monkeypatch.setattr(clob_client, "_egress_src_ip", lambda host, port=443: None)
    with pytest.raises(ClobError):
        assert_egress_tunneled()


def test_egress_src_ip_real_behaviour_loopback():
    """No env/monkeypatching — proves the UDP-connect probe works without
    sending any packets: routing 127.0.0.1 must resolve to source 127.0.0.1."""
    assert clob_client._egress_src_ip("127.0.0.1") == "127.0.0.1"


class _StubClientOrderNotAllowed:
    """Vendor-client stand-in whose order methods must never be reached if
    the guard is doing its job — calling either is a test failure."""

    def place_limit_order(self, *args, **kwargs):
        raise AssertionError("must not be called")

    def place_market_order(self, *args, **kwargs):
        raise AssertionError("must not be called")


def test_post_maker_blocked_before_vendor_call(monkeypatch):
    """Load-bearing: fails if the assert_egress_tunneled() call is ever
    removed from post_maker()."""
    monkeypatch.setenv("POLYCLAWD_EGRESS_REQUIRE_SRC", "10.2.0.2")
    monkeypatch.setattr(clob_client, "_egress_src_ip", lambda host, port=443: "5.223.63.4")
    monkeypatch.setattr(clob_client, "_get_client", lambda: _StubClientOrderNotAllowed())

    with pytest.raises(ClobError) as exc_info:
        clob_client.post_maker(
            token_id="tok123",
            side="BUY",
            price=0.5,
            size=10.0,
            tick_size=0.01,
        )
    assert "egress guard" in str(exc_info.value)


def test_cross_taker_blocked_before_vendor_call(monkeypatch):
    """Load-bearing: fails if the assert_egress_tunneled() call is ever
    removed from cross_taker()."""
    monkeypatch.setenv("POLYCLAWD_EGRESS_REQUIRE_SRC", "10.2.0.2")
    monkeypatch.setattr(clob_client, "_egress_src_ip", lambda host, port=443: "5.223.63.4")
    monkeypatch.setattr(clob_client, "_get_client", lambda: _StubClientOrderNotAllowed())

    with pytest.raises(ClobError) as exc_info:
        clob_client.cross_taker(
            token_id="tok123",
            side="BUY",
            amount=10.0,
            tick_size=0.01,
        )
    assert "egress guard" in str(exc_info.value)

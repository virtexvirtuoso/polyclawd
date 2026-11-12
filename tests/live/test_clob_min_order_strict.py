"""Task 1.12: get_min_order_size(token, strict=True) raises instead of returning the fallback.

The pre-flight must not read the 5-share fallback as a fact. strict=False is what it
always was: it logs a warning and returns DEFAULT_MIN_ORDER_SHARES.
"""

import pytest

from execution import clob_client

TOKEN = "0000AAAA1111BBBB2222"


class _Resp:
    def __init__(self, payload=None, exc=None):
        self._payload, self._exc = payload, exc

    def raise_for_status(self):
        if self._exc:
            raise self._exc

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("POLYCLAWD_MIN_ORDER_SHARES", raising=False)
    clob_client._min_order_size_cache.clear()
    yield
    clob_client._min_order_size_cache.clear()


def _serve(monkeypatch, resp):
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params))
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(clob_client._requests, "get", fake_get)
    return calls


def test_strict_failed_read_raises_clob_error_naming_token_and_cause(monkeypatch):
    _serve(monkeypatch, ConnectionError("vendor down"))
    with pytest.raises(clob_client.ClobError) as ei:
        clob_client.get_min_order_size(TOKEN, strict=True)
    assert TOKEN in str(ei.value)
    assert "vendor down" in str(ei.value)


@pytest.mark.parametrize("payload", [{}, {"min_order_size": "0"}, {"min_order_size": "abc"}])
def test_strict_unusable_field_raises(monkeypatch, payload):
    _serve(monkeypatch, _Resp(payload))
    with pytest.raises(clob_client.ClobError):
        clob_client.get_min_order_size(TOKEN, strict=True)


def test_strict_reads_the_exchange_value_even_under_pytest(monkeypatch):
    """The non-strict path answers the default under pytest; strict must not."""
    calls = _serve(monkeypatch, _Resp({"min_order_size": "15"}))
    assert clob_client.get_min_order_size(TOKEN, strict=True) == 15.0
    assert len(calls) == 1
    # and caches: a second read does not call again
    assert clob_client.get_min_order_size(TOKEN, strict=True) == 15.0
    assert len(calls) == 1


def test_strict_honours_the_environment_override_without_a_request(monkeypatch):
    calls = _serve(monkeypatch, ConnectionError("must not be called"))
    monkeypatch.setenv("POLYCLAWD_MIN_ORDER_SHARES", "7")
    assert clob_client.get_min_order_size(TOKEN, strict=True) == 7.0
    assert calls == []


def test_non_strict_failed_read_still_returns_the_default(monkeypatch):
    """Unchanged behaviour: outside pytest's short-circuit the failure falls back."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    _serve(monkeypatch, ConnectionError("vendor down"))
    assert clob_client.get_min_order_size(TOKEN) == clob_client.DEFAULT_MIN_ORDER_SHARES


def test_non_strict_under_pytest_returns_default_without_a_request(monkeypatch):
    calls = _serve(monkeypatch, _Resp({"min_order_size": "15"}))
    assert clob_client.get_min_order_size(TOKEN) == clob_client.DEFAULT_MIN_ORDER_SHARES
    assert calls == []

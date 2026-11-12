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


def test_strict_ignores_the_environment_override_and_reads_the_exchange(monkeypatch):
    """Rewritten in fix-6 (A1): the override used to win over the exchange in strict mode."""
    calls = _serve(monkeypatch, _Resp({"min_order_size": "15"}))
    for override in ("1", "0", "-1", "nan", "7"):
        clob_client._min_order_size_cache.clear()
        monkeypatch.setenv("POLYCLAWD_MIN_ORDER_SHARES", override)
        assert clob_client.get_min_order_size(TOKEN, strict=True) == 15.0, override
    assert len(calls) == 5


def test_strict_with_override_and_failed_read_raises_never_returns_the_override(monkeypatch):
    _serve(monkeypatch, ConnectionError("vendor down"))
    monkeypatch.setenv("POLYCLAWD_MIN_ORDER_SHARES", "0")
    with pytest.raises(clob_client.ClobError):
        clob_client.get_min_order_size(TOKEN, strict=True)


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "-3"])
def test_strict_non_finite_or_non_positive_exchange_value_raises(monkeypatch, raw):
    _serve(monkeypatch, _Resp({"min_order_size": raw}))
    with pytest.raises(clob_client.ClobError):
        clob_client.get_min_order_size(TOKEN, strict=True)


def test_non_strict_override_is_unchanged(monkeypatch):
    calls = _serve(monkeypatch, ConnectionError("must not be called"))
    monkeypatch.setenv("POLYCLAWD_MIN_ORDER_SHARES", "7")
    assert clob_client.get_min_order_size(TOKEN) == 7.0
    assert calls == []


def test_non_strict_fallback_is_never_cached_and_never_served_to_a_strict_call(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    _serve(monkeypatch, ConnectionError("vendor down"))
    assert clob_client.get_min_order_size(TOKEN) == clob_client.DEFAULT_MIN_ORDER_SHARES
    assert clob_client._min_order_size_cache == {}
    with pytest.raises(clob_client.ClobError):
        clob_client.get_min_order_size(TOKEN, strict=True)
    # the pytest short-circuit and the override path do not cache either
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    assert clob_client.get_min_order_size(TOKEN) == clob_client.DEFAULT_MIN_ORDER_SHARES
    monkeypatch.setenv("POLYCLAWD_MIN_ORDER_SHARES", "9")
    assert clob_client.get_min_order_size(TOKEN) == 9.0
    assert clob_client._min_order_size_cache == {}


def test_a_non_finite_value_a_non_strict_call_cached_is_not_served_to_a_strict_call(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    calls = _serve(monkeypatch, _Resp({"min_order_size": "nan"}))
    clob_client.get_min_order_size(TOKEN)  # non-strict keeps its old behaviour
    with pytest.raises(clob_client.ClobError):
        clob_client.get_min_order_size(TOKEN, strict=True)
    assert len(calls) == 2  # the strict call did not take the cached nan


def test_non_strict_failed_read_still_returns_the_default(monkeypatch):
    """Unchanged behaviour: outside pytest's short-circuit the failure falls back."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    _serve(monkeypatch, ConnectionError("vendor down"))
    assert clob_client.get_min_order_size(TOKEN) == clob_client.DEFAULT_MIN_ORDER_SHARES


def test_non_strict_under_pytest_returns_default_without_a_request(monkeypatch):
    calls = _serve(monkeypatch, _Resp({"min_order_size": "15"}))
    assert clob_client.get_min_order_size(TOKEN) == clob_client.DEFAULT_MIN_ORDER_SHARES
    assert calls == []

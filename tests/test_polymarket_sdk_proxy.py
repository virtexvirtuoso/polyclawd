"""polymarket-client host patch follows POLYPROXY_BASE.

No orders are placed. PublicClient() only builds transports from the
patched environment.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass

import pytest

import config.polymarket_urls as urls

pytest.importorskip("polymarket")

from polymarket._internal.environment import get_environment_config  # noqa: E402
from polymarket.clients.public import PublicClient  # noqa: E402
from polymarket.environments import PRODUCTION  # noqa: E402


@dataclass(frozen=True)
class _OldEnvironment:
    """polymarket-client before hosts moved under _config."""

    clob_url: str
    gamma_url: str
    data_url: str


@dataclass(frozen=True)
class _HostConfig:
    clob_url: str
    gamma_url: str
    data_url: str


@dataclass(frozen=True)
class _NestedEnvironment:
    """0.14 shape: Environment.name + _config, no clob_url on the wrapper."""

    name: str
    _config: _HostConfig


def _saved_hosts():
    config = get_environment_config(PRODUCTION)
    return {name: getattr(config, name) for name in urls._SDK_HOST_FIELDS}


def _restore_hosts(saved: dict) -> None:
    target = urls._sdk_host_target(PRODUCTION)
    for name, value in saved.items():
        object.__setattr__(target, name, value)


def test_nested_0_14_config_is_what_gets_patched():
    env = _NestedEnvironment("production", _HostConfig("c", "g", "d"))
    urls._apply_sdk_host_patch(
        env,
        {"clob_url": "https://clob.example", "gamma_url": "https://proxy/gamma", "data_url": "https://proxy/data"},
    )
    assert env._config.gamma_url == "https://proxy/gamma"
    assert env._config.data_url == "https://proxy/data"
    assert env._config.clob_url == "https://clob.example"
    assert not hasattr(env, "clob_url")


def test_older_environment_fields_still_patch():
    env = _OldEnvironment("c", "g", "d")
    urls._apply_sdk_host_patch(
        env,
        {"clob_url": "https://clob.example", "gamma_url": "https://proxy/gamma", "data_url": "https://proxy/data"},
    )
    assert env.gamma_url == "https://proxy/gamma"
    assert env.data_url == "https://proxy/data"
    assert env.clob_url == "https://clob.example"


def test_unpatchable_environment_raises():
    with pytest.raises(AttributeError):
        urls._apply_sdk_host_patch(object(), {"clob_url": "", "gamma_url": "", "data_url": ""})


def test_failed_patch_logs_error_and_does_not_raise(monkeypatch, caplog):
    monkeypatch.setattr(urls, "PROXY_ENABLED", True)
    import polymarket.environments as environments

    monkeypatch.setattr(environments, "PRODUCTION", object(), raising=False)
    caplog.set_level(logging.ERROR, logger="polyproxy")
    urls._patch_polymarket_sdk()
    assert any(
        r.levelno >= logging.ERROR and "Failed to monkey-patch polymarket-client SDK" in r.message
        for r in caplog.records
    )


def test_patched_client_resolves_gamma_and_data_to_the_proxy(monkeypatch):
    """PublicClient transports follow the patched hosts.

    Gamma and Data use POLYPROXY_BASE. CLOB stays on https://clob.polymarket.com
    because Worker /clob/* is rejected from this colo (CF 1010); order
    placement must not follow the proxy.
    """
    proxy = "https://polyproxy.test"
    saved = _saved_hosts()
    monkeypatch.setenv("POLYPROXY_BASE", proxy)
    reloaded = importlib.reload(urls)
    client = None
    try:
        client = PublicClient()
        assert client._ctx.gamma._base_url == f"{proxy}/gamma"
        assert client._ctx.data._base_url == f"{proxy}/data"
        assert client._ctx.clob._base_url == "https://clob.polymarket.com"
        assert get_environment_config(client.environment).data_url == f"{proxy}/data"
        assert get_environment_config(client.environment).gamma_url == f"{proxy}/gamma"
        # list_positions is a Data API read; its transport base is the proxy.
        assert client._ctx.data._base_url.startswith(proxy)
    finally:
        if client is not None:
            client.close()
        _restore_hosts(saved)
        monkeypatch.delenv("POLYPROXY_BASE", raising=False)
        importlib.reload(reloaded)

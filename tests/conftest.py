# Pytest configuration and fixtures for polyclawd tests
import asyncio
import functools
import os
import sys
from pathlib import Path
from typing import AsyncGenerator, Generator

import pytest
import httpx
from fastapi.testclient import TestClient

# Kill all Telegram sends in test runs — prevents test fixtures from leaking
# "Test Market" / "RN1" alerts into the real notification pipeline.
os.environ["SMART_WALLET_ALERT_SEND"] = "0"
os.environ.pop("TELEGRAM_BOT_TOKEN", None)

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from api.main import app


_NOTIFIER_ENV_VARS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL")
# module -> constant that holds its token/webhook, read once at import
_IMPORT_TIME_CREDENTIAL_CONSTANTS = {
    "signals.tier1_whale_alert": "TELEGRAM_BOT_TOKEN",
    "signals.ingame_monitor": "DISCORD_WEBHOOK_URL",
}


_NOTIFIER_HOSTS = ("api.telegram.org", "discord.com", "discordapp.com")


class NotifierHostBlocked(AssertionError):
    """A test tried to open an HTTP connection to a Telegram or Discord host."""


def _notifier_host(target):
    """Hostname of `target` (str, bytes, urllib Request or httpx Request) if it
    is a notifier host or a subdomain of one, else None. Parsed hostname only,
    never a substring match: `notdiscord.com` and `discord.com.evil.example`
    are not notifier hosts."""
    from urllib.parse import urlsplit

    url = getattr(target, "full_url", None) or getattr(target, "url", None) or target
    if isinstance(url, bytes):
        url = url.decode("utf-8", "replace")
    try:
        host = (urlsplit(str(url)).hostname or "").rstrip(".").lower()
    except ValueError:
        return None
    return host if any(host == h or host.endswith("." + h) for h in _NOTIFIER_HOSTS) else None


def _block_notifier_hosts(monkeypatch, calls, owner, name, url_of, is_async=False):
    """Replace owner.name with a wrapper that raises NotifierHostBlocked (and
    records a BLOCKED_HTTP entry) for a notifier host, else calls the original."""
    original = getattr(owner, name)

    def _check(args, kwargs):
        host = _notifier_host(url_of(*args, **kwargs))
        if host:
            calls.append({"fn": "BLOCKED_HTTP", "args": (), "kwargs": {"host": host, "via": name}})
            raise NotifierHostBlocked(f"test tried to reach notifier host {host} via {name}")

    if is_async:
        @functools.wraps(original)
        async def wrapper(*args, **kwargs):
            _check(args, kwargs)
            return await original(*args, **kwargs)
    else:
        @functools.wraps(original)
        def wrapper(*args, **kwargs):
            _check(args, kwargs)
            return original(*args, **kwargs)
    monkeypatch.setattr(owner, name, wrapper)


def _fail_on_swallowed_blocks(calls, opted_in):
    """Fail if the host block fired during a test that did not ask for it.

    NotifierHostBlocked is an AssertionError, but production senders wrap their
    HTTP call in `except Exception` and swallow it, so a test that triggers a
    block inside one would end green. The BLOCKED_HTTP entry is the evidence.
    `opted_in` is True for tests that provoke a block on purpose (they request
    the `notifier_host_blocked` fixture)."""
    if opted_in:
        return
    blocked = [e["kwargs"] for e in calls if e["fn"] == "BLOCKED_HTTP"]
    if blocked:
        what = "; ".join(f"{b['host']} via {b['via']}" for b in blocked)
        pytest.fail(
            f"test reached a notifier host and the block fired ({what}). A sender "
            "may have swallowed the NotifierHostBlocked exception, so the test would "
            "otherwise pass. Stub the sender, or request the `notifier_host_blocked` "
            "fixture if the block is intended.",
            pytrace=False,
        )


@pytest.fixture
def notifier_host_blocked():
    """The exception class the host block raises. Requesting this fixture marks
    the test as one that provokes a block on purpose."""
    return NotifierHostBlocked


@pytest.fixture(autouse=True)
def _notifier_capture(request, monkeypatch, tmp_path):
    """Suite-wide notifier capture (hard rule 2): no test can send a Telegram
    or Discord message; every attempt is recorded in the list this yields.

    Recorded by default: the wrapper `alert_openclaw`, its transports
    (`_alert_openclaw_inner`, `_telegram_http_send`) and the Discord sender
    (`signals.discord_alerts._send`, the function that performs the HTTP call).
    Patching the transports as well means a module that did
    `from scripts.openclaw_alerts import alert_openclaw` still ends at a recorder.
    Opt-outs, for tests of the send layer itself (they mock the network):
      @pytest.mark.real_alert_wrapper    keep the real wrapper
      @pytest.mark.real_alert_transport  keep the real wrapper and Telegram transports
    A test's own monkeypatch of the same names runs later and wins."""
    import scripts.openclaw_alerts as oa
    import signals.discord_alerts as da

    calls: list = []

    def recorder(name, result):
        def _record(*args, **kwargs):
            calls.append({"fn": name, "args": args, "kwargs": kwargs})
            return result
        return _record

    for var in _NOTIFIER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    # the send ledger is a live file the watchdog reads: never write it from a test
    monkeypatch.setenv("POLYCLAWD_LEDGER_PATH", str(tmp_path / "telegram_sent.jsonl"))

    keep_transport = request.node.get_closest_marker("real_alert_transport") is not None
    keep_wrapper = keep_transport or request.node.get_closest_marker("real_alert_wrapper") is not None
    if not keep_transport:
        monkeypatch.setattr(oa, "_telegram_http_send", recorder("_telegram_http_send", (True, "")))
        monkeypatch.setattr(oa, "_alert_openclaw_inner", recorder("_alert_openclaw_inner", (True, "")))
    if not keep_wrapper:
        monkeypatch.setattr(oa, "alert_openclaw", recorder("alert_openclaw", True))
    monkeypatch.setattr(da, "_send", recorder("discord_alerts._send", True))
    # Two modules copy their credential into a module constant at import time,
    # which the environment cleanup above cannot change. Blank the constant for
    # the test (monkeypatch undoes it) in each such module that is ALREADY
    # imported; never import them from here (ingame_monitor loads
    # /etc/default/polyclawd into os.environ at import).
    for modname, const in _IMPORT_TIME_CREDENTIAL_CONSTANTS.items():
        mod = sys.modules.get(modname)
        if mod is not None and getattr(mod, const, ""):
            monkeypatch.setattr(mod, const, "")
    # Host-level block: any HTTP attempt to a notifier host fails before network
    # I/O, whatever function makes it and whatever credentials are present.
    # (Tests that mock urlopen themselves patch later and win.)
    import urllib.request
    import requests.sessions

    _block_notifier_hosts(monkeypatch, calls, urllib.request, "urlopen",
                          lambda url, *a, **k: url)
    _block_notifier_hosts(monkeypatch, calls, urllib.request.OpenerDirector, "open",
                          lambda self, fullurl, *a, **k: fullurl)
    _block_notifier_hosts(monkeypatch, calls, requests.sessions.Session, "request",
                          lambda self, method, url, *a, **k: url)
    _block_notifier_hosts(monkeypatch, calls, httpx.Client, "send",
                          lambda self, request, *a, **k: request)
    _block_notifier_hosts(monkeypatch, calls, httpx.AsyncClient, "send",
                          lambda self, request, *a, **k: request, is_async=True)
    yield calls
    _fail_on_swallowed_blocks(calls, "notifier_host_blocked" in request.fixturenames)


@pytest.fixture
def sent_alerts(_notifier_capture) -> list:
    """Notifier calls recorded during this test: dicts with fn, args, kwargs."""
    return _notifier_capture


@pytest.fixture
def tmp_storage(tmp_path: Path) -> Path:
    """Temporary storage directory for test artifacts."""
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    return storage


@pytest.fixture
def test_client() -> Generator[TestClient, None, None]:
    """FastAPI TestClient for integration tests - no external server needed."""
    with TestClient(app) as client:
        yield client


@pytest.fixture
def mock_httpx_client(test_client: TestClient) -> Generator[TestClient, None, None]:
    """Alias for test_client - maintains backward compatibility with existing tests."""
    yield test_client


@pytest.fixture
async def async_httpx_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Async httpx client for testing async HTTP interactions."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        yield client


@pytest.fixture(scope="session")
def event_loop():
    """Create an event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def api_base_url() -> str:
    """Base URL for the API - empty string since TestClient uses relative paths."""
    return ""

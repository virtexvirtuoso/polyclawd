"""Suite-wide notifier capture (plan task 1.0a, hard rule 2).

Every test in every directory must be unable to send a Telegram or Discord
message; whatever a test tries to send is recorded in `sent_alerts`.
"""

import os
import subprocess
import urllib.request

import pytest

import scripts.openclaw_alerts as oa
import signals.discord_alerts as da

# A module-level alias: holds the REAL wrapper, so patching the attribute on
# scripts.openclaw_alerts does not reach it. It must still end at a recorder.
from scripts.openclaw_alerts import alert_openclaw as aliased_alert_openclaw

_ALERT_VARS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL")


@pytest.fixture(scope="module", autouse=True)
def _polluted_env():
    """Simulate a test process that has the live notifier variables set.

    Module scope runs before the function-scoped capture fixture, so the
    capture fixture is what has to remove them."""
    mp = pytest.MonkeyPatch()
    for name in _ALERT_VARS:
        mp.setenv(name, "live-value-must-not-survive")
    yield
    mp.undo()


@pytest.fixture
def no_real_io(monkeypatch):
    """Make any real subprocess or HTTP call fail the test loudly."""

    def _boom(*a, **k):
        raise AssertionError("real subprocess/HTTP call attempted")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)


def test_unpatched_alert_openclaw_is_recorded_not_sent(sent_alerts, no_real_io):
    result = oa.alert_openclaw("x")
    assert result is True
    assert [e["fn"] for e in sent_alerts] == ["alert_openclaw"]
    assert sent_alerts[0]["args"] == ("x",)


@pytest.mark.real_alert_wrapper
def test_real_wrapper_reaches_a_transport_recorder(sent_alerts, no_real_io):
    result = oa.alert_openclaw("x", silent=True)
    assert result is True
    assert [e["fn"] for e in sent_alerts] == ["_alert_openclaw_inner"]
    assert sent_alerts[0]["args"] == ("x",)
    assert sent_alerts[0]["kwargs"]["silent"] is True


def test_module_level_alias_still_ends_at_a_transport_recorder(sent_alerts, no_real_io):
    assert aliased_alert_openclaw("x") is True
    assert [e["fn"] for e in sent_alerts] == ["_alert_openclaw_inner"]


def test_telegram_http_send_is_recorded_and_returns_success_tuple(sent_alerts, no_real_io):
    assert oa._telegram_http_send("x", silent=False) == (True, "")
    assert [e["fn"] for e in sent_alerts] == ["_telegram_http_send"]


def test_discord_send_is_recorded_and_performs_no_http(monkeypatch, sent_alerts, no_real_io):
    # Even with a webhook configured at module level, nothing may go out.
    monkeypatch.setattr(da, "WEBHOOK_URL", "https://discord.invalid/api/webhooks/1/x")
    assert da._send([{"title": "t"}], content="c", alert_type="unit") is True
    assert [e["fn"] for e in sent_alerts] == ["discord_alerts._send"]
    assert sent_alerts[0]["kwargs"]["content"] == "c"


def test_notifier_env_vars_are_removed_inside_a_test():
    for name in _ALERT_VARS:
        assert name not in os.environ, name


@pytest.mark.real_alert_transport
def test_transport_marker_keeps_real_transport_but_env_stays_clean(sent_alerts):
    # The real _telegram_http_send runs; with the token removed it cannot send.
    assert oa._telegram_http_send("x") == (False, "no_token")
    assert sent_alerts == []


def test_ledger_path_is_redirected_away_from_the_repo(tmp_path):
    ledger = os.environ.get("POLYCLAWD_LEDGER_PATH", "")
    assert ledger and os.path.dirname(ledger).startswith(str(tmp_path.parent))


def test_capture_is_autouse_no_fixture_requested(no_real_io):
    # Requests neither sent_alerts nor the capture fixture: only autouse can
    # make the real transport unreachable here.
    assert oa._alert_openclaw_inner("x") == (True, "")
    assert oa._telegram_http_send("x") == (True, "")


# --- import-time credential constants (plan task 1.0c, part 1) --------------
# signals.tier1_whale_alert and signals.ingame_monitor copy their credentials
# into module constants at import, so the per-test environment cleanup cannot
# change them. Importing the real modules in a test is not safe (ingame_monitor
# reads /etc/default/polyclawd into os.environ at import), so these tests put
# stand-in module objects holding a non-empty constant of the same name into
# sys.modules before the capture fixture runs.

import sys
import types

_IMPORT_TIME_CONSTANTS = {
    "signals.tier1_whale_alert": "TELEGRAM_BOT_TOKEN",
    "signals.ingame_monitor": "DISCORD_WEBHOOK_URL",
}


@pytest.fixture(scope="module", autouse=True)
def _fake_import_time_modules():
    """Module scope runs before the function-scoped capture fixture."""
    mp = pytest.MonkeyPatch()
    for modname, const in _IMPORT_TIME_CONSTANTS.items():
        fake = types.ModuleType(modname)
        setattr(fake, const, "live-value-must-not-survive")
        mp.setitem(sys.modules, modname, fake)
    yield
    mp.undo()


@pytest.mark.parametrize("modname,const", sorted(_IMPORT_TIME_CONSTANTS.items()))
def test_import_time_credential_constant_is_emptied(modname, const, sent_alerts):
    assert getattr(sys.modules[modname], const) == ""


# --- host-level block (plan task 1.0d) --------------------------------------
# The capture above replaces known senders. The block below works one layer
# lower: whatever function tries to open an HTTP connection to a notifier host
# (Telegram, Discord) fails before any network I/O, with any credentials set.
# Every test here also installs a socket-layer guard, so a missing block shows
# up as "reached the socket layer" and never as a real connection.

import re
from pathlib import Path

import httpx
import requests


class SocketReached(Exception):
    """Raised by the socket-layer guard: the call got past the host block."""


@pytest.fixture
def socket_hits(monkeypatch):
    import socket

    hits: list = []

    def _guard(name):
        def _boom(*a, **k):
            hits.append(name)
            raise SocketReached(name)
        return _boom

    monkeypatch.setattr(socket.socket, "connect", _guard("connect"))
    monkeypatch.setattr(socket, "getaddrinfo", _guard("getaddrinfo"))
    return hits


def _blocked_entries(sent_alerts):
    return [e for e in sent_alerts if e["fn"] == "BLOCKED_HTTP"]


def test_urlopen_to_telegram_is_blocked_even_with_a_token(
        monkeypatch, sent_alerts, socket_hits, notifier_host_blocked):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:fake")
    with pytest.raises(notifier_host_blocked, match="api.telegram.org"):
        urllib.request.urlopen("https://api.telegram.org/botX/sendMessage", data=b"a=b")
    assert socket_hits == []
    entries = _blocked_entries(sent_alerts)
    assert len(entries) == 1
    assert entries[0]["kwargs"]["host"] == "api.telegram.org"


def test_urlopen_with_a_request_object_is_blocked(sent_alerts, socket_hits, notifier_host_blocked):
    req = urllib.request.Request("https://API.Telegram.org/botX/sendMessage", data=b"a=b")
    with pytest.raises(notifier_host_blocked):
        urllib.request.urlopen(req, timeout=5)
    assert socket_hits == []
    assert len(_blocked_entries(sent_alerts)) == 1


def test_opener_open_to_telegram_is_blocked(sent_alerts, socket_hits, notifier_host_blocked):
    opener = urllib.request.build_opener()
    with pytest.raises(notifier_host_blocked):
        opener.open("https://api.telegram.org/botX/sendMessage", data=b"a=b")
    assert socket_hits == []
    assert len(_blocked_entries(sent_alerts)) == 1


@pytest.mark.parametrize("url", [
    "https://discord.com/api/webhooks/1/abc",
    "https://ptb.discord.com/api/webhooks/1/abc",
    "https://canary.discord.com/api/webhooks/1/abc",
    "https://discordapp.com/api/webhooks/1/abc",
    "https://cdn.discordapp.com/x",
    "https://discord.com./api/webhooks/1/abc",
])
def test_requests_to_discord_hosts_are_blocked(url, sent_alerts, socket_hits, notifier_host_blocked):
    with pytest.raises(notifier_host_blocked):
        requests.Session().post(url, json={})
    assert socket_hits == []
    assert len(_blocked_entries(sent_alerts)) == 1


def test_requests_module_post_is_blocked(sent_alerts, socket_hits, notifier_host_blocked):
    with pytest.raises(notifier_host_blocked):
        requests.post("https://discord.com/api/webhooks/1/abc", json={})
    assert socket_hits == []


def test_httpx_client_to_telegram_is_blocked(sent_alerts, socket_hits, notifier_host_blocked):
    with httpx.Client() as client:
        with pytest.raises(notifier_host_blocked):
            client.post("https://api.telegram.org/botX/sendMessage", data={"a": "b"})
    assert socket_hits == []
    assert len(_blocked_entries(sent_alerts)) == 1


async def test_httpx_async_client_to_discord_is_blocked(sent_alerts, socket_hits, notifier_host_blocked):
    async with httpx.AsyncClient() as client:
        with pytest.raises(notifier_host_blocked):
            await client.post("https://discord.com/api/webhooks/1/abc", json={})
    assert socket_hits == []
    assert len(_blocked_entries(sent_alerts)) == 1


def test_non_notifier_host_is_passed_through_to_the_real_function(socket_hits):
    with pytest.raises(Exception):
        urllib.request.urlopen("http://example.test/x", timeout=1)
    assert socket_hits, "wrapper did not call through to the real opener"
    socket_hits.clear()
    with pytest.raises(Exception):
        requests.get("http://localhost:9/x", timeout=1)
    assert socket_hits
    socket_hits.clear()
    with pytest.raises(Exception):
        httpx.get("http://example.test/x", timeout=1)
    assert socket_hits


@pytest.mark.parametrize("host", [
    "discord.com.evil.example",
    "notdiscord.com",
    "api.telegram.org.evil.example",
    "evil.example",
])
def test_lookalike_hosts_are_not_treated_as_notifier_hosts(host, sent_alerts, socket_hits):
    # a notifier name in the path or query must not matter either
    url = f"http://{host}/discord.com/api.telegram.org?u=discord.com"
    with pytest.raises(Exception):
        urllib.request.urlopen(url, timeout=1)
    assert socket_hits, "look-alike host was blocked"
    assert _blocked_entries(sent_alerts) == []


# Each direct sender, called with a fake credential set, must end at the block.

def _assert_sender_blocked(sent_alerts, socket_hits):
    assert socket_hits == [], "sender reached the socket layer"
    entries = _blocked_entries(sent_alerts)
    assert entries, "sender did not hit the host block"
    assert {e["kwargs"]["host"] for e in entries} == {"api.telegram.org"}


def test_tier1_whale_alert_send_is_blocked(monkeypatch, sent_alerts, socket_hits,
                                           notifier_host_blocked):
    # this file puts a stand-in module in sys.modules (module-scoped fixture
    # above); drop it for this test so the real module is imported
    monkeypatch.delitem(sys.modules, "signals.tier1_whale_alert", raising=False)
    import signals.tier1_whale_alert as t1
    assert hasattr(t1, "_send_telegram")
    monkeypatch.setattr(t1, "TELEGRAM_BOT_TOKEN", "123:fake")
    assert t1._send_telegram("hello") is False
    _assert_sender_blocked(sent_alerts, socket_hits)


def test_prop_clv_logger_send_is_blocked(monkeypatch, sent_alerts, socket_hits,
                                         notifier_host_blocked):
    import scripts.prop_clv_logger as pc
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:fake")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    pc.send_telegram("hello")
    _assert_sender_blocked(sent_alerts, socket_hits)


def test_sports_pulse_send_is_blocked(monkeypatch, sent_alerts, socket_hits,
                                      notifier_host_blocked):
    import scripts.sports_pulse as sp
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:fake")
    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    sp._send_telegram("hello")
    _assert_sender_blocked(sent_alerts, socket_hits)


def test_scorer_paper_portfolio_send_is_blocked(monkeypatch, sent_alerts, socket_hits,
                                                notifier_host_blocked):
    import signals.scorer_paper_portfolio as sc
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:fake")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    sc.send_alert([], send=True)
    _assert_sender_blocked(sent_alerts, socket_hits)


# signals.ingame_monitor posts to Discord directly and loads /etc/default/polyclawd
# into os.environ when imported, so no test may import it until the capture
# stops depending on a module that was already imported.

_INGAME_IMPORT = re.compile(
    r"^\s*(?:import\s+(?:signals\.)?ingame_monitor\b"
    r"|from\s+(?:signals\.)?ingame_monitor\b"
    r"|from\s+signals\s+import\s+[^#\n]*\bingame_monitor\b)"
    r"|import_module\(\s*['\"](?:signals\.)?ingame_monitor['\"]"
    r"|__import__\(\s*['\"](?:signals\.)?ingame_monitor['\"]",
    re.MULTILINE,
)


def test_no_test_imports_ingame_monitor():
    here = Path(__file__).resolve()
    offenders = [
        str(p.relative_to(here.parent.parent))
        for p in sorted(here.parent.rglob("*.py"))
        if p.resolve() != here and _INGAME_IMPORT.search(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        f"{offenders} import signals.ingame_monitor, which loads /etc/default/polyclawd "
        "at import and posts to Discord directly. Extend the notifier capture in "
        "tests/conftest.py first (block or stub it before the import), then change this guard."
    )


def test_ingame_import_guard_regex_matches_the_import_forms():
    for line in ("import signals.ingame_monitor", "from signals.ingame_monitor import X",
                 "from signals import ingame_monitor", "    import ingame_monitor",
                 "importlib.import_module('signals.ingame_monitor')"):
        assert _INGAME_IMPORT.search(line), line
    for line in ('"signals.ingame_monitor": "X",', "# ingame_monitor loads the env at import"):
        assert not _INGAME_IMPORT.search(line), line


# --- a swallowed block must fail the test (task 1.0e) ------------------------
# NotifierHostBlocked is an AssertionError, but senders wrap their HTTP call in
# `except Exception`, so the exception alone cannot fail a test. The capture
# fixture fails the test at teardown if a BLOCKED_HTTP entry was recorded and
# the test did not request `notifier_host_blocked`.

import importlib.util
import textwrap

_CONFTEST = Path(__file__).resolve().parent / "conftest.py"


def _fail_on_swallowed_blocks(calls, opted_in):
    # a private copy of tests/conftest.py, loaded by path: the module name
    # `conftest` is ambiguous when several conftest.py files exist
    spec = importlib.util.spec_from_file_location("repo_tests_conftest_copy", _CONFTEST)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._fail_on_swallowed_blocks(calls, opted_in)


def _entry(host="api.telegram.org", via="urlopen"):
    return {"fn": "BLOCKED_HTTP", "args": (), "kwargs": {"host": host, "via": via}}


def test_teardown_check_raises_naming_host_and_function():
    with pytest.raises(pytest.fail.Exception) as exc:
        _fail_on_swallowed_blocks([{"fn": "alert_openclaw", "args": (), "kwargs": {}}, _entry()],
                                  opted_in=False)
    assert "api.telegram.org via urlopen" in str(exc.value)


def test_teardown_check_ignores_non_block_entries():
    _fail_on_swallowed_blocks([{"fn": "alert_openclaw", "args": (), "kwargs": {}}], opted_in=False)
    _fail_on_swallowed_blocks([], opted_in=False)


def test_teardown_check_passes_when_opted_in():
    _fail_on_swallowed_blocks([_entry()], opted_in=True)


_INNER = textwrap.dedent('''
    import importlib.util, sys
    import pytest

    spec = importlib.util.spec_from_file_location("repo_tests_conftest", {conftest!r})
    _m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_m)
    _notifier_capture = _m._notifier_capture
    notifier_host_blocked = _m.notifier_host_blocked
    sent_alerts = _m.sent_alerts
''')

_INNER_TESTS = textwrap.dedent('''
    import urllib.request

    def _swallowing_sender():
        try:
            urllib.request.urlopen("https://api.telegram.org/botX/sendMessage")
        except Exception:
            pass

    def test_swallowed_block_without_opt_in():
        _swallowing_sender()

    def test_swallowed_block_with_opt_in(notifier_host_blocked):
        _swallowing_sender()

    def test_clean():
        pass
''')


def _run_inner(tmp_path, selector):
    (tmp_path / "conftest.py").write_text(_INNER.format(conftest=str(_CONFTEST)))
    (tmp_path / "test_inner.py").write_text(_INNER_TESTS)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "--no-header",
         "--rootdir", str(tmp_path), f"test_inner.py::{selector}"],
        cwd=tmp_path, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
    )


def test_swallowed_block_fails_the_test_at_teardown(tmp_path):
    r = _run_inner(tmp_path, "test_swallowed_block_without_opt_in")
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "api.telegram.org via urlopen" in out, out


def test_swallowed_block_in_an_opted_in_test_passes(tmp_path):
    r = _run_inner(tmp_path, "test_swallowed_block_with_opt_in")
    assert r.returncode == 0, r.stdout + r.stderr


def test_clean_test_is_unaffected_by_the_teardown_check(tmp_path):
    r = _run_inner(tmp_path, "test_clean")
    assert r.returncode == 0, r.stdout + r.stderr


# --- the five wrappers keep the original's name and signature (task 1.0e) ----


def _wrapper_targets():
    import requests.sessions
    return [(urllib.request, "urlopen"), (urllib.request.OpenerDirector, "open"),
            (requests.sessions.Session, "request"), (httpx.Client, "send"),
            (httpx.AsyncClient, "send")]


@pytest.mark.parametrize("index", range(5))
def test_host_block_wrapper_preserves_name_and_signature(index):
    import inspect
    owner, name = _wrapper_targets()[index]
    wrapper = getattr(owner, name)
    original = inspect.unwrap(wrapper)
    assert wrapper is not original, "no wrapper installed"
    assert wrapper.__name__ == original.__name__ == name
    assert list(inspect.signature(wrapper).parameters) == list(inspect.signature(original).parameters)
    assert inspect.iscoroutinefunction(wrapper) == inspect.iscoroutinefunction(original)


def test_urlopen_signature_is_the_real_one():
    import inspect
    params = list(inspect.signature(urllib.request.urlopen).parameters)
    assert params[:2] == ["url", "data"] and "timeout" in params


def test_async_send_wrapper_is_still_a_coroutine_function():
    import inspect
    assert inspect.iscoroutinefunction(httpx.AsyncClient.send)

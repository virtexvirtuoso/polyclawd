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

"""Auth circuit breaker for the Odds API key (rewritten 2026-09-18).

Covers the incident this was built for: the prod key returned
401 DEACTIVATED_KEY and every fetch path retried it in a loop, with no alert.

Source of truth: odds/rate_limiter.py (BREAKER_* block + can_make_call).
Provenance: the original copy of this file died with the ~/Desktop/polyclawd
tree on 2026-09-14 and was never committed; this rewrite was recovered from
the stale .pyc test-name list plus the live VPS implementation.

2026-10-05: Telegram trip/clear pages removed at Mr. V's request (the Oct 5
billing lapse re-tripped 4x and a two-process race doubled every page).
Tests now assert the breaker trips/clears WITHOUT paging; the halt + probe
self-heal behaviour is unchanged.
"""

import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from odds import rate_limiter as rl


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    """Point breaker + usage state at a temp dir and capture alerts instead of
    sending them, so a test run can never page Mr. V or poison prod state."""
    breaker_file = tmp_path / "breaker.json"
    monkeypatch.setattr(rl, "BREAKER_FILE", breaker_file)
    monkeypatch.setattr(rl, "RATE_FILE", tmp_path / "usage.json")
    monkeypatch.setattr(rl, "REAL_CREDIT_FILE", tmp_path / "credit.json")

    alerts: list[str] = []
    monkeypatch.setitem(
        sys.modules,
        "scripts.openclaw_alerts",
        SimpleNamespace(alert_openclaw=lambda text, channel=None: alerts.append(text)),
    )
    return SimpleNamespace(breaker_file=breaker_file, alerts=alerts)


@pytest.fixture
def shift_clock(monkeypatch):
    """Shift rate_limiter's view of `now` forward incrementally, so probe-interval
    behaviour is tested without sleeping. Returns a function taking seconds."""
    state = {"delta": timedelta(0)}

    class _Shifted(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + state["delta"]

    monkeypatch.setattr(rl, "datetime", _Shifted)

    def _shift(seconds: float) -> None:
        state["delta"] += timedelta(seconds=seconds)

    return _shift


def _trip(key_prefix: str = "51ef") -> bool:
    """Trip the breaker the way the prod incident did (2026-08-29)."""
    return rl.note_auth_failure(
        401, '{"error_code": "DEACTIVATED_KEY"}', key_prefix=key_prefix
    )


def test_untripped_by_default_and_gate_open(isolate):
    assert rl.read_breaker()["tripped"] is False
    assert rl.breaker_blocks() == (False, "OK")
    assert rl.can_make_call("critical") == (True, "OK")


def test_401_deactivated_trips_without_paging(isolate):
    assert _trip() is True
    state = rl.read_breaker()
    assert state["tripped"] is True
    assert state["status"] == 401
    assert state["error_code"] == "DEACTIVATED_KEY"
    assert state["fail_count"] == 1
    # Telegram pages removed 2026-10-05 (Mr. V request): tripping must NOT page.
    assert isolate.alerts == []

    # A failure on an already-tripped breaker updates the count but must NOT
    # re-alert — one journal line per incident, never a page.
    assert _trip() is False
    assert rl.read_breaker()["fail_count"] == 2
    assert isolate.alerts == []


def test_tripped_breaker_blocks_every_priority_including_critical(isolate):
    _trip()
    for priority in ("critical", "high", "normal", "low"):
        allowed, reason = rl.can_make_call(priority)
        assert allowed is False, priority
        assert "breaker" in reason.lower(), (priority, reason)
    blocked, reason = rl.breaker_blocks()
    assert blocked is True
    assert "auth breaker" in reason.lower()


def test_transient_status_does_not_trip(isolate):
    for status in (429, 500, 503):
        assert rl.note_auth_failure(status, "transient") is False
    assert rl.read_breaker()["tripped"] is False
    assert isolate.alerts == []
    assert rl.breaker_blocks() == (False, "OK")


def test_half_open_releases_exactly_one_probe_per_interval(isolate, shift_clock):
    _trip()  # stamps last_probe at trip time
    assert rl.breaker_blocks()[0] is True

    # Just before the probe interval elapses: still blocked.
    shift_clock(rl.BREAKER_PROBE_INTERVAL_S - 1)
    assert rl.breaker_blocks()[0] is True

    # Interval elapsed: exactly one probe is released...
    shift_clock(2)
    released, reason = rl.breaker_blocks()
    assert released is False
    assert "probe" in reason.lower()
    # ...and releasing it re-blocks everyone else immediately.
    assert rl.breaker_blocks()[0] is True

    # A fresh interval releases exactly one more probe.
    shift_clock(rl.BREAKER_PROBE_INTERVAL_S + 1)
    assert rl.breaker_blocks()[0] is False
    assert rl.breaker_blocks()[0] is True


def test_success_clears_breaker_silently(isolate):
    _trip()
    assert isolate.alerts == []  # no TRIPPED page anymore

    rl.note_auth_success()
    state = rl.read_breaker()
    assert state["tripped"] is False
    assert "recovered_at" in state
    assert isolate.alerts == []  # no CLEARED page either
    assert rl.breaker_blocks() == (False, "OK")

    # Recovery is a no-op on an already-clear breaker.
    rl.note_auth_success()
    assert isolate.alerts == []


def test_corrupt_breaker_file_fails_open(isolate):
    # A missing state file must never fail closed and mute the whole fleet.
    isolate.breaker_file.write_text("{ this is not json")
    assert rl.read_breaker() == {"tripped": False}
    assert rl.breaker_blocks() == (False, "OK")
    assert rl.can_make_call("critical") == (True, "OK")


def test_clear_key_breaker_escape_hatch(isolate):
    _trip()
    prior = rl.clear_key_breaker("key rotated")
    assert prior["tripped"] is True
    assert rl.read_breaker()["tripped"] is False
    assert rl.breaker_blocks() == (False, "OK")


def test_track_credits_only_clears_on_authed_odds_response(isolate):
    """Regression 2026-10-05: betfair_edge/pitcher_profile hand the raw
    requests response to _track_credits_from_response — a 401 used to clear
    the tripped breaker (4 re-trips in one day). Only a 200 carrying
    x-requests-remaining may clear."""
    from odds import the_odds_api as toa

    _trip()
    toa._track_credits_from_response(SimpleNamespace(status_code=401, headers={}))
    toa._track_credits_from_response(SimpleNamespace(status_code=200, headers={}))
    assert rl.read_breaker()["tripped"] is True

    toa._track_credits_from_response(
        SimpleNamespace(status_code=200, headers={"x-requests-remaining": "4999999"})
    )
    assert rl.read_breaker()["tripped"] is False


def test_monitor_gate_non_odds_success_does_not_clear(isolate, shift_clock, monkeypatch):
    """While tripped, the half-open probe slot can be consumed by a non-Odds
    gated fetch (whale_alert_tg etc.); its success must NOT clear the breaker."""
    import odds.monitor_gate as mg

    _trip()
    shift_clock(rl.BREAKER_PROBE_INTERVAL_S + 1)  # release exactly one probe

    class FakeResp:
        headers = {"x-requests-remaining": "4999999"}

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        mg.urllib.request, "urlopen", lambda req, timeout=10: FakeResp()
    )
    data = mg.gated_fetch_json("https://api.elections.kalshi.com/trade-api/v2", {"limit": 1})
    assert data == {}
    assert rl.read_breaker()["tripped"] is True
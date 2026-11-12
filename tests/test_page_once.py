"""page_once: send unless `key` was DELIVERED within cooldown_s; stamp only on
delivery (plan task 1.0c part 3 / 3.1). `now=` is always passed: no sleeping."""

import pytest

import scripts.openclaw_alerts as oa


def _sent(sent_alerts):
    return [e for e in sent_alerts if e["fn"] == "alert_openclaw"]


@pytest.fixture
def failing_send(monkeypatch):
    """alert_openclaw that records the attempt and fails (Telegram down)."""
    calls = []

    def _fail(*a, **k):
        calls.append((a, k))
        return False

    monkeypatch.setattr(oa, "alert_openclaw", _fail)
    return calls


def test_first_call_delivers_and_stamps(sent_alerts):
    state = {}
    assert oa.page_once("k", 300, "hello", state=state, now=1000.0) is True
    assert len(_sent(sent_alerts)) == 1
    assert _sent(sent_alerts)[0]["args"] == ("hello",)
    assert state == {"k": 1000.0}


def test_second_call_inside_cooldown_sends_nothing(sent_alerts):
    state = {}
    oa.page_once("k", 300, "hello", state=state, now=1000.0)
    assert oa.page_once("k", 300, "hello", state=state, now=1299.0) is False
    assert len(_sent(sent_alerts)) == 1
    assert state == {"k": 1000.0}


def test_call_after_cooldown_sends_again_and_restamps(sent_alerts):
    state = {}
    oa.page_once("k", 300, "hello", state=state, now=1000.0)
    assert oa.page_once("k", 300, "hello", state=state, now=1300.0) is True
    assert len(_sent(sent_alerts)) == 2
    assert state == {"k": 1300.0}


def test_failed_send_is_not_stamped_and_next_call_sends_again(failing_send, monkeypatch):
    """The defect the helper exists to fix: stamp-before-send silences a page
    that never reached anyone."""
    state = {}
    assert oa.page_once("k", 300, "hello", state=state, now=1000.0) is False
    assert state == {}
    # next call, still inside what would have been the cooldown: sends again
    assert oa.page_once("k", 300, "hello", state=state, now=1010.0) is False
    assert len(failing_send) == 2
    # once delivery works, it is delivered and stamped
    monkeypatch.setattr(oa, "alert_openclaw", lambda *a, **k: True)
    assert oa.page_once("k", 300, "hello", state=state, now=1020.0) is True
    assert state == {"k": 1020.0}


def test_send_kwargs_are_forwarded(sent_alerts):
    oa.page_once("k", 300, "hello", state={}, now=1000.0, silent=True, parse_mode="HTML")
    (entry,) = _sent(sent_alerts)
    assert entry["args"] == ("hello",)
    assert entry["kwargs"] == {"silent": True, "parse_mode": "HTML"}


def test_different_keys_do_not_block_each_other(sent_alerts):
    state = {}
    assert oa.page_once("a", 300, "A", state=state, now=1000.0) is True
    assert oa.page_once("b", 300, "B", state=state, now=1001.0) is True
    assert oa.page_once("a", 300, "A", state=state, now=1002.0) is False
    assert len(_sent(sent_alerts)) == 2
    assert state == {"a": 1000.0, "b": 1001.0}


def test_default_now_uses_time_time(sent_alerts, monkeypatch):
    monkeypatch.setattr(oa.time, "time", lambda: 5000.0)
    state = {}
    assert oa.page_once("k", 60, "x", state=state) is True
    assert state == {"k": 5000.0}


# --- robustness (task 1.0e) -------------------------------------------------


def test_stamp_in_the_future_does_not_silence_the_page_and_is_corrected(sent_alerts):
    state = {"k": 9000.0}  # clock stepped back, or written under another clock
    assert oa.page_once("k", 300, "x", state=state, now=1000.0) is True
    assert len(_sent(sent_alerts)) == 1
    assert state == {"k": 1000.0}


@pytest.mark.parametrize("bad", [None, "", "abc", [], {}])
def test_non_numeric_stamp_is_treated_as_absent(sent_alerts, bad):
    state = {"k": bad}
    assert oa.page_once("k", 300, "x", state=state, now=1000.0) is True
    assert state == {"k": 1000.0}


def test_numeric_string_stamp_is_honoured(sent_alerts):
    state = {"k": "800"}
    assert oa.page_once("k", 300, "x", state=state, now=1000.0) is False
    assert _sent(sent_alerts) == []


@pytest.mark.parametrize("cooldown", [0, -5])
def test_zero_or_negative_cooldown_always_sends(sent_alerts, cooldown):
    state = {}
    assert oa.page_once("k", cooldown, "x", state=state, now=1000.0) is True
    assert oa.page_once("k", cooldown, "x", state=state, now=1000.0) is True
    assert len(_sent(sent_alerts)) == 2

"""Task 1.10: at scheduler start, LIVE but not armed is logged at once and paged in the background.

`main()` calls `_start_live_unarmed_page()` right after the existing start-up check.
It writes the ERROR line synchronously, then hands the page to a worker thread so a
slow alert transport (up to about 70 s when Telegram is down) cannot hold up the task
loops. The page goes through page_once with a per-process state dict and a constant
key: one page per scheduler start. Notifier calls are recorded by the suite-wide
capture (`sent_alerts`).
"""

import ast
import asyncio
import logging
import threading
from pathlib import Path

import pytest

import services.scheduler as sched
from execution import live_config


@pytest.fixture(autouse=True)
def _fresh_page_state(monkeypatch):
    monkeypatch.setattr(sched, "_LIVE_UNARMED_PAGE_STATE", {})


def _error_messages(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "scheduler" and r.levelno >= logging.ERROR]


# --- the log line (synchronous) -----------------------------------------------------


def test_log_writes_one_error_line_with_the_state_and_returns_the_page_text(mode_env, caplog):
    mode_env("LIVE", None, False)
    caplog.set_level(logging.INFO)
    text = sched._log_live_unarmed()
    errors = _error_messages(caplog)
    assert len(errors) == 1
    assert errors[0].startswith("LIVE but not armed: no order will be sent")
    assert live_config.arming_state_text() in errors[0]
    assert text is not None and live_config.arming_state_text() in text


def test_log_returns_none_and_logs_nothing_under_paper(mode_env, caplog):
    mode_env("PAPER", None, False)
    caplog.set_level(logging.INFO)
    assert sched._log_live_unarmed() is None
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_log_returns_none_when_armed(armed, caplog):
    caplog.set_level(logging.INFO)
    assert sched._log_live_unarmed() is None
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_log_never_raises_when_the_arming_check_fails(monkeypatch, caplog):
    def boom():
        raise RuntimeError("state is broken")

    monkeypatch.setattr(live_config, "mode", boom)
    caplog.set_level(logging.INFO)
    assert sched._log_live_unarmed() is None
    assert any("arming check failed" in m for m in _error_messages(caplog))


# --- the page text and delivery -----------------------------------------------------


def test_page_text_is_plain_readable_and_carries_the_state_text(mode_env, sent_alerts):
    mode_env("LIVE", "1", False)
    text = sched._log_live_unarmed()
    assert sched._page_live_unarmed(text) is True
    assert len(sent_alerts) == 1
    sent = sent_alerts[0]["args"][0]
    assert sent == text
    assert sent.startswith("Polyclawd scheduler: mode is LIVE but the bot is NOT armed")
    assert "behaving as PAPER" in sent and "will send no order" in sent
    assert live_config.arming_state_text() in sent
    assert "mode=LIVE" in sent and "enforce=True" in sent and "LIVE_ORDER_PATH_READY=False" in sent
    assert sent.endswith("otherwise set POLYCLAWD_MODE=PAPER.")
    assert sent_alerts[0]["kwargs"].get("parse_mode") is None  # no markdown that could break Telegram


def test_one_page_per_call_and_the_key_is_constant(mode_env, sent_alerts):
    """Per-process state and one call per start: nothing can suppress a page, and the
    key carries no date. A second call in the same process pages again."""
    mode_env("LIVE", None, False)
    text = sched._log_live_unarmed()
    assert sched._page_live_unarmed(text) is True
    assert list(sched._LIVE_UNARMED_PAGE_STATE) == ["live-unarmed"]
    assert len(sent_alerts) == 1


def test_a_failing_page_never_escapes_and_is_logged(mode_env, monkeypatch, caplog):
    import scripts.openclaw_alerts as oa

    def boom(*a, **k):
        raise RuntimeError("telegram is down")

    mode_env("LIVE", None, False)
    monkeypatch.setattr(oa, "page_once", boom)
    caplog.set_level(logging.INFO)
    assert sched._page_live_unarmed("text") is False
    assert any("telegram is down" in m for m in _error_messages(caplog))


def test_an_undelivered_page_is_retried_on_the_next_call(mode_env, monkeypatch, sent_alerts):
    """page_once stamps only on delivery: a failed send does not use up the cooldown."""
    import scripts.openclaw_alerts as oa

    mode_env("LIVE", None, False)
    real = oa.alert_openclaw
    monkeypatch.setattr(oa, "alert_openclaw", lambda *a, **k: False)
    assert sched._page_live_unarmed("text") is False
    assert sched._LIVE_UNARMED_PAGE_STATE == {}
    monkeypatch.setattr(oa, "alert_openclaw", real)
    assert sched._page_live_unarmed("text") is True
    assert len(sent_alerts) == 1


# --- start-up: log now, page in the background ----------------------------------------


async def test_start_logs_before_handing_to_a_thread_even_when_the_page_hangs(
    mode_env, monkeypatch, caplog
):
    """The page blocks (simulated slow transport). start returns at once, the ERROR line
    is already in the journal, and releasing the page lets the background task finish."""
    import scripts.openclaw_alerts as oa

    gate, entered = threading.Event(), threading.Event()

    def hang(*a, **k):
        entered.set()
        assert gate.wait(10), "test never released the page"
        return True

    mode_env("LIVE", None, False)
    monkeypatch.setattr(oa, "page_once", hang)
    caplog.set_level(logging.INFO)
    task = sched._start_live_unarmed_page()
    try:
        assert task is not None and not task.done()
        assert any("LIVE but not armed" in m for m in _error_messages(caplog))  # logged, page not done
        assert task in sched._BACKGROUND_TASKS  # a reference is kept
    finally:
        gate.set()
    await asyncio.wait_for(task, 10)
    assert entered.is_set()
    await asyncio.sleep(0)
    assert task not in sched._BACKGROUND_TASKS


async def test_start_logs_even_when_the_page_function_raises(mode_env, monkeypatch, caplog):
    import scripts.openclaw_alerts as oa

    def boom(*a, **k):
        raise RuntimeError("telegram is down")

    mode_env("LIVE", None, False)
    monkeypatch.setattr(oa, "page_once", boom)
    caplog.set_level(logging.INFO)
    task = sched._start_live_unarmed_page()
    assert any("LIVE but not armed" in m for m in _error_messages(caplog))
    await asyncio.wait_for(task, 10)  # does not raise into the caller
    assert task.exception() is None
    assert any("telegram is down" in m for m in _error_messages(caplog))


async def test_start_pages_once_under_live_unarmed(mode_env, sent_alerts):
    mode_env("LIVE", None, False)
    task = sched._start_live_unarmed_page()
    await asyncio.wait_for(task, 10)
    assert len(sent_alerts) == 1
    assert live_config.arming_state_text() in sent_alerts[0]["args"][0]


async def test_start_does_nothing_under_paper(mode_env, monkeypatch, sent_alerts):
    import scripts.openclaw_alerts as oa

    mode_env("PAPER", None, False)
    monkeypatch.setattr(oa, "page_once", lambda *a, **k: pytest.fail("page_once used under PAPER"))
    assert sched._start_live_unarmed_page() is None
    assert sent_alerts == []
    assert sched._LIVE_UNARMED_PAGE_STATE == {}


async def test_start_does_nothing_when_armed(armed, sent_alerts):
    assert sched._start_live_unarmed_page() is None
    assert sent_alerts == []


def test_main_starts_it_after_the_cap_check_and_before_the_task_loops():
    tree = ast.parse(Path(sched.__file__).read_text())
    main = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "main")
    calls = [
        n.value.func.id
        for n in main.body
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name)
    ]
    i = calls.index("_assert_live_cap_fits_min_action")
    assert calls[i + 1] == "_start_live_unarmed_page"
    # the call is a plain statement (not awaited): the page is off the start-up path
    tasks_line = next(
        n.lineno for n in main.body
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "tasks" for t in n.targets)
    )
    start_line = next(
        n.lineno for n in main.body
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
        and getattr(n.value.func, "id", None) == "_start_live_unarmed_page"
    )
    assert start_line < tasks_line

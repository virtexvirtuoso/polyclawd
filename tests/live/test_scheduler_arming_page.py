"""Task 1.10, reader 4: at scheduler start, LIVE but not armed is logged and paged once.

`_page_if_live_unarmed` runs in main() right after the existing start-up check. It
pages through page_once with a per-process state dict, so one page per process start
per UTC day. Notifier calls are recorded by the suite-wide capture (`sent_alerts`).
"""

import ast
import logging
from pathlib import Path

import pytest

import services.scheduler as sched

# 2026-11-11 12:00:00 UTC and 2026-11-12 12:00:00 UTC
T1 = 1_794_398_400.0
T2 = T1 + 86_400.0


@pytest.fixture(autouse=True)
def _fresh_page_state(monkeypatch):
    monkeypatch.setattr(sched, "_LIVE_UNARMED_PAGE_STATE", {})


def test_live_unarmed_pages_exactly_once_with_the_three_components(mode_env, sent_alerts, caplog):
    mode_env("LIVE", None, False)
    caplog.set_level(logging.INFO)
    assert sched._page_if_live_unarmed(now=T1) is True
    assert len(sent_alerts) == 1
    text = sent_alerts[0]["args"][0]
    assert text.startswith("LIVE but not armed: no order will be sent")
    assert "mode=LIVE" in text and "enforce=False" in text and "LIVE_ORDER_PATH_READY=False" in text
    errors = [r for r in caplog.records if r.name == "scheduler" and r.levelno == logging.ERROR]
    assert len(errors) == 1 and "LIVE but not armed" in errors[0].getMessage()


def test_called_twice_in_one_process_still_one_page(mode_env, sent_alerts):
    mode_env("LIVE", "1", False)
    assert sched._page_if_live_unarmed(now=T1) is True
    assert sched._page_if_live_unarmed(now=T1 + 600) is True  # still unarmed, page suppressed
    assert len(sent_alerts) == 1


def test_key_carries_the_utc_date_so_the_next_day_pages_again(mode_env, sent_alerts):
    mode_env("LIVE", None, False)
    sched._page_if_live_unarmed(now=T1)
    assert list(sched._LIVE_UNARMED_PAGE_STATE) == ["live-unarmed-2026-11-11"]
    sched._page_if_live_unarmed(now=T2)
    assert sorted(sched._LIVE_UNARMED_PAGE_STATE) == ["live-unarmed-2026-11-11", "live-unarmed-2026-11-12"]
    assert len(sent_alerts) == 2


def test_paper_does_nothing(mode_env, sent_alerts, caplog):
    mode_env("PAPER", None, False)
    caplog.set_level(logging.INFO)
    assert sched._page_if_live_unarmed(now=T1) is False
    assert sent_alerts == []
    assert sched._LIVE_UNARMED_PAGE_STATE == {}
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_armed_does_nothing(armed, sent_alerts):
    assert sched._page_if_live_unarmed(now=T1) is False
    assert sent_alerts == []


def test_paper_does_not_import_the_alert_module(mode_env, monkeypatch):
    """Under PAPER nothing beyond live_config is needed: poison the alert module's
    page_once so any use would fail the test."""
    import scripts.openclaw_alerts as oa

    mode_env("PAPER", None, False)
    monkeypatch.setattr(oa, "page_once", lambda *a, **k: pytest.fail("page_once used under PAPER"))
    assert sched._page_if_live_unarmed(now=T1) is False


def test_a_failing_page_never_escapes_and_is_logged(mode_env, monkeypatch, caplog):
    import scripts.openclaw_alerts as oa

    def boom(*a, **k):
        raise RuntimeError("telegram is down")

    mode_env("LIVE", None, False)
    monkeypatch.setattr(oa, "page_once", boom)
    caplog.set_level(logging.INFO)
    assert sched._page_if_live_unarmed(now=T1) is True  # the state is still unarmed
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("LIVE but not armed" in m for m in msgs)  # the state line
    assert any("telegram is down" in m for m in msgs)    # the paging failure


def test_an_undelivered_page_is_retried_on_the_next_call(mode_env, monkeypatch, sent_alerts):
    """page_once stamps only on delivery: a failed send does not use up the day."""
    import scripts.openclaw_alerts as oa

    mode_env("LIVE", None, False)
    real = oa.alert_openclaw
    monkeypatch.setattr(oa, "alert_openclaw", lambda *a, **k: False)
    sched._page_if_live_unarmed(now=T1)
    assert sched._LIVE_UNARMED_PAGE_STATE == {}
    monkeypatch.setattr(oa, "alert_openclaw", real)
    sched._page_if_live_unarmed(now=T1 + 5)
    assert len(sent_alerts) == 1


def test_main_calls_it_right_after_the_startup_check():
    tree = ast.parse(Path(sched.__file__).read_text())
    main = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "main")
    calls = [
        n.value.func.id
        for n in main.body
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name)
    ]
    i = calls.index("_assert_live_cap_fits_min_action")
    assert calls[i + 1] == "_page_if_live_unarmed"

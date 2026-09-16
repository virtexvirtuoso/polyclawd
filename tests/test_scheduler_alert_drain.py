"""Task 5.2 — alert-dispatch drain wired into the scheduler as an UNGATED
5-min task (gate counters reset on the 15-min restart and would starve it)."""
import services.scheduler as sched


def test_alert_drain_registered_ungated_5min():
    assert "alert_drain" in sched.TICK_TASKS["5min"]
    assert "alert_drain" not in sched.TICK_TASKS["5min_gated"]


def test_alert_drain_task_resolves_and_calls_drain(monkeypatch):
    assert callable(sched._task_fn("alert_drain"))
    calls = []
    monkeypatch.setattr("signals.alert_dispatch.drain",
                        lambda *a, **kw: calls.append(1) or 0)
    sched.task_alert_drain()
    assert calls == [1]

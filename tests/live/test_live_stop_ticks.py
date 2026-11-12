"""Task 1.2b: the scheduler side of the live stop (the "stop shadow").

`run_live_stop_tick` runs `evaluate_live_stops` once per tick, as a dry run unless
the bot is armed, and logs one summary line. It also keeps the unpriced-position
alarm (a state file) and pages through `page_once`. Nothing here sells: the exit
function is a recorder, notifier calls land in the `sent_alerts` capture, and every
test passes `now=` explicitly.
"""

import ast
import json
import logging
import os
import sqlite3
import threading
from pathlib import Path

import pytest

from odds.polymarket_clob import OrderBook, OrderBookLevel
from services import live_stop_shadow as shadow
from services import stop_evaluator

T0 = 1_800_000_000.0
MIN = 60.0


def make_book(token_id, bids, asks):
    bl = [OrderBookLevel(price=p, size=100.0) for p in bids]
    al = [OrderBookLevel(price=p, size=100.0) for p in asks]
    best_bid = bl[0].price if bl else 0
    best_ask = al[0].price if al else 1
    return OrderBook(
        market_id="m", token_id=token_id, outcome="Yes", bids=bl, asks=al,
        spread=round(best_ask - best_bid, 4),
        mid_price=round((best_bid + best_ask) / 2, 4) if bl and al else 0.5,
        timestamp="2026-11-11T00:00:00",
    )


class World:
    def __init__(self):
        self.books = {}
        self.exit_calls = []
        self.exit_result = "default"

    def fake_get_orderbook(self, token_id):
        res = self.books.get(token_id)
        if isinstance(res, Exception):
            raise res
        return res

    def fake_exit(self, row, mark, reason, hard_cap_frac=0.50):
        self.exit_calls.append((dict(row), mark, reason, hard_cap_frac))
        if self.exit_result == "default":
            return {"id": row["id"], "action": "closed", "pnl": -1.0}
        return self.exit_result


@pytest.fixture
def world(monkeypatch):
    w = World()
    monkeypatch.setattr("odds.polymarket_clob.get_orderbook", w.fake_get_orderbook)
    monkeypatch.setattr(stop_evaluator, "_close_live_position_early", w.fake_exit)
    monkeypatch.delenv("POLYCLAWD_STOP_EXEMPT_IDS", raising=False)
    return w


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    """A fresh paging state per test, and the tick lock free."""
    monkeypatch.setattr(shadow, "_PAGE_STATE", {})
    monkeypatch.setattr(shadow, "_TICK_LOCK", threading.Lock())


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "live_stop_state.json"


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.INFO, logger="live_stops")

    def get(level=None):
        return [r.getMessage() for r in caplog.records
                if r.name == "live_stops" and (level is None or r.levelno == level)]
    return get


def add_position(db, entry, *, status="open"):
    cur = db.conn.execute(
        "INSERT INTO live_positions (market_id, token_id, side, entry_price, shares,"
        " cost_usd, status) VALUES ('mkt', 'pending', 'YES', ?, 10.0, 1.0, ?)",
        (entry, status),
    )
    pid = cur.lastrowid
    db.conn.execute("UPDATE live_positions SET token_id=? WHERE id=?", (f"tok-{pid}", pid))
    db.conn.commit()
    return pid


def book(world, pid, bids, asks):
    world.books[f"tok-{pid}"] = make_book(f"tok-{pid}", bids, asks)


def tick(db, state_path, now=T0, source="stop_evaluator"):
    # evaluate_live_stops reads the temp database through POLYCLAWD_LIVE_DB (live_db fixture)
    return shadow.run_live_stop_tick(source, now=now, state_path=state_path)


def pages(sent_alerts):
    return [a for a in sent_alerts if a["fn"] == "alert_openclaw"]


def page_texts(sent_alerts):
    return [a["args"][0] if a["args"] else a["kwargs"].get("message") for a in pages(sent_alerts)]


# ---------------------------------------------------------------------------
# The summary line
# ---------------------------------------------------------------------------

def test_dryrun_line_paper_all_actions(live_db, world, state_path, logs, monkeypatch):
    p_ok = add_position(live_db, 0.50)
    p_exit = add_position(live_db, 0.24)
    p_held = add_position(live_db, 0.20)
    p_unp = add_position(live_db, 0.50)
    p_exempt = add_position(live_db, 0.24)
    p_bad = add_position(live_db, 0)
    book(world, p_ok, [0.50], [0.52])
    book(world, p_exit, [0.08], [0.09])
    book(world, p_held, [0.11], [0.13])
    book(world, p_unp, [0.30], [])
    book(world, p_exempt, [0.08], [0.09])
    monkeypatch.setenv("POLYCLAWD_STOP_EXEMPT_IDS", str(p_exempt))
    out = tick(live_db, state_path)
    assert out.get("failed") is not True and out.get("skipped") is not True
    summary = [m for m in logs() if m.startswith("live_stops DRYRUN")]
    assert summary == [
        f"live_stops DRYRUN mode=PAPER armed=false source=stop_evaluator checked=6 ok=1 "
        f"would_exit=[{p_exit}] spread_held=[{p_held}] unpriced=[{p_unp}] "
        f"exempt=[{p_exempt}] errors=[{p_bad}]"
    ]
    detail = [m for m in logs() if m.startswith("live_stops DETAIL")]
    assert detail == [
        f"live_stops DETAIL id={p_exit} action=would_exit mark=0.08 loss_pct=0.6667 "
        f"spread_pct=0.0417 threshold=0.4417",
        f"live_stops DETAIL id={p_held} action=spread_held mark=0.11 loss_pct=0.45 "
        f"spread_pct=0.1 threshold=0.5",
    ]
    assert world.exit_calls == []


def test_dryrun_line_with_no_positions(live_db, world, state_path, logs):
    tick(live_db, state_path)
    assert logs() == [
        "live_stops DRYRUN mode=PAPER armed=false source=stop_evaluator checked=0 ok=0 "
        "would_exit=[] spread_held=[] unpriced=[] exempt=[] errors=[]"
    ]


def test_live_but_unarmed_is_a_dry_run(live_db, world, state_path, logs, mode_env):
    mode_env("LIVE", None, False)
    pid = add_position(live_db, 0.24)
    book(world, pid, [0.08], [0.09])
    tick(live_db, state_path, source="stop_evaluator_urgent")
    assert logs()[0] == (
        f"live_stops DRYRUN mode=LIVE armed=false source=stop_evaluator_urgent checked=1 ok=0 "
        f"would_exit=[{pid}] spread_held=[] unpriced=[] exempt=[] errors=[]"
    )
    assert world.exit_calls == []


def test_armed_line_lists_exited(live_db, world, state_path, logs, armed):
    pid = add_position(live_db, 0.24)
    book(world, pid, [0.08], [0.09])
    tick(live_db, state_path)
    assert len(world.exit_calls) == 1
    summary = [m for m in logs() if m.startswith("live_stops ")]
    assert summary == [
        f"live_stops ARMED source=stop_evaluator checked=1 ok=0 exited=[{pid}] "
        f"spread_held=[] unpriced=[] exempt=[] errors=[]"
    ]
    assert not [m for m in logs() if "DRYRUN" in m]


@pytest.mark.parametrize("is_armed", [False, True])
def test_dry_run_flag_follows_arming(monkeypatch, state_path, request, is_armed):
    if is_armed:
        request.getfixturevalue("armed")
    seen = []

    def spy(*, dry_run=False, db_path=None):
        seen.append(dry_run)
        return []

    monkeypatch.setattr(stop_evaluator, "evaluate_live_stops", spy)
    shadow.run_live_stop_tick("stop_evaluator", now=T0, state_path=state_path)
    assert seen == [not is_armed]


def test_exempt_ids_in_every_summary_line(live_db, world, state_path, logs, monkeypatch):
    pid = add_position(live_db, 0.24)
    book(world, pid, [0.08], [0.09])
    monkeypatch.setenv("POLYCLAWD_STOP_EXEMPT_IDS", str(pid))
    tick(live_db, state_path, now=T0)
    tick(live_db, state_path, now=T0 + 60)
    lines = [m for m in logs() if m.startswith("live_stops DRYRUN")]
    assert len(lines) == 2 and all(f"exempt=[{pid}]" in m for m in lines)
    assert all("would_exit=[]" in m for m in lines)


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------

def test_overlapping_ticks_second_is_skipped_and_exit_called_once(live_db, world, state_path,
                                                                  logs, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow_eval(*, dry_run=False, db_path=None):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            release.wait(timeout=5)
            world.fake_exit({"id": 1}, 0.08, "x")
        return []

    monkeypatch.setattr(stop_evaluator, "evaluate_live_stops", slow_eval)
    results = []
    t = threading.Thread(
        target=lambda: results.append(shadow.run_live_stop_tick("stop_evaluator", now=T0,
                                                                state_path=state_path)))
    t.start()
    assert entered.wait(timeout=5)
    second = shadow.run_live_stop_tick("stop_evaluator_urgent", now=T0 + 1, state_path=state_path)
    assert second == {"skipped": True}
    assert "live_stops SKIPPED reason=lock source=stop_evaluator_urgent" in logs()
    release.set()
    t.join(timeout=5)
    assert len(world.exit_calls) == 1 and len(calls) == 1
    third = shadow.run_live_stop_tick("stop_evaluator", now=T0 + 2, state_path=state_path)
    assert third.get("skipped") is not True and len(calls) == 2


def test_lock_released_when_evaluation_raises(monkeypatch, state_path):
    def boom(*, dry_run=False, db_path=None):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(stop_evaluator, "evaluate_live_stops", boom)
    assert shadow.run_live_stop_tick("stop_evaluator", now=T0, state_path=state_path) == {"failed": True}
    assert shadow._TICK_LOCK.acquire(blocking=False)
    shadow._TICK_LOCK.release()


# ---------------------------------------------------------------------------
# The unpriced alarm
# ---------------------------------------------------------------------------

def read_state(path):
    return json.loads(path.read_text())


def test_unpriced_state_and_page_timing(live_db, world, state_path, sent_alerts):
    pid = add_position(live_db, 0.50)
    book(world, pid, [0.30], [])
    tick(live_db, state_path, now=T0)
    assert read_state(state_path) == {"unpriced_since": {str(pid): T0}}
    tick(live_db, state_path, now=T0 + 29 * MIN)
    assert pages(sent_alerts) == []
    tick(live_db, state_path, now=T0 + 31 * MIN)
    assert page_texts(sent_alerts) == [
        f"Polyclawd live stop: position {pid} has had no usable two-sided price for 31 "
        f"minutes. The stop cannot evaluate it. (shadow)"
    ]
    tick(live_db, state_path, now=T0 + 40 * MIN)
    assert len(pages(sent_alerts)) == 1                     # inside the 6 hour cooldown
    assert read_state(state_path) == {"unpriced_since": {str(pid): T0}}


def test_priced_again_clears_and_unpriced_again_restarts(live_db, world, state_path, sent_alerts):
    pid = add_position(live_db, 0.50)
    book(world, pid, [0.30], [])
    tick(live_db, state_path, now=T0)
    book(world, pid, [0.50], [0.52])
    tick(live_db, state_path, now=T0 + 10 * MIN)
    assert read_state(state_path) == {"unpriced_since": {}}
    book(world, pid, [], [])
    tick(live_db, state_path, now=T0 + 20 * MIN)
    assert read_state(state_path) == {"unpriced_since": {str(pid): T0 + 20 * MIN}}
    tick(live_db, state_path, now=T0 + 20 * MIN + 29 * MIN)
    assert pages(sent_alerts) == []


def test_position_closed_while_unpriced_is_removed(live_db, world, state_path):
    pid = add_position(live_db, 0.50)
    book(world, pid, [0.30], [])
    tick(live_db, state_path, now=T0)
    live_db.conn.execute("UPDATE live_positions SET status='closed' WHERE id=?", (pid,))
    live_db.conn.commit()
    tick(live_db, state_path, now=T0 + 60)
    assert read_state(state_path) == {"unpriced_since": {}}


def test_corrupt_state_is_empty_warned_once_then_rewritten(live_db, world, state_path, logs):
    pid = add_position(live_db, 0.50)
    book(world, pid, [0.30], [])
    state_path.write_text("{not json")
    tick(live_db, state_path, now=T0)
    warnings = [m for m in logs(logging.WARNING) if "state" in m]
    assert len(warnings) == 1
    assert read_state(state_path) == {"unpriced_since": {str(pid): T0}}
    tick(live_db, state_path, now=T0 + 60)
    assert len([m for m in logs(logging.WARNING) if "state" in m]) == 1


def test_state_file_not_rewritten_when_unchanged_and_write_is_atomic(live_db, world, state_path,
                                                                      monkeypatch, tmp_path):
    replaced = []
    real_replace = os.replace

    def counting_replace(src, dst):
        assert Path(src).parent == Path(dst).parent       # temp file in the same directory
        replaced.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(shadow.os, "replace", counting_replace)
    pid = add_position(live_db, 0.50)
    book(world, pid, [0.30], [])
    tick(live_db, state_path, now=T0)
    assert len(replaced) == 1 and replaced[0][1] == str(state_path)
    tick(live_db, state_path, now=T0 + 60)
    tick(live_db, state_path, now=T0 + 120)
    assert len(replaced) == 1
    assert sorted(p.name for p in tmp_path.iterdir()
                  if p.name.startswith("live_stop_state")) == ["live_stop_state.json"]
    assert read_state(state_path) == {"unpriced_since": {str(pid): T0}}
    # no priced position and nothing in the map: still no write
    book(world, pid, [0.50], [0.52])
    tick(live_db, state_path, now=T0 + 180)
    assert len(replaced) == 2
    tick(live_db, state_path, now=T0 + 240)
    assert len(replaced) == 2


# ---------------------------------------------------------------------------
# Immediate pages: bad_row, error, exit_failed
# ---------------------------------------------------------------------------

def test_bad_row_page_once_with_cooldown_and_shadow_tag(live_db, world, state_path, sent_alerts):
    pid = add_position(live_db, 0)
    tick(live_db, state_path, now=T0)
    assert page_texts(sent_alerts) == [
        f"Polyclawd live stop: position {pid} is bad_row and the stop could not evaluate it. (shadow)"
    ]
    tick(live_db, state_path, now=T0 + 60)
    assert len(pages(sent_alerts)) == 1
    tick(live_db, state_path, now=T0 + 61 * MIN)             # cooldown is 1 hour
    assert len(pages(sent_alerts)) == 2


def test_error_page_truncates_text_and_keeps_key(live_db, world, state_path, sent_alerts):
    pid = add_position(live_db, 0.50)
    world.books[f"tok-{pid}"] = RuntimeError("x" * 500)
    tick(live_db, state_path, now=T0)
    (text,) = page_texts(sent_alerts)
    assert f"position {pid}" in text and "no sale was attempted" in text and "(shadow)" in text
    shown = text[text.index("(") + 1:text.index(")")].split("(")[-1]
    assert len(shown) == 200 and shown.startswith("RuntimeError: x")
    assert f"live-stop-error-{pid}" in shadow._PAGE_STATE
    tick(live_db, state_path, now=T0 + 60)
    assert len(pages(sent_alerts)) == 1


def test_exit_failed_page_says_sale_attempted_outcome_unknown(live_db, world, state_path,
                                                               sent_alerts, armed):
    pid = add_position(live_db, 0.24)
    book(world, pid, [0.08], [0.09])
    world.exit_result = None
    tick(live_db, state_path, now=T0)
    (text,) = page_texts(sent_alerts)
    assert text == (f"Polyclawd live stop: the exit of position {pid} did not confirm a sale. "
                    "Check the exchange and the ledger.")
    assert "(shadow)" not in text
    tick(live_db, state_path, now=T0 + 60)
    assert len(pages(sent_alerts)) == 1
    assert len(world.exit_calls) == 2                        # the stop keeps trying; the page does not repeat


def patched_eval(monkeypatch, entries):
    monkeypatch.setattr(stop_evaluator, "evaluate_live_stops",
                        lambda *, dry_run=False, db_path=None: entries)


def unknown_entry(pid=7, error="e" * 300):
    return {"id": pid, "action": "exit_unknown", "mark": 0.08, "loss_pct": 0.6667,
            "spread_pct": 0.0417, "threshold": 0.4417, "error": error}


def test_exit_unknown_is_an_error_not_exited_and_pages(monkeypatch, state_path, logs,
                                                       sent_alerts, armed):
    patched_eval(monkeypatch, [unknown_entry(), {"id": 8, "action": "closed", "pnl": -1.0}])
    shadow.run_live_stop_tick("stop_evaluator", now=T0, state_path=state_path)
    (line,) = [m for m in logs() if m.startswith("live_stops ARMED")]
    assert "exited=[8]" in line and "errors=[7]" in line
    (text,) = page_texts(sent_alerts)
    assert text == ("Polyclawd live stop: a sale of position 7 was attempted and its outcome is "
                    f"UNKNOWN ({'e' * 200}). Check the exchange and the ledger now.")
    assert "live-stop-exit_unknown-7" in shadow._PAGE_STATE
    shadow.run_live_stop_tick("stop_evaluator", now=T0 + 60, state_path=state_path)
    assert len(pages(sent_alerts)) == 1


def test_exit_unknown_in_a_dry_run_does_not_crash(monkeypatch, state_path, logs, sent_alerts):
    patched_eval(monkeypatch, [unknown_entry()])
    out = shadow.run_live_stop_tick("stop_evaluator", now=T0, state_path=state_path)
    assert out.get("failed") is not True
    (line,) = [m for m in logs() if m.startswith("live_stops DRYRUN")]
    assert "errors=[7]" in line and "would_exit=[]" in line
    assert len(pages(sent_alerts)) == 1


def test_unknown_future_action_counts_as_exited(monkeypatch, state_path, logs, armed):
    patched_eval(monkeypatch, [{"id": 3, "action": "held_remainder"}])
    shadow.run_live_stop_tick("stop_evaluator", now=T0, state_path=state_path)
    assert any("exited=[3]" in m for m in logs())


# ---------------------------------------------------------------------------
# Failure of the whole evaluation, and of paging
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exc", [sqlite3.OperationalError("database is locked"),
                                 ValueError("POLYCLAWD_STOP_EXEMPT_IDS='2x6': bad")])
def test_whole_evaluation_raising_logs_error_pages_and_returns(monkeypatch, state_path, logs,
                                                                sent_alerts, exc):
    def boom(*, dry_run=False, db_path=None):
        raise exc

    monkeypatch.setattr(stop_evaluator, "evaluate_live_stops", boom)
    assert shadow.run_live_stop_tick("stop_evaluator", now=T0, state_path=state_path) == {"failed": True}
    errors = logs(logging.ERROR)
    assert errors == [f"live_stops FAILED source=stop_evaluator error={type(exc).__name__}: {exc}"]
    assert len(pages(sent_alerts)) == 1 and "live stop" in page_texts(sent_alerts)[0]
    shadow.run_live_stop_tick("stop_evaluator", now=T0 + 60, state_path=state_path)
    assert len(pages(sent_alerts)) == 1                      # cooldown
    assert not state_path.exists()                           # a failed tick changes no alarm state


def test_paging_failure_is_logged_and_tick_returns(live_db, world, state_path, logs, monkeypatch):
    add_position(live_db, 0)

    def broken_page_once(*a, **k):
        raise RuntimeError("transport down")

    monkeypatch.setattr("scripts.openclaw_alerts.page_once", broken_page_once)
    out = tick(live_db, state_path)
    assert out.get("failed") is not True
    assert any("page" in m and "transport down" in m for m in logs(logging.ERROR))
    assert any(m.startswith("live_stops DRYRUN") for m in logs())


# ---------------------------------------------------------------------------
# Scheduler wiring and start-up validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("task,paper_fn", [
    ("task_stop_evaluator", "evaluate_stops"),
    ("task_stop_evaluator_urgent", "evaluate_stops_urgent"),
])
def test_task_runs_shadow_even_when_paper_pass_raises(monkeypatch, task, paper_fn):
    import services.scheduler as sched
    seen = []

    def paper_boom():
        raise RuntimeError("paper pass broke")

    monkeypatch.setattr(stop_evaluator, paper_fn, paper_boom)
    monkeypatch.setattr(shadow, "run_live_stop_tick", lambda source, **k: seen.append(source) or {})
    with pytest.raises(RuntimeError, match="paper pass broke"):      # _run_safe still sees and logs it
        getattr(sched, task)()
    assert seen == [task.removeprefix("task_")]


@pytest.mark.parametrize("task,paper_fn", [
    ("task_stop_evaluator", "evaluate_stops"),
    ("task_stop_evaluator_urgent", "evaluate_stops_urgent"),
])
def test_task_survives_shadow_raising(monkeypatch, caplog, task, paper_fn):
    import services.scheduler as sched
    paper_calls = []
    monkeypatch.setattr(stop_evaluator, paper_fn, lambda: paper_calls.append(1))

    def boom(source, **k):
        raise RuntimeError("shadow broke")

    monkeypatch.setattr(shadow, "run_live_stop_tick", boom)
    getattr(sched, task)()                                           # must not raise
    assert paper_calls == [1]
    assert any("shadow broke" in r.getMessage() or (r.exc_info and "shadow broke" in str(r.exc_info[1]))
               for r in caplog.records if r.name == "scheduler")


def test_startup_validation_valid_logs_ids(monkeypatch, caplog):
    import services.scheduler as sched
    caplog.set_level(logging.INFO, logger="scheduler")
    monkeypatch.setenv("POLYCLAWD_STOP_EXEMPT_IDS", "26, 7")
    sched._validate_stop_exempt_ids()
    assert any("[7, 26]" in r.getMessage() and r.levelno == logging.INFO for r in caplog.records)


def test_startup_validation_empty_is_fine(monkeypatch, caplog):
    import services.scheduler as sched
    caplog.set_level(logging.INFO, logger="scheduler")
    monkeypatch.delenv("POLYCLAWD_STOP_EXEMPT_IDS", raising=False)
    sched._validate_stop_exempt_ids()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_startup_validation_invalid_exits_nonzero(monkeypatch, caplog):
    import services.scheduler as sched
    monkeypatch.setenv("POLYCLAWD_STOP_EXEMPT_IDS", "2x6")
    with pytest.raises(SystemExit) as ei:
        sched._validate_stop_exempt_ids()
    assert ei.value.code not in (0, None)
    crit = [r.getMessage() for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(crit) == 1
    assert crit[0].startswith("POLYCLAWD_STOP_EXEMPT_IDS is invalid:") and "refusing to start" in crit[0]


def test_main_validates_before_the_task_loops_start():
    src = (Path(__file__).resolve().parents[2] / "services" / "scheduler.py").read_text()
    main = next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "main")
    calls = [(c.lineno, c.func.id) for c in ast.walk(main)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]
    validate = [ln for ln, name in calls if name == "_validate_stop_exempt_ids"]
    creates = [c.lineno for c in ast.walk(main)
               if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "create_task"
               and not (c.args and isinstance(c.args[0], ast.Call)
                        and isinstance(c.args[0].func, ast.Attribute)
                        and c.args[0].func.attr == "to_thread")]
    assert len(validate) == 1
    assert creates and validate[0] < min(creates)

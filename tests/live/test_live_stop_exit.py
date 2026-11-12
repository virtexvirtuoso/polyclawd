"""Task 1.2c-1: the live exit path, `_close_live_position_early` and `_fire_stop`.

The function is the one that sells a real position once the system is armed. What
these tests pin:

* one price: the mark handed in is the price everything uses; no order-book fetch
  and no re-mark (on a one-sided book the old code marked at the 0.5 mid);
* an honest result: a dict in every case; a failure BEFORE the sale is
  `exit_failed` (stage "pre"), an exception from the sale itself propagates (the
  caller makes it `exit_unknown`), a failure AFTER the sale is recorded as
  `post_error` and never replaces the result;
* one alert path (`_fire_stop`) for Discord and Telegram, and a partial close is
  never announced as a close (#17).

Where the real `execute_exit` is driven, it runs end to end against `fake_clob`
(`live_db` is the database, the order functions are the real ones behind the
arming guard). Where it is replaced by a recorder, the test says why.
"""

import re

import pytest
from loguru import logger

from execution import clob_client, live_executor
from odds.polymarket_clob import OrderBook, OrderBookLevel
from services import stop_evaluator

TOKEN = "tok-exit-1"
ENTRY = 0.24
MARK = 0.08                     # the best bid the stop decided on
SHARES = 10.0
COST = 2.4


def _insert_position(db, *, entry=ENTRY, shares=SHARES, cost=COST, title="Will it rain in X?"):
    cur = db.conn.execute(
        "INSERT INTO live_positions (market_id, market_title, token_id, side, entry_price,"
        " shares, cost_usd, status, archetype) VALUES ('mkt', ?, ?, 'YES', ?, ?, ?, 'open', 'weather')",
        (title, TOKEN, entry, shares, cost),
    )
    db.conn.commit()
    return cur.lastrowid


def _row(db, pid):
    return dict(db.conn.execute("SELECT * FROM live_positions WHERE id=?", (pid,)).fetchone())


def _one_sided_book():
    """What the old re-mark would have read: bids only, so mid_price is the 0.5 default."""
    return OrderBook(market_id="m", token_id=TOKEN, outcome="Yes",
                     bids=[OrderBookLevel(price=MARK, size=50.0)], asks=[],
                     spread=0.0, mid_price=0.5, timestamp="2026-11-11T00:00:00")


@pytest.fixture
def env(live_db, fixed_tick_and_min_size, monkeypatch):
    """Everything the function touches that is not the exchange: no sleeping in the
    executor, no title/outcome lookups, no cooldown-file write, and get_orderbook is
    a tripwire that records any call (the function must never fetch a book)."""
    class Env:
        book_calls = []
        cooldowns = []
    Env.book_calls = []
    Env.cooldowns = []

    def get_orderbook(token_id):
        Env.book_calls.append(token_id)
        raise AssertionError("get_orderbook must not be called by the exit path")

    monkeypatch.setattr("odds.polymarket_clob.get_orderbook", get_orderbook)
    monkeypatch.setattr(live_executor, "_wait_for_maker_fill", lambda oid, timeout: True)
    monkeypatch.setattr(live_executor, "_FILL_CONFIRM_SLEEP", 0.0)
    monkeypatch.setattr(stop_evaluator, "_side_label", lambda token_id: "YES")
    monkeypatch.setattr("scripts.smart_wallet_fast_poll.register_exit_cooldown",
                        lambda token_id: Env.cooldowns.append(token_id))
    return Env


@pytest.fixture
def log_records():
    """loguru records as (level, message); caplog does not see loguru."""
    recs = []
    hid = logger.add(lambda m: recs.append((m.record["level"].name, m.record["message"])),
                     level="DEBUG")
    yield recs
    logger.remove(hid)


def _call(row, mark=MARK, reason="live stop 67%", cap=0.40):
    return stop_evaluator._close_live_position_early(row, mark, reason, hard_cap_frac=cap)


def _alerts(sent_alerts):
    discord = [c for c in sent_alerts if c["fn"] == "discord_alerts._send"]
    telegram = [c for c in sent_alerts if c["fn"] == "alert_openclaw"]
    return discord, telegram


def _discord_text(call):
    return call["args"][0][0]["description"]


def _full_maker_fill(fake_clob, shapes, *, matched="10"):
    fake_clob.script("place_limit_order", shapes.accepted_post("oid-m"))
    fake_clob.script("get_order", shapes.open_order(
        "oid-m", "MATCHED" if matched == "10" else "LIVE", side="SELL", price="0.08",
        original_size="10", size_matched=matched))


# ---------------------------------------------------------------------------
# C1: one price
# ---------------------------------------------------------------------------

def test_mark_handed_in_is_the_price_the_order_and_the_alert_use(
        live_db, fake_clob, shapes, env, armed, sent_alerts):
    pid = _insert_position(live_db)
    _full_maker_fill(fake_clob, shapes)
    out = _call(_row(live_db, pid))
    (args, kwargs), = fake_clob.calls_to("place_limit_order")
    assert kwargs["price"] == MARK and kwargs["side"] == "SELL" and kwargs["size"] == SHARES
    assert out["action"] == "maker_closed"
    assert out["current_price"] == MARK and out["exit_price"] == MARK
    discord, telegram = _alerts(sent_alerts)
    assert "0.0800" in _discord_text(discord[0]) and "0.0800" in telegram[0]["args"][0]
    assert env.book_calls == []


def test_no_order_book_fetch_and_nothing_marked_at_the_half(
        live_db, fake_clob, shapes, env, armed, monkeypatch):
    """A one-sided book used to give mid_price 0.5, and the exit was priced at 0.5."""
    pid = _insert_position(live_db)

    def one_sided(token_id):
        env.book_calls.append(token_id)
        return _one_sided_book()

    monkeypatch.setattr("odds.polymarket_clob.get_orderbook", one_sided)
    _full_maker_fill(fake_clob, shapes)
    out = _call(_row(live_db, pid))
    (args, kwargs), = fake_clob.calls_to("place_limit_order")
    assert kwargs["price"] == MARK != 0.5
    assert out["current_price"] == MARK
    assert env.book_calls == []                    # not even looked at


def test_exit_request_carries_the_given_mark(live_db, env, monkeypatch):
    """Recorder in place of execute_exit: this test is about what is passed ON."""
    pid = _insert_position(live_db)
    seen = {}

    def rec(conn, gov, **kw):
        seen.update(kw)
        return {"action": "maker_closed", "shares_sold": SHARES, "exit_price": kw["mark_price"],
                "fee_paid": 0.0, "pnl": -1.6, "reason": kw["reason"]}

    monkeypatch.setattr(live_executor, "execute_exit", rec)
    _call(_row(live_db, pid), cap=0.4)
    assert seen["mark_price"] == MARK and seen["hard_cap_frac"] == 0.4
    assert seen["position_row"]["id"] == pid and seen["reason"] == "live stop 67%"
    assert env.book_calls == []


# ---------------------------------------------------------------------------
# C2: an honest result
# ---------------------------------------------------------------------------

def test_done_sale_reports_what_the_executor_reported_and_alerts_both_channels(
        live_db, fake_clob, shapes, env, armed, sent_alerts):
    pid = _insert_position(live_db)
    _full_maker_fill(fake_clob, shapes)
    out = _call(_row(live_db, pid))
    assert out["action"] == "maker_closed" and out["live"] is True
    assert out["shares_sold"] == SHARES and out["position_id"] == pid
    assert out["pnl"] == pytest.approx(SHARES * (MARK - ENTRY))
    assert "post_error" not in out and "stage" not in out
    assert _row(live_db, pid)["status"] == "closed"
    discord, telegram = _alerts(sent_alerts)
    assert len(discord) == 1 and len(telegram) == 1
    assert "closed" in _discord_text(discord[0]).lower() and "closed" in telegram[0]["args"][0].lower()
    assert env.cooldowns == [TOKEN]


def test_the_executor_raising_propagates_out_of_the_function(live_db, env, monkeypatch, sent_alerts):
    pid = _insert_position(live_db)

    def boom(conn, gov, **kw):
        raise RuntimeError("exchange went away mid-sale")

    monkeypatch.setattr(live_executor, "execute_exit", boom)
    with pytest.raises(RuntimeError, match="exchange went away mid-sale"):
        _call(_row(live_db, pid))
    assert sent_alerts == []                       # nothing announced


def test_the_executor_raising_becomes_exit_unknown_through_evaluate_live_stops(
        live_db, env, armed, monkeypatch, sent_alerts):
    """Through the real caller: the position's figures stay in the entry."""
    pid = _insert_position(live_db, entry=0.50)
    book = OrderBook(market_id="m", token_id=TOKEN, outcome="Yes",
                     bids=[OrderBookLevel(price=0.20, size=50.0)],
                     asks=[OrderBookLevel(price=0.21, size=50.0)],
                     spread=0.01, mid_price=0.205, timestamp="2026-11-11T00:00:00")
    monkeypatch.setattr("odds.polymarket_clob.get_orderbook", lambda t: book)

    def boom(conn, gov, **kw):
        raise ConnectionError("reset by peer")

    monkeypatch.setattr(live_executor, "execute_exit", boom)
    out = stop_evaluator.evaluate_live_stops(db_path=live_db.path)
    assert len(out) == 1 and out[0]["action"] == "exit_unknown" and out[0]["id"] == pid
    assert out[0]["error"] == "ConnectionError: reset by peer"
    assert out[0]["mark"] == 0.20 and out[0]["loss_pct"] == 0.6


# =============================================================================
# DEFECT PINNED ON PURPOSE (1B-fix-3 F6). This test does not describe wanted
# behaviour. It records what the UNCHANGED executor does today: execute_exit posts
# a POST-ONLY sell at the mark, the mark is the best bid, the exchange rejects it
# as "would cross", and an armed stop therefore sells NOTHING.
# Phase 1C (task 1.7) must make an exit at or over the hard cap skip the maker leg
# and cross immediately. When it does, INVERT this test: assert the taker is
# called (place_market_order) and the position closes. Do not "fix" it by loosening.
# =============================================================================
@pytest.mark.flip_in_phase_1c(
    reason="pins an executor defect: a post-only SELL at the best bid is rejected, so an "
           "armed stop sells nothing; Phase 1C task 1.7 must cross taker at/over the hard "
           "cap, then invert this test")
def test_a_real_post_only_rejection_at_the_bid_propagates_and_nothing_is_sold(
        live_db, fake_clob, shapes, env, armed, sent_alerts):
    """CHARACTERIZATION of what happens today, not a wish: execute_exit posts a
    POST-ONLY sell at the mark, and the mark is now the best bid, so the exchange
    answers would-cross and post_maker raises ClobError. The exception leaves the
    function (the caller labels it exit_unknown); the taker leg is never reached."""
    pid = _insert_position(live_db)
    fake_clob.script("place_limit_order", shapes.rejected_would_cross())
    with pytest.raises(clob_client.ClobError, match="post_only_would_cross"):
        _call(_row(live_db, pid))
    assert fake_clob.calls_to("place_market_order") == []
    assert _row(live_db, pid)["status"] == "open" and _row(live_db, pid)["shares"] == SHARES
    assert sent_alerts == []


def test_a_failure_before_the_sale_is_exit_failed_stage_pre_and_the_executor_is_never_called(
        live_db, env, monkeypatch, sent_alerts):
    pid = _insert_position(live_db)
    called = []
    monkeypatch.setattr(live_executor, "execute_exit", lambda *a, **k: called.append(1))

    def boom(conn, mode=None):
        raise ValueError("governor state is corrupt")

    monkeypatch.setattr("execution.risk_governor.RiskGovernor", boom)
    out = _call(_row(live_db, pid))
    assert out["action"] == "exit_failed" and out["stage"] == "pre"
    assert out["error"] == "ValueError: governor state is corrupt"
    assert called == [] and sent_alerts == []


@pytest.mark.parametrize("bad", [0, 0.0, -0.1, float("nan"), None, "0.08", 1.5, True],
                         ids=["zero", "zerof", "neg", "nan", "none", "str", "over1", "bool"])
def test_an_unusable_mark_is_a_pre_stage_failure(live_db, env, monkeypatch, bad):
    pid = _insert_position(live_db)
    called = []
    monkeypatch.setattr(live_executor, "execute_exit", lambda *a, **k: called.append(1))
    out = _call(_row(live_db, pid), mark=bad)
    assert out["action"] == "exit_failed" and out["stage"] == "pre" and "mark" in out["error"]
    assert called == []


def test_a_post_sale_alert_failure_never_replaces_the_result(
        live_db, fake_clob, shapes, env, armed, monkeypatch, log_records):
    pid = _insert_position(live_db)
    _full_maker_fill(fake_clob, shapes)

    def down(*a, **k):
        raise OSError("webhook unreachable")

    monkeypatch.setattr("signals.discord_alerts._send", down)
    monkeypatch.setattr("scripts.openclaw_alerts.alert_openclaw", down)
    out = _call(_row(live_db, pid))
    assert out["action"] == "maker_closed" and out["shares_sold"] == SHARES   # the sale stands
    assert "OSError: webhook unreachable" in out["post_error"]
    assert _row(live_db, pid)["status"] == "closed"
    errors = [m for lvl, m in log_records if lvl == "ERROR"]
    assert any("webhook unreachable" in m for m in errors)


def test_post_error_is_one_string_with_the_type_and_message(live_db, env, monkeypatch):
    pid = _insert_position(live_db)
    monkeypatch.setattr(live_executor, "execute_exit", lambda conn, gov, **kw: {
        "action": "maker_closed", "shares_sold": SHARES, "exit_price": MARK, "pnl": -1.6})
    monkeypatch.setattr(stop_evaluator, "_fire_stop",
                        lambda *a, **k: (_ for _ in ()).throw(KeyError("title")))
    out = _call(_row(live_db, pid))
    assert out["action"] == "maker_closed" and out["post_error"] == "KeyError: 'title'"


@pytest.mark.parametrize("raw", [{"pnl": -1.0}, {"action": None}, {"action": ""}, {"action": 7},
                                 None, "closed", ["maker_closed"], {"action": "sold-ish"}],
                         ids=["noaction", "none", "empty", "int", "nonedict", "str", "list", "unknown"])
def test_an_executor_result_without_a_usable_action_is_exit_unknown_with_raw(
        live_db, env, monkeypatch, sent_alerts, raw):
    pid = _insert_position(live_db)
    monkeypatch.setattr(live_executor, "execute_exit", lambda conn, gov, **kw: raw)
    out = _call(_row(live_db, pid))
    assert out["action"] == "exit_unknown"
    assert out["raw"] == repr(raw)[:200]
    assert "action" in out["error"]
    assert sent_alerts == []                       # a sale of unknown outcome is not announced here


def test_raw_is_capped_at_200_characters(live_db, env, monkeypatch):
    pid = _insert_position(live_db)
    monkeypatch.setattr(live_executor, "execute_exit",
                        lambda conn, gov, **kw: {"pnl": "x" * 900})
    out = _call(_row(live_db, pid))
    assert out["action"] == "exit_unknown" and len(out["raw"]) == 200


def test_the_result_is_a_dict_with_an_action_in_every_case(live_db, env, monkeypatch):
    pid = _insert_position(live_db)
    for raw in ({"action": "held_remainder", "shares_sold": 0.0}, None, {}, {"action": "taker_closed",
                "shares_sold": SHARES, "exit_price": 0.07}):
        monkeypatch.setattr(live_executor, "execute_exit", lambda conn, gov, _r=raw, **kw: _r)
        out = _call(_row(live_db, pid))
        assert isinstance(out, dict) and isinstance(out["action"], str) and out["action"]


def test_keys_the_other_callers_read_are_kept(live_db, env, monkeypatch):
    """The paper stop loops feed this result to _send_discord_alert and
    _send_stop_close_telegram, which read these keys."""
    pid = _insert_position(live_db)
    monkeypatch.setattr(live_executor, "execute_exit", lambda conn, gov, **kw: {
        "action": "taker_closed", "shares_sold": SHARES, "exit_price": 0.07, "pnl": -1.7})
    out = _call(_row(live_db, pid))
    for key in ("position_id", "market_title", "side", "outcome", "entry_price", "current_price",
                "pnl", "bet_size", "reason", "strategy", "live", "action"):
        assert key in out, key
    assert out["market_title"] == "Will it rain in X?" and out["bet_size"] == COST
    assert out["strategy"] == "weather" and out["outcome"] == "YES"


def test_a_direct_call_under_paper_ends_in_the_arming_guard_and_sends_nothing(
        live_db, fake_clob, env, sent_alerts):
    """No `armed` fixture: mode is PAPER. post_maker's first statement refuses, before
    any exchange call. The guard's ClobError leaves the function (it is raised by the
    sale call), so evaluate_live_stops would label it exit_unknown; it is never
    reached from there unarmed (tests/live/test_live_stops.py)."""
    pid = _insert_position(live_db)
    with pytest.raises(clob_client.ClobError, match="not armed"):
        _call(_row(live_db, pid))
    fake_clob.assert_no_calls()
    assert sent_alerts == []
    assert _row(live_db, pid)["status"] == "open"


# ---------------------------------------------------------------------------
# C3: one alert path, and no false "closed" (#17)
# ---------------------------------------------------------------------------

ROW = {"id": 3, "market_id": "mkt", "market_title": "Will it rain in X?", "token_id": TOKEN,
       "entry_price": ENTRY, "shares": SHARES, "cost_usd": COST}

BARE_CLOSED = re.compile(r"(?<!partly )(?<!fully )(?<!not )closed", re.IGNORECASE)


def _result(action, sold, price=MARK, **extra):
    return {"action": action, "shares_sold": sold, "exit_price": price, "fee_paid": 0.0,
            "pnl": round(sold * (price - ENTRY), 4), **extra}


@pytest.mark.parametrize("action", ["maker_closed", "taker_closed"])
def test_full_close_alerts_each_channel_once_with_the_same_text(sent_alerts, action):
    fails = stop_evaluator._fire_stop(ROW, _result(action, SHARES, 0.07), MARK, "live stop 67%")
    assert fails == []
    discord, telegram = _alerts(sent_alerts)
    assert len(discord) == 1 and len(telegram) == 1 and len(sent_alerts) == 2
    text = telegram[0]["args"][0]
    assert _discord_text(discord[0]) == text
    assert "CLOSED" in text and "PARTLY" not in text and "NOT" not in text
    assert "10.00" in text and "0.0700" in text and "0.0800" in text     # shares, exit price, the mark
    assert "Will it rain in X?" in text and "live stop 67%" in text


def test_held_remainder_is_partly_closed_and_names_what_remains(sent_alerts):
    res = _result("held_remainder", 4.0)
    assert stop_evaluator._fire_stop(ROW, res, MARK, "live stop 67%") == []
    discord, telegram = _alerts(sent_alerts)
    assert len(discord) == 1 and len(telegram) == 1
    for text in (_discord_text(discord[0]), telegram[0]["args"][0]):
        assert "PARTLY CLOSED" in text and "not fully closed" in text.lower()
        assert "6.00" in text                      # what remains
        assert "4.00" in text                      # what was sold
        assert BARE_CLOSED.search(text) is None, text


def test_held_remainder_with_nothing_sold_says_no_shares_were_sold(sent_alerts):
    res = _result("held_remainder", 0.0, reason="below vendor minimum")
    stop_evaluator._fire_stop(ROW, res, MARK, "live stop 67%")
    discord, telegram = _alerts(sent_alerts)
    text = telegram[0]["args"][0]
    assert "10.00" in text and "NOT SOLD" in text.upper() and "closed" not in text.lower()


def test_partial_closed_with_shares_left_is_partly_closed(sent_alerts):
    """A taker leg can fill less than the remainder: the executor still names it
    partial_closed, and fewer shares sold than held is partly closed."""
    stop_evaluator._fire_stop(ROW, _result("partial_closed", 7.5), MARK, "r")
    text = _alerts(sent_alerts)[1][0]["args"][0]
    assert "PARTLY CLOSED" in text and "2.50" in text and BARE_CLOSED.search(text) is None


def test_a_closed_action_with_fewer_shares_sold_than_held_is_not_called_closed(sent_alerts):
    stop_evaluator._fire_stop(ROW, _result("maker_closed", 3.0), MARK, "r")
    text = _alerts(sent_alerts)[1][0]["args"][0]
    assert "PARTLY CLOSED" in text and "7.00" in text and BARE_CLOSED.search(text) is None


@pytest.mark.parametrize("action", ["exit_unknown", "exit_failed", "error", "sold-ish"])
def test_unknown_and_failed_results_send_no_close_alert(sent_alerts, action):
    assert stop_evaluator._fire_stop(ROW, {"action": action, "shares_sold": SHARES}, MARK, "r") == []
    assert sent_alerts == []


def test_one_channel_failing_does_not_stop_the_other(monkeypatch, sent_alerts, log_records):
    def down(*a, **k):
        raise OSError("discord 500")

    monkeypatch.setattr("signals.discord_alerts._send", down)
    fails = stop_evaluator._fire_stop(ROW, _result("maker_closed", SHARES), MARK, "r")
    assert len(fails) == 1 and "discord 500" in fails[0]
    assert len([c for c in sent_alerts if c["fn"] == "alert_openclaw"]) == 1      # Telegram still sent
    assert any(lvl == "ERROR" and "discord 500" in m for lvl, m in log_records)


def test_telegram_failing_does_not_stop_discord(monkeypatch, sent_alerts):
    def down(*a, **k):
        raise OSError("telegram 502")

    monkeypatch.setattr("scripts.openclaw_alerts.alert_openclaw", down)
    fails = stop_evaluator._fire_stop(ROW, _result("maker_closed", SHARES), MARK, "r")
    assert len(fails) == 1 and "telegram 502" in fails[0]
    assert len([c for c in sent_alerts if c["fn"] == "discord_alerts._send"]) == 1


def test_a_sender_that_reports_failure_is_a_failure(monkeypatch, sent_alerts):
    monkeypatch.setattr("scripts.openclaw_alerts.alert_openclaw", lambda *a, **k: False)
    fails = stop_evaluator._fire_stop(ROW, _result("maker_closed", SHARES), MARK, "r")
    assert len(fails) == 1 and "telegram" in fails[0].lower()


def test_channel_failure_lands_in_post_error_and_the_sale_still_stands(
        live_db, env, monkeypatch, sent_alerts):
    pid = _insert_position(live_db)
    monkeypatch.setattr(live_executor, "execute_exit", lambda conn, gov, **kw: _result("maker_closed", SHARES))

    def down(*a, **k):
        raise OSError("discord 500")

    monkeypatch.setattr("signals.discord_alerts._send", down)
    out = _call(_row(live_db, pid))
    assert out["action"] == "maker_closed" and "discord 500" in out["post_error"]
    assert len([c for c in sent_alerts if c["fn"] == "alert_openclaw"]) == 1


def test_a_partial_exit_through_the_whole_function_is_announced_as_partly_closed(
        live_db, fake_clob, shapes, env, armed, sent_alerts):
    """Real executor: maker fills 4 of 10, cap not reached (held_remainder)."""
    pid = _insert_position(live_db)
    fake_clob.script("place_limit_order", shapes.accepted_post("oid-m"))
    partial = shapes.open_order("oid-m", "LIVE", side="SELL", price="0.08",
                                original_size="10", size_matched="4")
    fake_clob.script("get_order", partial, partial, partial)
    fake_clob.script("cancel_order", shapes.cancel_confirmed("oid-m"))
    out = _call(_row(live_db, pid), cap=0.90)       # loss 67% < 90%: no taker cross
    assert out["action"] == "held_remainder" and out["shares_sold"] == 4.0
    assert _row(live_db, pid)["shares"] == pytest.approx(6.0)
    text = _alerts(sent_alerts)[1][0]["args"][0]
    assert "PARTLY CLOSED" in text and "6.00" in text and BARE_CLOSED.search(text) is None


# ---------------------------------------------------------------------------
# 1B-fix-3 F1: one writer. The position tracker (close_position, called inside
# execute_exit) already reduces shares and cost_usd for every leg; the evaluator
# must not write the row a second time.
# ---------------------------------------------------------------------------

def _statements_after_the_executor(monkeypatch, live_db):
    """Run the real execute_exit, then record every statement of every connection
    opened through live_db.connect AFTER it returned (that is the stage-3 code)."""
    from execution import live_db as live_db_module
    state = {"after": False, "connects": 0, "statements": []}
    real_connect = live_db_module.connect
    real_exit = live_executor.execute_exit

    def connect(*a, **k):
        conn = real_connect(*a, **k)
        if state["after"]:
            state["connects"] += 1
            conn.set_trace_callback(state["statements"].append)
        return conn

    def exit_then_mark(*a, **k):
        try:
            return real_exit(*a, **k)
        finally:
            state["after"] = True

    monkeypatch.setattr(live_db_module, "connect", connect)
    monkeypatch.setattr(live_executor, "execute_exit", exit_then_mark)
    return state


def test_partial_closed_leaves_shares_and_cost_to_the_tracker_and_writes_nothing_itself(
        live_db, fake_clob, shapes, env, armed, monkeypatch):
    """Real executor: the maker fills 4 of 10, the cap is reached, the taker FAK fills
    3 of the 6 remaining (partial_closed, 7 sold, 3 left). The row is what the
    tracker wrote (shares 3, cost 0.72), and _close_live_position_early opens no
    connection and runs no statement after the executor returned."""
    pid = _insert_position(live_db)
    fake_clob.script("place_limit_order", shapes.accepted_post("oid-m"))
    partial = shapes.open_order("oid-m", "LIVE", side="SELL", price="0.08",
                                original_size="10", size_matched="4")
    fake_clob.script("get_order", partial, partial, partial)
    fake_clob.script("cancel_order", shapes.cancel_confirmed("oid-m"))
    fake_clob.script("place_market_order", shapes.fak_filled_sell(shares="3", usd="0.24"))
    state = _statements_after_the_executor(monkeypatch, live_db)
    out = _call(_row(live_db, pid), cap=0.40)
    assert out["action"] == "partial_closed" and out["shares_sold"] == pytest.approx(7.0)
    row = _row(live_db, pid)
    assert row["status"] == "open"
    assert row["shares"] == pytest.approx(3.0)
    assert row["cost_usd"] == pytest.approx(COST * 0.3)          # the tracker reduced the cost too
    assert state["connects"] == 0 and state["statements"] == []


def test_partial_closed_never_rewrites_a_row_the_tracker_left_different(
        live_db, env, monkeypatch):
    """If the row and the executor's own sums ever disagree, the row stays as the
    tracker left it: a stale snapshot minus shares_sold is never written back."""
    pid = _insert_position(live_db)
    state = _statements_after_the_executor(monkeypatch, live_db)

    def exit_with_other_books(conn, gov, **kw):
        conn.execute("UPDATE live_positions SET shares=8.0, cost_usd=1.9 WHERE id=?", (pid,))
        conn.commit()
        return {"action": "partial_closed", "shares_sold": 7.0, "exit_price": MARK,
                "fee_paid": 0.0, "pnl": -1.0, "reason": "r"}

    monkeypatch.setattr(live_executor, "execute_exit",
                        lambda *a, **k: _run_then_mark(exit_with_other_books, state, *a, **k))
    out = _call(_row(live_db, pid))
    assert out["action"] == "partial_closed"
    row = _row(live_db, pid)
    assert row["shares"] == 8.0 and row["cost_usd"] == 1.9
    assert state["statements"] == []


def _run_then_mark(fn, state, *a, **k):
    try:
        return fn(*a, **k)
    finally:
        state["after"] = True


# ---------------------------------------------------------------------------
# 1B-fix-3 F2: a close with no share count is "unclear", never "NOT SOLD"
# ---------------------------------------------------------------------------

_MISSING = object()


def _result_without_count(action, count):
    res = {"action": action, "exit_price": MARK, "fee_paid": 0.0, "pnl": -1.0}
    if count is not _MISSING:
        res["shares_sold"] = count
    return res


@pytest.mark.parametrize("count", [_MISSING, None, "abc"], ids=["absent", "none", "abc"])
@pytest.mark.parametrize("action", ["maker_closed", "taker_closed", "partial_closed", "held_remainder"])
def test_an_executor_action_with_no_share_count_is_announced_as_unclear(
        sent_alerts, action, count):
    fails = stop_evaluator._fire_stop(ROW, _result_without_count(action, count), MARK, "live stop 67%")
    discord, telegram = _alerts(sent_alerts)
    assert len(discord) == 1 and len(telegram) == 1
    expected = (f"STOP-LOSS: Will it rain in X?: the executor reported {action} but no share "
                "count. Check the position on the exchange and in the ledger.")
    for text in (_discord_text(discord[0]), telegram[0]["args"][0]):
        assert expected in text
        assert "NOT SOLD" not in text.upper() and "CLOSED" not in text and "all 10.00" not in text
    assert _discord_text(discord[0]) == telegram[0]["args"][0]
    assert len(fails) == 1 and action in fails[0] and "no share count" in fails[0]


def test_unclear_close_through_the_whole_function_gains_post_error_and_keeps_the_action(
        live_db, env, monkeypatch, sent_alerts):
    pid = _insert_position(live_db)
    monkeypatch.setattr(live_executor, "execute_exit", lambda conn, gov, **kw: {
        "action": "taker_closed", "exit_price": MARK, "pnl": -1.6})
    out = _call(_row(live_db, pid))
    assert out["action"] == "taker_closed"
    assert "no share count" in out["post_error"]
    discord, telegram = _alerts(sent_alerts)
    assert "NOT SOLD" not in telegram[0]["args"][0].upper()
    assert env.cooldowns == [TOKEN]                    # still counts as a close for the cooldown


# ---------------------------------------------------------------------------
# 1B-fix-3 F3: after a failed sale, say what the ledger shows now
# ---------------------------------------------------------------------------

from execution import live_position_tracker
from services import live_stop_shadow as shadow


def _stop_book(monkeypatch, bid=0.20, ask=0.21):
    book = OrderBook(market_id="m", token_id=TOKEN, outcome="Yes",
                     bids=[OrderBookLevel(price=bid, size=50.0)],
                     asks=[OrderBookLevel(price=ask, size=50.0)],
                     spread=0.01, mid_price=0.205, timestamp="2026-11-11T00:00:00")
    monkeypatch.setattr("odds.polymarket_clob.get_orderbook", lambda t: book)


def _page_for(entry):
    pending = []
    shadow._page_errors([entry], False, pending)
    (key, cooldown, text), = pending
    return text


def test_executor_raising_after_the_tracker_recorded_a_partial_sale_shows_both_figures(
        live_db, env, armed, monkeypatch):
    pid = _insert_position(live_db, entry=0.50)
    _stop_book(monkeypatch)

    def sell_four_then_raise(conn, gov, **kw):
        live_position_tracker.close_position(
            conn, position_id=pid, exit_price=0.20, shares_sold=4.0, fee_paid=0.0,
            reason="maker_exit: t", liquidity="maker", order_id="o1")
        raise ConnectionError("reset by peer")

    monkeypatch.setattr(live_executor, "execute_exit", sell_four_then_raise)
    (entry,) = stop_evaluator.evaluate_live_stops(db_path=live_db.path)
    assert entry["action"] == "exit_unknown"
    assert entry["shares_before"] == 10.0 and entry["shares_now"] == pytest.approx(6.0)
    assert entry["status_now"] == "open" and "reread_error" not in entry
    text = _page_for(entry)
    assert "Before: 10 shares. The ledger now shows 6 shares, status open." in text


def test_executor_raising_before_anything_was_sold_shows_the_same_count(
        live_db, env, armed, monkeypatch):
    pid = _insert_position(live_db, entry=0.50)
    _stop_book(monkeypatch)

    def boom(conn, gov, **kw):
        raise ConnectionError("reset by peer")

    monkeypatch.setattr(live_executor, "execute_exit", boom)
    (entry,) = stop_evaluator.evaluate_live_stops(db_path=live_db.path)
    assert entry["shares_now"] == entry["shares_before"] == 10.0 and entry["status_now"] == "open"
    assert "Before: 10 shares. The ledger now shows 10 shares, status open." in _page_for(entry)


def test_a_result_that_comes_back_exit_unknown_gets_the_ledger_figures_too(
        live_db, env, armed, monkeypatch):
    pid = _insert_position(live_db, entry=0.50)
    _stop_book(monkeypatch)

    def sell_all_and_return_junk(conn, gov, **kw):
        live_position_tracker.close_position(
            conn, position_id=pid, exit_price=0.20, shares_sold=10.0, fee_paid=0.0,
            reason="maker_exit: t", liquidity="maker", order_id="o1")
        return {"pnl": -3.0}                                    # no action

    monkeypatch.setattr(live_executor, "execute_exit", sell_all_and_return_junk)
    (entry,) = stop_evaluator.evaluate_live_stops(db_path=live_db.path)
    assert entry["action"] == "exit_unknown"
    assert entry["shares_before"] == 10.0 and entry["status_now"] == "closed"
    assert "status closed" in _page_for(entry)


def test_a_failed_reread_is_marked_not_guessed(live_db, env, armed, monkeypatch):
    from execution import live_db as live_db_module
    pid = _insert_position(live_db, entry=0.50)
    _stop_book(monkeypatch)
    real_connect = live_db_module.connect
    broken = {"on": False}

    def connect(*a, **k):
        if broken["on"]:
            raise OSError("disk went away")
        return real_connect(*a, **k)

    def boom(conn, gov, **kw):
        broken["on"] = True
        raise ConnectionError("reset by peer")

    monkeypatch.setattr(live_db_module, "connect", connect)
    monkeypatch.setattr(live_executor, "execute_exit", boom)
    (entry,) = stop_evaluator.evaluate_live_stops(db_path=live_db.path)
    assert entry["action"] == "exit_unknown" and entry["shares_before"] == 10.0
    assert entry["shares_now"] is None and entry["reread_error"].startswith("OSError: disk went away")
    assert "The ledger could not be re-read." in _page_for(entry)


# ---------------------------------------------------------------------------
# 1B-fix-3 F4: every error text is bounded and keeps the exception type up front
# ---------------------------------------------------------------------------

HUGE = "x" * 2000


def test_short_keeps_the_type_name_and_the_limit():
    assert stop_evaluator._short(ValueError("boom")) == "ValueError: boom"
    out = stop_evaluator._short(ValueError(HUGE))
    assert len(out) == 300 and out.startswith("ValueError: xxx")
    assert stop_evaluator._short("a" * 500, limit=50) == "a" * 50
    assert stop_evaluator._short("plain") == "plain"


def test_post_error_is_bounded(live_db, env, monkeypatch):
    pid = _insert_position(live_db)
    monkeypatch.setattr(live_executor, "execute_exit", lambda conn, gov, **kw: _result("maker_closed", SHARES))
    monkeypatch.setattr(stop_evaluator, "_fire_stop",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError(HUGE)))
    out = _call(_row(live_db, pid))
    assert len(out["post_error"]) <= 300 and out["post_error"].startswith("ValueError: ")


def test_channel_failure_notes_and_the_post_error_built_from_them_are_bounded(
        live_db, env, monkeypatch, sent_alerts):
    pid = _insert_position(live_db)
    monkeypatch.setattr(live_executor, "execute_exit", lambda conn, gov, **kw: _result("maker_closed", SHARES))

    def down(*a, **k):
        raise OSError(HUGE)

    monkeypatch.setattr("signals.discord_alerts._send", down)
    monkeypatch.setattr("scripts.openclaw_alerts.alert_openclaw", down)
    fails = stop_evaluator._fire_stop(ROW, _result("maker_closed", SHARES), MARK, "r")
    assert len(fails) == 2 and all(len(f) <= 300 and f.startswith("OSError: ") for f in fails)
    assert fails[0].endswith("[discord]") and fails[1].endswith("[telegram]")
    out = _call(_row(live_db, pid))
    assert len(out["post_error"]) <= 300 and out["post_error"].startswith("OSError: ")


def test_exit_failed_error_is_bounded(live_db, env, monkeypatch):
    pid = _insert_position(live_db)

    def boom(conn, mode=None):
        raise ValueError(HUGE)

    monkeypatch.setattr("execution.risk_governor.RiskGovernor", boom)
    out = _call(_row(live_db, pid))
    assert out["action"] == "exit_failed" and out["stage"] == "pre"
    assert len(out["error"]) <= 300 and out["error"].startswith("ValueError: ")


def test_exit_unknown_error_is_bounded_and_raw_stays_at_200(live_db, env, armed, monkeypatch):
    pid = _insert_position(live_db, entry=0.50)
    _stop_book(monkeypatch)

    def boom(conn, gov, **kw):
        raise RuntimeError(HUGE)

    monkeypatch.setattr(live_executor, "execute_exit", boom)
    (entry,) = stop_evaluator.evaluate_live_stops(db_path=live_db.path)
    assert entry["action"] == "exit_unknown"
    assert len(entry["error"]) <= 300 and entry["error"].startswith("RuntimeError: ")
    monkeypatch.setattr(live_executor, "execute_exit",
                        lambda conn, gov, **kw: {"pnl": HUGE})
    out = _call(_row(live_db, pid))
    assert out["action"] == "exit_unknown" and len(out["raw"]) == 200


# ---------------------------------------------------------------------------
# 1B-fix-3 F5: a failed tick-size lookup falls back to 0.01 and says so
# ---------------------------------------------------------------------------

def test_a_failed_tick_size_lookup_falls_back_to_a_cent_and_logs_a_warning(
        live_db, env, monkeypatch, log_records):
    pid = _insert_position(live_db)
    seen = {}

    def no_tick(token_id):
        raise RuntimeError(HUGE)

    monkeypatch.setattr(clob_client, "get_tick_size", no_tick)

    def rec(conn, gov, **kw):
        seen.update(kw)
        return _result("maker_closed", SHARES)

    monkeypatch.setattr(live_executor, "execute_exit", rec)
    _call(_row(live_db, pid))
    assert seen["tick_size"] == 0.01
    warnings = [m for lvl, m in log_records if lvl == "WARNING" and "tick" in m.lower()]
    assert len(warnings) == 1
    assert f"position {pid}" in warnings[0] and "RuntimeError: xxx" in warnings[0]
    assert len(warnings[0]) < 600                       # the error went through _short


# ---------------------------------------------------------------------------
# 1B-fix-3 F7: stage-1 connect failure, the minimum-size notes, keyword-only cap
# ---------------------------------------------------------------------------

def test_a_connect_failure_in_stage_one_is_exit_failed_pre_and_the_executor_never_runs(
        live_db, env, monkeypatch, sent_alerts):
    from execution import live_db as live_db_module
    pid = _insert_position(live_db)
    called = []
    monkeypatch.setattr(live_executor, "execute_exit", lambda *a, **k: called.append(1))

    def no_connect(*a, **k):
        raise OSError("unable to open database file")

    monkeypatch.setattr(live_db_module, "connect", no_connect)
    out = _call(_row(live_db, pid))
    assert out["action"] == "exit_failed" and out["stage"] == "pre"
    assert out["error"] == "OSError: unable to open database file"
    assert called == [] and sent_alerts == []


def test_hard_stop_blocked_held_remainder_says_the_hard_stop_could_not_execute(sent_alerts):
    """What the executor returns for a sub-minimum remainder at the hard cap: nothing
    sold, min_size_blocked and hard_stop_blocked both set."""
    res = _result("held_remainder", 0.0, min_size_blocked=True, hard_stop_blocked=True)
    stop_evaluator._fire_stop(ROW, res, MARK, "live stop 67%")
    discord, telegram = _alerts(sent_alerts)
    for text in (_discord_text(discord[0]), telegram[0]["args"][0]):
        assert "NOT SOLD" in text.upper()
        assert "The hard stop could not execute (position below the exchange minimum)." in text


def test_hard_stop_blocked_partial_sale_says_the_rest_could_not_be_sold(sent_alerts):
    res = _result("held_remainder", 4.0, min_size_blocked=True, hard_stop_blocked=True)
    stop_evaluator._fire_stop(ROW, res, MARK, "live stop 67%")
    discord, telegram = _alerts(sent_alerts)
    for text in (_discord_text(discord[0]), telegram[0]["args"][0]):
        assert "PARTLY CLOSED" in text
        assert "The rest could not be sold (below the exchange minimum)." in text


def test_min_size_blocked_alone_adds_no_note_only_hard_stop_blocked_does(sent_alerts):
    """Pins what _fire_stop does today: it reads hard_stop_blocked only."""
    res = _result("held_remainder", 4.0, min_size_blocked=True)
    stop_evaluator._fire_stop(ROW, res, MARK, "live stop 67%")
    text = _alerts(sent_alerts)[1][0]["args"][0]
    assert "exchange minimum" not in text


def test_hard_cap_frac_is_required_and_keyword_only(live_db):
    row = _row(live_db, _insert_position(live_db))
    with pytest.raises(TypeError):
        stop_evaluator._close_live_position_early(row, MARK, "r")
    with pytest.raises(TypeError):
        stop_evaluator._close_live_position_early(row, MARK, "r", 0.4)

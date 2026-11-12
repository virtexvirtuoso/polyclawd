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

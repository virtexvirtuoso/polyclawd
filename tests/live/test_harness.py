"""Tests of the tests/live harness itself (plan task 1.0c).

Each test drives the REAL wrapper function in execution/clob_client.py against
FakeClob, so the wrapper's conversion code runs on real SDK objects. Where the
wrapper does something questionable, the test asserts what it does TODAY and its
docstring says "documents current behaviour, see audit".
"""

import pytest
import requests

from polymarket import Page, Paginator

from execution import clob_client, live_db as live_db_module
from execution.clob_client import ClobError

TOKEN = "71321045679252212594626385532706912750332728571942532289631379312455583992563"


# --- post_maker -------------------------------------------------------------


def test_post_maker_accepted(fake_clob, shapes):
    fake_clob.script("place_limit_order", shapes.accepted_post())
    out = clob_client.post_maker(TOKEN, "BUY", 0.42, 10.0, tick_size=0.01)
    assert out == {
        "orderID": "0xorder-live-1",
        "id": "0xorder-live-1",
        "status": "LIVE",
        "making_amount": 0.0,
        "taking_amount": 0.0,
    }
    assert fake_clob.calls_to("place_limit_order") == [
        ((), {"token_id": TOKEN, "price": 0.42, "size": 10.0, "side": "BUY", "post_only": True})
    ]


def test_post_maker_rejected_would_cross_raises_clob_error(fake_clob, shapes):
    """Today a RejectedOrder becomes ClobError(f"post_maker rejected ({code}): {message}")."""
    fake_clob.script("place_limit_order", shapes.rejected_would_cross())
    with pytest.raises(ClobError) as exc:
        clob_client.post_maker(TOKEN, "BUY", 0.99, 10.0, tick_size=0.01)
    assert str(exc.value) == (
        "post_maker rejected (post_only_would_cross): invalid post-only order: order crosses book"
    )


def test_post_maker_rejected_bad_tick_raises_clob_error(fake_clob, shapes):
    fake_clob.script("place_limit_order", shapes.rejected_bad_tick())
    with pytest.raises(ClobError, match=r"post_maker rejected \(unknown\): invalid price"):
        clob_client.post_maker(TOKEN, "BUY", 0.425, 10.0, tick_size=0.01)


# --- cross_taker ------------------------------------------------------------


def test_cross_taker_buy_filled(fake_clob, shapes):
    fake_clob.script("place_market_order", shapes.fak_filled_buy(usd="4.2", shares="10"))
    out = clob_client.cross_taker(TOKEN, "BUY", 4.2, tick_size=0.01)
    assert out["orderID"] == "0xorder-fak-1"
    assert out["status"] == "MATCHED"
    assert out["making_amount"] == 4.2 and out["taking_amount"] == 10.0
    assert out["size_matched"] == out["sizeMatched"] == "10.0"
    assert out["avg_price"] == pytest.approx(0.42)
    assert fake_clob.calls_to("place_market_order") == [
        ((), {"token_id": TOKEN, "side": "BUY", "amount": 4.2, "order_type": "FAK"})
    ]


def test_cross_taker_sell_filled(fake_clob, shapes):
    fake_clob.script("place_market_order", shapes.fak_filled_sell(shares="10", usd="4.0"))
    out = clob_client.cross_taker(TOKEN, "SELL", 10.0, tick_size=0.01)
    assert out["status"] == "MATCHED"
    assert out["size_matched"] == "10.0"
    assert out["avg_price"] == pytest.approx(0.4)
    assert fake_clob.calls_to("place_market_order") == [
        ((), {"token_id": TOKEN, "side": "SELL", "shares": 10.0, "order_type": "FAK"})
    ]


def test_cross_taker_fak_unfilled_is_unmatched_not_an_error(fake_clob, shapes):
    fake_clob.script("place_market_order", shapes.fak_unfilled())
    out = clob_client.cross_taker(TOKEN, "BUY", 4.2, tick_size=0.01)
    assert out == {
        "orderID": "",
        "id": "",
        "status": "UNMATCHED",
        "size_matched": "0",
        "sizeMatched": "0",
        "making_amount": 0.0,
        "taking_amount": 0.0,
    }


def test_cross_taker_hard_reject_raises(fake_clob, shapes):
    fake_clob.script("place_market_order", shapes.rejected_not_enough_balance())
    with pytest.raises(ClobError, match=r"cross_taker rejected \(not_enough_balance\)"):
        clob_client.cross_taker(TOKEN, "BUY", 4.2, tick_size=0.01)


# --- cancel -----------------------------------------------------------------


def test_cancel_confirmed(fake_clob, shapes):
    fake_clob.script("cancel_order", shapes.cancel_confirmed("0xorder-live-1"))
    assert clob_client.cancel("0xorder-live-1") == {"cancelled": True, "order_id": "0xorder-live-1"}
    assert fake_clob.calls_to("cancel_order") == [((), {"order_id": "0xorder-live-1"})]


def test_cancel_not_canceled_reports_false_and_drops_the_reason(fake_clob, shapes):
    """documents current behaviour, see audit: a `not_canceled` entry (the exchange
    saying why, e.g. already matched) is returned as plain cancelled=False; the
    reason is not surfaced, so 'already filled' and 'unknown order' look alike."""
    fake_clob.script("cancel_order", shapes.cancel_not_canceled("0xorder-live-1"))
    assert clob_client.cancel("0xorder-live-1") == {"cancelled": False, "order_id": "0xorder-live-1"}


def test_cancel_other_id_confirmed_is_false(fake_clob, shapes):
    fake_clob.script("cancel_order", shapes.cancel_confirmed("0xsomething-else"))
    assert clob_client.cancel("0xorder-live-1")["cancelled"] is False


# --- get_order / order_is_filled --------------------------------------------


def test_get_order_live(fake_clob, shapes):
    fake_clob.script("get_order", shapes.order_live())
    out = clob_client.get_order("0xorder-live-1")
    assert out == {
        "id": "0xorder-live-1",
        "orderID": "0xorder-live-1",
        "status": "LIVE",
        "original_size": "10",
        "originalSize": "10",
        "size_matched": "0",
        "sizeMatched": "0",
        "price": "0.42",
    }
    assert fake_clob.calls_to("get_order") == [((), {"order_id": "0xorder-live-1"})]
    assert clob_client.order_is_filled(out) is False


def test_get_order_matched(fake_clob, shapes):
    fake_clob.script("get_order", shapes.order_matched())
    out = clob_client.get_order("0xorder-live-1")
    assert out["status"] == "MATCHED" and out["size_matched"] == "10"
    assert clob_client.order_is_filled(out) is True


def test_get_order_cancelled(fake_clob, shapes):
    fake_clob.script("get_order", shapes.order_cancelled())
    out = clob_client.get_order("0xorder-live-1")
    assert out["status"] == "CANCELED" and out["size_matched"] == "0"
    assert clob_client.order_is_filled(out) is False


def test_get_order_sdk_error_becomes_clob_error(fake_clob):
    fake_clob.script("get_order", RuntimeError("404 not found"))
    with pytest.raises(ClobError, match=r"get_order\(0xgone\) failed: 404 not found"):
        clob_client.get_order("0xgone")


# --- get_balance ------------------------------------------------------------


def test_get_balance(fake_clob, shapes):
    fake_clob.script("get_balance_allowance", shapes.balance("12.5"))
    assert clob_client.get_balance() == 12.5
    assert fake_clob.calls_to("get_balance_allowance") == [((), {"asset_type": "COLLATERAL"})]


# --- list_open_orders (called on the client directly by scripts/position_sync.py) ---


def test_list_open_orders_empty_page(fake_clob, shapes):
    fake_clob.script("list_open_orders", [shapes.empty_page()])
    paginator = clob_client._get_client().list_open_orders()
    assert isinstance(paginator, Paginator)
    assert list(paginator.iter_items()) == []


def test_list_open_orders_two_pages_yield_every_item(fake_clob, shapes):
    fake_clob.script("list_open_orders", shapes.open_orders_pages())
    items = list(clob_client._get_client().list_open_orders().iter_items())
    assert [o.id for o in items] == ["0xorder-A", "0xorder-B", "0xorder-C"]
    assert items[2].side == "SELL"
    fake_clob.assert_all_consumed()


def test_iterating_the_paginator_itself_yields_pages_not_orders(fake_clob, shapes):
    """documents current behaviour, see audit: scripts/position_sync.py:852 runs
    `list(client.list_open_orders())` and then reads `.id` of each element. The SDK
    Paginator iterates PAGES, so every element is a Page with no `id`, and the set
    of CLOB order ids that sync_open_orders compares against is {""}."""
    fake_clob.script("list_open_orders", shapes.open_orders_pages())
    clob_orders = list(clob_client._get_client().list_open_orders())  # the line-852 expression
    assert all(isinstance(o, Page) for o in clob_orders)
    clob_ids = {str(getattr(o, "id", "") or "") for o in clob_orders}
    assert clob_ids == {""}


# --- the fake itself --------------------------------------------------------


def test_unqueued_call_raises_and_is_recorded(fake_clob):
    with pytest.raises(fake_clob.Unscripted, match="nothing scripted"):
        fake_clob.cancel_order(order_id="0xabc")
    assert fake_clob.calls == [("cancel_order", (), {"order_id": "0xabc"})]


def test_unqueued_call_through_post_maker_propagates(fake_clob):
    with pytest.raises(fake_clob.Unscripted):
        clob_client.post_maker(TOKEN, "BUY", 0.42, 10.0, tick_size=0.01)


def test_unqueued_get_order_is_wrapped_by_the_wrapper(fake_clob):
    """documents current behaviour, see audit: get_order() converts ANY exception
    from the client into ClobError, so an unscripted call surfaces as ClobError
    carrying the fake's message."""
    with pytest.raises(ClobError, match="nothing scripted"):
        clob_client.get_order("0xabc")


def test_each_method_has_its_own_queue(fake_clob, shapes):
    fake_clob.script("cancel_order", shapes.cancel_confirmed("0x1"))
    with pytest.raises(fake_clob.Unscripted):
        fake_clob.get_order(order_id="0x1")
    assert fake_clob.cancel_order(order_id="0x1").canceled == ("0x1",)
    with pytest.raises(fake_clob.Unscripted):  # answers are consumed once
        fake_clob.cancel_order(order_id="0x1")


def test_answers_come_out_in_order(fake_clob, shapes):
    fake_clob.script("get_order", shapes.order_live(), shapes.order_matched())
    assert fake_clob.get_order(order_id="x").status == "LIVE"
    assert fake_clob.get_order(order_id="x").status == "MATCHED"


def test_fake_rejects_a_keyword_the_sdk_method_does_not_take(fake_clob, shapes):
    fake_clob.script("place_limit_order", shapes.accepted_post())
    with pytest.raises(TypeError):
        fake_clob.place_limit_order(token_id=TOKEN, price=0.4, size=1.0, side="BUY", tick_size=0.01)


def test_fake_has_no_methods_beyond_the_six(fake_clob):
    with pytest.raises(AttributeError):
        fake_clob.cancel_all()


def test_fake_clob_is_the_injected_client_during_the_test(fake_clob):
    assert clob_client._get_client() is fake_clob


def test_no_call_is_recorded_when_the_wrapper_refuses_before_the_client(fake_clob, monkeypatch):
    """The egress guard fails closed before any client call (guard active)."""
    monkeypatch.setenv("POLYCLAWD_EGRESS_REQUIRE_SRC", "10.2.0.2")
    monkeypatch.setattr(clob_client, "_egress_src_ip", lambda host, port=443: "192.0.2.1")
    with pytest.raises(ClobError, match="egress guard"):
        clob_client.post_maker(TOKEN, "BUY", 0.42, 10.0, tick_size=0.01)
    fake_clob.assert_no_calls()


def test_egress_guard_is_inactive_by_default():
    """With POLYCLAWD_EGRESS_REQUIRE_SRC unset the guard returns without probing
    the network (the autouse fixture removes any ambient value and blocks sockets)."""
    clob_client.assert_egress_tunneled()


# --- no network -------------------------------------------------------------


def test_http_is_blocked_in_tests_live():
    with pytest.raises(AssertionError, match="network access attempted"):
        requests.get("http://127.0.0.1:9/")


def test_fixed_tick_and_min_size_helper(fixed_tick_and_min_size):
    assert clob_client.get_tick_size(TOKEN) == 0.01
    assert clob_client.get_min_order_size(TOKEN) == 5.0


# --- live_db ----------------------------------------------------------------


def test_live_db_applies_the_production_schema(live_db):
    tables = {r[0] for r in live_db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"live_positions", "live_fills", "live_open_orders", "live_portfolio_state"} <= tables


def test_live_db_redirects_argumentless_connect(live_db):
    conn = live_db_module.connect()
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        assert row[2] == str(live_db.path)
    finally:
        conn.close()


def test_live_db_does_not_touch_the_default_path(live_db):
    default_existed = live_db_module.DB_PATH.exists()
    before = live_db_module.DB_PATH.stat().st_mtime_ns if default_existed else None
    conn = live_db_module.connect()
    conn.close()
    assert live_db_module.DB_PATH.exists() == default_existed
    if default_existed:
        assert live_db_module.DB_PATH.stat().st_mtime_ns == before


# --- teardown ---------------------------------------------------------------
# The fake_clob fixture asserts its own reset in teardown. This pair adds an
# observation from the next test; it relies on file order (pytest's default).


def test_zz_teardown_part_1_injects_a_client(fake_clob):
    assert clob_client._client is fake_clob


def test_zz_teardown_part_2_client_was_reset():
    assert clob_client._client is None

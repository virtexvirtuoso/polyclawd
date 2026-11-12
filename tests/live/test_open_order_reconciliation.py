"""sync_open_orders against the fake exchange (plan task 1.4, audit #29).

`scripts/position_sync.py` used to run `list(client.list_open_orders())`, which
iterates the SDK Paginator and so yields Page objects, not orders. Every order id
it read was "", the set of exchange ids was {""}, and every order the database
held as resting looked absent from the exchange. These tests drive the real
`sync_open_orders` against the `live_db` and `fake_clob` fixtures.
"""

from __future__ import annotations

import inspect
import logging
import types

import pytest

from polymarket import Page

from scripts import position_sync as ps

LOGGER = "scripts.position_sync"


def _add_order(conn, order_id, status="live", ref=None, token="tokA"):
    conn.execute(
        "INSERT INTO live_open_orders (client_order_ref, order_id, token_id, side, "
        "price, size, status, ts) VALUES (?,?,?,?,?,?,?,?)",
        (ref or f"sw-ref-{order_id}", order_id, token, "BUY", 0.42, 10.0, status, "2026-11-11T00:00:00+00:00"),
    )
    conn.commit()


def _snapshot(conn):
    rows = conn.execute("SELECT id, client_order_ref, order_id, status FROM live_open_orders ORDER BY id").fetchall()
    return [tuple(r) for r in rows]


def _status_of(conn, order_id):
    return conn.execute("SELECT status FROM live_open_orders WHERE order_id=?", (order_id,)).fetchone()[0]


# --- the plan's test --------------------------------------------------------


def test_orders_on_the_exchange_over_two_pages_are_not_relabelled(live_db, fake_clob, shapes):
    for oid in ("0xorder-A", "0xorder-B", "0xorder-C"):
        _add_order(live_db.conn, oid)
    fake_clob.script("list_open_orders", shapes.open_orders_pages())
    before = _snapshot(live_db.conn)

    out = ps.sync_open_orders(live_db.conn)

    assert _snapshot(live_db.conn) == before
    assert {r[3] for r in _snapshot(live_db.conn)} == {"live"}
    assert out["cancelled_stale"] == 0 and out["filled_stale"] == 0
    assert fake_clob.calls_to("get_order") == []  # nothing absent, nothing probed
    fake_clob.assert_all_consumed()


# --- absent from the exchange's list: labelled by get_order ------------------


def test_absent_order_that_get_order_reports_filled_becomes_filled(live_db, fake_clob, shapes):
    for oid in ("0xorder-A", "0xorder-B", "0xorder-C", "0xorder-gone"):
        _add_order(live_db.conn, oid)
    fake_clob.script("list_open_orders", shapes.open_orders_pages())
    fake_clob.script("get_order", shapes.order_matched("0xorder-gone"))

    out = ps.sync_open_orders(live_db.conn)

    assert _status_of(live_db.conn, "0xorder-gone") == "filled"
    assert out["filled_stale"] == 1 and out["cancelled_stale"] == 0
    assert [_status_of(live_db.conn, o) for o in ("0xorder-A", "0xorder-B", "0xorder-C")] == ["live"] * 3
    assert fake_clob.calls_to("get_order") == [((), {"order_id": "0xorder-gone"})]


def test_absent_order_that_get_order_reports_cancelled_becomes_cancelled(live_db, fake_clob, shapes):
    for oid in ("0xorder-A", "0xorder-B", "0xorder-C", "0xorder-gone"):
        _add_order(live_db.conn, oid)
    fake_clob.script("list_open_orders", shapes.open_orders_pages())
    fake_clob.script("get_order", shapes.order_cancelled("0xorder-gone"))

    out = ps.sync_open_orders(live_db.conn)

    assert _status_of(live_db.conn, "0xorder-gone") == "cancelled"
    assert out["cancelled_stale"] == 1 and out["filled_stale"] == 0
    assert [_status_of(live_db.conn, o) for o in ("0xorder-A", "0xorder-B", "0xorder-C")] == ["live"] * 3


@pytest.mark.parametrize("status", ["LIVE", "live"])
def test_absent_order_that_get_order_still_reports_live_is_left_alone(live_db, fake_clob, shapes, caplog, status):
    _add_order(live_db.conn, "0xorder-resting")
    fake_clob.script("list_open_orders", shapes.empty_page())
    fake_clob.script("get_order", shapes.open_order("0xorder-resting", status=status))
    before = _snapshot(live_db.conn)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        out = ps.sync_open_orders(live_db.conn)

    assert _snapshot(live_db.conn) == before
    assert out["cancelled_stale"] == 0 and out["filled_stale"] == 0
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "0xorder-resting" in r.getMessage()]
    assert warnings, "expected a WARNING naming the order id"


def test_empty_open_orders_page_probes_every_database_row_and_labels_by_status(live_db, fake_clob, shapes):
    for oid in ("0xorder-1", "0xorder-2", "0xorder-3"):
        _add_order(live_db.conn, oid)
    fake_clob.script("list_open_orders", shapes.empty_page())
    fake_clob.script(
        "get_order",
        shapes.order_matched("0xorder-1"),
        shapes.order_cancelled("0xorder-2"),
        shapes.order_matched("0xorder-3"),
    )

    out = ps.sync_open_orders(live_db.conn)

    assert [_status_of(live_db.conn, o) for o in ("0xorder-1", "0xorder-2", "0xorder-3")] == [
        "filled",
        "cancelled",
        "filled",
    ]
    assert out == {"reconciled": 3, "cancelled_stale": 1, "filled_stale": 2}
    assert [k["order_id"] for _, k in fake_clob.calls_to("get_order")] == ["0xorder-1", "0xorder-2", "0xorder-3"]


def test_get_order_raising_for_one_row_does_not_stop_the_others(live_db, fake_clob, shapes):
    """Today a failed probe defaults that row to 'cancelled' (pinned by
    tests/test_late_maker_fill_attribution.py::test_sync_open_orders_probe_failure_defaults_cancelled)
    and the loop carries on. Kept as is; reported as a hazard."""
    for oid in ("0xorder-1", "0xorder-2"):
        _add_order(live_db.conn, oid)
    fake_clob.script("list_open_orders", shapes.empty_page())
    fake_clob.script("get_order", RuntimeError("clob down"), shapes.order_matched("0xorder-2"))

    out = ps.sync_open_orders(live_db.conn)

    assert _status_of(live_db.conn, "0xorder-2") == "filled"  # the other row was still processed
    assert _status_of(live_db.conn, "0xorder-1") == "cancelled"  # today's behaviour on a failed probe
    assert out["filled_stale"] == 1 and out["cancelled_stale"] == 1


# --- an item without a usable id aborts the whole run ------------------------


def _page_with(*items):
    return Page(items=tuple(items), has_more=False)


_NO_ID_ITEMS = {
    "empty-string-id": lambda shapes: shapes.open_order(""),
    "none-id": lambda shapes: types.SimpleNamespace(id=None),  # the SDK model rejects a None id
    "no-id-attribute": lambda shapes: types.SimpleNamespace(status="LIVE"),
    "integer-id": lambda shapes: types.SimpleNamespace(id=123),  # an int is not a usable id: str only
    "a-page-as-an-item": lambda shapes: _page_with(),  # the exact shape the old line 852 produced
}


@pytest.mark.parametrize("kind", list(_NO_ID_ITEMS))
def test_item_without_id_aborts_the_run_logs_error_and_changes_no_row(live_db, fake_clob, shapes, caplog, kind):
    for oid in ("0xorder-A", "0xorder-gone"):
        _add_order(live_db.conn, oid)
    bad = _NO_ID_ITEMS[kind](shapes)
    fake_clob.script("list_open_orders", _page_with(shapes.open_order("0xorder-A"), bad))
    fake_clob.script("get_order", shapes.order_matched("0xorder-gone"))  # would relabel it if the run went on
    before = _snapshot(live_db.conn)

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        out = ps.sync_open_orders(live_db.conn)  # must not raise: the tick must not crash

    assert _snapshot(live_db.conn) == before
    assert fake_clob.calls_to("get_order") == []
    assert out.get("error")
    assert out["cancelled_stale"] == 0 and out["filled_stale"] == 0
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, "expected an ERROR log line"
    assert type(bad).__name__ in errors[0].getMessage()


def test_the_tick_continues_after_a_malformed_open_order_list(live_db, fake_clob, shapes, monkeypatch):
    """Drives scripts.position_sync.run() itself. What is patched, and why: the
    three steps before the order sync (check_resolutions, check_wallet_balance,
    check_fill_reconciliation) reach the network or the data API and are not what
    this test is about; sync_positions is replaced by a recorder, which is what
    the assertion needs. Not patched: live_db (the temp database), the order
    sync, and the bankroll block, which calls the fake client's
    get_balance_allowance, has nothing scripted, and so fails inside its own
    try/except, as it does when the exchange is down. run() must not raise and
    must still reach sync_positions."""
    _add_order(live_db.conn, "0xorder-gone")
    live_db.conn.commit()
    monkeypatch.setattr(ps, "check_resolutions", lambda conn: [])
    monkeypatch.setattr(ps, "check_wallet_balance", lambda conn: None)
    monkeypatch.setattr(ps, "check_fill_reconciliation", lambda *a, **k: {})
    sync_calls = []
    monkeypatch.setattr(ps, "sync_positions", lambda conn: sync_calls.append(conn) or [])
    fake_clob.script("list_open_orders", _page_with(types.SimpleNamespace(id=None)))
    fake_clob.script("get_order", shapes.order_matched("0xorder-gone"))  # would relabel it if the sync went on
    before = _snapshot(live_db.conn)

    out = ps.run()                                  # must not raise

    assert len(sync_calls) == 1                     # the tick went on to the next step
    assert "error" not in out and out["new"] == 0   # a clean tick as far as run() reports
    assert fake_clob.calls_to("get_order") == []    # the malformed list stopped the order sync only
    assert _snapshot(live_db.conn) == before


# --- the status list lives in one place ---------------------------------------


def test_reconciled_statuses_are_defined_once_and_used_by_both_readers():
    assert ps._RECONCILED_ORDER_STATUSES == ("live",)
    assert "_RECONCILED_ORDER_STATUSES" in inspect.getsource(ps._reconciled_status_filter)
    for fn in (ps.sync_open_orders, ps._get_tracked_token_ids):
        src = inspect.getsource(fn)
        assert "_reconciled_status_filter()" in src
        assert "'live'" not in src and '"live"' not in src

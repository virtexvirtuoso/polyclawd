"""Late maker fills must be attributed to their sw- orders, not labeled manual.

2026-09-22: the Cury 3rd-place maker order filled ~11h after placement, outside
the executor's post-place watch window. position_sync then discovered the
position on-chain with no provenance and paged MANUAL POSITION DETECTED for a
trade our own executor placed. These tests pin the attribution + backfill and
the filled-vs-cancelled order classification.
"""

import sqlite3
import sys
import types

import pytest

from scripts import position_sync as ps


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.db"))
    c.execute("""CREATE TABLE live_positions (
        id INTEGER PRIMARY KEY, opened_at TEXT, market_id TEXT, market_slug TEXT,
        market_title TEXT, token_id TEXT, side TEXT, entry_price REAL, shares REAL,
        cost_usd REAL, status TEXT, closed_at TEXT, exit_price REAL, pnl REAL,
        close_reason TEXT, fee_paid_total REAL, archetype TEXT)""")
    c.execute("""CREATE TABLE live_open_orders (
        id INTEGER PRIMARY KEY, client_order_ref TEXT, order_id TEXT, token_id TEXT,
        side TEXT, price REAL, size REAL, status TEXT, ts TEXT)""")
    c.execute("""CREATE TABLE live_fills (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, position_id INTEGER,
        order_id TEXT, side TEXT, liquidity TEXT, price REAL, shares REAL,
        usd REAL, fee_paid REAL, fair_price REAL, slippage_vs_fair REAL)""")
    c.execute("""CREATE TABLE live_entry_reasoning (
        id INTEGER PRIMARY KEY AUTOINCREMENT, position_id INTEGER NOT NULL, ts TEXT,
        trigger_source TEXT, wallet_address TEXT, wallet_win_rate REAL,
        wallet_net_pnl REAL, edge_pct REAL, confidence REAL, reasoning TEXT,
        raw_json TEXT)""")
    c.commit()
    return c


TOKEN = "47627966085172760705785636278889708622869169862219868575174176690844073780006"


def _place_sw_order(
    conn, token=TOKEN, price=0.62, size=7.67580967741935, ref="sw-2026-09-22-0xbc0070bd88b113-1", status="cancelled"
):
    # status='cancelled' mirrors production: sync_open_orders marks a vanished
    # order cancelled before position_sync discovers the on-chain fill, so the
    # token is NOT in _get_tracked_token_ids at discovery time.
    conn.execute(
        "INSERT INTO live_open_orders (client_order_ref, order_id, token_id, side, "
        "price, size, status, ts) VALUES (?,?,?,?,?,?,?,?)",
        (
            ref,
            "0xe5f96b1fa82f610eb9240f712dfc7ad32f7d385b4d3cae4173a61973356b4dd6",
            token,
            "BUY",
            price,
            size,
            status,
            "2026-09-22T02:05:07+00:00",
        ),
    )
    conn.commit()


def _pm_position(token=TOKEN, avg=0.62, size=7.67):
    return {
        "asset": token,
        "conditionId": "0xbc0070bd88b113d9beedbae5efd78df0e605ca029aff8ef470c881010c923a24",
        "slug": "will-augusto-cury-finish-in-third-place",
        "title": "Will Augusto Cury finish in third place (1st round, 2026 Brazil)?",
        "avgPrice": avg,
        "size": size,
        "initialValue": round(avg * size, 4),
        "curPrice": 0.601,
        "cashPnl": -0.15,
        "redeemable": False,
    }


def _run_sync(conn, monkeypatch, pos):
    monkeypatch.setattr(ps, "_fetch_pm_positions", lambda: [pos])
    return ps.sync_positions(conn)


def test_late_maker_fill_attributed_to_sw_order(conn, monkeypatch):
    _place_sw_order(conn)
    new = _run_sync(conn, monkeypatch, _pm_position())
    assert len(new) == 1
    row = conn.execute("SELECT archetype FROM live_positions WHERE token_id=?", (TOKEN,)).fetchone()
    assert row[0] == "smart_wallet"
    reason = conn.execute(
        "SELECT trigger_source, reasoning FROM live_entry_reasoning ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert reason[0] == "smart_wallet"
    assert "late maker fill attributed" in reason[1]
    assert "0xe5f96b1fa82f61" in reason[1]  # 16-char order-id prefix, as written by the code
    fill = conn.execute("SELECT position_id, liquidity, price, shares, order_id FROM live_fills").fetchone()
    assert fill[0] == 1
    assert fill[1] == "maker"
    assert fill[2] == pytest.approx(0.62)
    assert fill[3] == pytest.approx(7.67)
    assert fill[4].startswith("0xe5f96b1f")
    order_status = conn.execute("SELECT status FROM live_open_orders WHERE client_order_ref LIKE 'sw-%'").fetchone()[0]
    assert order_status == "filled"


def test_unmatched_position_stays_manual(conn, monkeypatch):
    new = _run_sync(conn, monkeypatch, _pm_position())
    assert len(new) == 1
    row = conn.execute("SELECT archetype FROM live_positions").fetchone()
    assert row[0] == "manual"
    reason = conn.execute("SELECT trigger_source, reasoning FROM live_entry_reasoning").fetchone()
    assert reason[0] == "position_sync"
    assert "not originated by a strategy executor" in reason[1]
    assert conn.execute("SELECT COUNT(*) FROM live_fills").fetchone()[0] == 0


def test_price_size_mismatch_stays_manual(conn, monkeypatch):
    # Same token, but the placed order is for a different price/size: no attribution.
    _place_sw_order(conn, price=0.40, size=20.0)
    new = _run_sync(conn, monkeypatch, _pm_position())
    assert len(new) == 1
    assert conn.execute("SELECT archetype FROM live_positions").fetchone()[0] == "manual"
    assert conn.execute("SELECT COUNT(*) FROM live_fills").fetchone()[0] == 0


def test_non_sw_ref_ignored(conn, monkeypatch):
    _place_sw_order(conn, ref="manual-2026-09-22-abc")
    new = _run_sync(conn, monkeypatch, _pm_position())
    assert len(new) == 1
    assert conn.execute("SELECT archetype FROM live_positions").fetchone()[0] == "manual"


def test_still_live_order_token_is_skipped_not_reregistered(conn, monkeypatch):
    # While our order for the token is still status='live', the position is
    # considered tracked (executor owns it) and sync_positions skips it. If
    # the fill lands later, the order flips to filled/cancelled and the next
    # cycle attributes it — a one-cycle delay, not a provenance loss.
    _place_sw_order(conn, status="live")
    new = _run_sync(conn, monkeypatch, _pm_position())
    assert new == []
    assert conn.execute("SELECT COUNT(*) FROM live_positions").fetchone()[0] == 0


def test_attribution_lookup_failure_returns_manual_default(conn):
    # Drop the table so the attribution SELECT itself fails: the helper must
    # degrade to the manual default instead of raising (registration must
    # never be blocked by a provenance lookup problem).
    conn.execute("DROP TABLE live_open_orders")
    conn.commit()
    result = ps._attribute_to_placed_order(conn, TOKEN, 7.67, 0.62)
    assert result["archetype"] == "manual"
    assert result["trigger_source"] == "position_sync"
    assert result["order"] is None


# ---------------------------------------------------------------------------
# sync_open_orders: filled vs cancelled classification
# ---------------------------------------------------------------------------


def _inject_clob_module(monkeypatch, order_status: dict):
    fake = types.ModuleType("execution.clob_client")
    fake._get_client = lambda: type("C", (), {"list_open_orders": lambda self: []})()
    fake.get_order = lambda order_id: order_status
    fake.order_is_filled = lambda st: str(st.get("status", "")).upper() in ("MATCHED", "FILLED")
    monkeypatch.setitem(sys.modules, "execution.clob_client", fake)


def test_sync_open_orders_marks_filled(conn, monkeypatch):
    conn.execute(
        "INSERT INTO live_open_orders (client_order_ref, order_id, token_id, side, "
        "price, size, status, ts) VALUES ('sw-x-1', '0xabc', 'tokA', 'BUY', 0.5, 10, "
        "'live', '2026-09-22T00:00:00+00:00')"
    )
    conn.commit()
    _inject_clob_module(monkeypatch, {"status": "MATCHED", "original_size": "10", "size_matched": "10"})
    out = ps.sync_open_orders(conn)
    assert out["filled_stale"] == 1
    assert out["cancelled_stale"] == 0
    assert conn.execute("SELECT status FROM live_open_orders").fetchone()[0] == "filled"


def test_sync_open_orders_marks_cancelled(conn, monkeypatch):
    conn.execute(
        "INSERT INTO live_open_orders (client_order_ref, order_id, token_id, side, "
        "price, size, status, ts) VALUES ('sw-x-2', '0xdef', 'tokB', 'BUY', 0.5, 10, "
        "'live', '2026-09-22T00:00:00+00:00')"
    )
    conn.commit()
    _inject_clob_module(monkeypatch, {"status": "CANCELED", "original_size": "10", "size_matched": "0"})
    out = ps.sync_open_orders(conn)
    assert out["cancelled_stale"] == 1
    assert out["filled_stale"] == 0
    assert conn.execute("SELECT status FROM live_open_orders").fetchone()[0] == "cancelled"


def test_sync_open_orders_probe_failure_defaults_cancelled(conn, monkeypatch):
    conn.execute(
        "INSERT INTO live_open_orders (client_order_ref, order_id, token_id, side, "
        "price, size, status, ts) VALUES ('sw-x-3', '0x999', 'tokC', 'BUY', 0.5, 10, "
        "'live', '2026-09-22T00:00:00+00:00')"
    )
    conn.commit()
    fake = types.ModuleType("execution.clob_client")
    fake._get_client = lambda: type("C", (), {"list_open_orders": lambda self: []})()

    def _boom(order_id):
        raise RuntimeError("clob down")

    fake.get_order = _boom
    fake.order_is_filled = lambda st: False
    monkeypatch.setitem(sys.modules, "execution.clob_client", fake)
    out = ps.sync_open_orders(conn)
    assert out["cancelled_stale"] == 1
    assert conn.execute("SELECT status FROM live_open_orders").fetchone()[0] == "cancelled"

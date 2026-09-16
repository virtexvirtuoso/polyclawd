"""Regression tests for two defects found verifying the first live fill.

1. The correlation guard (RiskGovernor Rule 5.5) was DOUBLY inert:
   execute_intent() had no event_id parameter, so the entry/taker gates rebuilt
   an intent dict without it; and none of the three governor.record_fill() call
   sites passed one, so _event_id_by_market was never populated. Nothing to
   match against, and nothing matching.

2. record_real_fill() hardcoded archetype="weather" for EVERY live position.
   The first live fill -- a tennis market taken by smart_wallet -- was written
   to the ledger as "weather", which breaks the per-strategy CI attribution the
   canary gate is pre-registered on.
"""

import sqlite3

import pytest

from execution import live_db, live_config
from execution.risk_governor import RiskGovernor


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    live_db.init_live_tables(c)
    yield c
    c.close()


def _allowed_category():
    return sorted(live_config.live_strategy_allowlist())[0]


# ---------------------------------------------------------------------------
# 1. Correlation guard
# ---------------------------------------------------------------------------


def test_record_fill_registers_event_id(conn):
    """Without this the guard's lookup map is permanently empty."""
    gov = RiskGovernor(conn, mode="LIVE")
    gov.set_bankroll(100.0)

    gov.record_fill(market_id="mkt_A", usd=5.0, liquidity="maker", event_id="evt_1")

    assert gov._event_id_by_market == {"mkt_A": "evt_1"}


def test_guard_blocks_second_market_on_same_event(conn):
    gov = RiskGovernor(conn, mode="LIVE")
    gov.set_bankroll(1000.0)
    gov.record_fill(market_id="mkt_A", usd=5.0, liquidity="maker", event_id="evt_1")

    decision = gov.check({
        "size_usd": 5.0,
        "market_id": "mkt_B",          # different market...
        "event_id": "evt_1",           # ...same underlying event
        "category": _allowed_category(),
    })

    assert decision.allowed is False
    assert "correlation_guard" in decision.reason


def test_guard_allows_same_market_to_add(conn):
    """Adding to the market you already hold is not correlation risk."""
    gov = RiskGovernor(conn, mode="LIVE")
    gov.set_bankroll(1000.0)
    gov.record_fill(market_id="mkt_A", usd=5.0, liquidity="maker", event_id="evt_1")

    decision = gov.check({
        "size_usd": 5.0, "market_id": "mkt_A", "event_id": "evt_1",
        "category": _allowed_category(),
    })
    assert decision.allowed is True


def test_guard_allows_unrelated_event(conn):
    gov = RiskGovernor(conn, mode="LIVE")
    gov.set_bankroll(1000.0)
    gov.record_fill(market_id="mkt_A", usd=5.0, liquidity="maker", event_id="evt_1")

    decision = gov.check({
        "size_usd": 5.0, "market_id": "mkt_B", "event_id": "evt_2",
        "category": _allowed_category(),
    })
    assert decision.allowed is True


def test_guard_bypassed_when_event_id_absent(conn):
    """Documented safe default -- a missing event_id must not fail closed."""
    gov = RiskGovernor(conn, mode="LIVE")
    gov.set_bankroll(1000.0)
    gov.record_fill(market_id="mkt_A", usd=5.0, liquidity="maker", event_id="evt_1")

    decision = gov.check({
        "size_usd": 5.0, "market_id": "mkt_B",
        "category": _allowed_category(),
    })
    assert decision.allowed is True


def test_record_close_clears_the_event_entry(conn):
    """Otherwise the guard blocks the event forever after the first close."""
    gov = RiskGovernor(conn, mode="LIVE")
    gov.set_bankroll(1000.0)
    gov.record_fill(market_id="mkt_A", usd=5.0, liquidity="maker", event_id="evt_1")
    gov.record_close(market_id="mkt_A", usd=5.0)

    assert gov._event_id_by_market == {}
    decision = gov.check({
        "size_usd": 5.0, "market_id": "mkt_B", "event_id": "evt_1",
        "category": _allowed_category(),
    })
    assert decision.allowed is True


# ---------------------------------------------------------------------------
# 2. execute_intent must forward event_id and category
# ---------------------------------------------------------------------------


def test_execute_intent_forwards_event_id_to_entry_gate(monkeypatch, conn):
    """The entry gate rebuilds its own intent dict -- it must carry event_id.

    Reproduces the live defect: before the fix the rebuilt dict dropped
    event_id, so Rule 5.5 could never see it no matter what the caller passed.
    """
    from execution import live_executor as le

    seen = {}

    class FakeGov:
        def check(self, intent):
            seen.update(intent)
            from execution.risk_governor import Decision
            return Decision(False, "stop here -- we only need the intent")

        def record_fill(self, **kw):
            pass

    le.execute_intent(
        conn,
        FakeGov(),
        token_id="tok_1",
        side="BUY",
        fair_price=0.36,
        size_usd=5.97,
        tick_size=0.01,
        neg_risk=False,
        net_edge_taker=0.02,
        client_order_ref="test-ref-1",
        category="smart_wallet",
        event_id="evt_42",
    )

    assert seen.get("event_id") == "evt_42"
    assert seen.get("category") == "smart_wallet"


def test_execute_intent_event_id_defaults_to_empty(monkeypatch, conn):
    """Callers that don't supply one must still work (safe bypass)."""
    from execution import live_executor as le

    seen = {}

    class FakeGov:
        def check(self, intent):
            seen.update(intent)
            from execution.risk_governor import Decision
            return Decision(False, "stop")

        def record_fill(self, **kw):
            pass

    le.execute_intent(
        conn, FakeGov(), token_id="tok_1", side="BUY", fair_price=0.5,
        size_usd=5.0, tick_size=0.01, neg_risk=False, net_edge_taker=0.0,
        client_order_ref="test-ref-2", category="weather",
    )

    assert seen.get("event_id") == ""


# ---------------------------------------------------------------------------
# 3. Archetype must reflect the real strategy
# ---------------------------------------------------------------------------


def test_record_real_fill_persists_real_category(conn):
    """The first live fill was a TENNIS market stamped archetype='weather'."""
    from execution import live_position_tracker as lpt

    pid = lpt.record_real_fill(
        conn,
        order_id="0xabc",
        market_id="mkt_tennis",
        market_slug="fritz-vs-nakashima",
        side="BUY",
        liquidity="maker",
        price=0.36,
        shares=16.57,
        usd=5.9652,
        fee_paid=0.0,
        fair_price=0.36,
        token_id="tok_tennis",
        market_title="Cincinnati Open: Taylor Fritz vs Brandon Nakashima",
        category="smart_wallet",
    )

    row = conn.execute(
        "SELECT archetype, market_title FROM live_positions WHERE id = ?", (pid,)
    ).fetchone()
    assert row["archetype"] == "smart_wallet"
    assert row["archetype"] != "weather"


def test_record_real_fill_category_defaults_to_unknown_not_weather(conn):
    """A missing category must be visibly unknown, never silently 'weather'.

    Mislabelling as a real strategy corrupts per-strategy attribution; 'unknown'
    is self-announcing and can be repaired.
    """
    from execution import live_position_tracker as lpt

    pid = lpt.record_real_fill(
        conn, order_id="0xdef", market_id="mkt_x", market_slug="x", side="BUY",
        liquidity="maker", price=0.5, shares=10.0, usd=5.0, fee_paid=0.0,
        fair_price=0.5,
    )
    row = conn.execute(
        "SELECT archetype FROM live_positions WHERE id = ?", (pid,)
    ).fetchone()
    assert row["archetype"] == "unknown"


def test_weather_path_still_labels_weather(conn):
    """Guard against the fix regressing the path the old literal was right for."""
    from execution import live_position_tracker as lpt

    pid = lpt.record_real_fill(
        conn, order_id="0x111", market_id="mkt_w", market_slug="w", side="BUY",
        liquidity="maker", price=0.5, shares=10.0, usd=5.0, fee_paid=0.0,
        fair_price=0.5, category="weather_resolution",
    )
    row = conn.execute(
        "SELECT archetype FROM live_positions WHERE id = ?", (pid,)
    ).fetchone()
    assert row["archetype"] == "weather_resolution"

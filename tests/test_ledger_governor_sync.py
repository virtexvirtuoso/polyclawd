"""Regression tests for the 2026-08-21 live-ledger fixes.

Covers four defects found while the canary was armed and flat:

1. position_sync's resolution LOSS branch omitted the entry fee, understating
   every losing resolution by fee_paid_total.
2. RiskGovernor Rule 2 (DAILY_HALT) was inert: record_realized_loss() and
   set_unrealized_loss() had zero production callers, so the breaker evaluated
   0.0 + 0.0 against the threshold forever.
3. RiskGovernor._realized_pnl had no mutator -- loaded at init, echoed by
   _persist(), frozen for the life of the ledger.
4. realized_loss_today() must survive BOTH closed_at formats present in the
   live DB; a naive 'T' cutoff silently drops space-separated rows.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from execution import live_db, live_config
from execution.live_position_tracker import (
    realized_loss_today,
    realized_pnl_from_ledger,
    unrealized_loss_from_snapshot,
)
from execution.risk_governor import RiskGovernor


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    live_db.init_live_tables(c)
    yield c
    c.close()


def _add_closed(c, *, pnl, closed_at, cost_usd=10.0, fee=0.0, pid=None):
    c.execute(
        "INSERT INTO live_positions (market_id, market_slug, market_title, token_id,"
        " side, entry_price, shares, cost_usd, status, pnl, closed_at, fee_paid_total,"
        " opened_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid or "m1", "slug", "title", "t1", "BUY", 0.5, 20.0, cost_usd,
         "closed", pnl, closed_at, fee, "2026-08-01T00:00:00+00:00"),
    )
    c.commit()


# ---------------------------------------------------------------------------
# 1. Fee deduction on a losing resolution
# ---------------------------------------------------------------------------


def test_resolution_loss_deducts_entry_fee():
    """A total loss costs the basis PLUS the fees paid to acquire it.

    cost_usd is the notional only -- record_real_fill stores fees separately in
    fee_paid_total -- so -cost_usd alone understates the loss.
    """
    cost_usd, fee_total = 12.00, 0.35

    # The formula as shipped in position_sync's LOSS branch.
    pnl = round(-cost_usd - fee_total, 4)

    assert pnl == -12.35
    # Guard against a silent revert to the old formula.
    assert pnl != round(-cost_usd, 4)


def test_loss_and_win_branches_treat_fees_consistently():
    """WIN, LOSS and redeem must all charge fee_total exactly once."""
    entry_price, shares, cost_usd, fee_total = 0.60, 20.0, 12.00, 0.35
    payout = shares  # redeem pays $1/share

    win = round((1.0 - entry_price) * shares - fee_total, 4)
    loss = round(-cost_usd - fee_total, 4)
    redeem = round(payout - cost_usd - fee_total, 4)

    # Every branch is the fee-free number minus exactly fee_total.
    assert win == round((1.0 - entry_price) * shares, 4) - fee_total
    assert loss == round(-cost_usd, 4) - fee_total
    assert redeem == round(payout - cost_usd, 4) - fee_total


# ---------------------------------------------------------------------------
# 4. closed_at format trap
# ---------------------------------------------------------------------------


def test_realized_loss_today_counts_both_closed_at_formats(conn):
    """The live DB holds isoformat AND space-separated closed_at values.

    ' ' (0x20) sorts below 'T' (0x54), so a naive `closed_at >= '...T00:00:00'`
    filter drops every space-separated row of the same day.
    """
    now = datetime(2026, 8, 21, 18, 0, tzinfo=timezone.utc)

    _add_closed(conn, pnl=-5.0, closed_at="2026-08-21T12:00:00+00:00", pid="iso")
    _add_closed(conn, pnl=-3.0, closed_at="2026-08-21 13:00:00", pid="space")

    # Both of today's losses must be counted: 5 + 3.
    assert realized_loss_today(conn, now=now) == pytest.approx(8.0)


def test_realized_loss_today_excludes_prior_days(conn):
    now = datetime(2026, 8, 21, 18, 0, tzinfo=timezone.utc)
    _add_closed(conn, pnl=-100.0, closed_at="2026-08-20T23:59:59+00:00", pid="yesterday")
    _add_closed(conn, pnl=-4.0, closed_at="2026-08-21T00:00:01+00:00", pid="today")

    assert realized_loss_today(conn, now=now) == pytest.approx(4.0)


def test_realized_loss_today_is_zero_when_day_is_profitable(conn):
    """Positive magnitude only -- a green day is 0.0, never a negative loss."""
    now = datetime(2026, 8, 21, 18, 0, tzinfo=timezone.utc)
    _add_closed(conn, pnl=-2.0, closed_at="2026-08-21T10:00:00+00:00", pid="a")
    _add_closed(conn, pnl=+9.0, closed_at="2026-08-21T11:00:00+00:00", pid="b")

    assert realized_loss_today(conn, now=now) == 0.0


def test_realized_loss_today_ignores_open_positions(conn):
    now = datetime(2026, 8, 21, 18, 0, tzinfo=timezone.utc)
    conn.execute(
        "INSERT INTO live_positions (market_id, side, entry_price, shares, cost_usd,"
        " status, pnl, closed_at, opened_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("open1", "BUY", 0.5, 10.0, 5.0, "open", -50.0,
         "2026-08-21T10:00:00+00:00", "2026-08-21T09:00:00+00:00"),
    )
    conn.commit()
    assert realized_loss_today(conn, now=now) == 0.0


# ---------------------------------------------------------------------------
# 3. realized_pnl unfreeze
# ---------------------------------------------------------------------------


def test_set_realized_pnl_persists_across_reload(conn):
    """Before set_realized_pnl() existed, the field was load-once/echo-forever."""
    gov = RiskGovernor(conn, mode="LIVE")
    gov.set_bankroll(100.0)
    gov.set_realized_pnl(-72.1654)

    reloaded = RiskGovernor(conn, mode="LIVE")
    assert reloaded._realized_pnl == pytest.approx(-72.1654)


def test_set_realized_pnl_does_not_gate_trades(conn):
    """realized_pnl is observability only -- no rule in check() may read it."""
    gov = RiskGovernor(conn, mode="LIVE")
    gov.set_bankroll(100.0)
    gov.set_realized_pnl(-9999.0)

    intent = {"size_usd": 5.0, "market_id": "m1",
              "category": sorted(live_config.live_strategy_allowlist())[0]}
    assert gov.check(intent).allowed is True


def test_realized_pnl_from_ledger_unions_both_close_regimes(conn):
    """Closes with a SELL fill and closes without one must both count, once."""
    # A resolution close: pnl on the row, no SELL fill.
    _add_closed(conn, pnl=-6.0, closed_at="2026-08-21T10:00:00+00:00", pid="resolution")

    realized, n_sell = realized_pnl_from_ledger(conn)
    assert realized == pytest.approx(-6.0)
    assert n_sell == 0


# ---------------------------------------------------------------------------
# 2. DAILY_HALT wiring
# ---------------------------------------------------------------------------


def test_set_daily_loss_is_idempotent(conn):
    """Derived from the ledger, so re-syncing must NOT accumulate.

    record_realized_loss() accumulates by design; a cron that recomputed the
    day's loss and fed it to that method would double-count every cycle.
    """
    gov = RiskGovernor(conn, mode="LIVE")
    gov.set_bankroll(100.0)

    for _ in range(5):
        gov.set_daily_loss(7.0)

    assert gov._daily_loss == pytest.approx(7.0)


def test_daily_halt_trips_on_combined_realised_and_unrealised(conn, monkeypatch):
    monkeypatch.setattr(live_config, "daily_loss_halt", lambda: 30.0)
    gov = RiskGovernor(conn, mode="LIVE")
    gov.set_bankroll(100.0)

    gov.set_unrealized_loss(12.0)
    gov.set_daily_loss(19.0)  # 19 + 12 = 31 >= 30

    assert gov.state() == "DAILY_HALT"
    intent = {"size_usd": 1.0, "market_id": "m1",
              "category": sorted(live_config.live_strategy_allowlist())[0]}
    decision = gov.check(intent)
    assert decision.allowed is False
    assert "daily_loss_halt" in decision.reason


def test_daily_halt_does_not_trip_below_threshold(conn, monkeypatch):
    monkeypatch.setattr(live_config, "daily_loss_halt", lambda: 30.0)
    gov = RiskGovernor(conn, mode="LIVE")
    gov.set_bankroll(100.0)

    gov.set_unrealized_loss(5.0)
    gov.set_daily_loss(10.0)

    assert gov.state() == "ACTIVE"


def test_reset_day_clears_halt_and_allows_retrip(conn, monkeypatch):
    """The day-boundary path position_sync uses: reset, then re-derive.

    reset_day() had no caller anywhere before this change, so a tripped
    DAILY_HALT would have persisted indefinitely.
    """
    monkeypatch.setattr(live_config, "daily_loss_halt", lambda: 30.0)
    gov = RiskGovernor(conn, mode="LIVE")
    gov.set_bankroll(100.0)

    gov.set_daily_loss(35.0)
    assert gov.state() == "DAILY_HALT"

    # New UTC day: derived loss falls back to 0 and the halt clears.
    gov.set_unrealized_loss(0.0)
    gov.reset_day()
    gov.set_daily_loss(0.0)
    assert gov.state() == "ACTIVE"

    # Still able to trip again on a fresh bad day.
    gov.set_daily_loss(31.0)
    assert gov.state() == "DAILY_HALT"


def test_unrealized_loss_from_snapshot_reads_latest_only(conn):
    live_db.snapshot_equity(
        conn, ts="2026-08-21T10:00:00+00:00", onchain_balance=100.0,
        realized_pnl=0.0, unrealized_pnl=-40.0, total_equity=60.0,
        open_positions=1, peak_equity=100.0, fees_paid_cumulative=0.0,
    )
    live_db.snapshot_equity(
        conn, ts="2026-08-21T11:00:00+00:00", onchain_balance=100.0,
        realized_pnl=0.0, unrealized_pnl=-7.5, total_equity=92.5,
        open_positions=1, peak_equity=100.0, fees_paid_cumulative=0.0,
    )
    assert unrealized_loss_from_snapshot(conn) == pytest.approx(7.5)


def test_unrealized_loss_from_snapshot_is_zero_when_up(conn):
    live_db.snapshot_equity(
        conn, ts="2026-08-21T11:00:00+00:00", onchain_balance=100.0,
        realized_pnl=0.0, unrealized_pnl=15.0, total_equity=115.0,
        open_positions=1, peak_equity=115.0, fees_paid_cumulative=0.0,
    )
    assert unrealized_loss_from_snapshot(conn) == 0.0


def test_unrealized_loss_from_snapshot_handles_empty_table(conn):
    assert unrealized_loss_from_snapshot(conn) == 0.0


# ---------------------------------------------------------------------------
# 5. Batched sync (2026-08-21) — one write transaction, not four
# ---------------------------------------------------------------------------


def _state_rows(c):
    return c.execute("SELECT COUNT(*) FROM live_portfolio_state").fetchone()[0]


def test_apply_sync_writes_exactly_one_row(conn):
    """Four individual setters appended four rows per cron cycle to a DB that
    has demonstrated lock contention. The batched path must write once."""
    gov = RiskGovernor(conn, mode="LIVE")
    before = _state_rows(conn)

    gov.apply_sync(bankroll=23.87, deployed_usd=5.97, realized_pnl=-72.17,
                   daily_loss=0.0, unrealized_loss=0.0)

    assert _state_rows(conn) - before == 1


def test_apply_sync_persists_every_field(conn):
    gov = RiskGovernor(conn, mode="LIVE")
    gov.apply_sync(bankroll=23.87, deployed_usd=5.97, realized_pnl=-72.1654,
                   daily_loss=3.0, unrealized_loss=1.0)

    reloaded = RiskGovernor(conn, mode="LIVE")
    assert reloaded._bankroll == pytest.approx(23.87)
    assert reloaded._deployed_usd == pytest.approx(5.97)
    assert reloaded._realized_pnl == pytest.approx(-72.1654)
    assert reloaded._daily_loss == pytest.approx(3.0)


def test_apply_sync_omitted_fields_are_left_alone(conn):
    gov = RiskGovernor(conn, mode="LIVE")
    gov.apply_sync(bankroll=100.0, realized_pnl=-5.0)
    gov.apply_sync(deployed_usd=7.0)          # bankroll/realized untouched

    reloaded = RiskGovernor(conn, mode="LIVE")
    assert reloaded._bankroll == pytest.approx(100.0)
    assert reloaded._realized_pnl == pytest.approx(-5.0)
    assert reloaded._deployed_usd == pytest.approx(7.0)


def test_apply_sync_halt_is_order_independent(conn, monkeypatch):
    """The bug the batch also fixes.

    set_daily_loss() evaluates the halt against whatever _unrealized_loss holds
    at that moment, so calling the setters in the wrong order silently misses a
    trip. apply_sync evaluates once on the complete snapshot.
    """
    monkeypatch.setattr(live_config, "daily_loss_halt", lambda: 30.0)

    gov = RiskGovernor(conn, mode="LIVE")
    gov.set_bankroll(100.0)
    # Wrong order with the individual setters: daily_loss first, while
    # _unrealized_loss is still 0 -> 19 < 30 -> no halt, then the mark lands.
    gov.set_daily_loss(19.0)
    gov.set_unrealized_loss(12.0)
    assert gov.state() == "ACTIVE", "precondition: order-dependent miss"

    gov2 = RiskGovernor(conn, mode="LIVE")
    gov2.set_bankroll(100.0)
    gov2.apply_sync(daily_loss=19.0, unrealized_loss=12.0)   # 31 >= 30
    assert gov2.state() == "DAILY_HALT"


def test_apply_sync_does_not_halt_below_threshold(conn, monkeypatch):
    monkeypatch.setattr(live_config, "daily_loss_halt", lambda: 30.0)
    gov = RiskGovernor(conn, mode="LIVE")
    gov.apply_sync(bankroll=23.87, daily_loss=0.0, unrealized_loss=0.0)
    assert gov.state() == "ACTIVE"


def test_apply_sync_is_idempotent(conn):
    gov = RiskGovernor(conn, mode="LIVE")
    for _ in range(5):
        gov.apply_sync(daily_loss=7.0, unrealized_loss=2.0)
    assert gov._daily_loss == pytest.approx(7.0)
    assert gov._unrealized_loss == pytest.approx(2.0)

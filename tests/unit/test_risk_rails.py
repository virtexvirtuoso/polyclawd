"""Regression tests for the money rails: RiskGovernor state machine
(kill floor / daily halt / per-trade / deployed caps) and scaling-phase
daily limits. Pure + offline. Run:
    venv/bin/python -m pytest tests/unit/test_risk_rails.py -q --noconftest
"""

import sqlite3

import pytest

from execution import live_db
from execution.risk_governor import RiskGovernor
from config.scaling_phases import (
    Phase,
    get_phase,
    get_phase_config,
    check_daily_limits,
)


@pytest.fixture
def gov(monkeypatch):
    # Pin thresholds so env/config files can't skew assertions.
    monkeypatch.setenv("POLYCLAWD_DAILY_LOSS_HALT", "50.0")
    monkeypatch.setenv("POLYCLAWD_KILL_FLOOR", "250.0")
    monkeypatch.setenv("POLYCLAWD_WEATHER_PER_TRADE_CAP", "100.0")
    monkeypatch.setenv("POLYCLAWD_MAX_DEPLOYED_FRAC", "0.60")
    monkeypatch.delenv("POLYCLAWD_MAX_DEPLOYED_USD", raising=False)
    monkeypatch.delenv("POLYCLAWD_MAX_OPEN_MARKETS", raising=False)
    # Rule 0 (strategy allowlist, Task 3) requires an allowlisted category on
    # every intent. This suite tests the money rails downstream of Rule 0, so
    # pin an allowlisted category rather than weaken the gate.
    monkeypatch.setenv("POLYCLAWD_LIVE_STRATEGY_ALLOWLIST", "baseball_total")
    # Silence the alert side-channel — KILL/DAILY_HALT transitions would
    # otherwise append synthetic lines to storage/alerts.jsonl.
    monkeypatch.setattr("execution.risk_governor._alert", lambda msg: None)
    conn = sqlite3.connect(":memory:")
    live_db.init_live_tables(conn)
    g = RiskGovernor(conn, mode="PAPER")
    g.set_bankroll(1000.0)
    return g


def _intent(size_usd, market_id="m1"):
    return {"size_usd": size_usd, "market_id": market_id, "category": "baseball_total"}


class TestDailyHalt:
    def test_combined_loss_trips_halt(self, gov):
        gov.record_realized_loss(30.0)
        gov.set_unrealized_loss(25.0)  # 30 + 25 >= 50
        d = gov.check(_intent(10))
        assert not d.allowed and "daily_loss_halt" in d.reason
        assert gov.state() == "DAILY_HALT"

    def test_halt_is_sticky_until_reset_day(self, gov):
        gov.record_realized_loss(60.0)
        assert not gov.check(_intent(10)).allowed
        gov.set_unrealized_loss(0.0)
        # Zero the raw counter directly (no public API does this without
        # clearing HALT) — proves stickiness comes from persisted state,
        # not from numbers still breaching.
        gov._daily_loss = 0.0
        assert not gov.check(_intent(10)).allowed
        gov.reset_day()
        assert gov.check(_intent(10)).allowed


class TestKillFloor:
    def test_bankroll_below_floor_kills(self, gov):
        gov.set_bankroll(200.0)  # < 250 floor
        d = gov.check(_intent(10))
        assert not d.allowed and "kill_floor" in d.reason
        assert gov.state() == "KILL"

    def test_kill_is_sticky_and_beats_daily_halt(self, gov):
        gov.set_bankroll(200.0)
        gov.record_realized_loss(30.0)  # below halt threshold — isolates the kill rail
        d = gov.check(_intent(10))
        assert "kill_floor" in d.reason  # rule order: KILL wins
        gov.set_bankroll(1000.0)  # recovery does NOT auto-clear
        assert not gov.check(_intent(10)).allowed
        gov.reset_kill()
        assert gov.check(_intent(10)).allowed

    def test_kill_with_coincident_daily_breach_needs_both_resets(self, gov):
        """Defense-in-depth: reset_kill() alone does NOT resume trading if a
        daily-loss breach occurred during the KILL window — reset_day() is
        also required. Locks in the documented two-key recovery."""
        gov.set_bankroll(200.0)
        gov.record_realized_loss(60.0)  # breaches the $50 daily halt too
        assert not gov.check(_intent(10)).allowed
        gov.set_bankroll(1000.0)
        gov.reset_kill()
        d = gov.check(_intent(10))
        assert not d.allowed and "daily_loss_halt" in d.reason
        gov.reset_day()
        assert gov.check(_intent(10)).allowed


class TestTradeCaps:
    def test_exactly_at_cap_allowed_above_denied(self, gov):
        assert gov.check(_intent(100.0)).allowed  # strict >
        d = gov.check(_intent(100.01))
        assert not d.allowed and "per_trade_cap" in d.reason

    def test_deployed_cap(self, gov):
        gov.record_fill("m0", 550.0)  # 550 + 100 > 600 (60% of 1000)
        d = gov.check(_intent(100.0))
        assert not d.allowed and "max_deployed" in d.reason


class TestPhaseLimits:
    def test_phase_boundaries(self):
        assert get_phase(999.99) == Phase.SEED
        assert get_phase(1_000) == Phase.GROWTH
        assert get_phase(10_000) == Phase.ACCELERATION
        assert get_phase(100_000) == Phase.PRESERVATION

    def test_daily_loss_limit_blocks_trading(self):
        balance = 5_000.0
        cfg = get_phase_config(balance)
        at_limit = check_daily_limits(
            balance=balance,
            daily_pnl=-(balance * cfg.max_daily_loss_pct),
            daily_trades=0,
            current_exposure=0.0,
        )
        assert at_limit["can_trade"] is False
        assert at_limit["limit_type"] == "daily_loss"

    def test_small_loss_allows_trading(self):
        ok = check_daily_limits(balance=5_000.0, daily_pnl=-1.0, daily_trades=0, current_exposure=0.0)
        assert ok["can_trade"] is True

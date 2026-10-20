"""The startup live-gate check must size each allowlisted strategy the way its executor does.

On 2026-10-20 it logged "LIVE GATE INERT ... Every intent will be rejected" at every start,
because it compared the per-trade cap ($2.47) to the soccer/weather tier size ($3.50). The
strategy that actually trades, smart_wallet, sends $2.24 orders and fit under the cap; the real
blocker was the deployment cap, which the alarm sent the reader away from."""

import logging
import sqlite3

import services.scheduler as sched
from scripts import smart_wallet_fast_poll as sw

ALLOW = "smart_wallet,baseball_total,soccer_match_3way"


def _setup(monkeypatch, tmp_path, frac, bankroll=22.43902, allow=ALLOW):
    db = tmp_path / "shadow_trades.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE live_portfolio_state (id INTEGER PRIMARY KEY, bankroll REAL)")
    con.execute("INSERT INTO live_portfolio_state (bankroll) VALUES (?)", (bankroll,))
    con.commit()
    con.close()
    monkeypatch.setattr(sched, "DB_PATH", db)
    monkeypatch.setenv("POLYCLAWD_PER_TRADE_FRAC", str(frac))
    monkeypatch.setenv("POLYCLAWD_TIER_BASE_USD", "3.5")
    monkeypatch.setenv("POLYCLAWD_LIVE_STRATEGY_ALLOWLIST", allow)
    for var in ("POLYCLAWD_WEATHER_PER_TRADE_CAP", "POLYCLAWD_TIER_SIZE_CAP"):
        monkeypatch.delenv(var, raising=False)


def _gate_records(caplog):
    return [r for r in caplog.records if r.name == "scheduler" and "LIVE GATE" in r.getMessage()]


def test_smart_wallet_fits_so_no_inert_error(monkeypatch, tmp_path, caplog):
    # The 2026-10-20 production state: cap 22.44 x 0.11 = $2.47, smart_wallet $2.24, tier $3.50.
    _setup(monkeypatch, tmp_path, frac=0.11)
    caplog.set_level(logging.INFO)
    sched._assert_live_cap_fits_min_action()
    recs = _gate_records(caplog)
    assert not [r for r in recs if r.levelno >= logging.ERROR], [r.getMessage() for r in recs]
    partial = [r.getMessage() for r in recs if r.levelno == logging.WARNING]
    assert len(partial) == 1, [r.getMessage() for r in recs]
    blocked, trading = partial[0].split("trading:")
    assert "soccer_match_3way $3.50" in blocked
    assert "smart_wallet $2.24" in trading
    assert any("baseball_total" in r.getMessage() and "no live executor" in r.getMessage() for r in recs)


def test_every_strategy_too_big_is_still_an_error(monkeypatch, tmp_path, caplog):
    # Cap 22.44 x 0.05 = $1.12 is below both smart_wallet ($2.24) and the tier ($3.50).
    _setup(monkeypatch, tmp_path, frac=0.05)
    caplog.set_level(logging.INFO)
    sched._assert_live_cap_fits_min_action()
    errors = [r.getMessage() for r in _gate_records(caplog) if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and "LIVE GATE INERT" in errors[0]
    assert "smart_wallet $2.24" in errors[0] and "soccer_match_3way $3.50" in errors[0]


def test_everything_fits_is_ok(monkeypatch, tmp_path, caplog):
    # Cap 22.44 x 0.20 = $4.49 fits both.
    _setup(monkeypatch, tmp_path, frac=0.20)
    caplog.set_level(logging.INFO)
    sched._assert_live_cap_fits_min_action()
    recs = _gate_records(caplog)
    assert not [r for r in recs if r.levelno >= logging.WARNING], [r.getMessage() for r in recs]
    assert any("LIVE GATE OK" in r.getMessage() for r in recs)


def test_sizer_is_the_one_fast_poll_uses():
    # Floor, fraction and cap arms of the shared sizer.
    assert sw._sw_live_size_usd(10.0) == sw._SW_LIVE_MIN_USD
    assert abs(sw._sw_live_size_usd(22.43902) - 2.243902) < 1e-9
    assert sw._sw_live_size_usd(1000.0) == sw._SW_LIVE_MAX_USD

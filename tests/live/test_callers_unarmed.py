"""Task 1.10, reader 2: the five caller-side mode checks use live_armed().

Each caller is driven three ways: under PAPER, under LIVE-but-unarmed and armed.
The two unarmed states must give the same observable result (the PAPER branch);
the armed state must give a different one (the live branch), so the test cannot
pass by a caller being broken.
"""

import pytest

from execution import soccer_executor, weather_executor
from scripts import smart_wallet_fast_poll as sw
from services import stop_evaluator
from signals import paper_portfolio as pp

# (mode, enforce flag, LIVE_ORDER_PATH_READY)
UNARMED = {
    "paper": ("PAPER", None, False),
    "paper_everything_else_true": ("PAPER", "1", True),
    "live_flag_unset": ("LIVE", None, True),
    "live_flag_1_path_not_ready": ("LIVE", "1", False),
    "live_flag_true_word": ("LIVE", "true", True),
}
ARMED = ("LIVE", "1", True)


# --- observers: run the caller, return something that tells the branch apart ----


def _observe_smart_wallet(monkeypatch, live_db):
    """_route_live_smart_wallet returns None either way; the live branch reaches the
    sizing step (patched to a recorder), the PAPER branch returns before it."""
    reached = []
    monkeypatch.setattr(sw, "_sw_live_size_usd", lambda bankroll: reached.append(bankroll) or 1.0)
    monkeypatch.setattr(sw, "_check_governor_rails", lambda bankroll, size: None)
    assert sw._route_live_smart_wallet([], {}) is None
    return {"reached_sizing": len(reached)}


def _observe_soccer(monkeypatch, live_db):
    return soccer_executor.execute_tradeable_soccer_edges([])


def _observe_weather(monkeypatch, live_db):
    return weather_executor.execute_tradeable_weather_edges([])


def _observe_paper_portfolio(monkeypatch, live_db):
    """A weather signal under process_signals: the live branch calls _route_live_weather
    (a recorder) and never open_position; the PAPER branch is the reverse."""
    calls = []
    monkeypatch.setattr(pp, "evaluate_signal", lambda sig: {
        "eligible": True, "reason": "Criteria met", "edge": 0.2, "bet_size": 10.0})
    monkeypatch.setattr(pp, "open_position",
                        lambda sig: calls.append("open_position") or {"opened": True})
    monkeypatch.setattr(pp, "_route_live_weather",
                        lambda sig, ev: calls.append("route_live_weather") or {"action": "dropped"})
    monkeypatch.setattr(pp, "get_portfolio_status",
                        lambda: {"bankroll": 1000.0, "open_positions": 0, "total_pnl": 0.0})
    out = pp.process_signals([{
        "market_id": "0xweather1", "market_title": "Highest temperature in X", "side": "YES",
        "price": 0.4, "archetype": "weather"}])
    return {"calls": calls, "action": out["signals"][0]["action"]}


def _observe_stop_evaluator(monkeypatch, live_db):
    """An open live position exists; the PAPER branch does not look it up."""
    live_db.conn.execute(
        "INSERT INTO live_positions (market_id, status, side, shares) VALUES ('0xm1', 'open', 'BUY', 5)")
    live_db.conn.commit()
    row = stop_evaluator._get_live_position("0xm1")
    return {"found": None if row is None else row["market_id"]}


CALLERS = {
    "smart_wallet_fast_poll._route_live_smart_wallet": (
        _observe_smart_wallet, {"reached_sizing": 0}, {"reached_sizing": 1}),
    "soccer_executor.execute_tradeable_soccer_edges": (
        _observe_soccer,
        {"filled": 0, "dropped": 0, "skipped": 0, "errors": 0, "reason": "not armed"},
        {"filled": 0, "dropped": 0, "skipped": 0, "errors": 0}),
    "weather_executor.execute_tradeable_weather_edges": (
        _observe_weather,
        {"filled": 0, "dropped": 0, "skipped": 0, "errors": 0, "reason": "not armed"},
        {"filled": 0, "dropped": 0, "skipped": 0, "errors": 0}),
    "paper_portfolio.process_signals": (
        _observe_paper_portfolio,
        {"calls": ["open_position"], "action": "opened"},
        {"calls": ["route_live_weather"], "action": "skipped"}),
    "stop_evaluator._get_live_position": (
        _observe_stop_evaluator, {"found": None}, {"found": "0xm1"}),
}


@pytest.mark.parametrize("caller", sorted(CALLERS))
@pytest.mark.parametrize("state", sorted(UNARMED))
def test_unarmed_takes_the_paper_branch(caller, state, mode_env, monkeypatch, live_db):
    observe, paper_result, _ = CALLERS[caller]
    mode_env(*UNARMED[state])
    assert observe(monkeypatch, live_db) == paper_result


@pytest.mark.parametrize("caller", sorted(CALLERS))
def test_armed_takes_the_live_branch(caller, mode_env, monkeypatch, live_db):
    observe, paper_result, live_result = CALLERS[caller]
    mode_env(*ARMED)
    out = observe(monkeypatch, live_db)
    assert out == live_result
    assert out != paper_result


def test_unarmed_results_are_identical_to_paper(mode_env, monkeypatch, live_db):
    """The brief's one rule: LIVE-but-unarmed behaves as PAPER, for every caller."""
    for caller, (observe, _, _) in sorted(CALLERS.items()):
        mode_env(*UNARMED["paper"])
        under_paper = observe(monkeypatch, live_db)
        mode_env(*UNARMED["live_flag_1_path_not_ready"])
        assert observe(monkeypatch, live_db) == under_paper, caller

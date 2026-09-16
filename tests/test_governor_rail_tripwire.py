"""Every governor rail that can deny 100% of orders must alarm.

The 2026-08-21..25 outage ran four days because `per_trade_cap` denied every
order and nothing said so — the watchdog only printed "quiet". `/qa` then found
the tripwire built in response asserted ONLY that one rule, leaving the other
four rails with exactly the same silent-100%-denial shape.

No network, no real DB — the persisted governor row and live_config are stubbed.
"""

import ast
import inspect

import pytest

import scripts.smart_wallet_fast_poll as sw

BANKROLL = 34.48
SIZE = 3.45


@pytest.fixture(autouse=True)
def _reset_rail_state():
    sw._rail_alert_last.clear()
    yield
    sw._rail_alert_last.clear()


@pytest.fixture
def rig(monkeypatch):
    """Stub the governor row + config. Returns a dict the test can mutate."""
    cfg = {
        "governor_state": "ACTIVE",
        "deployed_usd": 0.0,
        "kill_floor": 10.0,
        "daily_loss_halt": 30.0,
        "max_deployed_frac": 0.60,
        "max_deployed_usd": None,
        "allowlist": {"smart_wallet", "baseball_total", "soccer_match_3way"},
        # Rule 3 belongs to _check_sizing_deadlock and has its own suite; pin it
        # to the PRODUCTION values so it stays silent here. Note the sizer uses
        # 10% and the governor 11% — that 1-point gap is what keeps size under
        # cap, and it comes from POLYCLAWD_PER_TRADE_FRAC=0.11 in
        # /etc/default/polyclawd, NOT from config/polymarket.env (which says
        # 0.10). A bare `python` reading the config file computes a cap 10% too
        # low, so never reason about this rail outside the service environment.
        "per_trade_frac": 0.11,
        "per_trade_cap": 15.0,
    }

    class _Conn:
        def close(self):
            pass

    monkeypatch.setattr("execution.live_db.connect", lambda *a, **k: _Conn())
    monkeypatch.setattr("execution.live_db.get_state", lambda conn: {
        "governor_state": cfg["governor_state"],
        "deployed_usd": cfg["deployed_usd"],
    })
    monkeypatch.setattr("execution.live_config.per_trade_frac", lambda: cfg["per_trade_frac"])
    monkeypatch.setattr("execution.live_config.per_trade_cap", lambda: cfg["per_trade_cap"])
    monkeypatch.setattr("execution.live_config.kill_floor", lambda: cfg["kill_floor"])
    monkeypatch.setattr("execution.live_config.daily_loss_halt", lambda: cfg["daily_loss_halt"])
    monkeypatch.setattr("execution.live_config.max_deployed_frac", lambda: cfg["max_deployed_frac"])
    monkeypatch.setattr("execution.live_config.max_deployed_usd", lambda: cfg["max_deployed_usd"])
    monkeypatch.setattr("execution.live_config.live_strategy_allowlist", lambda: cfg["allowlist"])
    return cfg


def _rails(bankroll=BANKROLL, size=SIZE):
    return {r for r, _, _ in sw._governor_rail_blocks(bankroll, size)}


# --------------------------------------------------------------------------- #
# The healthy case must be genuinely silent, or every test below is vacuous.
# --------------------------------------------------------------------------- #

def test_a_healthy_governor_reports_no_blocking_rail(rig):
    assert _rails() == set()


# --------------------------------------------------------------------------- #
# One test per rail. Each of these denied 100% of orders with no alert.
# --------------------------------------------------------------------------- #

def test_rule1_sticky_kill_is_reported(rig):
    """KILL survives restarts and needs a MANUAL reset_kill() — nothing in the
    tree clears it, so an unnoticed KILL is permanent."""
    rig["governor_state"] = "KILL"
    assert "kill_sticky" in _rails()


def test_rule1_kill_floor_breach_is_reported(rig):
    assert _rails(bankroll=rig["kill_floor"] - 0.01) == {"kill_floor"}


def test_rule1_at_the_floor_exactly_is_not_blocking(rig):
    """The rule is `bankroll < kill_floor`, so equality must pass — an
    off-by-one here would page on a perfectly tradeable account."""
    assert "kill_floor" not in _rails(bankroll=rig["kill_floor"])


def test_rule2_daily_halt_is_reported(rig):
    rig["governor_state"] = "DAILY_HALT"
    assert "daily_halt" in _rails()


def test_rule2_recovery_text_names_position_sync(rig):
    """DAILY_HALT is auto-cleared by position_sync and by nothing else, so the
    page must say so — otherwise the operator waits for a reset that never comes."""
    rig["governor_state"] = "DAILY_HALT"
    recovery = [rec for r, _, rec in sw._governor_rail_blocks(BANKROLL, SIZE) if r == "daily_halt"][0]
    assert "position_sync" in recovery


def test_rule4_max_deployed_is_reported(rig):
    rig["deployed_usd"] = rig["max_deployed_frac"] * BANKROLL
    assert "max_deployed" in _rails()


def test_rule4_respects_the_absolute_usd_cap(rig):
    """max_deployed_usd is an ADDITIONAL cap; ignoring it would let the tripwire
    miss a rail the governor actually enforces."""
    rig["max_deployed_usd"] = 1.0
    assert "max_deployed" in _rails()


def test_rule4_headroom_is_not_reported(rig):
    rig["deployed_usd"] = 1.0
    assert "max_deployed" not in _rails()


def test_rule0_strategy_allowlist_is_reported(rig):
    """Fail-closed: a category missing from the allowlist rejects every order of
    that strategy, silently and permanently."""
    rig["allowlist"] = {"baseball_total"}
    assert "strategy_allowlist" in _rails()


def test_rule0_uses_the_category_the_executor_actually_sends(rig):
    """A tripwire checking a DIFFERENT string than the executor sends would pass
    while the governor rejects everything."""
    import scripts.smart_wallet_fast_poll as m

    src = inspect.getsource(m._route_live_smart_wallet)
    assert f'"category": "{sw._SW_LIVE_STRATEGY}"' in src or \
           f'category="{sw._SW_LIVE_STRATEGY}"' in src, (
        "the rail checks a strategy name the routing code does not send"
    )


# --------------------------------------------------------------------------- #
# Interaction: the whole point is that one rail must not mask another.
# --------------------------------------------------------------------------- #

def test_every_blocking_rail_is_reported_not_just_the_first(rig):
    """The governor short-circuits on the FIRST failing rule, so its own reason
    string can only ever name one. The tripwire must not inherit that blindness
    or fixing rail A reveals rail B only after another silent outage."""
    rig["governor_state"] = "KILL"
    rig["allowlist"] = {"baseball_total"}
    rig["deployed_usd"] = 999.0
    assert _rails() == {"kill_sticky", "strategy_allowlist", "max_deployed"}


def test_the_cooldown_is_per_rail_not_global(rig, monkeypatch):
    sent = []
    monkeypatch.setattr("scripts.alert_formatter.send_telegram",
                        lambda m: (sent.append(m), True)[1])
    rig["governor_state"] = "KILL"
    sw._check_governor_rails(BANKROLL, SIZE)
    assert len(sent) == 1

    rig["allowlist"] = {"baseball_total"}   # a SECOND rail trips inside the cooldown
    sw._check_governor_rails(BANKROLL, SIZE)
    assert len(sent) == 2, "a global cooldown let the first rail mask the second"
    assert "strategy_allowlist" in sent[1]


def test_the_same_rail_does_not_page_every_poll(rig, monkeypatch):
    sent = []
    monkeypatch.setattr("scripts.alert_formatter.send_telegram",
                        lambda m: (sent.append(m), True)[1])
    rig["governor_state"] = "KILL"
    for _ in range(10):
        sw._check_governor_rails(BANKROLL, SIZE)
    assert len(sent) == 1


def test_an_undelivered_page_is_retried(rig, monkeypatch):
    sent = []
    monkeypatch.setattr("scripts.alert_formatter.send_telegram",
                        lambda m: (sent.append(m), False)[1])
    rig["governor_state"] = "KILL"
    sw._check_governor_rails(BANKROLL, SIZE)
    sw._check_governor_rails(BANKROLL, SIZE)
    assert len(sent) == 2, "a failed send stamped the cooldown"


def test_a_healthy_governor_pages_nothing(rig, monkeypatch):
    sent = []
    monkeypatch.setattr("scripts.alert_formatter.send_telegram",
                        lambda m: (sent.append(m), True)[1])
    sw._check_governor_rails(BANKROLL, SIZE)
    assert sent == []


# --------------------------------------------------------------------------- #
# Safety: the tripwire runs on the live path and must not perturb it.
# --------------------------------------------------------------------------- #

def test_a_broken_alert_channel_cannot_break_routing(rig, monkeypatch):
    def boom(_):
        raise RuntimeError("telegram down")

    monkeypatch.setattr("scripts.alert_formatter.send_telegram", boom)
    rig["governor_state"] = "KILL"
    sw._check_governor_rails(BANKROLL, SIZE)  # must not raise


def test_a_broken_governor_read_cannot_break_routing(monkeypatch):
    monkeypatch.setattr("execution.live_db.connect",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
    sw._check_governor_rails(BANKROLL, SIZE)  # must not raise


def test_the_tripwire_never_mutates_governor_state():
    """RiskGovernor.check() TRANSITIONS to KILL/DAILY_HALT as a side effect of
    being asked. A tripwire that called it could trip the very rail it reports."""
    for fn in (sw._governor_rail_blocks, sw._check_governor_rails):
        tree = ast.parse(inspect.getsource(fn))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"check", "set_bankroll", "set_deployed",
                                         "record_realized_loss", "set_daily_loss",
                                         "reset_day", "reset_kill", "_transition",
                                         "_persist", "set_state", "record_fill",
                                         "record_close", "set_realized_pnl"}, (
                    f"{fn.__name__} calls a state-mutating governor method: {node.attr}"
                )
            if isinstance(node, ast.Name):
                assert node.id != "RiskGovernor", (
                    f"{fn.__name__} instantiates RiskGovernor — construction reads, "
                    "but any later call can mutate; read the persisted row instead"
                )


def test_rule3_is_still_asserted(rig):
    """_check_governor_rails must not have DROPPED the original rule-3 check."""
    tree = ast.parse(inspect.getsource(sw._check_governor_rails))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_check_sizing_deadlock" in called


def test_routing_calls_the_rail_check_not_just_the_sizing_check():
    tree = ast.parse(inspect.getsource(sw._route_live_smart_wallet))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_check_governor_rails" in called, "routing still calls only the rule-3 tripwire"

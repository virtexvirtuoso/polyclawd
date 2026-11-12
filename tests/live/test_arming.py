"""Task 1.10: one arming rule, enforced where Polymarket orders are sent.

live_armed() = mode LIVE and POLYCLAWD_LIVE_STOPS_ENFORCE exactly "1" and the code
constant LIVE_ORDER_PATH_READY. Unarmed LIVE behaves as PAPER: post_maker and
cross_taker raise ClobError before touching the client; cancel / get_order /
get_balance stay allowed in every mode.
"""

import itertools

import pytest

from execution import clob_client, live_config
from execution.clob_client import ClobError

TOKEN = "71321045679252212594626385532706912750332728571942532289631379312455583992563"


# --- live_stops_enforce -------------------------------------------------------


def test_enforce_is_true_only_for_exactly_1(monkeypatch):
    monkeypatch.setenv("POLYCLAWD_LIVE_STOPS_ENFORCE", "1")
    assert live_config.live_stops_enforce() is True


@pytest.mark.parametrize("value", [None, "", "0", "true", "yes", "1x", " 1", "1 ", "01", "TRUE", "on", "2"])
def test_enforce_is_false_for_everything_else(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("POLYCLAWD_LIVE_STOPS_ENFORCE", raising=False)
    else:
        monkeypatch.setenv("POLYCLAWD_LIVE_STOPS_ENFORCE", value)
    assert live_config.live_stops_enforce() is False


def test_enforce_reads_the_environment_at_call_time(monkeypatch):
    monkeypatch.delenv("POLYCLAWD_LIVE_STOPS_ENFORCE", raising=False)
    assert live_config.live_stops_enforce() is False
    monkeypatch.setenv("POLYCLAWD_LIVE_STOPS_ENFORCE", "1")
    assert live_config.live_stops_enforce() is True


def test_enforce_never_raises(monkeypatch):
    class Boom:
        def get(self, *a, **k):
            raise RuntimeError("environ is broken")

    import types

    # only the module's own name `os` is replaced, not the real os.environ
    monkeypatch.setattr(live_config, "os", types.SimpleNamespace(environ=Boom()))
    assert live_config.live_stops_enforce() is False


def test_constant_is_false_in_code():
    """The last commit of Phase 1C flips this; until then the system cannot arm.
    Checked on the attribute and on the source line (a literal, not an env read)."""
    import inspect
    import re

    assert live_config.LIVE_ORDER_PATH_READY is False
    assert re.search(r"^LIVE_ORDER_PATH_READY = False\b", inspect.getsource(live_config), re.M)


# --- live_armed truth table ---------------------------------------------------


@pytest.mark.parametrize(
    "mode,enforce,ready",
    list(itertools.product(["PAPER", "LIVE"], [None, "1"], [False, True])),
)
def test_live_armed_truth_table(mode_env, mode, enforce, ready):
    mode_env(mode, enforce, ready)
    expected = mode == "LIVE" and enforce == "1" and ready is True
    assert live_config.live_armed() is expected


def test_live_armed_table_has_exactly_one_true_row(mode_env):
    trues = []
    for mode, enforce, ready in itertools.product(["PAPER", "LIVE"], [None, "1"], [False, True]):
        mode_env(mode, enforce, ready)
        if live_config.live_armed():
            trues.append((mode, enforce, ready))
    assert trues == [("LIVE", "1", True)]


def test_arming_state_reports_the_components(mode_env):
    mode_env("LIVE", "1", False)
    assert live_config.arming_state() == {
        "mode": "LIVE", "enforce": True, "path_ready": False, "armed": False,
    }
    mode_env("LIVE", "1", True)
    assert live_config.arming_state() == {
        "mode": "LIVE", "enforce": True, "path_ready": True, "armed": True,
    }
    mode_env("PAPER", None, False)
    assert live_config.arming_state() == {
        "mode": "PAPER", "enforce": False, "path_ready": False, "armed": False,
    }


# --- the guard in clob_client -------------------------------------------------


ORDER_CALLS = {
    "post_maker": lambda: clob_client.post_maker(TOKEN, "BUY", 0.42, 10.0, tick_size=0.01),
    "cross_taker": lambda: clob_client.cross_taker(TOKEN, "BUY", 4.2, tick_size=0.01),
}
UNARMED_STATES = {
    "paper": ("PAPER", "1", True),            # even with everything else true
    "live_flag_unset": ("LIVE", None, True),
    "live_flag_1_constant_false": ("LIVE", "1", False),
    "live_flag_true_word": ("LIVE", "true", True),
}


@pytest.mark.parametrize("call", sorted(ORDER_CALLS))
@pytest.mark.parametrize("state", sorted(UNARMED_STATES))
def test_unarmed_order_calls_raise_and_reach_no_client(fake_clob, mode_env, call, state):
    mode_env(*UNARMED_STATES[state])
    with pytest.raises(ClobError, match="not armed"):
        ORDER_CALLS[call]()
    fake_clob.assert_no_calls()


def test_not_armed_error_names_the_three_components(fake_clob, mode_env):
    mode_env("LIVE", None, False)
    with pytest.raises(ClobError) as exc:
        ORDER_CALLS["post_maker"]()
    text = str(exc.value)
    assert text.startswith("not armed")
    assert "mode=LIVE" in text and "enforce=False" in text and "LIVE_ORDER_PATH_READY=False" in text


def test_guard_runs_before_the_client_is_even_built(monkeypatch, mode_env):
    """No fake_clob here: _get_client() would raise on the missing key. The guard error
    comes first, so _get_client is never reached."""
    mode_env("PAPER", None, False)
    monkeypatch.setattr(clob_client, "_get_client", lambda: pytest.fail("client built before the guard"))
    for call in ORDER_CALLS.values():
        with pytest.raises(ClobError, match="not armed"):
            call()


def test_guard_runs_before_argument_processing(fake_clob, mode_env):
    """Even nonsense arguments get the not-armed error, not a side error."""
    mode_env("PAPER", None, False)
    with pytest.raises(ClobError, match="not armed"):
        clob_client.cross_taker(None, None, None, tick_size=None)
    fake_clob.assert_no_calls()


def test_guard_runs_before_the_egress_check(fake_clob, mode_env, monkeypatch):
    mode_env("PAPER", None, False)
    monkeypatch.setenv("POLYCLAWD_EGRESS_REQUIRE_SRC", "10.2.0.2")
    monkeypatch.setattr(clob_client, "_egress_src_ip", lambda host, port=443: "192.0.2.1")
    for call in ORDER_CALLS.values():
        with pytest.raises(ClobError, match="not armed"):
            call()
    fake_clob.assert_no_calls()


def test_armed_post_maker_reaches_the_fake_and_converts(fake_clob, shapes, armed):
    fake_clob.script("place_limit_order", shapes.accepted_post())
    out = ORDER_CALLS["post_maker"]()
    assert out["orderID"] == "0xorder-live-1" and out["status"] == "LIVE"
    assert len(fake_clob.calls_to("place_limit_order")) == 1


def test_armed_cross_taker_reaches_the_fake_and_converts(fake_clob, shapes, armed):
    fake_clob.script("place_market_order", shapes.fak_filled_buy(usd="4.2", shares="10"))
    out = ORDER_CALLS["cross_taker"]()
    assert out["status"] == "MATCHED" and out["size_matched"] == "10.0"
    assert len(fake_clob.calls_to("place_market_order")) == 1


def test_armed_but_egress_wrong_still_stops_at_the_egress_guard(fake_clob, armed, monkeypatch):
    monkeypatch.setenv("POLYCLAWD_EGRESS_REQUIRE_SRC", "10.2.0.2")
    monkeypatch.setattr(clob_client, "_egress_src_ip", lambda host, port=443: "192.0.2.1")
    with pytest.raises(ClobError, match="egress guard"):
        ORDER_CALLS["post_maker"]()
    fake_clob.assert_no_calls()


# --- reads and cancel stay allowed in every mode -------------------------------


@pytest.mark.parametrize("state", ["paper", "live_unarmed"])
def test_cancel_get_order_get_balance_work_unarmed(fake_clob, shapes, mode_env, state):
    mode_env(*{"paper": ("PAPER", None, False), "live_unarmed": ("LIVE", None, False)}[state])
    fake_clob.script("cancel_order", shapes.cancel_confirmed("0xorder-live-1"))
    fake_clob.script("get_order", shapes.order_live())
    fake_clob.script("get_balance_allowance", shapes.balance("12.5"))
    assert clob_client.cancel("0xorder-live-1") == {"cancelled": True, "order_id": "0xorder-live-1"}
    assert clob_client.get_order("0xorder-live-1")["status"] == "LIVE"
    assert clob_client.get_balance() == pytest.approx(12.5)
    assert len(fake_clob.calls) == 3

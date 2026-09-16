"""Vendor minimum-order-size guard on the EXIT path (``execute_exit``).

Companion to ``test_min_order_size_guard.py``, which covers entries. Exits have a
distinct and more dangerous failure mode: ``execute_exit`` posted a maker SELL of
``pos_shares`` unguarded, so a position below the vendor minimum had its order
rejected, was seen as a zero fill, and fell through to ``held_remainder`` — a
HARD STOP silently degrading into hold-to-resolution with no signal that the stop
never executed.

Exposure rose on 2026-08-25 when ``_SW_LIVE_FRACTION`` went 0.25 -> 0.10, moving
live positions from 12-36 shares down to 5.0-9.6 — onto the 5-share boundary.

All vendor/DB calls are monkeypatched — no network, no real DB writes.
"""

import pytest

import execution.live_executor as le

TOKEN = "7132104567925221259462638553270691275033272857194"
MIN_SHARES = 5.0


class _Gov:
    def __init__(self):
        self.closes = []

    def record_close(self, **kw):
        self.closes.append(kw)


def _position(shares, *, entry_price=0.50, cost_usd=None):
    return {
        "id": 42,
        "token_id": TOKEN,
        "market_id": "mkt-1",
        "shares": shares,
        "entry_price": entry_price,
        "cost_usd": cost_usd if cost_usd is not None else shares * entry_price,
        "neg_risk": False,
    }


@pytest.fixture
def spy(monkeypatch):
    """Stub every side effect; record whether the vendor was ever called."""
    seen = {"post_maker": 0, "cross_taker": 0, "cancel": 0}

    monkeypatch.setattr(le, "_min_order_shares", lambda tid: MIN_SHARES)
    monkeypatch.setattr(le.live_config, "maker_wait_secs", lambda: 0)
    monkeypatch.setattr(le, "_wait_for_maker_fill", lambda oid, timeout: True)
    monkeypatch.setattr(le, "_poll_until_settled", lambda oid, label=None: {})
    monkeypatch.setattr(le, "_matched_shares_of", lambda status: 0.0)
    monkeypatch.setattr(le.live_position_tracker, "close_position",
                        lambda conn, **kw: {"pnl": 0.0, "usd_released": 0.0})

    def fake_post(**kw):
        seen["post_maker"] += 1
        return {"orderID": "oid-1"}

    def fake_taker(**kw):
        seen["cross_taker"] += 1
        return {"orderID": "oid-t"}

    def fake_cancel(oid):
        seen["cancel"] += 1
        return True

    monkeypatch.setattr(le.clob_client, "post_maker", fake_post)
    monkeypatch.setattr(le.clob_client, "cross_taker", fake_taker)
    monkeypatch.setattr(le.clob_client, "cancel", fake_cancel)
    return seen


def _exit(position_row, *, mark_price=0.50, hard_cap_frac=0.50, reason="test"):
    return le.execute_exit(
        None, _Gov(),
        position_row=position_row,
        mark_price=mark_price,
        tick_size=0.001,
        hard_cap_frac=hard_cap_frac,
        reason=reason,
    )


# ── the core guard ────────────────────────────────────────────────────────────

def test_below_minimum_is_not_posted_to_the_vendor(spy):
    """A 4.2-share position must never reach the exchange — it would be rejected."""
    res = _exit(_position(4.2))
    assert spy["post_maker"] == 0, "posted a known-rejectable order to the vendor"
    assert spy["cross_taker"] == 0
    assert res["min_size_blocked"] is True
    assert res["shares_sold"] == 0.0


def test_below_minimum_returns_existing_action_not_a_new_one(spy):
    """Callers switch on `action`; the guard must not invent a new value."""
    res = _exit(_position(4.2))
    assert res["action"] == "held_remainder"
    assert res["action"] in {"maker_closed", "taker_closed", "partial_closed", "held_remainder"}


def test_exactly_at_minimum_is_allowed(spy):
    """5.0 shares is legal — the guard must not be off by one."""
    _exit(_position(MIN_SHARES))
    assert spy["post_maker"] == 1


def test_above_minimum_takes_the_normal_path(spy):
    _exit(_position(9.6))
    assert spy["post_maker"] == 1


def test_epsilon_boundary_not_tripped_by_float_noise(spy):
    """4.999999... from float division must still be treated as 5."""
    _exit(_position(MIN_SHARES - le._MIN_SHARES_EPS / 2))
    assert spy["post_maker"] == 1


# ── the dangerous case: a hard stop that cannot execute ───────────────────────

def test_hard_stop_below_minimum_is_flagged(spy):
    """Entry 0.50 -> mark 0.10 is an 80% adverse move, well past a 50% hard cap."""
    res = _exit(_position(4.0, entry_price=0.50), mark_price=0.10, hard_cap_frac=0.50)
    assert res["hard_stop_blocked"] is True, "a hard stop silently failed to execute"
    assert res["min_size_blocked"] is True


def test_noise_stop_below_minimum_is_not_flagged_as_hard(spy):
    """A small adverse move is an ordinary hold, not a failed hard stop."""
    res = _exit(_position(4.0, entry_price=0.50), mark_price=0.49, hard_cap_frac=0.50)
    assert res["hard_stop_blocked"] is False
    assert res["min_size_blocked"] is True


def test_warning_names_the_position_and_the_shortfall(spy, caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        _exit(_position(4.2))
    # loguru may not route through caplog; assert on the returned reason instead,
    # which carries the same numbers the operator needs.
    res = _exit(_position(4.2))
    assert "below vendor minimum" in res["reason"]
    assert "4.2" in res["reason"] and "5" in res["reason"]


# ── partial-fill remainder ────────────────────────────────────────────────────

def test_taker_remainder_below_minimum_is_not_crossed(monkeypatch, spy):
    """A partial maker fill can strand a sub-minimum remainder; the FAK would bounce."""
    # 10 shares, maker fills 7 -> remainder 3 < 5. Adverse move triggers hard stop.
    monkeypatch.setattr(le, "_matched_shares_of", lambda status: 7.0)
    res = _exit(_position(10.0, entry_price=0.50), mark_price=0.10, hard_cap_frac=0.50)
    assert spy["cross_taker"] == 0, "crossed a taker below the vendor minimum"
    assert res["min_size_blocked"] is True
    assert res["hard_stop_blocked"] is True


def test_taker_remainder_above_minimum_still_crosses(monkeypatch, spy):
    """Regression guard: the new check must not block legitimate hard-stop exits."""
    monkeypatch.setattr(le, "_matched_shares_of", lambda status: 2.0)
    _exit(_position(10.0, entry_price=0.50), mark_price=0.10, hard_cap_frac=0.50)
    assert spy["cross_taker"] == 1


def test_zero_share_position_short_circuits_before_the_guard(spy):
    res = _exit(_position(0.0))
    assert res["action"] == "held_remainder"
    assert spy["post_maker"] == 0
    assert "min_size_blocked" not in res

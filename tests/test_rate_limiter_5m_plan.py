"""Rate limiter recalibrated for the 5M/mo the-odds-api plan (upgraded 2026-06-25).

The old constants were sized to the dead 100K plan, so on 5M they (a) throttled
scanning ~50x via the daily soft budget and (b) the low-credit alert wouldn't
fire with any runway. New floor reserves 5% (250K) for critical crons.

Run: venv/bin/python -m pytest tests/test_rate_limiter_5m_plan.py -v --noconftest
"""
from odds import rate_limiter as rl


def test_constants_match_5m_plan():
    assert rl.MONTHLY_LIMIT == 5_000_000
    assert rl.CREDIT_FLOOR == 250_000          # 5% reserve
    assert rl.LOW_CREDIT_WATERMARK == 1_000_000  # 20% early-warning


def test_floor_reserves_250k_for_critical_only(monkeypatch):
    # 200K remaining is below the new 250K floor → a low-priority monitor call
    # must be gated (old 5K floor would have let it through).
    monkeypatch.setattr(rl, "read_real_remaining", lambda: 200_000)
    ok, why = rl.can_make_call("low")
    assert ok is False
    assert "floor" in why.lower()


def test_floor_does_not_block_a_healthy_balance(monkeypatch):
    # Well above the floor → the floor gate is not the blocker.
    monkeypatch.setattr(rl, "read_real_remaining", lambda: 4_990_000)
    ok, why = rl.can_make_call("critical")
    assert ok is True

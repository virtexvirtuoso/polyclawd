"""Regression test for the baseball edge line-matching bug (fixed 2026-06-02).

Bug: `_extract_total_prices` / `_extract_spread_prices` matched the bookmaker's
total/spread line to a Polymarket market by SUBSTRING (`point_str not in q`), and
the spread matcher additionally floored the target with `int()`. A bookmaker
alt-line of 8.0 therefore matched Polymarket's "O/U 8.5" market ("8" in "8.5"),
comparing two different bets and fabricating large spurious edges.

Fix: exact numeric line matching via regex. These tests pin that behavior.
Run from the app root:  pytest tests/test_baseball_point_match.py
"""
from odds.baseball_edge import _extract_total_prices, _extract_spread_prices


def _total_event(line):
    return {"markets": [{
        "question": f"Toronto Blue Jays vs. Atlanta Braves: O/U {line}",
        "outcomePrices": '["0.435", "0.565"]',
        "conditionId": "0xTOTAL",
    }]}


def _spread_event(line):
    return {"markets": [{
        "question": f"Spread: Atlanta Braves ({line})",
        "outcomePrices": '["0.500", "0.500"]',
        "conditionId": "0xSPREAD",
    }]}


def test_total_integer_line_does_not_match_half_line():
    # book 8.0 must NOT match Polymarket O/U 8.5 (different bets)
    assert _extract_total_prices(_total_event("8.5"), 8.0) is None


def test_total_exact_line_matches():
    r = _extract_total_prices(_total_event("8.5"), 8.5)
    assert r is not None and abs(r[0] - 0.435) < 1e-9


def test_total_no_cross_number_false_match():
    # book 9.0 must NOT match Polymarket O/U 19.5
    assert _extract_total_prices(_total_event("19.5"), 9.0) is None


def test_spread_integer_line_does_not_match_half_line():
    # book 1.0 must NOT match Polymarket (-1.5)
    assert _extract_spread_prices(_spread_event("-1.5"), "Atlanta Braves", 1.0) is None


def test_spread_exact_line_matches():
    assert _extract_spread_prices(_spread_event("-1.5"), "Atlanta Braves", -1.5) is not None

# tests/test_pm_us_book_pacing.py
"""PM-US book fetch: paced, cooldown on rate limit, failures never cached (2026-10-23)."""
import sys
import types

import pytest

import signals.nfl_fast_move_monitor as m
import scripts.espn_edge_adapter as ad


class RateLimitError(Exception):
    pass


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(round(s, 3))
        self.t += s


def install_client(monkeypatch, behaviour, calls):
    class Markets:
        def book(self, slug):
            calls.append(slug)
            r = behaviour(slug)
            if isinstance(r, Exception):
                raise r
            return r

    class PolymarketUS:
        def __init__(self):
            self.markets = Markets()

    mod = types.ModuleType("polymarket_us")
    mod.PolymarketUS = PolymarketUS
    monkeypatch.setitem(sys.modules, "polymarket_us", mod)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(m, "_clock", c.now)
    monkeypatch.setattr(m, "_sleeper", c.sleep)
    monkeypatch.setattr(m, "_book_last_ts", 0.0)
    monkeypatch.setattr(m, "_book_cooldown_until", 0.0)
    ad._BOOK_CACHE.clear()
    return c


def test_back_to_back_fetches_are_spaced(monkeypatch, clock):
    calls = []
    install_client(monkeypatch, lambda s: {"marketData": {"bids": [], "offers": []}}, calls)
    m._fetch_us_book("a")
    m._fetch_us_book("b")
    assert calls == ["a", "b"]
    assert clock.slept == [m._BOOK_MIN_INTERVAL_S]


def test_rate_limit_opens_a_cooldown_and_stops_requests(monkeypatch, clock):
    calls = []
    page = RateLimitError("<html>... <span>1015</span> ... You are being rate limited</html>")
    install_client(monkeypatch, lambda s: page, calls)
    assert m._fetch_us_book("a") is None
    assert m._fetch_us_book("b") is None          # inside the cooldown: no request at all
    assert calls == ["a"]
    clock.t += m._BOOK_COOLDOWN_S + 1
    assert m._fetch_us_book("c") is None          # cooldown over: one new attempt
    assert calls == ["a", "c"]


def test_rate_limit_log_line_carries_no_html(monkeypatch, clock):
    seen = []
    monkeypatch.setattr(m, "logger", types.SimpleNamespace(warning=seen.append, debug=seen.append))
    install_client(monkeypatch, lambda s: RateLimitError("<!DOCTYPE html><span>1015</span>" + "x" * 5000), [])
    m._fetch_us_book("a")
    assert len(seen) == 1 and "<" not in seen[0] and len(seen[0]) < 200


def test_other_errors_do_not_open_a_cooldown(monkeypatch, clock):
    calls = []
    install_client(monkeypatch, lambda s: ValueError("404 not found"), calls)
    m._fetch_us_book("a")
    m._fetch_us_book("b")
    assert calls == ["a", "b"]


def test_game_fetch_uses_the_paced_path_and_respects_the_cooldown(monkeypatch, clock):
    calls = []
    install_client(monkeypatch, lambda s: {"marketData": {}}, calls)

    class Search:
        def query(self, q):
            return {"events": [{"gameId": "g1", "markets": [
                {"slug": "nfl-a-b", "sportsMarketType": "football_team_full_game_winner"}]}]}

    monkeypatch.setattr(sys.modules["polymarket_us"].PolymarketUS, "search", Search(), raising=False)
    monkeypatch.setattr(m, "_book_cooldown_until", clock.t + 60)
    assert m._fetch_us_book_for_game("A", "B") is None      # cooldown: no book request, no half result
    assert calls == []
    monkeypatch.setattr(m, "_book_cooldown_until", 0.0)
    assert m._fetch_us_book_for_game("A", "B")["book"] == {"marketData": {}}
    assert calls == ["nfl-a-b"]


def test_adapter_never_caches_a_failure_and_clears_per_scan(monkeypatch, clock):
    calls = []
    monkeypatch.setattr(m, "_executable_edge_from_book", lambda book, side: {"available": False})
    results = iter([ValueError("timeout"), {"marketData": {"bids": [], "offers": []}}])
    install_client(monkeypatch, lambda s: next(results), calls)
    assert ad._executable_for_edge("slug-1", None) is None      # failed: not cached
    ad._executable_for_edge("slug-1", None)                      # retried in the same scan
    assert calls == ["slug-1", "slug-1"]
    assert "slug-1" in ad._BOOK_CACHE
    ad.espn_edges_to_alerts([])                                  # a new scan starts empty
    assert ad._BOOK_CACHE == {}

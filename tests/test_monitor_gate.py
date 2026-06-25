"""Gated+cached fetch for the live-monitor scripts (cross_sport_drift / mlb /
soccer). Fixes the 2026-06 credit leak: those monitors hit the-odds-api with raw
urllib — bypassing the CREDIT_FLOOR, re-fetching the full slate once per game,
and never writing the credit header back (stale-cache deadlock).

Run: venv/bin/python -m pytest tests/test_monitor_gate.py -v --noconftest
"""
import json
import odds.monitor_gate as mg
from odds import rate_limiter as rl


class _FakeResp:
    def __init__(self, data, headers):
        self._data = data
        self.headers = headers
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return json.dumps(self._data).encode()


def _fake_urlopen(monkeypatch, data, headers, counter):
    def fake(req, timeout=12):
        counter["n"] += 1
        return _FakeResp(data, headers)
    monkeypatch.setattr(mg.urllib.request, "urlopen", fake)


def setup_function(_):
    mg.clear_cache()


def test_caches_within_ttl_one_upstream_fetch(monkeypatch):
    monkeypatch.setattr(rl, "can_make_call", lambda p: (True, "ok"))
    monkeypatch.setattr(rl, "persist_real_remaining", lambda *a, **k: None)
    monkeypatch.setattr(rl, "update_from_headers", lambda *a, **k: None)
    c = {"n": 0}
    _fake_urlopen(monkeypatch, [{"x": 1}], {"x-requests-remaining": "5", "x-requests-used": "1"}, c)
    a = mg.gated_fetch_json("http://t", {"a": "1"}, ttl=90)
    b = mg.gated_fetch_json("http://t", {"a": "1"}, ttl=90)
    assert a == b == [{"x": 1}]
    assert c["n"] == 1   # second call served from cache, not refetched per-game


def test_gate_block_without_cache_returns_none_and_does_not_spend(monkeypatch):
    monkeypatch.setattr(rl, "can_make_call", lambda p: (False, "floor"))
    c = {"n": 0}
    _fake_urlopen(monkeypatch, [], {}, c)
    assert mg.gated_fetch_json("http://t", {"a": "1"}) is None
    assert c["n"] == 0   # respected the credit floor — no upstream call


def test_gate_block_serves_stale_cache(monkeypatch):
    monkeypatch.setattr(rl, "persist_real_remaining", lambda *a, **k: None)
    monkeypatch.setattr(rl, "update_from_headers", lambda *a, **k: None)
    c = {"n": 0}
    _fake_urlopen(monkeypatch, [{"v": 9}], {"x-requests-remaining": "5"}, c)
    monkeypatch.setattr(rl, "can_make_call", lambda p: (True, "ok"))
    mg.gated_fetch_json("http://t", {"a": "1"}, ttl=0)            # populate
    monkeypatch.setattr(rl, "can_make_call", lambda p: (False, "floor"))
    stale = mg.gated_fetch_json("http://t", {"a": "1"}, ttl=0)    # gated -> serve stale
    assert stale == [{"v": 9}]
    assert c["n"] == 1   # not refetched


def test_persists_real_remaining_from_header(monkeypatch):
    rec = {}
    monkeypatch.setattr(rl, "can_make_call", lambda p: (True, "ok"))
    monkeypatch.setattr(rl, "persist_real_remaining",
                        lambda rem, used=None: rec.update(remaining=rem, used=used))
    monkeypatch.setattr(rl, "update_from_headers", lambda h: None)
    c = {"n": 0}
    _fake_urlopen(monkeypatch, [{"ok": 1}],
                  {"x-requests-remaining": "4999000", "x-requests-used": "1000"}, c)
    mg.gated_fetch_json("http://t", {"a": "1"})
    assert rec["remaining"] == 4999000 and rec["used"] == 1000   # floor balance kept fresh

"""Tests for odds/gamma_title.py — Gamma title resolver with SQLite cache.

No live HTTP: urlopen is monkeypatched in every test that could hit the network.
"""
import io
import json
import sqlite3
import urllib.error

import pytest

import odds.gamma_title as gt


COND_ID = "0x" + "ab" * 32
QUESTION = "Will it rain in NYC on July 20?"


def _fake_response(payload):
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

    return _Resp(json.dumps(payload).encode())


@pytest.fixture
def db(tmp_path):
    return tmp_path / "shadow_trades.db"


def test_non_hex_id_returns_none_without_http(monkeypatch, db):
    def boom(*a, **kw):
        raise AssertionError("HTTP must not be attempted for non-0x ids")

    monkeypatch.setattr(gt.urllib.request, "urlopen", boom)
    assert gt.resolve_title("KXHIGHNY-26JUL20-B85", db_path=db) is None
    assert gt.resolve_title("", db_path=db) is None
    assert gt.resolve_title(None, db_path=db) is None


def test_resolves_question_and_caches(monkeypatch, db):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        assert timeout == 5
        return _fake_response([{"question": QUESTION}])

    monkeypatch.setattr(gt.urllib.request, "urlopen", fake_urlopen)

    assert gt.resolve_title(COND_ID, db_path=db) == QUESTION
    assert calls["n"] == 1
    # Second call served from cache — no additional HTTP.
    assert gt.resolve_title(COND_ID, db_path=db) == QUESTION
    assert calls["n"] == 1
    # Row landed in the title_cache table.
    con = sqlite3.connect(str(db))
    row = con.execute(
        "SELECT title FROM title_cache WHERE market_id = ?", (COND_ID,)
    ).fetchone()
    con.close()
    assert row == (QUESTION,)


def test_http_error_returns_none_and_does_not_cache(monkeypatch, db):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError("url", 500, "boom", {}, None)

    monkeypatch.setattr(gt.urllib.request, "urlopen", boom)
    assert gt.resolve_title(COND_ID, db_path=db) is None

    # Failure was not cached: a later success must resolve.
    monkeypatch.setattr(
        gt.urllib.request, "urlopen",
        lambda req, timeout=None: _fake_response([{"question": QUESTION}]),
    )
    assert gt.resolve_title(COND_ID, db_path=db) == QUESTION


def test_empty_or_malformed_body_returns_none(monkeypatch, db):
    monkeypatch.setattr(
        gt.urllib.request, "urlopen",
        lambda req, timeout=None: _fake_response([]),
    )
    assert gt.resolve_title(COND_ID, db_path=db) is None

    monkeypatch.setattr(
        gt.urllib.request, "urlopen",
        lambda req, timeout=None: _fake_response({"unexpected": "shape"}),
    )
    assert gt.resolve_title(COND_ID, db_path=db) is None


def test_never_raises_even_on_unwritable_db(monkeypatch, tmp_path):
    monkeypatch.setattr(
        gt.urllib.request, "urlopen",
        lambda req, timeout=None: _fake_response([{"question": QUESTION}]),
    )
    # Point the cache at a path whose parent does not exist — cache write
    # fails, but the resolved title must still come back and nothing raises.
    bad = tmp_path / "nonexistent-dir" / "x.db"
    assert gt.resolve_title(COND_ID, db_path=bad) == QUESTION


def test_resolves_decimal_clob_token_id(monkeypatch, tmp_path):
    """live_positions stores CLOB token ids (decimal, ~77 digits) in market_id;
    the resolver must route those through the clob_token_ids Gamma param."""
    import odds.gamma_title as gt
    seen = {}

    class _Resp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return b'[{"question": "Will it rain in NYC on July 17?"}]'

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(gt.urllib.request, "urlopen", fake_urlopen)
    tok = "9" * 40
    out = gt.resolve_title(tok, db_path=tmp_path / "t.db")
    assert out == "Will it rain in NYC on July 17?"
    assert "clob_token_ids" in seen["url"]


def test_short_digits_and_tickers_still_decline(tmp_path):
    import odds.gamma_title as gt
    assert gt.resolve_title("12345", db_path=tmp_path / "t.db") is None
    assert gt.resolve_title("KXMLBGAME-FOO", db_path=tmp_path / "t.db") is None


def test_closed_market_fallback(monkeypatch, tmp_path):
    """Closed markets are absent from Gamma's default listing — resolver must
    retry with closed=true before giving up."""
    import odds.gamma_title as gt
    calls = []

    class _Resp:
        def __init__(self, payload):
            self.payload = payload
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return self.payload

    def fake_urlopen(req, timeout):
        calls.append(req.full_url)
        if "closed=true" in req.full_url:
            return _Resp(b'[{"question": "Will Spain win on 2026-06-26?"}]')
        return _Resp(b"[]")

    monkeypatch.setattr(gt.urllib.request, "urlopen", fake_urlopen)
    out = gt.resolve_title("8" * 40, db_path=tmp_path / "t.db")
    assert out == "Will Spain win on 2026-06-26?"
    assert len(calls) == 2 and "closed=true" in calls[1]

"""CLOB /prices-history → Data API GET /v2/prices-history.

No network. The fixtures follow the documented HTTP contract
(https://docs.polymarket.com/api-reference/markets/get-a-tokens-price-history):
{data, pagination}, timestamp epoch seconds, price a JSON number,
bucket_seconds instead of fidelity minutes, and exactly one window form.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from config.data_api import (
    price_history_params,
    price_history_url,
    read_price_history,
)
from config.polymarket_urls import POLYMARKET_DATA_API


def _page(rows, cursor=None):
    return {
        "data": rows,
        "pagination": {
            "limit": 10000,
            "offset": 0,
            "has_more": cursor is not None,
            "next_cursor": cursor,
        },
    }


def test_url_is_data_api_v2_and_drops_clob_params():
    url = price_history_url("tok", interval="1w", fidelity=60)
    assert url.startswith(POLYMARKET_DATA_API + "/v2/prices-history?")
    assert "token_id=tok" in url
    assert "interval=1w" in url
    assert "bucket_seconds=3600" in url
    assert "market=" not in url
    assert "fidelity" not in url
    assert "clob.polymarket.com" not in url
    assert "offset" not in url


def test_daily_fidelity_on_max_is_86400_seconds():
    params = price_history_params("tok", interval="max", fidelity=1440)
    assert params["bucket_seconds"] == 86400
    assert params["interval"] == "max"
    assert "market" not in params
    assert "fidelity" not in params


def test_max_interval_floors_sub_minute_buckets(caplog):
    caplog.set_level(logging.WARNING)
    params = price_history_params("tok", interval="max", fidelity=1)
    assert params["bucket_seconds"] == 600
    assert any("clamped to 600" in r.message for r in caplog.records)


def test_start_drops_interval(caplog):
    caplog.set_level(logging.WARNING)
    params = price_history_params("tok", interval="1h", fidelity=60, start_ts=100, end_ts=200)
    assert "interval" not in params
    assert params["start"] == 100
    assert params["end"] == 200
    assert params["bucket_seconds"] == 3600
    assert any("dropping interval=1h" in r.message for r in caplog.records)


def test_as_of_is_exclusive(caplog):
    caplog.set_level(logging.WARNING)
    params = price_history_params("tok", interval="1d", fidelity=60, as_of=123)
    assert params == {"token_id": "tok", "as_of": 123}
    assert any("as_of is exclusive" in r.message for r in caplog.records)


def test_points_map_to_clob_t_p_and_follow_cursor():
    seen = []

    def fetch(url):
        seen.append(url)
        if "cursor=" not in url:
            return _page(
                [
                    {"timestamp": 100, "price": 0.2, "resolution_seconds": 3600},
                    {"timestamp": 200, "price": 0.3, "resolution_seconds": 0},
                ],
                cursor="page-2",
            )
        return _page(
            [{"timestamp": 300, "price": 0.4, "resolution_seconds": 3600}],
            cursor=None,
        )

    points = read_price_history("tok", fetch, interval="1w", fidelity=60, max_pages=5)
    assert points == [{"t": 100, "p": 0.2}, {"t": 200, "p": 0.3}, {"t": 300, "p": 0.4}]
    assert len(seen) == 2
    assert all("/v2/prices-history" in url for url in seen)
    assert "offset" not in seen[1]
    assert "cursor=page-2" in seen[1]
    # A short first page is not the end of the walk.
    assert "resolution_seconds" not in points[0]


def test_empty_and_null_data_warn(caplog):
    caplog.set_level(logging.WARNING)

    def empty(_url):
        return _page([], cursor=None)

    assert read_price_history("tok-empty", empty, interval="1d", fidelity=60) == []
    assert any("empty or missing data" in r.message and "tok-empty" in r.message for r in caplog.records)

    caplog.clear()

    def missing(_url):
        return {"pagination": {"next_cursor": None}}

    assert read_price_history("tok-missing", missing, interval="1d", fidelity=60) == []
    assert any("empty or missing data" in r.message and "tok-missing" in r.message for r in caplog.records)

    caplog.clear()

    def null_data(_url):
        return {"data": None, "pagination": {"next_cursor": None}}

    assert read_price_history("tok-null", null_data, interval="1d", fidelity=60) == []
    assert any("empty or missing data" in r.message and "tok-null" in r.message for r in caplog.records)


def test_fetch_failure_warns_and_returns_none(caplog):
    caplog.set_level(logging.WARNING)
    assert read_price_history("tok", lambda _url: None, interval="1h", fidelity=60) is None
    assert any("fetch failed" in r.message for r in caplog.records)


def test_long_start_end_is_split_at_15_days():
    seen = []

    def fetch(url):
        seen.append(url)
        if "start=1000&" in url or url.endswith("start=1000") or "start=1000&end=" in url:
            return _page([{"timestamp": 1000, "price": 0.1, "resolution_seconds": 3600}])
        return _page([{"timestamp": 1000 + 15 * 86400, "price": 0.2, "resolution_seconds": 3600}])

    start = 1000
    end = start + 20 * 86400
    points = read_price_history("tok", fetch, fidelity=60, start_ts=start, end_ts=end)
    assert len(seen) == 2
    assert f"start={start}" in seen[0]
    assert f"end={start + 15 * 86400}" in seen[0]
    assert f"start={start + 15 * 86400}" in seen[1]
    assert f"end={end}" in seen[1]
    assert "interval=" not in seen[0]
    assert [p["p"] for p in points] == [0.1, 0.2]


def test_later_page_failure_keeps_points(caplog):
    caplog.set_level(logging.WARNING)
    calls = {"n": 0}

    def fetch(_url):
        calls["n"] += 1
        if calls["n"] == 1:
            return _page([{"timestamp": 10, "price": 0.5, "resolution_seconds": 60}], cursor="c2")
        return None

    points = read_price_history("tok", fetch, interval="1h", fidelity=1, max_pages=3)
    assert points == [{"t": 10, "p": 0.5}]
    assert any("fetch failed" in r.message for r in caplog.records)


def test_clob_helper_keeps_ohlc_shape(monkeypatch):
    import importlib.util
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "polymarket_clob_under_test",
        repo_root / "odds" / "polymarket_clob.py",
    )
    clob = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(clob)

    monkeypatch.setattr(
        clob,
        "read_price_history",
        lambda token_id, fetch, **kw: [{"t": 50, "p": 0.25}],
    )
    candles = clob.get_price_history("tok", interval="6h", fidelity=60)
    assert candles == [{
        "timestamp": 50,
        "open": 0.25,
        "high": 0.25,
        "low": 0.25,
        "close": 0.25,
        "price": 0.25,
    }]


def test_slug_history_stays_t_p(monkeypatch, tmp_path):
    from signals import polymarket_price_history as hist

    monkeypatch.setattr(hist, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(hist, "get_yes_token_id", lambda slug: "yes-token")
    seen = {}

    def fake(token_id, fetch, **kw):
        seen["token_id"] = token_id
        seen.update(kw)
        return [{"t": i, "p": i / 4} for i in range(1, 5)]

    monkeypatch.setattr(hist, "read_price_history", fake)
    pts = hist.get_price_history("some-slug", max_points=60)
    assert pts == [{"t": 1, "p": 0.25}, {"t": 2, "p": 0.5}, {"t": 3, "p": 0.75}, {"t": 4, "p": 1.0}]
    assert seen["token_id"] == "yes-token"
    assert seen["interval"] == "max"
    assert seen["fidelity"] == 1440


def test_closing_line_uses_last_point_before_end(monkeypatch):
    from signals import tier1_whale_alert as tier1

    cutoff = datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp()

    def fetch_json(url, timeout=15):
        assert "/prices-history" not in url
        return {
            "tokens": [{"token_id": "yes-tok"}],
            "end_date_iso": "2026-10-20T00:00:00+00:00",
        }

    seen = {}

    def fake(token_id, fetch, **kw):
        seen["token_id"] = token_id
        seen.update(kw)
        return [
            {"t": int(cutoff) - 3600, "p": 0.42},
            {"t": int(cutoff) + 3600, "p": 0.99},
        ]

    monkeypatch.setattr(tier1, "_fetch_json", fetch_json)
    monkeypatch.setattr(tier1, "read_price_history", fake)
    assert tier1._get_closing_line("0xabc", "polymarket") == pytest.approx(0.42)
    assert seen["token_id"] == "yes-tok"
    assert seen["interval"] == "1w"
    assert seen["fidelity"] == 60

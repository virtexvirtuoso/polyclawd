"""Data API v2 boundary: envelope, field names, and cursor paging.

No network. The live contract these fixtures mirror was probed on 2026-10-24:
v2 wraps rows in {data, pagination}, renames camelCase fields, rejects offset
and market, and ignores asset_id / token_id / sizeThreshold.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from config.data_api import (
    ACCOUNTING_SNAPSHOT_PATH,
    adapt_data_api_payload,
    data_route_url,
    fetch_data_v2_page,
    map_v2_value,
    read_data_v2,
    request_url,
    translate_params,
)
from config.polymarket_urls import POLYMARKET_DATA_API, data_url


def _page(rows, cursor="next", limit=2):
    return {
        "data": rows,
        "pagination": {
            "limit": limit,
            "offset": 0,
            "has_more": cursor is not None,
            "next_cursor": cursor,
        },
    }


def test_accounting_snapshot_stays_on_v1():
    url = data_route_url(ACCOUNTING_SNAPSHOT_PATH)
    assert url == data_url(ACCOUNTING_SNAPSHOT_PATH)
    assert "/v2/" not in url
    with pytest.raises(ValueError):
        data_route_url("/v1/market-positions")


def test_reads_use_v2_through_the_central_url_helper():
    url, token_filter = request_url("trades", {"limit": 10, "takerOnly": "true"})
    assert url.startswith(POLYMARKET_DATA_API + "/v2/trades?")
    assert token_filter is None
    assert "taker_only=true" in url
    assert "takerOnly" not in url


def test_offset_and_market_are_not_sent(caplog):
    caplog.set_level(logging.WARNING)
    url, _ = request_url(
        "trades",
        {"market": "0xabc", "offset": 0, "limit": 5, "user": "0x1"},
    )
    assert "offset" not in url
    assert "market=" not in url
    assert "condition=0xabc" in url
    assert "user=0x1" in url
    assert any("dropping rejected param offset" in r.message for r in caplog.records)


def test_asset_id_is_not_sent_and_rows_are_filtered():
    """v2 /trades ignores asset_id and token_id and serves the global tape."""
    seen = []

    def fetch(url):
        seen.append(url)
        return _page(
            [
                {"token_id": "wanted", "condition_id": "0x1", "side": "BUY", "size": 2, "price": 0.4},
                {"token_id": "other", "condition_id": "0x1", "side": "SELL", "size": 1, "price": 0.6},
            ],
            cursor=None,
        )

    rows, cursor = fetch_data_v2_page(
        "trades",
        {"condition": "0x1", "asset_id": "wanted", "limit": 50},
        fetch,
    )
    assert "asset_id" not in seen[0]
    assert "token_id" not in seen[0]
    assert "condition=0x1" in seen[0]
    assert cursor is None
    assert rows == [
        {"asset": "wanted", "conditionId": "0x1", "side": "BUY", "size": 2, "price": 0.4}
    ]


def test_size_threshold_becomes_token_floor():
    url, _ = request_url("positions", {"user": "0xabc", "sizeThreshold": 0.01, "limit": 100})
    assert "sizeThreshold" not in url
    assert "size_threshold" not in url
    assert "filter_type=TOKENS" in url
    assert "filter_amount=0.01" in url


def test_sort_order_asc_is_sort_direction():
    url, _ = request_url(
        "activity",
        {"user": "0xabc", "limit": 1, "sortBy": "timestamp", "sortOrder": "asc", "start": 1},
    )
    assert "sortOrder" not in url
    assert "sortBy" not in url
    assert "sort_direction=ASC" in url
    assert "start=1" in url


def test_limit_above_1000_is_clamped(caplog):
    caplog.set_level(logging.WARNING)
    url, _ = request_url("trades", {"limit": 2000})
    assert "limit=1000" in url
    assert any("clamped" in r.message for r in caplog.records)


def test_translate_params_unit():
    query, token_filter = translate_params(
        {"market": "0xabc", "offset": 500, "asset_id": "tok", "takerOnly": True}
    )
    assert query["condition"] == "0xabc"
    assert query["taker_only"] is True
    assert "offset" not in query
    assert "market" not in query
    assert token_filter == "tok"


def test_unwrap_and_map_trade_and_position_fields():
    rows, cursor = adapt_data_api_payload(
        _page(
            [
                {
                    "proxy_wallet": "0xwallet",
                    "token_id": "99",
                    "condition_id": "0xcond",
                    "outcome_index": 1,
                    "transaction_hash": "0xhash",
                    "event_slug": "evt",
                    "usdc_size": 12.5,
                    "profile_image": "img",
                    "side": "BUY",
                    "size": 5,
                    "price": 0.42,
                    "timestamp": 1700000000,
                    "type": "TRADE",
                }
            ],
            cursor="abc",
        ),
        route="trades",
    )
    assert cursor == "abc"
    assert rows[0]["proxyWallet"] == "0xwallet"
    assert rows[0]["asset"] == "99"
    assert rows[0]["conditionId"] == "0xcond"
    assert rows[0]["outcomeIndex"] == 1
    assert rows[0]["transactionHash"] == "0xhash"
    assert rows[0]["eventSlug"] == "evt"
    assert rows[0]["usdcSize"] == 12.5
    assert rows[0]["profileImage"] == "img"
    assert rows[0]["side"] == "BUY"
    assert "token_id" not in rows[0]
    assert "proxy_wallet" not in rows[0]

    mapped = map_v2_value(
        {
            "token_id": "99",
            "current_size": 250,
            "current_price": 0.2,
            "current_value": 50,
            "avg_price": 0.4,
            "entry_cost_usdc": 100,
            "unrealized_pnl": -50,
            "realized_pnl": 3,
            "percent_pnl": -50,
            "percent_realized_pnl": 1.5,
            "total_size": 250,
            "opposite_token_id": "100",
            "opposite_outcome": "No",
            "end_date": "2026-07-01",
            "negative_risk": False,
            "redeemable": True,
            "condition_id": "0xcond",
        }
    )
    assert mapped["asset"] == "99"
    assert mapped["size"] == 250
    assert mapped["curPrice"] == 0.2
    assert mapped["currentValue"] == 50
    assert mapped["avgPrice"] == 0.4
    assert mapped["initialValue"] == 100
    assert mapped["cashPnl"] == -50
    assert mapped["realizedPnl"] == 3
    assert mapped["percentPnl"] == -50
    assert mapped["percentRealizedPnl"] == 1.5
    assert mapped["totalBought"] == 250
    assert mapped["oppositeAsset"] == "100"
    assert mapped["oppositeOutcome"] == "No"
    assert mapped["endDate"] == "2026-07-01"
    assert map_v2_value({"end_date": "1970-01-01"})["endDate"] == ""
    assert mapped["negativeRisk"] is False
    assert mapped["redeemable"] is True
    assert mapped["conditionId"] == "0xcond"


def test_holders_groups_are_mapped_nested():
    rows, _ = adapt_data_api_payload(
        {
            "data": [
                {
                    "token_id": "yes-token",
                    "holders": [
                        {
                            "proxy_wallet": "0xabc",
                            "outcome_index": 0,
                            "display_username_public": True,
                            "amount": 10,
                            "name": "Ada",
                            "pseudonym": "ada",
                        }
                    ],
                }
            ],
            "pagination": {"has_more": False, "limit": 20, "offset": 0, "next_cursor": None},
        },
        route="holders",
    )
    holder = rows[0]["holders"][0]
    assert rows[0]["asset"] == "yes-token"
    assert holder["proxyWallet"] == "0xabc"
    assert holder["outcomeIndex"] == 0
    assert holder["displayUsernamePublic"] is True
    assert holder["amount"] == 10


def test_missing_and_empty_data_warn(caplog):
    caplog.set_level(logging.WARNING)
    rows, cursor = adapt_data_api_payload({"pagination": {"next_cursor": None}}, route="positions")
    assert rows == []
    assert cursor is None
    rows, cursor = adapt_data_api_payload({"data": None, "pagination": {}}, route="activity")
    assert rows == []
    rows, _ = adapt_data_api_payload(
        {"data": [], "pagination": {"next_cursor": None, "has_more": False, "limit": 1, "offset": 0}},
        route="trades",
    )
    assert rows == []
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) >= 3
    assert all("empty or missing data" in message for message in warnings)


def test_cursor_pagination_replaces_offset_and_does_not_stop_on_a_short_page():
    pages = {
        None: _page([{"token_id": "a", "size": 1}], cursor="cursor-1", limit=1),
        "cursor-1": _page([{"token_id": "b", "size": 1}], cursor="cursor-2", limit=1),
        "cursor-2": _page([], cursor=None, limit=1),
    }
    seen = []

    def fetch(url):
        seen.append(url)
        query = url.split("?", 1)[-1]
        cursor = None
        for part in query.split("&"):
            if part.startswith("cursor="):
                cursor = part.split("=", 1)[1]
        assert "offset" not in query
        return pages[cursor]

    rows = read_data_v2("trades", {"limit": 1, "offset": 0}, fetch, max_pages=5)
    assert [row["asset"] for row in rows] == ["a", "b"]
    assert len(seen) == 3
    assert "cursor=" not in seen[0]
    assert "cursor=cursor-1" in seen[1]
    assert "cursor=cursor-2" in seen[2]


def test_first_page_transport_failure_is_none_and_later_failure_keeps_rows():
    calls = {"n": 0}

    def fetch(url):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return _page([], cursor=None)

    assert read_data_v2("positions", {"user": "0x1"}, fetch, max_pages=2) is None

    calls["n"] = 0

    def fetch_then_fail(url):
        calls["n"] += 1
        if calls["n"] == 1:
            return _page([{"token_id": "a", "current_size": 2}], cursor="more")
        return None

    rows = read_data_v2("positions", {"user": "0x1"}, fetch_then_fail, max_pages=3)
    assert rows == [{"asset": "a", "size": 2}]


def test_sdk_position_mapping_accepts_v2_attributes():
    from scripts.position_sync import _iter_sdk_positions, _sdk_position_to_v1

    pos = SimpleNamespace(
        token_id="99",
        condition_id="0xcond",
        slug="slug",
        title="Title",
        avg_price="0.40",
        current_size="250",
        entry_cost_usdc="100",
        current_price="0.20",
        unrealized_pnl="-50",
        redeemable=True,
    )
    mapped = _sdk_position_to_v1(pos)
    assert mapped["asset"] == "99"
    assert mapped["conditionId"] == "0xcond"
    assert mapped["size"] == 250
    assert mapped["curPrice"] == 0.2
    assert mapped["initialValue"] == 100
    assert mapped["cashPnl"] == -50
    assert mapped["avgPrice"] == 0.4
    assert mapped["redeemable"] is True

    class Page:
        def __init__(self, items):
            self.items = items

    class Client:
        def list_positions(self, **kwargs):
            assert kwargs["filter_amount"] == 0.01
            assert kwargs["page_size"] == 500
            assert "size_threshold" not in kwargs
            yield Page([pos])

    assert list(_iter_sdk_positions(Client(), filter_amount=0.01)) == [pos]

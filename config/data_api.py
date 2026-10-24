"""Polymarket Data API v2 boundary.

Call sites keep the camelCase row shapes v1 returned. This module is the
only place that knows about the v2 envelope, snake_case fields, and cursor
pagination. URLs are built with ``config.polymarket_urls.data_url`` so the
Cloudflare Worker proxy stays the single route for reads.

``GET /v1/accounting/snapshot`` has no v2 counterpart and is not rewritten.
"""

from __future__ import annotations

import logging
import urllib.parse
from typing import Any, Callable

from config.polymarket_urls import data_url

logger = logging.getLogger("config.data_api")

# v2 rejects limit above this with HTTP 400.
V2_MAX_LIMIT = 1000

# Stays on v1. Do not prefix it with /v2.
ACCOUNTING_SNAPSHOT_PATH = "/v1/accounting/snapshot"

# v2 names that are not a pure snake_case → camelCase of the v1 field.
# Checked before the generic conversion. Live v1 /positions and /trades
# rows (2026-10-24) use the right-hand names.
_FIELD_RENAMES = {
    "token_id": "asset",
    "current_size": "size",
    "current_price": "curPrice",
    "unrealized_pnl": "cashPnl",
    "entry_cost_usdc": "initialValue",
    "total_size": "totalBought",
    "opposite_token_id": "oppositeAsset",
}

_DROPPED_PARAMS = ("offset", "market_id", "marketId")
# Sent by v1 callers. v2 returns HTTP 200 and ignores them, which is worse
# than a 400: /v2/trades?token_id=<real> is the unfiltered global tape.
_TOKEN_FILTER_PARAMS = ("asset_id", "assetId", "token_id", "tokenId")


def data_route_url(path: str) -> str:
    """URL for a data-api path. Accounting snapshot stays on v1; other reads are /v2."""
    raw = "/" + path.lstrip("/")
    base_path, _, query = raw.partition("?")
    if base_path == ACCOUNTING_SNAPSHOT_PATH:
        return data_url(raw)
    if base_path.startswith("/v2/"):
        route = base_path
    elif base_path.startswith("/v1/"):
        raise ValueError(f"no v2 route for {base_path}; only {ACCOUNTING_SNAPSHOT_PATH} stays on v1")
    else:
        route = "/v2" + base_path
    return data_url(route + (("?" + query) if query else ""))


def translate_params(params: dict | None) -> tuple[dict, str | None]:
    """Rewrite a v1 query dict into v2 query params.

    Returns ``(query, token_filter)``. ``token_filter`` is set when the caller
    passed ``asset_id`` / ``token_id``. Those names are not server filters on
    ``/v2/trades`` (a request that includes them is the global tape), so they
    are removed from the query and applied to the unwrapped rows instead.
    """
    raw = dict(params or {})
    token_filter = None
    for key in _TOKEN_FILTER_PARAMS:
        if raw.get(key) not in (None, ""):
            token_filter = str(raw[key])
        raw.pop(key, None)

    for key in _DROPPED_PARAMS:
        if key in raw:
            logger.warning("data-api v2: dropping rejected param %s=%r", key, raw.pop(key))

    market = raw.pop("market", None)
    if market not in (None, "") and not any(raw.get(k) for k in ("condition", "condition_id", "conditionId")):
        raw["condition"] = market

    for key in ("sizeThreshold", "size_threshold"):
        if key not in raw:
            continue
        amount = raw.pop(key)
        if amount in (None, ""):
            continue
        # v2 ignores sizeThreshold (HTTP 200, unfiltered). The replacement
        # is the TOKENS floor; the default dust floor is 0.1, so a caller
        # that asked for 0.01 must send filter_amount or those rows vanish.
        raw.setdefault("filter_type", "TOKENS")
        raw.setdefault("filter_amount", amount)

    sort_order = raw.pop("sortOrder", None)
    if sort_order is None:
        sort_order = raw.pop("sort_order", None)
    else:
        raw.pop("sort_order", None)
    if sort_order not in (None, "") and "sort_direction" not in raw and "sortDirection" not in raw:
        # v1 sortOrder is ignored on v2 (the feed stays newest-first). The
        # honored name is sort_direction=ASC|DESC.
        raw["sort_direction"] = str(sort_order).upper()

    # v1 sortBy=timestamp is ignored. Timestamp is already the only activity
    # sort, so drop that value instead of forwarding a param the server skips.
    sort_by = raw.pop("sortBy", None)
    if sort_by is None:
        sort_by = raw.pop("sort_by", None)
    else:
        raw.pop("sort_by", None)
    if sort_by not in (None, "") and str(sort_by).lower() != "timestamp":
        raw["sort_by"] = sort_by

    if "takerOnly" in raw and "taker_only" not in raw:
        raw["taker_only"] = raw.pop("takerOnly")

    if "limit" in raw and raw["limit"] not in (None, ""):
        try:
            limit = int(raw["limit"])
        except (TypeError, ValueError):
            limit = None
        if limit is not None:
            if limit > V2_MAX_LIMIT:
                logger.warning("data-api v2: limit %s exceeds %s and was clamped", limit, V2_MAX_LIMIT)
            raw["limit"] = max(0, min(limit, V2_MAX_LIMIT))
    return raw, token_filter


def _camel(key: str) -> str:
    if key in _FIELD_RENAMES:
        return _FIELD_RENAMES[key]
    if "_" not in key:
        return key
    head, *rest = key.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest if part)


def map_v2_value(value: Any) -> Any:
    """Recursively rename v2 row fields to the v1 names callers already read."""
    if isinstance(value, list):
        return [map_v2_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, item in value.items():
        out[_camel(key)] = map_v2_value(item)
    # v2 uses 1970-01-01 when Gamma has no end date. v1 omitted the date;
    # a sentinel epoch makes "has this market ended?" true for live markets.
    if out.get("endDate") == "1970-01-01":
        out["endDate"] = ""
    return out


def adapt_data_api_payload(payload: Any, *, route: str) -> tuple[list, str | None]:
    """Unwrap ``{data, pagination}`` and map rows. Empty or missing ``data`` logs WARNING.

    A transport failure is the fetch function's job (return None / raise).
    A documented v2 miss is ``data: null`` or ``[]``, and that is not an error,
    but it is indistinguishable from an outage if nobody logs it.
    """
    if isinstance(payload, list):
        logger.warning("data-api v2 %s: missing data envelope; got a bare list of %d", route, len(payload))
        if not payload:
            logger.warning("data-api v2 %s: empty or missing data", route)
        return [map_v2_value(row) if isinstance(row, dict) else row for row in payload], None

    if not isinstance(payload, dict) or "data" not in payload or payload.get("data") is None:
        logger.warning(
            "data-api v2 %s: empty or missing data (payload type=%s)",
            route,
            type(payload).__name__,
        )
        return [], None

    data = payload.get("data")
    if isinstance(data, dict):
        rows = [map_v2_value(data)]
    elif isinstance(data, list):
        if not data:
            logger.warning("data-api v2 %s: empty or missing data", route)
        rows = [map_v2_value(row) if isinstance(row, dict) else row for row in data]
    else:
        logger.warning("data-api v2 %s: empty or missing data (data type=%s)", route, type(data).__name__)
        rows = []

    pagination = payload.get("pagination") or {}
    next_cursor = pagination.get("next_cursor") if isinstance(pagination, dict) else None
    if next_cursor in ("", None):
        next_cursor = None
    return rows, next_cursor


def _encode(query: dict) -> str:
    items = []
    for key, value in query.items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        items.append((key, value))
    return urllib.parse.urlencode(items)


def request_url(route: str, params: dict | None = None, *, cursor: str | None = None) -> tuple[str, str | None]:
    """``(url, token_filter)`` for one v2 page. ``route`` is ``trades`` / ``positions`` / ..."""
    query, token_filter = translate_params(params)
    if cursor:
        query["cursor"] = cursor
    path = f"/v2/{route.lstrip('/')}"
    encoded = _encode(query)
    url = data_url(path + (("?" + encoded) if encoded else ""))
    return url, token_filter


def fetch_data_v2_page(
    route: str,
    params: dict | None,
    fetch: Callable[[str], Any],
    *,
    cursor: str | None = None,
) -> tuple[list | None, str | None]:
    """Fetch one page. ``(None, None)`` when ``fetch`` returns None (transport failure).

    ``fetch`` receives the full URL (proxy-aware) and returns parsed JSON.
    Rows are v1-shaped. A token filter from ``asset_id`` / ``token_id`` is
    applied here because the server ignores those query params.
    """
    url, token_filter = request_url(route, params, cursor=cursor)
    payload = fetch(url)
    if payload is None:
        return None, None
    rows, next_cursor = adapt_data_api_payload(payload, route=route)
    if token_filter:
        rows = [row for row in rows if str(row.get("asset") or "") == token_filter]
    return rows, next_cursor


def read_data_v2(
    route: str,
    params: dict | None,
    fetch: Callable[[str], Any],
    *,
    max_pages: int = 1,
) -> list | None:
    """Follow ``pagination.next_cursor`` up to ``max_pages``.

    Returns None when the first page cannot be fetched. A later page failure
    returns the rows already collected. An empty ``data`` array is ``[]``
    (and a WARNING), not a transport failure. A short page is not the end of
    the walk — only a null cursor is.
    """
    cursor = None
    collected: list = []
    for index in range(max_pages):
        rows, cursor = fetch_data_v2_page(route, params, fetch, cursor=cursor)
        if rows is None:
            return None if index == 0 else collected
        collected.extend(rows)
        if not cursor:
            break
    return collected

"""Polymarket Data API v2 boundary.

Call sites keep the camelCase row shapes v1 returned. This module is the
only place that knows about the v2 envelope, snake_case fields, and cursor
pagination. URLs are built with ``config.polymarket_urls.data_url`` so the
Cloudflare Worker proxy stays the single route for reads.

``GET /v1/accounting/snapshot`` has no v2 counterpart and is not rewritten.

CLOB ``GET /prices-history`` is replaced by Data API ``GET /v2/prices-history``
(changelog 2026-09-04, "Price history moves to the data host").
``read_price_history`` speaks that route and returns the old ``{t, p}`` points.
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


# CLOB /prices-history → GET /v2/prices-history.
# https://docs.polymarket.com/api-reference/markets/get-a-tokens-price-history
# Exactly one window form (interval, start/end, or as_of). fidelity is minutes;
# bucket_seconds is seconds. Explicit start/end spans at most 15 days.
_BUCKET_MIN = 60
_BUCKET_MAX = 86400
_PRICE_HISTORY_WINDOW_CAP_S = 15 * 24 * 3600
_INTERVAL_BUCKET_FLOOR = {"max": 600, "all": 600, "1m": 600, "1w": 300}


def bucket_seconds_for(fidelity_minutes: int | None, interval: str | None, bucket_seconds: int | None) -> int | None:
    """Translate CLOB ``fidelity`` (minutes) into v2 ``bucket_seconds``.

    Returns None when neither was requested, so the server picks the width.
    Raises the documented per-interval floor (max/all/1m → 600, 1w → 300)
    and clamps to 60…86400.
    """
    if bucket_seconds is None and fidelity_minutes is None:
        return None
    raw = int(bucket_seconds) if bucket_seconds is not None else int(fidelity_minutes) * 60
    floor = _INTERVAL_BUCKET_FLOOR.get((interval or "").strip(), _BUCKET_MIN)
    clamped = min(_BUCKET_MAX, max(floor, raw))
    if clamped != raw:
        logger.warning(
            "data-api v2 prices-history: bucket_seconds %s clamped to %s for interval %s",
            raw, clamped, interval or "-",
        )
    return clamped


def price_history_params(
    token_id: str,
    *,
    interval: str | None = None,
    fidelity: int | None = None,
    bucket_seconds: int | None = None,
    start_ts: int | None = None,
    end_ts: int | None = None,
    as_of: int | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> dict:
    """v2 query params for one prices-history request.

    ``market`` and ``fidelity`` are not sent. ``start_ts`` / ``end_ts`` become
    ``start`` / ``end``. When ``start_ts`` is set, ``interval`` is dropped:
    v2 allows exactly one window form and rejects the combination.
    """
    query: dict = {"token_id": str(token_id)}
    window_interval: str | None = None
    send_bucket = True
    if as_of is not None:
        if any(v is not None for v in (interval, start_ts, end_ts, fidelity, bucket_seconds)):
            logger.warning(
                "data-api v2 prices-history: as_of is exclusive; dropping interval/start/bucket",
            )
        query["as_of"] = int(as_of)
        send_bucket = False
    elif start_ts is not None:
        if interval:
            logger.warning(
                "data-api v2 prices-history: dropping interval=%s because start is set",
                interval,
            )
        query["start"] = int(start_ts)
        if end_ts is not None:
            query["end"] = int(end_ts)
    elif interval:
        query["interval"] = interval
        window_interval = interval
    else:
        logger.warning("data-api v2 prices-history: no window form (interval, start, or as_of)")
    if send_bucket:
        width = bucket_seconds_for(fidelity, window_interval, bucket_seconds)
        if width is not None:
            query["bucket_seconds"] = width
    if cursor:
        query["cursor"] = cursor
    if limit is not None:
        query["limit"] = int(limit)
    return query


def price_history_url(token_id: str, **kwargs) -> str:
    """Proxy-aware URL for ``GET /v2/prices-history``."""
    query = price_history_params(token_id, **kwargs)
    encoded = _encode(query)
    return data_url("/v2/prices-history" + (("?" + encoded) if encoded else ""))


def _price_point(row: Any) -> dict | None:
    """One v2 point → the CLOB history shape ``{t, p}`` (epoch seconds, 0..1)."""
    if not isinstance(row, dict):
        return None
    ts = row.get("timestamp")
    price = row.get("price")
    if ts is None or price is None:
        return None
    return {"t": int(ts), "p": float(price)}


def _price_history_page(payload: Any, token_id: str) -> tuple[list | None, str | None]:
    """Unwrap one prices-history page. ``(None, None)`` is not used here.

    A missing or null ``data`` is an empty page plus a WARNING. Cursor walk
    stops only on a null ``next_cursor``.
    """
    if not isinstance(payload, dict) or "data" not in payload or payload.get("data") is None:
        logger.warning(
            "data-api v2 prices-history: empty or missing data (token_id=%s)", token_id,
        )
        pagination = payload.get("pagination") if isinstance(payload, dict) else None
        nxt = pagination.get("next_cursor") if isinstance(pagination, dict) else None
        return [], None if nxt in ("", None) else nxt
    data = payload["data"]
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        logger.warning(
            "data-api v2 prices-history: empty or missing data (token_id=%s, data type=%s)",
            token_id, type(data).__name__,
        )
        return [], None
    points = [pt for row in data if (pt := _price_point(row))]
    pagination = payload.get("pagination") or {}
    nxt = pagination.get("next_cursor") if isinstance(pagination, dict) else None
    if nxt in ("", None):
        nxt = None
    return points, nxt


def _price_history_windows(start_ts: int | None, end_ts: int | None) -> list[tuple[int | None, int | None]]:
    """Split an explicit start/end into <=15-day chunks. ``end`` is exclusive."""
    if start_ts is None or end_ts is None or end_ts - start_ts <= _PRICE_HISTORY_WINDOW_CAP_S:
        return [(start_ts, end_ts)]
    windows = []
    cursor = int(start_ts)
    end = int(end_ts)
    while cursor < end:
        nxt = min(cursor + _PRICE_HISTORY_WINDOW_CAP_S, end)
        windows.append((cursor, nxt))
        cursor = nxt
    return windows


def read_price_history(
    token_id: str,
    fetch: Callable[[str], Any],
    *,
    interval: str | None = None,
    fidelity: int | None = None,
    bucket_seconds: int | None = None,
    start_ts: int | None = None,
    end_ts: int | None = None,
    as_of: int | None = None,
    max_pages: int = 5,
) -> list | None:
    """Price history as CLOB ``{t, p}`` points, oldest first.

    ``fetch`` receives the full URL and returns parsed JSON, or None on
    transport failure. Returns None when the first page cannot be fetched
    (WARNING). An empty ``data`` array is ``[]`` (WARNING). A short page
    does not end the walk. Explicit ranges longer than 15 days are split,
    because v2 rejects a wider ``start``/``end``.
    """
    # v2 accepts exactly one window form. Resolve that once so a multi-page
    # walk does not repeat the warning on every cursor request.
    use_interval = interval
    use_start = start_ts
    use_as_of = as_of
    use_fidelity, use_bucket = fidelity, bucket_seconds
    if as_of is not None:
        if any(v is not None for v in (interval, start_ts, end_ts, fidelity, bucket_seconds)):
            logger.warning(
                "data-api v2 prices-history: as_of is exclusive; dropping interval/start/bucket",
            )
        use_interval = None
        use_start = None
        use_fidelity, use_bucket = None, None
        windows = [(None, None)]
    elif start_ts is not None:
        if interval:
            logger.warning(
                "data-api v2 prices-history: dropping interval=%s because start is set",
                interval,
            )
        use_interval = None
        windows = _price_history_windows(start_ts, end_ts)
    else:
        windows = [(None, None)]

    collected: list = []
    saw_page = False
    for win_i, (win_start, win_end) in enumerate(windows):
        cursor = None
        for _index in range(max_pages):
            url = price_history_url(
                token_id,
                interval=use_interval,
                fidelity=use_fidelity,
                bucket_seconds=use_bucket,
                start_ts=win_start if use_start is not None else None,
                end_ts=win_end if use_start is not None else None,
                as_of=use_as_of,
                cursor=cursor,
            )
            payload = fetch(url)
            if payload is None:
                logger.warning(
                    "data-api v2 prices-history: empty or missing data (fetch failed, token_id=%s)",
                    token_id,
                )
                return None if not saw_page else collected
            saw_page = True
            points, cursor = _price_history_page(payload, token_id)
            collected.extend(points or [])
            if not cursor:
                break
        else:
            if cursor:
                logger.warning(
                    "data-api v2 prices-history: page cap (%d) on window %d token_id=%s",
                    max_pages, win_i, token_id,
                )
    # Stable order across 15-day chunks. Duplicate timestamps keep the first.
    # An empty series after a successful fetch is a WARNING so a retired
    # route or a proxy miss does not look like "no points yet".
    if saw_page and not collected:
        logger.warning(
            "data-api v2 prices-history: empty or missing data (token_id=%s)", token_id,
        )
    deduped: list = []
    seen = set()
    for pt in sorted(collected, key=lambda row: row["t"]):
        if pt["t"] in seen:
            continue
        seen.add(pt["t"])
        deduped.append(pt)
    return deduped

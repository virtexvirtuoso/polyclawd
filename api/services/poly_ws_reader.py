"""
poly_ws_reader.py — fallback-safe reader for the Polymarket WS live store.

The `polyclawd-ws` service publishes live order books / trades / status to
memcached. This module lets the API + scanners read that live state WITHOUT
coupling to the WS process and WITHOUT ever blocking the event loop:

- All functions are async and use a short memcached timeout.
- On miss / stale (book keys carry a ~15s exptime) / any error -> return None,
  so the caller falls back to the REST path (odds.polymarket_clob.get_orderbook).
  WS down ≡ today's behavior, never worse.

Design: 02-Projects/Polyclawd/Development/Phase3-WebSocket-Design-2026-06-02.md
"""
import json
from typing import Dict, Optional

try:
    import aiomcache
except ImportError:  # pragma: no cover
    aiomcache = None

_MC_HOST, _MC_PORT = "localhost", 11211
_GET_TIMEOUT = 0.25  # seconds — never let a slow/missing memcached stall a request
_client = None


def _mc():
    global _client
    if _client is None and aiomcache is not None:
        _client = aiomcache.Client(_MC_HOST, _MC_PORT, pool_size=2)
    return _client


async def _get_json(key: str) -> Optional[Dict]:
    import asyncio
    c = _mc()
    if c is None or not key:
        return None
    try:
        raw = await asyncio.wait_for(c.get(key.encode()), timeout=_GET_TIMEOUT)
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


async def get_live_book(token_id: str) -> Optional[Dict]:
    """Live order-book snapshot for a token from the WS store, or None.

    Returns dict: {token_id, best_bid, best_ask, spread, mid, bids:[[p,s]],
    asks:[[p,s]], ts, hash, synced, pub_ts}. None => not live; caller uses REST.
    """
    return await _get_json(f"poly:book:{token_id}")


async def get_live_trade(token_id: str) -> Optional[Dict]:
    """Most recent trade for a token from the WS store, or None."""
    return await _get_json(f"poly:trade:{token_id}")


async def is_live(token_id: str) -> bool:
    """True iff a fresh live book exists for the token (exptime gates staleness)."""
    snap = await get_live_book(token_id)
    return snap is not None and snap.get("best_ask") is not None


async def get_ws_status() -> Optional[Dict]:
    """polyclawd-ws health/status, or None if the service isn't publishing."""
    return await _get_json("poly:ws:status")


async def get_live_orderbook(token_id: str):
    """Build an `odds.polymarket_clob.OrderBook` from the live WS snapshot, or None.

    Lets the executable-edge path consume the live book (pass as `book=` to
    `size_to_book`/`executable_edge`) instead of a REST fetch. None => use REST.
    """
    snap = await get_live_book(token_id)
    if not snap or snap.get("best_ask") is None:
        return None
    try:
        from odds import polymarket_clob as clob
    except Exception:
        return None
    try:
        bids = [clob.OrderBookLevel(float(p), float(s)) for p, s in snap.get("bids", [])]
        asks = [clob.OrderBookLevel(float(p), float(s)) for p, s in snap.get("asks", [])]
    except (ValueError, TypeError):
        return None
    if not asks:
        return None
    return clob.OrderBook(
        market_id="", token_id=token_id, outcome="",
        bids=bids, asks=asks,
        spread=float(snap.get("spread") or 0.0),
        mid_price=float(snap.get("mid") or 0.0),
        timestamp=str(snap.get("ts") or ""),
    )


async def register_watch(token_ids) -> None:
    """Hint the polyclawd-ws service to stream these tokens (fire-and-forget).

    RMW on poly:ws:registered (exptime 600s so resolved markets fall out). Never
    raises; a slow/missing memcached is a no-op. Keep callers off the hot path.
    """
    import asyncio
    c = _mc()
    if c is None or not token_ids:
        return
    try:
        raw = await asyncio.wait_for(c.get(b"poly:ws:registered"), timeout=_GET_TIMEOUT)
        cur = set(json.loads(raw)) if raw else set()
        cur.update(str(t) for t in token_ids if t)
        merged = list(cur)[:500]
        await asyncio.wait_for(
            c.set(b"poly:ws:registered", json.dumps(merged).encode(), exptime=600),
            timeout=_GET_TIMEOUT,
        )
    except Exception:
        return


# ── Sync variants (for executor-thread callers like sports_edge_common) ──
import socket as _socket


def _mc_get_sync(key: str, timeout: float = 0.25):
    """Blocking memcached GET via raw text protocol. For executor threads only.
    Never raises; returns bytes value or None (miss/error)."""
    try:
        s = _socket.create_connection((_MC_HOST, _MC_PORT), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(b"get " + key.encode() + b"\r\n")
        buf = b""
        while b"END\r\n" not in buf and len(buf) < 2_000_000:
            chunk = s.recv(8192)
            if not chunk:
                break
            buf += chunk
        try:
            s.close()
        except Exception:
            pass
    except Exception:
        return None
    if not buf.startswith(b"VALUE"):
        return None
    try:
        _, _, rest = buf.partition(b"\r\n")
        return rest.split(b"\r\nEND\r\n", 1)[0]
    except Exception:
        return None


def get_live_orderbook_sync(token_id: str):
    """Sync build of an OrderBook from the live WS snapshot, or None (-> REST)."""
    raw = _mc_get_sync(f"poly:book:{token_id}")
    if not raw:
        return None
    try:
        snap = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not snap or snap.get("best_ask") is None:
        return None
    try:
        from odds import polymarket_clob as clob
        bids = [clob.OrderBookLevel(float(p), float(s)) for p, s in snap.get("bids", [])]
        asks = [clob.OrderBookLevel(float(p), float(s)) for p, s in snap.get("asks", [])]
    except Exception:
        return None
    if not asks:
        return None
    return clob.OrderBook(market_id="", token_id=token_id, outcome="", bids=bids, asks=asks,
                          spread=float(snap.get("spread") or 0.0),
                          mid_price=float(snap.get("mid") or 0.0),
                          timestamp=str(snap.get("ts") or ""))

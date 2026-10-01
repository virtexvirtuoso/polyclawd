#!/usr/bin/env python3
"""
book_capture.py — raw Polymarket market-channel capture to zstd parquet (research only).

A standalone process holds its own `market` WebSocket and persists EVERY raw event
(full-depth books, price changes, trades) for offline research. Zero trading blast
radius: nothing here is read by the trading stack. The WS read loop never blocks on
disk — rows are buffered in memory and written from a worker thread.

Layers: the normalizer + buffered writer live in services/book_capture_writer.py
(API, layout, memory and clock notes there; the public names are re-exported here),
and BookCapture below (the WS lifecycle: watchset universe, reconnect/backoff, gap
meta, SIGTERM, status HTTP, CLI — see the "capture process" section).

Run:  python -m services.book_capture --seconds 60 --tokens a,b      (static universe)
      python -m services.book_capture --seconds 0                     (service: watchset)
"""

import argparse
import asyncio
import json
import os
import random
import signal
import subprocess
import sys
import time

try:
    import websockets
except ImportError:  # pragma: no cover — normalizer/writer still usable without it
    websockets = None

try:
    import aiomcache
except ImportError:  # pragma: no cover — only needed for the memcached watchset
    aiomcache = None

from services.book_capture_writer import (  # noqa: F401 — re-exported public writer API
    _HEARTBEATS,
    BOOK_SCHEMA,
    BUFFER_CAP,
    CHANGES_SCHEMA,
    DEFAULT_ROOT,
    GB,
    HOUR_MS,
    META_SCHEMA,
    QUARANTINE_SCHEMA,
    SCHEMAS,
    TRADES_SCHEMA,
    BookCaptureWriter,
    WriterLockedError,
    _now_ms,
    normalize_frame,
)

# ---------------------------------------------------------------- capture process
#
# BookCapture owns ONE market-channel WebSocket plus a BookCaptureWriter. The read
# path is: frame -> writer.add_frame(raw, recv_ts=now_ms) -> next frame. Nothing else.
#
# Universe: the token set poly_ws publishes to memcached `poly:ws:watchset` (JSON
# list, refreshed ~60 s, exptime 180), or a static `--tokens` list. The market
# channel IGNORES mid-stream subscribe messages, so a universe change is applied by
# reconnecting and resubscribing the full set — at most once per
# UNIVERSE_RECONNECT_MIN_S; changes arriving sooner wait for the next allowed slot.
#
# Meta rows written here (dataset `meta`, detail_json):
#   capture_start     {pid, root, universe_size, static_universe, ws_url, git_sha}
#   capture_start is always the FIRST lifecycle row of a run (before any startup
#                     watchset_missing).
#   capture_stop      {reason, stats}           (written BEFORE writer.close()).
#                     capture_stop TERMINATES any open gap: a gap_start with no gap_end
#                     before the next capture_stop ends at capture_stop (no gap_end is
#                     written); stats.gap_open says whether one was open.
#   universe_change   {added_count, removed_count, added, removed, universe_size}
#                     — at the subscribe that applies it (diff vs previous subscription)
#   watchset_missing  {kept_universe_size, startup, error} — once per outage
#   watchset_restored {outage_s, universe_size}
#   gap_start         {last_recv_ts, last_data_ts, reason, reconnects} — an established
#                     connection ended; last_recv_ts = last frame of ANY kind (incl. PONG),
#                     last_data_ts = last non-heartbeat frame (None if none yet)
#                     (reason "universe_change", "data silence" from the watchdog, or the
#                     connection error)
#   gap_end           {gap_ms, tokens}          — next successful subscribe; gap_ms is
#                     subscribe time minus min(last data frame, last frame of any kind),
#                     where "last data frame" falls back to the ended connection's connect
#                     time if it carried no data — an upper bound on the uncovered interval
#                     even when PONGs kept arriving (data-silence gaps). The server sends
#                     a fresh full `book` per token on subscribe (free re-anchor).

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
MC_HOST, MC_PORT = "localhost", 11211
WATCHSET_KEY = b"poly:ws:watchset"
CAPTURE_HTTP_PORT = int(os.environ.get("POLY_CAPTURE_PORT", "8424"))

UNIVERSE_INTERVAL_S = 60.0  # watchset re-read cadence
UNIVERSE_RECONNECT_MIN_S = 120.0  # min gap between universe-driven reconnects
NO_UNIVERSE_RETRY_S = 10.0  # startup: retry cadence while there is no universe
MC_TIMEOUT_S = 5.0  # memcached get must not hang the universe loop
PING_INTERVAL_S = 10.0  # app-level "PING" text heartbeat (server answers "PONG")
BACKOFF_BASE_S = 1.0  # error reconnect: 1 s doubling to 60 s + jitter (poly_ws)
BACKOFF_MAX_S = 60.0
BACKOFF_JITTER_S = 0.5
STABLE_RESET_S = 30.0  # a connection that lived longer resets the backoff
DATA_SILENCE_S = 300.0  # no DATA frame (PONGs don't count) this long -> reconnect;
# live rate is ~20 frames/s across ~240 tokens. 0 = off
DATA_SILENCE_MIN_CONN_S = 120.0  # ...only on a connection at least this old
CLOSE_TIMEOUT_S = 20.0  # bound on writer.close() at shutdown (hung disk)
TEARDOWN_TIMEOUT_S = 3.0  # bound on cancelling each group of background tasks
CONTROL_TICK_S = 1.0  # connection control-loop wake-up granularity
EXIT_WRITER_LOCKED = 3
EXIT_CLOSE_TIMEOUT = 1  # writer.close() timed out: data may be unflushed


def _git_sha():
    """Short git sha of this checkout, or None (cheap: one subprocess at startup)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            text=True,
            timeout=2,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


def _parse_watchset(raw):
    """memcached value -> non-empty list of token-id strings, else None (unusable)."""
    if not raw:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    toks = json.loads(raw)
    if not isinstance(toks, list):
        return None
    toks = [str(t) for t in toks if isinstance(t, (str, int)) and not isinstance(t, bool) and str(t)]
    return toks or None


class _ServerClosed(ConnectionError):
    """The server ended the stream without an error (clean close)."""


class _DataSilence(ConnectionError):
    """Connected and heartbeating, but no data frame for DATA_SILENCE_S."""


class BookCapture:
    """WS lifecycle around a BookCaptureWriter. See the section comment above."""

    def __init__(
        self,
        writer,
        *,
        tokens=None,
        universe_fn=None,
        ws_url=WS_URL,
        http_port=CAPTURE_HTTP_PORT,
        universe_interval_s=UNIVERSE_INTERVAL_S,
        universe_reconnect_min_s=UNIVERSE_RECONNECT_MIN_S,
        no_universe_retry_s=NO_UNIVERSE_RETRY_S,
        ping_interval_s=PING_INTERVAL_S,
        backoff_base_s=BACKOFF_BASE_S,
        backoff_max_s=BACKOFF_MAX_S,
        jitter_s=BACKOFF_JITTER_S,
        stable_reset_s=STABLE_RESET_S,
        data_silence_s=DATA_SILENCE_S,
        data_silence_min_conn_s=DATA_SILENCE_MIN_CONN_S,
        close_timeout_s=CLOSE_TIMEOUT_S,
        teardown_timeout_s=TEARDOWN_TIMEOUT_S,
        tick_s=CONTROL_TICK_S,
        now_ms_fn=None,
    ):
        self.writer = writer
        self.static = tokens is not None
        self.tokens = [str(t) for t in tokens] if tokens is not None else []
        self._universe_fn = universe_fn or self._read_watchset_mc
        self.ws_url = ws_url
        self.http_port = http_port
        self.universe_interval_s = universe_interval_s
        self.universe_reconnect_min_s = universe_reconnect_min_s
        self.no_universe_retry_s = no_universe_retry_s
        self.ping_interval_s = ping_interval_s
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self.jitter_s = jitter_s
        self.stable_reset_s = stable_reset_s
        self.data_silence_s = data_silence_s
        self.data_silence_min_conn_s = data_silence_min_conn_s
        self.close_timeout_s = close_timeout_s
        self.teardown_timeout_s = teardown_timeout_s
        self.tick_s = tick_s
        self._now_ms = now_ms_fn or _now_ms

        self.desired = set(self.tokens)  # universe we want subscribed
        self.subscribed = set()  # tokens sent on the current/last connection
        self._prev_subscribed = None  # set of the previous successful subscribe
        self.connected = False
        self.reconnects = 0
        self.gap_count = 0
        self.frames = 0  # non-heartbeat frames handed to the writer
        self.heartbeats = 0
        self.last_recv_ms = None  # last frame of ANY kind (liveness, gap bound)
        self.last_data_ms = None  # last NON-heartbeat frame (data-silence watchdog)
        self._established = False  # current run_once attempt reached subscribe
        self._conn_start = None  # monotonic start of the current connection
        self._conn_start_ms = None
        self._gap_start_ms = None
        self._gap_last_recv = None
        self._gap_last_data = None
        self.universe_reconnect_decisions = []  # monotonic times of planned reconnects
        self.start_ms = self._now_ms()
        self._gap_open = False
        self._backoff = backoff_base_s
        self._last_universe_reconnect = float("-inf")
        self._watchset_missing_since = None
        self._mc = None
        self._stop = asyncio.Event()
        self._stop_reason = None
        self._shutdown_result = None  # None until shutdown() ran; then True/False
        self._read_task = None
        self._tasks = []
        self._http = None
        self.close_timed_out = False
        self.teardown_abandoned = False

    # -- universe --------------------------------------------------------------------

    async def _read_watchset_mc(self):
        if aiomcache is None:
            raise RuntimeError("aiomcache not installed (use --tokens for a static universe)")
        if self._mc is None:
            self._mc = aiomcache.Client(MC_HOST, MC_PORT, pool_size=1)
        try:
            raw = await asyncio.wait_for(self._mc.get(WATCHSET_KEY), MC_TIMEOUT_S)
        except asyncio.TimeoutError:
            # a get cancelled mid-read may leave an unread reply in the pooled
            # connection; drop the whole client so the next read starts clean
            mc, self._mc = self._mc, None
            try:
                await asyncio.wait_for(mc.close(), MC_TIMEOUT_S)
            except BaseException:
                pass
            raise
        return _parse_watchset(raw)

    async def _read_universe(self):
        """-> (tokens or None, error string or None). Never raises."""
        try:
            return await self._universe_fn(), None
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"

    async def _refresh_universe(self, startup=False):
        """Re-read the universe. Missing/unreadable/empty -> keep the last one and
        record watchset_missing once per outage. Never raises."""
        if self.static:
            return
        self._apply_universe_read(*await self._read_universe(), startup=startup)

    def _apply_universe_read(self, toks, err, startup=False):
        if not toks:
            if self._watchset_missing_since is None:
                self._watchset_missing_since = time.monotonic()
                print(
                    f"[universe] watchset missing/unreadable ({err or 'no key'}) — "
                    f"keeping last universe ({len(self.desired)} tokens)"
                )
                self.writer.add_meta(
                    "watchset_missing", {"kept_universe_size": len(self.desired), "startup": startup, "error": err}
                )
            return
        if self._watchset_missing_since is not None:
            outage = round(time.monotonic() - self._watchset_missing_since, 1)
            self._watchset_missing_since = None
            print(f"[universe] watchset back after {outage}s ({len(toks)} tokens)")
            self.writer.add_meta("watchset_restored", {"outage_s": outage, "universe_size": len(toks)})
        self.desired = set(toks)

    async def _universe_loop(self):
        while not self._stop.is_set():
            if await self._sleep(self.universe_interval_s):
                return
            await self._refresh_universe()

    def _universe_reconnect_due(self):
        if self.static or not self.desired or self.desired == self.subscribed:
            return False
        return time.monotonic() - self._last_universe_reconnect >= self.universe_reconnect_min_s

    # -- helpers ---------------------------------------------------------------------

    async def _sleep(self, seconds):
        """Interruptible sleep. True if stop was requested."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.0, seconds))
        except asyncio.TimeoutError:
            pass
        return self._stop.is_set()

    def request_stop(self, reason):
        """Signal-safe: ask run() to stop reading and shut down."""
        if self._stop_reason is None:
            self._stop_reason = reason
            print(f"[capture] stop requested: {reason}")
        self._stop.set()

    def _note_conn_ended(self, lasted_s):
        """Any established connection ending (error OR planned): a stable one (> 30 s)
        resets the backoff so the next error starts at the base wait again."""
        if lasted_s is not None and lasted_s > self.stable_reset_s:
            self._backoff = self.backoff_base_s

    def _error_wait(self):
        """Backoff after an error disconnect (poly_ws policy): 1 s doubling to 60 s +
        jitter; the reset after a stable connection happens in _note_conn_ended."""
        wait = min(self.backoff_max_s, self._backoff) + random.uniform(0, self.jitter_s)
        self._backoff = min(self.backoff_max_s, self._backoff * 2)
        return wait

    # -- ws --------------------------------------------------------------------------

    async def _app_ping(self, ws):
        while True:
            await asyncio.sleep(self.ping_interval_s)
            try:
                await ws.send("PING")
            except Exception:
                return

    async def _read_loop(self, ws):
        add_frame = self.writer.add_frame
        async for raw in ws:
            now = self._now_ms()
            self.last_recv_ms = now
            if raw in _HEARTBEATS:
                self.heartbeats += 1
                continue
            add_frame(raw, recv_ts=now)
            self.last_data_ms = now
            self.frames += 1
        raise _ServerClosed("server closed the connection")

    def _on_subscribed(self, sub):
        now = self._now_ms()
        prev = self._prev_subscribed
        if prev is not None and prev != set(sub):
            added, removed = sorted(set(sub) - prev), sorted(prev - set(sub))
            print(f"[universe] resubscribed: +{len(added)} -{len(removed)} (now {len(sub)})")
            self.writer.add_meta(
                "universe_change",
                {
                    "added_count": len(added),
                    "removed_count": len(removed),
                    "added": added,
                    "removed": removed,
                    "universe_size": len(sub),
                },
                recv_ts=now,
            )
        if self._gap_open:
            self._gap_open = False
            known = [t for t in (self._gap_last_data, self._gap_last_recv) if t is not None]
            base = min(known) if known else self._gap_start_ms
            self.writer.add_meta("gap_end", {"gap_ms": max(0, now - base), "tokens": len(sub)}, recv_ts=now)
        self._prev_subscribed = set(sub)

    def _on_disconnect(self, reason):
        """An established connection ended (error or planned) -> gap_start."""
        self.gap_count += 1
        self._gap_open = True
        self._gap_start_ms = self._now_ms()
        self._gap_last_recv = self.last_recv_ms
        # Data coverage ended at the last DATA frame — or, if this connection never
        # carried one, at its connect (PONGs keep last_recv fresh but cover nothing).
        self._gap_last_data = self.last_data_ms if self.last_data_ms is not None else self._conn_start_ms
        self.writer.add_meta(
            "gap_start",
            {
                "last_recv_ts": self.last_recv_ms,
                "last_data_ts": self.last_data_ms,
                "reason": reason,
                "reconnects": self.reconnects,
            },
            recv_ts=self._gap_start_ms,
        )

    async def run_once(self, deadline):
        """One connection. Returns "stop" | "deadline" | "universe_change"; raises on
        any connection error (incl. a clean server close). Sets self._established."""
        self._established = False
        async with websockets.connect(
            self.ws_url, ping_interval=10, ping_timeout=25, max_size=2**23, open_timeout=15, close_timeout=2
        ) as ws:
            sub = sorted(self.desired)
            await ws.send(json.dumps({"assets_ids": sub, "type": "market"}))
            self.subscribed = set(sub)
            self.connected = True
            self._established = True
            self._conn_start = time.monotonic()
            self._conn_start_ms = self._now_ms()
            self._on_subscribed(sub)
            print(f"[sub] {len(sub)} tokens")
            ping_task = asyncio.create_task(self._app_ping(ws))
            self._read_task = read_task = asyncio.create_task(self._read_loop(ws))
            stop_task = asyncio.create_task(self._stop.wait())
            try:
                while True:
                    done, _ = await asyncio.wait(
                        {read_task, stop_task}, timeout=self.tick_s, return_when=asyncio.FIRST_COMPLETED
                    )
                    if read_task in done:
                        read_task.result()  # raises the connection error
                        raise _ServerClosed("read loop ended")
                    if self._stop.is_set():
                        return "stop"
                    if time.time() >= deadline:
                        return "deadline"
                    self._check_data_silence()
                    if self._universe_reconnect_due():
                        self._last_universe_reconnect = time.monotonic()
                        self.universe_reconnect_decisions.append(self._last_universe_reconnect)
                        print(
                            f"[universe] resubscribe via reconnect "
                            f"(desired={len(self.desired)} subscribed={len(self.subscribed)})"
                        )
                        return "universe_change"
            finally:
                self.connected = False
                self._note_conn_ended(time.monotonic() - self._conn_start)
                for t in (ping_task, read_task, stop_task):
                    t.cancel()
                await asyncio.gather(ping_task, read_task, stop_task, return_exceptions=True)
                self._read_task = None

    def _check_data_silence(self):
        """Heartbeats prove the socket, not the feed: raise _DataSilence when a
        connection older than data_silence_min_conn_s has had no data frame for
        data_silence_s (measured from the later of last data and this connect)."""
        if not self.data_silence_s or time.monotonic() - self._conn_start <= self.data_silence_min_conn_s:
            return
        ref = max(self.last_data_ms or 0, self._conn_start_ms)
        silent_s = (self._now_ms() - ref) / 1000
        if silent_s > self.data_silence_s:
            print(f"[watchdog] no data frames for {silent_s:.0f}s (heartbeats={self.heartbeats}) — reconnecting")
            raise _DataSilence("data silence")

    async def _wait_for_universe(self, deadline):
        """Startup: never connect with an empty set — retry every no_universe_retry_s."""
        while not self.desired and time.time() < deadline:
            if await self._sleep(min(self.no_universe_retry_s, max(0.0, deadline - time.time()))):
                return
            await self._refresh_universe(startup=True)

    async def run(self, seconds):
        """Capture until `seconds` elapse (<=0 = forever), stop is requested, or a
        signal arrives. Always ends with shutdown() (capture_stop + writer.close())."""
        if websockets is None:  # pragma: no cover
            raise SystemExit("websockets not installed")
        deadline = float("inf") if seconds <= 0 else time.time() + seconds
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.request_stop, sig.name)
            except (NotImplementedError, RuntimeError, ValueError):  # pragma: no cover
                pass
        reason = "deadline"
        try:
            self._tasks.append(asyncio.create_task(self.writer.run_flush_loop()))
            self._http = await self._start_http()
            # Read the universe first (for universe_size) but record its outcome only
            # AFTER capture_start, so capture_start is always the first lifecycle row.
            first = None if self.static else await self._read_universe()
            if first is not None and first[0]:
                self.desired = set(first[0])
            self.writer.add_meta(
                "capture_start",
                {
                    "pid": os.getpid(),
                    "root": str(self.writer.root),
                    "universe_size": len(self.desired),
                    "static_universe": self.static,
                    "ws_url": self.ws_url,
                    "git_sha": _git_sha(),
                },
            )
            if first is not None:
                self._apply_universe_read(*first, startup=True)
            await self._wait_for_universe(deadline)
            if not self.static:
                self._tasks.append(asyncio.create_task(self._universe_loop()))
            while not self._stop.is_set() and time.time() < deadline:
                try:
                    outcome = await self.run_once(deadline)
                    if outcome == "universe_change":
                        self.reconnects += 1
                        self._on_disconnect("universe_change")
                        continue  # planned: reconnect now, no backoff
                    break  # stop / deadline
                except Exception as e:
                    self.connected = False
                    if self._stop.is_set():
                        break
                    self.reconnects += 1
                    if self._established:
                        self._on_disconnect(str(e) if isinstance(e, _DataSilence) else f"{type(e).__name__}: {e}"[:500])
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    wait = self._error_wait()
                    print(f"[reconnect #{self.reconnects}] {type(e).__name__}: {e} -> {wait:.1f}s")
                    if await self._sleep(min(wait, remaining)):
                        break
            reason = self._stop_reason or "deadline"
        except BaseException as e:
            reason = self._stop_reason or f"crash: {type(e).__name__}: {e}"[:500]
            raise
        finally:
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.remove_signal_handler(sig)
                except Exception:  # pragma: no cover
                    pass
            await self.shutdown(reason)
            self.summary()

    async def shutdown(self, reason):
        """Stop reading, write capture_stop, then close the writer with a bounded wait.
        Returns True if the writer closed cleanly, False if close timed out/failed.
        Idempotent (later calls return the first result)."""
        if self._shutdown_result is not None:
            return self._shutdown_result
        self._shutdown_result = False
        if self._stop_reason is None:
            self._stop_reason = reason
        self._stop.set()
        await self._cancel_bounded([self._read_task], "read")
        self.connected = False
        self.writer.add_meta("capture_stop", {"reason": reason, "stats": self.status()})
        # asyncio.wait (not wait_for): on timeout we must NOT await the cancellation of a
        # close() stuck in a disk thread — that would hang right here.
        close_task = asyncio.ensure_future(self.writer.close())
        done, _ = await asyncio.wait({close_task}, timeout=self.close_timeout_s)
        ok = False
        if close_task in done:
            try:
                close_task.result()
                ok = True
            except Exception as e:
                print(f"[capture] writer.close() failed: {type(e).__name__}: {e}")
        else:
            self.close_timed_out = True
            print(
                f"[capture] writer.close() timed out after {self.close_timeout_s}s — "
                f"buffered rows may be lost; exiting anyway"
            )
        # Bounded too: a flush-loop task stuck in flush()'s cancel handler (awaiting a
        # hung disk thread) never finishes cancelling — abandon it rather than hang.
        await self._cancel_bounded(self._tasks, "background")
        self._tasks = []
        if self._http is not None:
            self._http.close()
            self._http = None
        if self._mc is not None:
            try:
                await asyncio.wait_for(self._mc.close(), self.teardown_timeout_s)
            except BaseException:
                pass
            self._mc = None
        self._shutdown_result = ok
        return ok

    async def _cancel_bounded(self, tasks, what):
        """Cancel tasks and wait at most teardown_timeout_s for them to finish.
        Stragglers are abandoned (logged, self.teardown_abandoned set). True if all ended."""
        tasks = [t for t in tasks if t is not None and not t.done()]
        if not tasks:
            return True
        for t in tasks:
            t.cancel()
        done, pending = await asyncio.wait(tasks, timeout=self.teardown_timeout_s)
        for t in done:
            if not t.cancelled():
                t.exception()  # retrieve, so asyncio does not log "never retrieved"
        if pending:
            self.teardown_abandoned = True
            print(
                f"[capture] {len(pending)} {what} task(s) did not stop within {self.teardown_timeout_s}s — abandoning"
            )
            return False
        return True

    # -- status ----------------------------------------------------------------------

    def status(self):
        ws = self.writer.stats()
        now = self._now_ms()
        last_flush = ws.get("last_flush_ms") or self.start_ms
        stale_ms = 3 * self.writer.flush_interval_s * 1000
        return {
            "connected": self.connected,
            "subscribed_count": len(self.subscribed) if self.connected else 0,
            "reconnects": self.reconnects,
            "last_msg_age_s": (round((now - self.last_recv_ms) / 1000, 1) if self.last_recv_ms else None),
            "last_data_age_s": (round((now - self.last_data_ms) / 1000, 1) if self.last_data_ms else None),
            "gap_count": self.gap_count,
            "gap_open": self._gap_open,
            "universe_size": len(self.desired),
            "static_universe": self.static,
            "frames": self.frames,
            "heartbeats": self.heartbeats,
            "uptime_s": round((now - self.start_ms) / 1000, 1),
            "writer": ws,
            "flush_stale": bool(ws.get("rows_buffered", 0) > 0 and now - last_flush > stale_ms),
        }

    async def _start_http(self):
        """Tiny status server (parity with poly_ws): any GET -> status JSON.
        A bind failure is logged and capture continues without it."""
        if self.http_port is None:
            return None

        async def handler(reader, writer):
            try:
                await reader.readline()
                body = json.dumps(self.status(), default=repr).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Connection: close\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
                )
                await writer.drain()
            except Exception:
                pass
            finally:
                try:
                    writer.close()
                except Exception:
                    pass

        try:
            srv = await asyncio.start_server(handler, "127.0.0.1", self.http_port)
            print(f"[http] status server on 127.0.0.1:{srv.sockets[0].getsockname()[1]}")
            return srv
        except Exception as e:
            print(f"[http] status server not started: {e}")
            return None

    def summary(self):
        st = self.status()
        w = st["writer"]
        print("\n===== BOOK CAPTURE SUMMARY =====")
        print("run seconds:      ", st["uptime_s"])
        print("stop reason:      ", self._stop_reason)
        print("frames / pongs:   ", self.frames, "/", self.heartbeats)
        print("rows written:     ", w["rows_written"])
        print("quarantined:      ", w["quarantined"])
        print(
            "dropped:          ",
            {
                "overflow": w["rows_dropped_overflow"],
                "flush_error": w["rows_dropped_flush_error"],
                "paused_frames": w["frames_discarded_paused"],
            },
        )
        print("reconnects / gaps:", self.reconnects, "/", self.gap_count)
        print("universe size:    ", st["universe_size"], "(static)" if self.static else "(watchset)")
        print("root:             ", w["root"])
        if self.close_timed_out:
            print("WARNING: writer.close() timed out — final rows may be lost")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Polymarket market-channel raw capture to parquet")
    ap.add_argument("--seconds", type=int, default=0, help="0 = run forever (service mode)")
    ap.add_argument("--root", type=str, default=DEFAULT_ROOT)
    ap.add_argument(
        "--tokens", type=str, default=None, help="comma-separated token ids: static universe, skips memcached"
    )
    ap.add_argument("--ws-url", type=str, default=WS_URL)
    ap.add_argument(
        "--data-silence-s",
        type=float,
        default=DATA_SILENCE_S,
        help="reconnect after this many seconds without a data frame (heartbeats don't count); 0 disables",
    )
    a = ap.parse_args(argv)
    toks = [t.strip() for t in a.tokens.split(",") if t.strip()] if a.tokens else None
    try:
        writer = BookCaptureWriter(a.root)
    except WriterLockedError as e:
        print(f"[capture] refusing to start: {e} (is another book_capture running?)")
        return EXIT_WRITER_LOCKED
    cap = BookCapture(writer, tokens=toks, ws_url=a.ws_url, data_silence_s=a.data_silence_s)

    async def _run():
        await cap.run(a.seconds)
        if cap.close_timed_out or cap.teardown_abandoned:
            # A task/thread stuck on a hung disk would also block asyncio.run()'s
            # cleanup, executor shutdown and interpreter exit — leave now. Exit 1 when
            # close() timed out (buffered rows may be unflushed), so systemd and
            # monitoring see a failed stop; 0 if only teardown stragglers were abandoned.
            sys.stdout.flush()
            os._exit(EXIT_CLOSE_TIMEOUT if cap.close_timed_out else 0)

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    sys.exit(main())

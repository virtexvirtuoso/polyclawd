#!/usr/bin/env python3
"""
book_capture.py — raw Polymarket market-channel capture to zstd parquet (research only).

A standalone process holds its own `market` WebSocket and persists EVERY raw event
(full-depth books, price changes, trades) for offline research. Zero trading blast
radius: nothing here is read by the trading stack. The WS read loop never blocks on
disk — rows are buffered in memory and written from a worker thread.

Layers: the normalizer + buffered writer (API below), and BookCapture (the WS
lifecycle: watchset universe, reconnect/backoff, gap meta, SIGTERM, status HTTP,
CLI — see the "capture process" section further down).

Run:  python -m services.book_capture --seconds 60 --tokens a,b      (static universe)
      python -m services.book_capture --seconds 0                     (service: watchset)

Writer API:

    normalize_frame(raw: str, recv_ts: int) -> list[tuple[str, dict]]
        Pure. One WS frame -> (dataset, row) pairs. Never raises; anything unknown
        or malformed becomes a `quarantine` row. "PING"/"PONG"/blank -> [].

    w = BookCaptureWriter(root="storage/book_capture", **knobs)
        Takes an exclusive flock on <root>/.writer.lock (raises WriterLockedError if
        another writer holds it), then renames any crash-leftover `*.inprogress` to a
        unique `*.corrupt` name (never overwriting) and records meta kind="crash_leftover".
    w.add_frame(raw: str, recv_ts: int | None = None) -> int
        Sync, call from the event-loop thread. Normalizes + buffers; O(rows);
        never touches disk, never raises. Returns rows buffered (0 when paused/closed).
    w.add_meta(kind: str, detail: dict, recv_ts: int | None = None) -> None
        Buffer a meta row (connects, reconnects, subscriptions, ...). Never raises;
        after close() the row is dropped and logged.
    await w.flush()            write everything buffered now + rotate past hours.
                               If a dataset-hour write fails twice its rows are DROPPED
                               (not requeued) and recorded as meta kind="flush_error".
                               Rows pyarrow cannot convert go to quarantine individually,
                               as event_type=<dataset name> (e.g. "changes") and
                               raw_json=<the normalized row as JSON>, not the raw frame.
    await w.run_flush_loop()   background task: flush every flush_interval_s or when
                               rows_buffered >= flush_rows; exits after close().
    await w.close()            final flush, finalize every open file, release the
                               root lock. Idempotent.
    w.stats() -> dict          counters for a status endpoint.

Layout:  <root>/<dataset>/YYYY-MM-DD/HH.parquet   (hour = row's recv_ts, UTC)
    datasets: changes, book, trades, meta, quarantine (schemas: SCHEMAS)
    One open pq.ParquetWriter per (dataset, hour); each flush adds a row group.
    While open the file is `HH.parquet.inprogress`; on rotation/close it is
    atomically renamed to `HH.parquet` (or HH.1.parquet, HH.2.parquet, ... if the
    name is taken — never overwritten). So `<dataset>/*/*.parquet` globs only
    complete, readable files.

Memory: buffered rows are capped at BUFFER_CAP (150K). Measured ~814 B per buffered
change row (Python dict + strings) -> ~120 MB at the cap, up to ~250 MB during a flush
(the batch being converted plus a refilling buffer). On overflow the OLDEST rows are
dropped and summarized in meta "overflow_drop".

Clocks: every row stores recv_ts (local UTC ms) and, where the exchange sends one,
exch_ts (the event `timestamp`).
"""
import argparse
import asyncio
import fcntl
import json
import math
import os
import random
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

try:
    import websockets
except ImportError:  # pragma: no cover — normalizer/writer still usable without it
    websockets = None

try:
    import aiomcache
except ImportError:  # pragma: no cover — only needed for the memcached watchset
    aiomcache = None

DEFAULT_ROOT = "storage/book_capture"
GB = 1024 ** 3
HOUR_MS = 3_600_000

FLUSH_INTERVAL_S = 30.0      # flush at least this often
FLUSH_ROWS = 50_000          # ...or as soon as this many rows are buffered
BUFFER_CAP = 150_000         # hard cap on buffered data rows; overflow drops OLDEST.
                             # ~814 B/row measured -> ~120 MB (~250 MB during a flush);
                             # live rate is ~30 rows/s
INT64_MAX = 2 ** 63
DISK_FLOOR_BYTES = 2 * GB    # pause buffering below this much free space
DISK_RESUME_MARGIN = GB // 2 # resume only above floor + margin (hysteresis)
DISK_CHECK_INTERVAL_S = 5.0  # free-space syscall at most this often
LOOP_TICK_S = 1.0            # flush-loop wake-up granularity
ZSTD_LEVEL = 3

_HEARTBEATS = frozenset({"PING", "PONG"})

CHANGES_SCHEMA = pa.schema([
    ("recv_ts", pa.int64()), ("exch_ts", pa.int64()),
    ("asset_id", pa.string()), ("market", pa.string()),
    ("price", pa.float64()), ("size", pa.float64()), ("side", pa.string()),
    ("hash", pa.string()), ("best_bid", pa.float64()), ("best_ask", pa.float64()),
])
BOOK_SCHEMA = pa.schema([
    ("recv_ts", pa.int64()), ("exch_ts", pa.int64()),
    ("asset_id", pa.string()), ("market", pa.string()), ("hash", pa.string()),
    ("tick_size", pa.float64()),
    ("bids_json", pa.string()), ("asks_json", pa.string()),   # [[price, size], ...] full depth
])
TRADES_SCHEMA = pa.schema([
    ("recv_ts", pa.int64()), ("exch_ts", pa.int64()),
    ("asset_id", pa.string()), ("market", pa.string()),
    ("price", pa.float64()), ("size", pa.float64()), ("side", pa.string()),
    ("fee_rate_bps", pa.float64()), ("tx_hash", pa.string()),
])
META_SCHEMA = pa.schema([
    ("recv_ts", pa.int64()), ("kind", pa.string()), ("detail_json", pa.string()),
])
QUARANTINE_SCHEMA = pa.schema([
    ("recv_ts", pa.int64()), ("event_type", pa.string()), ("raw_json", pa.string()),
])
SCHEMAS = {
    "changes": CHANGES_SCHEMA,
    "book": BOOK_SCHEMA,
    "trades": TRADES_SCHEMA,
    "meta": META_SCHEMA,
    "quarantine": QUARANTINE_SCHEMA,
}


def _now_ms():
    return int(time.time() * 1000)


# ---------------------------------------------------------------- normalization

class _Malformed(ValueError):
    pass


def _coerce_ms(v, fallback_fn):
    """recv_ts from a caller -> int64 ms; anything unusable -> fallback_fn()."""
    try:
        if v is None or isinstance(v, bool):
            return int(fallback_fn())
        i = int(v)
        if 0 <= i < INT64_MAX:
            return i
    except Exception:
        pass
    return int(fallback_fn())


def _num(v, required=False):
    """Exchange numeric string -> finite float. ''/None -> None (or _Malformed if required)."""
    if v is None or v == "":
        if required:
            raise _Malformed("missing number")
        return None
    if isinstance(v, bool):
        raise _Malformed(f"bool not a number: {v!r}")
    f = float(v)  # ValueError/TypeError -> caller quarantines
    if not math.isfinite(f):
        raise _Malformed(f"non-finite: {v!r}")
    return f


def _ts(v):
    """Exchange `timestamp` (ms, string or int) -> int; missing -> None; garbage -> _Malformed."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        raise _Malformed(f"bad timestamp {v!r}")
    if isinstance(v, float):
        if not math.isfinite(v):
            raise _Malformed(f"bad timestamp {v!r}")
    i = int(v)
    if not 0 <= i < INT64_MAX:
        raise _Malformed(f"timestamp out of int64 range {v!r}")
    return i


def _str(v, required=False):
    if v is None or v == "":
        if required:
            raise _Malformed("missing string")
        return None
    if not isinstance(v, (str, int)) or isinstance(v, bool):
        raise _Malformed(f"not a string: {v!r}")
    return str(v)


def _side(v, required=False):
    s = _str(v, required=required)
    return s.upper() if s is not None else None


def _levels(v):
    if not isinstance(v, list):
        raise _Malformed("levels not a list")
    out = []
    for lv in v:
        if not isinstance(lv, dict):
            raise _Malformed("level not a dict")
        out.append([_num(lv.get("price"), True), _num(lv.get("size"), True)])
    return json.dumps(out, separators=(",", ":"))


def _first(ev, *keys):
    for k in keys:
        if k in ev and ev[k] not in (None, ""):
            return ev[k]
    return None


def _norm_price_change(ev, recv_ts):
    pcs = ev.get("price_changes")
    if not isinstance(pcs, list) or not pcs:
        raise _Malformed("price_changes missing/not a list")
    exch_ts = _ts(ev.get("timestamp"))
    market = _str(ev.get("market"))
    rows = []
    for pc in pcs:
        if not isinstance(pc, dict):
            raise _Malformed("price_change entry not a dict")
        rows.append(("changes", {
            "recv_ts": recv_ts, "exch_ts": exch_ts,
            "asset_id": _str(pc.get("asset_id"), True),
            "market": _str(pc.get("market")) or market,
            "price": _num(pc.get("price"), True),
            "size": _num(pc.get("size"), True),
            "side": _side(pc.get("side"), True),
            "hash": _str(pc.get("hash")),
            "best_bid": _num(pc.get("best_bid")),
            "best_ask": _num(pc.get("best_ask")),
        }))
    return rows


def _norm_book(ev, recv_ts):
    return [("book", {
        "recv_ts": recv_ts, "exch_ts": _ts(ev.get("timestamp")),
        "asset_id": _str(ev.get("asset_id"), True),
        "market": _str(ev.get("market")),
        "hash": _str(ev.get("hash")),
        "tick_size": _num(ev.get("tick_size")),
        "bids_json": _levels(ev.get("bids")),
        "asks_json": _levels(ev.get("asks")),
    })]


def _norm_trade(ev, recv_ts):
    return [("trades", {
        "recv_ts": recv_ts, "exch_ts": _ts(ev.get("timestamp")),
        "asset_id": _str(ev.get("asset_id"), True),
        "market": _str(ev.get("market")),
        "price": _num(ev.get("price"), True),
        "size": _num(ev.get("size"), True),
        "side": _side(ev.get("side")),
        "fee_rate_bps": _num(_first(ev, "fee_rate_bps", "feeRateBps")),
        "tx_hash": _str(_first(ev, "transaction_hash", "transactionHash")),
    })]


def _norm_tick_size(ev, recv_ts):
    return [("meta", {"recv_ts": recv_ts, "kind": "tick_size_change",
                      "detail_json": json.dumps(ev, separators=(",", ":"))})]


_HANDLERS = {
    "price_change": _norm_price_change,
    "book": _norm_book,
    "last_trade_price": _norm_trade,
    "tick_size_change": _norm_tick_size,
}


def _safe_dumps(obj):
    try:
        return json.dumps(obj, separators=(",", ":"), default=repr)
    except Exception:
        return repr(obj)


def _quarantine(recv_ts, event_type, raw_json):
    return ("quarantine", {"recv_ts": recv_ts,
                           "event_type": event_type if isinstance(event_type, str) else None,
                           "raw_json": raw_json if raw_json else "<empty>"})


def _norm_event(ev, recv_ts):
    if not isinstance(ev, dict):
        return [_quarantine(recv_ts, None, _safe_dumps(ev))]
    etype = ev.get("event_type")
    handler = _HANDLERS.get(etype) if isinstance(etype, str) else None
    if handler is None:
        return [_quarantine(recv_ts, etype, _safe_dumps(ev))]
    try:
        return handler(ev, recv_ts)
    except Exception:  # malformed field in a known type -> keep the raw event
        return [_quarantine(recv_ts, etype, _safe_dumps(ev))]


def normalize_frame(raw, recv_ts):
    """One raw WS frame -> list of (dataset, row dict). Pure; never raises.

    A frame is a JSON dict (one event) or list (e.g. the subscribe snapshot of
    `book` events). Heartbeats and blank frames yield []. Unknown event types,
    non-JSON, and known types with unparseable fields yield `quarantine` rows.
    """
    try:
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8")
            except Exception:
                return [_quarantine(recv_ts, None, repr(bytes(raw)))]
        if not isinstance(raw, str):
            return [_quarantine(recv_ts, None, repr(raw))]
        s = raw.strip()
        if not s or s in _HEARTBEATS:
            return []
        try:
            obj = json.loads(s)
        except Exception:
            return [_quarantine(recv_ts, None, raw)]
        if isinstance(obj, list):
            out = []
            for ev in obj:
                out.extend(_norm_event(ev, recv_ts))
            return out
        return _norm_event(obj, recv_ts)
    except Exception as e:  # pragma: no cover — belt and braces
        return [_quarantine(recv_ts, None, f"<normalize error {type(e).__name__}> {raw!r}"[:100_000])]


# ---------------------------------------------------------------- writer

class WriterLockedError(RuntimeError):
    """Another BookCaptureWriter already owns this root (its .writer.lock is held)."""


def _corrupt_dest(tmp_path):
    """Unique `*.corrupt` name for a `*.inprogress` file — never an existing path.
    HH[.N].parquet.inprogress -> HH[.N].parquet.corrupt, else HH[.N].1.parquet.corrupt, ..."""
    tmp_path = Path(tmp_path)
    name = tmp_path.name[: -len(".inprogress")] if tmp_path.name.endswith(".inprogress") else tmp_path.name
    stem = name[: -len(".parquet")] if name.endswith(".parquet") else name
    n = 0
    while True:
        cand = tmp_path.with_name(f"{stem}.parquet.corrupt" if n == 0 else f"{stem}.{n}.parquet.corrupt")
        if not cand.exists():
            return cand
        n += 1


class _OpenFile:
    __slots__ = ("writer", "tmp_path", "final_path", "rows")

    def __init__(self, writer, tmp_path, final_path):
        self.writer = writer
        self.tmp_path = tmp_path
        self.final_path = final_path
        self.rows = 0


def _hour_dir_and_stem(root, dataset, bucket):
    st = time.gmtime(bucket * 3600)
    return root / dataset / time.strftime("%Y-%m-%d", st), time.strftime("%H", st)


class BookCaptureWriter:
    """Buffered, hour-partitioned parquet writer. See module docstring for the API."""

    def __init__(self, root=DEFAULT_ROOT, *,
                 flush_interval_s=FLUSH_INTERVAL_S,
                 flush_rows=FLUSH_ROWS,
                 buffer_cap=BUFFER_CAP,
                 disk_floor_bytes=DISK_FLOOR_BYTES,
                 disk_resume_margin_bytes=DISK_RESUME_MARGIN,
                 disk_check_interval_s=DISK_CHECK_INTERVAL_S,
                 free_bytes_fn=None,
                 now_ms_fn=None,
                 loop_tick_s=LOOP_TICK_S,
                 compression_level=ZSTD_LEVEL):
        self.root = Path(root)
        self.flush_interval_s = flush_interval_s
        self.flush_rows = flush_rows
        self.buffer_cap = buffer_cap
        self.disk_floor_bytes = disk_floor_bytes
        self.disk_resume_bytes = disk_floor_bytes + disk_resume_margin_bytes
        self.disk_check_interval_s = disk_check_interval_s
        self.loop_tick_s = loop_tick_s
        self.compression_level = compression_level
        self._now_ms = now_ms_fn or _now_ms
        self._free_bytes = free_bytes_fn or self._default_free_bytes

        self._buf = deque()           # (dataset, row) data rows, capped
        self._meta = []               # meta rows, never dropped by the cap
        self._open = {}               # (dataset, hour bucket) -> _OpenFile (worker thread only)
        self._lock = None             # asyncio.Lock, created lazily inside the loop
        self._io_lock = threading.Lock()  # one _flush_sync at a time, even across cancellation
        self._wake = None             # asyncio.Event
        self._closed = False
        self._paused = False
        self._last_disk_check = float("-inf")
        self._last_flush_mono = time.monotonic()
        self._overflow_since_flush = 0

        self.counters = {
            "frames_seen": 0, "frames_discarded_paused": 0, "frames_after_close": 0,
            "rows_written": {ds: 0 for ds in SCHEMAS},
            "rows_dropped_overflow": 0, "rows_dropped_flush_error": 0,
            "flushes": 0, "flush_retries": 0, "flush_errors": 0,
            "files_finalized": 0, "files_corrupt": 0, "quarantined": 0,
            "last_flush_ms": None, "last_flush_secs": None,
        }

        self.root.mkdir(parents=True, exist_ok=True)
        self._lock_fh = self._acquire_root_lock()   # BEFORE recovery: never touch a live writer's files
        self._recover_leftovers()

    # -- public, event-loop side -------------------------------------------------

    def add_frame(self, raw, recv_ts=None):
        """Normalize + buffer one WS frame. Never touches disk, never raises."""
        try:
            if self._closed:
                self.counters["frames_after_close"] += 1
                return 0
            recv_ts = _coerce_ms(recv_ts, self._now_ms)
            self.counters["frames_seen"] += 1
            self._maybe_check_disk(recv_ts)
            if self._paused:
                self.counters["frames_discarded_paused"] += 1
                return 0
            rows = normalize_frame(raw, recv_ts)
            buf = self._buf
            for item in rows:
                if item[0] == "meta":
                    self._meta.append(item[1])
                    continue
                if item[0] == "quarantine":
                    self.counters["quarantined"] += 1
                buf.append(item)
            over = len(buf) - self.buffer_cap
            if over > 0:
                for _ in range(over):
                    buf.popleft()
                self._overflow_since_flush += over
                self.counters["rows_dropped_overflow"] += over
            if len(buf) >= self.flush_rows and self._wake is not None:
                self._wake.set()
            return len(rows)
        except Exception as e:  # pragma: no cover — must never take down the WS loop
            print(f"[book_capture] add_frame error {type(e).__name__}: {e}")
            return 0

    def add_meta(self, kind, detail=None, recv_ts=None):
        """Buffer a meta row (kind + JSON detail). Never raises; dropped after close()."""
        try:
            if self._closed:
                print(f"[meta] dropped after close: {kind}")
                return
            self._meta.append({"recv_ts": _coerce_ms(recv_ts, self._now_ms),
                               "kind": str(kind), "detail_json": _safe_dumps(detail or {})})
        except Exception as e:  # pragma: no cover
            print(f"[book_capture] add_meta error {type(e).__name__}: {e}")

    def stats(self):
        s = dict(self.counters)
        s["rows_written"] = dict(self.counters["rows_written"])
        s["rows_buffered"] = len(self._buf)
        s["meta_buffered"] = len(self._meta)
        s["paused"] = self._paused
        s["closed"] = self._closed
        s["open_files"] = len(self._open)
        s["root"] = str(self.root)
        return s

    async def flush(self, _final=False):
        """Write all buffered rows (one row group per dataset-hour) and rotate past
        hours. Disk I/O runs in a worker thread; flushes are serialized."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            batch, self._buf = self._buf, deque()
            meta, self._meta = self._meta, []
            if self._overflow_since_flush:
                meta.append(self._meta_row("overflow_drop",
                                           {"dropped_rows": self._overflow_since_flush,
                                            "buffer_cap": self.buffer_cap}))
                self._overflow_since_flush = 0
            t0 = time.monotonic()
            # to_thread cannot be cancelled: if our caller is cancelled, keep holding the
            # asyncio lock until the thread really finishes, then re-raise.
            fut = asyncio.ensure_future(asyncio.to_thread(self._flush_sync, batch, meta, _final))
            try:
                new_meta = await asyncio.shield(fut)
            except asyncio.CancelledError:
                try:
                    self._meta.extend(await fut)
                except Exception as e:
                    print(f"[book_capture] flush crashed during cancel {type(e).__name__}: {e}")
                raise
            except Exception as e:  # _flush_sync handles its own errors; this is the backstop
                dropped = len(batch) + len(meta)
                print(f"[book_capture] flush crashed {type(e).__name__}: {e} — dropped {dropped} rows")
                self.counters["flush_errors"] += 1
                self.counters["rows_dropped_flush_error"] += dropped
                new_meta = [self._meta_row("flush_error", {"error": f"{type(e).__name__}: {e}",
                                                           "dropped_rows": dropped})]
            self._meta.extend(new_meta)
            self._last_flush_mono = time.monotonic()
            self.counters["flushes"] += 1
            self.counters["last_flush_ms"] = self._now_ms()
            self.counters["last_flush_secs"] = round(self._last_flush_mono - t0, 3)

    async def run_flush_loop(self):
        """Background task: flush on interval or row threshold until close()."""
        if self._wake is None:
            self._wake = asyncio.Event()
        while not self._closed:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.loop_tick_s)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if self._closed:
                break
            due = time.monotonic() - self._last_flush_mono >= self.flush_interval_s
            if due or len(self._buf) >= self.flush_rows:
                try:
                    await self.flush()
                except Exception as e:
                    print(f"[book_capture] flush loop error {type(e).__name__}: {e}")

    async def close(self):
        """Flush everything and finalize all open files. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._wake is not None:
            self._wake.set()
        try:
            await self.flush(_final=True)
            if self._meta:  # meta produced by the final flush itself (errors, drops)
                await self.flush(_final=True)
        finally:
            self._release_root_lock()
        print(f"[book_capture] closed: written={self.counters['rows_written']} "
              f"dropped_overflow={self.counters['rows_dropped_overflow']} "
              f"flush_errors={self.counters['flush_errors']}")

    # -- disk floor ----------------------------------------------------------------

    def _default_free_bytes(self):
        return shutil.disk_usage(self.root).free

    def _maybe_check_disk(self, recv_ts):
        now = time.monotonic()
        if now - self._last_disk_check < self.disk_check_interval_s:
            return
        self._last_disk_check = now
        try:
            free = self._free_bytes()
        except Exception as e:
            print(f"[book_capture] free-space check failed {type(e).__name__}: {e}")
            return
        if not self._paused and free < self.disk_floor_bytes:
            self._paused = True
            print(f"[book_capture] DISK FLOOR: {free / GB:.2f} GB free < "
                  f"{self.disk_floor_bytes / GB:.2f} GB — pausing capture")
            self._meta.append(self._meta_row("disk_floor_pause",
                                             {"free_bytes": free, "floor_bytes": self.disk_floor_bytes},
                                             recv_ts))
        elif self._paused and free > self.disk_resume_bytes:
            self._paused = False
            print(f"[book_capture] disk recovered: {free / GB:.2f} GB free — resuming capture")
            self._meta.append(self._meta_row("disk_floor_resume",
                                             {"free_bytes": free,
                                              "frames_discarded_total": self.counters["frames_discarded_paused"]},
                                             recv_ts))

    # -- worker-thread side --------------------------------------------------------

    def _meta_row(self, kind, detail, recv_ts=None):
        return {"recv_ts": int(recv_ts if recv_ts is not None else self._now_ms()),
                "kind": kind, "detail_json": _safe_dumps(detail)}

    def _acquire_root_lock(self):
        path = self.root / ".writer.lock"
        fh = open(path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            fh.close()
            raise WriterLockedError(f"another book_capture writer holds {path}") from e
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()}\n")
            fh.flush()
        except Exception:
            pass
        return fh

    def _release_root_lock(self):
        fh, self._lock_fh = self._lock_fh, None
        if fh is None:
            return
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            fh.close()
        except Exception:
            pass

    def _recover_leftovers(self):
        """Startup: rename crash-leftover *.inprogress -> *.corrupt (never delete)."""
        for p in sorted(self.root.glob("*/*/*.inprogress")):
            try:
                dst = _corrupt_dest(p)
                os.replace(p, dst)
                print(f"[book_capture] crash leftover {p} -> {dst.name}")
                self.counters["files_corrupt"] += 1
                self._meta.append(self._meta_row("crash_leftover",
                                                 {"path": str(p), "renamed_to": str(dst)}))
            except Exception as e:
                print(f"[book_capture] could not rename leftover {p}: {type(e).__name__}: {e}")

    def _pick_name(self, dataset, bucket):
        d, hh = _hour_dir_and_stem(self.root, dataset, bucket)
        d.mkdir(parents=True, exist_ok=True)
        n = 0
        while True:
            stem = hh if n == 0 else f"{hh}.{n}"
            final = d / f"{stem}.parquet"
            tmp = d / f"{stem}.parquet.inprogress"
            corrupt = d / f"{stem}.parquet.corrupt"
            if not (final.exists() or tmp.exists() or corrupt.exists()):
                return tmp, final
            n += 1

    def _get_file(self, dataset, bucket):
        key = (dataset, bucket)
        f = self._open.get(key)
        if f is None:
            tmp, final = self._pick_name(dataset, bucket)
            w = pq.ParquetWriter(str(tmp), SCHEMAS[dataset], compression="zstd",
                                 compression_level=self.compression_level)
            f = self._open[key] = _OpenFile(w, tmp, final)
        return f

    def _finalize(self, key):
        """Close + atomically rename one open file. Returns meta rows on trouble."""
        f = self._open.pop(key, None)
        if f is None:
            return []
        try:
            f.writer.close()
            if f.rows == 0:
                os.remove(f.tmp_path)   # never wrote a row group (failed first write)
                return []
            pq.read_metadata(str(f.tmp_path))   # footer must be readable before publishing
            os.replace(f.tmp_path, f.final_path)
            self.counters["files_finalized"] += 1
            return []
        except Exception as e:
            try:
                os.replace(f.tmp_path, _corrupt_dest(f.tmp_path))
            except Exception:
                pass
            self.counters["files_corrupt"] += 1
            print(f"[book_capture] finalize failed {f.tmp_path}: {type(e).__name__}: {e}")
            return [self._meta_row("finalize_error", {"path": str(f.tmp_path), "rows": f.rows,
                                                      "error": f"{type(e).__name__}: {e}"})]

    def _to_table(self, dataset, rows):
        """rows -> (table, bad_rows). A conversion error (not I/O) falls back to
        per-row conversion so one bad value cannot sink the whole batch."""
        schema = SCHEMAS[dataset]
        try:
            return pa.Table.from_pylist(rows, schema=schema), []
        except Exception:
            pass
        good, bad = [], []
        for r in rows:
            try:
                pa.Table.from_pylist([r], schema=schema)
                good.append(r)
            except Exception:
                bad.append(r)
        return pa.Table.from_pylist(good, schema=schema), bad

    def _write_table(self, dataset, bucket, table):
        f = self._get_file(dataset, bucket)
        f.writer.write_table(table)
        f.rows += table.num_rows

    def _flush_sync(self, batch, meta, final):
        with self._io_lock:
            return self._flush_sync_unlocked(batch, meta, final)

    def _flush_sync_unlocked(self, batch, meta, final):
        groups = {}
        for ds, row in batch:
            groups.setdefault((ds, row["recv_ts"] // HOUR_MS), []).append(row)
        for row in meta:
            groups.setdefault(("meta", row["recv_ts"] // HOUR_MS), []).append(row)

        # Convert first (pure CPU); unconvertible rows become quarantine rows.
        tables, quarantined, new_meta = {}, [], []
        for (ds, bucket), rows in groups.items():
            table, bad = self._to_table(ds, rows)
            tables[(ds, bucket)] = table
            for r in bad:
                quarantined.append({"recv_ts": _coerce_ms(r.get("recv_ts"), self._now_ms),
                                    "event_type": ds, "raw_json": _safe_dumps(r)})
        if quarantined:
            print(f"[book_capture] {len(quarantined)} unconvertible rows -> quarantine")
            self.counters["quarantined"] += len(quarantined)
            qgroups = {}
            for r in quarantined:
                qgroups.setdefault(r["recv_ts"] // HOUR_MS, []).append(r)
            for bucket, rows in qgroups.items():
                table, bad = self._to_table("quarantine", rows)
                key = ("quarantine", bucket)
                if key in tables:
                    table = pa.concat_tables([tables[key], table])
                tables[key] = table
                if bad:  # cannot happen with coerced recv_ts; count rather than loop
                    self.counters["rows_dropped_flush_error"] += len(bad)

        for (ds, bucket), table in tables.items():
            if table.num_rows == 0:
                continue
            n = table.num_rows
            err = None
            for attempt in (1, 2):
                try:
                    self._write_table(ds, bucket, table)
                    err = None
                    break
                except Exception as e:
                    err = e
                    # writer state is suspect: finalize what it already holds, retry on a fresh file
                    new_meta.extend(self._finalize((ds, bucket)))
                    if attempt == 1:
                        self.counters["flush_retries"] += 1
            if err is None:
                self.counters["rows_written"][ds] += n
            else:
                self.counters["flush_errors"] += 1
                self.counters["rows_dropped_flush_error"] += n
                msg = f"{type(err).__name__}: {err}"
                print(f"[book_capture] flush FAILED twice {ds} hour={bucket}: {msg} "
                      f"— dropped {n} rows")
                new_meta.append(self._meta_row("flush_error", {
                    "dataset": ds, "hour_bucket": bucket, "dropped_rows": n, "error": msg}))

        current = self._now_ms() // HOUR_MS
        for key in list(self._open):
            if final or key[1] < current:
                new_meta.extend(self._finalize(key))
        return new_meta


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
#   capture_stop      {reason, stats}           (written BEFORE writer.close())
#   universe_change   {added_count, removed_count, added, removed, universe_size}
#                     — at the subscribe that applies it (diff vs previous subscription)
#   watchset_missing  {kept_universe_size, startup, error} — once per outage
#   watchset_restored {outage_s, universe_size}
#   gap_start         {last_recv_ts, reason, reconnects} — an established connection ended
#   gap_end           {gap_ms, tokens}          — next successful subscribe; gap_ms is
#                     subscribe time minus last_recv_ts (last frame of ANY kind, incl.
#                     PONG), i.e. an upper bound on the uncovered interval. The server
#                     sends a fresh full `book` per token on subscribe (free re-anchor).

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
MC_HOST, MC_PORT = "localhost", 11211
WATCHSET_KEY = b"poly:ws:watchset"
CAPTURE_HTTP_PORT = int(os.environ.get("POLY_CAPTURE_PORT", "8424"))

UNIVERSE_INTERVAL_S = 60.0       # watchset re-read cadence
UNIVERSE_RECONNECT_MIN_S = 120.0 # min gap between universe-driven reconnects
NO_UNIVERSE_RETRY_S = 10.0       # startup: retry cadence while there is no universe
MC_TIMEOUT_S = 5.0               # memcached get must not hang the universe loop
PING_INTERVAL_S = 10.0           # app-level "PING" text heartbeat (server answers "PONG")
BACKOFF_BASE_S = 1.0             # error reconnect: 1 s doubling to 60 s + jitter (poly_ws)
BACKOFF_MAX_S = 60.0
BACKOFF_JITTER_S = 0.5
STABLE_RESET_S = 30.0            # a connection that lived longer resets the backoff
CLOSE_TIMEOUT_S = 20.0           # bound on writer.close() at shutdown (hung disk)
CONTROL_TICK_S = 1.0             # connection control-loop wake-up granularity
EXIT_WRITER_LOCKED = 3


def _git_sha():
    """Short git sha of this checkout, or None (cheap: one subprocess at startup)."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=os.path.dirname(os.path.abspath(__file__)),
                             capture_output=True, text=True, timeout=2)
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


class BookCapture:
    """WS lifecycle around a BookCaptureWriter. See the section comment above."""

    def __init__(self, writer, *, tokens=None, universe_fn=None, ws_url=WS_URL,
                 http_port=CAPTURE_HTTP_PORT,
                 universe_interval_s=UNIVERSE_INTERVAL_S,
                 universe_reconnect_min_s=UNIVERSE_RECONNECT_MIN_S,
                 no_universe_retry_s=NO_UNIVERSE_RETRY_S,
                 ping_interval_s=PING_INTERVAL_S,
                 backoff_base_s=BACKOFF_BASE_S, backoff_max_s=BACKOFF_MAX_S,
                 jitter_s=BACKOFF_JITTER_S, stable_reset_s=STABLE_RESET_S,
                 close_timeout_s=CLOSE_TIMEOUT_S, tick_s=CONTROL_TICK_S,
                 now_ms_fn=None):
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
        self.close_timeout_s = close_timeout_s
        self.tick_s = tick_s
        self._now_ms = now_ms_fn or _now_ms

        self.desired = set(self.tokens)   # universe we want subscribed
        self.subscribed = set()           # tokens sent on the current/last connection
        self._prev_subscribed = None      # set of the previous successful subscribe
        self.connected = False
        self.reconnects = 0
        self.gap_count = 0
        self.frames = 0                   # non-heartbeat frames handed to the writer
        self.heartbeats = 0
        self.last_recv_ms = None          # last frame of ANY kind (liveness, gap bound)
        self.start_ms = self._now_ms()
        self._gap_open = False
        self._backoff = backoff_base_s
        self._last_universe_reconnect = float("-inf")
        self._watchset_missing_since = None
        self._mc = None
        self._stop = asyncio.Event()
        self._stop_reason = None
        self._shutdown_result = None      # None until shutdown() ran; then True/False
        self._read_task = None
        self._tasks = []
        self._http = None
        self.close_timed_out = False

    # -- universe --------------------------------------------------------------------

    async def _read_watchset_mc(self):
        if aiomcache is None:
            raise RuntimeError("aiomcache not installed (use --tokens for a static universe)")
        if self._mc is None:
            self._mc = aiomcache.Client(MC_HOST, MC_PORT, pool_size=1)
        return _parse_watchset(await asyncio.wait_for(self._mc.get(WATCHSET_KEY), MC_TIMEOUT_S))

    async def _refresh_universe(self, startup=False):
        """Re-read the universe. Missing/unreadable/empty -> keep the last one and
        record watchset_missing once per outage. Never raises."""
        if self.static:
            return
        err = None
        try:
            toks = await self._universe_fn()
        except Exception as e:
            toks, err = None, f"{type(e).__name__}: {e}"
        if not toks:
            if self._watchset_missing_since is None:
                self._watchset_missing_since = time.monotonic()
                print(f"[universe] watchset missing/unreadable ({err or 'no key'}) — "
                      f"keeping last universe ({len(self.desired)} tokens)")
                self.writer.add_meta("watchset_missing", {
                    "kept_universe_size": len(self.desired), "startup": startup, "error": err})
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

    def _error_wait(self, conn_lasted_s):
        """Backoff after an error disconnect (poly_ws policy): 1 s doubling to 60 s +
        jitter, reset when the connection that just ended was stable > 30 s."""
        if conn_lasted_s is not None and conn_lasted_s > self.stable_reset_s:
            self._backoff = self.backoff_base_s
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
            self.frames += 1
        raise _ServerClosed("server closed the connection")

    def _on_subscribed(self, sub):
        now = self._now_ms()
        prev = self._prev_subscribed
        if prev is not None and prev != set(sub):
            added, removed = sorted(set(sub) - prev), sorted(prev - set(sub))
            print(f"[universe] resubscribed: +{len(added)} -{len(removed)} (now {len(sub)})")
            self.writer.add_meta("universe_change", {
                "added_count": len(added), "removed_count": len(removed),
                "added": added, "removed": removed, "universe_size": len(sub)}, recv_ts=now)
        if self._gap_open:
            self._gap_open = False
            base = self._gap_last_recv if self._gap_last_recv is not None else self._gap_start_ms
            self.writer.add_meta("gap_end", {"gap_ms": max(0, now - base), "tokens": len(sub)},
                                 recv_ts=now)
        self._prev_subscribed = set(sub)

    def _on_disconnect(self, reason):
        """An established connection ended (error or planned) -> gap_start."""
        self.gap_count += 1
        self._gap_open = True
        self._gap_start_ms = self._now_ms()
        self._gap_last_recv = self.last_recv_ms
        self.writer.add_meta("gap_start", {"last_recv_ts": self.last_recv_ms, "reason": reason,
                                           "reconnects": self.reconnects},
                             recv_ts=self._gap_start_ms)

    async def run_once(self, deadline):
        """One connection. Returns "stop" | "deadline" | "universe_change"; raises on
        any connection error (incl. a clean server close). Sets self._established."""
        self._established = False
        async with websockets.connect(self.ws_url, ping_interval=10, ping_timeout=25,
                                      max_size=2 ** 23, open_timeout=15) as ws:
            sub = sorted(self.desired)
            await ws.send(json.dumps({"assets_ids": sub, "type": "market"}))
            self.subscribed = set(sub)
            self.connected = True
            self._established = True
            self._conn_start = time.monotonic()
            self._on_subscribed(sub)
            print(f"[sub] {len(sub)} tokens")
            ping_task = asyncio.create_task(self._app_ping(ws))
            self._read_task = read_task = asyncio.create_task(self._read_loop(ws))
            stop_task = asyncio.create_task(self._stop.wait())
            try:
                while True:
                    done, _ = await asyncio.wait({read_task, stop_task}, timeout=self.tick_s,
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if read_task in done:
                        read_task.result()            # raises the connection error
                        raise _ServerClosed("read loop ended")
                    if self._stop.is_set():
                        return "stop"
                    if time.time() >= deadline:
                        return "deadline"
                    if self._universe_reconnect_due():
                        self._last_universe_reconnect = time.monotonic()
                        print(f"[universe] resubscribe via reconnect "
                              f"(desired={len(self.desired)} subscribed={len(self.subscribed)})")
                        return "universe_change"
            finally:
                self.connected = False
                for t in (ping_task, read_task, stop_task):
                    t.cancel()
                await asyncio.gather(ping_task, read_task, stop_task, return_exceptions=True)
                self._read_task = None

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
            await self._refresh_universe(startup=True)
            self.writer.add_meta("capture_start", {
                "pid": os.getpid(), "root": str(self.writer.root),
                "universe_size": len(self.desired), "static_universe": self.static,
                "ws_url": self.ws_url, "git_sha": _git_sha()})
            await self._wait_for_universe(deadline)
            if not self.static:
                self._tasks.append(asyncio.create_task(self._universe_loop()))
            while not self._stop.is_set() and time.time() < deadline:
                try:
                    outcome = await self.run_once(deadline)
                    if outcome == "universe_change":
                        self.reconnects += 1
                        self._on_disconnect("universe_change")
                        continue                  # planned: reconnect now, no backoff
                    break                         # stop / deadline
                except Exception as e:
                    self.connected = False
                    if self._stop.is_set():
                        break
                    self.reconnects += 1
                    lasted = None
                    if self._established:
                        lasted = time.monotonic() - self._conn_start
                        self._on_disconnect(f"{type(e).__name__}: {e}"[:500])
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    wait = self._error_wait(lasted)
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
        rt = self._read_task
        if rt is not None and not rt.done():
            rt.cancel()
            await asyncio.gather(rt, return_exceptions=True)
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
            print(f"[capture] writer.close() timed out after {self.close_timeout_s}s — "
                  f"buffered rows may be lost; exiting anyway")
        for t in self._tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self._http is not None:
            self._http.close()
            self._http = None
        if self._mc is not None:
            try:
                await self._mc.close()
            except Exception:
                pass
            self._mc = None
        self._shutdown_result = ok
        return ok

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
            "last_msg_age_s": (round((now - self.last_recv_ms) / 1000, 1)
                               if self.last_recv_ms else None),
            "gap_count": self.gap_count,
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
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                             b"Connection: close\r\nContent-Length: " +
                             str(len(body)).encode() + b"\r\n\r\n" + body)
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
        print("dropped:          ", {"overflow": w["rows_dropped_overflow"],
                                     "flush_error": w["rows_dropped_flush_error"],
                                     "paused_frames": w["frames_discarded_paused"]})
        print("reconnects / gaps:", self.reconnects, "/", self.gap_count)
        print("universe size:    ", st["universe_size"], "(static)" if self.static else "(watchset)")
        print("root:             ", w["root"])
        if self.close_timed_out:
            print("WARNING: writer.close() timed out — final rows may be lost")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Polymarket market-channel raw capture to parquet")
    ap.add_argument("--seconds", type=int, default=0, help="0 = run forever (service mode)")
    ap.add_argument("--root", type=str, default=DEFAULT_ROOT)
    ap.add_argument("--tokens", type=str, default=None,
                    help="comma-separated token ids: static universe, skips memcached")
    ap.add_argument("--ws-url", type=str, default=WS_URL)
    a = ap.parse_args(argv)
    toks = [t.strip() for t in a.tokens.split(",") if t.strip()] if a.tokens else None
    try:
        writer = BookCaptureWriter(a.root)
    except WriterLockedError as e:
        print(f"[capture] refusing to start: {e} (is another book_capture running?)")
        return EXIT_WRITER_LOCKED
    cap = BookCapture(writer, tokens=toks, ws_url=a.ws_url)

    async def _run():
        await cap.run(a.seconds)
        if cap.close_timed_out:
            # a close() stuck in a disk thread would also block asyncio.run()'s executor
            # shutdown and interpreter exit — leave now, exit 0 as a normal stop.
            sys.stdout.flush()
            os._exit(0)

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    sys.exit(main())

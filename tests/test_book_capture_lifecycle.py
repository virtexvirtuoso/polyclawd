"""Tests for the BookCapture WS lifecycle (Task 3) in services/book_capture.py.

Offline only: a fixture-driven fake market-channel server (websockets.serve on
127.0.0.1:0) and an injected universe source — no network, no memcached. All
parquet output goes to tmp_path.
"""
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import websockets

from services import book_capture as bc

REPO = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "book_capture_events.jsonl"
UNKNOWN_FRAME = json.dumps({"event_type": "brand_new_thing", "asset_id": "X", "foo": 1})


def _fixture_raws():
    with open(FIXTURE) as fh:
        return [json.loads(line)["raw"] for line in fh if line.strip()]


def _read(root, ds):
    files = sorted((Path(root) / ds).glob("*/*.parquet"))
    if not files:
        return []
    return pa.concat_tables([pq.read_table(f) for f in files]).to_pylist()


def _meta(root, kind=None):
    rows = _read(root, "meta")
    for r in rows:
        r["detail"] = json.loads(r["detail_json"])
    return [r for r in rows if kind is None or r["kind"] == kind]


def _inprogress(root):
    return sorted(Path(root).glob("*/*/*.inprogress"))


async def _until(pred, timeout=5.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(step)
    raise AssertionError("condition not met within %.1fs" % timeout)


class FakeMarketServer:
    """Replays fixture frames to each connection and records subscribe messages.

    `plan` is a list of per-connection frame lists; after its frames a connection
    either drops (transport.abort -> client sees an abnormal close) or stays open
    answering PING with PONG. Connections beyond the plan stay open, no frames.
    """

    def __init__(self, plan=None, drop=()):
        self.plan = plan or []
        self.drop = set(drop)          # connection indexes (0-based) to abort after frames
        self.subs = []                 # (monotonic_ts, assets_ids list)
        self.pings = 0
        self.conns = 0
        self.srv = None
        self.url = None

    async def handler(self, ws):
        idx = self.conns
        self.conns += 1
        msg = json.loads(await ws.recv())
        self.subs.append((time.monotonic(), msg.get("assets_ids"), msg.get("type")))
        for raw in (self.plan[idx] if idx < len(self.plan) else []):
            await ws.send(raw)
        if idx in self.drop:
            await asyncio.sleep(0.05)
            ws.transport.abort()
            return
        try:
            async for m in ws:
                if m == "PING":
                    self.pings += 1
                    await ws.send("PONG")
        except websockets.ConnectionClosed:
            pass

    async def __aenter__(self):
        self.srv = await websockets.serve(self.handler, "127.0.0.1", 0)
        port = self.srv.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc):
        self.srv.close()
        await self.srv.wait_closed()


class Universe:
    """Injectable watchset source: .value is returned (None = key missing)."""

    def __init__(self, value):
        self.value = value
        self.reads = 0

    async def __call__(self):
        self.reads += 1
        if isinstance(self.value, Exception):
            raise self.value
        return None if self.value is None else list(self.value)


def _writer(root):
    return bc.BookCaptureWriter(root, flush_interval_s=0.2, loop_tick_s=0.05)


def _capture(writer, **kw):
    base = dict(ws_url="ws://127.0.0.1:9", http_port=None, tick_s=0.02,
                ping_interval_s=0.1, backoff_base_s=0.05, jitter_s=0.0,
                universe_interval_s=0.05, universe_reconnect_min_s=0.0,
                no_universe_retry_s=0.05, close_timeout_s=5.0)
    base.update(kw)
    return bc.BookCapture(writer, **base)


# ---------------------------------------------------------------- integration

async def test_capture_reconnect_gap_and_parquet(tmp_path):
    raws = _fixture_raws()
    first, second = raws[:150] + [UNKNOWN_FRAME], raws[150:]
    toks = ["111", "222"]
    async with FakeMarketServer(plan=[first, second], drop={0}) as srv:
        w = _writer(tmp_path)
        cap = _capture(w, tokens=toks, ws_url=srv.url)
        task = asyncio.create_task(cap.run(0))
        await _until(lambda: len(srv.subs) >= 2 and cap.connected
                     and cap.frames >= len(raws) + 1 and srv.pings >= 1)
        cap.request_stop("test")
        await asyncio.wait_for(task, 10)

    assert [s[1] for s in srv.subs] == [toks, toks]            # resubscribed same set
    assert all(s[2] == "market" for s in srv.subs)
    assert cap.reconnects == 1 and cap.gap_count == 1

    expect = {"book": 0, "changes": 0}
    for raw in raws:
        for ds, _ in bc.normalize_frame(raw, 0):
            expect[ds] += 1
    assert len(_read(tmp_path, "book")) == expect["book"]
    assert len(_read(tmp_path, "changes")) == expect["changes"]
    q = _read(tmp_path, "quarantine")
    assert [r["event_type"] for r in q] == ["brand_new_thing"]  # unknown type end-to-end; PONGs not stored

    kinds = [m["kind"] for m in _meta(tmp_path)]
    assert kinds[0] == "capture_start" and kinds[-1] == "capture_stop"
    gs, = _meta(tmp_path, "gap_start")
    ge, = _meta(tmp_path, "gap_end")
    assert gs["detail"]["reconnects"] == 1 and gs["detail"]["last_recv_ts"] > 0
    assert gs["detail"]["reason"]
    assert gs["detail"]["last_data_ts"] is not None
    assert gs["detail"]["last_data_ts"] <= gs["detail"]["last_recv_ts"]
    assert ge["detail"]["tokens"] == 2 and ge["detail"]["gap_ms"] >= 0
    assert gs["recv_ts"] <= ge["recv_ts"]
    start, = _meta(tmp_path, "capture_start")
    assert start["detail"]["pid"] == os.getpid() and start["detail"]["universe_size"] == 2
    stop, = _meta(tmp_path, "capture_stop")
    assert stop["detail"]["reason"] == "test" and "writer" in stop["detail"]["stats"]
    assert _inprogress(tmp_path) == []


async def test_universe_change_reconnects_without_backoff(tmp_path):
    uni = Universe(["a", "b"])
    async with FakeMarketServer() as srv:
        w = _writer(tmp_path)
        # a 5 s error backoff would blow the 2 s wait below: planned reconnects skip it
        cap = _capture(w, universe_fn=uni, ws_url=srv.url, backoff_base_s=5.0)
        task = asyncio.create_task(cap.run(0))
        await _until(lambda: len(srv.subs) == 1)
        uni.value = ["b", "c", "d"]
        await _until(lambda: len(srv.subs) == 2 and cap.connected, timeout=2.0)
        cap.request_stop("test")
        await asyncio.wait_for(task, 10)

    assert sorted(srv.subs[1][1]) == ["b", "c", "d"]
    uc, = _meta(tmp_path, "universe_change")
    d = uc["detail"]
    assert d["added_count"] == 2 and d["removed_count"] == 1
    assert d["added"] == ["c", "d"] and d["removed"] == ["a"] and d["universe_size"] == 3
    gs, = _meta(tmp_path, "gap_start")
    assert gs["detail"]["reason"] == "universe_change"
    assert len(_meta(tmp_path, "gap_end")) == 1
    assert cap.reconnects == 1


async def test_universe_reconnects_rate_limited(tmp_path):
    uni = Universe(["a"])
    async with FakeMarketServer() as srv:
        w = _writer(tmp_path)
        cap = _capture(w, universe_fn=uni, ws_url=srv.url, universe_reconnect_min_s=1.0)
        task = asyncio.create_task(cap.run(0))
        await _until(lambda: len(srv.subs) == 1)
        uni.value = ["a", "b"]                 # first universe reconnect: allowed now
        await _until(lambda: len(srv.subs) == 2)
        uni.value = ["a", "b", "c"]            # second: must wait for the 1.0 s slot
        await _until(lambda: len(srv.subs) == 3, timeout=5.0)
        cap.request_stop("test")
        await asyncio.wait_for(task, 10)

    assert sorted(srv.subs[2][1]) == ["a", "b", "c"]
    # Judge the DECISION times the capture recorded (not wall-clock sleeps): load can
    # delay a reconnect but can never make two decisions closer than the limit.
    d0, d1 = cap.universe_reconnect_decisions
    assert d1 - d0 >= 1.0
    assert len(_meta(tmp_path, "universe_change")) == 2


async def test_missing_watchset_keeps_last_universe_one_meta_per_outage(tmp_path):
    uni = Universe(["a", "b"])
    w = _writer(tmp_path)
    cap = _capture(w, universe_fn=uni)
    await cap._refresh_universe()
    assert cap.desired == {"a", "b"}
    uni.value = None
    for _ in range(3):
        await cap._refresh_universe()
    uni.value = RuntimeError("memcached down")   # unreadable == missing, same outage
    await cap._refresh_universe()
    assert cap.desired == {"a", "b"}
    uni.value = ["a", "b"]
    await cap._refresh_universe()
    uni.value = []                              # empty list is not a usable universe
    await cap._refresh_universe()
    assert cap.desired == {"a", "b"}
    await cap.shutdown("test")

    missing = _meta(tmp_path, "watchset_missing")
    assert len(missing) == 2                    # two separate outages
    assert missing[0]["detail"]["kept_universe_size"] == 2
    assert len(_meta(tmp_path, "watchset_restored")) == 1


async def test_startup_without_universe_waits_and_never_subscribes_empty(tmp_path):
    uni = Universe(None)
    async with FakeMarketServer() as srv:
        w = _writer(tmp_path)
        cap = _capture(w, universe_fn=uni, ws_url=srv.url)
        task = asyncio.create_task(cap.run(0))
        await _until(lambda: uni.reads >= 3)
        assert srv.subs == [] and not cap.connected
        uni.value = ["z"]
        await _until(lambda: len(srv.subs) == 1)
        cap.request_stop("test")
        await asyncio.wait_for(task, 10)

    assert srv.subs[0][1] == ["z"]
    assert len(_meta(tmp_path, "watchset_missing")) == 1
    assert _meta(tmp_path, "gap_start") == []   # never connected before -> no gap


async def test_static_tokens_never_read_universe(tmp_path):
    uni = Universe(["x"])
    w = _writer(tmp_path)
    cap = _capture(w, tokens=["t1"], universe_fn=uni)
    await cap._refresh_universe()
    assert cap.desired == {"t1"} and uni.reads == 0
    await cap.shutdown("test")


def test_error_backoff_doubles_caps_and_resets_after_stable(tmp_path):
    w = _writer(tmp_path)
    cap = _capture(w, backoff_base_s=1.0, backoff_max_s=60.0, jitter_s=0.0, stable_reset_s=30.0)
    waits = [cap._error_wait() for _ in range(8)]
    assert waits == [1, 2, 4, 8, 16, 32, 60, 60]
    cap._note_conn_ended(5.0)                                # short-lived conn: no reset
    assert cap._error_wait() == 60
    cap._note_conn_ended(31.0)                               # stable conn: reset
    assert cap._error_wait() == 1
    assert cap._error_wait() == 2
    w._release_root_lock()


async def test_stable_planned_reconnect_resets_backoff(tmp_path):
    """A stable connection that ends in a PLANNED universe reconnect must still reset
    the backoff, so the next error starts again at the base wait."""
    uni = Universe(["a"])
    async with FakeMarketServer() as srv:
        w = _writer(tmp_path)
        cap = _capture(w, universe_fn=uni, ws_url=srv.url, stable_reset_s=0.2,
                       backoff_base_s=1.0)
        cap._backoff = 32.0                                  # grown by earlier errors
        task = asyncio.create_task(cap.run(0))
        await _until(lambda: len(srv.subs) == 1)
        await asyncio.sleep(0.3)                             # connection is now "stable"
        uni.value = ["a", "b"]
        await _until(lambda: len(srv.subs) == 2)
        cap.request_stop("test")
        await asyncio.wait_for(task, 10)
    assert cap._backoff == 1.0


# ---------------------------------------------------------------- shutdown

async def test_shutdown_writes_capture_stop_and_finalizes(tmp_path):
    w = _writer(tmp_path)
    cap = _capture(w, tokens=["t1"])
    for raw in _fixture_raws()[:20]:
        w.add_frame(raw)
    assert await cap.shutdown("SIGTERM") is True
    assert await cap.shutdown("again") is True              # idempotent
    stop, = _meta(tmp_path, "capture_stop")
    assert stop["detail"]["reason"] == "SIGTERM"
    assert stop["detail"]["stats"]["writer"]["rows_buffered"] > 0
    assert _read(tmp_path, "book") or _read(tmp_path, "changes")
    assert _inprogress(tmp_path) == []
    assert w.stats()["closed"]


async def test_shutdown_close_timeout_is_bounded(tmp_path, capsys):
    w = _writer(tmp_path)
    hang = asyncio.Event()

    async def hung_close():
        await hang.wait()

    w.close = hung_close
    cap = _capture(w, tokens=["t1"], close_timeout_s=0.2)
    t0 = time.monotonic()
    assert await cap.shutdown("SIGTERM") is False
    assert time.monotonic() - t0 < 1.0
    assert "timed out" in capsys.readouterr().out
    hang.set()
    await asyncio.sleep(0)
    w._release_root_lock()


async def test_sigterm_subprocess_exits_zero_with_capture_stop(tmp_path):
    async with FakeMarketServer(plan=[_fixture_raws()[:30]]) as srv:
        env = dict(os.environ, POLY_CAPTURE_PORT="0", PYTHONUNBUFFERED="1")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "services.book_capture", "--seconds", "0",
            "--root", str(tmp_path), "--tokens", "t1,t2", "--ws-url", srv.url,
            cwd=str(REPO), env=env, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
        await _until(lambda: len(srv.subs) == 1, timeout=8.0)
        await asyncio.sleep(0.2)
        proc.send_signal(signal.SIGTERM)
        out, _ = await asyncio.wait_for(proc.communicate(), 15)
    assert proc.returncode == 0, out.decode()
    assert srv.subs[0][1] == ["t1", "t2"]
    stop, = _meta(tmp_path, "capture_stop")
    assert stop["detail"]["reason"] == "SIGTERM"
    assert _read(tmp_path, "book")
    assert _inprogress(tmp_path) == []
    assert b"SUMMARY" in out


async def test_cli_writer_locked_exits_3(tmp_path):
    holder = bc.BookCaptureWriter(tmp_path)
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "services.book_capture", "--seconds", "1",
            "--root", str(tmp_path), "--tokens", "t1", "--ws-url", "ws://127.0.0.1:9",
            cwd=str(REPO), env=dict(os.environ, POLY_CAPTURE_PORT="0"),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), 15)
    finally:
        await holder.close()
    assert proc.returncode == 3, out.decode()
    assert b"another book_capture writer" in out


# ---------------------------------------------------------------- status endpoint

async def _http_get(port):
    r, wr = await asyncio.open_connection("127.0.0.1", port)
    wr.write(b"GET /status HTTP/1.1\r\nHost: x\r\n\r\n")
    await wr.drain()
    data = await r.read()
    wr.close()
    head, body = data.split(b"\r\n\r\n", 1)
    assert head.startswith(b"HTTP/1.1 200")
    return json.loads(body)


async def test_status_endpoint_json_shape(tmp_path):
    w = _writer(tmp_path)
    cap = _capture(w, tokens=["t1", "t2"], http_port=0)
    srv = await cap._start_http()
    assert srv is not None
    port = srv.sockets[0].getsockname()[1]
    st = await _http_get(port)
    assert set(st) >= {"connected", "subscribed_count", "reconnects", "last_msg_age_s",
                       "gap_count", "universe_size", "writer", "flush_stale"}
    assert st["connected"] is False and st["universe_size"] == 2
    assert st["last_msg_age_s"] is None and st["flush_stale"] is False
    assert "rows_written" in st["writer"] and "rows_buffered" in st["writer"]
    srv.close()
    await cap.shutdown("test")


async def test_flush_stale_flag(tmp_path):
    w = _writer(tmp_path)
    clock = {"ms": 1_000_000}
    cap = _capture(w, tokens=["t1"], now_ms_fn=lambda: clock["ms"])
    assert cap.status()["flush_stale"] is False               # nothing buffered
    w.add_frame(_fixture_raws()[0])
    assert cap.status()["flush_stale"] is False               # just started
    clock["ms"] += int(3 * w.flush_interval_s * 1000) + 1
    assert cap.status()["flush_stale"] is True
    await w.flush()
    w.counters["last_flush_ms"] = clock["ms"]
    w.add_frame(_fixture_raws()[1])
    assert cap.status()["flush_stale"] is False
    await cap.shutdown("test")


async def test_status_port_bind_failure_does_not_raise(tmp_path, capsys):
    blocker = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = blocker.sockets[0].getsockname()[1]
    w = _writer(tmp_path)
    cap = _capture(w, tokens=["t1"], http_port=port)
    assert await cap._start_http() is None
    assert "status server not started" in capsys.readouterr().out
    blocker.close()
    await cap.shutdown("test")


# ---------------------------------------------------------------- review fixes

async def test_shutdown_bounded_when_disk_hangs_mid_periodic_flush(tmp_path, capsys):
    """Disk hangs inside a flush-loop flush: close() times out AND the flush-loop task
    cannot finish its cancellation — shutdown must still return within its bound."""
    import threading
    w = bc.BookCaptureWriter(tmp_path, flush_interval_s=0.05, loop_tick_s=0.02)
    release, entered = threading.Event(), threading.Event()
    real = w._flush_sync_unlocked

    def hung(batch, meta, final):
        entered.set()
        release.wait(30)
        return real(batch, meta, final)

    w._flush_sync_unlocked = hung
    cap = _capture(w, tokens=["t1"], close_timeout_s=0.3, teardown_timeout_s=0.3)
    cap._tasks.append(asyncio.create_task(w.run_flush_loop()))
    w.add_frame(_fixture_raws()[0])
    await _until(entered.is_set, timeout=3.0)
    t0 = time.monotonic()
    try:
        assert await asyncio.wait_for(cap.shutdown("SIGTERM"), 5.0) is False
        assert time.monotonic() - t0 < 1.5
        assert cap.close_timed_out
        out = capsys.readouterr().out
        assert "timed out" in out and "abandoning" in out
    finally:
        release.set()
        await asyncio.sleep(0.3)
        for t in asyncio.all_tasks():
            if t is not asyncio.current_task():
                t.cancel()
        w._release_root_lock()


async def test_capture_start_precedes_startup_watchset_missing(tmp_path):
    uni = Universe(None)
    w = _writer(tmp_path)
    cap = _capture(w, universe_fn=uni)
    task = asyncio.create_task(cap.run(0))
    await _until(lambda: uni.reads >= 2)
    cap.request_stop("test")
    await asyncio.wait_for(task, 10)
    kinds = [m["kind"] for m in _meta(tmp_path)]
    assert kinds[:2] == ["capture_start", "watchset_missing"], kinds
    start, = _meta(tmp_path, "capture_start")
    assert start["detail"]["universe_size"] == 0
    assert len(_meta(tmp_path, "watchset_missing")) == 1


async def test_gap_open_in_status_and_capture_stop(tmp_path):
    w = _writer(tmp_path)
    cap = _capture(w, tokens=["t1"])
    assert cap.status()["gap_open"] is False
    cap._on_disconnect("ConnectionClosedError: boom")
    assert cap.status()["gap_open"] is True
    await cap.shutdown("SIGTERM")
    stop, = _meta(tmp_path, "capture_stop")
    assert stop["detail"]["stats"]["gap_open"] is True


# ---------------------------------------------------------------- quality-review fixes

async def test_pong_only_connection_trips_data_silence_watchdog(tmp_path):
    """Heartbeats alone must not look healthy: no DATA for data_silence_s on a
    connection older than data_silence_min_conn_s -> reconnect via the gap path."""
    async with FakeMarketServer() as srv:                    # answers PING with PONG only
        w = _writer(tmp_path)
        cap = _capture(w, tokens=["t1"], ws_url=srv.url, ping_interval_s=0.05,
                       data_silence_s=0.4, data_silence_min_conn_s=0.2)
        task = asyncio.create_task(cap.run(0))
        await _until(lambda: len(srv.subs) >= 2 and cap.connected, timeout=5.0)
        st = cap.status()
        cap.request_stop("test")
        await asyncio.wait_for(task, 10)
    assert srv.pings >= 1 and cap.heartbeats >= 1 and cap.frames == 0
    assert st["last_data_age_s"] is None
    gs = _meta(tmp_path, "gap_start")
    assert gs and gs[0]["detail"]["reason"] == "data silence"
    assert "last_data_ts" in gs[0]["detail"] and gs[0]["detail"]["last_data_ts"] is None
    ge = _meta(tmp_path, "gap_end")
    # PONGs kept last_recv_ts fresh; the gap must still cover the whole data silence
    assert ge and ge[0]["detail"]["gap_ms"] >= 0.4 * 1000


async def test_data_frames_keep_watchdog_quiet_and_set_last_data_age(tmp_path):
    raws = _fixture_raws()[:5]
    async with FakeMarketServer(plan=[raws]) as srv:
        w = _writer(tmp_path)
        cap = _capture(w, tokens=["t1"], ws_url=srv.url, ping_interval_s=0.05,
                       data_silence_s=5.0, data_silence_min_conn_s=0.1)
        task = asyncio.create_task(cap.run(0))
        await _until(lambda: cap.frames == 5 and cap.heartbeats >= 2)
        st = cap.status()
        cap.request_stop("test")
        await asyncio.wait_for(task, 10)
    assert len(srv.subs) == 1 and cap.gap_count == 0
    assert st["last_data_age_s"] is not None and st["last_msg_age_s"] is not None
    assert cap.last_data_ms <= cap.last_recv_ms


async def test_memcached_timeout_drops_pooled_client(tmp_path, monkeypatch):
    closed = []

    class HangClient:
        def __init__(self, *a, **kw):
            pass

        async def get(self, key):
            await asyncio.sleep(10)

        async def close(self):
            closed.append(True)

    class FakeMC:
        Client = HangClient

    monkeypatch.setattr(bc, "aiomcache", FakeMC)
    monkeypatch.setattr(bc, "MC_TIMEOUT_S", 0.05)
    w = _writer(tmp_path)
    cap = _capture(w, universe_fn=None)
    cap.desired = {"a"}
    await cap._refresh_universe()
    assert cap._mc is None and closed == [True]              # suspect pooled conn dropped
    assert cap.desired == {"a"}
    await cap.shutdown("test")
    missing, = _meta(tmp_path, "watchset_missing")
    assert "TimeoutError" in missing["detail"]["error"]


async def test_connect_params(tmp_path, monkeypatch):
    seen = {}

    def fake_connect(url, **kw):
        seen.update(kw, url=url)
        raise OSError("no network in tests")

    monkeypatch.setattr(bc.websockets, "connect", fake_connect)
    w = _writer(tmp_path)
    cap = _capture(w, tokens=["t1"], ws_url="ws://example.invalid/ws")
    with pytest.raises(OSError):
        await cap.run_once(float("inf"))
    assert seen == {"url": "ws://example.invalid/ws", "ping_interval": 10, "ping_timeout": 25,
                    "max_size": 2 ** 23, "open_timeout": 15, "close_timeout": 2}
    await cap.shutdown("test")


def test_fresh_capture_has_connection_fields(tmp_path):
    w = _writer(tmp_path)
    cap = _capture(w, tokens=["t1"])
    assert cap._established is False and cap._conn_start is None
    assert cap._gap_start_ms is None and cap._gap_last_recv is None
    assert cap.last_data_ms is None
    w._release_root_lock()


async def test_cli_exits_1_when_writer_close_times_out(tmp_path):
    code = (
        "import asyncio, sys\n"
        "import services.book_capture as bc\n"
        "async def hung_close(self):\n"
        "    await asyncio.Event().wait()\n"
        "bc.BookCaptureWriter.close = hung_close\n"
        "bc.BookCapture.__init__.__kwdefaults__['close_timeout_s'] = 0.3\n"
        "sys.exit(bc.main(sys.argv[1:]))\n")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, "--seconds", "1", "--root", str(tmp_path),
        "--tokens", "t1", "--ws-url", "ws://127.0.0.1:9",
        cwd=str(REPO), env=dict(os.environ, POLY_CAPTURE_PORT="0"),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(proc.communicate(), 15)
    assert proc.returncode == 1, out.decode()
    assert b"timed out" in out


def test_cli_data_silence_flag(monkeypatch, tmp_path):
    seen = {}

    class Stop(Exception):
        pass

    def fake_init(self, writer, **kw):
        seen.update(kw)
        raise Stop

    monkeypatch.setattr(bc.BookCapture, "__init__", fake_init)
    cases = ((["--data-silence-s", "0"], 0.0),
             (["--data-silence-s", "42.5"], 42.5),
             ([], bc.DATA_SILENCE_S))
    for i, (argv, want) in enumerate(cases):
        with pytest.raises(Stop):   # one root per case: main()'s writer is never closed here
            bc.main(["--root", str(tmp_path / str(i)), "--tokens", "t1"] + argv)
        assert seen["data_silence_s"] == want
        seen.clear()

"""Tests for services/book_capture.py — normalization, schemas, buffered writer.

All output goes to tmp_path; nothing touches the real storage/ tree.
"""
import asyncio
import json
import os
import time
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from services import book_capture as bc

FIXTURE = Path(__file__).parent / "fixtures" / "book_capture_events.jsonl"
HOUR_MS = 3_600_000
# 2026-10-01 10:00:00 UTC
T10 = 1790848800000


def _fixture_frames():
    with open(FIXTURE) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _by_ds(rows):
    out = {}
    for ds, row in rows:
        out.setdefault(ds, []).append(row)
    return out


def _pc(asset="A1", price="0.5", size="10", side="buy", ts="1790848800000", **extra):
    entry = {"asset_id": asset, "price": price, "size": size, "side": side,
             "hash": "h1", "best_bid": "0.49", "best_ask": "0.51"}
    entry.update(extra)
    return json.dumps({"market": "0xM", "price_changes": [entry],
                       "timestamp": ts, "event_type": "price_change"})


def _files(root, ds, pattern="*/*.parquet"):
    return sorted((Path(root) / ds).glob(pattern))


def _count(root, ds):
    return duckdb.sql(
        f"select count(*) from read_parquet('{root}/{ds}/*/*.parquet')").fetchone()[0]


def _meta(root):
    files = _files(root, "meta")
    if not files:
        return []
    return pa.concat_tables([pq.read_table(f) for f in files]).to_pylist()


# ---------------------------------------------------------------- Task 1: normalization

def test_fixture_normalizes_without_exceptions_and_counts_match():
    frames = _fixture_frames()
    assert len(frames) == 296
    n_entries = n_books = 0
    book_levels = []
    for fr in frames:
        obj = json.loads(fr["raw"])
        for ev in (obj if isinstance(obj, list) else [obj]):
            if ev["event_type"] == "price_change":
                n_entries += len(ev["price_changes"])
            elif ev["event_type"] == "book":
                n_books += 1
                book_levels.append((len(ev["bids"]), len(ev["asks"])))

    rows = []
    for fr in frames:
        out = bc.normalize_frame(fr["raw"], fr["recv_ts"])
        assert all(r["recv_ts"] == fr["recv_ts"] for _, r in out)
        rows.extend(out)
    by = _by_ds(rows)
    assert len(by.get("changes", [])) == n_entries == 590
    assert len(by.get("book", [])) == n_books == 22
    assert "quarantine" not in by
    # full depth preserved, in order received
    got_levels = [(len(json.loads(r["bids_json"])), len(json.loads(r["asks_json"])))
                  for r in by["book"]]
    assert got_levels == book_levels
    for r in by["changes"]:
        assert r["side"] in ("BUY", "SELL")
        assert isinstance(r["exch_ts"], int)


def test_price_change_multi_asset_one_event_n_rows():
    raw = json.dumps({"market": "0xM", "timestamp": "1790876622756", "event_type": "price_change",
                      "price_changes": [
                          {"asset_id": "A", "price": "0.9", "size": "0", "side": "buy",
                           "hash": "h", "best_bid": "0.91", "best_ask": "0.95"},
                          {"asset_id": "B", "price": "0.1", "size": "5", "side": "SELL",
                           "hash": "h2", "best_bid": "", "best_ask": None}]})
    out = bc.normalize_frame(raw, 123)
    assert [ds for ds, _ in out] == ["changes", "changes"]
    a, b = out[0][1], out[1][1]
    assert a == {"recv_ts": 123, "exch_ts": 1790876622756, "asset_id": "A", "market": "0xM",
                 "price": 0.9, "size": 0.0, "side": "BUY", "hash": "h",
                 "best_bid": 0.91, "best_ask": 0.95}
    assert b["asset_id"] == "B" and b["side"] == "SELL"
    assert b["best_bid"] is None and b["best_ask"] is None


def test_book_levels_compact_json_and_empty_sides():
    raw = json.dumps({"market": "0xM", "asset_id": "A", "timestamp": "5", "hash": "h",
                      "bids": [], "asks": [{"price": "0.999", "size": "1048.19"},
                                           {"price": "0.998", "size": "1"}],
                      "tick_size": "0.001", "event_type": "book", "last_trade_price": ""})
    (ds, r), = bc.normalize_frame(raw, 7)
    assert ds == "book"
    assert r["bids_json"] == "[]"
    assert json.loads(r["asks_json"]) == [[0.999, 1048.19], [0.998, 1.0]]
    assert " " not in r["asks_json"]
    assert r["tick_size"] == 0.001 and r["exch_ts"] == 5 and r["recv_ts"] == 7


@pytest.mark.parametrize("tx_key,fee_key", [("transaction_hash", "fee_rate_bps"),
                                            ("transactionHash", "feeRateBps")])
def test_last_trade_price_tolerant_keys(tx_key, fee_key):
    raw = json.dumps({"event_type": "last_trade_price", "asset_id": "A", "market": "0xM",
                      "price": "0.42", "size": "100", "side": "sell", fee_key: "0",
                      "timestamp": "99", tx_key: "0xabc"})
    (ds, r), = bc.normalize_frame(raw, 1)
    assert ds == "trades"
    assert r == {"recv_ts": 1, "exch_ts": 99, "asset_id": "A", "market": "0xM", "price": 0.42,
                 "size": 100.0, "side": "SELL", "fee_rate_bps": 0.0, "tx_hash": "0xabc"}


def test_tick_size_change_goes_to_meta():
    ev = {"event_type": "tick_size_change", "asset_id": "A", "market": "0xM",
          "old_tick_size": "0.01", "new_tick_size": "0.001", "timestamp": "3"}
    (ds, r), = bc.normalize_frame(json.dumps(ev), 9)
    assert ds == "meta" and r["kind"] == "tick_size_change" and r["recv_ts"] == 9
    assert json.loads(r["detail_json"]) == ev


@pytest.mark.parametrize("raw,etype", [
    (json.dumps({"event_type": "best_bid_ask", "asset_id": "A"}), "best_bid_ask"),
    (json.dumps({"event_type": "new_market"}), "new_market"),
    (json.dumps({"event_type": "market_resolved"}), "market_resolved"),
    (json.dumps({"event_type": "something_new"}), "something_new"),
    (json.dumps({"asset_id": "A"}), None),
    ("{not json", None),
    (json.dumps(42), None),
    (json.dumps([1, "x"]), None),
    (_pc(price="abc"), "price_change"),
    (_pc(price="nan"), "price_change"),
    (_pc(side=None), "price_change"),
    (_pc(ts="yesterday"), "price_change"),
    (json.dumps({"event_type": "price_change", "market": "0xM", "price_changes": "oops"}), "price_change"),
    (json.dumps({"event_type": "book", "asset_id": "A", "bids": [{"price": "x"}], "asks": []}), "book"),
    (json.dumps({"event_type": "book", "asset_id": "A", "bids": None, "asks": []}), "book"),
    (json.dumps({"event_type": "last_trade_price", "asset_id": "A", "price": "", "size": "1"}),
     "last_trade_price"),
    (json.dumps({"event_type": ["weird"]}), None),
])
def test_unknown_or_malformed_goes_to_quarantine(raw, etype):
    out = bc.normalize_frame(raw, 5)
    assert out, raw
    for ds, r in out:
        assert ds == "quarantine"
        assert r["recv_ts"] == 5 and r["event_type"] == etype
        assert isinstance(r["raw_json"], str) and r["raw_json"]


def test_list_frame_mixes_good_and_quarantined():
    good = json.loads(_pc())
    raw = json.dumps([good, {"event_type": "best_bid_ask"}, "junk"])
    by = _by_ds(bc.normalize_frame(raw, 1))
    assert len(by["changes"]) == 1 and len(by["quarantine"]) == 2


@pytest.mark.parametrize("raw", ["PING", "PONG", "", "   "])
def test_heartbeats_and_empty_produce_nothing(raw):
    assert bc.normalize_frame(raw, 1) == []


@pytest.mark.parametrize("raw", [None, b"\xff\xfe", 12345, object()])
def test_normalize_never_raises_on_garbage_types(raw):
    out = bc.normalize_frame(raw, 1)
    assert all(ds == "quarantine" for ds, _ in out)


def test_schemas_round_trip(tmp_path):
    rows = []
    for fr in _fixture_frames():
        rows.extend(bc.normalize_frame(fr["raw"], fr["recv_ts"]))
    rows += bc.normalize_frame(json.dumps({"event_type": "last_trade_price", "asset_id": "A",
                                           "price": "0.1", "size": "2", "side": "BUY",
                                           "timestamp": "1"}), 3)
    rows += bc.normalize_frame(json.dumps({"event_type": "tick_size_change", "asset_id": "A"}), 3)
    rows += bc.normalize_frame("garbage", 3)
    by = _by_ds(rows)
    assert set(by) == set(bc.SCHEMAS)
    for ds, schema in bc.SCHEMAS.items():
        t = pa.Table.from_pylist(by[ds], schema=schema)
        p = tmp_path / f"{ds}.parquet"
        pq.write_table(t, p, compression="zstd", compression_level=3)
        back = pq.read_table(p)
        assert back.schema.equals(schema)
        assert back.to_pylist() == by[ds]


# ---------------------------------------------------------------- Task 2: writer

class Clock:
    def __init__(self, ms):
        self.ms = ms

    def __call__(self):
        return self.ms


def _writer(tmp_path, clock=None, **kw):
    kw.setdefault("free_bytes_fn", lambda: 100 * bc.GB)
    return bc.BookCaptureWriter(root=tmp_path / "bc", now_ms_fn=clock or Clock(T10), **kw)


async def test_add_frame_buffers_without_touching_disk(tmp_path):
    w = _writer(tmp_path)
    n = w.add_frame(_pc(), T10)
    assert n == 1
    assert w.stats()["rows_buffered"] == 1
    assert not any((tmp_path / "bc").rglob("*.parquet*"))
    await w.close()


async def test_fixture_end_to_end_layout_and_duckdb(tmp_path):
    w = _writer(tmp_path)
    frames = _fixture_frames()
    for fr in frames:
        w.add_frame(fr["raw"], fr["recv_ts"])
    await w.flush()
    # mid-hour: file still open, invisible to the *.parquet glob
    root = tmp_path / "bc"
    assert _files(root, "changes", "*/*.parquet.inprogress")
    await w.close()
    assert not list(root.rglob("*.inprogress"))
    assert _count(root, "changes") == 590
    assert _count(root, "book") == 22
    s = w.stats()
    assert s["rows_written"]["changes"] == 590 and s["rows_written"]["book"] == 22
    assert s["rows_buffered"] == 0
    # layout <root>/<dataset>/YYYY-MM-DD/HH.parquet by recv_ts UTC hour
    hours = {time.strftime("%Y-%m-%d/%H", time.gmtime(fr["recv_ts"] // 1000)) for fr in frames}
    got = {f"{p.parent.name}/{p.name.split('.')[0]}" for p in _files(root, "changes")}
    assert got == hours
    # zstd
    md = pq.ParquetFile(_files(root, "changes")[0]).metadata
    assert md.row_group(0).column(0).compression == "ZSTD"


async def test_buffer_spanning_hour_boundary_splits(tmp_path):
    w = _writer(tmp_path, clock=Clock(T10 + 2 * HOUR_MS))
    w.add_frame(_pc(asset="early"), T10 + HOUR_MS - 1)   # 10:59:59.999
    w.add_frame(_pc(asset="late"), T10 + HOUR_MS)        # 11:00:00.000
    await w.close()
    files = _files(tmp_path / "bc", "changes")
    assert [f.name for f in files] == ["10.parquet", "11.parquet"]
    assert pq.read_table(files[0]).column("asset_id").to_pylist() == ["early"]
    assert pq.read_table(files[1]).column("asset_id").to_pylist() == ["late"]


async def test_rotation_finalizes_past_hours_and_row_groups_per_flush(tmp_path):
    clock = Clock(T10 + 10)
    w = _writer(tmp_path, clock=clock)
    root = tmp_path / "bc"
    w.add_frame(_pc(), T10 + 1)
    await w.flush()
    w.add_frame(_pc(), T10 + 2)
    await w.flush()
    assert not _files(root, "changes")                        # still in progress
    clock.ms = T10 + HOUR_MS + 5                               # hour advances
    w.add_frame(_pc(), T10 + HOUR_MS + 1)
    await w.flush()
    done = _files(root, "changes")
    assert [f.name for f in done] == ["10.parquet"]
    assert pq.ParquetFile(done[0]).metadata.num_row_groups == 2
    assert _files(root, "changes", "*/11.parquet.inprogress")
    # empty flush still rotates
    clock.ms = T10 + 2 * HOUR_MS
    await w.flush()
    assert [f.name for f in _files(root, "changes")] == ["10.parquet", "11.parquet"]
    await w.close()


async def test_restart_within_hour_never_overwrites(tmp_path):
    for _ in range(3):
        w = _writer(tmp_path)
        w.add_frame(_pc(), T10 + 1)
        await w.close()
    names = [f.name for f in _files(tmp_path / "bc", "changes")]
    assert names == ["10.1.parquet", "10.2.parquet", "10.parquet"]
    assert _count(tmp_path / "bc", "changes") == 3


async def test_crash_leftover_renamed_corrupt_and_logged(tmp_path):
    day = tmp_path / "bc" / "changes" / "2026-10-01"
    day.mkdir(parents=True)
    left = day / "10.parquet.inprogress"
    left.write_bytes(b"PAR1-half-written")
    w = _writer(tmp_path)
    assert not left.exists()
    assert (day / "10.parquet.corrupt").read_bytes() == b"PAR1-half-written"
    w.add_frame(_pc(), T10 + 1)
    await w.close()
    # new data for the same hour skips the stem held by the corrupt file, so a later
    # crash leftover (10.1.parquet.inprogress -> 10.1.parquet.corrupt) can never collide
    assert [f.name for f in _files(tmp_path / "bc", "changes")] == ["10.1.parquet"]
    assert (day / "10.parquet.corrupt").exists()
    kinds = [m for m in _meta(tmp_path / "bc") if m["kind"] == "crash_leftover"]
    assert len(kinds) == 1
    assert "10.parquet.inprogress" in json.loads(kinds[0]["detail_json"])["path"]


def _fail_writes(monkeypatch, n_failures):
    calls = {"n": 0}
    real = pq.ParquetWriter.write_table

    def flaky(self, table, *a, **k):
        calls["n"] += 1
        if calls["n"] <= n_failures:
            raise OSError("simulated EIO")
        return real(self, table, *a, **k)

    monkeypatch.setattr(pq.ParquetWriter, "write_table", flaky)
    return calls


async def test_flush_failure_retry_succeeds(tmp_path, monkeypatch):
    w = _writer(tmp_path)
    for i in range(5):
        w.add_frame(_pc(asset=f"A{i}"), T10 + i)
    _fail_writes(monkeypatch, 1)
    await w.flush()
    monkeypatch.undo()
    await w.close()
    root = tmp_path / "bc"
    assert _count(root, "changes") == 5
    s = w.stats()
    assert s["flush_errors"] == 0 and s["rows_dropped_flush_error"] == 0
    assert s["flush_retries"] == 1
    assert not [m for m in _meta(root) if m["kind"] == "flush_error"]
    assert not list(root.rglob("*.inprogress"))


async def test_flush_failure_twice_drops_batch_and_records_meta(tmp_path, monkeypatch):
    w = _writer(tmp_path)
    for i in range(5):
        w.add_frame(_pc(asset=f"A{i}"), T10 + i)
    _fail_writes(monkeypatch, 2)
    await w.flush()
    monkeypatch.undo()
    s = w.stats()
    assert s["flush_errors"] == 1 and s["rows_dropped_flush_error"] == 5
    # writer recovers: subsequent rows land
    w.add_frame(_pc(asset="after"), T10 + 10)
    await w.close()
    root = tmp_path / "bc"
    assert _count(root, "changes") == 1
    errs = [m for m in _meta(root) if m["kind"] == "flush_error"]
    assert len(errs) == 1
    d = json.loads(errs[0]["detail_json"])
    assert d["dropped_rows"] == 5 and d["dataset"] == "changes" and "simulated EIO" in d["error"]
    assert not list(root.rglob("*.inprogress"))


async def test_overflow_drops_oldest_one_meta_per_flush(tmp_path):
    w = _writer(tmp_path, buffer_cap=10, flush_rows=10_000)
    for i in range(15):
        w.add_frame(_pc(asset=f"A{i:02d}"), T10 + i)
    s = w.stats()
    assert s["rows_buffered"] == 10 and s["rows_dropped_overflow"] == 5
    await w.flush()
    for i in range(12):
        w.add_frame(_pc(asset=f"B{i:02d}"), T10 + 100 + i)
    await w.close()
    root = tmp_path / "bc"
    assets = duckdb.sql(f"select asset_id from read_parquet('{root}/changes/*/*.parquet') "
                        "order by recv_ts").fetchall()
    assets = [a for (a,) in assets]
    assert assets[:10] == [f"A{i:02d}" for i in range(5, 15)]       # oldest 5 dropped
    assert assets[10:] == [f"B{i:02d}" for i in range(2, 12)]
    drops = [json.loads(m["detail_json"])["dropped_rows"]
             for m in _meta(root) if m["kind"] == "overflow_drop"]
    assert drops == [5, 2]


async def test_disk_floor_pause_and_resume(tmp_path):
    free = {"b": 100 * bc.GB}
    clock = Clock(T10)
    w = _writer(tmp_path, clock=clock, free_bytes_fn=lambda: free["b"],
                disk_floor_bytes=2 * bc.GB, disk_check_interval_s=0)
    w.add_frame(_pc(asset="ok1"), T10 + 1)
    free["b"] = 1 * bc.GB
    for i in range(4):
        w.add_frame(_pc(asset=f"lost{i}"), T10 + 2 + i)
    s = w.stats()
    assert s["paused"] is True and s["frames_discarded_paused"] == 4
    free["b"] = int(2.3 * bc.GB)                 # above floor but below floor+0.5GB
    w.add_frame(_pc(asset="lost-hyst"), T10 + 10)
    assert w.stats()["paused"] is True
    free["b"] = int(2.6 * bc.GB)
    w.add_frame(_pc(asset="ok2"), T10 + 20)
    assert w.stats()["paused"] is False
    await w.close()
    root = tmp_path / "bc"
    got = [a for (a,) in duckdb.sql(
        f"select asset_id from read_parquet('{root}/changes/*/*.parquet') order by recv_ts").fetchall()]
    assert got == ["ok1", "ok2"]
    kinds = [m["kind"] for m in _meta(root)]
    assert kinds.count("disk_floor_pause") == 1 and kinds.count("disk_floor_resume") == 1


async def test_disk_check_is_rate_limited(tmp_path):
    calls = {"n": 0}

    def free():
        calls["n"] += 1
        return 100 * bc.GB

    w = _writer(tmp_path, free_bytes_fn=free, disk_check_interval_s=60)
    for i in range(1000):
        w.add_frame(_pc(), T10 + i)
    assert calls["n"] <= 1
    await w.close()


async def test_flush_loop_triggers_on_row_threshold(tmp_path):
    w = _writer(tmp_path, flush_rows=50, flush_interval_s=3600)
    task = asyncio.create_task(w.run_flush_loop())
    for i in range(60):
        w.add_frame(_pc(), T10 + i)
    for _ in range(100):
        await asyncio.sleep(0.02)
        if w.stats()["flushes"] >= 1:
            break
    assert w.stats()["flushes"] >= 1 and w.stats()["rows_buffered"] == 0
    await w.close()
    await asyncio.wait_for(task, 2)


async def test_flush_loop_triggers_on_interval(tmp_path):
    w = _writer(tmp_path, flush_rows=10_000, flush_interval_s=0.1, loop_tick_s=0.02)
    task = asyncio.create_task(w.run_flush_loop())
    w.add_frame(_pc(), T10)
    for _ in range(100):
        await asyncio.sleep(0.02)
        if w.stats()["rows_written"]["changes"] == 1:   # buffer empties at swap, before the write lands
            break
    assert w.stats()["rows_written"]["changes"] == 1
    await w.close()
    await asyncio.wait_for(task, 2)


async def test_concurrent_flushes_serialize(tmp_path):
    w = _writer(tmp_path)
    for i in range(100):
        w.add_frame(_pc(), T10 + i)
    await asyncio.gather(*(w.flush() for _ in range(5)))
    await w.close()
    assert _count(tmp_path / "bc", "changes") == 100


async def test_add_meta_and_add_frame_never_raise(tmp_path):
    w = _writer(tmp_path)
    assert w.add_frame(None, T10) >= 0
    assert w.add_frame(b"\x00", T10) >= 0
    w.add_meta("ws_connect", {"tokens": 3, "obj": object()})
    w.add_meta("reconnect", {"n": 1}, recv_ts=T10 + 5)
    await w.close()
    kinds = [m["kind"] for m in _meta(tmp_path / "bc")]
    assert "ws_connect" in kinds and "reconnect" in kinds
    assert _count(tmp_path / "bc", "quarantine") == 2


async def test_close_is_idempotent_and_rejects_later_frames(tmp_path):
    w = _writer(tmp_path)
    w.add_frame(_pc(), T10)
    await w.close()
    await w.close()
    assert w.add_frame(_pc(), T10) == 0
    assert _count(tmp_path / "bc", "changes") == 1


async def test_perf_100k_events_under_60s(tmp_path):
    w = _writer(tmp_path, clock=Clock(T10 + HOUR_MS))
    levels = [{"price": f"{0.01 * i:.2f}", "size": f"{i * 3.5:.2f}"} for i in range(1, 21)]
    book = json.dumps({"market": "0xM", "asset_id": "A", "timestamp": "1", "hash": "h",
                       "bids": levels, "asks": levels, "tick_size": "0.01", "event_type": "book"})
    t0 = time.perf_counter()
    n_changes = n_books = 0
    for i in range(100_000):
        if i % 10 == 0:
            w.add_frame(book, T10 + i)
            n_books += 1
        else:
            w.add_frame(_pc(asset=f"A{i % 300}", price=f"0.{i % 97:02d}", size=str(i)), T10 + i)
            n_changes += 1
        if w.stats()["rows_buffered"] >= w.flush_rows:
            await w.flush()
    await w.close()
    elapsed = time.perf_counter() - t0
    root = tmp_path / "bc"
    assert _count(root, "changes") == n_changes
    assert _count(root, "book") == n_books
    print(f"\n[perf] 100K events -> {n_changes} changes + {n_books} book rows in {elapsed:.2f}s")
    assert elapsed < 60


# ---------------------------------------------------------------- spec-review fixes

def test_recover_never_overwrites_existing_corrupt(tmp_path):
    day = tmp_path / "bc" / "changes" / "2026-10-01"
    day.mkdir(parents=True)
    (day / "05.parquet.corrupt").write_bytes(b"older-crash")
    (day / "05.parquet.inprogress").write_bytes(b"newer-crash")
    w = _writer(tmp_path)
    corrupt = sorted(day.glob("*.corrupt"))
    assert len(corrupt) == 2
    assert sorted(p.read_bytes() for p in corrupt) == [b"newer-crash", b"older-crash"]
    assert (day / "05.parquet.corrupt").read_bytes() == b"older-crash"
    assert not list(day.glob("*.inprogress"))
    asyncio.run(w.close())


async def test_finalize_failure_never_overwrites_existing_corrupt(tmp_path, monkeypatch):
    w = _writer(tmp_path)
    w.add_frame(_pc(), T10 + 1)
    await w.flush()
    (f,) = w._open.values()
    pre = f.tmp_path.with_name(f.tmp_path.name[: -len(".inprogress")] + ".corrupt")
    pre.write_bytes(b"pre-existing")
    monkeypatch.setattr(bc.pq, "read_metadata", lambda *a, **k: (_ for _ in ()).throw(OSError("bad footer")))
    await w.close()
    monkeypatch.undo()
    assert pre.read_bytes() == b"pre-existing"
    corrupt = sorted(pre.parent.glob("*.corrupt"))
    assert len(corrupt) == 2
    assert w.stats()["files_corrupt"] >= 1   # the meta file also fails the patched footer check


async def test_second_writer_on_same_root_is_refused(tmp_path):
    w1 = _writer(tmp_path)
    w1.add_frame(_pc(), T10 + 1)
    await w1.flush()
    live = list((tmp_path / "bc").rglob("*.inprogress"))
    assert len(live) == 1
    with pytest.raises(bc.WriterLockedError):
        _writer(tmp_path)
    assert live[0].exists()                                   # not renamed to .corrupt
    assert not list((tmp_path / "bc").rglob("*.corrupt"))
    await w1.close()
    assert _count(tmp_path / "bc", "changes") == 1
    w2 = _writer(tmp_path)                                    # lock released on close
    await w2.close()


async def test_add_meta_after_close_is_dropped_and_logged(tmp_path, capsys):
    w = _writer(tmp_path)
    await w.close()
    w.add_meta("late_kind", {"x": 1})
    assert w.stats()["meta_buffered"] == 0
    assert "[meta] dropped after close: late_kind" in capsys.readouterr().out

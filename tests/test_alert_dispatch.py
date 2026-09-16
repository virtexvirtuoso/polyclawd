#!/usr/bin/env python3
"""TDD tests for signals/alert_dispatch.py (Task 5.1) — cases (a)-(i) from
docs/plans/2026-07-16-alert-system-overhaul.md, plus the >6h drop rule.

All sends monkeypatched — no real Telegram traffic. Temp sqlite per test.
"""
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import signals.alert_dispatch as ad


@pytest.fixture
def db(tmp_path):
    return tmp_path / "dispatch_test.db"


@pytest.fixture
def sender(monkeypatch):
    """Fake alert_openclaw capturing calls; toggle state['ok'] to simulate failure."""
    state = {"ok": True, "calls": []}

    def fake(message, channel="telegram", silent=False, parse_mode=None, **kw):
        state["calls"].append({"message": message, "parse_mode": parse_mode})
        return state["ok"]

    monkeypatch.setattr(ad, "alert_openclaw", fake)
    return state


def rows(db, table="alert_queue"):
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(f"SELECT * FROM {table}")]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


# (a) tier 1 calls sender immediately
def test_tier1_sends_immediately(db, sender):
    ok = ad.dispatch("stops", "🛑 stop hit", ad.TIER_CRITICAL, db_path=db)
    assert ok is True
    assert len(sender["calls"]) == 1
    assert sender["calls"][0]["message"] == "🛑 stop hit"
    assert rows(db) == []  # nothing queued on success


# (b) tier 1 with failing sender enqueues a redelivery row; drain() resends it
def test_tier1_failure_enqueues_then_drain_redelivers(db, sender):
    sender["ok"] = False
    ok = ad.dispatch("stops", "🛑 stop hit", ad.TIER_CRITICAL, db_path=db)
    assert ok is False
    q = rows(db)
    assert len(q) == 1 and q[0]["tier"] == 1 and q[0]["shadow"] == 0

    sender["ok"] = True
    sender["calls"].clear()
    n = ad.drain(db_path=db)  # redelivery goes out regardless of batch window
    assert n == 1
    assert len(sender["calls"]) == 1
    assert sender["calls"][0]["message"].startswith("(redelivery)")
    assert "🛑 stop hit" in sender["calls"][0]["message"]
    assert rows(db) == []


# (c) tier 2 enqueues; drain(now=t+16min) sends exactly one combined message
def test_tier2_batch_flushes_after_window(db, sender):
    ad.dispatch("rising_wallets", "wallet A entered X", ad.TIER_BATCH, dedup_key="a", db_path=db)
    ad.dispatch("rising_wallets", "wallet B entered Y", ad.TIER_BATCH, dedup_key="b", db_path=db)
    sender["calls"].clear()
    n = ad.drain(db_path=db, now=time.time() + 16 * 60)
    assert n == 1
    assert len(sender["calls"]) == 1
    msg = sender["calls"][0]["message"]
    assert msg.startswith("📨 rising_wallets — 2 events (")
    assert "wallet A entered X" in msg and "wallet B entered Y" in msg
    assert rows(db) == []


# (d) drain before the 15-min window sends nothing
def test_tier2_not_flushed_before_window(db, sender):
    ad.dispatch("rising_wallets", "wallet A entered X", ad.TIER_BATCH, dedup_key="a", db_path=db)
    sender["calls"].clear()
    n = ad.drain(db_path=db, now=time.time() + 5 * 60)
    assert n == 0
    assert sender["calls"] == []
    assert len(rows(db)) == 1  # still queued


# (e) duplicate (pipeline, dedup_key) within open batch inserted once
def test_dedup_key_within_window(db, sender):
    ad.dispatch("graduation", "wallet W graduated", ad.TIER_BATCH, dedup_key="W", db_path=db)
    ad.dispatch("graduation", "wallet W graduated (again)", ad.TIER_BATCH, dedup_key="W", db_path=db)
    q = rows(db)
    assert len(q) == 1
    assert q[0]["message"] == "wallet W graduated"


# (f) tier 4 never sends — suppressed-log only
def test_tier4_suppresses_and_logs(db, sender):
    ok = ad.dispatch("bybit_listings", "new listing FOO", ad.TIER_SUPPRESS, db_path=db)
    assert ok is True
    assert sender["calls"] == []
    assert rows(db) == []
    slog = rows(db, "alert_suppressed_log")
    assert len(slog) == 1 and slog[0]["pipeline"] == "bybit_listings"
    # and drain never touches it
    assert ad.drain(db_path=db, now=time.time() + 16 * 60) == 0
    assert sender["calls"] == []


# (g) two concurrent enqueues from separate connections both land
def test_concurrent_enqueues_both_land(db, sender):
    ad.dispatch("warm", "warmup", ad.TIER_BATCH, dedup_key="warm", db_path=db)  # create tables
    errs = []

    def enqueue(key):
        try:
            ad.dispatch("whale_resolutions", f"event {key}", ad.TIER_BATCH,
                        dedup_key=key, db_path=db)
        except Exception as ex:  # noqa: BLE001
            errs.append(ex)

    t1 = threading.Thread(target=enqueue, args=("k1",))
    t2 = threading.Thread(target=enqueue, args=("k2",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert errs == []
    got = {r["dedup_key"] for r in rows(db) if r["pipeline"] == "whale_resolutions"}
    assert got == {"k1", "k2"}


# (h) enqueue under a held write lock falls back to direct send (F4 fail-open)
def test_lock_contention_falls_back_to_direct_send(db, sender):
    ad.dispatch("warm", "warmup", ad.TIER_BATCH, dedup_key="warm", db_path=db)  # create tables
    holder = sqlite3.connect(str(db))
    holder.execute("BEGIN IMMEDIATE")
    try:
        sender["calls"].clear()
        ok = ad.dispatch("rising_wallets", "urgent-ish", ad.TIER_BATCH,
                         dedup_key="z", db_path=db)
        assert ok is True
        assert len(sender["calls"]) == 1
        assert sender["calls"][0]["message"] == "urgent-ish"
    finally:
        holder.rollback()
        holder.close()
    assert not any(r["dedup_key"] == "z" for r in rows(db))  # never landed in queue


# (i) shadow=True rows are recorded by drain() but NEVER sent
def test_shadow_rows_logged_never_sent(db, sender):
    ok = ad.dispatch("whale_resolutions", "resolved: market M", ad.TIER_BATCH,
                     dedup_key="m", shadow=True, db_path=db)
    assert ok is True
    assert sender["calls"] == []  # dispatch never sends shadow
    q = rows(db)
    assert len(q) == 1 and q[0]["shadow"] == 1

    n = ad.drain(db_path=db, now=time.time() + 16 * 60)
    assert n == 0  # nothing SENT
    assert sender["calls"] == []
    shlog = rows(db, "alert_shadow_log")
    assert len(shlog) == 1
    assert shlog[0]["pipeline"] == "whale_resolutions" and shlog[0]["n_events"] == 1
    assert rows(db) == []  # recorded and cleared


# >6h rows are dropped to the suppressed log, not replayed forever (F2)
def test_stale_rows_dropped_after_6h(db, sender):
    ad.dispatch("rising_wallets", "ancient event", ad.TIER_BATCH, dedup_key="old", db_path=db)
    con = sqlite3.connect(str(db))
    con.execute("UPDATE alert_queue SET ts = ts - ?", (7 * 3600,))
    con.commit(); con.close()

    sender["calls"].clear()
    n = ad.drain(db_path=db)
    assert n == 0
    assert sender["calls"] == []
    assert rows(db) == []
    slog = rows(db, "alert_suppressed_log")
    assert any("ancient event" in r["message"] and "6h" in r["reason"] for r in slog)

"""RISING WALLETS alert filters (2026-08-18).

Two noise classes killed at the alert layer (seeding/graduation untouched):
1. Thin-category climbers: negative/small monthly PnL wallets that jump 10+
   spots on economics/culture boards purely because the field is tiny.
   -> riser must show pnl >= $10K (same bar as the NEW-WALLETS alert).
2. Short-cycle grinders: monthly-green wallets farming 5m/15m "Up or Down"
   markets (e.g. 0x251c1a28: 100 open up/down positions, -$4K unrealized).
   -> >50% of open positions in up-or-down markets = suppress + tag grinder=1
      in pm_wallets so they never re-alert (no repeat API calls).
API failure fails OPEN (alert anyway) — never lose a real riser to a blip.
"""
import json
import sqlite3
from pathlib import Path

import pytest

from scripts import pm_leaderboard_scraper as pls


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "whale_meta.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE pm_wallets (wallet TEXT PRIMARY KEY, name TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(pls, "META_DB_PATH", db)
    monkeypatch.setattr(pls, "_RANK_RISER_DEDUP_FILE", tmp_path / "dedup.json")
    sent = []
    monkeypatch.setattr(pls, "send_telegram", lambda msg: sent.append(msg))
    monkeypatch.setattr(pls, "_shadow_dispatch", lambda *a, **k: None)
    return {"db": db, "sent": sent}


def _riser(**kw):
    base = {"wallet": "0xabc1", "name": "test-wallet", "seed_rank": 28,
            "current_rank": 16, "category": "crypto", "pnl": 109_000.0}
    base.update(kw)
    return base


def _seed_wallet(db, wallet, grinder=None):
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO pm_wallets (wallet, name) VALUES (?, 'x')", (wallet,))
    if grinder is not None:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pm_wallets)")}
        if "grinder" not in cols:
            conn.execute("ALTER TABLE pm_wallets ADD COLUMN grinder INTEGER DEFAULT 0")
        conn.execute("UPDATE pm_wallets SET grinder=? WHERE wallet=?", (grinder, wallet))
    conn.commit()
    conn.close()


def _grinder_flag(db, wallet):
    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT grinder FROM pm_wallets WHERE wallet=?", (wallet,)).fetchone()
    conn.close()
    return row[0] if row else None


# --- filter 1: PnL floor ----------------------------------------------------

def test_negative_pnl_riser_suppressed(env, monkeypatch):
    monkeypatch.setattr(pls, "_short_cycle_share", lambda w: 0.0)
    pls.alert_rank_risers([_riser(pnl=-5_823.0)])
    assert env["sent"] == []


def test_small_positive_pnl_riser_suppressed(env, monkeypatch):
    monkeypatch.setattr(pls, "_short_cycle_share", lambda w: 0.0)
    pls.alert_rank_risers([_riser(pnl=4_000.0)])
    assert env["sent"] == []


def test_big_pnl_clean_riser_alerts(env, monkeypatch):
    monkeypatch.setattr(pls, "_short_cycle_share", lambda w: 0.0)
    _seed_wallet(env["db"], "0xabc1")
    pls.alert_rank_risers([_riser()])
    assert len(env["sent"]) == 1
    assert "test-wallet" in env["sent"][0]


# --- filter 2: short-cycle grinders ----------------------------------------

def test_grinder_share_suppresses_and_tags(env, monkeypatch):
    monkeypatch.setattr(pls, "_short_cycle_share", lambda w: 0.92)
    _seed_wallet(env["db"], "0xabc1")
    pls.alert_rank_risers([_riser()])
    assert env["sent"] == []
    assert _grinder_flag(env["db"], "0xabc1") == 1


def test_tagged_grinder_skipped_without_api_call(env, monkeypatch):
    def boom(w):
        raise AssertionError("positions API must not be called for tagged grinders")
    monkeypatch.setattr(pls, "_short_cycle_share", boom)
    _seed_wallet(env["db"], "0xabc1", grinder=1)
    pls.alert_rank_risers([_riser()])
    assert env["sent"] == []


def test_api_failure_fails_open(env, monkeypatch):
    monkeypatch.setattr(pls, "_short_cycle_share", lambda w: None)
    _seed_wallet(env["db"], "0xabc1")
    pls.alert_rank_risers([_riser()])
    assert len(env["sent"]) == 1


def test_mixed_batch_only_clean_big_riser_alerts(env, monkeypatch):
    shares = {"0xgood": 0.0, "0xgrind": 0.8}
    monkeypatch.setattr(pls, "_short_cycle_share", lambda w: shares[w])
    _seed_wallet(env["db"], "0xgood")
    _seed_wallet(env["db"], "0xgrind")
    pls.alert_rank_risers([
        _riser(wallet="0xgood", name="real-climber", pnl=109_000.0),
        _riser(wallet="0xgrind", name="updown-farmer", pnl=44_000.0),
        _riser(wallet="0xthin", name="less-red", pnl=-26_072.0),
    ])
    assert len(env["sent"]) == 1
    assert "real-climber" in env["sent"][0]
    assert "updown-farmer" not in env["sent"][0]
    assert "less-red" not in env["sent"][0]


# --- _short_cycle_share unit ------------------------------------------------

def test_short_cycle_share_computation(monkeypatch):
    positions = [
        {"title": "Bitcoin Up or Down - August 18, 3:35PM ET", "slug": "btc-up-or-down"},
        {"title": "Ethereum Up or Down - August 18, 4PM ET", "slug": "eth-up-or-down"},
        {"title": "Will the Fed cut rates in September?", "slug": "fed-september"},
        {"title": "XRP up or down - 15m", "slug": "xrp-updown-15m"},
    ]
    monkeypatch.setattr(pls, "_fetch_positions", lambda w: positions)
    assert pls._short_cycle_share("0xany") == pytest.approx(0.75)


def test_short_cycle_share_none_on_fetch_failure(monkeypatch):
    monkeypatch.setattr(pls, "_fetch_positions", lambda w: None)
    assert pls._short_cycle_share("0xany") is None


def test_short_cycle_share_none_on_empty(monkeypatch):
    monkeypatch.setattr(pls, "_fetch_positions", lambda w: [])
    assert pls._short_cycle_share("0xany") is None

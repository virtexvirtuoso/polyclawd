#!/usr/bin/env python3
"""Tests for scripts/wallet_allowlist.py — selector, fail-safe tiering, gate report."""

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.wallet_allowlist as wa


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "shadow.db"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE smart_wallet_shadows (id INTEGER PRIMARY KEY, wallet TEXT, ts_alert INTEGER,"
        " price_at_alert REAL, outcome_result TEXT, alert_type TEXT, direction TEXT,"
        " resolved INTEGER, near_settled INTEGER)"
    )
    con.commit()
    con.close()
    monkeypatch.setenv("POLYCLAWD_ALLOWLIST_DB", str(db))
    monkeypatch.setenv("POLYCLAWD_ALLOWLIST_SNAPSHOTS", str(tmp_path / "snaps.json"))
    monkeypatch.setenv("POLYCLAWD_ALLOWLIST_CURRENT", str(tmp_path / "current.json"))
    return db


def _add(db, wallet, days_ago, price, result, alert_type="entry", direction="BUY", resolved=1, near=0):
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO smart_wallet_shadows (wallet, ts_alert, price_at_alert, outcome_result,"
        " alert_type, direction, resolved, near_settled) VALUES (?,?,?,?,?,?,?,?)",
        (wallet, int(time.time() - days_ago * 86400), price, result, alert_type, direction, resolved, near),
    )
    con.commit()
    con.close()


def test_select_picks_top_positive_wallets(env):
    for _ in range(12):
        _add(env, "0xAAA", 5, 0.30, "WIN")
    for _ in range(12):
        _add(env, "0xBBB", 5, 0.30, "LOSS")
    snap = wa.select()
    assert list(snap["wallets"]) == ["0xAAA"]


def test_select_requires_min_n(env):
    for _ in range(wa.MIN_N - 1):
        _add(env, "0xAAA", 5, 0.30, "WIN")
    snap = wa.select()
    assert snap["wallets"] == {}


def test_select_ignores_ineligible_rows(env):
    for _ in range(12):
        _add(env, "0xAAA", 45, 0.30, "WIN")  # outside 30d window
    for _ in range(12):
        _add(env, "0xBBB", 5, 0.70, "WIN")  # price >= 0.60
    for _ in range(12):
        _add(env, "0xCCC", 5, 0.30, "WIN", direction="SELL")
    for _ in range(12):
        _add(env, "0xDDD", 5, 0.30, "WIN", resolved=0)
    for _ in range(12):
        _add(env, "0xEEE", 5, 0.30, "WIN", near=1)
    snap = wa.select()
    assert snap["wallets"] == {}


def test_select_replaces_same_day_snapshot(env):
    for _ in range(12):
        _add(env, "0xAAA", 5, 0.30, "WIN")
    wa.select()
    wa.select()
    snaps = json.loads(Path(wa._snapshots_path()).read_text())["snapshots"]
    assert len(snaps) == 1


def test_current_allowlist_fresh_lowercased(env):
    wa.select()
    got = wa.current_allowlist()
    assert got == {"0xaaa"}


def test_current_allowlist_stale_failsafe(env):
    wa.select()
    cur = json.loads(Path(wa._current_path()).read_text())
    cur["ts"] = int(time.time() - 72 * 3600)
    Path(wa._current_path()).write_text(json.dumps(cur))
    assert wa.current_allowlist() == set()


def test_current_allowlist_missing_file_failsafe(env):
    assert wa.current_allowlist() == set()


def test_page_tier_for_promotes_allowlisted(env, monkeypatch):
    import scripts.alert_dispatch as ad

    monkeypatch.setattr(ad, "TIER_BATCH", 2, raising=False)
    monkeypatch.setattr(ad, "TIER_DIGEST", 3, raising=False)
    wa.select()
    assert wa.page_tier_for("0xAAA", "entry") == 2
    assert wa.page_tier_for("0xaaa", "refire") == 2
    assert wa.page_tier_for("0xOTHER", "entry") == 3
    assert wa.page_tier_for("0xAAA", "exit") == 3
    assert wa.page_tier_for(None, "entry") == 3


def test_page_tier_for_failsafe_on_missing_state(env, monkeypatch):
    monkeypatch.setenv("POLYCLAWD_ALLOWLIST_CURRENT", "/nonexistent/current.json")
    assert wa.page_tier_for("0xAAA", "entry") == 3


def test_allowlisted_at_uses_latest_snapshot_before_ts(env):
    wa.select()
    snaps = json.loads(Path(wa._snapshots_path()).read_text())["snapshots"]
    s = snaps[0]
    assert wa._allowlisted_at(snaps, s["ts"] + 10) == {"0xaaa"}
    assert wa._allowlisted_at(snaps, s["ts"] - 10) == set()


def test_report_runs_and_gate_not_yet_with_no_forward_data(env, capsys):
    wa.select()
    wa.report()
    out = capsys.readouterr().out
    assert "NOT YET" in out
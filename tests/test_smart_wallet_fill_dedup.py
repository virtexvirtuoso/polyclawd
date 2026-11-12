"""Fill dedup across both smart-wallet alert paths (task 1.5, audit #30 and #59).

The fast poll (90 s) and the whale sweep (5 min) both reach `scanner_hook` ->
`run_from_scan` -> `check_and_fire` -> `_accumulate`; the sweep's inclusive
`since_ts` cursor also re-serves the newest second. Trade identity used to be
lost before `_accumulate`, so one fill counted twice against the $1,000
threshold. `_accumulate` now stores a per-trade key in each window entry and
skips a key already inside the window.

No network. State lives in an in-memory sqlite (or tmp_path for the shadow DB).
"""
import json
import os
import sqlite3
import subprocess
import sys
import types
from pathlib import Path

import pytest

from scripts import smart_wallet_alert as swa
from scripts import smart_wallet_fast_poll as swfp
from scripts import trade_key as tk

W = "0xabc"
CID = "0xcid"
SMART = {W: {"name": "Test Whale", "win_rate": 0.6, "net_pnl": 1000,
             "closed_positions": 50, "source_category": "sports", "is_bot": 0}}


def _trade(tx="0xtx1", size=1500.0, price=0.4, wallet=W, asset="tok1",
           side="BUY", ts=1000):
    """size * price = 600 by default: below THRESHOLD, so nothing fires."""
    return {"transactionHash": tx, "asset": asset, "proxyWallet": wallet,
            "conditionId": CID, "side": side, "size": size, "price": price,
            "outcome": "Yes", "outcomeIndex": 0, "title": "Test Market",
            "timestamp": ts}


@pytest.fixture()
def meta():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    swa.init_accum(c)
    return c


@pytest.fixture()
def hooks(meta, tmp_path, monkeypatch):
    """The two real entry points, sharing one accumulator, shadow DB in tmp."""
    monkeypatch.setattr(swa, "SHADOW_DB", tmp_path / "shadow.db")

    def via_run_from_scan(trades):
        sconn = sqlite3.connect(str(tmp_path / "shadow-direct.db"))
        sconn.row_factory = sqlite3.Row
        swa.init_shadows(sconn)
        try:
            return swa.run_from_scan(meta, sconn, trades, {}, SMART)
        finally:
            sconn.close()

    def via_scanner_hook(trades):
        return swa.scanner_hook(meta, trades, {}, SMART)

    return via_run_from_scan, via_scanner_hook


def _accum(meta):
    return meta.execute(
        "SELECT total_usd, num_fills, fills_json FROM smart_wallet_accum "
        "WHERE wallet=? AND market=?", (W, CID)).fetchone()


def test_same_trade_through_both_hooks_counts_once(meta, hooks):
    """The plan's test: 600, not 1,200."""
    via_run_from_scan, via_scanner_hook = hooks
    t = _trade()
    via_run_from_scan([t])
    via_scanner_hook([dict(t)])
    row = _accum(meta)
    assert row["total_usd"] == pytest.approx(600.0)
    assert row["num_fills"] == 1


def test_same_trade_twice_through_same_hook_counts_once(meta, hooks):
    """Inclusive same-second cursor: the newest trade is served again."""
    _, via_scanner_hook = hooks
    t = _trade()
    via_scanner_hook([t])
    via_scanner_hook([dict(t)])
    assert _accum(meta)["total_usd"] == pytest.approx(600.0)


def test_two_trades_same_second_same_size_both_count(meta, hooks):
    """Distinct transaction hashes keep two equal-looking fills apart."""
    via_run_from_scan, _ = hooks
    via_run_from_scan([_trade(tx="0xA", size=500.0, ts=1000),
                       _trade(tx="0xB", size=500.0, ts=1000)])
    via_run_from_scan([_trade(tx="0xA", size=500.0, ts=1000),
                       _trade(tx="0xB", size=500.0, ts=1000)])
    assert _accum(meta)["total_usd"] == pytest.approx(400.0)  # 2 x 200, once each


def test_overlapping_batches_count_each_trade_once(meta, hooks):
    """Batch 1 = A,B (aggregated into one fill); batch 2 = B,C. Only C is new."""
    via_run_from_scan, via_scanner_hook = hooks
    a, b, c = (_trade(tx=t, size=500.0) for t in ("0xA", "0xB", "0xC"))  # $200 each
    via_run_from_scan([a, b])
    via_scanner_hook([b, c])
    row = _accum(meta)
    assert row["total_usd"] == pytest.approx(600.0)
    assert row["num_fills"] == 2


def test_trade_without_hash_is_never_deduped(meta, hooks):
    """No transactionHash -> no safe identity -> keyless: both deliveries count
    (over-count rather than silently drop a real fill)."""
    via_run_from_scan, _ = hooks
    t = _trade(size=500.0)
    del t["transactionHash"]
    via_run_from_scan([t])
    via_run_from_scan([dict(t)])
    assert _accum(meta)["total_usd"] == pytest.approx(400.0)


def test_legacy_three_element_entry_still_counts_and_does_not_block(meta, hooks, monkeypatch):
    """An entry written before this change has no key: it counts toward the
    total, reads without error, and does not stop a new trade."""
    now = 1_700_000_000
    monkeypatch.setattr(swa, "time", types.SimpleNamespace(time=lambda: now))
    via_run_from_scan, _ = hooks
    meta.execute(
        "INSERT INTO smart_wallet_accum (wallet, market, direction, outcome, "
        "fills_json, total_usd, num_fills, first_seen, last_seen) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (W, CID, "BUY", "Yes", json.dumps([[now - 100, 300.0, 0.5]]),
         300.0, 1, now - 100, now - 100))
    via_run_from_scan([_trade(size=500.0)])   # $200
    row = _accum(meta)
    assert row["total_usd"] == pytest.approx(500.0)
    assert row["num_fills"] == 2
    via_run_from_scan([_trade(size=500.0)])   # same trade again: still once
    assert _accum(meta)["total_usd"] == pytest.approx(500.0)


def test_duplicate_after_window_expiry_counts_again(meta):
    """Dedup applies inside the 4h window only."""
    args = (meta, W, CID, "BUY", "Yes")
    swa._accumulate(*args, 200.0, 0.4, 1000, parts=[("k1", 200.0, 0.4)])
    inside = swa._accumulate(*args, 200.0, 0.4, 1000 + swa.ACCUM_WINDOW - 1,
                             parts=[("k1", 200.0, 0.4)])
    assert inside[0] == pytest.approx(200.0)
    after = swa._accumulate(*args, 200.0, 0.4, 1000 + swa.ACCUM_WINDOW + 1,
                            parts=[("k1", 200.0, 0.4)])
    assert after[0] == pytest.approx(200.0)       # first entry aged out, dup is new
    assert after[1] == 1


def test_accumulate_stores_keys_as_a_list_fourth_element(meta):
    swa._accumulate(meta, W, CID, "BUY", "Yes", 200.0, 0.4, 1000,
                    parts=[("k1", 200.0, 0.4)])
    stored = json.loads(_accum(meta)["fills_json"])
    assert stored == [[1000, 200.0, 0.4, ["k1"]]]


def test_accumulate_without_key_keeps_three_element_shape(meta):
    swa._accumulate(meta, W, CID, "BUY", "Yes", 200.0, 0.4, 1000)
    swa._accumulate(meta, W, CID, "BUY", "Yes", 200.0, 0.4, 1000)
    assert json.loads(_accum(meta)["fills_json"]) == [[1000, 200.0, 0.4]] * 2


def test_trade_key_lives_in_alert_module_and_fast_poll_reexports_it():
    assert swfp._trade_key is swa._trade_key
    assert swfp._norm is swa._norm
    base = _trade()
    assert swa._trade_key({**base, "size": 100}) == swa._trade_key({**base, "size": "100.0"})
    assert swa._trade_key({**base, "size": 100}) != swa._trade_key({**base, "size": 101})


# --------------------------------------------------------------------------- #
# Twin fills: one transaction, several identical fills (task 1.5b)
# --------------------------------------------------------------------------- #
def _stored_keys(meta):
    fills = json.loads(_accum(meta)["fills_json"])
    return [k for f in fills for k in swa._entry_keys(f)]


def test_twin_fills_in_one_batch_both_count(meta, hooks):
    """A taker order matching two makers for the same size and price yields two
    real rows with identical hash/asset/wallet/side/size/price. Both count."""
    via_run_from_scan, _ = hooks
    t = _trade(size=500.0)                                    # $200 each
    via_run_from_scan([t, dict(t)])
    base = swa._accum_key(t)
    assert _accum(meta)["total_usd"] == pytest.approx(400.0)
    assert _stored_keys(meta) == [f"{base}#0", f"{base}#1"]


def test_twin_batch_delivered_again_adds_nothing(meta, hooks):
    via_run_from_scan, via_scanner_hook = hooks
    t = _trade(size=500.0)
    via_run_from_scan([t, dict(t)])
    via_scanner_hook([dict(t), dict(t)])
    via_run_from_scan([dict(t), dict(t)])
    assert _accum(meta)["total_usd"] == pytest.approx(400.0)
    assert len(_stored_keys(meta)) == 2


def test_one_twin_then_both_adds_exactly_one(meta, hooks):
    """Path 1 (fast poll collapse) delivers one twin; the sweep delivers both."""
    via_run_from_scan, via_scanner_hook = hooks
    t = _trade(size=500.0)
    via_scanner_hook([t])
    assert _accum(meta)["total_usd"] == pytest.approx(200.0)
    via_run_from_scan([dict(t), dict(t)])
    assert _accum(meta)["total_usd"] == pytest.approx(400.0)


def test_three_twins_then_three_again_count_three(meta, hooks):
    via_run_from_scan, _ = hooks
    t = _trade(size=500.0)
    via_run_from_scan([t, dict(t), dict(t)])
    via_run_from_scan([dict(t), dict(t), dict(t)])
    assert _accum(meta)["total_usd"] == pytest.approx(600.0)
    assert len(_stored_keys(meta)) == 3


def test_single_trade_key_is_base_hash_zero():
    t = _trade()
    (fill,) = swa.fills_from_trades([t], SMART)
    assert fill["trade_parts"] == [(f"{swa._accum_key(t)}#0", 600.0, 0.4)]


def test_numbering_counts_the_whole_batch_not_each_fill():
    """Same base key on a second, different-fill row must not restart at #0
    for a row of another (wallet, market, side, outcome) with another key."""
    a = _trade(size=500.0)
    b = _trade(tx="0xB", size=500.0)
    parts = swa.fills_from_trades([a, b, dict(a)], SMART)[0]["trade_parts"]
    assert [p[0] for p in parts] == [f"{swa._accum_key(a)}#0",
                                     f"{swa._accum_key(b)}#0",
                                     f"{swa._accum_key(a)}#1"]


def test_keyless_trade_gets_no_number():
    t = _trade(size=500.0)
    del t["transactionHash"]
    (fill,) = swa.fills_from_trades([t, dict(t)], SMART)
    assert [p[0] for p in fill["trade_parts"]] == [None, None]


# --------------------------------------------------------------------------- #
# Stored shapes (task 1.5b change 2)
# --------------------------------------------------------------------------- #
def test_entry_keys_accepts_all_three_stored_shapes():
    assert swa._entry_keys([1, 2.0, 0.5]) == ()
    assert swa._entry_keys([1, 2.0, 0.5, "k"]) == ("k",)        # written by 8fcff46
    assert swa._entry_keys([1, 2.0, 0.5, ["k", "j"]]) == ("k", "j")


def test_accumulate_always_writes_a_list_even_for_one_key(meta):
    swa._accumulate(meta, W, CID, "BUY", "Yes", 400.0, 0.4, 1000,
                    parts=[("a", 200.0, 0.4), ("b", 200.0, 0.4)])
    swa._accumulate(meta, W, CID, "BUY", "Yes", 200.0, 0.4, 1001,
                    parts=[("c", 200.0, 0.4)])
    swa._accumulate(meta, W, CID, "BUY", "Yes", 200.0, 0.4, 1002,
                    parts=[(None, 200.0, 0.4)])
    stored = json.loads(_accum(meta)["fills_json"])
    assert stored == [[1000, 400.0, 0.4, ["a", "b"]],
                      [1001, 200.0, 0.4, ["c"]],
                      [1002, 200.0, 0.4]]


def test_string_key_written_by_the_earlier_commit_still_dedups(meta, hooks, monkeypatch):
    now = 1_700_000_000
    monkeypatch.setattr(swa, "time", types.SimpleNamespace(time=lambda: now))
    via_run_from_scan, _ = hooks
    t = _trade(size=500.0)
    meta.execute(
        "INSERT INTO smart_wallet_accum (wallet, market, direction, outcome, "
        "fills_json, total_usd, num_fills, first_seen, last_seen) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (W, CID, "BUY", "Yes",
         json.dumps([[now - 10, 200.0, 0.4, f"{swa._accum_key(t)}#0"]]),
         200.0, 1, now - 10, now - 10))
    via_run_from_scan([t])
    assert _accum(meta)["total_usd"] == pytest.approx(200.0)


# --------------------------------------------------------------------------- #
# Overlap and mixed-shape cases (task 1.5b change 5)
# --------------------------------------------------------------------------- #
def test_batch_a_then_batch_a_b_adds_only_b(meta, hooks):
    via_run_from_scan, _ = hooks
    a, b = _trade(tx="0xA", size=500.0), _trade(tx="0xB", size=500.0)
    via_run_from_scan([a])
    via_run_from_scan([dict(a), b])
    row = _accum(meta)
    assert row["total_usd"] == pytest.approx(400.0)
    assert row["num_fills"] == 2


def test_stored_multi_key_entry_then_single_delivery_adds_nothing(meta, hooks):
    via_run_from_scan, via_scanner_hook = hooks
    a, b = _trade(tx="0xA", size=500.0), _trade(tx="0xB", size=500.0)
    via_run_from_scan([a, b])
    assert len(json.loads(_accum(meta)["fills_json"])) == 1     # one multi-key entry
    via_scanner_hook([dict(a)])
    via_scanner_hook([dict(b)])
    row = _accum(meta)
    assert row["total_usd"] == pytest.approx(400.0)
    assert row["num_fills"] == 1


def _old_code_total(fills_json, now, add_usd):
    """The window arithmetic of `_accumulate` at d4fca3c, which only knows
    entry[0] and entry[1]:
        fills = json.loads(row["fills_json"] or "[]")
        fills.append([int(now), float(usd), float(price)])
        cutoff = now - ACCUM_WINDOW
        fills = [f for f in fills if f[0] >= cutoff]
        total = sum(f[1] for f in fills)
    """
    fills = json.loads(fills_json or "[]")
    fills.append([int(now), float(add_usd), 0.5])
    cutoff = now - swa.ACCUM_WINDOW
    fills = [f for f in fills if f[0] >= cutoff]
    return sum(f[1] for f in fills)


def test_rollback_shape_old_code_reads_new_state(meta):
    """If this change is rolled back, the old code must read what the new code
    stored: entries keep [ts, usd, price] in front, the key list is extra."""
    now = 1_000_000
    swa._accumulate(meta, W, CID, "BUY", "Yes", 400.0, 0.4, now - swa.ACCUM_WINDOW - 5,
                    parts=[("old", 400.0, 0.4)])                          # will expire
    swa._accumulate(meta, W, CID, "BUY", "Yes", 400.0, 0.4, now - 20,
                    parts=[("a", 200.0, 0.4), ("b", 200.0, 0.4)])       # multi-key
    swa._accumulate(meta, W, CID, "BUY", "Yes", 100.0, 0.4, now - 10,
                    parts=[("c", 100.0, 0.4)])                           # one key
    swa._accumulate(meta, W, CID, "BUY", "Yes", 50.0, 0.4, now - 5,
                    parts=[(None, 50.0, 0.4)])                           # keyless
    new_total = swa._accumulate(meta, W, CID, "BUY", "Yes", 25.0, 0.5, now,
                                parts=[("d", 25.0, 0.5)])[0]
    old_total = _old_code_total(_accum(meta)["fills_json"], now, 25.0)
    # old code re-adds the new fill on top of what is stored (which already has it)
    assert old_total == pytest.approx(new_total + 25.0)
    assert new_total == pytest.approx(575.0)


# --------------------------------------------------------------------------- #
# Adapter contract (task 1.5b change 5e)
# --------------------------------------------------------------------------- #
def test_v2_row_through_adapter_gets_the_same_accum_key_as_a_v1_row():
    """Contract between config.data_api.map_v2_value and the dedup: the
    accumulator keys on transactionHash/asset/proxyWallet. If the adapter ever
    renames one of those v2 fields differently, _accum_key silently becomes
    None (or forks) and the dedup is off with no error; this test fails first."""
    from config.data_api import map_v2_value
    v2 = {"transaction_hash": "0xhash", "proxy_wallet": W, "token_id": "99",
          "condition_id": CID, "outcome": "Yes", "outcome_index": 0,
          "price": 0.42, "side": "BUY", "size": 5, "timestamp": 1700000000}
    v1 = {"transactionHash": "0xhash", "proxyWallet": W, "asset": "99",
          "conditionId": CID, "outcome": "Yes", "outcomeIndex": 0,
          "price": 0.42, "side": "BUY", "size": 5, "timestamp": 1700000000}
    mapped = map_v2_value(v2)
    assert swa._accum_key(mapped) is not None
    assert swa._accum_key(mapped) == swa._accum_key(v1)


# --------------------------------------------------------------------------- #
# Key helpers live in a leaf module (task 1.5b change 4)
# --------------------------------------------------------------------------- #
def test_key_helpers_are_one_object_in_all_three_modules():
    assert swfp._trade_key is swa._trade_key is tk._trade_key
    assert swfp._norm is swa._norm is tk._norm


def test_importing_fast_poll_does_not_import_the_alert_module(tmp_path):
    """Other tests in this process have already imported both, so ask a fresh
    interpreter. A failing import of the alert module (config.data_api,
    signals.alert_dispatch, ...) must not take down every fast-poll importer."""
    repo = Path(__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "COINALYZE_API_KEY")}
    env.update(HOME=str(tmp_path), POLYCLAWD_ENV_FILE="/dev/null",
               POLYCLAWD_MODE="PAPER", PYTHONDONTWRITEBYTECODE="1")
    code = ("import sys, json; import scripts.smart_wallet_fast_poll; "
            "print(json.dumps([m for m in ('scripts.smart_wallet_alert', "
            "'scripts.trade_key') if m in sys.modules]))")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(repo), env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == ["scripts.trade_key"]

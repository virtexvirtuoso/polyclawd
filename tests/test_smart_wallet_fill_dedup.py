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
import sqlite3

import pytest

from scripts import smart_wallet_alert as swa
from scripts import smart_wallet_fast_poll as swfp

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


def test_legacy_three_element_entry_still_counts_and_does_not_block(meta, hooks):
    """An entry written before this change has no key: it counts toward the
    total, reads without error, and does not stop a new trade."""
    import time
    via_run_from_scan, _ = hooks
    now = int(time.time())
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
    swa._accumulate(*args, 200.0, 0.4, 1000, fill_key="k1")
    inside = swa._accumulate(*args, 200.0, 0.4, 1000 + swa.ACCUM_WINDOW - 1,
                             fill_key="k1")
    assert inside[0] == pytest.approx(200.0)
    after = swa._accumulate(*args, 200.0, 0.4, 1000 + swa.ACCUM_WINDOW + 1,
                            fill_key="k1")
    assert after[0] == pytest.approx(200.0)       # first entry aged out, dup is new
    assert after[1] == 1


def test_accumulate_stores_key_as_fourth_element(meta):
    swa._accumulate(meta, W, CID, "BUY", "Yes", 200.0, 0.4, 1000, fill_key="k1")
    stored = json.loads(_accum(meta)["fills_json"])
    assert stored == [[1000, 200.0, 0.4, "k1"]]


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

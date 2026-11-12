"""Write window of the smart-wallet alert path (task 1.13).

`check_and_fire` used to hold SQLite's write lock on whale_meta.db (and on
shadow_trades.db) from the first accumulator or shadow write of a cycle until
the commits at the very end, across an order-book HTTP call, Telegram sends and
other slow work per fill. Every other writer in the scheduler process waited out
its busy timeout and failed with `database is locked`. Each write is now
committed right after it is made, and `scanner_hook` opens the shadow DB through
`db.connect` (busy_timeout 30000) like every other writer.

Both databases are real WAL files under tmp_path. A "probe" is a second
connection with a 0.2 s timeout that tries `BEGIN IMMEDIATE` + INSERT + COMMIT
into a scratch table of the same file: it succeeds only when the code under test
holds no write transaction on that file at that moment.
"""
import json
import sqlite3
import sys

import pytest

from scripts import smart_wallet_alert as swa

W = "0xabc"
SMART = {W: {"name": "Test Whale", "win_rate": 0.6, "net_pnl": 1000,
             "closed_positions": 50, "source_category": "sports", "is_bot": 0}}
NOW = 1_700_000_000
BOOK = {"executable_ask": 0.41, "exec_best_ask": 0.40,
        "exec_fillable_usd": 500.0, "exec_spread": 0.02}


# --------------------------------------------------------------------------- #
# fixtures and helpers
# --------------------------------------------------------------------------- #
def _open(path, timeout=30):
    c = sqlite3.connect(str(path), timeout=timeout)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


@pytest.fixture()
def paths(tmp_path, monkeypatch):
    meta_p, shadow_p = tmp_path / "whale_meta.db", tmp_path / "shadow_trades.db"
    monkeypatch.setattr(swa, "SHADOW_DB", shadow_p)
    # keep the test off the real allowlist file and off every send path
    monkeypatch.setattr(swa, "page_tier_for", lambda wallet, alert_type: 3)
    return meta_p, shadow_p


@pytest.fixture()
def dbs(paths):
    meta_p, shadow_p = paths
    meta, shadow = _open(meta_p), _open(shadow_p)
    swa.init_accum(meta)
    swa.init_shadows(shadow)
    for c in (meta, shadow):
        c.execute("CREATE TABLE probe_scratch (k INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)")
        c.commit()
    yield meta, shadow
    for c in (meta, shadow):
        try:
            c.close()
        except sqlite3.Error:
            pass


def _probe(path, label, results):
    """Try a write to `path` from a second connection; append (label, outcome)."""
    c = sqlite3.connect(str(path), timeout=0.2, isolation_level=None)
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT INTO probe_scratch (v) VALUES (?)", (label,))
        c.execute("COMMIT")
        results.append((label, "ok"))
    except sqlite3.OperationalError as exc:
        results.append((label, str(exc)))
    finally:
        c.close()


def _meta_provider(volume_by_market=None):
    vols = volume_by_market or {}

    def provider(cid):
        return {"volume": vols.get(cid, 1_000_000), "price": 0.40,
                "title": f"Title {cid}", "close_time": "2026-12-31",
                "slug": f"slug-{cid}"}
    return provider


def _fill(wallet=W, market="m1", direction="BUY", usd=1100.0, price=0.40,
          outcome="Yes", outcome_index=0, is_bot=0):
    return dict(wallet=wallet, market=market, direction=direction, usd=usd,
                price=price, outcome=outcome, outcome_index=outcome_index,
                name=wallet, title=f"Title {market}", is_bot=is_bot)


@pytest.fixture()
def sends(monkeypatch):
    """Record every delivery attempt of check_and_fire instead of sending."""
    rec = {"dispatch": [], "telegram": []}
    monkeypatch.setattr(swa, "dispatch",
                        lambda pipeline, msg, tier, **kw: rec["dispatch"].append((pipeline, msg, tier)))
    import scripts.alert_formatter as af
    monkeypatch.setattr(af, "send_telegram", lambda msg, *a, **k: rec["telegram"].append(msg))
    return rec


def _rows(path, sql):
    c = sqlite3.connect(str(path), timeout=5)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql)]
    finally:
        c.close()


# --------------------------------------------------------------------------- #
# 1. the lock is free during slow work
# --------------------------------------------------------------------------- #
def test_meta_lock_is_free_while_the_order_book_is_fetched(dbs, paths, monkeypatch):
    meta, shadow = dbs
    results = []

    def slow_book(cid, outcome_index, direction):
        _probe(paths[0], "meta@book", results)
        return dict(BOOK)

    monkeypatch.setattr(swa, "_executable_snapshot", slow_book)
    swa.check_and_fire(meta, shadow, [_fill()], _meta_provider(), send=False, now=NOW)
    assert results == [("meta@book", "ok")]


def test_meta_lock_is_free_right_after_the_accumulator_write(dbs, paths):
    """meta_for runs after _accumulate and before _mark_fired: only the commit
    after _accumulate can free the lock here."""
    meta, shadow = dbs
    results = []
    provider = _meta_provider()

    def probing_meta_for(cid):
        _probe(paths[0], "meta@meta_for", results)
        return provider(cid)

    swa.check_and_fire(meta, shadow, [_fill()], probing_meta_for, send=False, now=NOW)
    assert results == [("meta@meta_for", "ok")]


def test_both_locks_are_free_while_the_alert_is_sent(dbs, paths, monkeypatch):
    """The first slow step after the shadow write is the delivery."""
    meta, shadow = dbs
    results = []
    monkeypatch.setattr(swa, "_executable_snapshot", lambda *a, **k: dict(BOOK))

    def slow_dispatch(pipeline, msg, tier, **kw):
        _probe(paths[0], "meta@dispatch", results)
        _probe(paths[1], "shadow@dispatch", results)

    monkeypatch.setattr(swa, "dispatch", slow_dispatch)
    fired = swa.check_and_fire(meta, shadow, [_fill()], _meta_provider(), send=True, now=NOW)
    assert len(fired) == 1 and fired[0]["alert_type"] == "entry"
    assert results == [("meta@dispatch", "ok"), ("shadow@dispatch", "ok")]


def test_shadow_lock_is_free_while_the_second_fills_book_is_fetched(dbs, paths, monkeypatch):
    """Second fill's slow step follows the first fill's shadow write."""
    meta, shadow = dbs
    results = []
    calls = []

    def slow_book(cid, outcome_index, direction):
        calls.append(cid)
        if len(calls) == 2:
            _probe(paths[1], "shadow@book2", results)
        return dict(BOOK)

    monkeypatch.setattr(swa, "_executable_snapshot", slow_book)
    swa.check_and_fire(meta, shadow, [_fill(market="m1"), _fill(market="m2")],
                       _meta_provider(), send=False, now=NOW)
    assert results == [("shadow@book2", "ok")]


# --------------------------------------------------------------------------- #
# 2. same alerts, same totals (expectations captured on the unchanged code)
# --------------------------------------------------------------------------- #
def _scenario_fills():
    return [
        _fill(wallet="0xA", market="m1", usd=400.0, price=0.40),
        _fill(wallet="0xA", market="m1", usd=400.0, price=0.41),
        _fill(wallet="0xA", market="m1", usd=400.0, price=0.42),   # crosses: entry
        _fill(wallet="0xA", market="m1", usd=100.0, price=0.45),   # 1300 < 2400: no refire
        _fill(wallet="0xA", market="m2", usd=300.0, price=0.30),   # below threshold
        _fill(wallet="0xB", market="m3", direction="SELL", usd=1200.0, price=0.55),  # exit
        _fill(wallet="0xC", market="m4", usd=1100.0, price=0.35),                    # two-sided: arb
        _fill(wallet="0xC", market="m4", usd=200.0, price=0.65, outcome="No", outcome_index=1),
        _fill(wallet="0xD", market="m5", usd=1500.0, price=0.30, is_bot=1),          # bot: logged only
        _fill(wallet="0xE", market="m6", usd=1000.0, price=0.80),                    # price band: not delivered
        _fill(wallet="0xF", market="m7", usd=1200.0, price=0.40),                    # thin market: gated
    ]


def _run_scenario(dbs, paths, sends, monkeypatch):
    meta, shadow = dbs
    monkeypatch.setattr(swa, "_executable_snapshot", lambda *a, **k: dict(BOOK))
    fired = swa.check_and_fire(meta, shadow, _scenario_fills(),
                               _meta_provider({"m7": 10_000}), send=True, now=NOW)

    def norm(v):
        return round(v, 6) if isinstance(v, float) else v

    accum = [tuple(norm(v) for v in r.values()) for r in _rows(
        paths[0], "SELECT wallet, market, direction, outcome, fills_json, total_usd, "
                  "num_fills, first_seen, last_seen, alert_fired, fired_total, "
                  "arb_fired, arb_fired_total FROM smart_wallet_accum "
                  "ORDER BY wallet, market, outcome")]
    shadows = [tuple(norm(v) for v in r.values()) for r in _rows(
        paths[1], "SELECT wallet, market, direction, outcome, outcome_index, price_at_alert, "
                  "cumulative_usd, num_fills, alert_type, ts_alert, executable_ask "
                  "FROM smart_wallet_shadows ORDER BY id")]
    return {
        "fired": [(r["wallet"], r["market"], r["alert_type"], r["cumulative_usd"]) for r in fired],
        "dispatch": [(p, t) for p, _m, t in sends["dispatch"]],
        "dispatch_text": [m for _p, m, _t in sends["dispatch"]],
        "telegram": list(sends["telegram"]),
        "accum": accum,
        "shadows": shadows,
    }


# Captured by running _run_scenario against the code BEFORE task 1.13 (commit
# af07d6b): the alerts delivered, the accumulator rows and the shadow rows.
EXPECTED = {'accum': [['0xA',
            'm1',
            'BUY',
            'Yes',
            '[[1700000000, 400.0, 0.4], [1700000000, 400.0, 0.41], [1700000000, 400.0, 0.42], '
            '[1700000000, 100.0, 0.45]]',
            1300.0,
            4,
            1700000000,
            1700000000,
            1700000000,
            1200.0,
            0,
            0.0],
           ['0xA',
            'm2',
            'BUY',
            'Yes',
            '[[1700000000, 300.0, 0.3]]',
            300.0,
            1,
            1700000000,
            1700000000,
            0,
            0.0,
            0,
            0.0],
           ['0xB',
            'm3',
            'SELL',
            'Yes',
            '[[1700000000, 1200.0, 0.55]]',
            1200.0,
            1,
            1700000000,
            1700000000,
            1700000000,
            1200.0,
            0,
            0.0],
           ['0xC',
            'm4',
            'BUY',
            'No',
            '[[1700000000, 200.0, 0.65]]',
            200.0,
            1,
            1700000000,
            1700000000,
            0,
            0.0,
            0,
            0.0],
           ['0xC',
            'm4',
            'BUY',
            'Yes',
            '[[1700000000, 1100.0, 0.35]]',
            1100.0,
            1,
            1700000000,
            1700000000,
            0,
            0.0,
            1700000000,
            1100.0],
           ['0xD',
            'm5',
            'BUY',
            'Yes',
            '[[1700000000, 1500.0, 0.3]]',
            1500.0,
            1,
            1700000000,
            1700000000,
            1700000000,
            1500.0,
            0,
            0.0],
           ['0xE',
            'm6',
            'BUY',
            'Yes',
            '[[1700000000, 1000.0, 0.8]]',
            1000.0,
            1,
            1700000000,
            1700000000,
            1700000000,
            1000.0,
            0,
            0.0],
           ['0xF',
            'm7',
            'BUY',
            'Yes',
            '[[1700000000, 1200.0, 0.4]]',
            1200.0,
            1,
            1700000000,
            1700000000,
            0,
            0.0,
            0,
            0.0]],
 'dispatch': [['wallet_moves', 3]],
 'dispatch_text': ['🧠 ENTRY — Yes @ 42¢\n'
                   'Title m1\n'
                   '$1,200 in 3 fills · 0xA\n'
                   'polymarket.com/event/slug-m1'],
 'fired': [['0xA', 'm1', 'entry', 1200.0],
           ['0xB', 'm3', 'exit', 1200.0],
           ['0xD', 'm5', 'entry', 1500.0],
           ['0xE', 'm6', 'entry', 1000.0]],
 'shadows': [['0xA', 'm1', 'BUY', 'Yes', 0, 0.42, 1200.0, 3, 'entry', 1700000000, 0.41],
             ['0xB', 'm3', 'SELL', 'Yes', 0, 0.55, 1200.0, 1, 'exit', 1700000000, 0.41],
             ['0xC', 'm4', 'BUY', 'Yes', 0, 0.35, 1100.0, 1, 'arb', 1700000000, None],
             ['0xD', 'm5', 'BUY', 'Yes', 0, 0.3, 1500.0, 1, 'entry', 1700000000, 0.41],
             ['0xE', 'm6', 'BUY', 'Yes', 0, 0.8, 1000.0, 1, 'entry', 1700000000, 0.41]],
 'telegram': ['🧠 <b>Smart Wallet Exit</b> ⚠️\n'
              '\n'
              '⚠️ <b>0xB</b>\n'
              '━━━━━━━━━━━━━━━━━━━━\n'
              'Title m3\n'
              '🔗 https://polymarket.com/event/slug-m3\n'
              '\n'
              'Exited <b>$1,200</b> → <b>Sold YES @ ~55.0¢</b>  (1 fills)']}


def test_same_alerts_same_totals_same_rows(dbs, paths, sends, monkeypatch):
    snap = _run_scenario(dbs, paths, sends, monkeypatch)
    assert json.loads(json.dumps(snap)) == EXPECTED


# --------------------------------------------------------------------------- #
# 3. a midway failure keeps what was done; what it does with the exception is
#    unchanged
# --------------------------------------------------------------------------- #
def _trade(market, tx, usd=1100.0, price=0.40, wallet=W):
    return {"transactionHash": tx, "asset": f"tok-{market}", "proxyWallet": wallet,
            "conditionId": market, "side": "BUY", "size": usd / price, "price": price,
            "outcome": "Yes", "outcomeIndex": 0, "title": f"Title {market}",
            "timestamp": NOW}


def _gamma(*markets):
    return {m: {"volumeNum": 1_000_000, "last_price": 0.40, "question": f"Title {m}",
                "endDate": "2026-12-31", "slug": f"slug-{m}"} for m in markets}


def _five_fills():
    return [_fill(market=f"m{i}") for i in range(1, 6)]


def _fail_on_third_book(monkeypatch):
    calls = []

    def book(cid, outcome_index, direction):
        calls.append(cid)
        if len(calls) == 3:
            raise RuntimeError("book boom")
        return dict(BOOK)

    monkeypatch.setattr(swa, "_executable_snapshot", book)
    return calls


def test_midway_failure_still_propagates_out_of_check_and_fire(dbs, monkeypatch):
    meta, shadow = dbs
    calls = _fail_on_third_book(monkeypatch)
    with pytest.raises(RuntimeError, match="book boom"):
        swa.check_and_fire(meta, shadow, _five_fills(), _meta_provider(), send=False, now=NOW)
    assert calls == ["m1", "m2", "m3"]          # fills 4 and 5 are never reached


def test_midway_failure_keeps_the_fills_already_done(dbs, paths, monkeypatch, capsys):
    """Through scanner_hook, with meta_conn closed the way the fast poll closes
    it (no commit by the caller). Before 1.13 nothing of the cycle survived."""
    meta, _shadow = dbs
    _fail_on_third_book(monkeypatch)
    trades = [_trade(f"m{i}", f"0xtx{i}") for i in range(1, 6)]
    out = swa.scanner_hook(meta, trades, _gamma(*[f"m{i}" for i in range(1, 6)]), SMART)
    meta.close()
    # what scanner_hook does with the exception: stderr banner, returns [], no raise
    assert out == []
    err = capsys.readouterr().err
    assert "SMART_WALLET_ALERT scanner_hook FAILED" in err and "book boom" in err
    # visible from fresh connections
    accum = _rows(paths[0], "SELECT market, alert_fired FROM smart_wallet_accum ORDER BY market")
    assert [r["market"] for r in accum] == ["m1", "m2", "m3"]
    assert all(r["alert_fired"] > 0 for r in accum)
    shadows = _rows(paths[1], "SELECT market FROM smart_wallet_shadows ORDER BY id")
    assert [r["market"] for r in shadows] == ["m1", "m2"]


# --------------------------------------------------------------------------- #
# 4. late, not lost
# --------------------------------------------------------------------------- #
def _cycle(meta, shadow, trades, gamma):
    return swa.run_from_scan(meta, shadow, trades, gamma, SMART)


def test_row_over_threshold_but_not_marked_fires_on_the_next_delivery(dbs, paths, monkeypatch):
    """Failure after the accumulator commit and before _mark_fired commits."""
    meta, shadow = dbs
    monkeypatch.setattr(swa, "_executable_snapshot", lambda *a, **k: dict(BOOK))
    trades = [_trade("m1", "0xA", usd=700.0), _trade("m1", "0xB", usd=500.0)]   # 1200 in one fill
    gamma = _gamma("m1")

    real_gates = swa._gates_suppress
    monkeypatch.setattr(swa, "_gates_suppress",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gates boom")))
    with pytest.raises(RuntimeError, match="gates boom"):
        _cycle(meta, shadow, trades, gamma)
    row = _rows(paths[0], "SELECT total_usd, alert_fired FROM smart_wallet_accum")[0]
    assert row["total_usd"] == pytest.approx(1200.0) and row["alert_fired"] == 0
    assert _rows(paths[1], "SELECT * FROM smart_wallet_shadows") == []

    # next cycle: the same two trades again (duplicates only, nothing new counted)
    monkeypatch.setattr(swa, "_gates_suppress", real_gates)
    fired = _cycle(meta, shadow, [dict(t) for t in trades], gamma)
    assert [r["alert_type"] for r in fired] == ["entry"]
    assert _rows(paths[0], "SELECT total_usd FROM smart_wallet_accum")[0]["total_usd"] == pytest.approx(1200.0)
    assert len(_rows(paths[1], "SELECT * FROM smart_wallet_shadows")) == 1


def test_failure_after_mark_fired_is_not_sent_late_it_waits_for_the_refire(dbs, paths, monkeypatch):
    """Documents the other half (finding, not a fix): once the mark is committed
    the alert of a failed fill is gone; the next alert for the row is the refire
    at 2x. Today's whale-sweep path already behaves like this."""
    meta, shadow = dbs
    trades = [_trade("m1", "0xA", usd=1200.0)]
    gamma = _gamma("m1")

    def boom(*a, **k):
        raise RuntimeError("book boom")

    monkeypatch.setattr(swa, "_executable_snapshot", boom)
    with pytest.raises(RuntimeError, match="book boom"):
        _cycle(meta, shadow, trades, gamma)
    assert _rows(paths[0], "SELECT alert_fired FROM smart_wallet_accum")[0]["alert_fired"] > 0

    monkeypatch.setattr(swa, "_executable_snapshot", lambda *a, **k: dict(BOOK))
    assert _cycle(meta, shadow, [dict(t) for t in trades], gamma) == []
    assert _rows(paths[1], "SELECT * FROM smart_wallet_shadows") == []


# --------------------------------------------------------------------------- #
# 5. scanner_hook's shadow connection waits like the others
# --------------------------------------------------------------------------- #
def test_scanner_hook_opens_shadow_db_with_the_project_timeout(dbs, paths, monkeypatch):
    meta, _shadow = dbs
    seen = {}

    def spy(meta_conn, sconn, trades, gamma, smart):
        seen["busy_timeout"] = sconn.execute("PRAGMA busy_timeout").fetchone()[0]
        seen["journal"] = sconn.execute("PRAGMA journal_mode").fetchone()[0]
        seen["conn"] = sconn
        return []

    monkeypatch.setattr(swa, "run_from_scan", spy)
    assert swa.scanner_hook(meta, [_trade("m1", "0xA")], _gamma("m1"), SMART) == []
    assert seen["busy_timeout"] == 30000
    assert seen["journal"] == "wal"
    with pytest.raises(sqlite3.ProgrammingError):     # closed exactly as before
        seen["conn"].execute("SELECT 1")

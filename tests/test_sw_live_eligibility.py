"""Live eligibility flag on the records `check_and_fire` returns (task 1.3, part 1).

The alert path already decides, per fill, whether an alert is delivered: not a bot
wallet, price under the band (#54: entries at 60c or more lost money) and a wallet
whose closing-line value is not poor. The live router never consulted those
filters. `check_and_fire` now stamps every record it returns with
`live_eligible` (True only for an entry that passes all of them) and
`live_filter_reason` (None, or the FIRST failing reason in a fixed order), plus the
facts the router's shadow log needs. Which alerts are delivered is unchanged.

Real WAL databases under tmp_path, the real `check_and_fire`, no network.
"""
import json
import logging
import sqlite3

import pytest

from scripts import smart_wallet_alert as swa

W = "0xabc"
NOW = 1_700_000_000
BOOK = {"executable_ask": 0.41, "exec_best_ask": 0.40,
        "exec_fillable_usd": 500.0, "exec_spread": 0.02}


def _open(path):
    c = sqlite3.connect(str(path), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


@pytest.fixture()
def dbs(tmp_path, monkeypatch):
    meta_p, shadow_p = tmp_path / "whale_meta.db", tmp_path / "shadow_trades.db"
    monkeypatch.setattr(swa, "SHADOW_DB", shadow_p)
    monkeypatch.setattr(swa, "page_tier_for", lambda wallet, alert_type: 3)
    monkeypatch.setattr(swa, "_executable_snapshot", lambda *a, **k: dict(BOOK))
    meta, shadow = _open(meta_p), _open(shadow_p)
    swa.init_accum(meta)
    swa.init_shadows(shadow)
    yield meta, shadow, meta_p, shadow_p
    for c in (meta, shadow):
        try:
            c.close()
        except sqlite3.Error:
            pass


@pytest.fixture()
def sends(monkeypatch):
    rec = {"dispatch": [], "telegram": []}
    monkeypatch.setattr(swa, "dispatch",
                        lambda pipeline, msg, tier, **kw: rec["dispatch"].append((pipeline, msg, tier)))
    import scripts.alert_formatter as af
    monkeypatch.setattr(af, "send_telegram", lambda msg, *a, **k: rec["telegram"].append(msg))
    return rec


def _meta_provider(volume_by_market=None):
    vols = volume_by_market or {}

    def provider(cid):
        return {"volume": vols.get(cid, 1_000_000), "price": 0.40,
                "title": f"Title {cid}", "close_time": "2026-12-31",
                "slug": f"slug-{cid}"}
    return provider


def _fill(wallet=W, market="m1", direction="BUY", usd=1100.0, price=0.40,
          outcome="Yes", outcome_index=0, is_bot=0, **extra):
    d = dict(wallet=wallet, market=market, direction=direction, usd=usd,
             price=price, outcome=outcome, outcome_index=outcome_index,
             name=wallet, title=f"Title {market}", is_bot=is_bot)
    d.update(extra)
    return d


def _run(dbs, fills, send=False, volume_by_market=None):
    meta, shadow = dbs[0], dbs[1]
    return swa.check_and_fire(meta, shadow, fills, _meta_provider(volume_by_market),
                              send=send, now=NOW)


def _mute_wallet(shadow, wallet, n=4, clv=-0.10):
    """n resolved shadows with poor closing-line value: the CLV gate mutes the wallet."""
    for i in range(n):
        shadow.execute(
            "INSERT INTO smart_wallet_shadows (wallet, market, direction, alert_type, "
            "resolved, clv, price_at_alert, ts_alert) VALUES (?,?,?,?,?,?,?,?)",
            (wallet, f"old{i}", "BUY", "entry", 1, clv, 0.4, NOW - 1000 - i))
    shadow.commit()


# --------------------------------------------------------------------------- #
# the flag and the reason
# --------------------------------------------------------------------------- #
def test_clean_entry_is_eligible_with_no_reason(dbs):
    (r,) = _run(dbs, [_fill(price=0.40)])
    assert r["alert_type"] == "entry"
    assert r["live_eligible"] is True
    assert r["live_filter_reason"] is None


def test_entry_at_072_is_returned_but_not_eligible_price_band(dbs, sends):
    fired = _run(dbs, [_fill(price=0.72)], send=True)
    assert len(fired) == 1                       # still returned, as before
    r = fired[0]
    assert r["live_eligible"] is False
    assert r["live_filter_reason"] == "price_band"
    assert sends["dispatch"] == []               # and still not delivered, as before


def test_price_band_boundary(dbs):
    (lo,) = _run(dbs, [_fill(market="a", price=0.59)])
    (hi,) = _run(dbs, [_fill(market="b", price=0.60)])
    assert lo["live_eligible"] is True
    assert (hi["live_eligible"], hi["live_filter_reason"]) == (False, "price_band")


def test_bot_wallet_is_not_eligible(dbs):
    (r,) = _run(dbs, [_fill(is_bot=1)])
    assert (r["live_eligible"], r["live_filter_reason"]) == (False, "bot_wallet")
    assert r["is_bot"] is True


def test_clv_muted_wallet_is_not_eligible(dbs):
    _mute_wallet(dbs[1], W)
    (r,) = _run(dbs, [_fill(price=0.40)])
    assert (r["live_eligible"], r["live_filter_reason"]) == (False, "clv_muted")
    assert r["clv_muted"] is True


def test_exit_alert_is_not_eligible(dbs):
    (r,) = _run(dbs, [_fill(direction="SELL", price=0.55)])
    assert r["alert_type"] == "exit"
    assert (r["live_eligible"], r["live_filter_reason"]) == (False, "not_entry")


def test_refire_alert_is_not_eligible(dbs):
    fired = _run(dbs, [_fill(usd=1200.0), _fill(usd=1300.0, price=0.45)])
    assert [r["alert_type"] for r in fired] == ["entry", "refire"]
    assert fired[0]["live_eligible"] is True
    assert (fired[1]["live_eligible"], fired[1]["live_filter_reason"]) == (False, "not_entry")


def test_reason_order_when_several_fail(dbs):
    _mute_wallet(dbs[1], "0xmuted")
    _mute_wallet(dbs[1], "0xbotmuted")
    fired = _run(dbs, [
        # bot + price band + muted: bot_wallet comes first
        _fill(wallet="0xbotmuted", market="m1", is_bot=1, price=0.72),
        # price band + muted: price_band comes before clv_muted
        _fill(wallet="0xmuted", market="m2", price=0.72),
        # bot exit: not_entry comes before bot_wallet
        _fill(wallet="0xbot", market="m3", direction="SELL", is_bot=1),
    ])
    by_market = {r["market"]: r for r in fired}
    assert by_market["m1"]["live_filter_reason"] == "bot_wallet"
    assert by_market["m2"]["live_filter_reason"] == "price_band"
    assert by_market["m3"]["live_filter_reason"] == "not_entry"
    # the facts behind every verdict are still there
    assert by_market["m2"]["clv_muted"] is True


def test_error_in_the_clv_gate_fails_closed_and_logs_once(dbs, monkeypatch, caplog):
    def boom(shadow_conn, wallet):
        raise RuntimeError("clv boom")

    monkeypatch.setattr(swa, "_clv_gate_suppress", boom)
    with caplog.at_level(logging.WARNING, logger=swa.__name__):
        (r,) = _run(dbs, [_fill()], send=False)
    assert (r["live_eligible"], r["live_filter_reason"]) == (False, "eligibility_error")
    assert r["clv_muted"] is None                # unknown, not a made-up False
    warnings = [x for x in caplog.records if x.levelno == logging.WARNING]
    assert len(warnings) == 1 and "clv boom" in warnings[0].getMessage()


def test_error_reason_comes_last(dbs, monkeypatch):
    monkeypatch.setattr(swa, "_clv_gate_suppress",
                        lambda c, w: (_ for _ in ()).throw(RuntimeError("clv boom")))
    fired = _run(dbs, [_fill(market="m1", price=0.72), _fill(market="m2", is_bot=1)])
    by_market = {r["market"]: r["live_filter_reason"] for r in fired}
    assert by_market == {"m1": "price_band", "m2": "bot_wallet"}


def test_clv_gate_is_asked_once_per_record(dbs, monkeypatch, sends):
    calls = []
    real = swa._clv_gate_suppress

    def counting(shadow_conn, wallet):
        calls.append(wallet)
        return real(shadow_conn, wallet)

    monkeypatch.setattr(swa, "_clv_gate_suppress", counting)
    fired = _run(dbs, [_fill(market="m1"), _fill(wallet="0xb", market="m2")], send=True)
    assert len(fired) == 2
    assert calls == [W, "0xb"]


def test_a_failing_clv_gate_still_breaks_delivery_exactly_as_before(dbs, monkeypatch, sends):
    """With sending on, the delivery decision used to call the CLV gate with no
    guard, and an error there left the function. It still does."""
    monkeypatch.setattr(swa, "_clv_gate_suppress",
                        lambda c, w: (_ for _ in ()).throw(RuntimeError("clv boom")))
    with pytest.raises(RuntimeError, match="clv boom"):
        _run(dbs, [_fill()], send=True)


# --------------------------------------------------------------------------- #
# the facts the router's shadow log reads
# --------------------------------------------------------------------------- #
def test_record_carries_the_shadow_log_facts(dbs):
    (r,) = _run(dbs, [_fill(price=0.40, asset="tokA", market="mkt1")])
    assert r["price"] == 0.40
    assert r["wallet"] == W
    assert r["is_bot"] is False
    assert r["clv_muted"] is False
    assert r["alert_type"] == "entry"
    assert r["market"] == "mkt1"
    assert r["outcome_index"] == 0
    assert r["asset"] == "tokA"
    assert r["title"] == "Title mkt1"


def test_missing_asset_is_an_empty_string_not_a_made_up_token(dbs):
    (r,) = _run(dbs, [_fill()])
    assert r["asset"] == ""


def test_fills_from_trades_carries_the_trades_own_asset():
    trades = [{"transactionHash": "0xtx", "asset": "tokB", "proxyWallet": W,
               "conditionId": "m1", "side": "BUY", "size": 2750.0, "price": 0.4,
               "outcome": "No", "outcomeIndex": 1, "title": "Some market", "timestamp": NOW}]
    smart = {W: {"name": "x", "win_rate": 0.6, "net_pnl": 1, "closed_positions": 9,
                 "source_category": "sports", "is_bot": 0}}
    (f,) = swa.fills_from_trades(trades, smart)
    assert f["asset"] == "tokB"
    # a feed row with no asset gives an empty string
    t2 = dict(trades[0], conditionId="m2")
    t2.pop("asset")
    (f2,) = swa.fills_from_trades([t2], smart)
    assert f2["asset"] == ""


# --------------------------------------------------------------------------- #
# nothing else changed: the alerts sent and the accumulator rows of a mixed cycle
# (expectations captured by running this scenario on the code BEFORE task 1.3)
# --------------------------------------------------------------------------- #
def _scenario_fills():
    return [
        _fill(wallet="0xA", market="m1", usd=400.0, price=0.40),
        _fill(wallet="0xA", market="m1", usd=400.0, price=0.41),
        _fill(wallet="0xA", market="m1", usd=400.0, price=0.42),   # crosses: entry
        _fill(wallet="0xA", market="m1", usd=1300.0, price=0.45),  # 2500 >= 2400: refire
        _fill(wallet="0xB", market="m3", direction="SELL", usd=1200.0, price=0.55),  # exit
        _fill(wallet="0xC", market="m4", usd=1100.0, price=0.35),                    # two-sided: arb
        _fill(wallet="0xC", market="m4", usd=200.0, price=0.65, outcome="No", outcome_index=1),
        _fill(wallet="0xD", market="m5", usd=1500.0, price=0.30, is_bot=1),          # bot
        _fill(wallet="0xE", market="m6", usd=1000.0, price=0.72),                    # price band
        _fill(wallet="0xF", market="m7", usd=1200.0, price=0.40),                    # thin market
        _fill(wallet="0xG", market="m8", usd=1200.0, price=0.40),                    # clv muted
        _fill(wallet="0xH", market="m9", usd=1200.0, price=0.35),                    # clean entry
    ]


def _rows(path, sql):
    c = sqlite3.connect(str(path), timeout=5)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql)]
    finally:
        c.close()


def _snapshot(dbs, sends):
    meta, shadow, meta_p, shadow_p = dbs
    _mute_wallet(shadow, "0xG")
    fired = swa.check_and_fire(meta, shadow, _scenario_fills(),
                               _meta_provider({"m7": 10_000}), send=True, now=NOW)

    def norm(v):
        return round(v, 6) if isinstance(v, float) else v

    accum = [tuple(norm(v) for v in r.values()) for r in _rows(
        meta_p, "SELECT wallet, market, direction, outcome, fills_json, total_usd, "
                "num_fills, first_seen, last_seen, alert_fired, fired_total, "
                "arb_fired, arb_fired_total FROM smart_wallet_accum "
                "ORDER BY wallet, market, outcome")]
    shadows = [tuple(norm(v) for v in r.values()) for r in _rows(
        shadow_p, "SELECT wallet, market, direction, outcome, outcome_index, price_at_alert, "
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


EXPECTED = {'accum': [['0xA',
            'm1',
            'BUY',
            'Yes',
            '[[1700000000, 400.0, 0.4], [1700000000, 400.0, 0.41], [1700000000, 400.0, 0.42], '
            '[1700000000, 1300.0, 0.45]]',
            2500.0,
            4,
            1700000000,
            1700000000,
            1700000000,
            2500.0,
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
            '[[1700000000, 1000.0, 0.72]]',
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
            0.0],
           ['0xG',
            'm8',
            'BUY',
            'Yes',
            '[[1700000000, 1200.0, 0.4]]',
            1200.0,
            1,
            1700000000,
            1700000000,
            1700000000,
            1200.0,
            0,
            0.0],
           ['0xH',
            'm9',
            'BUY',
            'Yes',
            '[[1700000000, 1200.0, 0.35]]',
            1200.0,
            1,
            1700000000,
            1700000000,
            1700000000,
            1200.0,
            0,
            0.0]],
 'dispatch': [['wallet_moves', 3], ['wallet_moves', 3], ['wallet_moves', 3]],
 'dispatch_text': ['🧠 ENTRY — Yes @ 42¢\n'
                   'Title m1\n'
                   '$1,200 in 3 fills · 0xA\n'
                   'polymarket.com/event/slug-m1',
                   '🔁 ADD — Yes @ 45¢\n'
                   'Title m1\n'
                   '$2,500 in 4 fills · 0xA\n'
                   'polymarket.com/event/slug-m1',
                   '🧠 ENTRY — Yes @ 35¢\n'
                   'Title m9\n'
                   '$1,200 in 1 fills · 0xH\n'
                   'polymarket.com/event/slug-m9'],
 'fired': [['0xA', 'm1', 'entry', 1200.0],
           ['0xA', 'm1', 'refire', 2500.0],
           ['0xB', 'm3', 'exit', 1200.0],
           ['0xD', 'm5', 'entry', 1500.0],
           ['0xE', 'm6', 'entry', 1000.0],
           ['0xG', 'm8', 'entry', 1200.0],
           ['0xH', 'm9', 'entry', 1200.0]],
 'shadows': [['0xG', 'old0', 'BUY', None, None, 0.4, None, None, 'entry', 1699999000, None],
             ['0xG', 'old1', 'BUY', None, None, 0.4, None, None, 'entry', 1699998999, None],
             ['0xG', 'old2', 'BUY', None, None, 0.4, None, None, 'entry', 1699998998, None],
             ['0xG', 'old3', 'BUY', None, None, 0.4, None, None, 'entry', 1699998997, None],
             ['0xA', 'm1', 'BUY', 'Yes', 0, 0.42, 1200.0, 3, 'entry', 1700000000, 0.41],
             ['0xA', 'm1', 'BUY', 'Yes', 0, 0.45, 2500.0, 4, 'refire', 1700000000, 0.41],
             ['0xB', 'm3', 'SELL', 'Yes', 0, 0.55, 1200.0, 1, 'exit', 1700000000, 0.41],
             ['0xC', 'm4', 'BUY', 'Yes', 0, 0.35, 1100.0, 1, 'arb', 1700000000, None],
             ['0xD', 'm5', 'BUY', 'Yes', 0, 0.3, 1500.0, 1, 'entry', 1700000000, 0.41],
             ['0xE', 'm6', 'BUY', 'Yes', 0, 0.72, 1000.0, 1, 'entry', 1700000000, 0.41],
             ['0xG', 'm8', 'BUY', 'Yes', 0, 0.4, 1200.0, 1, 'entry', 1700000000, 0.41],
             ['0xH', 'm9', 'BUY', 'Yes', 0, 0.35, 1200.0, 1, 'entry', 1700000000, 0.41]],
 'telegram': ['🧠 <b>Smart Wallet Exit</b> ⚠️\n'
              '\n'
              '⚠️ <b>0xB</b>\n'
              '━━━━━━━━━━━━━━━━━━━━\n'
              'Title m3\n'
              '🔗 https://polymarket.com/event/slug-m3\n'
              '\n'
              'Exited <b>$1,200</b> → <b>Sold YES @ ~55.0¢</b>  (1 fills)']}


def test_alerts_and_rows_of_a_mixed_cycle_are_unchanged(dbs, sends):
    snap = json.loads(json.dumps(_snapshot(dbs, sends)))
    assert snap == EXPECTED

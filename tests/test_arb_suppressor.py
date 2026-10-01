"""Two-sided merge-arb suppressor tests (2026-10-01).

Trace 2026-10-01: 31/48 alerts in 12h came from wallets buying BOTH outcomes
of one market (complete-set merge arb: buy YES+NO when sum < $1.00, MERGE
sets back to USDC). These tests pin the suppressor: two-sided cycles
shadow-log as alert_type='arb' and never fire/deliver/route, one-sided
cycles are untouched, and 'arb' rows stay out of wallet CLV stats.
"""
import sqlite3
import pytest

from scripts import smart_wallet_alert as swa

T = swa.THRESHOLD
CROSS = T * 0.4  # 3 of these (1.2T) cross the threshold on the third fill


@pytest.fixture()
def conns():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    swa.init_accum(c)
    swa.init_shadows(c)
    return c, c


def _meta_stub(volume=1_000_000, price=0.40):
    def provider(cid):
        return {"volume": volume, "price": price, "title": "Test Market",
                "close_time": "2026-12-31"}
    return provider


def _fill(wallet="W1", market="m1", direction="BUY", usd=CROSS, price=0.40,
          outcome="Yes", outcome_index=0, name="W1", title="Test Market",
          set_ops=None):
    d = dict(wallet=wallet, market=market, direction=direction, usd=usd,
             price=price, outcome=outcome, outcome_index=outcome_index,
             name=name, title=title)
    if set_ops is not None:
        d["set_ops"] = set_ops
    return d


def _shadows(conn, alert_type=None):
    if alert_type:
        return conn.execute(
            "SELECT * FROM smart_wallet_shadows WHERE alert_type=?",
            (alert_type,)).fetchall()
    return conn.execute("SELECT * FROM smart_wallet_shadows").fetchall()


def test_two_sided_cycle_suppressed(conns):
    meta, shadow = conns
    fills = ([_fill(outcome="Yes", outcome_index=0, price=0.40)] * 3
             + [_fill(outcome="No", outcome_index=1, price=0.56)] * 3)
    fired = swa.check_and_fire(meta, shadow, fills, _meta_stub(), now=1000)
    assert fired == []
    arb = _shadows(shadow, "arb")
    assert len(arb) == 2  # both legs logged as counterfactuals
    # accumulator must NOT be marked fired -> a later one-sided cycle fires
    marked = meta.execute(
        "SELECT COUNT(*) FROM smart_wallet_accum WHERE alert_fired > 0"
    ).fetchone()[0]
    assert marked == 0


def test_one_sided_still_fires(conns):
    meta, shadow = conns
    fired = swa.check_and_fire(meta, shadow, [_fill()] * 3, _meta_stub(), now=1000)
    assert len(fired) == 1 and fired[0]["alert_type"] == "entry"
    assert _shadows(shadow, "arb") == []


def test_cross_cycle_arb_suppressed(conns):
    meta, shadow = conns
    # cycle 1: one-sided Yes accumulation (fires normally)
    fired1 = swa.check_and_fire(meta, shadow, [_fill()] * 3, _meta_stub(), now=1000)
    assert len(fired1) == 1
    # cycle 2: the OTHER outcome crosses while Yes is still fresh in the window
    fired2 = swa.check_and_fire(
        meta, shadow,
        [_fill(outcome="No", outcome_index=1, price=0.56)] * 3,
        _meta_stub(), now=2000)
    assert fired2 == []
    assert len(_shadows(shadow, "arb")) == 1


def test_arb_expires_then_fires(conns):
    meta, shadow = conns
    swa.check_and_fire(meta, shadow, [_fill()] * 3, _meta_stub(), now=1000)
    # beyond the 4h window the Yes accum row is stale -> No may fire
    late = 1000 + swa.ACCUM_WINDOW + 60
    fired = swa.check_and_fire(
        meta, shadow,
        [_fill(outcome="No", outcome_index=1, price=0.56)] * 3,
        _meta_stub(), now=late)
    assert len(fired) == 1 and fired[0]["alert_type"] == "entry"


def test_set_ops_suppresses_single_outcome(conns):
    meta, shadow = conns
    fired = swa.check_and_fire(
        meta, shadow, [_fill(set_ops=2)] * 3, _meta_stub(), now=1000)
    assert fired == []
    assert len(_shadows(shadow, "arb")) == 1


def test_kill_switch_restores_firing(conns, monkeypatch):
    monkeypatch.setattr(swa, "ARB_SUPPRESS_ENABLED", False)
    meta, shadow = conns
    fills = [_fill()] * 3 + [_fill(outcome="No", outcome_index=1, price=0.56)] * 3
    fired = swa.check_and_fire(meta, shadow, fills, _meta_stub(), now=1000)
    assert len(fired) == 2
    assert _shadows(shadow, "arb") == []


def test_unknown_outcome_fails_open(conns):
    meta, shadow = conns
    # an unlabeled accum row must NOT flag a labeled outcome (fail-open rule)
    swa._accumulate(meta, "W1", "m1", "BUY", "", T * 0.5, 0.40, 1000)
    fired = swa.check_and_fire(meta, shadow, [_fill()] * 3, _meta_stub(), now=1100)
    assert len(fired) == 1


def test_clv_gate_ignores_arb_rows(conns):
    meta, shadow = conns
    for _ in range(swa.CLV_GATE_MIN_SHADOWS):
        shadow.execute(
            "INSERT INTO smart_wallet_shadows (wallet, market, title, direction, "
            "outcome, outcome_index, price_at_alert, cumulative_usd, num_fills, "
            "alert_type, ts_alert, resolved, clv) "
            "VALUES ('W1','m1','t','BUY','Yes',0,0.4,100,1,?,1000,1,-0.5)",
            ("arb",))
    # arb-only losing history must NOT engage the delivery gate
    assert swa._clv_gate_suppress(shadow, "W1") is False
    # the same history as real entries -> gate engages
    shadow.execute(
        "UPDATE smart_wallet_shadows SET alert_type='entry' WHERE alert_type='arb'")
    assert swa._clv_gate_suppress(shadow, "W1") is True


def test_fills_from_trades_counts_set_ops():
    trades = [
        {"proxyWallet": "W1", "conditionId": "m1", "side": "BUY", "size": 100,
         "price": 0.4, "outcomeIndex": 0, "outcome": "Yes", "title": "T"},
        {"proxyWallet": "W1", "conditionId": "m1", "side": "", "type": "MERGE",
         "size": 50, "price": 0},
        {"proxyWallet": "W1", "conditionId": "m1", "side": "BUY", "size": 100,
         "price": 0.5, "outcomeIndex": 1, "outcome": "No", "title": "T"},
    ]
    fills = swa.fills_from_trades(trades, {"W1": {"name": "W"}})
    assert len(fills) == 2
    by_outcome = {f["outcome"]: f for f in fills}
    assert by_outcome["Yes"]["usd"] == pytest.approx(40.0)
    assert by_outcome["Yes"]["set_ops"] == 1
    assert by_outcome["No"]["set_ops"] == 1
    # REDEEM is routine settlement — never counted as a set-op
    trades.append({"proxyWallet": "W1", "conditionId": "m1", "side": "",
                   "type": "REDEEM", "size": 10, "price": 0})
    fills = swa.fills_from_trades(trades, {"W1": {"name": "W"}})
    assert all(f["set_ops"] == 1 for f in fills)


def test_arb_dedup_no_per_sweep_spam(conns):
    meta, shadow = conns
    fills = [_fill()] * 3 + [_fill(outcome="No", outcome_index=1, price=0.56)] * 3
    swa.check_and_fire(meta, shadow, fills, _meta_stub(), now=1000)
    assert len(_shadows(shadow, "arb")) == 2
    # next sweep, small top-up, no doubling: suppressed but NOT re-logged
    swa.check_and_fire(meta, shadow, [_fill(usd=T * 0.1)] * 2, _meta_stub(), now=1100)
    assert len(_shadows(shadow, "arb")) == 2
    # cycle doubles -> re-log (mirrors refire semantics)
    swa.check_and_fire(
        meta, shadow,
        [_fill(usd=T * 1.5),
         _fill(outcome="No", outcome_index=1, price=0.56, usd=T * 1.5)],
        _meta_stub(), now=1200)
    assert len(_shadows(shadow, "arb")) == 4
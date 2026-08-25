# Tests for signals/whale_follower.py — paper-only informed-flow follower.
#
# Covers: INFO hard gates (G1-G5), archetype classification, exact Kalshi fee,
# entry pricing with slippage, and the end-to-end entry pass against a fake
# alerts DB (no network: pm_lookup monkeypatched).
import json
import time

import pytest

from signals.whale_follower import (
    ARCH_PRIORS,
    INFO_THRESHOLD,
    classify_archetype,
    ensure_table,
    info_score,
    kalshi_fee_pc,
    open_new_follows,
    _entry_price,
)
from signals.whale_outcomes import get_meta_db
from signals.whale_scanner import get_db as get_alerts_db, log_alert


@pytest.fixture
def meta(tmp_path):
    conn = get_meta_db(tmp_path / "meta_test.db")
    ensure_table(conn)
    yield conn
    conn.close()


@pytest.fixture
def alerts_db(tmp_path):
    path = tmp_path / "alerts_test.db"
    conn = get_alerts_db(path)
    yield conn, path
    conn.close()


# ── Fee model ────────────────────────────────────────────────────────────────

def test_kalshi_fee_maximal_at_mid():
    assert kalshi_fee_pc(0.5) == pytest.approx(0.0175)
    assert kalshi_fee_pc(0.85) == pytest.approx(0.0089, abs=1e-4)
    assert kalshi_fee_pc(0.5) > kalshi_fee_pc(0.1) > kalshi_fee_pc(0.01)


# ── Archetype ────────────────────────────────────────────────────────────────

def test_classify_archetype_kalshi_prefixes():
    assert classify_archetype("kalshi", "KXHIGHTDC-26APR10-B55") == "weather"
    assert classify_archetype("kalshi", "KXMLBHR-26JUN11XX-Y") == "sports"
    assert classify_archetype("kalshi", "KXRATECUT-26SEP") == "econ"
    assert classify_archetype("kalshi", "SENATEGA-26") == "policy"
    assert classify_archetype("kalshi", "KXBTCD-26JUN12") == "crypto"
    assert classify_archetype("kalshi", "KXSOMETHINGELSE-26") == "other"


# ── INFO gates ───────────────────────────────────────────────────────────────

# A whale scenario deep enough to absorb the $1000 paper size: $25k of flow
# against ~$91k of standing depth. At SIZE_USD=1000 the slippage model makes
# thin books fail the edge gate by design — that honesty is load-bearing.
GOOD_KALSHI = {
    "flow_dollars": 25000.0, "flow_yes": 24000.0, "flow_no": 1000.0,
    "best_bid": 0.44, "best_ask": 0.46, "bid_depth": 3000.0,
    "ask_depth": 200000.0, "open_interest": 8000.0, "last_yes_price": 0.45,
    "title": "Will it rain?",
}

def test_info_gate_dollar_floor(meta):
    p = {**GOOD_KALSHI, "flow_dollars": 100.0}
    score, comps = info_score(meta, "kalshi", "KXHIGHTDC-26APR10-B55", "vol_spike_500", p)
    assert score == 0.0 and comps["gate_fail"] == "G1_dollar_floor"

def test_info_gate_near_settled(meta):
    p = {**GOOD_KALSHI, "best_bid": 0.97, "best_ask": 0.99, "last_yes_price": 0.98}
    score, comps = info_score(meta, "kalshi", "KXHIGHTDC-26APR10-B55", "vol_spike_500", p)
    assert score == 0.0 and comps["gate_fail"] == "G2_near_settled"

def test_info_gate_first_sight(meta):
    score, comps = info_score(meta, "kalshi", "KXHIGHTDC-26APR10-B55",
                              "vol_spike_500,first_sight", GOOD_KALSHI)
    assert score == 0.0 and comps["gate_fail"] == "G5_first_sight"

def test_info_gate_unexecutable_spread(meta):
    p = {**GOOD_KALSHI, "best_bid": 0.30, "best_ask": 0.46}
    score, comps = info_score(meta, "kalshi", "KXHIGHTDC-26APR10-B55", "vol_spike_500", p)
    assert score == 0.0 and comps["gate_fail"] == "G4_unexecutable"

def test_info_gate_game_day_sports(meta, monkeypatch):
    from datetime import date
    import signals.whale_follower as wf
    monkeypatch.setattr(wf, "_today_et", lambda: date(2026, 6, 11))
    score, comps = info_score(meta, "kalshi", "KXMLBHR-26JUN11XX-Y",
                              "vol_spike_500", GOOD_KALSHI)
    assert score == 0.0 and comps["gate_fail"] == "G3_reactive_sports"

def test_info_passes_thin_market_whale(meta):
    """$25k one-sided sweep vs ~$91k standing depth, weather: high INFO."""
    score, comps = info_score(meta, "kalshi", "KXHIGHTDC-26APR10-B55",
                              "vol_spike_5000,taker_YES_96%,level_jump_bid_2000",
                              GOOD_KALSHI)
    assert score >= INFO_THRESHOLD
    assert comps["archetype"] == "weather"
    assert comps["f_size"] >= 0.8   # $5k vs ~$24k effective liquidity

def test_info_big_flow_in_huge_market_scores_low(meta):
    """$96k into a 2.2M-depth market: f_size collapses, INFO below threshold."""
    p = {**GOOD_KALSHI, "flow_dollars": 96000.0, "open_interest": 4_000_000.0,
         "bid_depth": 2_242_630.0, "ask_depth": 977_513.0}
    score, _ = info_score(meta, "kalshi", "KXSOMETHINGELSE-26", "vol_spike_96000", p)
    assert score < INFO_THRESHOLD


# ── Entry pricing ────────────────────────────────────────────────────────────

def test_entry_price_pays_ask_plus_slippage():
    px = _entry_price(1, bid=0.44, ask=0.46, last=0.45, ask_depth=2500.0, mid=0.45)
    assert px is not None and px >= 0.46          # never better than the ask

def test_entry_price_short_hits_bid():
    px = _entry_price(-1, bid=0.44, ask=0.46, last=0.45, ask_depth=2500.0, mid=0.45)
    assert px is not None and px <= 0.44


# ── End-to-end entry pass ────────────────────────────────────────────────────

def _mk_kalshi_alert(conn, market, payload, score=9, reasons="vol_spike_5000,taker_YES_96%"):
    alert = {"platform": "kalshi", "market": market, "severity": "CRITICAL",
             "score": score, "reasons": reasons, **payload}
    log_alert(conn, alert)
    conn.commit()

def test_open_new_follows_enters_qualifying_alert(meta, alerts_db, monkeypatch):
    import signals.whale_follower as wf
    monkeypatch.setattr(wf, "pm_lookup", lambda slugs: {})
    conn, path = alerts_db
    _mk_kalshi_alert(conn, "KXHIGHTDC-26APR10-B55", GOOD_KALSHI)
    stats = open_new_follows(meta, alerts_db_path=path)
    assert stats["entered"] == 1
    row = meta.execute("SELECT * FROM whale_follows").fetchone()
    assert row["direction"] == 1                  # taker_YES flow
    assert row["entry_px"] >= 0.46                # paid the ask
    assert row["archetype"] == "weather"
    assert json.loads(row["info_components"])["f_size"] >= 0.8
    # idempotent: rerun does not double-enter
    stats2 = open_new_follows(meta, alerts_db_path=path)
    assert stats2["entered"] == 0

def test_open_new_follows_skips_minnow(meta, alerts_db, monkeypatch):
    import signals.whale_follower as wf
    monkeypatch.setattr(wf, "pm_lookup", lambda slugs: {})
    conn, path = alerts_db
    _mk_kalshi_alert(conn, "KXNHLPTS-27JAN01XX-Y",
                     {**GOOD_KALSHI, "flow_dollars": 88.0})
    stats = open_new_follows(meta, alerts_db_path=path)
    assert stats["entered"] == 0
    assert stats["skip_info"] == 1

def test_cursor_advances_past_stale_rows(meta, alerts_db, monkeypatch):
    """Regression (2026-06-12): with nothing entered, the scan cursor must
    still advance past stale alerts, or the window pins to the oldest batch
    forever and fresh alerts are never reached."""
    import signals.whale_follower as wf
    monkeypatch.setattr(wf, "pm_lookup", lambda slugs: {})
    conn, path = alerts_db
    # a stale alert (2h old) that would otherwise occupy the window
    old = {"platform": "kalshi", "market": "KXHIGHTDC-26APR10-B55",
           "severity": "CRITICAL", "score": 9,
           "reasons": "vol_spike_5000,taker_YES_96%", **GOOD_KALSHI}
    conn.execute(
        "INSERT INTO whale_alerts (ts, platform, market, severity, score,"
        " reasons, payload) VALUES (?,?,?,?,?,?,?)",
        (time.time() - 7200, "kalshi", old["market"], "CRITICAL", 9,
         old["reasons"], json.dumps(old)))
    conn.commit()
    stats1 = open_new_follows(meta, alerts_db_path=path)
    assert stats1["entered"] == 0
    cursor = int(meta.execute(
        "SELECT value FROM follower_kv WHERE key='last_alert_id'").fetchone()[0])
    assert cursor >= 1            # advanced past the stale row despite no entry
    # a fresh qualifying alert arrives: next pass must reach and enter it
    _mk_kalshi_alert(conn, "KXHIGHTDC-26APR11-B57", GOOD_KALSHI)
    stats2 = open_new_follows(meta, alerts_db_path=path)
    assert stats2["entered"] == 1

def test_bump_funnel_accumulates_daily(meta):
    from signals.whale_follower import bump_funnel
    bump_funnel(meta, {"scanned": 10, "entered": 1, "skip_info": 8, "skip_dir": 1})
    bump_funnel(meta, {"scanned": 5, "skip_info": 5})
    row = meta.execute(
        "SELECT value FROM follower_kv WHERE key LIKE 'funnel:%'").fetchone()
    f = json.loads(row[0])
    assert f["scanned"] == 15 and f["entered"] == 1 and f["skip_info"] == 13

def test_classify_pm_hurricanes_is_sports_not_weather():
    """Regression: 'Carolina Hurricanes' must not match the weather keyword."""
    assert classify_archetype("polymarket", "nhl-car-las-2026-06-14",
                              "Hurricanes vs Golden Knights") == "sports"
    assert classify_archetype("polymarket", "nyc-rain-jun-14",
                              "Will it rain in NYC on June 14?") == "weather"

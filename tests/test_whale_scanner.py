# Tests for signals/whale_scanner.py v3 — sweep + book whale detection.
#
# Covers: Kalshi orderbook_fp parsing (yes/no -> bid/ask complement),
# sweep scoring (relative+absolute flow gates so busy markets don't flood),
# book scoring (steady-state walls must NOT alert), market_state upserts,
# kv cursor, alert dedup, snapshot persistence and pruning.
import json
import time

import pytest

from signals.whale_scanner import (
    ALERT_MIN_SCORE,
    aggregate_trades,
    book_summary,
    build_watchlist,
    flow_direction,
    get_db,
    kv_get,
    kv_set,
    load_prev_snapshot,
    load_state,
    log_alert,
    parse_kalshi_book,
    prune_snapshots,
    recently_alerted,
    save_snapshot,
    score_change,
    sweep_score,
    upsert_state,
)


@pytest.fixture
def db(tmp_path):
    conn = get_db(tmp_path / "whale_test.db")
    yield conn
    conn.close()


# ── Parsing ─────────────────────────────────────────────────────────────────

def test_parse_kalshi_book_complement():
    """no_dollars are resting NO bids == YES asks at (1 - p)."""
    fp = {
        "yes_dollars": [["0.3000", "150.00"], ["0.2500", "50.00"]],
        "no_dollars": [["0.6000", "200.00"], ["0.5500", "75.00"]],
    }
    bids, asks = parse_kalshi_book(fp)
    assert bids == [(0.30, 150.0), (0.25, 50.0)]            # best bid first
    assert asks == [(0.40, 200.0), (0.45, 75.0)]            # 1 - 0.60 = 0.40, best ask first

def test_parse_kalshi_book_empty():
    assert parse_kalshi_book({}) == ([], [])
    assert parse_kalshi_book({"yes_dollars": None, "no_dollars": None}) == ([], [])


def test_book_summary_fields():
    s = book_summary([(0.30, 150.0)], [(0.40, 200.0)], oi=181.0, volume=182.0)
    assert s["bid_depth"] == 150.0
    assert s["ask_depth"] == 200.0
    assert s["best_bid"] == 0.30
    assert s["best_ask"] == 0.40
    assert s["max_level"] == 200.0
    assert s["oi"] == 181.0
    assert s["levels"]["B:0.3000"] == 150.0
    assert s["levels"]["A:0.4000"] == 200.0


# ── Sweep scoring (executed flow) ───────────────────────────────────────────

def test_sweep_vol_spike_thin_market():
    """600 contracts into a previously quiet market: spike + thin bonus."""
    score, reasons = sweep_score({"oi": 800.0, "volume": 1000.0},
                                 {"oi": 800.0, "volume": 1600.0})
    assert any("vol_spike" in r for r in reasons)
    assert any("thin_flow" in r for r in reasons)
    assert score >= 5

def test_sweep_busy_market_needs_relative_jump():
    """600 contracts in 5 min on a 50k-volume in-game market is routine."""
    score, reasons = sweep_score({"oi": 30000.0, "volume": 50000.0},
                                 {"oi": 30000.0, "volume": 50600.0})
    assert score < ALERT_MIN_SCORE

def test_sweep_busy_market_whale_burst_alerts():
    """A 20k burst on a 50k market clears the 30% relative gate."""
    score, reasons = sweep_score({"oi": 30000.0, "volume": 50000.0},
                                 {"oi": 30000.0, "volume": 70000.0})
    assert any("vol_spike" in r for r in reasons)

def test_sweep_oi_spike():
    score, reasons = sweep_score({"oi": 1000.0, "volume": 5000.0},
                                 {"oi": 1700.0, "volume": 5000.0})
    assert any("oi_spike" in r for r in reasons)

def test_sweep_unseen_market_measures_from_zero():
    """A market trading 800 contracts out of nowhere is the signal."""
    score, reasons = sweep_score(None, {"oi": 600.0, "volume": 800.0})
    assert score >= ALERT_MIN_SCORE

def test_sweep_no_change_scores_zero():
    score, _ = sweep_score({"oi": 500.0, "volume": 900.0},
                           {"oi": 500.0, "volume": 900.0})
    assert score == 0

def test_sweep_custom_labels():
    _, reasons = sweep_score({"oi": 100.0, "volume": 100.0},
                             {"oi": 100.0, "volume": 900.0},
                             vol_label="vol$", oi_label="liq$")
    assert any(r.startswith("vol$_spike") for r in reasons)


# ── Book scoring: change alerts, steady state does not ──────────────────────

def _summary(bids, asks, oi=None, volume=None):
    return book_summary(bids, asks, oi=oi, volume=volume)

def test_first_seen_book_is_baseline_not_alert():
    cur = _summary([(0.50, 5000.0)], [(0.55, 5000.0)])
    score, reasons = score_change(None, cur)
    assert score < ALERT_MIN_SCORE

def test_steady_state_wall_does_not_alert():
    """A persistent 5,000-contract wall is MM quoting, not a whale entry."""
    prev = _summary([(0.50, 5000.0)], [(0.55, 4000.0)])
    cur = _summary([(0.50, 5000.0)], [(0.55, 4000.0)])
    score, reasons = score_change(prev, cur)
    assert score == 0

def test_level_jump_alerts():
    """<100 -> >=1,000 at a level is the whale signature."""
    prev = _summary([(0.50, 50.0)], [(0.55, 100.0)])
    cur = _summary([(0.50, 1500.0)], [(0.55, 100.0)])
    score, reasons = score_change(prev, cur)
    assert score >= 4
    assert any("level_jump" in r for r in reasons)

def test_new_level_jump_alerts():
    """A brand-new >=1,000 level (absent before) also counts."""
    prev = _summary([(0.50, 50.0)], [(0.55, 100.0)])
    cur = _summary([(0.50, 50.0), (0.45, 2000.0)], [(0.55, 100.0)])
    score, reasons = score_change(prev, cur)
    assert score >= 4

def test_depth_surge():
    prev = _summary([(0.50, 300.0)], [(0.55, 300.0)])
    cur = _summary([(0.50, 480.0), (0.49, 480.0)], [(0.55, 300.0)])
    score, reasons = score_change(prev, cur)
    assert any("depth_surge" in r for r in reasons)

def test_imbalance_flip():
    prev = _summary([(0.50, 400.0)], [(0.55, 400.0)])
    cur = _summary([(0.50, 400.0), (0.49, 1800.0)], [(0.55, 400.0)])
    score, reasons = score_change(prev, cur)
    assert any("imbalance_flip" in r for r in reasons)

def test_persistent_imbalance_no_flip():
    prev = _summary([(0.50, 3000.0)], [(0.55, 100.0)])
    cur = _summary([(0.50, 3100.0)], [(0.55, 100.0)])
    score, reasons = score_change(prev, cur)
    assert not any("imbalance_flip" in r for r in reasons)

def test_spread_collapse():
    prev = _summary([(0.40, 200.0)], [(0.47, 200.0)])
    cur = _summary([(0.43, 200.0)], [(0.44, 200.0)])
    score, reasons = score_change(prev, cur)
    assert any("spread_collapse" in r for r in reasons)

def test_book_score_ignores_oi_and_volume():
    """Executed flow belongs to the sweep layer — no double counting."""
    prev = _summary([(0.50, 200.0)], [(0.55, 200.0)], oi=100.0, volume=100.0)
    cur = _summary([(0.50, 200.0)], [(0.55, 200.0)], oi=900.0, volume=900.0)
    score, _ = score_change(prev, cur)
    assert score == 0


# ── Persistence ─────────────────────────────────────────────────────────────

def test_snapshot_roundtrip(db):
    cur = _summary([(0.50, 150.0)], [(0.55, 200.0)], oi=181.0)
    save_snapshot(db, "kalshi", "KXTEST-26JUN12-T97", cur)
    prev = load_prev_snapshot(db, "KXTEST-26JUN12-T97")
    assert prev is not None
    assert prev["bid_depth"] == 150.0
    assert prev["levels"]["B:0.5000"] == 150.0
    assert prev["oi"] == 181.0

def test_load_prev_returns_latest(db):
    save_snapshot(db, "kalshi", "M1", _summary([(0.50, 100.0)], []), ts=time.time() - 60)
    save_snapshot(db, "kalshi", "M1", _summary([(0.50, 999.0)], []), ts=time.time())
    assert load_prev_snapshot(db, "M1")["bid_depth"] == 999.0

def test_alert_logged_and_dedup(db):
    alert = {"platform": "kalshi", "market": "M1", "score": 6,
             "severity": "HIGH", "reasons": "level_jump_1500"}
    log_alert(db, alert)
    db.commit()
    row = db.execute("SELECT * FROM whale_alerts").fetchone()
    assert row["market"] == "M1"
    assert json.loads(row["payload"])["reasons"] == "level_jump_1500"
    assert "M1" in recently_alerted(db)
    assert "M2" not in recently_alerted(db)

def test_market_state_upsert_and_load(db):
    upsert_state(db, "kalshi", [("M1", 100.0, 200.0, "Title one")])
    upsert_state(db, "kalshi", [("M1", 150.0, 900.0, "Title one")])
    db.commit()
    state = load_state(db, "kalshi")
    assert state["M1"] == {"oi": 150.0, "volume": 900.0}
    assert load_state(db, "polymarket") == {}

def test_kv_roundtrip(db):
    assert kv_get(db, "cursor", "0") == "0"
    kv_set(db, "cursor", "42")
    db.commit()
    assert kv_get(db, "cursor") == "42"
    kv_set(db, "cursor", "43")
    assert kv_get(db, "cursor") == "43"

def test_build_watchlist_weather_and_thin(db):
    # seed the weather kv cache so the test stays offline
    kv_set(db, "weather_watchlist", json.dumps(["KXHIGHNY-26JUN12-T97"]))
    kv_set(db, "weather_watchlist_ts", str(time.time()))
    upsert_state(db, "kalshi", [
        ("KXNBA-26JUN12-LAL", 500.0, 10.0, ""),      # thin-active: in
        ("KXNBA-26JUN12-BOS", 50000.0, 10.0, ""),    # too big: out
        ("KXNBA-26JUN12-NYK", 50.0, 10.0, ""),       # below OI floor: out
    ])
    db.commit()
    watch = build_watchlist(db)
    assert "KXHIGHNY-26JUN12-T97" in watch
    assert "KXNBA-26JUN12-LAL" in watch
    assert "KXNBA-26JUN12-BOS" not in watch
    assert "KXNBA-26JUN12-NYK" not in watch
    assert watch == sorted(watch)


def test_aggregate_trades_direction_and_dollars():
    trades = [
        {"ticker": "M1", "count_fp": "100.00", "taker_side": "yes",
         "yes_price_dollars": "0.7000", "no_price_dollars": "0.3000"},
        {"ticker": "M1", "count_fp": "800.00", "taker_side": "yes",
         "yes_price_dollars": "0.7100", "no_price_dollars": "0.2900"},
        {"ticker": "M1", "count_fp": "50.00", "taker_side": "no",
         "yes_price_dollars": "0.7100", "no_price_dollars": "0.2900"},
        {"ticker": "M2", "count_fp": "10.00", "taker_side": "no",
         "yes_price_dollars": "0.1000", "no_price_dollars": "0.9000"},
    ]
    agg = aggregate_trades(trades)
    assert agg["M1"]["vol"] == 950.0
    assert agg["M1"]["yes_vol"] == 900.0
    assert agg["M1"]["no_vol"] == 50.0
    assert agg["M1"]["dollars"] == pytest.approx(100 * 0.70 + 800 * 0.71 + 50 * 0.29)
    assert agg["M2"]["vol"] == 10.0


def test_flow_direction():
    assert flow_direction({"yes_vol": 900.0, "no_vol": 50.0}) == ("YES", 95)
    assert flow_direction({"yes_vol": 100.0, "no_vol": 300.0}) == ("NO", 75)
    assert flow_direction({"yes_vol": 100.0, "no_vol": 90.0}) == (None, 0)
    assert flow_direction({"yes_vol": 0.0, "no_vol": 0.0}) == (None, 0)


def test_level_jump_reason_includes_side():
    prev = _summary([(0.50, 50.0)], [(0.55, 100.0)])
    cur = _summary([(0.50, 1500.0)], [(0.55, 100.0)])
    _, reasons = score_change(prev, cur)
    assert any("level_jump_bid" in r for r in reasons)
    cur2 = _summary([(0.50, 50.0)], [(0.55, 100.0), (0.60, 1500.0)])
    _, reasons2 = score_change(prev, cur2)
    assert any("level_jump_ask" in r for r in reasons2)

def test_prune_snapshots_and_state(db):
    old_ts = time.time() - 60 * 60 * 72   # 72h ago
    save_snapshot(db, "kalshi", "OLD", _summary([(0.5, 1.0)], []), ts=old_ts)
    save_snapshot(db, "kalshi", "NEW", _summary([(0.5, 1.0)], []))
    upsert_state(db, "kalshi", [("GONE", 1.0, 1.0, "t")], ts=old_ts)
    upsert_state(db, "kalshi", [("LIVE", 1.0, 1.0, "t")])
    prune_snapshots(db, max_age_hours=48)
    snaps = [r["market"] for r in db.execute("SELECT market FROM whale_snapshots")]
    state = [r["market"] for r in db.execute("SELECT market FROM market_state")]
    assert snaps == ["NEW"]
    assert state == ["LIVE"]


# ── Polymarket flow aggregation ─────────────────────────────────────────────

def test_aggregate_pm_trades_dominant_flow():
    from signals.whale_scanner import aggregate_pm_trades, pm_flow_desc
    trades = [
        {"conditionId": "0xa", "slug": "m-a", "title": "Match A", "side": "BUY",
         "outcome": "UCAM", "size": 1000, "price": 0.45, "timestamp": 1},
        {"conditionId": "0xa", "slug": "m-a", "title": "Match A", "side": "BUY",
         "outcome": "UCAM", "size": 500, "price": 0.46, "timestamp": 2},
        {"conditionId": "0xa", "slug": "m-a", "title": "Match A", "side": "SELL",
         "outcome": "Misa", "size": 100, "price": 0.50, "timestamp": 3},
    ]
    agg = aggregate_pm_trades(trades)
    a = agg["0xa"]
    assert a["dollars"] == pytest.approx(1000 * 0.45 + 500 * 0.46 + 100 * 0.50)
    tag, desc = pm_flow_desc(a)
    assert tag is not None and "BUY_UCAM" in tag
    assert "BUY UCAM" in desc

def test_pm_flow_desc_balanced_returns_none():
    from signals.whale_scanner import pm_flow_desc
    flow = {"flows": {("BUY", "Yes"): 500.0, ("SELL", "Yes"): 480.0}}
    assert pm_flow_desc(flow) == (None, None)



# ── Alert-quality gates (2026-06-12 calibration audit) ──────────────────────
# Gates demote to LOW (DB keeps the row for shadow validation); they never
# block log_alert. See alert_gate() and the gated_* reason tags.

def test_ticker_event_date_parses_game_day_tickers():
    from datetime import date
    from signals.whale_scanner import _ticker_event_date
    assert _ticker_event_date("KXMLBHR-26JUN111905SEABAL-BALCCOWSER17-1") == date(2026, 6, 11)
    assert _ticker_event_date("KXATPMATCH-26JUN12ZHAMAN-ZHA") == date(2026, 6, 12)
    assert _ticker_event_date("KXWNBA1HWINNER-26JUN11NYATL-NY") == date(2026, 6, 11)

def test_ticker_event_date_none_for_undated_series():
    from signals.whale_scanner import _ticker_event_date
    assert _ticker_event_date("SENATEGA-26") is None
    assert _ticker_event_date("KXCRYPTOSTRUCTURE-26JAN-FEB1") is None  # no day digits
    assert _ticker_event_date("KXHIGHTDC") is None
    assert _ticker_event_date("KXMLBHR-26XXX11FOO") is None            # bogus month

def test_alert_gate_usd_floor():
    from signals.whale_scanner import alert_gate
    det = {"flow_dollars": 88.0, "last_yes_price": 0.11}
    assert alert_gate("kalshi", "KXNHLPTS-27JAN01XX-Y", det, None) == "usd_floor"
    # smart-wallet flow bypasses the floor
    assert alert_gate("kalshi", "KXNHLPTS-27JAN01XX-Y", det, None, smart=True) is None

def test_alert_gate_near_settled_book():
    from signals.whale_scanner import alert_gate
    det = {"flow_dollars": 9000.0}
    cur_hi = {"best_bid": 0.99, "best_ask": None}
    cur_lo = {"best_bid": 0.01, "best_ask": 0.02}
    assert alert_gate("kalshi", "KXMLBHR-27JAN01XX-Y", det, cur_hi) == "near_settled"
    assert alert_gate("kalshi", "KXMLBHR-27JAN01XX-Y", det, cur_lo) == "near_settled"

def test_alert_gate_near_settled_last_price():
    from signals.whale_scanner import alert_gate
    det = {"flow_dollars": 9000.0, "last_yes_price": 0.99}
    assert alert_gate("kalshi", "KXMLBHR-27JAN01XX-Y", det, None) == "near_settled"

def test_alert_gate_game_day(monkeypatch):
    from datetime import date
    import signals.whale_scanner as ws
    monkeypatch.setattr(ws, "_today_et", lambda: date(2026, 6, 11))
    det = {"flow_dollars": 9000.0, "last_yes_price": 0.45}
    cur = {"best_bid": 0.44, "best_ask": 0.46}
    assert ws.alert_gate("kalshi", "KXWNBA1HWINNER-26JUN11NYATL-NY", det, cur) == "game_day"
    # same ticker the day after the event: passes
    monkeypatch.setattr(ws, "_today_et", lambda: date(2026, 6, 12))
    assert ws.alert_gate("kalshi", "KXWNBA1HWINNER-26JUN11NYATL-NY", det, cur) is None
    # polymarket markets never hit the game_day gate
    assert ws.alert_gate("polymarket", "some-slug", det, cur) is None

def test_alert_gate_first_sight():
    from signals.whale_scanner import alert_gate
    det = {"flow_dollars": 9000.0, "last_yes_price": 0.45}
    assert alert_gate("kalshi", "KXMLBHR-27JAN01XX-Y", det, None, first_sight=True) == "first_sight"

def test_alert_gate_passes_real_whale():
    """$9k one-sided flow, mid-range book, future-dated event: deliverable."""
    from signals.whale_scanner import alert_gate
    det = {"flow_dollars": 9000.0, "last_yes_price": 0.45}
    cur = {"best_bid": 0.44, "best_ask": 0.46}
    assert alert_gate("kalshi", "KXATPMATCH-99DEC31XX-Y", det, cur) is None


def test_live_game_market_classifier():
    from signals.whale_scanner import is_live_game_market
    # live-game classes (cap at HIGH)
    assert is_live_game_market("kalshi", "KXMLBHR-26JUN111310MINDET-X")
    assert is_live_game_market("kalshi", "KXATPCHALLENGERMATCH-26JUN11FAUTAB-FAU")
    assert is_live_game_market("kalshi", "KXTRUMPMENTION-26JUN11-AI")
    assert is_live_game_market("kalshi", "KXWCGOAL-26JUN11MEXRSA-2")
    assert is_live_game_market("polymarket", "wnba-chi-ind-2026-06-11")
    # -vs- without a game date = matchup futures, eligible (refined 2026-06-12)
    assert not is_live_game_market("polymarket", "will-x-vs-y-happen")
    # quiet classes (CRITICAL-eligible)
    assert not is_live_game_market("kalshi", "KXHIGHMIA-26JUN11-B89.5")
    assert not is_live_game_market("kalshi", "KXFED-26JUL-T425")
    assert not is_live_game_market("kalshi", "KXPRES-2028-DJT")
    assert not is_live_game_market("polymarket", "will-4-fed-rate-cuts-happen-in-2026")

def test_sports_futures_stay_critical_eligible():
    """Per-game markets cap at HIGH; championship/season futures don't —
    a whale on World Series futures off-game IS the signal (Mr. V 2026-06-12)."""
    from signals.whale_scanner import is_live_game_market
    # per-game (game-dated): capped
    assert is_live_game_market("kalshi", "KXMLBHR-26JUN111310MINDET-X")
    assert is_live_game_market("polymarket", "mlb-stl-nym-2026-06-11-total-8pt5")
    # futures (no game date): eligible
    assert not is_live_game_market("kalshi", "KXMLBSERIES-26-LAD")
    assert not is_live_game_market("kalshi", "KXNBACHAMP-26-OKC")
    # trackers: capped unconditionally
    assert is_live_game_market("kalshi", "KXGOLDD-26JUN12-T3400")
    assert is_live_game_market("kalshi", "KXBNB15M-26JUN121145-45")


# ── Pierce rules (intelligent re-inclusion of capped classes) ───────────────

def _crit_alert():
    return {"severity": "CRITICAL", "reasons": "vol_spike_900"}

def test_ceiling_caps_churn_without_pierce(monkeypatch):
    from signals import whale_scanner as ws
    monkeypatch.setitem(ws._CLASS_P995, "KXMLBHR", 5000.0)
    a = _crit_alert()
    ws.apply_livegame_ceiling(a, "kalshi", "KXMLBHR-26JUN111310MINDET-X",
                              {"flow_dollars": 800.0, "volume": 9000}, now=1e9)
    assert a["severity"] == "HIGH" and "livegame_capped" in a["reasons"]

def test_class_outlier_pierces(monkeypatch):
    from signals import whale_scanner as ws
    monkeypatch.setitem(ws._CLASS_P995, "KXMLBHR", 5000.0)
    a = _crit_alert()
    ws.apply_livegame_ceiling(a, "kalshi", "KXMLBHR-26JUN111310MINDET-X",
                              {"flow_dollars": 42000.0, "volume": 9000}, now=1e9)
    assert a["severity"] == "CRITICAL" and "class_outlier" in a["reasons"]

def test_smart_wallet_pierces():
    from signals import whale_scanner as ws
    a = {"severity": "CRITICAL", "reasons": "vol$_spike_2000,smart_wallet_RN1_59%wr"}
    ws.apply_livegame_ceiling(a, "polymarket", "mlb-stl-nym-2026-06-11-total-8pt5",
                              {"flow_dollars": 2000.0, "top_wallet_usd": 1500.0}, now=1e9)
    assert a["severity"] == "CRITICAL" and "smart_pierce" in a["reasons"]

def test_pregame_steam_pierces_game_not_tracker():
    from signals import whale_scanner as ws
    from datetime import datetime, timezone
    now = 1e9
    far_close = datetime.fromtimestamp(now + 6 * 3600, tz=timezone.utc).isoformat()
    a = _crit_alert()
    ws.apply_livegame_ceiling(a, "kalshi", "KXMLBHR-26JUN111310MINDET-X",
                              {"flow_dollars": 600.0, "volume": 500,
                               "close_time": far_close}, now=now)
    assert a["severity"] == "CRITICAL" and "pregame_steam" in a["reasons"]
    # tracker with identical timing must NOT get the pregame pierce
    b = _crit_alert()
    ws.apply_livegame_ceiling(b, "kalshi", "KXGOLDD-26JUN12-T3400",
                              {"flow_dollars": 600.0, "volume": 500,
                               "close_time": far_close}, now=now)
    assert b["severity"] == "HIGH"

def test_quiet_market_untouched():
    from signals import whale_scanner as ws
    a = _crit_alert()
    ws.apply_livegame_ceiling(a, "kalshi", "KXHIGHMIA-26JUN11-B89.5",
                              {"flow_dollars": 50.0}, now=1e9)
    assert a["severity"] == "CRITICAL" and "capped" not in a["reasons"]

def test_live_game_class_taxonomy():
    from signals.whale_scanner import live_game_class
    assert live_game_class("kalshi", "KXMLBHR-26JUN111310MINDET-X") == "game"
    assert live_game_class("kalshi", "KXGOLDD-26JUN12-T3400") == "tracker"
    assert live_game_class("kalshi", "KXMLBSERIES-26-LAD") is None
    assert live_game_class("kalshi", "KXHIGHMIA-26JUN11-B89.5") is None

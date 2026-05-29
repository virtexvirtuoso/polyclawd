from signals.options_implied import implied_prob_above, prob_in_bracket


def test_deep_itm_approaches_one():
    assert implied_prob_above(S=300, K=200, T_years=0.02, sigma=0.5) > 0.99


def test_deep_otm_approaches_zero():
    assert implied_prob_above(S=200, K=300, T_years=0.02, sigma=0.5) < 0.01


def test_atm_near_half():
    p = implied_prob_above(S=200, K=200, T_years=0.02, sigma=0.5)
    assert 0.45 < p < 0.51


def test_guards_return_none():
    assert implied_prob_above(S=200, K=200, T_years=0, sigma=0.5) is None
    assert implied_prob_above(S=200, K=200, T_years=0.02, sigma=0) is None


def test_bracket_is_difference_of_tails():
    b = prob_in_bracket(S=200, lo=195, hi=205, T_years=0.02, sigma=0.5)
    assert 0 < b < 1


def test_store_roundtrip(tmp_path):
    from signals.options_implied import init_db, upsert_rows

    db = tmp_path / "opt.db"
    init_db(db)
    row = {
        "date": "2026-05-29",
        "options_as_of": "2026-05-28",
        "poly_market_id": "m1",
        "ticker": "NVDA",
        "expiry": "2026-05-29",
        "strike": 200.0,
        "bracket_lo": 195.0,
        "bracket_hi": 200.0,
        "market_type": "bracket",
        "poly_price": 0.30,
        "implied_prob": 0.34,
        "spread_pp": -4.0,
        "underlying": 198.5,
        "iv": 0.5,
        "poly_liquidity": 1200.0,
        "poly_vol_24h": 300.0,
    }
    assert upsert_rows(db, [row]) == 1
    assert upsert_rows(db, [row]) == 0  # idempotent on (date,poly_market_id,strike)


def test_parse_poly_event_brackets():
    from signals.options_implied import parse_poly_event

    event = {
        "id": 514761,
        "slug": "nvda-week-may-29-2026",
        "endDate": "2026-05-29T20:00:00Z",
        "markets": [
            {
                "conditionId": "0xaaa",
                "question": "NVDA between $195 and $200?",
                "outcomes": '["Yes","No"]',
                "outcomePrices": '["0.30","0.70"]',
                "liquidityNum": 1200,
                "volume24hr": 300,
            },
            {
                "conditionId": "0xbbb",
                "question": "NVDA above $240?",
                "outcomes": '["Yes","No"]',
                "outcomePrices": '["0.05","0.95"]',
                "liquidityNum": 800,
                "volume24hr": 150,
            },
        ],
    }
    out = parse_poly_event(event, "NVDA")
    assert out["ticker"] == "NVDA" and out["resolution_date"] == "2026-05-29"
    b = {m["conditionId"]: m for m in out["markets"]}
    assert b["0xaaa"]["market_type"] == "bracket"
    assert b["0xaaa"]["bracket_lo"] == 195.0 and b["0xaaa"]["bracket_hi"] == 200.0
    assert b["0xaaa"]["poly_price"] == 0.30
    assert b["0xbbb"]["market_type"] == "above" and b["0xbbb"]["bracket_lo"] == 240.0


def test_pick_iv_nearest_strike_skips_zero_iv():
    from signals.options_implied import pick_iv

    snaps = {
        "NVDA260605C00200000": {"impliedVolatility": 0.50, "greeks": {"delta": 0.5}},
        "NVDA260605C00205000": {"impliedVolatility": 0.48, "greeks": {"delta": 0.45}},
        "NVDA260605C00210000": {"impliedVolatility": 0.0, "greeks": {}},  # 0DTE-style, skip
    }
    assert pick_iv(snaps, expiry="2026-06-05", strike=202.0, right="C") == 0.50


def test_zscore_needs_min_obs(tmp_path):
    import sqlite3
    from signals.options_implied import init_db, trailing_z, MIN_OBS

    db = tmp_path / "z.db"
    init_db(db)
    con = sqlite3.connect(db)
    for i in range(MIN_OBS):
        con.execute(
            "INSERT INTO options_implied (date,poly_market_id,strike,spread_pp) VALUES (?,?,?,?)",
            (f"2026-05-{i + 1:02d}", "m1", 200.0, -8.0),
        )
    con.commit()
    con.close()
    n, mu, sd = trailing_z(db, "m1", 200.0, before="2026-06-01")
    assert n == MIN_OBS and abs(mu + 8.0) < 1e-9


import sqlite3
from datetime import date, timedelta

# 5 strikes at underlying=200 -> distinct log-moneyness buckets. Target = 210.
_STRIKES = [190.0, 200.0, 210.0, 220.0, 230.0]
_BASE = {190.0: 1.0, 200.0: 2.0, 210.0: 3.0, 220.0: 4.0, 230.0: 5.0}  # per-strike premium
_COMMON = [-4.0, -2.0, 0.0, 2.0, 4.0, 1.0, -1.0, 3.0, -3.0]            # daily market factor


def _noise(strike_idx, i):
    # deterministic, decorrelated-ish across strikes, range [-2,2] -> residual sd > SD_FLOOR
    return ((i * 7 + strike_idx * 13) % 5) - 2


def _day_spread(strike, i):
    return _COMMON[i % len(_COMMON)] + _BASE[strike] + _noise(_STRIKES.index(strike), i)


def _seed_rotation(db, n_weeks, today=None, today_delta=0.0,
                   ticker="NVDA", market_type="above"):
    """n_weeks DISTINCT conditionIds (weekly rotation) across 5 strikes (constant
    moneyness buckets via underlying=200). Each day's spreads share a common market
    factor + per-strike premium + decorrelated noise, so residuals have realistic
    variance (sd > floor). Proves obs accumulate ACROSS rotating conditionIds for the
    target's (ticker, market_type, moneyness-bucket) key -- the QA bug the old
    (poly_market_id, strike) key could never satisfy. `today_delta` shocks ONLY the
    210 target on the final day."""
    con = sqlite3.connect(db)

    def ins(d, cid, strike, sp, **extra):
        cols = ["date", "poly_market_id", "strike", "underlying", "spread_pp",
                "ticker", "market_type", "expiry", "bracket_lo"]
        vals = [d, cid, strike, 200.0, sp, ticker, market_type, d, strike]
        for k, v in extra.items():
            cols.append(k)
            vals.append(v)
        con.execute(f"INSERT INTO options_implied ({','.join(cols)}) "
                    f"VALUES ({','.join('?' * len(vals))})", vals)

    base = date(2026, 1, 5)
    for i in range(n_weeks):
        d = (base + timedelta(weeks=i)).isoformat()
        for s in _STRIKES:
            ins(d, f"0x{int(s)}_{i:03d}", s, _day_spread(s, i))
    if today is not None:
        i = n_weeks
        for s in _STRIKES:
            sp = _day_spread(s, i) + (today_delta if s == 210.0 else 0.0)
            extra = {"poly_price": 0.60, "implied_prob": 0.45} if s == 210.0 else {}
            cid = "0xTODAY" if s == 210.0 else f"0x{int(s)}_today"
            ins(today, cid, s, sp, **extra)
    con.commit()
    con.close()


def test_trade_fires_across_rotation(tmp_path):
    """OPTIONS_MIN_OBS distinct weekly conditionIds at one moneyness bucket -> obs
    accumulate despite weekly rotation (the QA bug); outlier-high target fires NO."""
    from signals.options_implied import init_db, build_trade_signals, OPTIONS_MIN_OBS

    db = tmp_path / "t.db"
    init_db(db)
    _seed_rotation(db, OPTIONS_MIN_OBS, today="2026-06-01", today_delta=8.0)
    sigs = build_trade_signals(db)
    tgt = [s for s in sigs if s["market_id"] == "0xTODAY"]
    assert len(tgt) == 1, [s["market_id"] for s in sigs]
    s = tgt[0]
    assert s["side"] == "NO" and s["archetype"] == "options"
    assert s["trailing_obs"] >= OPTIONS_MIN_OBS and s["low_confidence"] is False
    assert s["edge_pct"] > 0


def test_trade_low_z_is_YES(tmp_path):
    """Outlier-low target (residual below trailing) -> YES."""
    from signals.options_implied import init_db, build_trade_signals, OPTIONS_MIN_OBS

    db = tmp_path / "t.db"
    init_db(db)
    _seed_rotation(db, OPTIONS_MIN_OBS, today="2026-06-01", today_delta=-8.0)
    sigs = build_trade_signals(db)
    tgt = [s for s in sigs if s["market_id"] == "0xTODAY"]
    assert len(tgt) == 1 and tgt[0]["side"] == "YES"


def test_trade_skips_under_min_obs(tmp_path):
    """Fewer than the low-confidence floor of distinct dates -> no trade for target."""
    from signals.options_implied import init_db, build_trade_signals, OPTIONS_MIN_OBS_LOWCONF

    db = tmp_path / "t.db"
    init_db(db)
    _seed_rotation(db, OPTIONS_MIN_OBS_LOWCONF - 1, today="2026-06-01", today_delta=8.0)
    sigs = build_trade_signals(db)
    assert [s for s in sigs if s["market_id"] == "0xTODAY"] == []


def test_trade_skips_small_deviation(tmp_path):
    """Enough obs but target on-trend (no shock) -> |z| below threshold -> no trade."""
    from signals.options_implied import init_db, build_trade_signals, OPTIONS_MIN_OBS

    db = tmp_path / "t.db"
    init_db(db)
    _seed_rotation(db, OPTIONS_MIN_OBS, today="2026-06-01", today_delta=0.0)
    sigs = build_trade_signals(db)
    assert [s for s in sigs if s["market_id"] == "0xTODAY"] == []


def test_options_inserts_paper_position(tmp_path, monkeypatch):
    """End-to-end open path: an eligible options signal inserts a paper_positions row
    with archetype='options' (DB_PATH + evaluate_signal monkeypatched so the test is
    independent of live source-health gates)."""
    import signals.paper_portfolio as pp

    dbp = tmp_path / "shadow.db"
    monkeypatch.setattr(pp, "DB_PATH", dbp)
    conn = pp._get_db()
    pp._init_tables(conn)
    conn.close()
    monkeypatch.setattr(pp, "evaluate_signal", lambda sig: {
        "eligible": True, "reason": "test", "edge": 5.0, "kelly_pct": 0.05, "bet_size": 25.0})
    sig = {
        "market_id": "0xTODAY", "market": "NVDA above $210 (2026-06-06)", "side": "NO",
        "entry_price": 0.40, "market_price": 0.40, "confidence": 0.7, "edge_pct": 5.0,
        "strategy": "options_implied", "archetype": "options", "platform": "polymarket",
        "source": "options_implied", "days_to_close": 5,
    }
    res = pp.open_position(sig)
    assert res.get("opened") is True, res
    con = sqlite3.connect(dbp)
    row = con.execute(
        "SELECT archetype, side FROM paper_positions WHERE market_id='0xTODAY'").fetchone()
    con.close()
    assert row == ("options", "NO")


def test_resolution_logfiles_register_options():
    from signals.resolution_logger import LOG_FILES, AUTO_LOG_FILES
    assert "options_implied" in LOG_FILES and "options_implied" in AUTO_LOG_FILES


def test_resolution_model_prob_options_schema():
    import json
    from signals.resolution_logger import _model_p_yes_from_forecast
    fc = json.dumps({"type": "options_implied", "implied_prob": 0.30})
    assert abs(_model_p_yes_from_forecast(fc, 0.7, "YES") - 0.30) < 1e-6
    assert abs(_model_p_yes_from_forecast(fc, 0.7, "NO") - 0.70) < 1e-6


def test_options_open_captures_model_prob(tmp_path, monkeypatch):
    """open_position stores implied_prob in entry_forecast_json so the calibration
    tracker has a real model P(YES) for Brier — weather's self-learning hook."""
    import json
    import signals.paper_portfolio as pp
    dbp = tmp_path / "s.db"
    monkeypatch.setattr(pp, "DB_PATH", dbp)
    conn = pp._get_db()
    pp._init_tables(conn)
    conn.close()
    monkeypatch.setattr(pp, "evaluate_signal", lambda s: {
        "eligible": True, "reason": "t", "edge": 5.0, "kelly_pct": 0.05, "bet_size": 25.0})
    sig = {"market_id": "0xZ", "market": "NVDA above $210", "side": "NO", "entry_price": 0.40,
           "confidence": 0.7, "edge_pct": 5.0, "strategy": "options_implied", "archetype": "options",
           "platform": "polymarket", "implied_prob": 0.45, "z_score": 3.1, "trailing_obs": 22}
    assert pp.open_position(sig).get("opened") is True
    con = sqlite3.connect(dbp)
    efj = con.execute("SELECT entry_forecast_json FROM paper_positions WHERE market_id='0xZ'").fetchone()[0]
    con.close()
    d = json.loads(efj)
    assert d["type"] == "options_implied" and d["implied_prob"] == 0.45

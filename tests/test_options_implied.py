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


def _seed_history(
    db,
    market_id,
    strike,
    n,
    mean=5.0,
    today_spread=None,
    today="2026-06-01",
    ticker="NVDA",
    market_type="above",
    expiry="2026-06-05",
    poly_price=0.60,
    implied_prob=0.55,
):
    """Seed n trailing rows (alternating ±1 around `mean` so sd>0) + one `today` row."""
    import sqlite3

    con = sqlite3.connect(db)
    for i in range(n):
        sp = mean + (1.0 if i % 2 else -1.0)  # mean preserved, sd ~ 1
        con.execute(
            "INSERT INTO options_implied (date,poly_market_id,strike,spread_pp,ticker,market_type,expiry,bracket_lo) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"2026-04-{i + 1:02d}", market_id, strike, sp, ticker, market_type, expiry, strike),
        )
    if today_spread is not None:
        con.execute(
            "INSERT INTO options_implied (date,poly_market_id,strike,spread_pp,ticker,market_type,expiry,bracket_lo,poly_price,implied_prob) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (today, market_id, strike, today_spread, ticker, market_type, expiry, strike, poly_price, implied_prob),
        )
    con.commit()
    con.close()


def test_trade_signal_high_z_is_NO(tmp_path):
    """Spread far ABOVE its own mean (z>0, poly unusually rich) -> fade -> NO."""
    from signals.options_implied import init_db, build_trade_signals, OPTIONS_MIN_OBS

    db = tmp_path / "t.db"
    init_db(db)
    _seed_history(db, "m1", 200.0, OPTIONS_MIN_OBS, mean=5.0, today_spread=15.0)
    sigs = build_trade_signals(db)
    assert len(sigs) == 1
    s = sigs[0]
    assert s["side"] == "NO"
    assert s["archetype"] == "options" and s["strategy"] == "options_implied"
    assert s["market_id"] == "m1" and s["platform"] == "polymarket"
    assert s["edge_pct"] > 0  # |spread - mu| ~ 10pp


def test_trade_signal_low_z_is_YES(tmp_path):
    """Spread far BELOW its own mean (z<0, poly unusually cheap) -> YES."""
    from signals.options_implied import init_db, build_trade_signals, OPTIONS_MIN_OBS

    db = tmp_path / "t.db"
    init_db(db)
    _seed_history(db, "m1", 200.0, OPTIONS_MIN_OBS, mean=5.0, today_spread=-6.0)
    sigs = build_trade_signals(db)
    assert len(sigs) == 1 and sigs[0]["side"] == "YES"


def test_trade_signal_skips_under_min_obs(tmp_path):
    """Fewer than OPTIONS_MIN_OBS trailing obs -> no trade emitted."""
    from signals.options_implied import init_db, build_trade_signals, OPTIONS_MIN_OBS

    db = tmp_path / "t.db"
    init_db(db)
    _seed_history(db, "m1", 200.0, OPTIONS_MIN_OBS - 1, mean=5.0, today_spread=15.0)
    assert build_trade_signals(db) == []


def test_trade_signal_skips_small_deviation(tmp_path):
    """Enough obs but |z| below threshold -> no trade."""
    from signals.options_implied import init_db, build_trade_signals, OPTIONS_MIN_OBS

    db = tmp_path / "t.db"
    init_db(db)
    _seed_history(db, "m1", 200.0, OPTIONS_MIN_OBS, mean=5.0, today_spread=5.5)  # ~0.5 z
    assert build_trade_signals(db) == []

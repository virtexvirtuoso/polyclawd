def test_dashboard_payload_shape(tmp_path, monkeypatch):
    from signals.options_implied import init_db, upsert_rows
    db = tmp_path/"o.db"; init_db(db)
    upsert_rows(db, [{"date":"2026-05-29","poly_market_id":"m1","ticker":"NVDA",
        "expiry":"2026-05-29","strike":200.0,"market_type":"above","poly_price":0.30,
        "implied_prob":0.34,"spread_pp":-4.0,"underlying":198.5,"iv":0.5,
        "poly_liquidity":1200,"poly_vol_24h":300,"options_as_of":"2026-05-28",
        "bracket_lo":200.0,"bracket_hi":None}])
    monkeypatch.setenv("OPTIONS_DB", str(db))
    from fastapi.testclient import TestClient
    from api.main import app
    c = TestClient(app); r = c.get("/api/options/dashboard")
    assert r.status_code == 200
    j = r.json()
    assert {"totals","by_ticker","divergences","rows"} <= set(j)
    assert j["totals"]["matched"] >= 1

"""entry_price convention: held-side cost (2026-08-18 poly_delta falsification).

Convention (fixed 2026-08-18): shadow_trades.entry_price is ALWAYS the cost per
share of the token actually held. Writers whose display `price` is the YES
price of a market they fade (MCW, twc_resolution_edge) must pass an explicit
`entry_price`; log_shadow_trade prefers it over `price`. Downstream:
  - resolver NO-win payoff is 1 - entry (was +entry: overstated MCW losses,
    understated sports NO wins);
  - the dedup UPDATE never rewrites the original entry (it silently corrupted
    29/60 poly_delta study rows);
  - poly_delta_tracker never guesses a token (clob_token_ids[0] fallback
    measured the WRONG outcome on named-outcome markets) and never invents an
    entry (`entry_price or 0.5`).
"""

import sqlite3
from datetime import datetime, timezone, timedelta

import pytest

from signals import shadow_tracker as st
from services import poly_delta_tracker as pdt


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "shadow_trades.db"
    monkeypatch.setattr(st, "DB_PATH", db)
    monkeypatch.setattr(pdt, "DB_PATH", db)
    monkeypatch.setattr(st, "_migrate_legacy_json", lambda conn: None)
    return db


def _mcw_signal(**kw):
    base = {
        "market_id": "0xMCW1",
        "market": "Will the mystery novel top the bestseller list?",
        "platform": "polymarket",
        "side": "NO",
        "price": 0.58,  # YES display price
        "entry_price": 0.42,  # held-side (NO) cost
        "confidence": 70,
        "confirmations": 2,
        "days_to_close": 10,
        "volume": 50000,
        "category": "entertainment",
        "strategy": "MispricedCategoryWhale",
        "reasoning": "test",
    }
    base.update(kw)
    return base


def _read_row(db, market_id):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM shadow_trades WHERE market_id = ?", (market_id,)).fetchone()
    conn.close()
    return row


# --- writer boundary -------------------------------------------------------


def test_log_shadow_trade_prefers_entry_price_over_display_price(tmp_db):
    assert st.log_shadow_trade(_mcw_signal())
    row = _read_row(tmp_db, "0xMCW1")
    assert row["side"] == "NO"
    assert row["entry_price"] == pytest.approx(0.42)


def test_log_shadow_trade_falls_back_to_price_without_entry_price(tmp_db):
    sig = _mcw_signal(
        market_id="0xARB1", market="Arb leg market?", side="YES", price=0.42, strategy="cross_platform_arb"
    )
    del sig["entry_price"]
    assert st.log_shadow_trade(sig)
    assert _read_row(tmp_db, "0xARB1")["entry_price"] == pytest.approx(0.42)


def test_dedup_update_preserves_original_entry_price(tmp_db):
    assert st.log_shadow_trade(_mcw_signal())
    # Signal re-fires later at a different price: entry must stay the original.
    assert st.log_shadow_trade(_mcw_signal(price=0.65, entry_price=0.35, confidence=80))
    row = _read_row(tmp_db, "0xMCW1")
    assert row["entry_price"] == pytest.approx(0.42)
    assert row["confidence"] == 80  # non-anchor fields still refresh


def test_units_guard_rejects_cents_entry_price(tmp_db):
    # itunes_rss_edge/calendar_edge (2026-07-09) logged Kalshi CENTS (51.0, 64.0)
    # as entry_price, producing impossible -50/-64 per-share PnLs at resolution.
    sig = _mcw_signal(market_id="0xCENTS1", market="cents writer?", side="YES", price=51.0, strategy="itunes_rss_edge")
    del sig["entry_price"]
    assert st.log_shadow_trade(sig) is False
    assert _read_row(tmp_db, "0xCENTS1") is None


def test_units_guard_rejects_negative_entry_price(tmp_db):
    sig = _mcw_signal(market_id="0xNEG1", market="negative entry?", entry_price=-0.1)
    assert st.log_shadow_trade(sig) is False
    assert _read_row(tmp_db, "0xNEG1") is None


def test_units_guard_allows_boundary_prices(tmp_db):
    sig = _mcw_signal(market_id="0xEDGE1", market="boundary entry?", entry_price=1.0)
    assert st.log_shadow_trade(sig) is True
    assert _read_row(tmp_db, "0xEDGE1")["entry_price"] == pytest.approx(1.0)


# --- resolver PnL ----------------------------------------------------------


def _insert_open_trade(db, market_id, side, entry_price, platform="kalshi"):
    conn = st.get_db()
    conn.execute(
        """INSERT INTO shadow_trades
           (timestamp, market_id, market, platform, side, entry_price,
            resolved, snapshot_date)
           VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
        (
            datetime.now(timezone.utc).isoformat(),
            market_id,
            f"resolver test {market_id}",
            platform,
            side,
            entry_price,
            datetime.now(timezone.utc).date().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def _resolve_with_result(monkeypatch, result):
    monkeypatch.setattr(st, "_fetch_json", lambda url, timeout=8: {"market": {"result": result}})
    monkeypatch.setattr(st.time, "sleep", lambda s: None)
    return st.resolve_trades(batch_size=5, delay=0)


def test_no_side_win_pays_complement_of_entry(tmp_db, monkeypatch):
    _insert_open_trade(tmp_db, "KXNOWIN-1", "NO", 0.42)
    _resolve_with_result(monkeypatch, "no")
    row = _read_row(tmp_db, "KXNOWIN-1")
    assert row["resolved"] == 1
    assert row["pnl"] == pytest.approx(0.58)  # 1 - 0.42, NOT +0.42


def test_no_side_loss_costs_entry(tmp_db, monkeypatch):
    _insert_open_trade(tmp_db, "KXNOLOSE-1", "NO", 0.42)
    _resolve_with_result(monkeypatch, "yes")
    row = _read_row(tmp_db, "KXNOLOSE-1")
    assert row["pnl"] == pytest.approx(-0.42)  # lose the NO cost, NOT -0.58


def test_yes_side_pnl_unchanged(tmp_db, monkeypatch):
    _insert_open_trade(tmp_db, "KXYES-1", "YES", 0.42)
    _resolve_with_result(monkeypatch, "yes")
    assert _read_row(tmp_db, "KXYES-1")["pnl"] == pytest.approx(0.58)


# --- poly_delta_tracker ----------------------------------------------------


def _fake_fetch(named=False):
    outcomes = '["Los Angeles Dodgers","Minnesota Twins"]' if named else '["Yes","No"]'

    def fetch(url, timeout=8):
        if "/markets?condition_ids=" in url:
            return [{"clobTokenIds": '["tokA","tokB"]', "outcomes": outcomes}]
        if "/book" in url:
            return {"bids": [{"price": "0.40"}], "asks": [{"price": "0.44"}]}
        return None

    return fetch


def test_get_mid_returns_none_when_side_matches_no_outcome(monkeypatch):
    # Named-outcome market + side "NO": guessing token[0] measured the WRONG
    # outcome (row 350 judged a Phillies fill by the Nationals book). Refuse.
    monkeypatch.setattr(pdt, "_fetch_json", _fake_fetch(named=True))
    assert pdt._get_mid_price("0xabc", "NO") is None


def test_get_mid_still_resolves_matching_named_outcome(monkeypatch):
    monkeypatch.setattr(pdt, "_fetch_json", _fake_fetch(named=True))
    assert pdt._get_mid_price("0xabc", "Minnesota Twins") == 0.42


def test_run_once_skips_rows_with_null_entry_price(tmp_db, monkeypatch):
    conn = st.get_db()  # create schema
    ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    conn.execute(
        """INSERT INTO shadow_trades
           (timestamp, market_id, market, platform, side, entry_price, resolved)
           VALUES (?, '0xNULL1', 'null entry', 'polymarket', 'YES', NULL, 0)""",
        (ts,),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(pdt, "_get_mid_price", lambda mid, side: 0.60)
    pdt.run_once()
    row = _read_row(tmp_db, "0xNULL1")
    assert row["poly_delta_60"] is None  # was: 0.60 - 0.5 fabricated delta


def test_run_once_processes_same_day_recent_fill(tmp_db, monkeypatch):
    # The window SQL compared ISO-T timestamps against sqlite's space-separated
    # datetime() output: same-day rows NEVER matched, so deltas were only
    # written by a once-nightly sweep just after UTC midnight (lag 1min-24h,
    # and fills resolving before midnight were never measured at all).
    conn = st.get_db()
    ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    conn.execute(
        """INSERT INTO shadow_trades
           (timestamp, market_id, market, platform, side, entry_price, resolved)
           VALUES (?, '0xFRESH1', 'fresh fill', 'polymarket', 'YES', 0.42, 0)""",
        (ts,),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(pdt, "_get_mid_price", lambda mid, side: 0.40)
    result = pdt.run_once()
    assert result["updated_60"] == 1
    row = _read_row(tmp_db, "0xFRESH1")
    assert row["poly_delta_60"] == pytest.approx(-0.02)


# --- MCW construction wiring ----------------------------------------------


def test_kalshi_mcw_signal_carries_held_side_entry_price(monkeypatch):
    import signals.mispriced_category_signal as mcs

    close = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat().replace("+00:00", "Z")
    market = {
        "ticker": "KXTEST-ENTRY-1",
        "event_ticker": "KXTESTENTRY",
        "_event_category": "Entertainment",
        "title": "Will the mystery novel top the bestseller list?",
        "volume": 100000,
        "last_price_dollars": "0.58",
        "close_time": close,
    }
    monkeypatch.setattr(mcs, "fetch_kalshi_markets", lambda pages=3, per_page=30: [market])
    signals = mcs.scan_kalshi_signals()
    assert len(signals) == 1
    sig = signals[0]
    assert sig["side"] == "NO"
    assert sig["price"] == pytest.approx(0.58)  # display stays YES
    assert sig["entry_price"] == pytest.approx(0.42)  # held-side NO cost


# --- weather converter wiring ----------------------------------------------


def test_weather_buy_no_shadow_carries_held_side_entry_price():
    from services.scheduler import _weather_signal_to_shadow

    s = {
        "direction": "buy_no",
        "market_price": 0.62,
        "twc_implied_prob": 0.3,
        "edge_pp": 12.0,
        "horizon_hours": 24,
        "condition_id": "0xW1",
        "city": "NYC",
        "threshold_f": 90,
    }
    out = _weather_signal_to_shadow(s)
    assert out["side"] == "NO"
    assert out["price"] == pytest.approx(0.62)
    assert out["entry_price"] == pytest.approx(0.38)


def test_weather_buy_yes_shadow_entry_equals_price():
    from services.scheduler import _weather_signal_to_shadow

    s = {
        "direction": "buy_yes",
        "market_price": 0.62,
        "twc_implied_prob": 0.8,
        "edge_pp": 12.0,
        "horizon_hours": 24,
        "condition_id": "0xW2",
        "city": "NYC",
        "threshold_f": 90,
    }
    out = _weather_signal_to_shadow(s)
    assert out["side"] == "YES"
    assert out["entry_price"] == pytest.approx(0.62)

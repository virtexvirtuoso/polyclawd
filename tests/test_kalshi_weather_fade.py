"""Tests for the Kalshi weather tail-fade paper strategy (2026-06-10).

Covers: ticker date parse, evening-window calc (DST + Phoenix), fee-capitalized
fill round-trip against the generic resolver math, exposure caps, market dedup,
quote normalization, and the void-market resolver patch.
"""

import io
import json
import math
import sqlite3
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from signals import kalshi_weather_fade as kf
from signals import paper_portfolio as pp


# ---------------------------------------------------------------- ticker parse

def test_ticker_event_date_bracket_and_tail():
    assert kf.ticker_event_date("KXHIGHNY-26JUN09-T85") == date(2026, 6, 9)
    assert kf.ticker_event_date("KXHIGHTDC-26APR10-B55") == date(2026, 4, 10)
    assert kf.ticker_event_date("KXLOWTPHX-27JAN01-B42.5") == date(2027, 1, 1)


def test_ticker_event_date_bad_inputs():
    assert kf.ticker_event_date("") is None
    assert kf.ticker_event_date(None) is None
    assert kf.ticker_event_date("KXHIGHNY") is None
    assert kf.ticker_event_date("KXHIGHNY-26XXX09-T85") is None
    assert kf.ticker_event_date("KXHIGHNY-26FEB30-T85") is None  # invalid date


# ---------------------------------------------------------------- window calc

def _utc_for_local(tz: str, y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


def test_window_hits_nyc_at_2000_local():
    now = _utc_for_local("America/New_York", 2026, 6, 10, 20, 0)
    hits = {s for s, *_ in kf.series_in_window(now)}
    assert "KXHIGHNY" in hits and "KXLOWTNYC" in hits


def test_window_excludes_nyc_at_2100_local():
    now = _utc_for_local("America/New_York", 2026, 6, 10, 21, 0)
    hits = {s for s, *_ in kf.series_in_window(now)}
    assert "KXHIGHNY" not in hits


def test_window_target_date_is_local_tomorrow():
    now = _utc_for_local("America/New_York", 2026, 6, 10, 20, 0)
    for s, city, tz, target in kf.series_in_window(now):
        if s == "KXHIGHNY":
            assert target == date(2026, 6, 11)


def test_window_phoenix_no_dst():
    # 20:00 in Phoenix (UTC-7 year-round) = 03:00 UTC next day in June
    now = _utc_for_local("America/Phoenix", 2026, 6, 10, 20, 0)
    hits = {s for s, *_ in kf.series_in_window(now)}
    assert "KXHIGHTPHX" in hits
    # at that moment NYC is 23:00 — not in window
    assert "KXHIGHNY" not in hits


def test_window_dst_winter_vs_summer_consistency():
    # January (EST): 20:00 local must still hit the window
    now = _utc_for_local("America/New_York", 2026, 1, 15, 20, 0)
    hits = {s for s, *_ in kf.series_in_window(now)}
    assert "KXHIGHNY" in hits


# ------------------------------------------------------- fee + fill round-trip

def _resolver_pnl(side, entry_price, bet_size, won):
    """Exact P&L formulas from paper_portfolio.resolve_open_positions()."""
    if not won:
        return -bet_size
    if side == "YES":
        return bet_size * (1 / entry_price - 1)
    return bet_size * (1 / (1 - entry_price) - 1)


@pytest.mark.parametrize("side,exec_price,budget", [
    ("NO", 0.06, 100.0),   # fade a 6c longshot
    ("NO", 0.12, 100.0),
    ("YES", 0.55, 50.0),   # favorite buy
    ("YES", 0.68, 50.0),
])
def test_fill_roundtrip_nets_fee_exactly(side, exec_price, budget):
    fill = kf.simulate_fill(side, exec_price, budget)
    assert fill, "fill should be possible"
    fee = kf.kalshi_fee(exec_price)
    cost = exec_price + fee
    c = fill["contracts"]
    assert c == math.floor(budget / cost)
    assert fill["bet_size"] == pytest.approx(c * cost, abs=1e-3)

    # WIN: resolver formula must equal contracts * (1 - cost) exactly
    win = _resolver_pnl(side, fill["entry_price"], fill["bet_size"], True)
    assert win == pytest.approx(c * (1 - cost), rel=1e-3)
    # LOSS: lose the full stake including capitalized fee
    loss = _resolver_pnl(side, fill["entry_price"], fill["bet_size"], False)
    assert loss == pytest.approx(-c * cost, rel=1e-9)


def test_fill_rejects_unfillable():
    assert kf.simulate_fill("NO", 0.999, 100.0) == {}   # cost > 0.99
    assert kf.simulate_fill("YES", 0.99, 50.0) == {}
    assert kf.simulate_fill("YES", 0.60, 0.30) == {}    # budget < 1 contract


# ------------------------------------------------------------------ quotes

def test_quotes_dollars_preferred_and_cents_fallback():
    q = kf.quotes_from_market({"yes_bid_dollars": "0.05", "yes_ask_dollars": "0.07",
                               "no_bid_dollars": "0.93", "no_ask_dollars": "0.95"})
    assert q == {"yes_bid": 0.05, "yes_ask": 0.07, "no_bid": 0.93, "no_ask": 0.95}
    q2 = kf.quotes_from_market({"yes_bid": 5, "yes_ask": 7})  # cents
    assert q2["yes_bid"] == 0.05 and q2["yes_ask"] == 0.07
    # derived no-side from yes identity
    assert q2["no_ask"] == pytest.approx(0.95)
    assert q2["no_bid"] == pytest.approx(0.93)


def test_depth_orientation():
    ob = {"yes": [[3, 200], [5, 120]], "no": [[90, 40], [93, 75]]}
    # NO buy crosses best yes bid (0.05 level, qty 120)
    assert kf.depth_for_buy(ob, "NO") == 120
    # YES buy crosses best no bid (0.93 level, qty 75)
    assert kf.depth_for_buy(ob, "YES") == 75


# ------------------------------------------------------------------ caps + dedup

def _tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "shadow_trades.db"
    monkeypatch.setattr(pp, "DB_PATH", db)
    conn = pp._get_db()
    return conn


def _insert_fade(conn, ticker, city, bet, status="open"):
    conn.execute(
        """INSERT INTO paper_positions
        (opened_at, market_id, market_title, platform, side, entry_price,
         bet_size, status, archetype, strategy, entry_forecast_json)
        VALUES (?, ?, ?, 'kalshi', 'NO', 0.9, ?, ?, ?, 'kalshi_fade_longshot_no', ?)""",
        (datetime.now(timezone.utc).isoformat(), ticker, ticker, bet, status,
         kf.ARCHETYPE, json.dumps({"city": city})))
    conn.commit()


def test_date_exposure_and_city_cap(tmp_path, monkeypatch):
    conn = _tmp_db(tmp_path, monkeypatch)
    _insert_fade(conn, "KXHIGHNY-26JUN11-T85", "nyc", 100)
    _insert_fade(conn, "KXHIGHCHI-26JUN11-B70", "chi", 100)
    _insert_fade(conn, "KXHIGHMIA-26JUN12-T95", "mia", 100)  # different date
    rows = kf._open_fade_rows(conn)
    assert kf._date_exposure(rows, date(2026, 6, 11)) == pytest.approx(200)
    assert kf._date_exposure(rows, date(2026, 6, 12)) == pytest.approx(100)
    assert kf._count_city_date(rows, "nyc", date(2026, 6, 11)) == 1
    assert kf._count_city_date(rows, "chi", date(2026, 6, 12)) == 0
    conn.close()


def test_market_dedup_any_status(tmp_path, monkeypatch):
    conn = _tmp_db(tmp_path, monkeypatch)
    _insert_fade(conn, "KXHIGHNY-26JUN11-T85", "nyc", 100, status="lost")
    assert kf._market_exists(conn, "KXHIGHNY-26JUN11-T85") is True
    assert kf._market_exists(conn, "KXHIGHNY-26JUN12-T85") is False
    conn.close()


# ------------------------------------------------------------- void resolver fix

class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_void_kalshi_market_returns_stake_not_loss(tmp_path, monkeypatch):
    conn = _tmp_db(tmp_path, monkeypatch)
    _insert_fade(conn, "KXHIGHNY-26JUN09-T85", "nyc", 100)
    conn.close()

    def fake_urlopen(req, timeout=10):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        assert "kalshi" in url
        return _FakeResp(json.dumps(
            {"market": {"result": "void", "status": "settled"}}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    pp.resolve_open_positions()

    conn = pp._get_db()
    row = conn.execute("SELECT status, pnl, close_reason FROM paper_positions").fetchone()
    conn.close()
    assert row["status"] == "stopped"
    assert row["pnl"] == 0
    assert "VOID" in row["close_reason"]


def test_yes_result_still_resolves_normally(tmp_path, monkeypatch):
    conn = _tmp_db(tmp_path, monkeypatch)
    _insert_fade(conn, "KXHIGHNY-26JUN09-T85", "nyc", 100)  # side=NO, entry 0.9
    conn.close()

    def fake_urlopen(req, timeout=10):
        return _FakeResp(json.dumps(
            {"market": {"result": "no", "status": "settled"}}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    pp.resolve_open_positions()

    conn = pp._get_db()
    row = conn.execute("SELECT status, pnl FROM paper_positions").fetchone()
    conn.close()
    assert row["status"] == "won"
    assert row["pnl"] == pytest.approx(100 * (1 / (1 - 0.9) - 1))


# ------------------------------------------------------- zombie guard (QA fix)

def test_whitespace_result_resolves_as_win(tmp_path, monkeypatch):
    conn = _tmp_db(tmp_path, monkeypatch)
    _insert_fade(conn, "KXHIGHNY-26JUN09-T85", "nyc", 100)  # side=NO
    conn.close()

    def fake_urlopen(req, timeout=10):
        return _FakeResp(json.dumps(
            {"market": {"result": "no ", "status": "settled"}}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    pp.resolve_open_positions()
    conn = pp._get_db()
    row = conn.execute("SELECT status FROM paper_positions").fetchone()
    conn.close()
    assert row["status"] == "won"  # 'no ' must strip to 'no', not book as void


def test_zombie_fade_row_refunded_after_3_days(tmp_path, monkeypatch):
    conn = _tmp_db(tmp_path, monkeypatch)
    _insert_fade(conn, "KXHIGHNY-26JUN01-T85", "nyc", 100)  # event long past
    conn.close()

    def fake_urlopen(req, timeout=10):
        raise OSError("404")  # market gone — fetch returns None

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    pp.resolve_open_positions()
    conn = pp._get_db()
    row = conn.execute("SELECT status, pnl, close_reason FROM paper_positions").fetchone()
    conn.close()
    assert row["status"] == "stopped"
    assert row["pnl"] == 0
    assert "UNRESOLVABLE" in row["close_reason"]


def test_recent_fade_row_not_zombie_closed(tmp_path, monkeypatch):
    from datetime import date as _date, timedelta as _td
    recent = _date.today() - _td(days=1)
    ticker = f"KXHIGHNY-{recent.strftime('%y%b%d').upper()}-T85"
    conn = _tmp_db(tmp_path, monkeypatch)
    _insert_fade(conn, ticker, "nyc", 100)
    conn.close()

    def fake_urlopen(req, timeout=10):
        raise OSError("404")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    pp.resolve_open_positions()
    conn = pp._get_db()
    row = conn.execute("SELECT status FROM paper_positions").fetchone()
    conn.close()
    assert row["status"] == "open"  # only 1 day past event — leave it alone

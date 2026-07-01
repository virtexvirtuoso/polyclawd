"""Tests for the PAPER-ONLY goalscorer portfolio + alerting
(signals/scorer_paper_portfolio.py).

Synthetic data only — NO network, NO real money, NO Telegram send. The resolver
is injected, so settlement is driven by hand-built verdicts. Covers the P&L math,
the full position lifecycle, dedup, and the report aggregation.

Run: venv/bin/python -m pytest tests/test_scorer_paper_portfolio.py -q --noconftest
"""

import pytest

from signals.scorer_paper_portfolio import (
    db_connect,
    record_positions,
    resolve_open_positions,
    portfolio_report,
    format_alert,
    send_alert,
    ResolveState,
    STATUS_OPEN,
    STATUS_WON,
    STATUS_LOST,
    STATUS_VOID,
    STATUS_DISPUTED,
)


@pytest.fixture()
def db(tmp_path):
    con = db_connect(str(tmp_path / "scorer_paper.db"))
    yield con
    con.close()


def bet(player, event_title, stake=10.0, decimal_price=3.0, **extra):
    b = {"player": player, "event_title": event_title, "stake": stake,
         "decimal_price": decimal_price}
    b.update(extra)
    return b


def _status(db, player):
    row = db.execute(
        "SELECT status, pnl, result_value FROM scorer_paper_positions WHERE player = ?",
        (player,),
    ).fetchone()
    return row  # (status, pnl, result_value)


def resolver_map(verdicts):
    """Build an injected resolver that returns a fixed ResolveState per player."""
    def _fn(position):
        return verdicts.get(position["player"], ResolveState.PENDING)
    return _fn


# ── (1) won position: pnl = stake*(odds-1) ────────────────────────────────────
def test_won_pnl_is_stake_times_odds_minus_one(db):
    record_positions([bet("Kane", "ENG vs FRA", stake=10.0, decimal_price=3.0)], db)
    settled = resolve_open_positions(db, resolver_map({"Kane": ResolveState.YES}))
    assert settled == 1
    status, pnl, rv = _status(db, "Kane")
    assert status == STATUS_WON
    assert rv == ResolveState.YES
    assert pnl == pytest.approx(10.0 * (3.0 - 1.0))  # 20.0


# ── (2) lost position: pnl = -stake ───────────────────────────────────────────
def test_lost_pnl_is_negative_stake(db):
    record_positions([bet("Mbappe", "ENG vs FRA", stake=10.0, decimal_price=3.0)], db)
    resolve_open_positions(db, resolver_map({"Mbappe": ResolveState.NO}))
    status, pnl, rv = _status(db, "Mbappe")
    assert status == STATUS_LOST
    assert pnl == pytest.approx(-10.0)


# ── (3) void / disputed → pnl 0 and excluded from win-rate ────────────────────
def test_void_and_disputed_pnl_zero_and_excluded_from_winrate(db):
    record_positions(
        [
            bet("Kane", "ENG vs FRA", stake=10.0, decimal_price=3.0),     # won
            bet("Saka", "ENG vs FRA", stake=10.0, decimal_price=2.0),     # void
            bet("Foden", "ENG vs ESP", stake=10.0, decimal_price=4.0),    # disputed
        ],
        db,
    )
    resolve_open_positions(
        db,
        resolver_map({
            "Kane": ResolveState.YES,
            "Saka": ResolveState.VOID,
            "Foden": ResolveState.DISPUTED,
        }),
    )
    assert _status(db, "Saka")[0] == STATUS_VOID
    assert _status(db, "Saka")[1] == pytest.approx(0.0)
    assert _status(db, "Foden")[0] == STATUS_DISPUTED
    assert _status(db, "Foden")[1] == pytest.approx(0.0)

    rpt = portfolio_report(db)
    # Only the won bet is decisive → win-rate 1/1, void+disputed not counted.
    assert rpt["won"] == 1
    assert rpt["lost"] == 0
    assert rpt["void"] == 1
    assert rpt["disputed"] == 1
    assert rpt["win_rate"] == pytest.approx(1.0)
    # paper P&L = won (+20) only; void/disputed contribute 0.
    assert rpt["paper_pnl"] == pytest.approx(20.0)


# ── (4) open positions stay pending until final ───────────────────────────────
def test_open_positions_stay_pending(db):
    record_positions([bet("Vinicius", "BRA vs ARG", stake=5.0, decimal_price=2.5)], db)
    # Resolver says PENDING → must remain open, nothing settled.
    settled = resolve_open_positions(db, resolver_map({"Vinicius": ResolveState.PENDING}))
    assert settled == 0
    status, pnl, rv = _status(db, "Vinicius")
    assert status == STATUS_OPEN
    assert pnl is None and rv is None
    assert portfolio_report(db)["open"] == 1

    # UNMATCHED also leaves it open.
    assert resolve_open_positions(db, resolver_map({"Vinicius": ResolveState.UNMATCHED})) == 0
    assert _status(db, "Vinicius")[0] == STATUS_OPEN

    # Now it goes final → settles.
    assert resolve_open_positions(db, resolver_map({"Vinicius": ResolveState.YES})) == 1
    assert _status(db, "Vinicius")[0] == STATUS_WON


# ── (5) dedup: same player/event not double-recorded ──────────────────────────
def test_dedup_same_event_player(db):
    n1 = record_positions([bet("Kane", "ENG vs FRA", stake=10.0, decimal_price=3.0)], db)
    # Re-record the same prop (e.g. a later snapshot) — must be a no-op.
    n2 = record_positions([bet("Kane", "ENG vs FRA", stake=99.0, decimal_price=5.0)], db)
    assert n1 == 1
    assert n2 == 0
    count = db.execute("SELECT COUNT(*) FROM scorer_paper_positions").fetchone()[0]
    assert count == 1
    # Same player, DIFFERENT event is a distinct position.
    n3 = record_positions([bet("Kane", "ENG vs ESP", stake=10.0, decimal_price=3.0)], db)
    assert n3 == 1


# ── (6) portfolio_report aggregates correctly ─────────────────────────────────
def test_portfolio_report_aggregation(db):
    record_positions(
        [
            bet("A", "M1", stake=10.0, decimal_price=3.0),   # won  → +20
            bet("B", "M1", stake=20.0, decimal_price=2.0),   # lost → -20
            bet("C", "M2", stake=10.0, decimal_price=4.0),   # void → 0
            bet("D", "M2", stake=10.0, decimal_price=2.5),   # open
        ],
        db,
    )
    resolve_open_positions(
        db,
        resolver_map({
            "A": ResolveState.YES,
            "B": ResolveState.NO,
            "C": ResolveState.VOID,
            "D": ResolveState.PENDING,
        }),
    )
    rpt = portfolio_report(db)
    assert rpt["open"] == 1
    assert rpt["settled"] == 3            # won + lost + void
    assert rpt["won"] == 1
    assert rpt["lost"] == 1
    assert rpt["void"] == 1
    assert rpt["disputed"] == 0
    # paper P&L = +20 (A) -20 (B) + 0 (C) = 0
    assert rpt["paper_pnl"] == pytest.approx(0.0)
    # decisive staked = 10 (A) + 20 (B) = 30; win-rate = 1/2
    assert rpt["total_staked"] == pytest.approx(30.0)
    assert rpt["win_rate"] == pytest.approx(0.5)
    assert rpt["roi"] == pytest.approx(0.0)


def test_report_empty_db_has_none_winrate(db):
    rpt = portfolio_report(db)
    assert rpt["open"] == 0
    assert rpt["settled"] == 0
    assert rpt["win_rate"] is None
    assert rpt["roi"] is None
    assert rpt["paper_pnl"] == pytest.approx(0.0)


# ── price coercion: american input → decimal P&L ─────────────────────────────
def test_american_price_coerced_to_decimal(db):
    # +200 american = 3.0 decimal → won pays stake*(3-1) = 20.
    record_positions([{"player": "X", "event_title": "E", "stake": 10.0, "american": 200}], db)
    resolve_open_positions(db, resolver_map({"X": ResolveState.YES}))
    assert _status(db, "X")[1] == pytest.approx(20.0)


# ── alert formatting: clearly PAPER, no send in tests ────────────────────────
def test_format_alert_is_clearly_paper():
    msg = format_alert([bet("Kane", "ENG vs FRA", stake=10.0, decimal_price=3.0, edge_pct=6.2)])
    assert "PAPER" in msg
    assert "Kane" in msg
    assert "no real money" in msg.lower()


def test_send_alert_default_does_not_send(monkeypatch):
    # Guard: even with env set, send=False (default) must not hit the network.
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("network must not be called in tests")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "y")
    out = send_alert([bet("Kane", "ENG vs FRA")])  # send defaults to False
    assert "PAPER" in out
    assert called["n"] == 0

"""Task 1.2c-2: the paper stop loops close paper positions in the paper books and nothing else.

Live positions are stopped by evaluate_live_stops (best-bid mark, spread allowance, one
alert). Before this task the two paper loops also looked up an open live position on the
same market and, when armed, sold it through _close_live_position_early at the YES price
of the market (wrong for a NO-side position) and announced the result as a paper
close. These tests arm the process, open a live position on the very market a paper
position is stopped on, make any live exit raise, and require the paper stop to happen
in the paper books with the live position and the exchange untouched.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import services.stop_evaluator as se

PAPER_SCHEMA = """
CREATE TABLE paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, opened_at TEXT NOT NULL, market_id TEXT NOT NULL,
    market_title TEXT, platform TEXT DEFAULT 'kalshi', side TEXT NOT NULL, entry_price REAL NOT NULL,
    bet_size REAL NOT NULL, potential_payout REAL, confidence REAL, edge_pct REAL,
    status TEXT DEFAULT 'open', closed_at TEXT, exit_price REAL, pnl REAL, close_reason TEXT,
    strategy TEXT DEFAULT '', archetype TEXT DEFAULT '', entry_forecast_json TEXT
);
CREATE TABLE paper_portfolio_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, bankroll REAL NOT NULL,
    total_pnl REAL DEFAULT 0, total_trades INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0, win_rate REAL DEFAULT 0, max_drawdown REAL DEFAULT 0,
    peak_bankroll REAL NOT NULL, current_drawdown_pct REAL DEFAULT 0, sharpe_estimate REAL DEFAULT 0
);
"""

MARKET = "0xmarket-shared-by-paper-and-live"


@pytest.fixture
def paper_db(tmp_path, monkeypatch):
    db = tmp_path / "paper.db"
    conn = sqlite3.connect(db)
    conn.executescript(PAPER_SCHEMA)
    conn.execute("INSERT INTO paper_portfolio_state (timestamp, bankroll, peak_bankroll)"
                 " VALUES (?, 1000.0, 1000.0)", (datetime.now(timezone.utc).isoformat(),))
    conn.commit()
    conn.close()
    monkeypatch.setattr(se, "DB_PATH", db)
    monkeypatch.setattr(se, "_load_engine_state", lambda: {})
    monkeypatch.setattr(se, "_parse_market_date",
                        lambda title: datetime.now(timezone.utc) + timedelta(hours=3))
    import signals.resolution_logger as rl
    monkeypatch.setattr(rl, "log_position_close", lambda *a, **k: None)
    return db


def _paper_position(db, side, entry):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO paper_positions (opened_at, market_id, market_title, platform, side,"
        " entry_price, bet_size, edge_pct, status, strategy)"
        " VALUES (?, ?, 'Paper market on July 20?', 'polymarket', ?, ?, 10.0, 0.05, 'open', '')",
        (datetime.now(timezone.utc).isoformat(), MARKET, side, entry))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("loop", ["evaluate_stops", "evaluate_stops_urgent"])
@pytest.mark.parametrize("side,entry,yes_price", [
    ("YES", 0.50, 0.27),    # -46% on the YES token
    ("NO", 0.50, 0.73),     # -46% on the NO token: the YES price is NOT the NO bid
])
def test_paper_loop_stops_the_paper_position_and_never_the_live_one(
        loop, side, entry, yes_price, armed, live_db, fake_clob, paper_db, monkeypatch, sent_alerts):
    live_db.conn.execute(
        "INSERT INTO live_positions (market_id, token_id, side, entry_price, shares, cost_usd, status)"
        " VALUES (?, 'tok1', 'BUY', 0.5, 20, 10.0, 'open')", (MARKET,))
    live_db.conn.commit()
    _paper_position(paper_db, side, entry)

    def _live_exit_must_not_run(*a, **k):
        raise AssertionError("the paper loop reached the live exit")

    monkeypatch.setattr(se, "_close_live_position_early", _live_exit_must_not_run)
    import execution.live_executor as le
    monkeypatch.setattr(le, "execute_exit", _live_exit_must_not_run)
    monkeypatch.setattr(se, "_fetch_price", lambda pos: (pos["id"], yes_price))
    monkeypatch.setattr(se, "_send_discord_alert", lambda info: None)
    import scripts.openclaw_alerts as oa
    sent = []
    monkeypatch.setattr(oa, "alert_openclaw", lambda msg, **kw: sent.append(msg) or True)

    stopped = getattr(se, loop)()

    assert len(stopped) == 1
    assert "UNIVERSAL STOP" in stopped[0]["reason"]
    assert "live" not in stopped[0]                      # a paper result, not a live one
    conn = sqlite3.connect(paper_db)
    status, close_reason = conn.execute("SELECT status, close_reason FROM paper_positions").fetchone()
    conn.close()
    assert status == "stopped" and close_reason.startswith("stop-loss: UNIVERSAL STOP")
    assert len(sent) == 1 and "STOP-LOSS CLOSED" in sent[0]      # the paper close alert, unchanged
    live_row = live_db.conn.execute("SELECT status, shares FROM live_positions").fetchone()
    assert (live_row["status"], live_row["shares"]) == ("open", 20)
    fake_clob.assert_no_calls()


def test_the_live_position_lookup_is_gone():
    assert not hasattr(se, "_get_live_position")

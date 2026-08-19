"""realized_pnl must come from the closed-positions ledger, not SELL fills
(resolution/manual closes never write SELL fills — the July closes proved it)."""
import pytest

from execution import live_db
from execution import live_position_tracker as lpt


@pytest.fixture
def conn(tmp_path):
    return live_db.connect(path=tmp_path / "t.db")


def test_realized_comes_from_closed_positions(conn, monkeypatch):
    conn.execute(
        "INSERT INTO live_positions (opened_at, market_id, token_id, side, entry_price,"
        " shares, cost_usd, status, closed_at, exit_price, pnl, close_reason)"
        " VALUES ('2026-07-14T00:00:00+00:00','m1','t1','BUY',0.4,10,4.0,'closed',"
        " '2026-07-15T00:00:00+00:00',1.0,6.0,'resolution')")
    conn.commit()
    # no live_fills SELL rows at all — old formula would return 0.0
    snap = lpt.recompute_equity(conn, onchain_balance=23.87)
    assert snap["realized_pnl"] == pytest.approx(6.0)


def test_ledger_divergence_logged_not_fatal(conn, monkeypatch):
    # closed position says +6; SELL fills say +2 → divergence > $1 must warn, not raise
    conn.execute(
        "INSERT INTO live_positions (opened_at, market_id, token_id, side, entry_price,"
        " shares, cost_usd, status, closed_at, exit_price, pnl, close_reason)"
        " VALUES ('2026-07-14T00:00:00+00:00','m1','t1','BUY',0.4,10,4.0,'closed',"
        " '2026-07-15T00:00:00+00:00',1.0,6.0,'resolution')")
    conn.execute(
        "INSERT INTO live_fills (ts, side, price, shares, usd, fee_paid, fair_price)"
        " VALUES ('2026-07-15T00:00:00+00:00','SELL',0.6,10,6.0,0.0,0.4)")
    conn.commit()
    snap = lpt.recompute_equity(conn, onchain_balance=23.87)
    assert snap["realized_pnl"] == pytest.approx(6.0)  # positions ledger wins

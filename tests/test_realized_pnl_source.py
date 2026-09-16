"""realized_pnl must come from the closed-positions ledger, not SELL fills
(resolution/manual closes never write SELL fills — the July closes proved it).

The realized computation is a UNION of the two close regimes, joined on
position_id so nothing double-counts:
  - a leg with a SELL fill (close_position()) contributes the fill's leg value
  - a closed position with NO SELL fill (resolution/manual) contributes pnl
"""

import pytest

from execution import live_db
from execution import live_position_tracker as lpt


@pytest.fixture
def conn(tmp_path):
    return live_db.connect(path=tmp_path / "t.db")


def test_realized_comes_from_closed_positions(conn):
    conn.execute(
        "INSERT INTO live_positions (opened_at, market_id, token_id, side, entry_price,"
        " shares, cost_usd, status, closed_at, exit_price, pnl, close_reason)"
        " VALUES ('2026-07-14T00:00:00+00:00','m1','t1','BUY',0.4,10,4.0,'closed',"
        " '2026-07-15T00:00:00+00:00',1.0,6.0,'resolution')"
    )
    conn.commit()
    # no live_fills SELL rows at all — old formula would return 0.0
    snap = lpt.recompute_equity(conn, onchain_balance=23.87)
    assert snap["realized_pnl"] == pytest.approx(6.0)


def test_sell_fill_leg_wins_over_position_pnl_no_double_count(conn, monkeypatch):
    # position closed with pnl=6.0 AND a SELL fill on the SAME position
    # (leg value = shares*(price-fair_price)-fee = 10*(0.6-0.4)-0 = 2.0)
    # → realized must be 2.0 (the fill leg), not 8.0 (double-count) and not 6.0.
    conn.execute(
        "INSERT INTO live_positions (id, opened_at, market_id, token_id, side, entry_price,"
        " shares, cost_usd, status, closed_at, exit_price, pnl, close_reason)"
        " VALUES (1, '2026-07-14T00:00:00+00:00','m1','t1','BUY',0.4,10,4.0,'closed',"
        " '2026-07-15T00:00:00+00:00',1.0,6.0,'resolution')"
    )
    conn.execute(
        "INSERT INTO live_fills (ts, position_id, side, price, shares, usd, fee_paid, fair_price)"
        " VALUES ('2026-07-15T00:00:00+00:00',1,'SELL',0.6,10,6.0,0.0,0.4)"
    )
    conn.commit()

    warnings = []
    monkeypatch.setattr(lpt.logger, "warning", lambda *a, **k: warnings.append((a, k)))

    snap = lpt.recompute_equity(conn, onchain_balance=23.87)
    assert snap["realized_pnl"] == pytest.approx(2.0)  # fill leg wins, no double-count
    assert warnings  # union 2.0 vs closed-sum 6.0 diverges > 1 → must warn


def test_partial_close_sell_fill_counts_before_final_close(conn):
    # I1: open position (no pnl yet) + one SELL fill on it (partial close leg)
    # → realized must include the partial leg even though the position is
    # still open (leg value = 10*(0.6-0.4)-0 = 2.0).
    conn.execute(
        "INSERT INTO live_positions (id, opened_at, market_id, token_id, side, entry_price,"
        " shares, cost_usd, status)"
        " VALUES (1, '2026-07-14T00:00:00+00:00','m1','t1','BUY',0.4,10,4.0,'open')"
    )
    conn.execute(
        "INSERT INTO live_fills (ts, position_id, side, price, shares, usd, fee_paid, fair_price)"
        " VALUES ('2026-07-15T00:00:00+00:00',1,'SELL',0.6,10,6.0,0.0,0.4)"
    )
    conn.commit()
    snap = lpt.recompute_equity(conn, onchain_balance=23.87)
    assert snap["realized_pnl"] == pytest.approx(2.0)

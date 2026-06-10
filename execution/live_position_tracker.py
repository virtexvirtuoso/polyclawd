"""Live position tracker — records real fills and maintains the live equity curve.

Design notes
------------
* `record_real_fill` is the sole write path for live fills.  It writes to
  `live_fills` and upserts `live_positions` (VWAP on subsequent fills into
  the same open market).
* `recompute_equity` computes unrealized P&L by calling
  `odds.polymarket_clob.get_orderbook` for each open position's token_id.
  Tests MUST monkeypatch that function — it makes real network calls.
* `total_equity = onchain_balance + unrealized_pnl` (onchain_balance already
  reflects realised cash; we only add the mark-to-market delta for open legs).
* `peak_equity` is monotonically non-decreasing across calls: it's loaded from
  the most recent snapshot before writing the next one.
* `get_live_portfolio` is the dashboard contract for the /live endpoint
  (Phase H).  Keys are stable — downstream consumers depend on them.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from execution.live_db import (
    insert_position,
    record_fill,
    snapshot_equity,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _open_position_for_market(conn: sqlite3.Connection, market_id: str) -> dict | None:
    """Return the first OPEN live_position for *market_id*, or None."""
    cur = conn.execute(
        "SELECT * FROM live_positions WHERE market_id = ? AND status = 'open' LIMIT 1",
        (market_id,),
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


def _get_prev_peak(conn: sqlite3.Connection) -> float:
    """Return peak_equity from the most recent equity snapshot, or 0.0."""
    cur = conn.execute("SELECT peak_equity FROM live_equity_snapshots ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    return float(row[0]) if row is not None else 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def record_real_fill(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    market_id: str,
    market_slug: str,
    side: str,
    liquidity: str,
    price: float,
    shares: float,
    usd: float,
    fee_paid: float,
    fair_price: float,
    token_id: str | None = None,
    market_title: str | None = None,
) -> int:
    """Record a real fill and open/update the live position.

    Returns
    -------
    int
        The position_id for the affected live_positions row.
    """
    slippage = price - fair_price

    # --- atomic upsert + fill (C2: BEGIN IMMEDIATE prevents TOCTOU duplicates)
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _open_position_for_market(conn, market_id)
        if existing is not None:
            # VWAP update; also patch token_id / market_title when newly known (I2)
            old_cost = existing["cost_usd"]
            old_shares = existing["shares"]
            new_cost = old_cost + usd
            new_shares = old_shares + shares
            new_entry = new_cost / new_shares if new_shares else price
            new_fee_total = (existing["fee_paid_total"] or 0.0) + fee_paid
            conn.execute(
                """UPDATE live_positions
                   SET entry_price    = ?,
                       shares         = ?,
                       cost_usd       = ?,
                       fee_paid_total = ?,
                       token_id       = COALESCE(NULLIF(token_id, ''), ?),
                       market_title   = COALESCE(NULLIF(market_title, ''), ?)
                   WHERE id = ?""",
                (
                    new_entry,
                    new_shares,
                    new_cost,
                    new_fee_total,
                    token_id or "",
                    market_title or "",
                    existing["id"],
                ),
            )
            position_id = existing["id"]
        else:
            position_id = insert_position(
                conn,
                commit=False,
                opened_at=_utcnow(),
                market_id=market_id,
                market_slug=market_slug,
                market_title=market_title or "",
                token_id=token_id or "",
                side=side,
                entry_price=price,
                shares=shares,
                cost_usd=usd,
                status="open",
                fee_paid_total=fee_paid,
                archetype="weather",
            )

        record_fill(
            conn,
            commit=False,
            ts=_utcnow(),
            position_id=position_id,
            order_id=order_id,
            side=side,
            liquidity=liquidity,
            price=price,
            shares=shares,
            usd=usd,
            fee_paid=fee_paid,
            fair_price=fair_price,
            slippage_vs_fair=slippage,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return position_id


def recompute_equity(conn: sqlite3.Connection, onchain_balance: float) -> dict[str, Any]:
    """Compute a live equity snapshot and persist it.

    Unrealized P&L is computed for every OPEN position by calling
    ``odds.polymarket_clob.get_orderbook(token_id)`` — tests must
    monkeypatch this to avoid real network calls.

    total_equity = onchain_balance + unrealized_pnl
    (onchain_balance already embeds realized cash; we add only the
    mark-to-market delta of open legs.)

    Returns
    -------
    dict
        The newly written snapshot row as a plain dict.
    """
    # Import here so monkeypatching in tests works correctly
    from odds.polymarket_clob import get_orderbook

    # Realized P&L — aggregate over live_fills SELL rows so that every close
    # leg (partial maker + taker) is counted.  close_position() records each
    # SELL fill with fair_price = entry_price, so:
    #   shares * (price - fair_price)  →  per-share gain/loss vs entry
    #   - fee_paid                     →  cost deducted per leg
    # This is partial-close-safe: each leg contributes independently, unlike
    # reading live_positions.pnl which is only written on the FINAL close leg.
    cur = conn.execute(
        "SELECT COALESCE(SUM(shares * (price - fair_price) - fee_paid), 0.0) FROM live_fills WHERE side = 'SELL'"
    )
    realized_pnl = float(cur.fetchone()[0])

    # Open positions for unrealized mark
    cur = conn.execute("SELECT id, token_id, entry_price, shares FROM live_positions WHERE status = 'open'")
    open_rows = [dict(r) for r in cur.fetchall()]
    open_count = len(open_rows)

    unrealized_pnl = 0.0
    for pos in open_rows:
        tid = pos.get("token_id") or ""
        if tid:
            book = get_orderbook(tid)
            if book is not None:
                mid = book.mid_price
            else:
                mid = pos["entry_price"]
        else:
            logger.warning(
                "recompute_equity: position id={} market_id={} has no token_id — "
                "falling back to entry_price mark (unrealized contribution = 0)",
                pos["id"],
                pos.get("market_id", "unknown"),
            )
            mid = pos["entry_price"]
        unrealized_pnl += pos["shares"] * (mid - pos["entry_price"])

    total_equity = onchain_balance + unrealized_pnl

    # Fees cumulative
    cur = conn.execute("SELECT COALESCE(SUM(fee_paid), 0.0) FROM live_fills")
    fees_cumulative = float(cur.fetchone()[0])

    # Peak equity — monotonically non-decreasing
    prev_peak = _get_prev_peak(conn)
    peak_equity = max(prev_peak, total_equity)

    ts = _utcnow()
    snap_id = snapshot_equity(
        conn,
        ts=ts,
        onchain_balance=onchain_balance,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        total_equity=total_equity,
        open_positions=open_count,
        peak_equity=peak_equity,
        fees_paid_cumulative=fees_cumulative,
    )

    return {
        "id": snap_id,
        "ts": ts,
        "onchain_balance": onchain_balance,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "total_equity": total_equity,
        "open_positions": open_count,
        "peak_equity": peak_equity,
        "fees_paid_cumulative": fees_cumulative,
    }


def close_position(
    conn,
    *,
    position_id=None,
    market_id=None,
    exit_price,
    shares_sold,
    fee_paid,
    reason,
    liquidity="maker",
    order_id=None,
):
    """Mark a live position closed (or partially closed) and record the exit fill.

    Parameters
    ----------
    position_id : int, optional
        Explicit position row id. If omitted, *market_id* is used to find the
        first open position for that market.
    market_id : str, optional
        Market id used to find the open position when *position_id* is not given.
    exit_price : float
        Execution price at which shares_sold were sold.
    shares_sold : float
        Number of shares actually sold (may be < position shares for partial close).
    fee_paid : float
        Taker fee paid on this exit leg (0.0 for maker exits).
    reason : str
        Close reason label (e.g. "hard_cap_stop", "maker_close").
    liquidity : str
        "maker" or "taker".
    order_id : str, optional
        The CLOB order id for the exit fill row (used in live_fills).

    Returns
    -------
    dict
        pnl             float -- realised pnl for shares_sold
        usd_released    float -- cost basis released (proportional for partial close)
        position_id     int   -- the affected position row id
        status          str   -- "closed" or "open" (partial)
        shares_sold     float
        exit_price      float
        fee_paid        float
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Locate the open position
        if position_id is not None:
            cur = conn.execute(
                "SELECT * FROM live_positions WHERE id = ? AND status = 'open' LIMIT 1",
                (position_id,),
            )
        elif market_id is not None:
            cur = conn.execute(
                "SELECT * FROM live_positions WHERE market_id = ? AND status = 'open' LIMIT 1",
                (market_id,),
            )
        else:
            raise ValueError("close_position: one of position_id or market_id is required")

        row = cur.fetchone()
        if row is None:
            raise ValueError(
                f"close_position: no open position found for position_id={position_id} market_id={market_id}"
            )
        pos = dict(row)
        pid = int(pos["id"])

        pos_shares = float(pos["shares"])
        pos_cost = float(pos["cost_usd"])
        entry_price = float(pos["entry_price"])
        old_fee_total = float(pos.get("fee_paid_total") or 0.0)

        # Clamp shares_sold to what is actually held.
        shares_sold = min(float(shares_sold), pos_shares)
        if shares_sold <= 0:
            raise ValueError("close_position: shares_sold must be > 0")

        is_partial = shares_sold < pos_shares - 1e-9

        # pnl for a long/BUY position sold at exit_price
        pnl = shares_sold * (exit_price - entry_price) - fee_paid

        # Cost basis released is proportional to shares sold.
        frac = shares_sold / pos_shares if pos_shares > 0 else 1.0
        usd_released = pos_cost * frac

        now = _utcnow()

        if is_partial:
            # Reduce position -- keep status open
            new_shares = pos_shares - shares_sold
            new_cost = pos_cost - usd_released
            new_fee_total = old_fee_total + fee_paid
            conn.execute(
                """UPDATE live_positions
                   SET shares         = ?,
                       cost_usd       = ?,
                       fee_paid_total = ?
                   WHERE id = ?""",
                (new_shares, new_cost, new_fee_total, pid),
            )
            new_status = "open"
        else:
            # Full close
            new_fee_total = old_fee_total + fee_paid
            conn.execute(
                """UPDATE live_positions
                   SET status         = 'closed',
                       closed_at      = ?,
                       exit_price     = ?,
                       pnl            = ?,
                       close_reason   = ?,
                       fee_paid_total = ?
                   WHERE id = ?""",
                (now, exit_price, pnl, reason, new_fee_total, pid),
            )
            new_status = "closed"

        # Write the exit fill row.
        slippage = exit_price - entry_price
        record_fill(
            conn,
            commit=False,
            ts=now,
            position_id=pid,
            order_id=order_id or "",
            side="SELL",
            liquidity=liquidity,
            price=exit_price,
            shares=shares_sold,
            usd=shares_sold * exit_price,
            fee_paid=fee_paid,
            fair_price=entry_price,
            slippage_vs_fair=slippage,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "pnl": pnl,
        "usd_released": usd_released,
        "position_id": pid,
        "status": new_status,
        "shares_sold": shares_sold,
        "exit_price": exit_price,
        "fee_paid": fee_paid,
    }


def get_live_portfolio(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return the dashboard dict consumed by the /live endpoint (Phase H).

    Note (M3): this function reads the *latest persisted equity snapshot*. The
    unrealized_pnl and total_equity figures reflect the last time
    ``recompute_equity`` was called — they are NOT recomputed here.  Callers
    that need fresh mark-to-market numbers must call ``recompute_equity`` first.

    Keys (stable contract)
    ----------------------
    ts                  ISO timestamp of the latest equity snapshot
    onchain_balance     float
    realized_pnl        float
    unrealized_pnl      float
    total_equity        float
    open_positions      int  — count
    peak_equity         float
    fees_paid_cumulative float
    positions           list[dict] — all open live_positions rows
    fill_split          dict with keys "maker" and "taker" (fill counts)
    """
    # Latest equity snapshot
    cur = conn.execute("SELECT * FROM live_equity_snapshots ORDER BY id DESC LIMIT 1")
    snap_row = cur.fetchone()
    if snap_row is not None:
        snap = dict(snap_row)
    else:
        # No snapshot yet — return zeroed structure
        snap = {
            "ts": None,
            "onchain_balance": 0.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "total_equity": 0.0,
            "open_positions": 0,
            "peak_equity": 0.0,
            "fees_paid_cumulative": 0.0,
        }

    # Open positions list
    cur = conn.execute("SELECT * FROM live_positions WHERE status = 'open' ORDER BY opened_at")
    positions = [dict(r) for r in cur.fetchall()]

    # Maker / taker fill split
    cur = conn.execute("SELECT liquidity, COUNT(*) as cnt FROM live_fills GROUP BY liquidity")
    fill_split: dict[str, int] = {"maker": 0, "taker": 0}
    for row in cur.fetchall():
        liq = (row["liquidity"] or "").lower()
        fill_split[liq] = int(row["cnt"])

    return {
        "ts": snap.get("ts"),
        "onchain_balance": snap.get("onchain_balance", 0.0),
        "realized_pnl": snap.get("realized_pnl", 0.0),
        "unrealized_pnl": snap.get("unrealized_pnl", 0.0),
        "total_equity": snap.get("total_equity", 0.0),
        "open_positions": snap.get("open_positions", 0),
        "peak_equity": snap.get("peak_equity", 0.0),
        "fees_paid_cumulative": snap.get("fees_paid_cumulative", 0.0),
        "positions": positions,
        "fill_split": fill_split,
    }

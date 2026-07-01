"""Live trading dashboard endpoints — Phase H.

Read-only display layer over the live_* tables in shadow_trades.db.
No endpoint mutates any live state.

Endpoints (all rooted under /api/live/ via the /api prefix in main.py):
  GET /api/live/portfolio       — equity snapshot + governor state + caps
  GET /api/live/positions       — open live_positions rows
  GET /api/live/fills?limit=N   — recent live_fills rows
  GET /api/live/governor        — governor state + raw live_portfolio_state
  GET /api/live/edge-capture?limit=N — per-fill realized-vs-fair edge

Connection pattern: open a fresh live_db connection per request in a
try/finally block so we never leak connections (no shared app-level conn).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query

from execution import live_config, live_db
from execution.live_position_tracker import get_live_portfolio, recompute_equity

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_recompute(conn, onchain_balance: float) -> None:
    """Call recompute_equity but swallow any network / import error.

    The CLOB orderbook fetch is a real network call; tests monkeypatch
    odds.polymarket_clob.get_orderbook.  On failure we log a warning and
    return without updating the snapshot — the portfolio endpoint will still
    return the *last persisted* snapshot values (unrealized may be stale but
    the endpoint returns 200).
    """
    try:
        recompute_equity(conn, onchain_balance)
    except Exception as exc:
        logger.warning("live/portfolio: recompute_equity failed (non-fatal): %s", exc)


def _get_governor_dict(conn) -> dict:
    """Read the latest live_portfolio_state row for governor context."""
    state = live_db.get_state(conn)
    if state is None:
        state = {}
    return {
        "governor_state": state.get("governor_state", "ACTIVE"),
        "daily_loss": float(state.get("daily_loss") or 0.0),
        "bankroll": float(state.get("bankroll") or 0.0),
        "deployed_usd": float(state.get("deployed_usd") or 0.0),
        "ramp_stage": state.get("ramp_stage"),
    }


# ---------------------------------------------------------------------------
# /api/live/portfolio
# ---------------------------------------------------------------------------


@router.get("/live/portfolio")
def get_live_portfolio_endpoint():
    """Return live account summary.

    Calls recompute_equity best-effort (guards network errors so the endpoint
    always returns 200).  Keys match the Phase H contract spec.
    """
    conn = live_db.connect()
    try:
        # Read onchain_balance from the latest snapshot to seed recompute.
        state = live_db.get_state(conn)
        onchain_balance = float(state.get("bankroll") or 0.0) if state else 0.0

        # Best-effort fresh mark — swallowed on network/import failure.
        _safe_recompute(conn, onchain_balance)

        portfolio = get_live_portfolio(conn)
        gov_raw = _get_governor_dict(conn)

        total_equity = float(portfolio.get("total_equity") or 0.0)
        realized_pnl = float(portfolio.get("realized_pnl") or 0.0)
        unrealized_pnl = float(portfolio.get("unrealized_pnl") or 0.0)
        onchain_bal = float(portfolio.get("onchain_balance") or 0.0)
        peak_equity = float(portfolio.get("peak_equity") or 0.0)

        bankroll = gov_raw["bankroll"] or onchain_bal
        deployed_usd = gov_raw["deployed_usd"]
        reserve_usd = max(0.0, bankroll - deployed_usd)

        kill_floor = live_config.kill_floor()
        distance_to_kill = max(0.0, total_equity - kill_floor)

        daily_loss_limit = live_config.daily_loss_halt()
        daily_pnl = -abs(gov_raw["daily_loss"])  # daily_loss stored as positive magnitude

        since_inception_pct: Optional[float] = None
        if peak_equity > 0 and onchain_bal > 0:
            # Use the initial bankroll approximation: first equity snapshot would
            # be onchain_balance with no pnl; we approximate via realized P&L.
            initial_approx = onchain_bal - realized_pnl
            if initial_approx > 0:
                since_inception_pct = round(
                    (total_equity - initial_approx) / initial_approx * 100, 2
                )

        return {
            "onchain_balance": round(onchain_bal, 4),
            "total_equity": round(total_equity, 4),
            "realized_pnl": round(realized_pnl, 4),
            "unrealized_pnl": round(unrealized_pnl, 4),
            "since_inception_pct": since_inception_pct,
            "ramp_stage": gov_raw.get("ramp_stage"),
            "governor_state": gov_raw["governor_state"],
            "daily_pnl": round(daily_pnl, 4),
            "daily_loss_limit": round(daily_loss_limit, 2),
            "deployed_usd": round(deployed_usd, 4),
            "reserve_usd": round(reserve_usd, 4),
            "distance_to_kill": round(distance_to_kill, 4),
            "per_trade_cap": round(live_config.per_trade_cap(), 2),
            "mode": live_config.mode(),
            # extras for completeness
            "peak_equity": round(peak_equity, 4),
            "fees_paid_cumulative": round(
                float(portfolio.get("fees_paid_cumulative") or 0.0), 4
            ),
            "fill_split": portfolio.get("fill_split", {"maker": 0, "taker": 0}),
            "ts": portfolio.get("ts"),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# /api/live/positions
# ---------------------------------------------------------------------------


@router.get("/live/positions")
def get_live_positions():
    """Return all OPEN live_positions rows with age in seconds."""
    conn = live_db.connect()
    try:
        cur = conn.execute(
            "SELECT * FROM live_positions WHERE status = 'open' ORDER BY opened_at"
        )
        rows = [dict(r) for r in cur.fetchall()]

        now = _utcnow()
        result = []
        for r in rows:
            opened_at = r.get("opened_at") or ""
            age_secs: Optional[float] = None
            if opened_at:
                try:
                    opened_dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
                    now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
                    age_secs = round((now_dt - opened_dt).total_seconds(), 1)
                except Exception:
                    pass

            result.append(
                {
                    "id": r.get("id"),
                    "market": r.get("market_id"),
                    "market_title": r.get("market_title"),
                    "market_slug": r.get("market_slug"),
                    "side": r.get("side"),
                    "entry": round(float(r.get("entry_price") or 0), 6),
                    "shares": round(float(r.get("shares") or 0), 4),
                    "cost_usd": round(float(r.get("cost_usd") or 0), 4),
                    "fee_paid_total": round(float(r.get("fee_paid_total") or 0), 6),
                    "unrealized": None,  # Phase H: mark comes from recompute_equity snapshot
                    "opened_at": opened_at,
                    "age_secs": age_secs,
                    "status": r.get("status"),
                    "archetype": r.get("archetype"),
                    "token_id": r.get("token_id"),
                }
            )
        return {"positions": result, "count": len(result)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# /api/live/fills
# ---------------------------------------------------------------------------


@router.get("/live/fills")
def get_live_fills(limit: int = Query(default=50, ge=1, le=500)):
    """Return recent live_fills rows, newest-first.

    Includes liquidity tag (maker/taker), fee_paid, and slippage_vs_fair for
    the edge-capture display.
    """
    conn = live_db.connect()
    try:
        cur = conn.execute(
            "SELECT * FROM live_fills ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = [dict(r) for r in cur.fetchall()]
        result = []
        for r in rows:
            result.append(
                {
                    "id": r.get("id"),
                    "ts": r.get("ts"),
                    "position_id": r.get("position_id"),
                    "order_id": r.get("order_id"),
                    "side": r.get("side"),
                    "liquidity": r.get("liquidity"),
                    "price": round(float(r.get("price") or 0), 6),
                    "shares": round(float(r.get("shares") or 0), 4),
                    "usd": round(float(r.get("usd") or 0), 4),
                    "fee_paid": round(float(r.get("fee_paid") or 0), 6),
                    "fair_price": round(float(r.get("fair_price") or 0), 6),
                    "slippage_vs_fair": round(
                        float(r.get("slippage_vs_fair") or 0), 6
                    ),
                }
            )
        return {"fills": result, "count": len(result)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# /api/live/governor
# ---------------------------------------------------------------------------


@router.get("/live/governor")
def get_live_governor():
    """Return current risk governor state and caps."""
    conn = live_db.connect()
    try:
        gov = _get_governor_dict(conn)
        return {
            "state": gov["governor_state"],
            "per_trade_cap": round(live_config.per_trade_cap(), 2),
            "daily_loss_halt": round(live_config.daily_loss_halt(), 2),
            "kill_floor": round(live_config.kill_floor(), 2),
            "max_deployed_frac": live_config.max_deployed_frac(),
            "daily_loss": round(gov["daily_loss"], 4),
            "deployed_usd": round(gov["deployed_usd"], 4),
            "bankroll": round(gov["bankroll"], 4),
            "ramp_stage": gov.get("ramp_stage"),
            "mode": live_config.mode(),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# /api/live/edge-capture
# ---------------------------------------------------------------------------


@router.get("/live/edge-capture")
def get_live_edge_capture(limit: int = Query(default=100, ge=1, le=1000)):
    """Return per-fill realized-vs-fair edge for the edge-capture chart.

    Each row shows:
      fill_id, ts, liquidity, realized_net_edge
        = price_received - fair_price - fee_paid_per_share
        (negative on BUY fills → cost; positive on SELL fills → gain vs entry)
      slippage_vs_fair (raw)
      side
    """
    conn = live_db.connect()
    try:
        cur = conn.execute(
            "SELECT id, ts, side, liquidity, price, shares, fee_paid, fair_price, slippage_vs_fair "
            "FROM live_fills ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        result = []
        for r in rows:
            shares = float(r.get("shares") or 0)
            fee_paid = float(r.get("fee_paid") or 0)
            slippage = float(r.get("slippage_vs_fair") or 0)
            # fee per share (avoid div/0)
            fee_per_share = (fee_paid / shares) if shares > 0 else 0.0
            # realized net edge: on BUY, we pay slippage (negative is bad); on SELL it's a gain
            side = (r.get("side") or "").upper()
            if side == "BUY":
                realized_net_edge = -slippage - fee_per_share
            else:
                realized_net_edge = slippage - fee_per_share

            result.append(
                {
                    "fill_id": r.get("id"),
                    "ts": r.get("ts"),
                    "side": side,
                    "liquidity": r.get("liquidity"),
                    "slippage_vs_fair": round(slippage, 6),
                    "fee_per_share": round(fee_per_share, 6),
                    "realized_net_edge": round(realized_net_edge, 6),
                }
            )
        return {"edge_captures": result, "count": len(result)}
    finally:
        conn.close()

"""
Stop-Loss Evaluator — Phase 1 of Adaptive Exit System

Conservative fixed stops for now. Will transition to learned/adaptive
exits once position_price_log has enough resolved trade trajectories.

Conservative defaults (Phase 1):
- Exit if unrealized loss > 50% of bet size
- Exit if edge flipped negative (signal reversal)

Called from scheduler tick_5min().
"""

import sqlite3
import json
import urllib.request
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from loguru import logger

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "storage" / "shadow_trades.db"
CLOB_API = "https://clob.polymarket.com"
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

# ── Conservative Stop Config ──────────────────────────────────────────────
# These are intentionally wide. Phase 2 will learn tighter per-strategy stops.

STOP_CONFIG = {
    "default": {
        "max_loss_pct": 0.50,      # exit if unrealized loss > 50% of bet size
        "edge_floor": -0.02,       # exit if current edge < -2pp (signal flipped)
    },
    # Per-strategy overrides (tighter where we have more confidence in data)
    "weather": {
        "max_loss_pct": 0.50,
        "edge_floor": -0.02,
    },
    "tweet_count_mc": {
        "max_loss_pct": 0.50,
        "edge_floor": -0.02,
    },
}

# Cooldown: don't re-alert on same position within N minutes
ALERT_COOLDOWN_MINUTES = 60
_alert_cache = {}  # position_id → last_alert_ts


def _db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _fetch_url(url, timeout=10):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug("Stop evaluator fetch failed {}: {}", url, e)
        return None


def _fetch_price(pos):
    """Fetch current YES token price. Returns (position_id, price_or_None)."""
    market_id = pos["market_id"]
    platform = pos.get("platform") or "kalshi"

    if platform == "polymarket" or market_id.startswith("0x"):
        data = _fetch_url(f"{CLOB_API}/markets/{market_id}")
        if data:
            tokens = data.get("tokens", [])
            if tokens:
                return (pos["id"], float(tokens[0].get("price", 0)))
    else:
        data = _fetch_url(f"{KALSHI_API}/markets/{market_id}")
        if data:
            market = data.get("market", data)
            cp = market.get("last_price")
            if cp and cp > 1:
                cp = cp / 100
            return (pos["id"], cp)

    return (pos["id"], None)


def _compute_unrealized_pnl(side, entry_price, current_yes_price, bet_size):
    """Compute unrealized P&L if we sold at current price."""
    if side == "YES":
        # Bought YES at entry_price, current value = current_yes_price
        return bet_size * (current_yes_price / entry_price - 1)
    else:
        # Bought NO at (1 - entry_price), current NO value = (1 - current_yes_price)
        no_entry = 1 - entry_price
        no_current = 1 - current_yes_price
        return bet_size * (no_current / no_entry - 1) if no_entry > 0 else 0


def _get_config(strategy):
    """Get stop config for a strategy, falling back to defaults."""
    return STOP_CONFIG.get(strategy, STOP_CONFIG["default"])


def _should_alert(position_id):
    """Check cooldown — avoid spamming alerts for same position."""
    now = datetime.now(timezone.utc)
    last = _alert_cache.get(position_id)
    if last and (now - last).total_seconds() < ALERT_COOLDOWN_MINUTES * 60:
        return False
    _alert_cache[position_id] = now
    return True


def _close_position_early(conn, pos, current_yes_price, unrealized_pnl, reason):
    """
    Close a position at current market price (early exit).
    Status = 'stopped' to distinguish from won/lost/void.
    """
    pnl = round(unrealized_pnl, 2)
    exit_price = current_yes_price

    conn.execute("""
        UPDATE paper_positions
        SET status = 'stopped',
            closed_at = ?,
            exit_price = ?,
            pnl = ?,
            close_reason = ?
        WHERE id = ?
    """, (
        datetime.now(timezone.utc).isoformat(),
        round(exit_price, 4),
        pnl,
        f"stop-loss: {reason}",
        pos["id"],
    ))

    # Update bankroll
    from signals.paper_portfolio import _get_bankroll, _save_state
    bankroll = _get_bankroll(conn) + pnl
    _save_state(conn, bankroll, pnl)

    logger.info(
        "STOP-LOSS: {} | {} @ {:.0%} → {:.0%} | P&L ${:+.2f} | reason: {}",
        (pos["market_title"] or "")[:50], pos["side"],
        pos["entry_price"], exit_price, pnl, reason,
    )

    return {
        "position_id": pos["id"],
        "market_title": pos["market_title"],
        "side": pos["side"],
        "entry_price": pos["entry_price"],
        "current_price": current_yes_price,
        "pnl": pnl,
        "bet_size": pos["bet_size"],
        "reason": reason,
        "strategy": pos["strategy"] or "",
    }


def _send_discord_alert(stop_info):
    """Send Discord alert for a stop-loss trigger."""
    try:
        from signals.discord_alerts import _send, _portfolio_context, COLOR_ORANGE, COLOR_RED

        entry = stop_info["entry_price"]
        current = stop_info["current_price"]
        side = stop_info["side"]
        bet_size = stop_info["bet_size"]
        pnl = stop_info["pnl"]
        full_loss = -bet_size
        saved = abs(full_loss) - abs(pnl)

        ctx = _portfolio_context()

        fields = [
            {"name": "Side", "value": f"**{side}**", "inline": True},
            {"name": "Entry → Exit", "value": f"{entry:.0%} → {current:.0%}", "inline": True},
            {"name": "Strategy", "value": stop_info["strategy"] or "—", "inline": True},
            {"name": "Loss (stopped)", "value": f"**-${abs(pnl):,.2f}**", "inline": True},
            {"name": "Loss (if held)", "value": f"-${abs(full_loss):,.2f}", "inline": True},
            {"name": "Saved", "value": f"**+${saved:,.2f}**" if saved > 0 else "—", "inline": True},
            {"name": "Reason", "value": stop_info["reason"], "inline": False},
        ]

        _send([{
            "title": f"🛑 STOP-LOSS — {(stop_info['market_title'] or '?')[:70]}",
            "description": f"Position closed early to limit loss",
            "color": COLOR_RED,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": f"Stop-Loss Monitor · 💰 ${ctx['bankroll']:,.0f} · {ctx['record']}"},
        }], alert_type="stop_loss", alert_meta={
            "market": (stop_info["market_title"] or "")[:200],
            "side": side, "entry": entry, "exit": current,
            "pnl": pnl, "saved": saved, "reason": stop_info["reason"],
        })
    except Exception as e:
        logger.warning("Stop-loss Discord alert failed: {}", e)


def evaluate_stops():
    """
    Main entry point. Check all open positions against stop criteria.
    Close positions that breach stops. Returns list of stopped positions.
    """
    conn = _db()
    rows = conn.execute(
        "SELECT id, market_id, market_title, platform, side, entry_price, "
        "bet_size, edge_pct, strategy, opened_at "
        "FROM paper_positions WHERE status = 'open'"
    ).fetchall()

    if not rows:
        conn.close()
        return []

    positions = [dict(r) for r in rows]

    # Parallel price fetch
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_fetch_price, positions))

    price_map = {pid: price for pid, price in results if price is not None}
    stopped = []

    for pos in positions:
        pid = pos["id"]
        current_yes_price = price_map.get(pid)
        if current_yes_price is None:
            continue

        config = _get_config(pos["strategy"] or "")
        side = pos["side"]
        entry_price = pos["entry_price"]
        bet_size = pos["bet_size"]

        # Compute unrealized P&L
        unrealized = _compute_unrealized_pnl(side, entry_price, current_yes_price, bet_size)
        loss_pct = abs(unrealized) / bet_size if unrealized < 0 else 0

        # ── Check 1: Max loss percentage ──
        if unrealized < 0 and loss_pct >= config["max_loss_pct"]:
            reason = f"loss {loss_pct:.0%} >= {config['max_loss_pct']:.0%} threshold"
            result = _close_position_early(conn, pos, current_yes_price, unrealized, reason)
            conn.commit()
            stopped.append(result)
            _send_discord_alert(result)
            continue

        # ── Check 2: Edge erosion (placeholder for Phase 2) ──
        # Will re-run signal here once we have fast edge re-estimation.
        # For now, only the max_loss_pct stop is active.

    conn.close()

    if stopped:
        logger.info("Stop evaluator: {} positions stopped", len(stopped))
    else:
        logger.debug("Stop evaluator: all {} positions within limits", len(positions))

    return stopped


if __name__ == "__main__":
    results = evaluate_stops()
    if results:
        for r in results:
            print(f"STOPPED: {r['market_title'][:60]} | {r['side']} | P&L ${r['pnl']:+.2f} | {r['reason']}")
    else:
        print("All positions within stop limits")

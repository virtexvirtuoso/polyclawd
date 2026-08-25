"""daily_pnl_summary.py — Nightly live positions P&L report via Telegram.

Fires at 22:xx ET (02:xx UTC next day) to cover US sports markets.
Summarizes: open positions, today's exits, net realized P&L, win rate.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


def _fmt_pnl(v: float) -> str:
    return f"${v:+.2f}"


def run() -> dict:
    try:
        from execution import live_db
        conn = live_db.connect()
    except Exception as exc:
        logger.warning("daily_pnl: db connect failed: %s", exc)
        return {"error": str(exc)}

    try:
        # ── Open positions ────────────────────────────────────────────────────
        open_rows = conn.execute(
            "SELECT market_title, side, entry_price, shares, cost_usd, archetype "
            "FROM live_positions WHERE status='open' ORDER BY opened_at DESC"
        ).fetchall()

        # ── Today's closed positions (last 24h) ───────────────────────────────
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        closed_rows = conn.execute(
            "SELECT market_title, side, entry_price, exit_price, shares, pnl, close_reason "
            "FROM live_positions WHERE status != 'open' AND closed_at >= ? ORDER BY closed_at DESC",
            (cutoff,)
        ).fetchall()

        conn.close()

        # ── Build message ─────────────────────────────────────────────────────
        lines = [f"📊 <b>DAILY LIVE P&amp;L SUMMARY</b> — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"]

        # Open positions
        lines.append(f"\n<b>Open Positions ({len(open_rows)})</b>")
        if open_rows:
            for r in open_rows:
                title = r[0][:40] if r[0] else "Unknown"
                lines.append(f"  • {title} | {r[1]} @ {r[2]:.2f} | {r[3]:.1f} shares | ${r[4]:.2f}")
        else:
            lines.append("  (none)")

        # Closed today
        realized_pnl = sum(float(r[5] or 0) for r in closed_rows)
        wins = sum(1 for r in closed_rows if (r[5] or 0) > 0)
        win_rate = wins / len(closed_rows) if closed_rows else 0

        lines.append(f"\n<b>Closed Today ({len(closed_rows)})</b>")
        if closed_rows:
            for r in closed_rows:
                title = r[0][:40] if r[0] else "Unknown"
                pnl_str = _fmt_pnl(float(r[5] or 0))
                lines.append(f"  {'✅' if (r[5] or 0) > 0 else '❌'} {title} | {pnl_str} [{r[6] or 'exit'}]")
            lines.append(f"\n  Net realized: {_fmt_pnl(realized_pnl)} | WR: {win_rate:.0%} ({wins}/{len(closed_rows)})")
        else:
            lines.append("  (none today)")

        msg = "\n".join(lines)

        try:
            from scripts.alert_formatter import send_telegram
            send_telegram(msg)
        except Exception as tg_exc:
            logger.warning("daily_pnl: telegram failed: %s", tg_exc)

        return {
            "open": len(open_rows),
            "closed_today": len(closed_rows),
            "realized_pnl": round(realized_pnl, 2),
            "win_rate": round(win_rate, 3),
        }

    except Exception as exc:
        logger.error("daily_pnl: failed: %s", exc)
        return {"error": str(exc)}

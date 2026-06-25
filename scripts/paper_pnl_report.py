#!/usr/bin/env python3
"""Daily paper-portfolio P&L report — LLM-free replacement for the OpenClaw
agent cron. Reads shadow_trades.db and pushes a formatted summary straight to
Telegram via the Bot API. Always sends (this is a daily heartbeat report).

Run from VPS cron:
    set -a && . ~/.config/polyclawd/alerts.env && set +a
    venv/bin/python3 scripts/paper_pnl_report.py --send
"""

import argparse
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "storage" / "shadow_trades.db"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--send", action="store_true", help="push to Telegram (Bot API, no LLM)"
    )
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB), timeout=10)
    conn.row_factory = sqlite3.Row

    state = conn.execute(
        "SELECT bankroll, total_pnl, wins, losses, win_rate, peak_bankroll, "
        "current_drawdown_pct, sharpe_estimate FROM paper_portfolio_state "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()

    status = {
        r["status"]: r["cnt"]
        for r in conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM paper_positions GROUP BY status"
        )
    }
    open_n = status.get("open", 0)

    # Today's resolutions (closed in the last day), split W/L.
    today = conn.execute(
        "SELECT "
        " SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) AS w, "
        " SUM(CASE WHEN status IN ('lost','stopped') THEN 1 ELSE 0 END) AS l, "
        " ROUND(SUM(COALESCE(pnl,0)),2) AS pnl "
        "FROM paper_positions WHERE closed_at >= date('now','-1 day')"
    ).fetchone()

    daily = conn.execute(
        "SELECT date, trades_resolved, wins, losses, win_rate "
        "FROM daily_summaries ORDER BY date DESC LIMIT 1"
    ).fetchone()
    conn.close()

    bankroll = state["bankroll"] if state else 0.0
    total_pnl = state["total_pnl"] if state else 0.0
    peak = state["peak_bankroll"] if state else bankroll

    # Compute WR + drawdown from ground truth — the stored win_rate /
    # current_drawdown_pct fields are stale/buggy (both read ~0.4 while the real
    # values are ~35% and ~39%).
    won = status.get("won", 0)
    losses_all = status.get("lost", 0) + status.get("stopped", 0)
    resolved = won + losses_all
    cum_wr = (won / resolved * 100) if resolved else 0.0
    dd = ((peak - bankroll) / peak * 100) if peak else 0.0

    tw = today["w"] or 0
    tl = today["l"] or 0
    tpnl = today["pnl"] or 0.0
    arrow = "🟢" if tpnl >= 0 else "🔴"

    # Plain text only — alert_openclaw sends with parse_mode=Markdown, and any
    # stray * / _ (e.g. table names) breaks the parse with HTTP 400.
    lines = [
        "📊 Polyclawd Paper P&L — daily",
        f"Bankroll: ${bankroll:,.0f} (peak ${peak:,.0f}, dd from peak {dd:.0f}%)",
        f"Total P&L: ${total_pnl:+,.0f}",
        f"Today: {arrow} {tw}W-{tl}L | ${tpnl:+,.0f}",
        f"Open positions: {open_n}",
        f"Cumulative WR (incl stops): {cum_wr:.0f}% ({won}W / {losses_all}L+stop)",
    ]
    if daily:
        lines.append(
            f"summary {daily['date']}: {daily['trades_resolved']} resolved, "
            f"{daily['wins']}W-{daily['losses']}L"
        )
    text = "\n".join(lines)
    print(text)

    if args.send:
        try:
            sys.path.insert(0, str(BASE))
            from scripts.openclaw_alerts import alert_openclaw

            print(f"[send] telegram ok={alert_openclaw(text)}")
        except Exception as e:
            print(f"[send] failed: {e}")


if __name__ == "__main__":
    main()

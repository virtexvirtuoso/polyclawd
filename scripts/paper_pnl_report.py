#!/usr/bin/env python3
"""Daily paper-portfolio P&L report — LLM-free replacement for the OpenClaw
agent cron. Reads shadow_trades.db and pushes a formatted summary straight to
Telegram via the Bot API. Always sends (this is a daily heartbeat report).

Run from VPS cron:
    set -a && . ~/.config/polyclawd/alerts.env && set +a
    venv/bin/python3 scripts/paper_pnl_report.py --send
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "storage" / "shadow_trades.db"
LEDGER = BASE / "logs" / "telegram_sent.jsonl"


def stops_proof_line(db_path=None, now=None) -> str:
    """Daily proof-of-life for the stop evaluator (plan Task 2.1 Step 3):
    reads the stop_heartbeat row written by evaluate_stops(). Never raises."""
    now_ts = int(now if now is not None else time.time())
    try:
        conn = sqlite3.connect(str(db_path or DB), timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT ts, positions_checked, warnings_fired FROM stop_heartbeat WHERE id=1").fetchone()
        finally:
            conn.close()
        if row is None or row["ts"] is None:
            return "stops: no heartbeat recorded yet"
        age_m = max(0, now_ts - int(row["ts"])) // 60
        return (
            f"stops: checked {row['positions_checked'] or 0} positions, "
            f"{row['warnings_fired'] or 0} warnings fired, last run {age_m}m ago"
        )
    except Exception:
        return "stops: no heartbeat recorded yet"


def delivery_line(ledger_path=None, now=None) -> str:
    """Delivery success rate over the last 24h from the send ledger
    (covers spec P0 action 5 at zero marginal cost). Never raises."""
    now_ts = now if now is not None else time.time()
    path = Path(ledger_path or os.environ.get("POLYCLAWD_LEDGER_PATH") or LEDGER)
    total = ok = 0
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    ts = datetime.fromisoformat(r["ts"]).timestamp()
                except Exception:
                    continue
                if now_ts - ts <= 24 * 3600:
                    total += 1
                    if r.get("ok"):
                        ok += 1
    except OSError:
        return "delivery: no send ledger found"
    if not total:
        return "delivery: no sends in last 24h"
    return f"delivery: {ok / total * 100:.0f}% success last 24h ({ok}/{total} from send ledger)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="push to Telegram (Bot API, no LLM)")
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
        for r in conn.execute("SELECT status, COUNT(*) AS cnt FROM paper_positions GROUP BY status")
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
        "SELECT date, trades_resolved, wins, losses, win_rate FROM daily_summaries ORDER BY date DESC LIMIT 1"
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
            f"summary {daily['date']}: {daily['trades_resolved']} resolved, {daily['wins']}W-{daily['losses']}L"
        )
    # Proof-of-life lines (plan Task 2.1 Step 3): stops heartbeat + delivery rate
    lines.append(stops_proof_line())
    lines.append(delivery_line())
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

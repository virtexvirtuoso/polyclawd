#!/usr/bin/env python3
"""
Daily Paper Portfolio Report — Telegram-friendly tree format

Features:
- Tree structure (├─ └─ │) instead of bullet lists
- Emoji coding (🟢 won / 🔴 stopped / 🟡 open)
- TL;DR line for quick scanning
- Top edge% positions in open section

Sends via OpenClaw Gateway HTTP API to Telegram.
"""
import sqlite3
import sys
import os
import json
import html
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Any
from scripts.alert_formatter import send_telegram

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "storage" / "shadow_trades.db"
def _db():
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.execute("PRAGMA busy_timeout=8000")
    conn.row_factory = sqlite3.Row
    return conn


def get_portfolio_summary() -> Dict[str, Any]:
    """Fetch portfolio stats and positions."""
    conn = _db()

    # Bankroll
    bankroll_row = conn.execute(
        "SELECT bankroll FROM paper_portfolio_state ORDER BY id DESC LIMIT 1"
    ).fetchone()
    bankroll = bankroll_row["bankroll"] if bankroll_row else 10000.0

    # Today's resolutions (midnight to now UTC)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_row = conn.execute("""
        SELECT COUNT(*) as n, SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, SUM(pnl) as pnl
        FROM paper_positions
        WHERE DATE(closed_at) = ? AND status != 'open'
    """, (today,)).fetchone()
    today_resolved = today_row["n"] or 0
    today_wins = today_row["wins"] or 0
    today_pnl = today_row["pnl"] or 0.0

    # Cumulative stats
    cum_row = conn.execute("""
        SELECT COUNT(*) as n, SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins
        FROM paper_positions WHERE status != 'open'
    """).fetchone()
    cum_resolved = cum_row["n"] or 0
    cum_wins = cum_row["wins"] or 0
    cum_wr = (cum_wins / cum_resolved * 100) if cum_resolved else 0.0

    # Position counts by status
    status_counts = {}
    for row in conn.execute("""
        SELECT status, COUNT(*) as n FROM paper_positions GROUP BY status
    """):
        status_counts[row["status"]] = row["n"]

    # Recent activity (last 24h)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    won = conn.execute("""
        SELECT id, market_title, pnl, entry_price, side
        FROM paper_positions
        WHERE closed_at >= ? AND status = 'won'
        ORDER BY closed_at DESC LIMIT 10
    """, (cutoff,)).fetchall()

    stopped = conn.execute("""
        SELECT id, market_title, pnl, entry_price, side
        FROM paper_positions
        WHERE closed_at >= ? AND status = 'stopped'
        ORDER BY closed_at DESC LIMIT 10
    """, (cutoff,)).fetchall()

    open_pos = conn.execute("""
        SELECT id, market_title, entry_price, edge_pct, side, archetype
        FROM paper_positions
        WHERE status = 'open'
        ORDER BY edge_pct DESC LIMIT 10
    """).fetchall()

    conn.close()

    return {
        "bankroll": bankroll,
        "today_resolved": today_resolved,
        "today_wins": today_wins,
        "today_pnl": today_pnl,
        "cum_resolved": cum_resolved,
        "cum_wins": cum_wins,
        "cum_wr": cum_wr,
        "status_counts": status_counts,
        "won": [dict(r) for r in won],
        "stopped": [dict(r) for r in stopped],
        "open": [dict(r) for r in open_pos],
    }


def format_report(data: Dict[str, Any]) -> str:
    """Format as Telegram-friendly tree structure."""
    lines = []
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Header
    lines.append(f"**Paper Portfolio Daily P&L — {date}**")
    lines.append("")

    # TL;DR (top line for quick scanning)
    net_24h = sum(p["pnl"] or 0 for p in data["won"]) + sum(p["pnl"] or 0 for p in data["stopped"])
    tldr_sign = "+" if net_24h >= 0 else ""
    lines.append(f"**TL;DR:** {data['cum_wins']}W/{data['cum_resolved']} ({data['cum_wr']:.1f}%) · Net 24h: **{tldr_sign}${net_24h:,.2f}** · Bankroll: **${data['bankroll']:,.2f}**")
    lines.append("")

    # Key metrics
    lines.append("**Portfolio:**")
    lines.append(f"├─ Bankroll: ${data['bankroll']:,.2f}")
    today_losses = data['today_resolved'] - data['today_wins']
    pnl_sign = '+' if data['today_pnl'] >= 0 else ''
    lines.append(f"├─ Today: {data['today_wins']}W/{today_losses}L ({pnl_sign}${data['today_pnl']:,.2f})")
    lines.append(f"└─ Cumulative: {data['cum_wins']} / {data['cum_resolved']} ({data['cum_wr']:.1f}% WR)")
    lines.append("")

    # Position status
    lines.append("**Position Status:**")
    status_order = ["won", "stopped", "lost", "closed_manual", "displaced", "open"]
    active_statuses = [s for s in status_order if data["status_counts"].get(s, 0) > 0]
    for i, status in enumerate(active_statuses):
        count = data["status_counts"][status]
        is_last = (i == len(active_statuses) - 1)
        prefix = "└─ " if is_last else "├─ "
        lines.append(f"{prefix}{status}: {count}")
    lines.append("")

    # Recent activity (last 24h)
    lines.append("**Recent Activity (24h):**")

    won_total = sum(p["pnl"] or 0 for p in data["won"])
    stopped_total = sum(p["pnl"] or 0 for p in data["stopped"])

    sections = []

    # Won section
    if data["won"]:
        won_lines = [f"🟢 Won: {len(data['won'])} positions (+${won_total:,.2f})"]
        for p in data["won"][:5]:
            raw_title = p["market_title"][:50] if p["market_title"] else "Unknown"
            title = html.escape(raw_title, quote=False)
            won_lines.append(f"   ├─ {p['id']}: {title} → +${p['pnl']:,.2f}")
        sections.append(("won", won_lines))

    # Stopped section
    if data["stopped"]:
        stopped_lines = [f"🔴 Stopped: {len(data['stopped'])} positions (${stopped_total:,.2f})"]
        for p in data["stopped"][:5]:
            raw_title = p["market_title"][:50] if p["market_title"] else "Unknown"
            title = html.escape(raw_title, quote=False)
            stopped_lines.append(f"   ├─ {p['id']}: {title} → ${p['pnl']:,.2f}")
        sections.append(("stopped", stopped_lines))

    # Open section
    if data["open"]:
        open_lines = [f"🟡 Open: {len(data['open'])} positions"]
        for p in data["open"][:5]:
            raw_title = p["market_title"][:45] if p["market_title"] else "Unknown"
            title = html.escape(raw_title, quote=False)
            edge_str = f" | Edge: {p['edge_pct']:.1f}%" if p.get("edge_pct") else ""
            open_lines.append(f"   ├─ {p['id']}: {title} @ {p['entry_price']:.2f}{edge_str}")
        sections.append(("open", open_lines))

    # Join sections with proper tree connectors
    for i, (section_name, section_lines) in enumerate(sections):
        is_last_section = (i == len(sections) - 1)
        for j, line in enumerate(section_lines):
            if j == 0:
                # Section header
                prefix = "└─ " if is_last_section else "├─ "
                lines.append(f"{prefix}{line}")
            else:
                # Section items
                prefix = "   " if is_last_section else "│  "
                # Fix last item in section
                if j == len(section_lines) - 1:
                    line = line.replace("├─", "└─")
                lines.append(f"{prefix}{line}")

    lines.append("")
    lines.append(f"**Net (24h): {'+' if net_24h >= 0 else ''}${net_24h:,.2f}**")

    return "\n".join(lines)


def main():
    """Generate and send daily report."""
    print("Fetching portfolio data...")
    data = get_portfolio_summary()

    if data["cum_resolved"] == 0:
        print("No resolved trades yet — skipping report")
        return

    print("Formatting report...")
    report = format_report(data)

    print("Sending to Telegram...")
    success = send_telegram(report)

    if success:
        print("✅ Report sent successfully")
    else:
        print("❌ Failed to send report (gateway may be down)")

    # Also print to stdout for logging
    print("\n--- REPORT ---")
    print(report)


if __name__ == "__main__":
    main()

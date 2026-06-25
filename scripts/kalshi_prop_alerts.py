#!/usr/bin/env python3
"""
kalshi_prop_alerts.py — Scan Kalshi MLB props vs Pinnacle and send Telegram alerts.

Wraps the existing get_kalshi_prop_scan() with novice-friendly Telegram output.
Only fires when STRONG signals (book + L10 agree) are found.

Usage:
  python3 scripts/kalshi_prop_alerts.py [--min-edge 3.0] [--dry-run]

Cron (run once per hour during game windows):
  0 14-23 * * * cd /var/www/.../polyclawd && python3 scripts/kalshi_prop_alerts.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from scripts.alert_formatter import send_telegram

SIGNAL_EMOJI = {
    "STRONG_YES": "🔥",
    "STRONG_NO":  "🔥",
    "BUY_YES":    "📈",
    "BUY_NO":     "📉",
}

PROP_LABELS = {
    "HR":  "home run",
    "KS":  "strikeout",
    "HIT": "hit",
}


def _format_alert(results: list, maker_subsidized: bool) -> str:
    """Build a novice-friendly Telegram message from scan results."""
    strong = [r for r in results if "STRONG" in r["signal"]]
    other  = [r for r in results if "STRONG" not in r["signal"]]

    lines = ["⚾ <b>KALSHI MLB PROP EDGES</b>", ""]

    if strong:
        lines.append("🔥 <b>STRONG SIGNALS</b> (sportsbook + recent games agree):")
        lines.append("")
        for r in strong:
            _add_row(lines, r, maker_subsidized)

    if other:
        if strong:
            lines.append("")
        lines.append("📊 <b>BOOK-ONLY SIGNALS</b> (sportsbook edge, recent games neutral):")
        lines.append("")
        for r in other[:5]:  # cap at 5 book-only
            _add_row(lines, r, maker_subsidized)

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━")
    lines.append(f"💡 <b>How to read this:</b>")
    lines.append("Edge = how much cheaper Kalshi is vs Vegas. Positive = BUY YES on Kalshi.")
    lines.append("Taker fee ≈ 2-4¢ per contract. Maker orders are fee-free.")
    if maker_subsidized:
        lines.append("✅ <b>Maker incentive program active</b> — post limit orders to earn rebates.")

    return "\n".join(lines)


def _add_row(lines: list, r: dict, maker_subsidized: bool) -> None:
    prop_label = PROP_LABELS.get(r["prop_type"], r["prop_type"])
    signal = r["signal"]
    emoji = SIGNAL_EMOJI.get(signal, "")
    direction = "YES" if "YES" in signal else "NO"
    action = "BUY" if "YES" in signal else "SELL"

    edge = r["edge_vs_book_pct"]
    fee_adj = r["fee_adj_edge_pct"]
    kal_mid = r["kalshi_mid"]
    book_ip = r["avg_book_ip"]
    l10 = r["l10_hit_rate"]
    l10_games = r["l10_games"]

    lines.append(
        f"{emoji} <b>{r['player']}</b> — {r['prop']} {prop_label}"
    )
    lines.append(
        f"   Kalshi mid: <b>{kal_mid:.0f}¢</b>  |  Vegas (Pinnacle): <b>{book_ip:.0f}¢</b>"
    )
    lines.append(
        f"   Edge: <b>{edge:+.1f}pts</b>  (after fee: {fee_adj:+.1f}pts)"
    )
    if l10 is not None:
        lines.append(
            f"   Last {l10_games} games hit rate: <b>{l10:.0f}%</b>"
        )
    lines.append(
        f"   → <b>{action} {direction}</b> on Kalshi at {kal_mid:.0f}¢"
    )
    lines.append("")


async def main(min_edge: float = 3.0, dry_run: bool = False) -> None:
    from odds.kalshi_props import get_kalshi_prop_scan

    print(f"[kalshi_prop_alerts] Scanning (min_edge={min_edge}%)", flush=True)
    payload = await get_kalshi_prop_scan(min_edge_pct=min_edge, last_n=10)

    results = payload.get("results", [])
    error = payload.get("error")

    if error:
        print(f"[kalshi_prop_alerts] Error: {error}", flush=True)
        return

    print(f"[kalshi_prop_alerts] {len(results)} signal(s) found", flush=True)

    if not results:
        print("[kalshi_prop_alerts] Nothing to alert.", flush=True)
        return

    msg = _format_alert(results, payload.get("maker_subsidized", False))

    if dry_run:
        print("=== DRY RUN ===")
        print(msg)
        return

    send_telegram(msg)
    print("[kalshi_prop_alerts] Alert sent.", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-edge", type=float, default=3.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(main(min_edge=args.min_edge, dry_run=args.dry_run))

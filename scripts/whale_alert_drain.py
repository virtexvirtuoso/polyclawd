#!/usr/bin/env python3
"""Drain new CRITICAL/HIGH whale alerts for the OpenClaw polyclawd Telegram cron.

Cursor-based: each run prints only alerts newer than the last drained id,
then advances the cursor. No new alerts -> prints NO_NEW_WHALE_ALERTS so the
cron agent can reply NO_REPLY and suppress Telegram delivery.

Usage:
    python3 scripts/whale_alert_drain.py            # drain + advance cursor
    python3 scripts/whale_alert_drain.py --peek     # show without advancing
"""

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB_PATH = BASE / "storage" / "whale_scanner.db"
CURSOR_PATH = BASE / "storage" / "whale_alert_cursor.txt"


DASHBOARD_URL = "https://virtuosocrypto.com/polyclawd/whale-flow.html"


def market_link(platform: str, market: str) -> str:
    if platform == "kalshi":
        # series-prefix URL — full tickers 404 on kalshi.com
        return f"https://kalshi.com/markets/{market.split('-')[0]}"
    return f"https://polymarket.com/market/{market}"


def usd_bar(flow_d: float, width: int = 10) -> str:
    """Log-scale dollar magnitude bar: $10 = 0 blocks, $100k = full."""
    import math

    if flow_d < 10:
        filled = 0
    else:
        filled = min(width, round((math.log10(flow_d) - 1) / 4 * width))
    return "▰" * filled + "▱" * (width - filled)


def closes_in(close_iso: str) -> str:
    try:
        close = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
        hours = (close - datetime.now(timezone.utc)).total_seconds() / 3600
    except (ValueError, AttributeError):
        return ""
    if hours < 0:
        return "closed"
    if hours < 2:
        return f"closes in {hours * 60:.0f}min"
    if hours < 48:
        return f"closes in {hours:.0f}h"
    return f"closes in {hours / 24:.0f}d"


def _alert_direction(p: dict, reasons: str) -> tuple:
    """Return (direction_emoji, direction_text, flow_dollars)."""
    fy = p.get("flow_yes") or 0
    fn = p.get("flow_no") or 0
    flow_d = p.get("flow_dollars") or 0

    if "taker_NO" in reasons and "taker_YES" not in reasons:
        return ("🔴", "BET NO", flow_d)
    if "taker_YES" in reasons and "taker_NO" not in reasons:
        return ("🟢", "BET YES", flow_d)
    if fn >= 2 * fy and fn > 0:
        return ("🔴", "BET NO", flow_d)
    if fy >= 2 * fn and fy > 0:
        return ("🟢", "BET YES", flow_d)
    return ("", "NO SIGNAL", flow_d)


def _action_price(p: dict) -> str:
    bid = p.get("best_bid")
    ask = p.get("best_ask")
    if bid is not None and ask is not None:
        bid_cents = int(bid * 100)
        ask_cents = int(ask * 100)
        return f"@{bid_cents}¢/{ask_cents}¢"
    elif p.get("current_price") is not None:
        cents = int(p["current_price"] * 100)
        return f"@{cents}¢"
    return ""


def format_alert(row, p: dict) -> str:
    sev = row["severity"]
    when = datetime.fromtimestamp(row["ts"], tz=timezone.utc).strftime("%H:%M UTC")

    name = p.get("title", "") or row["market"]
    sub = p.get("sub_title", "")
    ticker = row["market"]

    dir_emoji, dir_text, flow_d = _alert_direction(p, row["reasons"])
    action_px = _action_price(p)
    ci = closes_in(p.get("close_time", ""))

    # Line 1: Market title (leads the alert)
    title_line = name
    if sub:
        title_line += f" [{sub}]"
    lines = [title_line]

    # Line 2: DIRECTION + PRICE + score + time
    badge = (
        f"🚨 {sev} {row['score']}/10"
        if sev == "CRITICAL"
        else f"⚠️ {sev} {row['score']}/10"
    )
    lines.append(f"{dir_emoji} {dir_text} {action_px}    {badge} | {when}")

    # Line 3: Mid price + depth + close (no ticker)
    px_bits = []
    if p.get("best_bid") is not None and p.get("best_ask") is not None:
        bid = p["best_bid"]
        ask = p["best_ask"]
        mid = (bid + ask) / 2
        px_bits.append(f"mid {mid:.2f}")
    if p.get("bid_depth") is not None:
        bid_k = p['bid_depth'] / 1000
        ask_k = p['ask_depth'] / 1000
        px_bits.append(f"depth ${bid_k:.0f}K/${ask_k:.0f}K")
    if ci:
        px_bits.append(ci)
    if px_bits:
        lines.append(" | ".join(px_bits))

    # Line 4: Flow summary
    fy = p.get("flow_yes") or 0
    fn = p.get("flow_no") or 0
    reasons = row["reasons"]
    flow_emoji = "🟢" if fy > fn else ("🔴" if fn > fy else "")
    flow_summary = (
        f"Flow: {flow_emoji} {fy:,.0f}Y / {fn:,.0f}N | ${flow_d:,.0f} {usd_bar(flow_d)}"
    )

    # Add taker % if present
    for part in reasons.split(","):
        part = part.strip()
        if part.startswith("taker_YES_"):
            pct = part.split("_")[-1].replace("%", "")
            flow_summary += f" | {pct}% taker YES"
        elif part.startswith("taker_NO_"):
            pct = part.split("_")[-1].replace("%", "")
            flow_summary += f" | {pct}% taker NO"
    lines.append(flow_summary)

    # Line 6: Human-readable key signal (no raw codes)
    key_signal = ""
    if "imbalance_flip" in reasons:
        key_signal += "Book flipped one-sided. "
    if "level_jump_bid" in reasons:
        key_signal += "Fresh bid wall appeared. "
    elif "level_jump_ask" in reasons:
        key_signal += "Fresh ask wall appeared. "
    if "spread_collapse" in reasons:
        key_signal += "Spread snapped tight (pro quoting). "
    if "depth_surge_B" in reasons:
        key_signal += "Bid depth surged. "
    if "depth_surge_A" in reasons:
        key_signal += "Ask depth surged. "
    if "smart_wallet" in reasons:
        key_signal += (
            f"Tracked wallet active: {p.get('top_wallet_name', '') or 'known winner'}. "
        )
    if key_signal:
        lines.append(key_signal.strip())

    lines.append(market_link(row["platform"], ticker))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--peek",
        action="store_true",
        help="show pending alerts without advancing the cursor",
    )
    args = parser.parse_args()

    if not DB_PATH.exists():
        print("NO_NEW_WHALE_ALERTS")
        return

    cursor = 0
    if CURSOR_PATH.exists():
        try:
            cursor = int(CURSOR_PATH.read_text().strip() or 0)
        except ValueError:
            cursor = 0

    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM whale_alerts WHERE id > ? AND severity = 'CRITICAL' ORDER BY id",
        (cursor,),
    ).fetchall()
    max_id_row = conn.execute("SELECT MAX(id) AS m FROM whale_alerts").fetchone()
    conn.close()

    if not rows:
        if not args.peek and max_id_row["m"]:
            CURSOR_PATH.write_text(str(max_id_row["m"]))
        print("NO_NEW_WHALE_ALERTS")
        return

    MAX_FULL = 8
    # Parse payloads once; rank by REAL dollars, not score — score saturates
    # at 10 on game-day churn (2026-06-11: 1,732 CRITICALs in 24h, all 10/10).
    parsed = []
    for r in rows:
        try:
            payload = json.loads(r["payload"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        parsed.append((r, payload, payload.get("flow_dollars") or 0))
    parsed.sort(key=lambda x: -x[2])

    total_usd = sum(d for _, _, d in parsed)
    print(f"🦈 WHALE SHARK — {len(rows)} alert(s) | ≈${total_usd:,.0f} total flow\n")
    for r, payload, _ in parsed[:MAX_FULL]:
        print(format_alert(r, payload))
        print()
    if len(rows) > MAX_FULL:
        hidden_usd = sum(d for _, _, d in parsed[MAX_FULL:])
        print(
            f"(+{len(rows) - MAX_FULL} more ≈${hidden_usd:,.0f} — full tape: {DASHBOARD_URL})"
        )
    else:
        print(f"live tape: {DASHBOARD_URL}")

    if not args.peek:
        CURSOR_PATH.write_text(str(max_id_row["m"]))


if __name__ == "__main__":
    main()

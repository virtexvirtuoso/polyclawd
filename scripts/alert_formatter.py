#!/usr/bin/env python3
"""
Shared alert formatter — two-tier format for all signal types.

Tier 1 (newcomer): clean, spaced, explains what happened
Tier 2 (power user): compact data line with raw metrics

Usage:
    from scripts.alert_formatter import format_alert, send_telegram

    msg = format_alert(
        alert_type="insider",       # "insider" | "ufc" | "arb" | "whale"
        rank=1,
        emoji="🚨",
        title="Insider detected on Polymarket",
        direction="YES",
        price_cents=65,
        action="Bought $50,000 YES · 3 min before move",
        signal_score="95/100",
        close_info="Resolves Jun 20",
        data_line="Wallet age 2h · Bet size $50K · 100% concentration",
        links=["<a href='...'>Polymarket</a>"],
        tags=["🔴 CLOSING SOON"],
    )
    send_telegram(msg)
"""

import os
import re

TELEGRAM_CHAT_ID = "468298295"


def format_alert(
    alert_type: str,
    rank: int = 1,
    emoji: str = "📊",
    title: str = "",
    direction: str = "",
    price_cents: int | None = None,
    action: str = "",
    signal_score: str = "",
    close_info: str = "",
    data_line: str = "",
    links: list[str] | None = None,
    tags: list[str] | None = None,
) -> str:
    """Two-tier alert format — newcomer-friendly + power user data."""
    lines = []

    # ── Tags ─────────────────────────────────────────────────────────
    if tags:
        lines.append(" · ".join(tags))

    # ── Header ───────────────────────────────────────────────────────
    lines.append(f"{emoji} <b>#{rank}</b> · {alert_type.upper()}")

    # ── Blank line after header ──────────────────────────────────────
    lines.append("")

    # ── Title ────────────────────────────────────────────────────────
    lines.append(title)

    # ── Price ─────────────────────────────────────────────────────────
    if direction and price_cents is not None:
        lines.append(f"{direction} @ {price_cents}¢")
    elif price_cents is not None:
        lines.append(f"{price_cents}¢")

    # ── Action (what happened) ───────────────────────────────────────
    if action:
        lines.append(action)

    # ── Signal + close time ─────────────────────────────────────────
    info_parts = []
    if signal_score:
        info_parts.append(f"⭐ {signal_score}")
    if close_info:
        info_parts.append(close_info)
    if info_parts:
        lines.append(" · ".join(info_parts))

    # ── Power user data line ─────────────────────────────────────────
    if data_line:
        lines.append("")
        lines.append(f"📊 {data_line}")

    # ── Links ────────────────────────────────────────────────────────
    if links:
        lines.append(" · ".join(links))

    return "\n".join(lines)


def send_telegram(message: str) -> bool:
    """Formatter delivery — delegates to the ONE hardened send path
    (scripts.openclaw_alerts.alert_openclaw: ledger + err detail +
    transient-only retry). Kept for API compat with format_alert callers."""
    return _send_telegram_inner(message)


def _send_telegram_inner(message: str) -> bool:
    """Send a Telegram message through alert_openclaw in HTML mode; if the
    HTML parse is rejected (raw </>/& interpolated by a caller — Telegram
    400s 'can't parse entities'), degrade to plain text with tags stripped
    rather than dropping the alert (same pattern as whale_alert_drain).
    Every attempt lands in the send ledger with an err class — never a
    silent swallow."""
    # Never fire during pytest runs — PYTEST_CURRENT_TEST is set before any module import
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("SMART_WALLET_ALERT_SEND") == "0":
        return True
    from scripts.openclaw_alerts import alert_openclaw

    if alert_openclaw(message, parse_mode="HTML"):
        return True
    return alert_openclaw(re.sub(r"<[^>]+>", "", message), parse_mode=None)

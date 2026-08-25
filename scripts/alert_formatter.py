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

import html
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
    """Two-tier alert format — newcomer-friendly + power user data.

    All caller-supplied text fields are HTML-escaped here (the message is
    sent with parse_mode=HTML; raw </>/& in market titles 400 at Telegram —
    "can't parse entities"). `links` is the one exception: documented as
    containing intentional <a> markup, passed through verbatim.
    """
    esc = lambda s: html.escape(str(s), quote=False)  # noqa: E731
    lines = []

    # ── Tags ─────────────────────────────────────────────────────────
    if tags:
        lines.append(" · ".join(esc(t) for t in tags))

    # ── Header ───────────────────────────────────────────────────────
    lines.append(f"{esc(emoji)} <b>#{rank}</b> · {esc(alert_type.upper())}")

    # ── Blank line after header ──────────────────────────────────────
    lines.append("")

    # ── Title ────────────────────────────────────────────────────────
    lines.append(esc(title))

    # ── Price ─────────────────────────────────────────────────────────
    if direction and price_cents is not None:
        lines.append(f"{esc(direction)} @ {price_cents}¢")
    elif price_cents is not None:
        lines.append(f"{price_cents}¢")

    # ── Action (what happened) ───────────────────────────────────────
    if action:
        lines.append(esc(action))

    # ── Signal + close time ─────────────────────────────────────────
    info_parts = []
    if signal_score:
        info_parts.append(f"⭐ {esc(signal_score)}")
    if close_info:
        info_parts.append(esc(close_info))
    if info_parts:
        lines.append(" · ".join(info_parts))

    # ── Power user data line ─────────────────────────────────────────
    if data_line:
        lines.append("")
        lines.append(f"📊 {esc(data_line)}")

    # ── Links ────────────────────────────────────────────────────────
    if links:
        lines.append(" · ".join(links))

    return "\n".join(lines)


def format_grid(header: list[str], rows: list[list[str]]) -> str:
    """Render a monospace-aligned table as a Telegram <pre> block.

    This is the ONLY way to get true column alignment in Telegram — normal
    text is proportional-font, so space-padding never lines up. <pre> is
    monospace, but you CANNOT nest <b>/<i> inside it (bold is sacrificed
    inside the table).

    Rules:
      - Every cell is HTML-escaped (a team name with < or & inside <pre>
        still breaks the parse).
      - First column is left-aligned (labels); remaining columns are
        right-aligned (numbers).
      - Emoji are double-width in monospace and WILL break alignment — keep
        them OUT of the aligned columns. Use ASCII markers (^/v, +/-, <-)
        inside the table, or put the emoji in the HTML header line above it.

    Returns the full "<pre>...</pre>" block (already escaped).
    """
    esc = lambda s: html.escape(str(s), quote=False)  # noqa: E731
    ncols = max([len(header)] + [len(r) for r in rows])
    norm = [list(r) + [""] * (ncols - len(r)) for r in ([header] + rows)]
    widths = [0] * ncols
    for r in norm:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(esc(cell)))
    out = []
    for r in norm:
        cells = []
        for i, cell in enumerate(r):
            s = esc(cell)
            cells.append(s.ljust(widths[i]) if i == 0 else s.rjust(widths[i]))
        out.append("  ".join(cells).rstrip())
    return "<pre>" + "\n".join(out) + "</pre>"


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

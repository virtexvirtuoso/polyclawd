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


def _human_reasons(reasons: str) -> str:
    """Convert raw reason codes to a short human-readable line."""
    parts = []
    for r in reasons.split(","):
        r = r.strip()
        if r.startswith("vol_spike_"):
            try: parts.append(f"Vol {int(float(r.split('_')[-1]))/1000:.0f}K")
            except: pass
        elif r.startswith("vol_move_"):
            try: parts.append(f"Vol +{int(float(r.split('_')[-1]))/1000:.0f}K")
            except: pass
        elif r.startswith("oi_spike_"):
            try: parts.append(f"OI +{int(float(r.split('_')[-1]))/1000:.0f}K")
            except: pass
        elif r.startswith("taker_YES_"):
            try: parts.append(f"{r.split('_')[-1].replace('%','')}% taker YES")
            except: pass
        elif r.startswith("taker_NO_"):
            try: parts.append(f"{r.split('_')[-1].replace('%','')}% taker NO")
            except: pass
        elif r.startswith("level_jump_bid_"):
            try: parts.append(f"Bid wall ${int(float(r.split('_')[-1]))/1000:.0f}K")
            except: pass
        elif r.startswith("level_jump_ask_"):
            try: parts.append(f"Ask wall ${int(float(r.split('_')[-1]))/1000:.0f}K")
            except: pass
        elif r.startswith("flow_mag_"):
            try: parts.append(f"${int(float(r.split('_')[-1]))/1000:.0f}K flow")
            except: pass
        elif r.startswith("whale_flow_pierce_"):
            try: parts.append(f"Whale ${int(float(r.split('_')[-1]))/1000:.0f}K")
            except: pass
        elif r.startswith("intensity_"):
            try: parts.append(f"{r.split('_')[-1].replace('%','')}% intensity")
            except: pass
        elif r == "aggressive_taker_100%":
            parts.append("100% aggressive")
        elif r == "bilateral_moderate":
            parts.append("Both sides")
        elif r == "imbalance_flip":
            parts.append("Book flipped")
        elif r == "spread_collapse":
            parts.append("Spread tight")
        elif r == "smart_wallet":
            parts.append("Smart wallet")
        elif r.startswith("class_outlier_"):
            pass
    return " · ".join(parts[:3])


def _implication(score: float, flow: float, fy: float, fn: float, bid, ask, reasons: str, dir_text: str) -> str:
    """Generate a one-line implication: what this alert means for a trader."""
    parts = []

    total = fy + fn
    yes_pct = fy / total * 100 if total > 0 else 50

    is_aggressive = "aggressive_taker" in reasons
    is_whale = "whale_flow_pierce" in reasons
    is_smart = "smart_wallet" in reasons
    tight_spread = False
    if bid is not None and ask is not None:
        tight_spread = (ask - bid) / ((bid + ask) / 2) * 100 < 2

    if score >= 9 and is_whale and is_aggressive and tight_spread:
        parts.append("🟢 STRONG ENTRY")
    elif score >= 9 and is_whale and tight_spread:
        parts.append("🟢 ENTRY")
    elif score >= 9 and is_aggressive and tight_spread:
        parts.append("🟢 ENTRY")
    elif score >= 9 and tight_spread:
        parts.append("🟡 WATCH")
    elif score >= 7 and is_whale:
        parts.append("🟡 WATCH")
    elif score >= 7:
        parts.append("🔵 MONITOR")
    else:
        parts.append("⚪ NOISE")

    edge_parts = []
    if is_whale:
        edge_parts.append("whale")
    if is_aggressive:
        edge_parts.append("aggressive")
    if is_smart:
        edge_parts.append("smart money")
    if tight_spread:
        edge_parts.append("tight")
    if edge_parts:
        parts.append(" · ".join(edge_parts))

    if yes_pct >= 90:
        parts.append("99%+ YES")
    elif yes_pct >= 70:
        parts.append(f"{yes_pct:.0f}% YES")
    elif yes_pct <= 10:
        parts.append("99%+ NO")
    elif yes_pct <= 30:
        parts.append(f"{100-yes_pct:.0f}% NO")

    if flow >= 100000:
        parts.append(f"${flow/1000:.0f}K")
    elif flow >= 50000:
        parts.append(f"${flow/1000:.0f}K")

    if not parts:
        return ""
    return "💡 " + " · ".join(parts)


def _infer_category(title: str, ticker: str = "") -> str:
    tl = title.lower()
    tk = ticker.upper()
    if any(tk.startswith(p) for p in ["KXWNBA", "KXNBA", "KXNFL", "KXNHL", "KXMLB", "KXNCAA"]):
        return "🏀"
    if any(w in tl for w in ["election", "president", "senate", "house ", "governor", "democrat", "republican"]):
        return "🏛️"
    if any(w in tl for w in ["win the", "match?", "round of", "wta", "atp", "grand slam", "qualification"]):
        return "🎾"
    if any(w in tl for w in [" vs ", "goal", "draw", "fc ", "united", "city ", "real ", "juventus", "liverpool", "bayern", "psg", "barcelona"]):
        return "⚽"
    if any(w in tl for w in ["bitcoin", "eth", "crypto", "btc", "sol", "price of"]):
        return "₿"
    if any(w in tl for w in ["temperature", "climate", "weather", "co2", "emission"]):
        return "🌡️"
    return "📊"


def _short_title(title: str) -> str:
    import re
    # Try to extract "X vs Y" pattern — prefer capitalized names
    m = re.search(r'([A-Z][a-z]+(?: [A-Z][a-z]+)*)\s+vs\s+([A-Z][a-z]+(?: [A-Z][a-z]+)*)', title)
    if m:
        return f"{m.group(1).strip()} vs {m.group(2).strip()}"
    # "Will X [verb] ... vs Y" → extract X and Y
    m = re.match(r'Will (?:the |a |an )?(.+?) (?:score|beat|defeat|reach|make|have).+?vs\s+([A-Za-z][A-Za-z .\'-]+)', title)
    if m:
        return f"{m.group(1).strip()} vs {m.group(2).strip()}"
    # Broader vs match: anything before/after 'vs'
    m = re.search(r'([A-Za-z][A-Za-z .\'-]+?)\s+vs\s+([A-Za-z][A-Za-z .\'-]+)', title)
    if m:
        return f"{m.group(1).strip()} vs {m.group(2).strip()}"
    # Last resort: split on ' vs '
    if ' vs ' in title:
        parts = title.split(' vs ')
        left = parts[0].split()[-1] if parts[0].split() else ''
        right = parts[1].split()[0] if parts[1].split() else ''
        if left and right:
            return f"{left} vs {right}"
    m = re.match(r'Will (?:the |a |an )?(.+?) win(?:s|$|\s)', title)
    if m and 'final score' not in m.group(1).lower():
        name = m.group(1)
        if len(name) > 40:
            name = name[:37] + "..."
        return name
    # "Will the final score be X" → "X"
    m = re.match(r'Will the final score be (.+?)[?]?$', title)
    if m:
        return m.group(1).strip()
    m = re.match(r'Will (?:the |a |an )?(.+?) (?:score|beat|defeat|reach|make|have)\s+(?:a |the |an |vs |to |in |on |at |for )', title)
    if m:
        name = m.group(1)
        if len(name) > 40:
            name = name[:37] + "..."
        return name
    if len(title) > 50:
        return title[:47] + "..."
    return title



def _signal_tier(reasons: str) -> tuple:
    """Return (emoji, label) for signal quality tier.
    MEGA FILL > WHALE FILL > FLOW BURST > BOOK SIGNAL."""
    if 'mega_single_trade' in reasons:
        return ('⚡', 'MEGA FILL')
    if 'whale_single_trade' in reasons:
        return ('🐋', 'WHALE FILL')
    if ('flow_mag' in reasons or 'vol_spike' in reasons) and (
            'taker_YES' in reasons or 'taker_NO' in reasons or 'taker_BUY' in reasons):
        return ('📈', 'FLOW BURST')
    return ('📖', 'BOOK SIGNAL')


def _flow_label(fy: float, fn: float) -> str:
    """'one-sided', '85% YES', or '' for at-a-glance flow direction summary."""
    total = (fy or 0) + (fn or 0)
    if total < 500:
        return ''
    if not fn or fn <= 0:
        return 'one-sided'
    if not fy or fy <= 0:
        return 'one-sided'
    ratio = max(fy, fn) / total
    if ratio >= 0.85:
        dominant = 'YES' if fy > fn else 'NO'
        return f'{ratio * 100:.0f}% {dominant}'
    return ''


def format_alert_compact(row, p: dict) -> str:
    """One-line summary for secondary alerts in a batch."""
    reasons = row["reasons"]
    tier_emoji, _ = _signal_tier(reasons)
    cat_emoji = _infer_category(p.get("title", "") or row["market"], row["market"])
    short = _short_title(p.get("title", "") or row["market"])
    dir_emoji, dir_text, flow_d = _alert_direction(p, reasons)
    bid = p.get("best_bid")
    bid_c = int(bid * 100) if bid is not None else None
    signal = _human_reasons(reasons)
    parts = [short]
    price_str = f"{bid_c}¢" if bid_c is not None else ""
    if dir_emoji and dir_text != "NO SIGNAL":
        parts.append(f"{dir_emoji} {price_str}")
    if flow_d:
        parts.append(f"${flow_d:,.0f}")
    if signal:
        parts.append(signal)
    return f"{tier_emoji} {cat_emoji} {' · '.join(parts)}"


def format_alert(row, p: dict) -> str:
    """Action-first alert. Tier on line 1, decision on line 3, signal on line 4."""
    reasons = row["reasons"]
    tier_emoji, tier_label = _signal_tier(reasons)
    cat_emoji = _infer_category(p.get("title", "") or row["market"], row["market"])
    platform_name = row["platform"].capitalize()
    short = _short_title(p.get("title", "") or row["market"])
    ticker = row["market"]

    dir_emoji, dir_text, flow_d = _alert_direction(p, reasons)
    bid = p.get("best_bid")
    ask = p.get("best_ask")
    mid = p.get("mid", 0) or ((bid or 0) + (ask or 0)) / 2
    bid_c = int(bid * 100) if bid is not None else None
    ask_c = int(ask * 100) if ask is not None else None
    spread_pct = (ask - bid) / mid * 100 if (bid is not None and ask is not None and mid) else None

    fy = p.get("flow_yes", 0) or 0
    fn = p.get("flow_no", 0) or 0
    score = row["score"]
    ci = closes_in(p.get("close_time", ""))
    signal = _human_reasons(reasons)
    flow_lbl = _flow_label(fy, fn)

    imp = _implication(score, flow_d, fy, fn, bid, ask, reasons, dir_text)
    if imp:
        # "💡 🟢 STRONG ENTRY · whale · tight" → "🟢 STRONG ENTRY"
        action_label = imp.replace("💡 ", "").split(" · ")[0].strip()
    else:
        action_label = f"{dir_emoji} {dir_text}" if (dir_emoji and dir_text != "NO SIGNAL") else ""

    lines = []

    # L1: Signal tier · category · platform
    lines.append(f"{tier_emoji} <b>{tier_label}</b> · {cat_emoji} {platform_name}")

    # L2: Market name
    lines.append(short)
    lines.append("")

    # L3: THE decision line — action + price + flow (most important, read first)
    decision_parts = []
    if action_label:
        decision_parts.append(action_label)
    if bid_c is not None and ask_c is not None:
        decision_parts.append(f"{bid_c}¢/{ask_c}¢")
        if spread_pct is not None and spread_pct < 5:
            decision_parts.append(f"spread {spread_pct:.1f}%")
    elif bid_c is not None:
        decision_parts.append(f"{bid_c}¢")
    if flow_d:
        decision_parts.append(f"${flow_d:,.0f}")
    lines.append(" · ".join(decision_parts))

    # L4: Signal evidence + flow direction label
    sig_parts = []
    if signal:
        sig_parts.append(signal)
    if flow_lbl and "taker" not in signal:
        sig_parts.append(flow_lbl)
    if sig_parts:
        lines.append(" · ".join(sig_parts))

    # L5: Flow split + close time
    ctx_parts = []
    if fy or fn:
        ctx_parts.append(f"Y ${fy:,.0f} / N ${fn:,.0f}")
    if ci:
        ctx_parts.append(ci)
    if ctx_parts:
        lines.append(" · ".join(ctx_parts))

    lines.append("")

    # L6: Link only — dashboard available on tap
    lines.append(market_link(row["platform"], ticker))
    return "\n".join(lines)



def _send_telegram_message(text: str) -> bool:
    """Deliver via the fleet helper. HTML first (format uses <b>/<a> tags), then a
    tag-stripped plain-text retry so an unescaped & or < in a market title can't
    400 the whole batch. Telegram hard-caps messages at 4096 chars."""
    import re
    import sys as _s

    if str(BASE) not in _s.path:
        _s.path.insert(0, str(BASE))
    from scripts.openclaw_alerts import alert_openclaw

    if len(text) > 3900:
        text = text[:3890] + "\n…truncated"
    if alert_openclaw(text, parse_mode="HTML"):
        return True
    return alert_openclaw(re.sub(r"<[^>]+>", "", text), parse_mode=None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--peek",
        action="store_true",
        help="show pending alerts without advancing the cursor",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="deliver to Telegram; cursor only advances if delivery succeeds",
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

    conn = sqlite3.connect(str(DB_PATH), timeout=15)
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

    # Parse payloads; sort by signal tier first, then flow dollars.
    # Score saturates at 10 during game-day churn — tier is the quality signal.
    _TIER_ORDER = {'MEGA FILL': 0, 'WHALE FILL': 1, 'FLOW BURST': 2, 'BOOK SIGNAL': 3}
    parsed = []
    for r in rows:
        try:
            payload = json.loads(r["payload"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        parsed.append((r, payload, payload.get("flow_dollars") or 0))
    parsed.sort(key=lambda x: (_TIER_ORDER.get(_signal_tier(x[0]["reasons"])[1], 9), -x[2]))

    total_usd = sum(d for _, _, d in parsed)
    count = len(parsed)
    label = "alert" if count == 1 else "alerts"
    out = [f"🦈 {count} whale {label} · ≈${total_usd:,.0f} flow\n"]

    # Top pick — full detail
    top_row, top_payload, _ = parsed[0]
    out.append(format_alert(top_row, top_payload))

    # Secondary alerts — compact one-liners
    MAX_COMPACT = 5
    secondary = parsed[1:MAX_COMPACT + 1]
    if secondary:
        out.append("")
        out.append("─" * 16)
        for r, pl, _ in secondary:
            out.append(format_alert_compact(r, pl))
        if count > MAX_COMPACT + 1:
            hidden_n = count - MAX_COMPACT - 1
            hidden_usd = sum(d for _, _, d in parsed[MAX_COMPACT + 1:])
            out.append(f"  +{hidden_n} more ≈${hidden_usd:,.0f}")

    out.append(f"\n<a href='{DASHBOARD_URL}'>Full tape</a>")
    text = "\n".join(out)
    print(text)

    delivered = True
    if args.send:
        delivered = _send_telegram_message(text)
        print(f"[send] telegram ok={delivered}")

    # A failed --send must NOT advance the cursor — that silently drops the
    # batch forever (the pre-2026-07-06 consumer-gone failure mode).
    if not args.peek and delivered:
        CURSOR_PATH.write_text(str(max_id_row["m"]))


if __name__ == "__main__":
    main()

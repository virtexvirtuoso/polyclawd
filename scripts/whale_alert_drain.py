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


def format_alert(row, p: dict) -> str:
    sev = row["severity"]
    when = datetime.fromtimestamp(row["ts"], tz=timezone.utc).strftime("%H:%M UTC")

    name = p.get("title", "") or row["market"]
    ticker = row["market"]

    dir_emoji, dir_text, flow_d = _alert_direction(p, row["reasons"])
    action_px = _action_price(p)
    ci = closes_in(p.get("close_time", ""))

    bid = p.get("best_bid")
    ask = p.get("best_ask")
    mid = p.get("mid", 0) or ((bid or 0) + (ask or 0)) / 2
    bid_d = p.get("bid_depth", 0)
    ask_d = p.get("ask_depth", 0)
    oi = p.get("open_interest", 0)
    vol = p.get("volume", 0)
    score = row["score"]
    reasons = row["reasons"]
    fy = p.get("flow_yes", 0)
    fn = p.get("flow_no", 0)

    cat_emoji = _infer_category(name, ticker)
    short = _short_title(name)
    bid_c = int(bid * 100) if bid is not None else None
    ask_c = int(ask * 100) if ask is not None else None
    spread_pct = (ask - bid) / mid * 100 if (bid is not None and ask is not None and mid) else None
    oiv_ratio = oi / vol if (oi and vol) else 0

    # Line 1: Header
    lines = [f"{cat_emoji} <b>#{sev}</b>"]

    # Line 2: Short title
    lines.append(short)
    lines.append("")  # spacer

    # Line 3: Direction + Price + Flow + Score
    action_bits = []
    if dir_emoji and dir_text:
        action_bits.append(f"{dir_emoji} {dir_text}")
    if bid_c is not None and ask_c is not None:
        action_bits.append(f"{bid_c}¢/{ask_c}¢")
        if spread_pct is not None and spread_pct < 5:
            action_bits.append(f"spread {spread_pct:.1f}%")
    if flow_d:
        action_bits.append(f"${flow_d:,.0f}")
    action_bits.append(f"{score}/10")
    lines.append(" · ".join(action_bits))

    # Line 4: Flow breakdown + Depth
    flow_bits = []
    if fy or fn:
        flow_bits.append(f"Y ${fy:,.0f} / N ${fn:,.0f}")
    if bid_d or ask_d:
        flow_bits.append(f"D ${bid_d/1000:.0f}K/${ask_d/1000:.0f}K")
    if flow_bits:
        lines.append(" · ".join(flow_bits))

    # Line 5: OI + Close
    health_bits = []
    if oi:
        health_bits.append(f"OI ${oi/1000:.0f}K")
    if oiv_ratio > 0:
        health_bits.append(f"OIV {oiv_ratio:.2f}")
    if ci:
        health_bits.append(f"→ {ci}")
    if health_bits:
        lines.append(" · ".join(health_bits))
    lines.append("")  # spacer

    # Line 6: Implication
    imp = _implication(score, flow_d, fy, fn, bid, ask, reasons, dir_text)
    if imp:
        lines.append(imp)

    # Line 6: Trigger
    signal = _human_reasons(reasons)
    if signal:
        lines.append(signal)
    lines.append("")  # spacer

    # Line 7: Links
    lines.append(market_link(row["platform"], ticker))
    lines.append(f"<a href='{DASHBOARD_URL}'>Dashboard</a>")
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

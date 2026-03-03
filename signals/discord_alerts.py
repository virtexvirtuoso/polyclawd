"""
Discord Webhook Alerts for Polyclawd
Sends trade opens, resolutions, edge alerts, and scorecards to #prediction-alerts
"""

import json
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

WEBHOOK_URL = "https://discord.com/api/webhooks/1478409757386870875/ROLCqrUiJzbzDQj6bJu3RgbPEY-5pyFLqL2kfLV_GAeTlxkHh1D3JB1QiJNoICiTLuq-"
BOT_NAME = "Polyclawd"
AVATAR_URL = "https://virtuosocrypto.com/polyclawd/assets/logo.png"

# Colors
COLOR_GREEN = 0x2ECC71   # win / new position
COLOR_RED = 0xE74C3C     # loss
COLOR_BLUE = 0x3498DB    # edge signal
COLOR_GOLD = 0xF1C40F    # scorecard
COLOR_GRAY = 0x95A5A6    # void / neutral


def _send(embeds: list, content: str = "") -> bool:
    """Send a Discord webhook message with embeds."""
    payload = {
        "username": BOT_NAME,
        "avatar_url": AVATAR_URL,
        "embeds": embeds,
    }
    if content:
        payload["content"] = content

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Polyclawd/1.0 (Discord Webhook)",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        logger.debug("Discord alert sent: %d", resp.status)
        return True
    except Exception as e:
        logger.warning("Discord alert failed: %s", e)
        return False


def alert_position_opened(market_title: str, side: str, entry_price: float,
                          bet_size: float, strategy: str, edge_pct: float = 0,
                          **kwargs) -> bool:
    """Alert when a new paper position is opened."""
    emoji = "📈" if side == "YES" else "📉"
    color = COLOR_GREEN

    fields = [
        {"name": "Side", "value": f"**{side}**", "inline": True},
        {"name": "Entry", "value": f"{entry_price:.0%}", "inline": True},
        {"name": "Size", "value": f"${bet_size:.2f}", "inline": True},
        {"name": "Strategy", "value": strategy or "—", "inline": True},
        {"name": "Edge", "value": f"+{edge_pct:.1f}pp" if edge_pct else "—", "inline": True},
    ]

    return _send([{
        "title": f"{emoji} Position Opened",
        "description": market_title[:200],
        "color": color,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Paper Portfolio"},
    }])


def alert_position_closed(market_title: str, side: str, outcome: str,
                           pnl: float, entry_price: float, exit_price: float = 0,
                           strategy: str = "", **kwargs) -> bool:
    """Alert when a position resolves."""
    if outcome == "void":
        emoji, color, result = "⚪", COLOR_GRAY, "VOID"
    elif pnl > 0:
        emoji, color, result = "✅", COLOR_GREEN, "WIN"
    else:
        emoji, color, result = "❌", COLOR_RED, "LOSS"

    fields = [
        {"name": "Result", "value": f"**{result}**", "inline": True},
        {"name": "Side", "value": side, "inline": True},
        {"name": "P&L", "value": f"{'+'if pnl>=0 else ''}{pnl:.2f}", "inline": True},
        {"name": "Entry → Exit", "value": f"{entry_price:.0%} → {exit_price:.0%}" if exit_price else f"{entry_price:.0%}", "inline": True},
        {"name": "Strategy", "value": strategy or "—", "inline": True},
    ]

    return _send([{
        "title": f"{emoji} Position Resolved — {result}",
        "description": market_title[:200],
        "color": color,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Paper Portfolio"},
    }])


def alert_edge_signal(market_title: str, side: str, edge_pct: float,
                       price: float, strategy: str, platform: str = "",
                       **kwargs) -> bool:
    """Alert on a high-edge signal (>25pp)."""
    fields = [
        {"name": "Side", "value": f"**{side}**", "inline": True},
        {"name": "Edge", "value": f"**+{edge_pct:.1f}pp**", "inline": True},
        {"name": "Price", "value": f"{price:.0%}", "inline": True},
        {"name": "Strategy", "value": strategy or "—", "inline": True},
        {"name": "Platform", "value": platform or "—", "inline": True},
    ]

    return _send([{
        "title": "🎯 High Edge Signal",
        "description": market_title[:200],
        "color": COLOR_BLUE,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Signal Scanner"},
    }])


def alert_scorecard(strategy: str, n: int, brier: float, win_rate: float,
                     avg_edge: float = 0, **kwargs) -> bool:
    """Alert with calibration scorecard summary."""
    grade = "🟢" if brier < 0.20 else "🟡" if brier < 0.25 else "🔴"

    fields = [
        {"name": "Resolutions", "value": str(n), "inline": True},
        {"name": "Brier Score", "value": f"{grade} {brier:.3f}", "inline": True},
        {"name": "Win Rate", "value": f"{win_rate:.1%}", "inline": True},
    ]
    if avg_edge:
        fields.append({"name": "Avg Edge", "value": f"{avg_edge:.1f}pp", "inline": True})

    return _send([{
        "title": f"📊 Calibration Scorecard — {strategy}",
        "description": f"Performance after {n} resolutions",
        "color": COLOR_GOLD,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Learning System"},
    }])


def alert_daily_summary(bankroll: float, open_positions: int,
                         today_resolved: int = 0, today_wins: int = 0,
                         today_pnl: float = 0, **kwargs) -> bool:
    """Daily portfolio summary."""
    fields = [
        {"name": "Bankroll", "value": f"**${bankroll:,.2f}**", "inline": True},
        {"name": "Open", "value": str(open_positions), "inline": True},
        {"name": "Today", "value": f"{today_wins}W/{today_resolved - today_wins}L" if today_resolved else "No resolutions", "inline": True},
    ]
    if today_pnl:
        fields.append({"name": "Today P&L", "value": f"{'+'if today_pnl>=0 else ''}${today_pnl:.2f}", "inline": True})

    return _send([{
        "title": "📋 Daily Portfolio Summary",
        "color": COLOR_GREEN if today_pnl >= 0 else COLOR_RED if today_pnl < 0 else COLOR_GRAY,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Paper Portfolio"},
    }])


if __name__ == "__main__":
    # Test all alert types
    print("Testing alerts...")
    alert_position_opened("Will Elon Musk post 40-64 tweets Mar 2-4?", "NO", 0.40, 797.81, "tweet_count_mc", 35.0)
    print("  ✅ position_opened")

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


def alert_weekly_recap(bankroll: float, start_bankroll: float,
                        week_resolved: int, week_wins: int, week_pnl: float,
                        best_trade: Optional[dict] = None,
                        worst_trade: Optional[dict] = None,
                        open_positions: int = 0, **kwargs) -> bool:
    """Weekly P&L recap — Sunday summary."""
    pnl_pct = (week_pnl / start_bankroll * 100) if start_bankroll else 0
    streak_emoji = "🔥" if week_wins > week_resolved / 2 else "🧊"
    wr = (week_wins / week_resolved * 100) if week_resolved else 0

    fields = [
        {"name": "Bankroll", "value": f"**${bankroll:,.2f}**", "inline": True},
        {"name": "Week P&L", "value": f"{'+'if week_pnl>=0 else ''}${week_pnl:.2f} ({pnl_pct:+.1f}%)", "inline": True},
        {"name": "Record", "value": f"{streak_emoji} {week_wins}W/{week_resolved - week_wins}L ({wr:.0f}%)", "inline": True},
        {"name": "Open Positions", "value": str(open_positions), "inline": True},
    ]

    if best_trade:
        fields.append({
            "name": "🏆 Best Trade",
            "value": f"+${best_trade.get('pnl', 0):.2f} — {best_trade.get('market_title', '?')[:50]}",
            "inline": False,
        })
    if worst_trade:
        fields.append({
            "name": "💀 Worst Trade",
            "value": f"-${abs(worst_trade.get('pnl', 0)):.2f} — {worst_trade.get('market_title', '?')[:50]}",
            "inline": False,
        })

    return _send([{
        "title": "📊 Weekly P&L Recap",
        "color": COLOR_GREEN if week_pnl >= 0 else COLOR_RED,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Paper Portfolio — Weekly"},
    }])


def alert_weather_shift(market_title: str, city: str, side: str,
                         old_forecast: float, new_forecast: float,
                         threshold: float, entry_price: float,
                         shift_f: float, **kwargs) -> bool:
    """Alert when weather forecast shifts significantly on an open position."""
    direction = "↑" if new_forecast > old_forecast else "↓"
    danger = abs(shift_f) >= 5.0

    # Assess impact on our position
    if "higher" in market_title.lower() or "or higher" in market_title.lower():
        # YES = above threshold. If forecast drops below, YES loses
        if side == "YES" and new_forecast < threshold:
            impact = "⚠️ **EDGE LOST** — forecast now below threshold"
        elif side == "NO" and new_forecast >= threshold:
            impact = "⚠️ **EDGE LOST** — forecast now above threshold"
        else:
            impact = "✅ Edge intact"
    elif "between" in market_title.lower():
        impact = f"Forecast moved {direction} — check bracket fit"
    else:
        impact = f"Forecast shifted {direction}{abs(shift_f):.1f}°F"

    fields = [
        {"name": "City", "value": city.title(), "inline": True},
        {"name": "Our Side", "value": f"**{side}** @ {entry_price:.0%}", "inline": True},
        {"name": "Shift", "value": f"{direction} {abs(shift_f):.1f}°F", "inline": True},
        {"name": "Forecast", "value": f"{old_forecast:.1f}°F → **{new_forecast:.1f}°F**", "inline": True},
        {"name": "Threshold", "value": f"{threshold:.0f}°F", "inline": True},
        {"name": "Impact", "value": impact, "inline": False},
    ]

    return _send([{
        "title": f"🌡️ Weather Forecast Shift — {city.title()}",
        "description": market_title[:200],
        "color": COLOR_RED if danger else COLOR_GOLD,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Weather Ensemble"},
    }])


def alert_tweet_pace(handle: str, market_title: str, side: str,
                      entry_price: float, posts_so_far: int,
                      projected_total: int, bracket_low: int, bracket_high: int,
                      daily_mean: float, current_pace: float,
                      sigma_deviation: float, days_left: float,
                      **kwargs) -> bool:
    """Alert when tweet pace diverges >2σ from MC projection."""
    if projected_total >= bracket_low and projected_total <= bracket_high:
        in_bracket = True
    else:
        in_bracket = False

    # Assess danger to our position
    if side == "YES" and not in_bracket:
        impact = "⚠️ **Pace moving AWAY from bracket** — YES position at risk"
        color = COLOR_RED
    elif side == "NO" and in_bracket:
        impact = "⚠️ **Pace moving INTO bracket** — NO position at risk"
        color = COLOR_RED
    elif side == "YES" and in_bracket:
        impact = "✅ Pace confirms bracket — YES position strengthening"
        color = COLOR_GREEN
    else:
        impact = "✅ Pace outside bracket — NO position strengthening"
        color = COLOR_GREEN

    pace_dir = "🔥 Hot" if current_pace > daily_mean else "🧊 Cold"

    fields = [
        {"name": "Account", "value": f"@{handle}", "inline": True},
        {"name": "Our Side", "value": f"**{side}** @ {entry_price:.0%}", "inline": True},
        {"name": "Bracket", "value": f"{bracket_low}-{bracket_high}", "inline": True},
        {"name": "Posts So Far", "value": str(posts_so_far), "inline": True},
        {"name": "Projected Total", "value": f"**{projected_total}**", "inline": True},
        {"name": "Days Left", "value": f"{days_left:.1f}", "inline": True},
        {"name": "Pace", "value": f"{pace_dir} ({current_pace:.0f}/day vs {daily_mean:.0f} avg, {sigma_deviation:+.1f}σ)", "inline": False},
        {"name": "Impact", "value": impact, "inline": False},
    ]

    return _send([{
        "title": f"🐦 Tweet Pace Alert — @{handle}",
        "description": market_title[:200],
        "color": color,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Tweet Count Scanner"},
    }])


if __name__ == "__main__":
    # Test all alert types
    print("Testing alerts...")
    alert_position_opened("Will Elon Musk post 40-64 tweets Mar 2-4?", "NO", 0.40, 797.81, "tweet_count_mc", 35.0)
    print("  ✅ position_opened")

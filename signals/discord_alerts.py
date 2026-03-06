"""
Discord Webhook Alerts for Polyclawd
Sends trade opens, resolutions, edge alerts, and scorecards to #prediction-alerts

Alert types:
- Position opened/closed (with portfolio context)
- High edge signals (batched)
- Weather forecast shifts
- Tweet pace divergence
- Whale wall detection
- Daily summary (with strategy breakdown)
- Weekly recap (with best/worst, streaks)
- Scorecard milestones
- API health (down/recovered)
"""

import json
import logging
import os
import sqlite3
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
BOT_NAME = "VPredict"
AVATAR_URL = "https://virtuosocrypto.com/polyclawd/icons/icon-192.png"
DASHBOARD_URL = "https://virtuosocrypto.com/polyclawd/portfolio.html"
DB_PATH = Path(__file__).parent.parent / "storage" / "shadow_trades.db"
ALERTS_LOG = Path(__file__).parent.parent / "storage" / "alerts.jsonl"

# Colors — matched to dashboard palette
COLOR_GREEN = 0x00D68F   # win / position opened
COLOR_RED = 0xF65164     # loss
COLOR_BLUE = 0x6C5CE7    # edge signal / accent
COLOR_GOLD = 0xFFD700    # scorecard
COLOR_CYAN = 0x4ECDC4    # info / whale
COLOR_ORANGE = 0xF0A050  # warning / shift
COLOR_GRAY = 0x6B6B85    # void / neutral


def _log_alert(alert_type: str, metadata: dict, sent: bool) -> None:
    """Append alert record to JSONL log."""
    try:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": alert_type,
            "sent": sent,
            **metadata,
        }
        with open(ALERTS_LOG, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        logger.debug("Alert log write failed: %s", e)


def _send(embeds: list, content: str = "", alert_type: str = "",
          alert_meta: Optional[dict] = None) -> bool:
    """Send a Discord webhook message with embeds and log it."""
    sent = False
    if not WEBHOOK_URL:
        logger.debug("Discord webhook URL not set, skipping alert")
    else:
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
            sent = True
        except Exception as e:
            logger.warning("Discord alert failed: %s", e)

    # Always log (even if send failed or webhook not set)
    if alert_type:
        _log_alert(alert_type, alert_meta or {}, sent)

    return sent


def _portfolio_context() -> dict:
    """Get current portfolio snapshot for embedding in alerts."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        state = conn.execute("SELECT * FROM paper_portfolio_state ORDER BY id DESC LIMIT 1").fetchone()
        bankroll = float(state["bankroll"]) if state else 10000
        open_count = conn.execute("SELECT COUNT(*) c FROM paper_positions WHERE status='open'").fetchone()["c"]
        won = conn.execute("SELECT COUNT(*) c FROM paper_positions WHERE status='won'").fetchone()["c"]
        lost = conn.execute("SELECT COUNT(*) c FROM paper_positions WHERE status='lost'").fetchone()["c"]
        total_resolved = won + lost
        car = conn.execute("SELECT COALESCE(SUM(bet_size),0) c FROM paper_positions WHERE status='open'").fetchone()["c"]
        conn.close()
        wr = (won / total_resolved * 100) if total_resolved > 0 else 0
        return {
            "bankroll": bankroll,
            "open": open_count,
            "won": won,
            "lost": lost,
            "wr": wr,
            "at_risk": car,
            "record": f"{won}W/{lost}L ({wr:.0f}%)" if total_resolved else "No resolutions yet",
        }
    except Exception:
        return {"bankroll": 0, "open": 0, "won": 0, "lost": 0, "wr": 0, "at_risk": 0, "record": "—"}


def _market_url(slug: str = "", platform: str = "polymarket") -> str:
    if slug and platform == "polymarket":
        return f"https://polymarket.com/event/{slug}"
    return ""


def _strategy_label(strategy: str) -> str:
    return {
        "tweet_count_mc": "Tweet MC",
        "weather": "Weather",
        "weather_ensemble": "Weather",
        "whale_wall": "Whale Wall",
        "category_mispricing": "Category",
        "hf_latency_divergence": "HF Latency",
        "hf_virtuoso_trigger": "HF Trigger",
        "hf_bridge": "HF Bridge",
        "hf_neg_vig": "HF Neg Vig",
    }.get(strategy, strategy or "—")


def _strategy_emoji(strategy: str) -> str:
    return {
        "tweet_count_mc": "🐦",
        "weather": "🌡️",
        "weather_ensemble": "🌡️",
        "whale_wall": "🐋",
        "category_mispricing": "📊",
        "hf_latency_divergence": "⚡",
        "hf_virtuoso_trigger": "⚡",
        "hf_bridge": "⚡",
        "hf_neg_vig": "⚡",
    }.get(strategy, "📊")


# ── Position Alerts ──────────────────────────────────────────────────────

def alert_position_opened(market_title: str, side: str, entry_price: float,
                          bet_size: float, strategy: str, edge_pct: float = 0,
                          market_url: str = "", confidence: float = 0,
                          archetype: str = "", potential_payout: float = 0,
                          **kwargs) -> bool:
    """Alert when a new paper position is opened — includes portfolio context."""
    ctx = _portfolio_context()
    emoji = _strategy_emoji(strategy)
    label = _strategy_label(strategy)

    description = market_title[:200]
    if market_url:
        description = f"[{description}]({market_url})"

    # Potential ROI
    roi = ((potential_payout / bet_size - 1) * 100) if bet_size and potential_payout else 0

    fields = [
        {"name": "Side", "value": f"**{side}**", "inline": True},
        {"name": "Entry", "value": f"{entry_price:.0%}", "inline": True},
        {"name": "Size", "value": f"${bet_size:,.2f}", "inline": True},
        {"name": "Edge", "value": f"**+{edge_pct:.1f}pp**" if edge_pct else "—", "inline": True},
        {"name": "Strategy", "value": f"{emoji} {label}", "inline": True},
        {"name": "Potential ROI", "value": f"+{roi:.0f}%" if roi > 0 else "—", "inline": True},
    ]

    # Portfolio context footer
    risk_pct = (ctx["at_risk"] / ctx["bankroll"] * 100) if ctx["bankroll"] > 0 else 0

    return _send([{
        "title": f"📈 Position Opened — {side}",
        "description": description,
        "color": COLOR_GREEN if side == "YES" else COLOR_RED,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": f"💰 ${ctx['bankroll']:,.0f} · {ctx['open']} open · {risk_pct:.0f}% at risk · {ctx['record']}"},
    }], alert_type="position_opened", alert_meta={
        "market": market_title[:200], "side": side, "entry_price": entry_price,
        "bet_size": bet_size, "strategy": strategy, "edge_pct": edge_pct,
        "confidence": confidence, "bankroll": ctx["bankroll"],
    })


def alert_position_closed(market_title: str, side: str, outcome: str,
                           pnl: float, entry_price: float, exit_price: float = 0,
                           strategy: str = "", close_reason: str = "",
                           market_url: str = "", slug: str = "",
                           **kwargs) -> bool:
    """Alert when a position resolves — includes P&L impact and close reason."""
    ctx = _portfolio_context()
    label = _strategy_label(strategy)

    if outcome == "void":
        emoji, color, result = "⚪", COLOR_GRAY, "VOID"
    elif pnl > 0:
        emoji, color, result = "✅", COLOR_GREEN, "WIN"
    else:
        emoji, color, result = "❌", COLOR_RED, "LOSS"

    # Classify close type
    reason = close_reason or ""
    if "take-profit" in reason.lower():
        close_type = "🎯 Take Profit"
    elif "reeval" in reason.lower() and "flipped" in reason.lower():
        close_type = "🛑 Stop Loss (reeval)"
    elif "auto-resolved" in reason.lower() or "resolution" in reason.lower():
        close_type = "⏰ Market Resolved"
    elif "manual" in reason.lower():
        close_type = "👤 Manual Close"
    elif "fresh_start" in reason.lower():
        close_type = "🔄 Reset"
    else:
        close_type = "⏰ Resolved"

    description = market_title[:200]
    url = market_url or _market_url(slug)
    if url:
        description = f"[{description}]({url})"

    fields = [
        {"name": "Result", "value": f"**{result}**", "inline": True},
        {"name": "P&L", "value": f"**{'+'if pnl>=0 else ''}${pnl:,.2f}**", "inline": True},
        {"name": "Side", "value": side, "inline": True},
        {"name": "Entry → Exit", "value": f"{entry_price:.0%} → {exit_price:.0%}" if exit_price else f"{entry_price:.0%}", "inline": True},
        {"name": "Close Type", "value": close_type, "inline": True},
        {"name": "Strategy", "value": f"{_strategy_emoji(strategy)} {label}", "inline": True},
    ]

    return _send([{
        "title": f"{emoji} Position {result} — {'+'if pnl>=0 else ''}${pnl:,.2f}",
        "description": description,
        "color": color,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": f"💰 ${ctx['bankroll']:,.0f} · {ctx['open']} open · {ctx['record']}"},
    }], alert_type="position_closed", alert_meta={
        "market": market_title[:200], "side": side, "outcome": outcome,
        "pnl": pnl, "entry_price": entry_price, "exit_price": exit_price,
        "strategy": strategy, "close_reason": close_reason, "bankroll": ctx["bankroll"],
    })


# ── Edge Signals ─────────────────────────────────────────────────────────

def alert_edge_signal(market_title: str, side: str, edge_pct: float,
                       price: float, strategy: str, platform: str = "",
                       **kwargs) -> bool:
    """Alert on a high-edge signal (>25pp)."""
    label = _strategy_label(strategy)
    fields = [
        {"name": "Side", "value": f"**{side}**", "inline": True},
        {"name": "Edge", "value": f"**+{edge_pct:.1f}pp**", "inline": True},
        {"name": "Price", "value": f"{price:.0%}", "inline": True},
        {"name": "Strategy", "value": f"{_strategy_emoji(strategy)} {label}", "inline": True},
        {"name": "Platform", "value": platform.title() if platform else "—", "inline": True},
    ]

    return _send([{
        "title": "🎯 High Edge Signal",
        "description": market_title[:200],
        "color": COLOR_BLUE,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Signal Scanner"},
    }], alert_type="edge_signal", alert_meta={
        "market": market_title[:200], "side": side, "edge_pct": edge_pct,
        "price": price, "strategy": strategy, "platform": platform,
    })


def alert_edge_batch(signals: list) -> bool:
    """Batched edge alert — single embed with top signals as fields."""
    if not signals:
        return False

    fields = []
    for s in signals[:5]:
        emoji = _strategy_emoji(s.get("strategy", ""))
        price = s.get("price", 0)
        price_str = f"{price:.0%}" if price and price > 0.005 else "—"
        url = s.get("url", "")
        market_text = s.get("market", "?")[:80]
        if url:
            market_text = f"[{market_text}]({url})"
        fields.append({
            "name": f"{emoji} {s['side']} @ {price_str} — +{s['edge']:.0f}pp",
            "value": market_text,
            "inline": False,
        })

    ctx = _portfolio_context()
    return _send([{
        "title": f"🎯 Top Edge Signals ({len(signals)})",
        "color": COLOR_BLUE,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": f"Signal Scanner · 💰 ${ctx['bankroll']:,.0f} · {ctx['open']} open"},
    }], alert_type="edge_batch", alert_meta={
        "count": len(signals),
        "signals": [{"market": s.get("market", "?")[:100], "side": s.get("side"), "edge": s.get("edge")} for s in signals[:5]],
    })


# ── Scorecard & Milestones ───────────────────────────────────────────────

def alert_scorecard(strategy: str, n: int, brier: float, win_rate: float,
                     avg_edge: float = 0, **kwargs) -> bool:
    """Alert with calibration scorecard summary."""
    grade = "🟢" if brier < 0.20 else "🟡" if brier < 0.25 else "🔴"
    label = _strategy_label(strategy)

    fields = [
        {"name": "Resolutions", "value": str(n), "inline": True},
        {"name": "Brier Score", "value": f"{grade} {brier:.3f}", "inline": True},
        {"name": "Win Rate", "value": f"{win_rate:.1%}", "inline": True},
    ]
    if avg_edge:
        fields.append({"name": "Avg Edge", "value": f"{avg_edge:.1f}pp", "inline": True})

    return _send([{
        "title": f"📊 Calibration Scorecard — {label}",
        "description": f"Performance after {n} resolutions",
        "color": COLOR_GOLD,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Learning System"},
    }], alert_type="scorecard", alert_meta={
        "strategy": strategy, "n": n, "brier": brier, "win_rate": win_rate, "avg_edge": avg_edge,
    })


def alert_scorecard_milestone(strategy: str, n: int, wins: int,
                                win_rate: float, brier: Optional[float] = None,
                                **kwargs) -> bool:
    """Alert when a strategy hits its first scorecard milestone (20 resolutions)."""
    label = _strategy_label(strategy)

    fields = [
        {"name": "Strategy", "value": f"**{label}**", "inline": True},
        {"name": "Resolutions", "value": f"🏆 **{n}**", "inline": True},
        {"name": "Record", "value": f"{wins}W/{n - wins}L ({win_rate:.0%})", "inline": True},
    ]
    if brier is not None:
        grade = "🟢 Excellent" if brier < 0.15 else "🟡 Fair" if brier < 0.25 else "🔴 Poor"
        fields.append({"name": "Brier Score", "value": f"{brier:.3f} — {grade}", "inline": True})

    return _send([{
        "title": "🏆 Scorecard Milestone — First 20 Resolutions!",
        "description": f"**{label}** has enough data for calibration analysis",
        "color": COLOR_GOLD,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Learning System"},
    }], alert_type="scorecard_milestone", alert_meta={
        "strategy": strategy, "n": n, "wins": wins, "win_rate": win_rate, "brier": brier,
    })


# ── Daily & Weekly Summaries ─────────────────────────────────────────────

def alert_daily_summary(bankroll: float, open_positions: int,
                         today_resolved: int = 0, today_wins: int = 0,
                         today_pnl: float = 0, starting_bankroll: float = 10000,
                         strategies: dict = None, **kwargs) -> bool:
    """Daily portfolio summary with strategy breakdown and trend."""
    pnl_total = bankroll - starting_bankroll
    pnl_pct = (pnl_total / starting_bankroll * 100) if starting_bankroll else 0
    today_losses = today_resolved - today_wins

    fields = [
        {"name": "Bankroll", "value": f"**${bankroll:,.2f}**", "inline": True},
        {"name": "Total P&L", "value": f"{'+'if pnl_total>=0 else ''}${pnl_total:,.2f} ({pnl_pct:+.1f}%)", "inline": True},
        {"name": "Open", "value": str(open_positions), "inline": True},
        {"name": "Today", "value": f"**{today_wins}W/{today_losses}L** ({'+'if today_pnl>=0 else ''}${today_pnl:,.2f})" if today_resolved else "No resolutions", "inline": True},
    ]

    # Strategy breakdown if provided
    if strategies:
        breakdown_lines = []
        for strat, stats in sorted(strategies.items(), key=lambda x: abs(x[1].get("pnl", 0)), reverse=True):
            label = _strategy_label(strat)
            emoji = _strategy_emoji(strat)
            s_pnl = stats.get("pnl", 0)
            s_wr = stats.get("wr", 0)
            s_n = stats.get("n", 0)
            breakdown_lines.append(f"{emoji} **{label}**: {'+'if s_pnl>=0 else ''}${s_pnl:.0f} ({s_wr:.0f}% WR, {s_n} trades)")
        if breakdown_lines:
            fields.append({"name": "Strategy Breakdown", "value": "\n".join(breakdown_lines[:5]), "inline": False})

    color = COLOR_GREEN if today_pnl >= 0 else COLOR_RED if today_pnl < 0 else COLOR_GRAY
    return _send([{
        "title": f"📋 Daily Summary — {'+'if today_pnl>=0 else ''}${today_pnl:,.2f}",
        "description": f"[View Dashboard]({DASHBOARD_URL})",
        "color": color,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Paper Portfolio · Daily"},
    }], alert_type="daily_summary", alert_meta={
        "bankroll": bankroll, "open_positions": open_positions,
        "today_resolved": today_resolved, "today_wins": today_wins, "today_pnl": today_pnl,
    })


def alert_weekly_recap(bankroll: float, start_bankroll: float,
                        week_resolved: int, week_wins: int, week_pnl: float,
                        best_trade: Optional[dict] = None,
                        worst_trade: Optional[dict] = None,
                        open_positions: int = 0,
                        strategies: dict = None, **kwargs) -> bool:
    """Weekly P&L recap — Sunday summary with strategy breakdown."""
    pnl_pct = (week_pnl / start_bankroll * 100) if start_bankroll else 0
    week_losses = week_resolved - week_wins
    wr = (week_wins / week_resolved * 100) if week_resolved else 0

    # Streak
    if week_wins > week_losses:
        streak = f"🔥 {week_wins}W/{week_losses}L"
    elif week_losses > week_wins:
        streak = f"🧊 {week_wins}W/{week_losses}L"
    else:
        streak = f"⚖️ {week_wins}W/{week_losses}L"

    fields = [
        {"name": "Bankroll", "value": f"**${bankroll:,.2f}**", "inline": True},
        {"name": "Week P&L", "value": f"**{'+'if week_pnl>=0 else ''}${week_pnl:,.2f}** ({pnl_pct:+.1f}%)", "inline": True},
        {"name": "Record", "value": f"{streak} ({wr:.0f}%)", "inline": True},
        {"name": "Open Positions", "value": str(open_positions), "inline": True},
    ]

    if best_trade:
        fields.append({
            "name": "🏆 Best Trade",
            "value": f"**+${best_trade.get('pnl', 0):,.2f}** — {best_trade.get('market_title', '?')[:60]}",
            "inline": False,
        })
    if worst_trade:
        fields.append({
            "name": "💀 Worst Trade",
            "value": f"**-${abs(worst_trade.get('pnl', 0)):,.2f}** — {worst_trade.get('market_title', '?')[:60]}",
            "inline": False,
        })

    # Strategy breakdown
    if strategies:
        lines = []
        for strat, stats in sorted(strategies.items(), key=lambda x: abs(x[1].get("pnl", 0)), reverse=True):
            label = _strategy_label(strat)
            emoji = _strategy_emoji(strat)
            s_pnl = stats.get("pnl", 0)
            s_wr = stats.get("wr", 0)
            s_n = stats.get("n", 0)
            lines.append(f"{emoji} **{label}**: {'+'if s_pnl>=0 else ''}${s_pnl:.0f} ({s_wr:.0f}%, {s_n} trades)")
        if lines:
            fields.append({"name": "By Strategy", "value": "\n".join(lines[:6]), "inline": False})

    return _send([{
        "title": f"📊 Weekly Recap — {'+'if week_pnl>=0 else ''}${week_pnl:,.2f} ({pnl_pct:+.1f}%)",
        "description": f"[View Dashboard]({DASHBOARD_URL})",
        "color": COLOR_GREEN if week_pnl >= 0 else COLOR_RED,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Paper Portfolio · Weekly"},
    }], alert_type="weekly_recap", alert_meta={
        "bankroll": bankroll, "week_pnl": week_pnl, "week_resolved": week_resolved,
        "week_wins": week_wins, "open_positions": open_positions,
    })


# ── Weather Alerts ───────────────────────────────────────────────────────

def alert_weather_shift(market_title: str, city: str, side: str,
                         old_forecast: float, new_forecast: float,
                         threshold: float, entry_price: float,
                         shift_f: float, **kwargs) -> bool:
    """Alert when weather forecast shifts significantly on an open position."""
    direction = "↑" if new_forecast > old_forecast else "↓"
    danger = abs(shift_f) >= 5.0

    # Assess impact on our position
    if "higher" in market_title.lower() or "or higher" in market_title.lower():
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
        {"name": "Shift", "value": f"**{direction} {abs(shift_f):.1f}°F**", "inline": True},
        {"name": "Forecast", "value": f"{old_forecast:.1f}°F → **{new_forecast:.1f}°F**", "inline": True},
        {"name": "Threshold", "value": f"{threshold:.0f}°F", "inline": True},
        {"name": "Impact", "value": impact, "inline": False},
    ]

    return _send([{
        "title": f"🌡️ Forecast Shift — {city.title()} ({direction}{abs(shift_f):.1f}°F)",
        "description": market_title[:200],
        "color": COLOR_RED if danger else COLOR_ORANGE,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Weather Ensemble"},
    }], alert_type="weather_shift", alert_meta={
        "market": market_title[:200], "city": city, "side": side,
        "old_forecast": old_forecast, "new_forecast": new_forecast,
        "threshold": threshold, "shift_f": shift_f,
    })


# ── Tweet Pace Alerts ────────────────────────────────────────────────────

def alert_tweet_pace(handle: str, market_title: str, side: str,
                      entry_price: float, posts_so_far: int,
                      projected_total: int, bracket_low: int, bracket_high: int,
                      daily_mean: float, current_pace: float,
                      sigma_deviation: float, days_left: float,
                      **kwargs) -> bool:
    """Alert when tweet pace diverges >2σ from MC projection."""
    in_bracket = bracket_low <= projected_total <= bracket_high

    # Assess danger to our position
    if side == "YES" and not in_bracket:
        impact = "⚠️ **Pace AWAY from bracket** — YES at risk"
        color = COLOR_RED
    elif side == "NO" and in_bracket:
        impact = "⚠️ **Pace INTO bracket** — NO at risk"
        color = COLOR_RED
    elif side == "YES" and in_bracket:
        impact = "✅ Pace confirms bracket — YES strengthening"
        color = COLOR_GREEN
    else:
        impact = "✅ Pace outside bracket — NO strengthening"
        color = COLOR_GREEN

    pace_dir = "🔥 Hot" if current_pace > daily_mean else "🧊 Cold"

    fields = [
        {"name": "Account", "value": f"**@{handle}**", "inline": True},
        {"name": "Our Side", "value": f"**{side}** @ {entry_price:.0%}", "inline": True},
        {"name": "Bracket", "value": f"{bracket_low}-{bracket_high}", "inline": True},
        {"name": "Posts So Far", "value": str(posts_so_far), "inline": True},
        {"name": "Projected", "value": f"**{projected_total}**", "inline": True},
        {"name": "Days Left", "value": f"{days_left:.1f}", "inline": True},
        {"name": "Pace", "value": f"{pace_dir} ({current_pace:.0f}/day vs {daily_mean:.0f} avg, **{sigma_deviation:+.1f}σ**)", "inline": False},
        {"name": "Impact", "value": impact, "inline": False},
    ]

    return _send([{
        "title": f"🐦 Tweet Pace — @{handle} ({sigma_deviation:+.1f}σ)",
        "description": market_title[:200],
        "color": color,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Tweet Count Scanner"},
    }], alert_type="tweet_pace", alert_meta={
        "market": market_title[:200], "handle": handle, "side": side,
        "posts_so_far": posts_so_far, "projected_total": projected_total,
        "bracket": f"{bracket_low}-{bracket_high}", "sigma_deviation": sigma_deviation,
    })


# ── Whale Wall Alerts ────────────────────────────────────────────────────

def alert_whale_wall(market_title: str, side: str, imbalance_ratio: float,
                     bid_depth: float, ask_depth: float,
                     bid_walls: int = 0, ask_walls: int = 0,
                     max_wall_usd: float = 0, spread_cents: float = 0,
                     volume_24h: float = 0, slug: str = "",
                     **kwargs) -> bool:
    """Alert on significant orderbook imbalance detection."""
    direction = "BID" if side == "YES" else "ASK"
    emoji = "🐋" if imbalance_ratio >= 5.0 else "🔔"
    url = _market_url(slug)

    description = f"**{imbalance_ratio:.1f}:1 {direction}-heavy** imbalance detected"
    if url:
        description += f"\n[View Market]({url})"

    fields = [
        {"name": "Signal", "value": f"**{side}** (follow {direction.lower()} pressure)", "inline": True},
        {"name": "24h Volume", "value": f"${volume_24h:,.0f}", "inline": True},
        {"name": "Spread", "value": f"{spread_cents:.1f}¢", "inline": True},
        {"name": "Bid Depth", "value": f"${bid_depth:,.0f}" + (f" ({bid_walls} walls)" if bid_walls else ""), "inline": True},
        {"name": "Ask Depth", "value": f"${ask_depth:,.0f}" + (f" ({ask_walls} walls)" if ask_walls else ""), "inline": True},
    ]
    if max_wall_usd >= 10000:
        fields.append({"name": "Largest Wall", "value": f"**${max_wall_usd:,.0f}**", "inline": True})

    color = COLOR_CYAN
    return _send([{
        "title": f"{emoji} Whale Wall — {market_title[:70]}",
        "description": description,
        "color": color,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Whale Wall Scanner"},
    }], alert_type="whale_wall", alert_meta={
        "market": market_title[:200], "side": side, "imbalance_ratio": imbalance_ratio,
        "bid_depth": bid_depth, "ask_depth": ask_depth, "volume_24h": volume_24h, "slug": slug,
    })


# ── System Health ────────────────────────────────────────────────────────

def alert_api_down(consecutive_failures: int, last_error: str = "",
                    restart_attempted: bool = False, **kwargs) -> bool:
    """Alert when API health check fails 3+ times."""
    fields = [
        {"name": "Consecutive Failures", "value": f"**{consecutive_failures}**", "inline": True},
        {"name": "Restart", "value": "Attempted ✅" if restart_attempted else "Not yet", "inline": True},
    ]
    if last_error:
        fields.append({"name": "Error", "value": f"```{last_error[:200]}```", "inline": False})

    return _send([{
        "title": "🚨 API DOWN",
        "description": f"Health check failed {consecutive_failures}x — [Dashboard]({DASHBOARD_URL})",
        "color": COLOR_RED,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Scheduler Monitor"},
    }], alert_type="api_down", alert_meta={
        "consecutive_failures": consecutive_failures, "last_error": last_error[:200],
    })


def alert_api_recovered(**kwargs) -> bool:
    """Alert when API comes back online after downtime."""
    return _send([{
        "title": "✅ API Recovered",
        "description": f"Health check passing — [Dashboard]({DASHBOARD_URL})",
        "color": COLOR_GREEN,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Scheduler Monitor"},
    }], alert_type="api_recovered", alert_meta={})


# ── Test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Testing alerts...")
    alert_position_opened("Will Elon Musk post 40-64 tweets Mar 2-4?", "NO", 0.40, 797.81, "tweet_count_mc", 35.0)
    print("  ✅ position_opened")

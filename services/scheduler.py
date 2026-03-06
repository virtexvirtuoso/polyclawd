"""
Polyclawd Scheduler Service — replaces cron watchdog

Persistent asyncio service that orchestrates all periodic tasks:
- 30s:   HF signal processing + resolution
- 5min:  health check, paper resolution, shadow resolution, weather reeval, alerts, calibration
- 30min: signal scans (category, weather, tweets), edge alerts, source_health touch
- 6h:    arena snapshots
- daily:  Discord summary (22:00 UTC)
- weekly: Discord recap + scorecard (Sunday 23:50 UTC)

Replaces: /usr/local/bin/polyclawd-watchdog.sh (v12, 556 lines bash)
Run via: systemd polyclawd-scheduler.service
"""

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

DB_PATH = PROJECT_ROOT / "storage" / "shadow_trades.db"
HEALTH_URL = "http://127.0.0.1:8420/health"
SERVICE_NAME = "polyclawd-api"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("scheduler")

# ============================================================================
# State — persistent across ticks (advantage over cron)
# ============================================================================

_state = {
    "consecutive_restarts": 0,
    "edge_alert_state": {},        # dedup for edge alerts
    "weather_shift_cache": {},     # previous forecast temps
    "pace_alert_sent": {},         # rate limit tweet pace alerts
    "daily_sent": None,            # date string
    "weekly_sent": None,           # year+week string
    "scorecard_sent": None,        # year+week string
    "milestone_sent": {},          # strategy → bool
}


# ============================================================================
# Helpers
# ============================================================================

def _db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _health_check() -> bool:
    """Check API health. Returns True if healthy."""
    import urllib.request
    for attempt in range(3):
        try:
            req = urllib.request.Request(HEALTH_URL)
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = resp.read().decode()
                if '"healthy"' in data:
                    return True
        except Exception:
            pass
        if attempt < 2:
            time.sleep(3)
    return False


def _restart_service():
    """Restart polyclawd-api via systemctl."""
    _state["consecutive_restarts"] += 1
    count = _state["consecutive_restarts"]
    logger.warning("Health check failed, restarting %s (attempt #%d)", SERVICE_NAME, count)

    if count >= 5:
        logger.error("Backing off: %d consecutive restarts", count)
        return

    try:
        from signals.discord_alerts import alert_api_down
        alert_api_down(count, "Health check failed 3x", restart_attempted=True)
    except Exception:
        pass

    subprocess.run(["sudo", "systemctl", "restart", SERVICE_NAME], timeout=30)
    logger.info("Service restarted")


def _run_safe(name: str, fn, *args, **kwargs):
    """Run a function, catching all exceptions."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logger.error("Task %s failed: %s", name, e)
        return None


# ============================================================================
# Task implementations
# ============================================================================

def task_health_check():
    """Check API health, restart if needed."""
    if _health_check():
        if _state["consecutive_restarts"] > 0:
            logger.info("API recovered after %d restarts", _state["consecutive_restarts"])
            try:
                from signals.discord_alerts import alert_api_recovered
                alert_api_recovered()
            except Exception:
                pass
        _state["consecutive_restarts"] = 0
    else:
        _restart_service()


def task_shadow_resolution():
    """Resolve shadow trades + snapshot + summary."""
    venv = str(PROJECT_ROOT / "venv" / "bin" / "python3")
    for cmd in ["resolve", "snapshot", "summary"]:
        subprocess.run(
            [venv, str(PROJECT_ROOT / "signals" / "shadow_tracker.py"), cmd],
            capture_output=True, timeout=60,
        )


def task_paper_resolution():
    """Resolve open paper portfolio positions."""
    from signals.paper_portfolio import resolve_open_positions
    resolve_open_positions()


def task_hf_signals():
    """Process HF signals → paper positions + resolve HF trades."""
    from services.hf_paper_trader import process_hf_signals, resolve_hf_positions
    result = process_hf_signals()
    if result.get("positions_opened", 0) > 0:
        logger.info("HF: opened %d positions", result["positions_opened"])
    resolve_hf_positions()


def task_resolution_scanner():
    """Tier 1 resolution certainty scanning."""
    venv = str(PROJECT_ROOT / "venv" / "bin" / "python3")
    subprocess.run(
        [venv, str(PROJECT_ROOT / "signals" / "resolution_scanner.py"), "scan"],
        capture_output=True, timeout=60,
    )


def task_weather_reeval():
    """Re-evaluate weather positions with latest forecasts."""
    from signals.weather_scanner import reeval_weather_positions
    reeval_weather_positions()


def task_weather_shift_alerts():
    """Alert on significant forecast shifts for open weather positions."""
    conn = _db()
    positions = conn.execute(
        "SELECT id, market_title, market_id, side, entry_price "
        "FROM paper_positions WHERE status='open' AND archetype='weather'"
    ).fetchall()
    conn.close()

    if not positions:
        return

    from signals.weather_scanner import (
        _extract_city_from_market, _extract_date_from_market, _extract_temp_threshold,
    )
    from signals.weather_ensemble import get_ensemble_forecast
    from signals.discord_alerts import alert_weather_shift

    prev = _state["weather_shift_cache"]
    current = {}

    for pos in positions:
        title = pos["market_title"]
        city = _extract_city_from_market(title)
        target_date = _extract_date_from_market(title)
        temp_info = _extract_temp_threshold(title)
        if not city or not target_date or not temp_info:
            continue

        forecast = get_ensemble_forecast(city, target_date)
        if not forecast:
            continue

        high_f = forecast.get("high_f", 0)
        key = str(pos["id"])
        current[key] = high_f

        if key in prev:
            shift = high_f - prev[key]
            if abs(shift) >= 3.0:
                threshold = temp_info.get("threshold", 0) if isinstance(temp_info, dict) else temp_info
                if isinstance(threshold, tuple):
                    threshold = threshold[0]
                alert_weather_shift(
                    title, city, pos["side"], prev[key], high_f,
                    float(threshold), pos["entry_price"], shift,
                )

    _state["weather_shift_cache"] = current


def task_tweet_pace_alerts():
    """Alert on statistically significant tweet pace deviations."""
    conn = _db()
    positions = conn.execute(
        "SELECT id, market_title, market_id, side, entry_price "
        "FROM paper_positions WHERE status='open' AND strategy='tweet_count_mc'"
    ).fetchall()
    conn.close()

    if not positions:
        return

    import re
    from signals.tweet_count_scanner import (
        fetch_post_history, _extract_bracket, ACCOUNTS, scan_tweet_markets,
    )
    from signals.discord_alerts import alert_tweet_pace

    now = time.time()
    pace_sent = _state["pace_alert_sent"]

    for pos in positions:
        title = pos["market_title"]
        key = str(pos["id"])

        # Rate limit: 1 per position per 2h
        if key in pace_sent and (now - pace_sent[key]) < 7200:
            continue

        # Find handle
        handle = None
        for h, cfg in ACCOUNTS.items():
            name = cfg.get("name", "").lower()
            if name and name in title.lower():
                handle = h
                break
        if not handle:
            continue

        bracket = _extract_bracket(title)
        if not bracket or "-" not in bracket:
            continue
        parts = bracket.split("-")
        try:
            bracket_low, bracket_high = int(parts[0]), int(parts[1])
        except ValueError:
            continue

        cfg = ACCOUNTS[handle]
        daily_mean = cfg.get("daily_mean", 50)
        daily_std = cfg.get("daily_std", 25)

        try:
            signals = scan_tweet_markets(handle)
        except Exception:
            continue

        for s in signals:
            if s.get("bracket") == bracket and pos["market_id"] in s.get("market_id", ""):
                posts_so_far = s.get("posts_so_far", 0)
                projected = s.get("projected_total", 0)
                days_left = s.get("days_to_close", 0)
                days_elapsed = max(s.get("days_elapsed", 1), 0.1)
                current_pace = posts_so_far / days_elapsed
                sigma_dev = (current_pace - daily_mean) / max(daily_std, 1)

                if abs(sigma_dev) >= 2.0:
                    alert_tweet_pace(
                        handle, title, pos["side"], pos["entry_price"],
                        posts_so_far, projected, bracket_low, bracket_high,
                        daily_mean, current_pace, sigma_dev, days_left,
                    )
                    pace_sent[key] = now
                break


def task_calibration_check():
    """Check calibration health, log Brier scores."""
    from signals.resolution_logger import load_resolutions, get_scorecard

    for strategy in ("tweet_count_mc", "weather_ensemble"):
        records = load_resolutions(strategy)
        n = len(records)
        if n < 20:
            logger.info("CALIBRATION %s: %d/20 resolutions (collecting)", strategy, n)
            continue

        card = get_scorecard(strategy)
        if card:
            brier = card["brier"]
            wr = card["win_rate"]
            status = "GREEN" if brier < 0.15 else "YELLOW" if brier < 0.25 else "RED"
            logger.info("CALIBRATION %s: Brier=%.3f (%s) WR=%.0f%% n=%d", strategy, brier, status, wr * 100, n)

            # Milestone alert (first time hitting 20)
            if not _state["milestone_sent"].get(strategy):
                try:
                    from signals.discord_alerts import alert_scorecard_milestone
                    wins = sum(1 for r in records if r.get("won"))
                    alert_scorecard_milestone(strategy, n, wins, wr, brier)
                    _state["milestone_sent"][strategy] = True
                except Exception:
                    pass


def task_signal_scan():
    """30-min signal scan: category + weather + tweet → paper portfolio."""
    from signals.paper_portfolio import process_signals

    # Category signals
    try:
        from signals.mispriced_category_signal import get_mispriced_category_signals
        result = get_mispriced_category_signals()
        signals = result.get("signals", [])
        if signals:
            process_signals(signals)
    except Exception as e:
        logger.error("Category scan failed: %s", e)

    # Weather signals
    try:
        from signals.weather_scanner import get_weather_portfolio_signals
        signals = get_weather_portfolio_signals(min_edge=15.0, max_signals=3)
        if signals:
            process_signals(signals)
    except Exception as e:
        logger.error("Weather scan failed: %s", e)

    # Tweet count signals
    try:
        from signals.tweet_count_scanner import get_tweet_portfolio_signals
        signals = get_tweet_portfolio_signals(min_edge=5.0, max_signals=3)
        if signals:
            process_signals(signals)
    except Exception as e:
        logger.error("Tweet scan failed: %s", e)

    logger.info("Signal scan complete (category + weather + tweets)")


def task_source_health_touch():
    """Touch source_health timestamps to prevent staleness gate."""
    from api.services.source_health import touch_source
    for src in ("polymarket_gamma", "kalshi", "polymarket_clob"):
        touch_source(src)


def task_edge_alerts():
    """Smart edge alerts to Discord — dedup, cooldown, liquidity filter."""
    from signals.discord_alerts import alert_edge_batch
    from signals.tweet_count_scanner import scan_all_tweet_markets
    from signals.weather_scanner import scan_all_weather

    COOLDOWN_HOURS = 4
    EDGE_CHANGE_THRESHOLD = 10
    MIN_LIQUIDITY = 1000

    prev_state = _state["edge_alert_state"]

    # Load open position IDs to skip
    open_market_ids = set()
    try:
        conn = _db()
        rows = conn.execute('SELECT market_id FROM paper_positions WHERE status="open"').fetchall()
        open_market_ids = {r[0] for r in rows}
        conn.close()
    except Exception:
        pass

    now = time.time()
    raw_signals = []

    try:
        tweet_result = scan_all_tweet_markets()
        for s in tweet_result.get("signals", []):
            edge = s.get("edge_pct", 0)
            if edge >= 25:
                slug = s.get("event_slug", "")
                url = f"https://polymarket.com/event/{slug}" if slug else ""
                raw_signals.append({
                    "market": s.get("market_title", "")[:60],
                    "side": s.get("side", ""), "edge": edge,
                    "price": s.get("entry_price", 0),
                    "strategy": "tweet_count_mc", "url": url,
                    "market_id": s.get("market_id", ""),
                    "volume": s.get("volume", 0),
                    "days_left": s.get("days_to_close", 99),
                })
    except Exception:
        pass

    try:
        weather_result = scan_all_weather()
        for s in weather_result.get("signals", []):
            edge = s.get("edge_pct", 0)
            if edge >= 25:
                yes_p = s.get("yes_price", 0)
                side = s.get("side", "NO")
                eff_price = yes_p if side == "YES" else (1 - yes_p) if yes_p else 0
                slug = s.get("slug", "")
                url = f"https://polymarket.com/event/{slug}" if slug else ""
                raw_signals.append({
                    "market": s.get("market", "")[:60],
                    "side": side, "edge": edge,
                    "price": eff_price,
                    "strategy": "weather_ensemble", "url": url,
                    "market_id": s.get("market_id", ""),
                    "volume": s.get("volume", 0) or s.get("liquidity", 0),
                    "days_left": 1,
                })
    except Exception:
        pass

    # Smart filtering
    filtered = []
    new_state = dict(prev_state)

    for s in raw_signals:
        mid = s.get("market_id", "")
        key = mid[:20] if mid else s["market"][:30]

        if mid in open_market_ids:
            continue
        if s.get("volume", 0) < MIN_LIQUIDITY:
            continue

        prev = prev_state.get(key, {})
        last_alerted = prev.get("ts", 0)
        last_edge = prev.get("edge", 0)
        hours_since = (now - last_alerted) / 3600

        edge_changed = abs(s["edge"] - last_edge) >= EDGE_CHANGE_THRESHOLD
        is_urgent = s.get("days_left", 99) < 1

        if hours_since < COOLDOWN_HOURS and not edge_changed and not is_urgent:
            continue

        if is_urgent:
            s["market"] = "⏰ " + s["market"]

        filtered.append(s)
        new_state[key] = {"ts": now, "edge": s["edge"]}

    _state["edge_alert_state"] = new_state

    if filtered:
        filtered.sort(key=lambda x: x["edge"], reverse=True)
        alert_edge_batch(filtered[:5])


def task_arena_snapshot():
    """AI arena leaderboard snapshot."""
    venv = str(PROJECT_ROOT / "venv" / "bin" / "python3")
    subprocess.run(
        [venv, str(PROJECT_ROOT / "signals" / "ai_model_tracker.py"), "snapshot"],
        capture_output=True, timeout=60,
    )
    logger.info("Arena leaderboard snapshot taken")


def task_daily_discord_summary():
    """Daily portfolio summary to Discord (22:00 UTC)."""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    if _state["daily_sent"] == today:
        return

    from signals.discord_alerts import alert_daily_summary

    conn = _db()
    row = conn.execute("SELECT bankroll FROM paper_portfolio_state ORDER BY id DESC LIMIT 1").fetchone()
    bankroll = row["bankroll"] if row else 10000
    open_count = conn.execute('SELECT COUNT(*) as c FROM paper_positions WHERE status="open"').fetchone()["c"]
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    closed = conn.execute(
        'SELECT * FROM paper_positions WHERE closed_at >= ? AND status != "open"', (cutoff,)
    ).fetchall()
    wins = sum(1 for r in closed if r["pnl"] and r["pnl"] > 0)
    pnl = sum(r["pnl"] or 0 for r in closed)
    conn.close()

    alert_daily_summary(bankroll, open_count, len(closed), wins, pnl)
    _state["daily_sent"] = today
    logger.info("Daily Discord summary sent")


def task_weekly_recap():
    """Weekly Discord recap + Telegram scorecard (Sunday 23:xx UTC)."""
    year_week = datetime.now(timezone.utc).strftime("%Y%W")
    if _state["weekly_sent"] == year_week:
        return

    from signals.discord_alerts import alert_weekly_recap

    conn = _db()
    row = conn.execute("SELECT bankroll FROM paper_portfolio_state ORDER BY id DESC LIMIT 1").fetchone()
    bankroll = row["bankroll"] if row else 10000
    open_count = conn.execute('SELECT COUNT(*) as c FROM paper_positions WHERE status="open"').fetchone()["c"]

    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    closed = conn.execute(
        'SELECT * FROM paper_positions WHERE closed_at >= ? AND status != "open"', (week_ago,)
    ).fetchall()
    wins = sum(1 for r in closed if r["pnl"] and r["pnl"] > 0)
    pnl = sum(r["pnl"] or 0 for r in closed)

    best = max(closed, key=lambda r: r["pnl"] or 0) if closed else None
    worst = min(closed, key=lambda r: r["pnl"] or 0) if closed else None
    best_d = {"pnl": best["pnl"], "market_title": best["market_title"]} if best else None
    worst_d = {"pnl": worst["pnl"], "market_title": worst["market_title"]} if worst else None

    start_bankroll = bankroll - pnl
    alert_weekly_recap(bankroll, start_bankroll, len(closed), wins, pnl, best_d, worst_d, open_count)
    conn.close()

    # Scorecard
    if _state["scorecard_sent"] != year_week:
        from signals.resolution_logger import load_resolutions, get_scorecard

        lines = ["Weekly Calibration Report", ""]
        for strategy, label in [("tweet_count_mc", "Tweet MC"), ("weather_ensemble", "Weather")]:
            records = load_resolutions(strategy)
            n = len(records)
            if n == 0:
                lines.append(f"{label}: No resolutions yet")
                continue
            wins_s = sum(1 for r in records if r.get("won"))
            losses_s = n - wins_s
            wr = wins_s / n * 100
            if n < 20:
                lines.append(f"{label}: {wins_s}W/{losses_s}L ({wr:.0f}% WR) — {n}/20 for Brier")
                continue
            card = get_scorecard(strategy)
            if card:
                brier = card["brier"]
                status = "GOOD" if brier < 0.15 else "FAIR" if brier < 0.25 else "BAD"
                lines.append(f"{label}: Brier={brier:.3f} ({status}) | {wins_s}W/{losses_s}L ({wr:.0f}% WR) | n={n}")

        report = "\n".join(lines)
        logger.info("Weekly scorecard:\n%s", report)
        _state["scorecard_sent"] = year_week

    _state["weekly_sent"] = year_week
    logger.info("Weekly Discord recap sent")


# ============================================================================
# Scheduler loop
# ============================================================================

async def run_in_thread(fn, *args, **kwargs):
    """Run blocking function in executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


async def tick_30s():
    """Every 30 seconds: HF signals."""
    while True:
        await run_in_thread(_run_safe, "hf_signals", task_hf_signals)
        await asyncio.sleep(30)


async def tick_5min():
    """Every 5 minutes: health, resolution, reeval, alerts, calibration."""
    while True:
        await run_in_thread(_run_safe, "health_check", task_health_check)
        await run_in_thread(_run_safe, "shadow_resolution", task_shadow_resolution)
        await run_in_thread(_run_safe, "paper_resolution", task_paper_resolution)
        await run_in_thread(_run_safe, "resolution_scanner", task_resolution_scanner)
        await run_in_thread(_run_safe, "weather_reeval", task_weather_reeval)
        await run_in_thread(_run_safe, "weather_shift_alerts", task_weather_shift_alerts)
        await run_in_thread(_run_safe, "tweet_pace_alerts", task_tweet_pace_alerts)
        await run_in_thread(_run_safe, "calibration_check", task_calibration_check)
        logger.debug("5-min tick complete")
        await asyncio.sleep(300)


async def tick_30min():
    """Every 30 minutes: signal scans, edge alerts, source health."""
    while True:
        await run_in_thread(_run_safe, "signal_scan", task_signal_scan)
        await run_in_thread(_run_safe, "source_health_touch", task_source_health_touch)
        await run_in_thread(_run_safe, "edge_alerts", task_edge_alerts)
        logger.info("30-min tick complete")
        await asyncio.sleep(1800)


async def tick_6h():
    """Every 6 hours: arena snapshot."""
    while True:
        await run_in_thread(_run_safe, "arena_snapshot", task_arena_snapshot)
        await asyncio.sleep(21600)


async def tick_scheduled():
    """Check daily/weekly tasks every 10 minutes."""
    while True:
        now = datetime.now(timezone.utc)

        # Daily summary at 22:xx UTC
        if now.hour == 22:
            await run_in_thread(_run_safe, "daily_summary", task_daily_discord_summary)

        # Weekly recap: Sunday 23:xx UTC
        if now.weekday() == 6 and now.hour == 23:
            await run_in_thread(_run_safe, "weekly_recap", task_weekly_recap)

        await asyncio.sleep(600)


async def main():
    logger.info("=" * 60)
    logger.info("Polyclawd Scheduler starting")
    logger.info("Project: %s", PROJECT_ROOT)
    logger.info("DB: %s", DB_PATH)
    logger.info("=" * 60)

    # Stagger starts to avoid thundering herd
    tasks = [
        asyncio.create_task(tick_30s()),
        asyncio.create_task(_delayed_start(5, tick_5min)),
        asyncio.create_task(_delayed_start(15, tick_30min)),
        asyncio.create_task(_delayed_start(60, tick_6h)),
        asyncio.create_task(_delayed_start(30, tick_scheduled)),
    ]

    await asyncio.gather(*tasks)


async def _delayed_start(delay_s: int, coro_fn):
    """Start a tick loop after an initial delay."""
    await asyncio.sleep(delay_s)
    await coro_fn()


if __name__ == "__main__":
    asyncio.run(main())

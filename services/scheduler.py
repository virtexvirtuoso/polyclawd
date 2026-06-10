"""
Polyclawd Scheduler Service — replaces cron watchdog

Persistent asyncio service that orchestrates all periodic tasks:
- 30s:   HF signal processing + resolution
- 60s:   urgent stop-loss for positions resolving within 6h (15% weather threshold)
- 5min:  health check, stop-loss eval, price logging, paper resolution, shadow resolution, weather reeval, alerts, calibration
- 5min:  weather signal scan (fast loop — edge decays quickly)
- 30min: signal scans (category, tweets), edge alerts, source_health touch
- 6h:    arena snapshots
- daily:  Discord summary (22:00 UTC)
- weekly: Discord recap + scorecard (Sunday 23:50 UTC)

Replaces: /usr/local/bin/polyclawd-watchdog.sh (v12, 556 lines bash)
Run via: systemd polyclawd-scheduler.service
"""

import asyncio
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
    "mlb_props_alert_state": {},   # dedup for MLB prop alerts (player|market -> {ts, edge})
    "weather_shift_cache": {},     # previous forecast temps
    "pace_alert_sent": {},         # rate limit tweet pace alerts
    "daily_sent": None,            # date string
    "weekly_sent": None,           # year+week string
    "scorecard_sent": None,        # year+week string
    "milestone_sent": {},          # strategy → bool
    "election_report_sent": None,  # year+week string
    "election_snapshot_sent": None, # date string
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
        logger.exception("Task %s failed: %s", name, e)
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


def task_equity_snapshot():
    """Capture a periodic equity snapshot (realized + unrealized) for the time-series chart."""
    from signals.paper_portfolio import snapshot_equity, backfill_equity_snapshots
    # Idempotent — runs once on first call, no-op after
    backfill_equity_snapshots()
    snap = snapshot_equity()
    logger.debug("Equity snapshot: $%.2f (realized $%.2f, unrealized $%.2f)",
                 snap.get("equity", 0), snap.get("realized", 0), snap.get("unrealized", 0))


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


def task_stop_evaluator():
    """Check all open positions against stop-loss thresholds."""
    from services.stop_evaluator import evaluate_stops
    evaluate_stops()


def task_stop_evaluator_urgent():
    """Fast stop check for positions resolving within 6 hours."""
    from services.stop_evaluator import evaluate_stops_urgent
    evaluate_stops_urgent()


def task_price_logger():
    """Log current prices for all open positions."""
    from services.price_logger import log_position_prices
    log_position_prices()


def task_book_logger():
    """Log adverse-side orderbook microstructure for all open Polymarket
    positions. Pure observability — no trading impact. Builds dataset for
    future orderbook-aware stop logic. See services/book_logger.py."""
    from services.book_logger import log_position_books
    log_position_books()


def task_weather_fast_scan():
    """Fast weather scan every 5min — edge decays fast, scan often.

    Lower min_edge (8%), higher max_signals (8), includes liquidity filter.
    Also checks for take-profit opportunities on open positions.

    RETIRED 2026-06-10 (Weather-Edge-Analysis-Jun2026): PM day-ahead strategy
    falsified -- no edge. New entries gated OFF via engine state; existing open
    positions wind down naturally via task_weather_reeval + stop evaluator.
    """
    from api.routes.engine import load_engine_state
    if not load_engine_state().get("weather_trading_enabled", False):
        return

    from signals.paper_portfolio import process_signals

    try:
        from signals.weather_scanner import get_weather_portfolio_signals
        signals = get_weather_portfolio_signals(min_edge=8.0, max_signals=8)
        if signals:
            opened = process_signals(signals)
            n = opened.get("opened", 0) if isinstance(opened, dict) else 0
            if n > 0:
                logger.info("Weather fast scan: opened %d positions", n)
    except Exception as e:
        logger.exception("Weather fast scan failed: %s", e)


def task_kalshi_fade_scan():
    """Kalshi weather tail-fade (PAPER) -- evening-window entries only.

    Validated 2026-06-10 (Weather-Edge-Analysis-Jun2026 sec 10). The module
    no-ops outside each city's local 19:30-20:30 window, so a 30-min cadence
    yields 1-2 in-window calls per city; market_id dedup makes repeats no-ops.
    """
    from api.routes.engine import load_engine_state
    if not load_engine_state().get("kalshi_fade_enabled", True):
        return
    from signals.kalshi_weather_fade import run_evening_scan
    run_evening_scan()


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

    from signals.tweet_count_scanner import (
        _extract_bracket, TRACKED_ACCOUNTS as ACCOUNTS, scan_tweet_markets,
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
    """Check calibration health, log Brier scores.

    Two metrics (Option-B split, 2026-04-28):
      • model_calibration  — auto-resolved closes only. Tests "did the
        model's P(NO) match actual market resolutions?" This is the metric
        that determines whether the model itself needs recalibration.
      • stop_policy_outcome — every close (auto, stop, manual, partial).
        Tests "did our trading outcomes match the model's prior belief?"
        High Brier here with low Brier above means stops or sizing are the
        problem, not the model.

    Threshold (each metric independently): GREEN <0.15, YELLOW <0.25, RED >=0.25.
    """
    from signals.resolution_logger import (
        load_resolutions, get_scorecard, get_auto_scorecard,
    )

    def _status(brier):
        return "GREEN" if brier < 0.15 else "YELLOW" if brier < 0.25 else "RED"

    for strategy in ("tweet_count_mc", "weather_ensemble", "options_implied"):
        # Outcome metric (mixed close-types) — current behaviour
        outcome_records = load_resolutions(strategy)
        n_out = len(outcome_records)
        if n_out >= 20:
            card = get_scorecard(strategy)
            if card:
                logger.info(
                    "CALIBRATION %s outcome: Brier=%.3f (%s) WR=%.0f%% n=%d",
                    strategy, card["brier"], _status(card["brier"]),
                    card["win_rate"] * 100, n_out,
                )
        else:
            logger.info("CALIBRATION %s outcome: %d/20 (collecting)", strategy, n_out)

        # Model-calibration metric (auto-resolved only) — true model accuracy
        auto_card = get_auto_scorecard(strategy)
        if auto_card:
            logger.info(
                "CALIBRATION %s model: Brier=%.3f (%s) WR=%.0f%% n=%d",
                strategy, auto_card["brier"], _status(auto_card["brier"]),
                auto_card["win_rate"] * 100, auto_card["n"],
            )
        else:
            # Look up raw count even when below threshold so we can see growth
            from signals.resolution_logger import load_auto_resolutions
            n_auto = len(load_auto_resolutions(strategy))
            logger.info("CALIBRATION %s model: %d/20 (collecting auto-resolved)",
                        strategy, n_auto)

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
    """30-min signal scan: category + tweet → paper portfolio. (Weather moved to 5-min fast loop.)"""
    from signals.paper_portfolio import process_signals

    # Category signals
    try:
        from signals.mispriced_category_signal import get_mispriced_category_signals
        result = get_mispriced_category_signals()
        signals = result.get("signals", [])
        if signals:
            process_signals(signals)
    except Exception as e:
        logger.exception("Category scan failed: %s", e)

    # Tweet count signals
    try:
        from signals.tweet_count_scanner import get_tweet_portfolio_signals
        signals = get_tweet_portfolio_signals(min_edge=5.0, max_signals=3)
        if signals:
            process_signals(signals)
    except Exception as e:
        logger.exception("Tweet scan failed: %s", e)

    # Whale wall signals
    try:
        from signals.whale_wall_scanner import get_whale_portfolio_signals
        signals = get_whale_portfolio_signals(min_imbalance=3.0, max_signals=3)
        if signals:
            process_signals(signals)
    except Exception as e:
        logger.exception("Whale wall scan failed: %s", e)

    logger.info("Signal scan complete (category + tweets + whale walls)")


def task_options_scan():
    """30-min options-implied scanner: fetch Alpaca + Polymarket, compute spreads, paper-trade z-gated signals."""
    from signals.options_implied import run, open_trades
    
    # Step 1: Run the scanner (fetch + compute + write to options_implied.db)
    written = run()
    if written is None:
        logger.warning("Options scan: run() returned None (likely missing ALPACA_API_KEY)")
        return
    
    logger.info("Options scan: %d rows written to DB", written)
    
    # Step 2: Open paper trades for z-gated signals
    try:
        result = open_trades()
        if result and result.get("opened", 0) > 0:
            logger.info("Options scan: opened %d new positions", result["opened"])
        elif result:
            logger.debug("Options scan: no signals cleared z-gate")
    except Exception as e:
        logger.exception("Options paper trade failed: %s", e)


def task_whale_wall_alerts():
    """Alert on new whale wall detections (dedup by market_id, 4h cooldown)."""
    from signals.whale_wall_scanner import scan_whale_walls
    from signals.discord_alerts import alert_whale_wall

    COOLDOWN = 14400  # 4 hours
    now = time.time()

    if "whale_alert_sent" not in _state:
        _state["whale_alert_sent"] = {}

    scan = scan_whale_walls()
    for m in scan.get("alerts", []):
        key = m.get("market_id", "")
        if not key:
            continue
        last_sent = _state["whale_alert_sent"].get(key, 0)
        if now - last_sent < COOLDOWN:
            continue

        alert_whale_wall(
            market_title=m.get("question", "")[:80],
            side=m.get("signal_side", "YES"),
            imbalance_ratio=m.get("imbalance_ratio", 0),
            bid_depth=m.get("bid_depth_usd", 0),
            ask_depth=m.get("ask_depth_usd", 0),
            bid_walls=m.get("bid_walls", 0),
            ask_walls=m.get("ask_walls", 0),
            max_wall_usd=max(m.get("max_bid_wall_usd", 0), m.get("max_ask_wall_usd", 0)),
            spread_cents=m.get("spread_cents", 0),
            volume_24h=m.get("volume_24h", 0),
            slug=m.get("slug", ""),
        )
        _state["whale_alert_sent"][key] = now
        logger.info("Whale wall alert: %s %.1f:1 %s",
                    m.get("question", "")[:40], m.get("imbalance_ratio", 0), m.get("signal_side", ""))


def task_source_health_touch():
    """Touch source_health timestamps to prevent staleness gate."""
    from api.services.source_health import touch_source
    for src in ("polymarket_gamma", "kalshi", "polymarket_clob"):
        touch_source(src)


def task_options_resolution():
    """Resolve expired options-implied markets against Polymarket (every 30min)."""
    try:
        from signals.options_resolver import scan_resolved_options_markets
        result = scan_resolved_options_markets()
        if result.get("resolved", 0) > 0 or result.get("forecast_logged", 0) > 0:
            logger.info(
                "Options resolution: %d resolved, %d logged",
                result["resolved"], result["forecast_logged"],
            )
    except Exception as e:
        logger.exception("Options resolution failed: %s", e)


def task_options_monitor():
    """Check open options positions for edge decay (every 30 min)."""
    try:
        from signals.options_implied import reeval_options_positions
        result = reeval_options_positions()
        if result.get("closed", 0) > 0:
            logger.info("Options monitor: %d closed (take-profit/stop-loss)", result["closed"])
    except Exception as e:
        logger.exception("Options monitor failed: %s", e)


def task_credit_refresh():
    """Refresh Odds API credit balance from /v4/sports endpoint (free call)."""
    try:
        from odds.the_odds_api import refresh_credit_balance
        status = refresh_credit_balance()
        remaining = status.get("remaining")
        if remaining is not None:
            logger.info("Odds API credits: %s remaining", remaining)
    except Exception as e:
        logger.debug("Credit refresh skipped: %s", e)


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


def task_mlb_props_alert():
    """WS-A: scan MLB props inside per-game windows; alert + shadow-log edges.

    No-ops outside game windows. Reuses the prop-scout/Odds-API cache (no new
    Odds credits); statsapi is free."""
    import asyncio

    from signals.mlb_prop_alerts import run_prop_alert_scan

    res = asyncio.run(run_prop_alert_scan())
    if res.get("alerted") or res.get("scanned"):
        logger.info("mlb_props_alert: %s", res)


def task_mlb_props_scratch():
    """WS-A scratch guard: retract open prop alerts whose player drops from the
    confirmed lineup (cheap — uses cached schedule)."""
    from signals.mlb_prop_alerts import retract_scratched_alerts

    retract_scratched_alerts()


def task_mlb_props_resolve():
    """WS-A: box-score auto-resolution of open MLB prop shadows at game final."""
    from signals.mlb_prop_alerts import resolve_open_prop_shadows

    resolve_open_prop_shadows()


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
        for strategy, label in [("tweet_count_mc", "Tweet MC"), ("weather_ensemble", "Weather"), ("options_implied", "Options Implied")]:
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


def task_gdelt_refresh():
    """Pre-compute GDELT sentiment overlay every 6 hours.

    Writes results to storage/gdelt_cache.json so the election API
    can serve cached GDELT data without blocking on rate-limited queries.
    """
    import json as _json
    try:
        from signals.gdelt_client import build_gdelt_overlay
        result = build_gdelt_overlay()
        cache_path = PROJECT_ROOT / "storage" / "gdelt_cache.json"
        cache_path.write_text(_json.dumps(result))
        n_candidates = len(result.get("candidate_sentiment", []))
        n_states = len(result.get("state_sentiment", []))
        logger.info("GDELT overlay cached: %d candidates, %d states", n_candidates, n_states)
    except Exception as e:
        logger.exception("GDELT refresh failed: %s", e)


def task_ie_spending_refresh():
    """Pre-compute FEC IE spending overlay every 6 hours."""
    import json as _json
    try:
        from signals.election_tracker import _fetch_ie_spending_overlay
        result = _fetch_ie_spending_overlay()
        cache_path = PROJECT_ROOT / "storage" / "ie_spending_cache.json"
        cache_path.write_text(_json.dumps(result))
        n_surges = len(result.get("spending_surges", []))
        logger.info("IE spending overlay cached: %d surges", n_surges)
    except Exception as e:
        logger.exception("IE spending refresh failed: %s", e)


def task_election_snapshot():
    """Daily election market snapshot at 6am UTC — feeds trend DB."""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    if _state["election_snapshot_sent"] == today:
        return

    try:
        from signals.election_tracker import snapshot_elections, save_snapshot
        snapshot = snapshot_elections()
        save_snapshot(snapshot)
        total = snapshot.get("summary", {}).get("total_markets", 0)
        logger.info("Election snapshot saved: %d markets", total)
    except Exception as e:
        logger.exception("Election snapshot failed: %s", e)
        return

    _state["election_snapshot_sent"] = today


def task_election_weekly_report():
    """Weekly election PDF report — Monday 6am UTC."""
    year_week = datetime.now(timezone.utc).strftime("%Y%W")
    if _state["election_report_sent"] == year_week:
        return

    try:
        from signals.election_tracker import generate_report, save_snapshot
        from signals.election_pdf import generate_election_pdf

        report = generate_report()
        save_snapshot(report)
        pdf_path = generate_election_pdf(report)

        summary = report.get("summary", {})
        pc = summary.get("party_control", {})
        midterm = report.get("insights", {}).get("midterm", {})
        fl = midterm.get("flipping", {}).get("senate", {})

        logger.info(
            "Election weekly report generated: %s | %d markets | Senate D%.0f/R%.0f | %s",
            pdf_path,
            summary.get("total_markets", 0),
            pc.get("senate", {}).get("democrat", 0) * 100,
            pc.get("senate", {}).get("republican", 0) * 100,
            fl.get("net_shift_label", "?"),
        )

        # Send Discord alert if available
        try:
            from signals.discord_alerts import alert_election_report
            alert_election_report(report)
            logger.info("Election Discord alert sent")
        except Exception as e:
            logger.warning("Election Discord alert failed (non-fatal): %s", e)

    except Exception as e:
        logger.exception("Election weekly report failed: %s", e)
        return

    _state["election_report_sent"] = year_week


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


async def tick_1min():
    """Every 60 seconds: urgent stop-loss for positions resolving within 6 hours."""
    while True:
        await run_in_thread(_run_safe, "stop_evaluator_urgent", task_stop_evaluator_urgent)
        await asyncio.sleep(60)


async def tick_5min():
    """Every 5 minutes: health, stops, resolution, reeval, weather scan, alerts, calibration."""
    while True:
        await run_in_thread(_run_safe, "health_check", task_health_check)
        await run_in_thread(_run_safe, "stop_evaluator", task_stop_evaluator)
        await run_in_thread(_run_safe, "price_logger", task_price_logger)
        await run_in_thread(_run_safe, "book_logger", task_book_logger)
        await run_in_thread(_run_safe, "shadow_resolution", task_shadow_resolution)
        await run_in_thread(_run_safe, "paper_resolution", task_paper_resolution)
        await run_in_thread(_run_safe, "equity_snapshot", task_equity_snapshot)
        await run_in_thread(_run_safe, "resolution_scanner", task_resolution_scanner)
        await run_in_thread(_run_safe, "mlb_props_scratch", task_mlb_props_scratch)
        await run_in_thread(_run_safe, "weather_reeval", task_weather_reeval)
        await run_in_thread(_run_safe, "weather_fast_scan", task_weather_fast_scan)
        await run_in_thread(_run_safe, "weather_shift_alerts", task_weather_shift_alerts)
        await run_in_thread(_run_safe, "tweet_pace_alerts", task_tweet_pace_alerts)
        await run_in_thread(_run_safe, "calibration_check", task_calibration_check)
        logger.debug("5-min tick complete")
        await asyncio.sleep(300)


async def tick_30min():
    """Every 30 minutes: signal scans, options scan, edge alerts, source health."""
    while True:
        await run_in_thread(_run_safe, "signal_scan", task_signal_scan)
        await run_in_thread(_run_safe, "options_scan", task_options_scan)
        await run_in_thread(_run_safe, "options_resolution", task_options_resolution)
        await run_in_thread(_run_safe, "options_monitor", task_options_monitor)
        await run_in_thread(_run_safe, "whale_wall_alerts", task_whale_wall_alerts)
        await run_in_thread(_run_safe, "credit_refresh", task_credit_refresh)
        await run_in_thread(_run_safe, "source_health_touch", task_source_health_touch)
        await run_in_thread(_run_safe, "edge_alerts", task_edge_alerts)
        await run_in_thread(_run_safe, "mlb_props_alert", task_mlb_props_alert)
        await run_in_thread(_run_safe, "mlb_props_resolve", task_mlb_props_resolve)
        await run_in_thread(_run_safe, "kalshi_fade_scan", task_kalshi_fade_scan)
        logger.info("30-min tick complete")
        await asyncio.sleep(1800)


def task_state_cleanup():
    """Prune stale entries from scheduler state dicts to prevent memory growth."""
    pruned = 0
    # edge_alert_state: keep only last 100 entries
    for key in ("edge_alert_state", "mlb_props_alert_state", "pace_alert_sent", "milestone_sent"):
        d = _state.get(key, {})
        if len(d) > 100:
            # Keep most recent 50 (by key insertion order)
            keys_to_remove = list(d.keys())[:-50]
            for k in keys_to_remove:
                del d[k]
            pruned += len(keys_to_remove)
    # weather_shift_cache: bounded by open positions, but cap at 200
    wsc = _state.get("weather_shift_cache", {})
    if len(wsc) > 200:
        keys_to_remove = list(wsc.keys())[:-100]
        for k in keys_to_remove:
            del wsc[k]
        pruned += len(keys_to_remove)
    if pruned:
        logger.info("State cleanup: pruned %d stale entries", pruned)


def task_db_maintenance():
    """Archive old forecast_log rows and run VACUUM to reclaim space."""
    conn = _db()
    try:
        # Delete forecast_log rows older than 7 days
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        cursor = conn.execute(
            "DELETE FROM forecast_log WHERE timestamp < ?", (cutoff,)
        )
        deleted = cursor.rowcount
        if deleted > 0:
            logger.info("DB maintenance: deleted %d old forecast_log rows", deleted)
        conn.commit()
    except Exception as e:
        logger.debug("forecast_log cleanup skipped: %s", e)
    finally:
        conn.close()

    # VACUUM in separate connection (can't run inside transaction)
    try:
        conn2 = sqlite3.connect(str(DB_PATH))
        conn2.execute("VACUUM")
        conn2.close()
        logger.info("DB maintenance: VACUUM complete")
    except Exception as e:
        logger.warning("VACUUM failed: %s", e)


async def tick_6h():
    """Every 6 hours: arena snapshot, state cleanup, DB maintenance, GDELT/IE refresh."""
    while True:
        await run_in_thread(_run_safe, "arena_snapshot", task_arena_snapshot)
        await run_in_thread(_run_safe, "state_cleanup", task_state_cleanup)
        await run_in_thread(_run_safe, "db_maintenance", task_db_maintenance)
        await run_in_thread(_run_safe, "gdelt_refresh", task_gdelt_refresh)
        await run_in_thread(_run_safe, "ie_spending_refresh", task_ie_spending_refresh)
        await asyncio.sleep(21600)


async def tick_scheduled():
    """Check daily/weekly tasks every 10 minutes."""
    while True:
        now = datetime.now(timezone.utc)

        # Daily summary at 22:xx UTC
        if now.hour == 22:
            await run_in_thread(_run_safe, "daily_summary", task_daily_discord_summary)

        # Daily election snapshot at 6:xx UTC
        if now.hour == 6:
            await run_in_thread(_run_safe, "election_snapshot", task_election_snapshot)

        # Weekly election PDF report: Monday 6:xx UTC
        if now.weekday() == 0 and now.hour == 6:
            await run_in_thread(_run_safe, "election_report", task_election_weekly_report)

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
        asyncio.create_task(_delayed_start(3, tick_1min)),
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


def task_daily_portfolio_telegram():
    """Send daily paper portfolio report to Telegram (tree format)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _state.get("portfolio_report_sent") == today:
        return
    
    try:
        from scripts.daily_portfolio_report import get_portfolio_summary, format_report, send_telegram
        
        data = get_portfolio_summary()
        if data["cum_resolved"] == 0:
            return
        
        report = format_report(data)
        success = send_telegram(report)
        
        if success:
            _state["portfolio_report_sent"] = today
            logger.info("Daily portfolio report sent to Telegram")
        else:
            logger.warning("Portfolio report send failed - saved to pending/")
    except Exception as e:
        logger.exception(f"Portfolio report failed: {e}")

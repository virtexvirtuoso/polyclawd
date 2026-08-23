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

from services import task_state

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
    "recalibrate_sent": None,      # year+week string
}


# ============================================================================
# Helpers
# ============================================================================

def _db():
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.execute("PRAGMA busy_timeout=8000")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
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
        logger.warning("alert_api_down failed (count=%d)", count, exc_info=True)

    subprocess.run(["sudo", "systemctl", "restart", SERVICE_NAME], timeout=30)
    logger.info("Service restarted")


_task_locks: dict = {}


def _run_safe(name: str, fn, *args, **kwargs):
    """Run a function, catching all exceptions.

    Per-task non-blocking lock: if the same task is already running (e.g.
    live-burst trigger overlapping the periodic tick), skip instead of
    running concurrently — the monitors are stateful (score-snap tables)
    and the next tick picks up anything missed."""
    import threading
    lock = _task_locks.setdefault(name, threading.Lock())
    if not lock.acquire(blocking=False):
        logger.debug("Task %s already running — skipped", name)
        return None
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logger.exception("Task %s failed: %s", name, e)
    finally:
        lock.release()
        return None


# ============================================================================
# Task Schedule Registry — single source of truth for what runs when
#
# Each tick group lists task names.  The function is resolved at runtime via
# _task_fn(name) → globals()[f"task_{name}"], with overrides for the few
# names that don't follow the convention.
# ============================================================================

TICK_TASKS = {
    "30s": ["hf_signals"],

    "1min": [
        "stop_evaluator_urgent", "sport_whale_trades",
        # Moved from 5min tick 2026-07-10: in-play edges decay in 1-3 min.
        # All self-gate on "any live game?" (free ESPN check) so idle cost ~0.
        "soccer_live_monitor", "mlb_live_monitor", "ufc_live_monitor",
        "ingame_monitor", "cross_sport_drift",
    ],

    "5min": [
        "health_check", "stop_evaluator", "price_logger", "book_logger",
        "shadow_resolution", "paper_resolution", "equity_snapshot",
        "position_sync", "resolution_scanner", "mlb_props_scratch", "dashboard_warm",
        "weather_reeval", "weather_fast_scan", "weather_shift_alerts",
        "tweet_pace_alerts", "calibration_check", "insider_scan",
        "tier1_whale_alerts",
        "poly_delta", "manifold_shadow", "clv_snapshot",
        "hf_spread_5m",
        # UNGATED (Task 5.2): gate counters reset on the 15-min health-check
        # restart and would starve the dispatch-queue drain.
        "alert_drain",
    ],

    # Tasks gated to every Nth tick of a parent group
    "5min_gated": {"ufc_edge_scan": 3, "hf_spread_15m": 3},          # every 15min (3 × 5min)

    "30min": [
        "signal_scan", "options_scan", "options_resolution", "options_monitor",
        "whale_wall_alerts", "whale_outcomes", "whale_wallets",
        "credit_refresh", "source_health_touch", "stale_line_scan",
        "ufc_prop_scan", "edge_alerts", "mlb_props_alert",
        "stop_silence_alarm",
        "mlb_props_resolve", "baseball_resolve", "ufc_resolve", "nfl_resolve",
        "scorer_clv_snapshot", "kalshi_fade_scan",
        "pm_maker_shadow", "ensemble_recorder", "arb_scan",
        "resolution_edge_scan",
        "hf_backfill_outcomes",
    ],

    "30min_gated": {                              # every Nth × 30min
        "soccer_match_scan": 4,                   # every 2h
        "soccer_resolve": 4,
        "scorer_edge_scan": 4,                    # every 2h (same cadence as match scan)
        "nfl_edge_scan": 4,                       # every 2h (self-gates in off-season)
        "betfair_scan": 4,                        # every 2h (futures don't move fast)
        "hf_window_snapshot": 2,                   # every 1h — conditional WR data collection
        "hf_spread_scan": 2,                        # every 1h — cross-asset spread anomaly scan
    },

    "whale": ["whale_scanner", "whale_follower"],  # sequential, dynamic sleep
    "clob": ["whale_clob"],                        # dynamic sleep
    "vpin": ["vpin_scan"],

    "6h": [
        "arena_snapshot", "state_cleanup", "db_maintenance",
        "ie_spending_refresh", "gdelt_refresh", "ufc_event_discovery",
        "pm_leaderboard", "smart_wallet_resolve",
    ],
}

# Names where the task function doesn't follow task_{name} convention
_TASK_FN_ALIASES = {
    "daily_summary": "task_daily_discord_summary",
    "election_report": "task_election_weekly_report",
}


def _task_fn(name: str):
    """Resolve a task name to its function. Uses globals() lookup with alias support."""
    fn_name = _TASK_FN_ALIASES.get(name, f"task_{name}")
    return globals()[fn_name]


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
                logger.warning("alert_api_recovered failed", exc_info=True)
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


def task_position_sync():
    """Sync live positions: check for market resolutions + wallet balance."""
    from scripts.position_sync import run as _run
    result = _run()
    resolved = result.get("resolved", 0)
    new = result.get("new", 0)
    if resolved > 0 or new > 0:
        logger.info("position_sync: %d resolved, %d new positions detected", resolved, new)



def task_hf_signals():
    """Process HF signals → paper positions + resolve HF trades."""
    from services.hf_paper_trader import process_hf_signals, resolve_hf_positions
    result = process_hf_signals()
    if result.get("positions_opened", 0) > 0:
        logger.info("HF: opened %d positions", result["positions_opened"])
    resolve_hf_positions()



def task_hf_window_snapshot():
    """Capture HF window-open snapshots for conditional WR data collection.
    
    Runs every 1h (30min_gated every_n=2). Self-gates: only snapshots within
    ±7 minutes of an actual 1h or 4h window open to avoid stale data.
    """
    from datetime import datetime, timezone
    from services.hf_window_snapshot import snapshot_window_open
    
    now = datetime.now(timezone.utc)
    minute = now.minute
    
    # Gate: only run within ±7 min of a window open (minute 0 or 53-59)
    near_1h = minute <= 7 or minute >= 53
    near_4h = near_1h and (now.hour % 4 == 0 or (now.hour % 4 == 3 and minute >= 53))
    
    if near_1h:
        r = snapshot_window_open("1h")
        if r["written"]:
            logger.info("HF window snapshot (1h): %d rows written", r["written"])
    
    if near_4h:
        r = snapshot_window_open("4h")
        if r["written"]:
            logger.info("HF window snapshot (4h): %d rows written", r["written"])


def task_hf_backfill_outcomes():
    """Backfill resolved outcomes into hf_window_snapshots for WR analysis."""
    from services.hf_window_snapshot import backfill_outcomes
    r = backfill_outcomes()
    if r["updated"]:
        logger.info("HF window snapshot outcomes backfilled: %d", r["updated"])


def task_hf_spread_scan():
    """1h+4h cross-asset spread scan."""
    from services.hf_spread_scanner import scan_spreads
    alerts = scan_spreads(["1h", "4h"])
    if alerts:
        logger.info("HF spread anomalies [1h/4h]: %d flagged", len(alerts))

def task_hf_spread_5m():
    """5m crypto spread scan — runs every 5min."""
    from services.hf_spread_scanner import scan_spreads
    alerts = scan_spreads(["5m"])
    if alerts:
        logger.info("HF spread anomalies [5m]: %d flagged", len(alerts))

def task_hf_spread_15m():
    """15m crypto spread scan — runs every 15min."""
    from services.hf_spread_scanner import scan_spreads
    alerts = scan_spreads(["15m"])
    if alerts:
        logger.info("HF spread anomalies [15m]: %d flagged", len(alerts))

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


def _milestone_already_sent(strategy: str) -> bool:
    """Durable milestone guard (kv row), mirroring task_stop_silence_alarm.
    Backfilled as ALREADY-SENT for any strategy present in the historical
    alerts ledger, so the 2026-08-21 repair cannot replay old milestones."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("CREATE TABLE IF NOT EXISTS scheduler_kv (k TEXT PRIMARY KEY, v TEXT)")
        row = conn.execute("SELECT v FROM scheduler_kv WHERE k=?",
                           (f"milestone_sent:{strategy}",)).fetchone()
        conn.close()
        return row is not None
    except Exception as exc:  # noqa: BLE001 — a guard failure must not spam
        logger.warning("milestone guard read failed (%s); suppressing alert", exc)
        return True  # fail CLOSED: never alert when the guard is unreadable


def _mark_milestone_sent(strategy: str) -> None:
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("CREATE TABLE IF NOT EXISTS scheduler_kv (k TEXT PRIMARY KEY, v TEXT)")
        conn.execute("INSERT OR REPLACE INTO scheduler_kv (k, v) VALUES (?, ?)",
                     (f"milestone_sent:{strategy}", str(int(time.time()))))
        conn.commit()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("milestone guard write failed (%s)", exc)


STOP_SILENCE_THRESHOLD_SEC = 30 * 60   # heartbeat older than this = evaluator silent
STOP_SILENCE_REFIRE_SEC = 6 * 3600      # alarm re-fires at most once per 6h


def task_stop_silence_alarm(db_path=None, now=None):
    """Alarm ONLY if the stop-evaluator heartbeat is silent >30 min (D3:
    silence-alarm only — zero steady-state noise). Re-fire gated to once per
    6h via a kv DB row, NOT _state — the 15-min health-check restart wipes
    process memory. Healthy = silent."""
    path = str(db_path or DB_PATH)
    now_ts = int(now if now is not None else time.time())
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        # Writer (stop_evaluator) may not be deployed yet — create both tables
        # so registration order doesn't matter (plan Task 2.1 schema).
        conn.execute(
            "CREATE TABLE IF NOT EXISTS stop_heartbeat ("
            " id INTEGER PRIMARY KEY, ts INTEGER,"
            " positions_checked INTEGER, warnings_fired INTEGER)")
        conn.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        conn.commit()
        row = conn.execute("SELECT ts FROM stop_heartbeat WHERE id=1").fetchone()
        if row is None or row["ts"] is None:
            return  # no heartbeat ever written (writer not deployed) — nothing to judge
        age = now_ts - int(row["ts"])
        if age <= STOP_SILENCE_THRESHOLD_SEC:
            return
        gate = conn.execute("SELECT v FROM kv WHERE k='last_silence_alarm'").fetchone()
        if gate and now_ts - int(float(gate["v"])) < STOP_SILENCE_REFIRE_SEC:
            return
        from scripts.openclaw_alerts import alert_openclaw
        msg = (
            f"🚨 stop evaluator SILENT for {age // 60}m — no heartbeat in "
            f"shadow_trades.db (threshold 30m). Check the polyclawd-scheduler "
            f"stop_evaluator task."
        )
        if alert_openclaw(msg, parse_mode=None):
            # Arm the 6h gate only on a delivered alarm — a failed send retries
            # on the next 30-min tick.
            conn.execute(
                "INSERT INTO kv (k, v) VALUES ('last_silence_alarm', ?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(now_ts),))
            conn.commit()
    except Exception as e:
        logger.error("stop_silence_alarm failed: %s", e)
    finally:
        conn.close()


def task_alert_drain():
    """Flush the tiered alert dispatch queue (signals/alert_dispatch.py):
    tier-1 redeliveries first, then 15-min tier-2 batches. DB timestamps
    drive the timing, so the 15-min restarts are harmless."""
    from signals.alert_dispatch import drain
    n = drain()
    if n:
        logger.info("Alert drain: %d message(s) sent", n)


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


def task_poly_delta():
    """Snapshot PM mid price at +60s/+300s post-fill to measure adverse selection.
    See services/poly_delta_tracker.py."""
    from services.poly_delta_tracker import run_once
    run_once()


def task_manifold_shadow():
    """Shadow-log Manifold-vs-Polymarket leading-indicator edges (#3).
    See services/manifold_shadow.py."""
    from services.manifold_shadow import run_once as _manifold_run
    _manifold_run()


def task_clv_snapshot():
    """Snapshot CLV at T-10min before resolution for open PM positions.
    See services/clv_snapshot.py."""
    from services.clv_snapshot import run_once
    run_once()


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


def task_pm_maker_shadow():
    """Polymarket maker-shadow recorder (knob 7) -- PAPER-less evidence
    collection: snapshots tomorrow's PM weather brackets in the same evening
    windows and records hypothetical resting orders. Fills judged next morning
    from the public trade tape. No positions, no DB writes."""
    from api.routes.engine import load_engine_state
    if not load_engine_state().get("pm_maker_shadow_enabled", True):
        return
    from signals.pm_maker_shadow import record_evening
    record_evening()


def task_ensemble_recorder():
    """Evening 82-member ensemble snapshot per city (knob 6 training data --
    perishable; see signals/ensemble_recorder.py). Pure data capture."""
    from api.routes.engine import load_engine_state
    if not load_engine_state().get("ensemble_recorder_enabled", True):
        return
    from signals.ensemble_recorder import record
    record()


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
            # Milestone alert — fires the first time an auto-scorecard EXISTS.
            # Moved here 2026-08-21 (/qa audit). It previously sat in the `else`
            # (below-threshold) branch and referenced `records`, `n`, `wr`,
            # `brier` — none bound in that scope — so it raised NameError on
            # every tick and was swallowed by the except. This alert has never
            # fired. The else-branch was also wrong by construction: auto_card
            # is None there, i.e. the milestone had NOT been reached.
            # Durable flag, NOT _state: process memory is wiped on every restart
            # (14 restarts in the last 7 days), and the ledger shows 89 prior
            # milestone alerts including 5 for weather_ensemble inside 90 min.
            # Gate on the FIRST CROSSING to >=20, not on auto_card merely
            # existing — otherwise a mature n=197 strategy re-announces
            # "First 20 Resolutions!" after every restart.
            _n = auto_card["n"]
            if _n >= 20 and not _milestone_already_sent(strategy):
                try:
                    from signals.discord_alerts import alert_scorecard_milestone
                    _wr = auto_card["win_rate"]
                    alert_scorecard_milestone(
                        strategy, _n, round(_wr * _n), _wr, auto_card["brier"]
                    )
                    _mark_milestone_sent(strategy)
                except Exception:
                    logger.warning("alert_scorecard_milestone failed (strategy=%s)",
                                   strategy, exc_info=True)
        else:
            # Look up raw count even when below threshold so we can see growth
            from signals.resolution_logger import load_auto_resolutions
            n_auto = len(load_auto_resolutions(strategy))
            logger.info("CALIBRATION %s model: %d/20 (collecting auto-resolved)",
                        strategy, n_auto)


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


def task_soccer_whale_trades():
    """Live whale taker-flow alert for active WC soccer games. Every 60s."""
    from scripts.soccer_whale_trades import run as _run
    _run()


def task_soccer_live_monitor():
    """Soccer live game monitor — all leagues (WC, MLS, EPL, UCL, LaLiga).
    Fires run/drift/whale/edge-inversion alerts for active games."""
    from scripts.soccer_live_monitor import main as _main
    _main()


def task_mlb_live_monitor():
    """MLB live game monitor — run trigger, line drift, edge inversion.
    Alert-only: does NOT overlap with ingame_monitor.py (stop-loss stays there)."""
    from scripts.mlb_live_monitor import main as _main
    _main()


def task_ingame_monitor():
    """MLB in-game shadow monitor — stop-loss (−40%), take-profit (+67%), edge inversion.
    Runs every 5min during MLB game hours (12:00–01:00 ET). Does not overlap with
    task_mlb_live_monitor (alert-only). See signals/ingame_monitor.py."""
    import datetime as _dt
    now_et = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    # UTC hours 16–05 = ET noon–1am (covers all MLB windows + extra buffer)
    h = _dt.datetime.utcnow().hour
    if not (16 <= h or h < 6):
        return  # skip overnight/early morning ticks
    from signals.ingame_monitor import run_monitor
    run_monitor()


def task_cross_sport_drift():
    """Cross-sport Pinnacle line drift scanner — MLB, WC Soccer, MLS, UFC.
    Fires drift alert + PM gap when Vegas moves > threshold since last snapshot.
    Credits: ~768/day (bookmakers=pinnacle, 1 credit per sport per tick)."""
    from scripts.cross_sport_drift import run as _run
    _run()


def task_ufc_live_monitor():
    """UFC live fight monitor — round trigger, line drift, edge inversion.
    Alert-only: fires on round transitions and fight conclusions."""
    from scripts.ufc_live_monitor import main as _main
    _main()


def task_sport_whale_trades():
    """Multi-sport whale trade alert — WC Soccer, MLS, MLB, UFC, NBA.
    Replaces soccer_whale_trades for all sports. Every 60s."""
    from scripts.sport_whale_trades import run as _run
    _run()


def task_pm_leaderboard():
    """Polymarket leaderboard discovery — scrapes top traders, seeds new wallets,
    promotes qualifying wallets to smart status, alerts on new discoveries."""
    from scripts.pm_leaderboard_scraper import run as _run
    result = _run()
    if result.get("new") or result.get("graduated"):
        logger.info("PM leaderboard: %d new, %d graduated", result["new"], result["graduated"])


def task_ufc_edge_scan():
    """UFC edge scan: compares Polymarket + Kalshi vs Pinnacle.
    Self-gating: skips if no events within 6h window (0 credits)."""
    from signals.ufc_edge_cron import run_ufc_edge_scan
    result = run_ufc_edge_scan()
    if result.get("scanned"):
        logger.info("UFC edge scan: %d edges, %d alerts, %d credits",
                     result["edges_found"], result["alerts_sent"], result["credits_used"])
    else:
        logger.debug("UFC edge scan: no active events, skipped")


def task_ufc_event_discovery():
    """UFC event discovery: polls Odds API events list (FREE, 0 credits)."""
    from odds.ufc_event_discovery import discover_events, store_events, _get_db
    events = discover_events()
    if events:
        conn = _get_db()
        store_events(conn, events)
        conn.close()
        logger.info("UFC event discovery: %d events stored", len(events))


def task_ufc_prop_scan():
    """UFC cross-platform prop edge scan (Polymarket Gamma vs Kalshi), alerts on
    tradeable edges. Free APIs + self-gating (Kalshi prop series only list near
    fight night), so most runs send nothing. Sole delivery path (no cron)."""
    from odds.ufc_prop_edge import run_prop_edge_scan
    result = run_prop_edge_scan()
    if result.get("edges_found") or result.get("alerts_sent"):
        logger.info("UFC prop scan: %d edges, %d alerts, %d suppressed",
                     result["edges_found"], result["alerts_sent"], result.get("suppressed", 0))
    else:
        logger.debug("UFC prop scan: no prop edges")


def task_insider_scan():
    """Scan for insider trading activity on Polymarket.
    Runs every 5min — insider trades decay fast."""
    try:
        from signals.insider_detector import scan_for_insiders, send_alerts
        results = scan_for_insiders()
        if results:
            send_alerts(results)
            logger.info("Insider scan: %d alerts sent", len(results))
        else:
            logger.debug("Insider scan: no new activity")
    except Exception as e:
        logger.exception("Insider scan failed: %s", e)


def task_arb_scan():
    """Cross-platform arbitrage scan: Kalshi vs Polymarket.
    Runs every 30min — arb opportunities persist longer."""
    try:
        import subprocess, sys
        from pathlib import Path
        proc = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "arb_alert.py")],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            logger.warning("arb_alert stderr: %s", proc.stderr[:300])
        else:
            out = proc.stdout.strip()
            if out:
                logger.info("arb_alert: %s", out[:200])
    except Exception as e:
        logger.warning("arb_alert failed: %s", e)


def task_resolution_edge_scan():
    """Resolution-source edge scan for weather markets.
    NWS edge for Kalshi, TWC edge for Polymarket. 30min cadence.
    Logs shadow trades for all PM signals, executes when in LIVE mode."""
    try:
        from signals.weather_resolution_edge import scan_resolution_edges
        signals = scan_resolution_edges()
        high = [s for s in signals if s.get("conviction_tier") == "HIGH"]
        if signals:
            logger.info("resolution_edge: %d signals (%d HIGH)", len(signals), len(high))
        if high:
            for s in high:
                logger.info("resolution_edge HIGH: %s %s edge=%+.1fpp %s (thr=%.0f)",
                            s["city"], s["resolution_source"], s["edge_pp"],
                            s["direction"], s["threshold_f"])

        # Log shadow trades for all PM weather resolution signals
        pm_signals = [s for s in signals if s.get("platform") == "polymarket" and s.get("condition_id")]
        if pm_signals:
            from signals.shadow_tracker import log_shadow_trade
            logged = 0
            for s in pm_signals:
                shadow_signal = _weather_signal_to_shadow(s)
                if shadow_signal and log_shadow_trade(shadow_signal):
                    logged += 1
            if logged:
                logger.info("resolution_edge: logged %d/%d PM shadow trades", logged, len(pm_signals))

            # Execute tradeable PM weather edges in LIVE mode
            from execution.weather_executor import execute_tradeable_weather_edges
            result = execute_tradeable_weather_edges(pm_signals)
            if result.get("filled", 0) > 0:
                logger.info("resolution_edge: weather executor filled %d/%d",
                            result["filled"], len(pm_signals))
    except Exception as e:
        logger.debug("resolution_edge_scan: %s", e)


def _weather_signal_to_shadow(s: dict) -> dict:
    """Convert a weather resolution edge signal dict to shadow_trade format."""
    direction = s.get("direction", "buy_no")
    side = "NO" if direction == "buy_no" else "YES"
    market_price = s.get("market_price", 0.5)
    twc_implied = s.get("twc_implied_prob", 0.5)
    edge_pp = s.get("edge_pp", 0)
    horizon_hours = s.get("horizon_hours", 24)

    if direction == "buy_no":
        confidence = (1.0 - twc_implied) * 100
    else:
        confidence = twc_implied * 100

    bracket_low = s.get("bracket_low_f")
    bracket_high = s.get("bracket_high_f")
    if bracket_low and bracket_high:
        market_desc = f"{s['city']} {bracket_low:.0f}-{bracket_high:.0f}F"
    else:
        market_desc = f"{s['city']} {s.get('threshold_f', 0):.0f}F"

    return {
        "market_id": s.get("condition_id", ""),
        "market": s.get("market_title", market_desc)[:200],
        "category": "weather_resolution",
        "category_tier": s.get("conviction_tier", "LOW"),
        "platform": "polymarket",
        "side": side,
        "price": market_price,
        "entry_price": (round(1.0 - market_price, 4)
                        if side == "NO" else market_price),  # held-side cost
        "confidence": confidence,
        "confirmations": 1,
        "days_to_close": max(0.5, horizon_hours / 24),
        "volume": 0,
        "reasoning": f"TWC={s.get('twc_forecast_f','?')}F RMSE={s.get('twc_rmse','?')} edge={edge_pp:+.1f}pp tier={s.get('conviction_tier','?')}",
        "archetype": "weather_resolution",
        "strategy": "twc_resolution_edge",
    }


def task_stale_line_scan():
    """Multi-book stale line scan: soft sportsbooks vs Pinnacle across active sports.
    Self-gating via NEAR_WINDOW_HOURS (0 credits when no events are near kickoff).
    Sole delivery path — do NOT also run this as a cron (two schedulers would
    double the Odds API spend and the cooldown store is per-process best-effort)."""
    from signals.stale_line_alerts import run_stale_line_scan
    result = run_stale_line_scan()
    if result.get("lines_found") or result.get("alerts_sent"):
        logger.info("Stale line scan: %d lines, %d alerts, %d suppressed",
                     result["lines_found"], result["alerts_sent"], result.get("suppressed", 0))
    else:
        logger.debug("Stale line scan: no stale lines")


def _send_whale_alert_tg():
    """Send whale alerts to Telegram via the alert_tg helper script.
    The script fetches enriched alerts from the API and handles dedup."""
    import subprocess
    try:
        proc = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "whale_alert_tg.py")],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            logger.warning("whale_alert_tg stderr: %s", proc.stderr[:300])
        else:
            out = proc.stdout.strip()
            if out:
                logger.info("whale_alert_tg: %s", out[:200])
    except Exception as e:
        logger.warning("whale_alert_tg failed: %s", e)


def task_whale_scanner():
    """Thin-market whale scanner: Kalshi weather + Polymarket props, 5-min tick."""
    from signals.whale_scanner import run_scan
    from signals.discord_alerts import alert_whale_scan

    COOLDOWN = 1200  # 20 min per market
    now = time.time()

    if "whale_alert_cooldown" not in _state:
        _state["whale_alert_cooldown"] = {}

    try:
        alerts = run_scan()
        for a in alerts:
            market = a.get("market", "")
            score = a.get("score", 0)
            severity = a.get("severity", "LOW")

            if score < 8:   # CRITICAL only (per Mr. V 2026-06-11)
                continue

            last = _state["whale_alert_cooldown"].get(market, 0)
            if now - last < COOLDOWN:
                continue

            _state["whale_alert_cooldown"][market] = now
            alert_whale_scan(a)
            logger.info(
                "Whale scanner alert [%s]: %s score=%d %s",
                severity, market, score, a.get("reasons", ""),
            )
    except Exception as e:
        logger.exception("Whale scanner failed: %s", e)


def task_whale_follower():
    """PAPER shadow follower of informed whale flow. No real orders ever —
    writes only whale_follows in whale_meta.db (see signals/whale_follower.py
    docstring + the 2026-06-12 design doc for kill criteria K1-K6)."""
    from signals.whale_follower import run_pass
    stats = run_pass()
    if stats.get("entered") or stats.get("closed"):
        logger.info("Whale follower: %s", stats)


def task_whale_outcomes():
    """Label whale alerts with subsequent prices + resolution (shadow loop)."""
    from signals.whale_outcomes import run_pass
    stats = run_pass()
    if stats.get("ingested") or stats.get("filled"):
        logger.info("Whale outcomes: %s", stats)


def task_whale_wallets():
    """Refresh the PM wallet ledger from the seen-queue."""
    from signals.whale_wallets import get_meta_db, refresh_wallets
    conn = get_meta_db()
    try:
        stats = refresh_wallets(conn)
        if stats.get("refreshed"):
            logger.info("Whale wallets: %s", stats)
    finally:
        conn.close()


def task_smart_wallet_resolve():
    """Resolve outstanding smart-wallet shadow alerts (Gate-2 calibration)."""
    import sqlite3
    from scripts.smart_wallet_alert import (SHADOW_DB, init_shadows,
                                            resolve_shadows,
                                            settle_via_market_resolution)
    conn = sqlite3.connect(str(SHADOW_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        init_shadows(conn)
        n = resolve_shadows(conn, settle_via_market_resolution)
        if n:
            logger.info("Smart-wallet shadows resolved: %d", n)
    finally:
        conn.close()


def task_smart_wallet_fast():
    """Fast-poll smart wallet fills every 90s (see tick_smart_wallet).
    Lightweight — only last 3 min of PM trades filtered to smart addresses.
    Decoupled from heavy whale_scanner so convergence fires within 90s."""
    from scripts.smart_wallet_fast_poll import run as _run
    result = _run()
    if result.get("alerts_fired"):
        logger.info("Smart wallet fast poll: %s", result)
    elif result.get("error"):
        logger.warning("Smart wallet fast poll error: %s", result["error"])


def task_whale_clob():
    """Scan CLOB order books for large resting orders from known whales."""
    from signals.whale_clob import run_scan
    result = run_scan()
    if result.get("alerts"):
        logger.info("Whale CLOB: %s", result)


def task_tier1_whale_alerts():
    """Process whale alerts through Tier-1 conviction pipeline.

    Runs every 5min: reads from the whale_alerts DB table, evaluates for Tier-1,
    logs sized alerts, fires Tier-1 alerts to Telegram, resolves pending.
    """
    try:
        from signals.tier1_whale_alert import process_whale_alerts, resolve_tier1_alerts

        resolve_tier1_alerts()

        try:
            conn = _db()
            rows = conn.execute(
                "SELECT * FROM whale_alerts WHERE ts > datetime('now', '-24 hours') "
                "ORDER BY ts DESC LIMIT 200"
            ).fetchall()
            conn.close()
            if rows:
                alerts = [dict(r) for r in rows]
                result = process_whale_alerts(alerts)
                if result["tier1_fired"] > 0:
                    logger.info(
                        "TIER-1: %d fired, %d sized, %d processed",
                        result["tier1_fired"], result["sized_alerts"], result["alerts_processed"],
                    )
                elif result["sized_alerts"] > 0:
                    logger.debug(
                        "TIER-1: %d sized alerts (no Tier-1), %d processed",
                        result["sized_alerts"], result["alerts_processed"],
                    )
        except Exception as e:
            logger.debug("Tier-1 whale alert DB read failed: %s", e)
    except Exception as e:
        logger.error("Tier-1 whale alert task failed: %s", e)


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
                url = f"https://polymarket.com/market/{slug}" if slug else ""
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
                url = f"https://polymarket.com/market/{slug}" if slug else ""
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
    """WS-A: box-score auto-resolution of open MLB prop shadows at game final,
    plus control-sample grading (calibration integrity)."""
    from signals.mlb_prop_alerts import resolve_open_prop_shadows, resolve_scan_log_outcomes

    resolve_open_prop_shadows()
    resolve_scan_log_outcomes()


def task_dashboard_warm():
    """Pre-warm slow dashboard caches so NO user request ever eats a cold compute:
    baseball scout (last_n=10, the dashboard key vs the alert scan's last_n=20),
    baseball Kalshi scan (cold ~28s), and options IV/RV (cold ~8s yfinance refetch
    on the 1h RV-cache expiry). All effectively free (reuse cached/free upstreams)."""
    import asyncio

    async def _warm():
        from odds.mlb_prop_scout import get_prop_scout
        from odds.kalshi_props import get_kalshi_prop_scan
        try:
            await get_prop_scout(last_n=10, min_edge=-0.99, min_games=7)
        except Exception as e:
            logger.debug("scout warm skipped: %s", e)
        try:
            await get_kalshi_prop_scan(min_edge=2.0, last_n=10)
        except Exception as e:
            logger.debug("kalshi warm skipped: %s", e)

    asyncio.run(_warm())
    try:
        from signals.vol_spread import get_iv_rv_status
        get_iv_rv_status()  # options.html — warms vol_cache.json (yfinance RV)
    except Exception as e:
        logger.debug("iv-rv warm skipped: %s", e)


def task_soccer_match_scan():
    """Soccer/WC: refresh the per-match 3-way edge cache (~every 2h) and
    execute tradeable edges when in LIVE mode."""
    import asyncio

    from scripts.sports_edge_scan import run
    from odds.soccer_match_edge import find_soccer_match_edges
    from execution.soccer_executor import execute_tradeable_soccer_edges

    # 1. Find + enrich edges (same as before)
    edges = asyncio.run(find_soccer_match_edges(min_edge=0.03))

    # 2. Cache for dashboard
    asyncio.run(run(["soccer_match"]))

    # 3. Execute tradeable edges in LIVE mode
    if edges:
        tradeable = [e for e in edges if getattr(e, "tradeable", False)]
        if tradeable:
            logger.info("soccer_match_scan: %d tradeable edges found", len(tradeable))
            result = execute_tradeable_soccer_edges(tradeable)
            logger.info("soccer_match_scan: execution result %s", result)
        else:
            logger.debug("soccer_match_scan: %d edges, none tradeable", len(edges))


def task_soccer_futures_scan():
    """Soccer/WC: refresh the outright (World Cup winner) edge cache (daily)."""
    import asyncio

    from scripts.sports_edge_scan import run

    asyncio.run(run(["soccer_futures", "soccer_wc_board"]))


def task_scorer_clv_snapshot():
    """Scorer CLV: snapshot DK/FD anytime-goalscorer lines for upcoming matches.
    Feeds the CLV logger's pre/close comparison and control-corrected gate."""
    from scripts.scorer_paper_logger import db_connect, live_snapshot

    con = db_connect(str(PROJECT_ROOT / "storage" / "scorer_clv.db"))
    live_snapshot(con, "soccer_fifa_world_cup", window_hours=30.0)
    con.close()


def task_scorer_edge_scan():
    """Scorer edge scan: find anytime-goalscorer edges + enrich with ESPN stats.
    Runs every 2h alongside soccer_match_scan."""
    import os
    import requests as _req
    from odds.scorer_edge import find_scorer_edges, ScorerSportConfig

    sport_key = "soccer_fifa_world_cup"
    config = ScorerSportConfig(sport_key=sport_key)
    api_key = os.environ.get("ODDS_API_KEY", "")
    if not api_key:
        logger.warning("scorer_edge_scan: no ODDS_API_KEY")
        return

    # Fetch events with scorer props
    try:
        r = _req.get(
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/events",
            params={"apiKey": api_key}, timeout=15,
        )
        events = r.json() if r.status_code == 200 else []
    except Exception as e:
        logger.warning(f"scorer_edge_scan: events fetch failed: {e}")
        return

    # Fetch odds for each event (limit to 5 nearest events to save credits)
    events_odds = []
    for ev in events[:5]:
        eid = ev.get("id", "")
        try:
            r2 = _req.get(
                f"https://api.the-odds-api.com/v4/sports/{sport_key}/events/{eid}/odds",
                params={
                    "apiKey": api_key,
                    "regions": config.regions,
                    "markets": "player_goal_scorer_anytime",
                    "oddsFormat": "american",
                },
                timeout=15,
            )
            if r2.status_code == 200:
                events_odds.append(r2.json())
        except Exception:
            pass

    if not events_odds:
        return

    edges = find_scorer_edges(events_odds, config)
    logger.info(f"scorer_edge_scan: {len(edges)} edges from {len(events_odds)} events")

    # Enrich with ESPN stats
    try:
        from odds.soccer_stats import fetch_scorers, enrich_scorer_edge
        from odds.sports_edge_common import log_enrichment
        # Pre-fetch tournament stats (cached 1h)
        fetch_scorers(sport_key)
        for edge in edges:
            enrichment = enrich_scorer_edge(edge, sport_key)
            if enrichment:
                log_enrichment(
                    shadow_trade_id=None,
                    sport="scorer",
                    stats_score=enrichment.get("goals_per_match", 0.0),
                    stats_confirmation=enrichment.get("stats_agrees", False) or False,
                    alert_tier="confirmed" if enrichment.get("stats_agrees") else "speculative",
                    stats_detail=f"{enrichment.get('goals', 0)}G/{enrichment.get('matches', 0)}M "
                                 f"GPG={enrichment.get('goals_per_match', 0):.2f} "
                                 f"({enrichment.get('team', '')})",
                )
    except Exception as e:
        logger.warning(f"scorer_edge_scan: enrichment failed: {e}")


def task_soccer_resolve():
    """Soccer/WC: resolve closed shadows + capture PM closing mid (CLV) + feed
    soccer_forecast_log calibration."""
    from signals.soccer_resolver import scan_resolved_soccer_trades

    scan_resolved_soccer_trades()


def task_baseball_resolve():
    """Resolve closed Polymarket baseball game shadows (moneyline/spread/total)
    via CLOB per-market lookup."""
    from signals.baseball_resolver import scan_resolved_baseball_games

    scan_resolved_baseball_games()


def task_ufc_resolve():
    """Resolve closed Polymarket UFC fight shadows via CLOB per-market lookup."""
    from signals.ufc_resolver import scan_resolved_ufc_trades

    scan_resolved_ufc_trades()


def task_nfl_resolve():
    """Resolve closed Polymarket NFL game shadows via CLOB + capture CLV."""
    from signals.nfl_resolver import scan_resolved_nfl_trades
    scan_resolved_nfl_trades()


def task_nfl_edge_scan():
    """NFL edge scan: consensus devig vs Polymarket. Self-gating: skips if
    no games within the 42-day window (off-season = 0 credits). Scans both
    regular season and preseason sport keys."""
    import asyncio
    from odds.nfl_edge import find_nfl_edges, CFG
    from odds.sports_edge_common import summarize
    edges = asyncio.run(find_nfl_edges())
    if edges:
        summarize(edges, CFG)
        logger.info(f"NFL edge scan: {len(edges)} edges")
        # Fire Telegram alerts for tradeable edges above threshold (dedup'd)
        try:
            from signals.sport_edge_alerts import run_sport_edge_alerts
            res = run_sport_edge_alerts(edges, sport="NFL")
            if res.get("alerted"):
                logger.info(f"NFL edge alerts: {res['alerted']} fired")
        except Exception as e:
            logger.debug(f"NFL edge alert step failed: {e}")
    else:
        logger.debug("NFL edge scan: no edges (off-season or no games)")


_BETFAIR_DEDUP_FILE = Path("/tmp/betfair_dedup.json")

def _load_betfair_dedup() -> dict:
    try:
        if _BETFAIR_DEDUP_FILE.exists():
            return json.loads(_BETFAIR_DEDUP_FILE.read_text())
    except Exception:
        pass
    return {}

def _save_betfair_dedup(d: dict) -> None:
    try:
        _BETFAIR_DEDUP_FILE.write_text(json.dumps(d))
    except Exception:
        pass

def task_betfair_scan():
    """Betfair Exchange vs Polymarket futures edge scan."""
    import asyncio
    from odds.betfair_edge import find_betfair_edges
    edges = asyncio.run(find_betfair_edges(min_edge=0.05))
    if not edges:
        logger.debug("Betfair scan: no edges")
        return
    now = time.time()
    betfair_dedup = _load_betfair_dedup()
    to_alert = []
    for e in edges:
        key = f"{e.sport}|{e.selection}"
        last = betfair_dedup.get(key, 0)
        if now - last >= 14400:  # 4h dedup
            to_alert.append(e)
            betfair_dedup[key] = now
    _save_betfair_dedup(betfair_dedup)
    if to_alert:
        lines = [f"⚡ Betfair Futures Edges ({len(to_alert)})"]
        for e in to_alert[:5]:
            pm_str = f"{e.polymarket_price*100:.0f}¢" if e.polymarket_price else "N/A"
            lines.append(
                f"  {e.selection} ({e.sport}): Betfair {e.betfair_prob*100:.1f}% vs PM {pm_str} → {e.edge_pct*100:+.1f}%"
            )
        msg = "\n".join(lines)
        logger.info(msg)
        try:
            from scripts.alert_formatter import send_telegram
            send_telegram(msg)
        except Exception:
            logger.warning("send_telegram (betfair) failed", exc_info=True)
    logger.info(f"Betfair scan: {len(edges)} edges, {len(to_alert)} alerted")


def task_arena_snapshot():
    """AI arena leaderboard snapshot."""
    venv = str(PROJECT_ROOT / "venv" / "bin" / "python3")
    result = subprocess.run(
        [venv, str(PROJECT_ROOT / "signals" / "ai_model_tracker.py"), "snapshot"],
        capture_output=True, timeout=60, text=True,
    )
    if result.returncode != 0:
        logger.error(
            "Arena snapshot FAILED (rc={}): {}", result.returncode, (result.stderr or "")[-500:]
        )
    else:
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
    Self-gating: skips if cache is less than 6h old (prevents burst on restart).
    """
    import json as _json
    cache_path = PROJECT_ROOT / "storage" / "gdelt_cache.json"
    if cache_path.exists():
        age_s = time.time() - cache_path.stat().st_mtime
        if age_s < 21600:
            logger.info("GDELT cache fresh (%.1fh old), skipping refresh", age_s / 3600)
            return
    try:
        from signals.gdelt_client import build_gdelt_overlay
        result = build_gdelt_overlay()
        # Only overwrite the cache when the fetch actually returned data —
        # a rate-limited (429) empty result must never blank a good cache.
        if result.get("candidate_sentiment") or result.get("state_sentiment"):
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
        for name in TICK_TASKS["30s"]:
            await run_in_thread(_run_safe, name, _task_fn(name))
        await asyncio.sleep(30)


async def tick_1min():
    """Every 60 seconds: urgent stop-loss + sport whale trade alerts."""
    while True:
        for name in TICK_TASKS["1min"]:
            await run_in_thread(_run_safe, name, _task_fn(name))
        await asyncio.sleep(60)


# ── Live score burst (Phase 2 latency fix, 2026-07-10) ──────────────────────
# Poll ESPN scoreboards every 20s while games are live; on any score/round/
# status change, immediately run the matching live monitor instead of waiting
# for the next 1-min tick. Alert lag target: ~25s from event.
# ESPN polls are free (no API credits). Backs off to 120s when nothing live.

BURST_ENDPOINTS = {
    "soccer_live_monitor": [
        "soccer/fifa.world/scoreboard", "soccer/usa.1/scoreboard",
        "soccer/eng.1/scoreboard", "soccer/uefa.champions/scoreboard",
        "soccer/esp.1/scoreboard",
    ],
    "mlb_live_monitor": ["baseball/mlb/scoreboard"],
    "ufc_live_monitor": ["mma/ufc/scoreboard"],
}
_burst_state: dict = {}
_burst_moves_seen: float = 0.0  # ts of newest PM move event already processed


def _read_pm_moves():
    """Read poly:moves:recent from memcached (written by polyclawd-ws).

    Phase 3: sharp PM flow often reprices a live game before the ESPN
    scoreboard updates — a >=10pp/60s mid move is treated like a score change."""
    import socket
    try:
        s = socket.create_connection(("localhost", 11211), timeout=0.4)
        s.settimeout(0.4)
        s.sendall(b"get poly:moves:recent\r\n")
        buf = b""
        while b"END\r\n" not in buf and len(buf) < 500_000:
            chunk = s.recv(8192)
            if not chunk:
                break
            buf += chunk
        s.close()
        if not buf.startswith(b"VALUE"):
            return []
        body = buf.split(b"\r\n", 1)[1].rsplit(b"\r\nEND\r\n", 1)[0]
        import json as _json
        return _json.loads(body)
    except Exception:
        return []


def _burst_fingerprint(path: str):
    """Return (fingerprint, n_live) for one ESPN scoreboard.

    Fingerprint covers every live event's per-competition status name, period
    and scores — so goals, runs, round transitions and fight finishes all
    register as a change. UFC cards are one event with many competitions."""
    import requests
    r = requests.get(
        f"https://site.api.espn.com/apis/site/v2/sports/{path}", timeout=8
    )
    r.raise_for_status()
    parts, n_live = [], 0
    for ev in r.json().get("events", []):
        state = ((ev.get("status") or {}).get("type") or {}).get("state")
        if state != "in":
            continue
        n_live += 1
        for comp in ev.get("competitions") or []:
            st = comp.get("status") or ev.get("status") or {}
            parts.append((
                comp.get("id"),
                (st.get("type") or {}).get("name"),
                st.get("period"),
                tuple(c.get("score") for c in comp.get("competitors") or []),
            ))
    return tuple(parts), n_live


async def tick_live_burst():
    """Every 20s while games are live: ESPN change detection → immediate
    monitor run. Monitors are idempotent (DB score-snap state) and _run_safe's
    per-task lock prevents overlap with the 1-min tick."""
    global _burst_moves_seen
    while True:
        any_live = False
        live_by_task: dict = {}
        for task, paths in BURST_ENDPOINTS.items():
            changed = False
            for path in paths:
                try:
                    fp, n_live = await run_in_thread(_burst_fingerprint, path)
                except Exception as e:
                    logger.debug("live burst: %s fetch failed: %s", path, e)
                    continue
                if n_live:
                    any_live = True
                    live_by_task[task] = True
                prev = _burst_state.get(path)
                if prev is not None and fp != prev:
                    changed = True
                _burst_state[path] = fp
            if changed:
                logger.info("live burst: score/status change → %s", task)
                await run_in_thread(_run_safe, task, _task_fn(task))

        # Phase 3: PM websocket fast moves (>=10pp/60s) — treat like a score
        # change for every sport that currently has a live game. Sharp flow
        # front-runs the scoreboard, so this often fires BEFORE ESPN updates.
        if any_live:
            moves = await run_in_thread(_read_pm_moves)
            fresh = [m for m in moves if m.get("ts", 0) > _burst_moves_seen]
            if fresh:
                _burst_moves_seen = max(m["ts"] for m in fresh)
                logger.info("live burst: %d PM fast move(s) → running live monitors", len(fresh))
                for task in live_by_task:
                    await run_in_thread(_run_safe, task, _task_fn(task))

        await asyncio.sleep(20 if any_live else 120)


async def tick_5min():
    """Every 5 minutes: health, stops, resolution, reeval, weather scan, alerts, calibration."""
    while True:
        for name in TICK_TASKS["5min"]:
            await run_in_thread(_run_safe, name, _task_fn(name))
        # Gated tasks — run every Nth tick
        for name, every_n in TICK_TASKS["5min_gated"].items():
            if task_state.should_run_safe(name, every_n * 300):
                await run_in_thread(_run_safe, name, _task_fn(name))
        logger.debug("5-min tick complete")
        await asyncio.sleep(300)


async def tick_whale():
    """Whale scanner on its own loop: the full-exchange sweep + book pass can
    take minutes and must never delay stop evaluation in tick_5min."""
    while True:
        t0 = time.time()
        # Sequential — follower rides whale_scanner so ts_entry stays close to ts_alert
        for name in TICK_TASKS["whale"]:
            await run_in_thread(_run_safe, name, _task_fn(name))
        await asyncio.sleep(max(60, 300 - (time.time() - t0)))


async def tick_clob():
    """CLOB order flow scanner: polls order books every 60s for large
    resting orders from known whale wallets."""
    while True:
        t0 = time.time()
        for name in TICK_TASKS["clob"]:
            await run_in_thread(_run_safe, name, _task_fn(name))
        await asyncio.sleep(max(10, 60 - (time.time() - t0)))


async def tick_smart_wallet():
    """Fast-poll smart wallet fills every 90s.

    Decoupled from tick_whale() (heavy full-exchange sweep). Only fetches
    the last 3 min of PM trades filtered to ~40 smart wallet addresses.
    Enables sub-2-minute convergence detection for live sports markets.
    Rate: ~40 req / 90s = 0.44 req/sec — well within PM CLOB limits.
    """
    while True:
        t0 = time.time()
        await run_in_thread(_run_safe, "smart_wallet_fast", task_smart_wallet_fast)
        await asyncio.sleep(max(30, 90 - (time.time() - t0)))


async def tick_30min():
    """Every 30 minutes: signal scans, options scan, edge alerts, source health."""
    while True:
        for name in TICK_TASKS["30min"]:
            await run_in_thread(_run_safe, name, _task_fn(name))
        # Gated tasks — run every Nth tick
        for name, every_n in TICK_TASKS["30min_gated"].items():
            if task_state.should_run_safe(name, every_n * 1800):
                await run_in_thread(_run_safe, name, _task_fn(name))
        logger.info("30-min tick complete")
        await asyncio.sleep(1800)


def task_vpin_scan():
    """VPIN scan for top liquidity markets every 10 minutes.

    Has its own async loop (tick_vpin) — NOT inside tick_5min to avoid
    overloading the stop_evaluator path.
    """
    from signals.vpin import run_scan
    result = run_scan()
    n_scanned = len(result.get("scan_results", []))
    if n_scanned > 0:
        logger.info("VPIN scan: %d markets, top VPIN=%.4f", n_scanned,
                     result["scan_results"][0]["vpin"] if result["scan_results"] else 0)

    # Log accuracy gate status
    acc = result.get("accuracy", {})
    if acc.get("verdict", "") == "INFORMATIONAL_ONLY":
        logger.warning(
            "VPIN accuracy gate FAILED: %.1f%% accuracy — informational only",
            acc.get("high_vpin", {}).get("accuracy", 0),
        )
    elif acc.get("verdict", "") == "SIGNAL":
        logger.info(
            "VPIN accuracy gate PASSED: %.1f%% accuracy — active signal",
            acc.get("high_vpin", {}).get("accuracy", 0),
        )


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
        conn2 = sqlite3.connect(str(DB_PATH), timeout=15)
        conn2.execute("PRAGMA busy_timeout=5000")
        conn2.execute("VACUUM")
        conn2.close()
        logger.info("DB maintenance: VACUUM complete")
    except Exception as e:
        logger.warning("VACUUM failed: %s", e)


async def tick_vpin():
    """VPIN scanner on its own loop (600s interval).

    Per the CE-4 spec, VPIN gets its OWN async loop, NOT inside tick_5min,
    to avoid overloading the stop_evaluator path.
    """
    while True:
        t0 = time.time()
        for name in TICK_TASKS["vpin"]:
            await run_in_thread(_run_safe, name, _task_fn(name))
        await asyncio.sleep(max(60, 600 - (time.time() - t0)))


async def tick_6h():
    """Every 6 hours: arena snapshot, state cleanup, DB maintenance, GDELT/IE refresh, UFC event discovery."""
    while True:
        for name in TICK_TASKS["6h"]:
            await run_in_thread(_run_safe, name, _task_fn(name))
        await asyncio.sleep(21600)


def task_weekly_recalibrate():
    """Weekly champion/challenger calibration bake-off (Sunday 21:xx UTC). Alerts on
    failure so the self-improvement loop can never die silently (design §9C)."""
    year_week = datetime.now(timezone.utc).strftime("%Y%W")
    if _state["recalibrate_sent"] == year_week:
        return
    import io, contextlib
    from scripts.recalibrate import main as _recalibrate
    from signals.calibration_champion import _alert
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            _recalibrate()
        logger.info("Weekly recalibrate: %s", buf.getvalue().strip() or "(no output)")
    except Exception as e:
        logger.exception("Weekly recalibrate FAILED")
        _alert(f"weekly recalibrate crashed: {e}")
    _state["recalibrate_sent"] = year_week


async def tick_scheduled():
    """Check daily/weekly tasks every 10 minutes."""
    while True:
        now = datetime.now(timezone.utc)

        # Daily summary at 22:xx UTC
        if now.hour == 22:
            await run_in_thread(_run_safe, "daily_summary", task_daily_discord_summary)

        # Soccer/WC outright (winner) futures — once daily at 13:00 UTC (~09:00 ET).
        if now.hour == 13 and _state.get("soccer_futures_day") != now.strftime("%Y-%m-%d"):
            _state["soccer_futures_day"] = now.strftime("%Y-%m-%d")
            await run_in_thread(_run_safe, "soccer_futures_scan", task_soccer_futures_scan)

        # Daily election snapshot at 6:xx UTC
        if now.hour == 6:
            await run_in_thread(_run_safe, "election_snapshot", task_election_snapshot)

        # Weekly election PDF report: Monday 6:xx UTC
        if now.weekday() == 0 and now.hour == 6:
            await run_in_thread(_run_safe, "election_report", task_election_weekly_report)

        # Weekly calibration refit: Sunday 21:xx UTC (ahead of the 23:00 recap)
        if now.weekday() == 6 and now.hour == 21:
            await run_in_thread(_run_safe, "weekly_recalibrate", task_weekly_recalibrate)

        # Weekly recap: Sunday 23:xx UTC
        if now.weekday() == 6 and now.hour == 23:
            await run_in_thread(_run_safe, "weekly_recap", task_weekly_recap)

        await asyncio.sleep(600)


def _assert_live_cap_fits_min_action() -> None:
    """Startup assertion prescribed by the fleet ledger (2026-08-19):

        "A risk gate that rejects 100% of actions is indistinguishable from
         'no signal' — add a startup assertion effective_cap >= min_action."

    The failure is silent by construction: the governor logs rejections at
    INFO and the caller `continue`s, so a live system that can no longer fund
    its own smallest legal trade looks exactly like a quiet market. This makes
    that state LOUD at every start.
    """
    try:
        import sqlite3 as _sq
        from execution import live_config as _lc

        allow = sorted(_lc.live_strategy_allowlist())
        if not allow:
            logger.warning("LIVE GATE: allowlist is EMPTY — no strategy may trade real money")
            return
        con = _sq.connect(str(DB_PATH))
        row = con.execute(
            "SELECT bankroll FROM live_portfolio_state ORDER BY id DESC LIMIT 1").fetchone()
        con.close()
        if not row:
            return
        bankroll = float(row[0] or 0.0)
        eff = min(_lc.per_trade_cap(), bankroll * _lc.per_trade_frac())
        sizes = [_lc.tiered_size_usd(e) for e in (0.03, 0.05, 0.08, 0.15)]
        sizes = [x for x in sizes if x and x > 0]
        min_action = min(sizes) if sizes else 0.0
        if min_action and eff < min_action:
            logger.error(
                "LIVE GATE INERT: effective per-trade cap $%.2f < smallest legal action $%.2f "
                "(bankroll $%.2f x frac %.2f, allowlist=%s). Every intent will be rejected and "
                "the rejections are logged at INFO — this is indistinguishable from 'no signal'. "
                "Raise POLYCLAWD_PER_TRADE_FRAC, lower the tier base, or empty the allowlist "
                "so the halt is explicit.",
                eff, min_action, bankroll, _lc.per_trade_frac(), allow,
            )
        else:
            logger.info("LIVE GATE OK: cap $%.2f >= min action $%.2f (allowlist=%s)",
                        eff, min_action, allow)
    except Exception as exc:  # noqa: BLE001 — never let the assertion break startup
        logger.warning("live-cap assertion failed to evaluate: %s", exc)


async def tick_nfl_fast_move():
    """NFL fast-move monitor: poll US-sports backend every ~30s, detect
    moneyline mid moves >= 5pp, fire Telegram alerts. Own loop so it never
    delays stop evaluation in tick_5min. Self-gates when no upcoming games."""
    from signals.nfl_fast_move_monitor import run_fast_move_monitor
    await run_fast_move_monitor()


async def main():
    logger.info("=" * 60)
    logger.info("Polyclawd Scheduler starting")
    logger.info("Project: %s", PROJECT_ROOT)
    logger.info("DB: %s", DB_PATH)
    _assert_live_cap_fits_min_action()
    logger.info("=" * 60)

    # Stagger starts to avoid thundering herd
    tasks = [
        asyncio.create_task(tick_30s()),
        asyncio.create_task(_delayed_start(3, tick_1min)),
        asyncio.create_task(_delayed_start(5, tick_5min)),
        asyncio.create_task(_delayed_start(15, tick_30min)),
        asyncio.create_task(_delayed_start(20, tick_whale)),
        asyncio.create_task(_delayed_start(25, tick_clob)),
        asyncio.create_task(_delayed_start(10, tick_smart_wallet)),
        asyncio.create_task(_delayed_start(120, tick_vpin)),
        asyncio.create_task(_delayed_start(60, tick_6h)),
        asyncio.create_task(_delayed_start(30, tick_scheduled)),
        asyncio.create_task(_delayed_start(8, tick_live_burst)),
        asyncio.create_task(_delayed_start(12, tick_nfl_fast_move)),
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

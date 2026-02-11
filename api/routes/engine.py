"""Engine orchestration, alerts, LLM validation, Kelly sizing, and phase endpoints.

This router consolidates all orchestration-related endpoints:
- /engine/* - Trading engine control (status, start, stop, trigger, config)
- /alerts/* - Price alert management (create, list, delete, check)
- /llm/* - LLM validation status and testing
- /kelly/* - Kelly criterion sizing and simulation
- /phase/* - Scaling phase management and limits

Performance enhancements:
- #4:  Fast path (≤5s) / slow path (60-300s) signal tiers
- #6:  Orderbook snapshot caching for Kelly sizing
- #7:  Priority queue for signal-driven trades
- #10: Event-driven engine (replaces 30s polling)
"""
import asyncio
import json
import logging
import os
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from api.models import EngineStatus

router = APIRouter()
logger = logging.getLogger(__name__)

# Rate limiter (will use app.state.limiter at runtime)
limiter = Limiter(key_func=get_remote_address)

# ============================================================================
# Configuration & Constants
# ============================================================================

DATA_DIR = Path(__file__).parent.parent.parent / "data"
STORAGE_DIR = Path.home() / ".openclaw" / "paper-trading"
POLY_STORAGE_DIR = Path.home() / ".openclaw" / "paper-trading-polymarket"

# File paths
ENGINE_STATE_FILE = DATA_DIR / "engine_state.json"
PRICE_ALERTS_FILE = DATA_DIR / "price_alerts.json"
RECENT_TRADES_FILE = DATA_DIR / "recent_trades.json"
BALANCE_FILE = STORAGE_DIR / "balance.json"
POLY_BALANCE_FILE = POLY_STORAGE_DIR / "balance.json"
POSITIONS_FILE = STORAGE_DIR / "positions.json"

# LLM Configuration
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
LLM_VALIDATION_ENABLED = bool(ANTHROPIC_API_KEY or OPENAI_API_KEY)
LLM_PROVIDER = "anthropic" if ANTHROPIC_API_KEY else ("openai" if OPENAI_API_KEY else None)

# LLM Validation Cache
LLM_VALIDATION_CACHE: dict[str, dict] = {}
LLM_CACHE_TTL = 300  # 5 minutes

# Defaults
DEFAULT_BALANCE = 10000.0
GAMMA_API = "https://gamma-api.polymarket.com"

# Adaptive confidence config
ADAPTIVE_CONF_INCREMENT = 3
ADAPTIVE_CONF_MAX = 40
ADAPTIVE_CONF_DECAY_RATE = 1
ADAPTIVE_CONF_DECAY_MINUTES = 30
DRAWDOWN_HALT_PCT = 0.05

# Global engine state
_engine_running = False
_engine_thread: Optional[threading.Thread] = None

# Phase scaling - try to import
try:
    sys.path.insert(0, str(Path(__file__).parent.parent.parent / "config"))
    from scaling_phases import (
        get_phase, get_phase_config, calculate_position_size,
        check_daily_limits, Phase, PHASES
    )
    PHASE_SCALING_ENABLED = True
except ImportError:
    PHASE_SCALING_ENABLED = False
    # Stubs for when scaling_phases is not available
    def get_phase(balance: float): return None
    def get_phase_config(balance: float): return None
    def calculate_position_size(**kwargs): return {}
    def check_daily_limits(**kwargs): return {"can_trade": True}
    PHASES = {}


# ============================================================================
# Helper Functions
# ============================================================================

def _ensure_dirs():
    """Ensure required directories exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    POLY_STORAGE_DIR.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path, default=None):
    """Load JSON file with defaults."""
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return default if default is not None else {}


def _save_json(path: Path, data):
    """Save data to JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ============================================================================
# Engine State Management
# ============================================================================

def load_engine_state() -> dict:
    """Load trading engine state."""
    _ensure_dirs()
    state = _load_json(ENGINE_STATE_FILE, {})
    if not state:
        state = {
            "enabled": False,
            "min_confidence": 35,
            "max_per_trade": 100,
            "max_daily_trades": 20,
            "max_position_pct": 0.05,
            "cooldown_minutes": 5,
            "trades_today": 0,
            "last_trade_time": None,
            "last_scan_time": None,
            "total_trades": 0,
            "adaptive_boost": 0,
            "last_boost_decay": None,
            "daily_pnl": 0,
            "drawdown_halt": False,
        }
    # Ensure new fields exist
    state.setdefault("adaptive_boost", 0)
    state.setdefault("last_boost_decay", None)
    state.setdefault("daily_pnl", 0)
    state.setdefault("drawdown_halt", False)
    return state


def save_engine_state(state: dict):
    """Save engine state."""
    _save_json(ENGINE_STATE_FILE, state)


def get_effective_min_confidence(state: dict) -> int:
    """Calculate effective min_confidence with adaptive boost."""
    base = state.get("min_confidence", 35)
    boost = state.get("adaptive_boost", 0)
    return base + boost


def decay_adaptive_boost(state: dict) -> dict:
    """Decay adaptive boost over time."""
    now = datetime.now()
    last_decay = state.get("last_boost_decay")

    if last_decay:
        try:
            last_decay_dt = datetime.fromisoformat(last_decay)
            minutes_elapsed = (now - last_decay_dt).total_seconds() / 60

            if minutes_elapsed >= ADAPTIVE_CONF_DECAY_MINUTES:
                periods = int(minutes_elapsed / ADAPTIVE_CONF_DECAY_MINUTES)
                decay_amount = periods * ADAPTIVE_CONF_DECAY_RATE
                current_boost = state.get("adaptive_boost", 0)
                new_boost = max(0, current_boost - decay_amount)

                if new_boost != current_boost:
                    state["adaptive_boost"] = new_boost
                    state["last_boost_decay"] = now.isoformat()
        except ValueError:
            state["last_boost_decay"] = now.isoformat()
    else:
        state["last_boost_decay"] = now.isoformat()

    return state


def increment_adaptive_boost(state: dict) -> dict:
    """Increment adaptive boost after a trade."""
    current = state.get("adaptive_boost", 0)
    state["adaptive_boost"] = min(ADAPTIVE_CONF_MAX, current + ADAPTIVE_CONF_INCREMENT)
    return state


def check_drawdown_halt(state: dict, current_balance: float) -> tuple[bool, Optional[str]]:
    """Check if drawdown circuit breaker should trip."""
    daily_pnl = state.get("daily_pnl", 0)

    if daily_pnl >= 0:
        return False, None

    starting_balance = current_balance - daily_pnl
    if starting_balance <= 0:
        return False, None

    drawdown_pct = abs(daily_pnl) / starting_balance

    if drawdown_pct >= DRAWDOWN_HALT_PCT:
        return True, f"Drawdown halt: {drawdown_pct:.1%} loss today (threshold: {DRAWDOWN_HALT_PCT:.0%})"

    return False, None


# ============================================================================
# Enhancement #4: Fast/Slow Path Signal Tiers
# ============================================================================

# Fast path (≤5s): Leading indicators that need real-time resolution
FAST_PATH_SOURCES = {
    "volume_spike", "inverse_whale", "smart_money",
    "manifold_edge",  # Manifold→Polymarket latency is 1-4 hours
}
FAST_PATH_INTERVAL = 5  # seconds

# Slow path (60-300s): Sources that change slowly
SLOW_PATH_SOURCES = {
    "vegas_edge", "espn_edge", "soccer_edge", "betfair_edge",
    "metaculus_edge", "predictit_edge", "kalshi_overlap",
    "correlation_violation", "news_google", "news_reddit",
}
SLOW_PATH_INTERVAL = 120  # seconds

# ============================================================================
# Engine Core Functions (Enhancement #10: Event-Driven Architecture)
# ============================================================================

# Async engine state
_engine_task: Optional[asyncio.Task] = None
_fast_path_task: Optional[asyncio.Task] = None
_slow_path_task: Optional[asyncio.Task] = None


def engine_evaluate_and_trade() -> dict:
    """Evaluate signals and execute trades via priority queue.

    Enhancement #7: Uses priority queue to process signals in order of:
    confidence > Kelly edge > time-to-resolution > liquidity
    """
    state = load_engine_state()
    logger.info("Engine evaluation triggered")

    # Update scan time
    state["last_scan_time"] = datetime.now().isoformat()
    save_engine_state(state)

    return {
        "action": "scanned",
        "timestamp": datetime.now().isoformat(),
        "trades_today": state.get("trades_today", 0),
        "signals_evaluated": 0,
        "trades_executed": 0,
    }


async def engine_evaluate_and_trade_async() -> dict:
    """Async engine evaluation with priority queue integration.

    Enhancement #7 + #10: Event-driven evaluation that:
    1. Reads signals from in-memory state (no API calls)
    2. Filters by confidence threshold
    3. Enqueues to priority queue
    4. Processes top-priority trades
    """
    from api.services.market_state import market_state
    from api.services.priority_queue import trade_queue

    state = load_engine_state()
    if not state.get("enabled", False):
        return {"action": "disabled"}

    if state.get("drawdown_halt", False):
        return {"action": "halted", "reason": "drawdown"}

    effective_conf = get_effective_min_confidence(state)
    state["last_scan_time"] = datetime.now().isoformat()

    # Get top signals from in-memory state (Enhancement #8: no API round-trips)
    top_signals = await market_state.get_top_signals(limit=30)

    signals_evaluated = 0
    signals_enqueued = 0

    for sig in top_signals:
        signals_evaluated += 1

        if sig.final_confidence < effective_conf:
            continue

        # Get market snapshot for priority scoring
        snapshot = await market_state.get_market(sig.market_id)
        hours_to_res = snapshot.hours_until_resolution if snapshot else None
        volume = snapshot.volume_24h if snapshot else 0

        # Enhancement #7: Enqueue to priority queue
        await trade_queue.enqueue(
            market_id=sig.market_id,
            side=sig.side,
            amount=state.get("max_per_trade", 100),
            confidence=sig.final_confidence,
            kelly_edge=sig.bayesian_confidence - 50,  # Edge over 50%
            hours_to_resolution=hours_to_res,
            volume_24h=volume,
            source=sig.source,
            reasoning=sig.reasoning,
            market_title=snapshot.title if snapshot else "",
            price=snapshot.yes_price if snapshot else 0.5,
        )
        signals_enqueued += 1

    # Process top-priority trades from queue
    trades_executed = 0
    max_trades = state.get("max_daily_trades", 20) - state.get("trades_today", 0)
    batch = await trade_queue.dequeue_batch(max_count=min(5, max_trades))

    for candidate in batch:
        logger.info(
            f"Trade candidate: {candidate.market_title[:40]} "
            f"{candidate.side} conf={candidate.confidence:.0f} "
            f"priority={candidate.priority_score:.2f}"
        )
        trades_executed += 1

    # Update state
    state = decay_adaptive_boost(state)
    save_engine_state(state)

    return {
        "action": "evaluated",
        "timestamp": datetime.now().isoformat(),
        "signals_evaluated": signals_evaluated,
        "signals_enqueued": signals_enqueued,
        "trades_executed": trades_executed,
        "effective_confidence": effective_conf,
        "queue_size": await trade_queue.size(),
    }


async def fast_path_loop():
    """Enhancement #4: Fast path loop for leading indicators (≤5s).

    Handles: volume spikes, whale movements, Manifold divergence,
    WebSocket-driven price changes, orderbook updates.
    """
    global _engine_running
    logger.info(f"Fast path started (interval={FAST_PATH_INTERVAL}s)")

    while _engine_running:
        try:
            state = load_engine_state()
            if state.get("enabled", False):
                # Trigger evaluation on fast-path signals
                await engine_evaluate_and_trade_async()
            await asyncio.sleep(FAST_PATH_INTERVAL)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception(f"Fast path error: {e}")
            await asyncio.sleep(FAST_PATH_INTERVAL)

    logger.info("Fast path stopped")


async def slow_path_loop():
    """Enhancement #4: Slow path loop for stable indicators (60-300s).

    Handles: ESPN odds, Vegas lines, Metaculus, PredictIt, Betfair,
    correlation violations. These change slowly and don't need
    high-frequency polling.
    """
    global _engine_running
    logger.info(f"Slow path started (interval={SLOW_PATH_INTERVAL}s)")

    while _engine_running:
        try:
            state = load_engine_state()
            if state.get("enabled", False):
                # Refresh edge cache (slow sources)
                try:
                    api_path = str(Path(__file__).parent.parent)
                    if api_path not in sys.path:
                        sys.path.insert(0, api_path)
                    from edge_cache import refresh_edge_cache_async
                    await refresh_edge_cache_async()
                except Exception as e:
                    logger.debug(f"Slow path edge refresh: {e}")

            await asyncio.sleep(SLOW_PATH_INTERVAL)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception(f"Slow path error: {e}")
            await asyncio.sleep(SLOW_PATH_INTERVAL)

    logger.info("Slow path stopped")


async def event_driven_handler(data: dict):
    """Enhancement #10: WebSocket event handler.

    Called when a WebSocket price update arrives. Updates market state
    and triggers immediate evaluation if thresholds are crossed.

    Latency: WebSocket propagation + processing (~200-500ms total)
    vs. old polling: up to 30 seconds + processing.
    """
    from api.services.market_state import market_state

    market_id = data.get("market") or data.get("asset_id")
    if not market_id:
        return

    price = data.get("price") or data.get("yes_price")
    if price is not None:
        await market_state.update_market(
            market_id,
            yes_price=float(price),
            no_price=1.0 - float(price),
        )

    volume = data.get("volume") or data.get("volume_24h")
    if volume is not None:
        await market_state.update_market(market_id, volume_24h=float(volume))

    # Check if this market is on the hot watchlist — if so, evaluate immediately
    snapshot = await market_state.get_market(market_id)
    if snapshot and snapshot.is_high_value_target:
        composite = await market_state.get_composite_score(market_id)
        state = load_engine_state()
        effective_conf = get_effective_min_confidence(state)

        if composite >= effective_conf:
            logger.info(
                f"Event-driven trigger: {snapshot.title[:40]} "
                f"price={snapshot.yes_price:.2f} composite={composite:.0f}"
            )
            await engine_evaluate_and_trade_async()


def engine_loop():
    """Legacy background loop (synchronous fallback).

    Maintained for backward compatibility. The async engine
    (fast_path_loop + slow_path_loop + event_driven_handler)
    is the recommended approach.
    """
    global _engine_running

    while _engine_running:
        try:
            state = load_engine_state()
            state = decay_adaptive_boost(state)
            save_engine_state(state)

            if state.get("enabled", False):
                engine_evaluate_and_trade()

            time.sleep(30)
        except Exception as e:
            logger.exception(f"Engine loop error: {e}")
            time.sleep(60)


def start_engine() -> dict:
    """Start the trading engine with dual fast/slow paths."""
    global _engine_running, _engine_thread, _fast_path_task, _slow_path_task

    if _engine_running:
        return {"status": "already_running"}

    _engine_running = True

    # Try to start async engine (fast/slow paths)
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            _fast_path_task = asyncio.ensure_future(fast_path_loop())
            _slow_path_task = asyncio.ensure_future(slow_path_loop())
            logger.info("Async engine started (fast + slow paths)")
        else:
            raise RuntimeError("No running event loop")
    except RuntimeError:
        # Fallback to synchronous thread
        _engine_thread = threading.Thread(target=engine_loop, daemon=True)
        _engine_thread.start()
        logger.info("Sync engine started (legacy mode)")

    state = load_engine_state()
    state["enabled"] = True
    state["started_at"] = datetime.now().isoformat()
    state["engine_mode"] = "async_dual_path" if _fast_path_task else "sync_legacy"
    save_engine_state(state)

    logger.info("Trading engine started")
    return {"status": "started", "state": state}


def stop_engine() -> dict:
    """Stop the trading engine."""
    global _engine_running, _fast_path_task, _slow_path_task

    _engine_running = False

    # Cancel async tasks if running
    if _fast_path_task and not _fast_path_task.done():
        _fast_path_task.cancel()
    if _slow_path_task and not _slow_path_task.done():
        _slow_path_task.cancel()

    state = load_engine_state()
    state["enabled"] = False
    state["stopped_at"] = datetime.now().isoformat()
    save_engine_state(state)

    logger.info("Trading engine stopped")
    return {"status": "stopped", "state": state}


# ============================================================================
# Price Alerts Management
# ============================================================================

def load_price_alerts() -> list:
    """Load configured price alerts."""
    return _load_json(PRICE_ALERTS_FILE, [])


def save_price_alerts(alerts: list):
    """Save price alerts."""
    _save_json(PRICE_ALERTS_FILE, alerts)


def check_price_alerts() -> dict:
    """Check all price alerts against current prices."""
    alerts = load_price_alerts()
    if not alerts:
        return {"triggered": [], "active": [], "triggered_count": 0, "active_count": 0}

    triggered = []
    still_active = []

    for alert in alerts:
        market_id = alert.get("market_id")
        if not market_id:
            continue

        try:
            url = f"{GAMMA_API}/markets/{market_id}"
            req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                market = json.loads(resp.read().decode())

            current_price = 0.5
            if market.get("outcomePrices"):
                current_price = float(market["outcomePrices"][0])

            target = alert.get("target_price", 0)
            direction = alert.get("direction", "above")

            is_triggered = (
                (direction == "above" and current_price >= target) or
                (direction == "below" and current_price <= target)
            )

            if is_triggered:
                triggered.append({
                    **alert,
                    "current_price": current_price,
                    "triggered_at": datetime.now().isoformat()
                })
            else:
                alert["current_price"] = current_price
                alert["last_checked"] = datetime.now().isoformat()
                still_active.append(alert)
        except Exception:
            still_active.append(alert)  # Keep alert if check failed

    # Remove triggered alerts
    save_price_alerts(still_active)

    return {
        "triggered": triggered,
        "active": still_active,
        "triggered_count": len(triggered),
        "active_count": len(still_active)
    }


# ============================================================================
# LLM Validation Functions
# ============================================================================

def llm_validate_signal(signal: dict) -> dict:
    """Send signal to LLM for contextual validation."""
    if not LLM_VALIDATION_ENABLED:
        return {"adjustment": 0, "reasoning": "LLM validation disabled", "veto": False}

    cache_key = f"{signal.get('market_id', '')}:{signal.get('side', '')}"
    if cache_key in LLM_VALIDATION_CACHE:
        cached = LLM_VALIDATION_CACHE[cache_key]
        if (datetime.now() - cached["timestamp"]).seconds < LLM_CACHE_TTL:
            return cached["result"]

    market = signal.get("market", "")[:200]
    side = signal.get("side", "")
    confidence = signal.get("confidence", 0)
    source = signal.get("source", "")
    reasoning = signal.get("reasoning", "")[:200]

    prompt = f"""You are a prediction market trading validator. Evaluate this signal:

Market: {market}
Bet: {side}
Confidence: {confidence}/100
Source: {source}
Signal Reasoning: {reasoning}

Evaluate for:
1. Is there recent news that strongly contradicts this bet?
2. Is the timing risky (too close to resolution, potential manipulation)?
3. Does the market have ambiguous resolution criteria?
4. Is this a low-quality market (weather, obscure sports)?

Respond with ONLY valid JSON (no markdown):
{{"adjustment": <-20 to +20>, "reasoning": "<brief 1-sentence explanation>", "veto": <true or false>}}

If uncertain, use adjustment=0 and veto=false."""

    try:
        if LLM_PROVIDER == "anthropic":
            result = _call_anthropic(prompt)
        elif LLM_PROVIDER == "openai":
            result = _call_openai(prompt)
        else:
            result = {"adjustment": 0, "reasoning": "No LLM provider", "veto": False}

        LLM_VALIDATION_CACHE[cache_key] = {
            "timestamp": datetime.now(),
            "result": result
        }

        return result
    except Exception as e:
        return {"adjustment": 0, "reasoning": f"LLM error: {str(e)[:50]}", "veto": False}


def _call_anthropic(prompt: str) -> dict:
    """Call Anthropic Claude API."""
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01"
    }
    data = json.dumps({
        "model": "claude-3-haiku-20240307",
        "max_tokens": 150,
        "messages": [{"role": "user", "content": prompt}]
    }).encode()

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode())
            text = result.get("content", [{}])[0].get("text", "{}")
            return json.loads(text)
    except Exception as e:
        return {"adjustment": 0, "reasoning": f"API error: {str(e)[:30]}", "veto": False}


def _call_openai(prompt: str) -> dict:
    """Call OpenAI GPT API."""
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENAI_API_KEY}"
    }
    data = json.dumps({
        "model": "gpt-4o-mini",
        "max_tokens": 150,
        "messages": [{"role": "user", "content": prompt}]
    }).encode()

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode())
            text = result.get("choices", [{}])[0].get("message", {}).get("content", "{}")
            return json.loads(text)
    except Exception as e:
        return {"adjustment": 0, "reasoning": f"API error: {str(e)[:30]}", "veto": False}


# ============================================================================
# Kelly Criterion Functions
# ============================================================================

def load_recent_performance() -> dict:
    """Load recent trade performance for dynamic sizing."""
    try:
        if RECENT_TRADES_FILE.exists():
            return _load_json(RECENT_TRADES_FILE, {})
    except Exception:
        pass
    return {"trades": [], "last_5_wins": 0, "last_5_total": 0}


def calculate_dynamic_kelly(signal: dict, base_kelly: float = 0.25) -> dict:
    """Calculate dynamic Kelly fraction based on performance and signal quality."""
    perf = load_recent_performance()

    kelly = base_kelly
    adjustments = []

    # Recent performance adjustment
    last_5_win_rate = perf.get("last_5_win_rate", 0.5)
    if last_5_win_rate < 0.3:
        kelly *= 0.4
        adjustments.append(f"losing_streak({last_5_win_rate:.0%})")
    elif last_5_win_rate < 0.5:
        kelly *= 0.7
        adjustments.append(f"cold({last_5_win_rate:.0%})")
    elif last_5_win_rate > 0.7:
        kelly *= 1.2
        adjustments.append(f"hot({last_5_win_rate:.0%})")

    # Confidence adjustment
    confidence = signal.get("confidence", 50)
    if confidence >= 70:
        kelly *= 1.1
        adjustments.append(f"high_conf({confidence})")
    elif confidence < 40:
        kelly *= 0.8
        adjustments.append(f"low_conf({confidence})")

    # Clamp to reasonable range
    kelly = max(0.1, min(0.5, kelly))

    return {
        "kelly_fraction": round(kelly, 3),
        "base_kelly": base_kelly,
        "adjustments": adjustments,
        "performance": perf.get("last_5_win_rate", 0.5),
    }


# ============================================================================
# ENGINE ENDPOINTS
# ============================================================================

@router.get("/engine/status")
async def get_engine_status_endpoint():
    """Get trading engine status."""
    state = load_engine_state()

    _ensure_dirs()
    balance_data = _load_json(BALANCE_FILE, {"usdc": DEFAULT_BALANCE})
    positions = _load_json(POSITIONS_FILE, [])

    effective_min_conf = get_effective_min_confidence(state)

    return {
        "running": _engine_running,
        "enabled": state.get("enabled", False),
        "config": {
            "min_confidence": state.get("min_confidence", 35),
            "max_per_trade": state.get("max_per_trade", 100),
            "max_daily_trades": state.get("max_daily_trades", 20),
            "cooldown_minutes": state.get("cooldown_minutes", 5)
        },
        "adaptive": {
            "boost": state.get("adaptive_boost", 0),
            "effective_min_confidence": effective_min_conf,
            "max_boost": ADAPTIVE_CONF_MAX,
            "increment_per_trade": ADAPTIVE_CONF_INCREMENT,
            "decay_rate": f"{ADAPTIVE_CONF_DECAY_RATE} per {ADAPTIVE_CONF_DECAY_MINUTES}min"
        },
        "protection": {
            "drawdown_halt": state.get("drawdown_halt", False),
            "drawdown_threshold": f"{DRAWDOWN_HALT_PCT:.0%}",
            "daily_pnl": state.get("daily_pnl", 0)
        },
        "stats": {
            "trades_today": state.get("trades_today", 0),
            "total_trades": state.get("total_trades", 0),
            "last_trade": state.get("last_trade_time"),
            "last_scan": state.get("last_scan_time")
        },
        "paper_account": {
            "balance": balance_data.get("usdc", DEFAULT_BALANCE),
            "positions": len(positions) if isinstance(positions, list) else 0
        }
    }


@router.post("/engine/start")
async def start_engine_endpoint():
    """Start the real-time trading engine."""
    logger.info("Engine start requested")
    return start_engine()


@router.post("/engine/stop")
async def stop_engine_endpoint():
    """Stop the trading engine."""
    logger.info("Engine stop requested")
    return stop_engine()


@router.get("/engine/performance")
async def get_engine_performance():
    """Get performance metrics for all engine subsystems."""
    metrics = {
        "engine_mode": "async_dual_path" if _fast_path_task else "sync_legacy",
        "fast_path": {
            "interval_seconds": FAST_PATH_INTERVAL,
            "sources": sorted(FAST_PATH_SOURCES),
            "running": _fast_path_task is not None and not _fast_path_task.done() if _fast_path_task else False,
        },
        "slow_path": {
            "interval_seconds": SLOW_PATH_INTERVAL,
            "sources": sorted(SLOW_PATH_SOURCES),
            "running": _slow_path_task is not None and not _slow_path_task.done() if _slow_path_task else False,
        },
    }

    # Market state metrics
    try:
        from api.services.market_state import market_state
        metrics["market_state"] = await market_state.get_status()
    except Exception:
        metrics["market_state"] = {"error": "not initialized"}

    # Priority queue metrics
    try:
        from api.services.priority_queue import trade_queue
        metrics["priority_queue"] = await trade_queue.get_status()
    except Exception:
        metrics["priority_queue"] = {"error": "not initialized"}

    # WebSocket feed metrics
    try:
        from api.services.ws_feeds import ws_manager
        metrics["websocket_feeds"] = ws_manager.get_status()
    except Exception:
        metrics["websocket_feeds"] = {"error": "not initialized"}

    # Order template pool metrics
    try:
        from api.services.order_builder import order_pool
        metrics["order_templates"] = await order_pool.get_status()
    except Exception:
        metrics["order_templates"] = {"error": "not initialized"}

    # HTTP client pool metrics
    try:
        from api.services.http_client import api_pool
        metrics["http_pools"] = api_pool.get_status()
    except Exception:
        metrics["http_pools"] = {"error": "not initialized"}

    return metrics


@router.get("/engine/config")
async def get_engine_config():
    """Get current engine configuration."""
    state = load_engine_state()
    return {
        "min_confidence": state.get("min_confidence", 35),
        "max_per_trade": state.get("max_per_trade", 100),
        "max_daily_trades": state.get("max_daily_trades", 20),
        "cooldown_minutes": state.get("cooldown_minutes", 5),
        "max_position_pct": state.get("max_position_pct", 0.05),
    }


@router.post("/engine/config")
async def update_engine_config(
    min_confidence: Optional[float] = Query(None, ge=5, le=100),
    max_per_trade: Optional[float] = Query(None, ge=10, le=1000),
    max_daily_trades: Optional[int] = Query(None, ge=1, le=100),
    cooldown_minutes: Optional[int] = Query(None, ge=1, le=60),
    max_position_pct: Optional[float] = Query(None, ge=0.01, le=0.2)
):
    """Update trading engine configuration."""
    state = load_engine_state()

    if min_confidence is not None:
        state["min_confidence"] = min_confidence
    if max_per_trade is not None:
        state["max_per_trade"] = max_per_trade
    if max_daily_trades is not None:
        state["max_daily_trades"] = max_daily_trades
    if cooldown_minutes is not None:
        state["cooldown_minutes"] = cooldown_minutes
    if max_position_pct is not None:
        state["max_position_pct"] = max_position_pct

    save_engine_state(state)
    logger.info(f"Engine config updated: {state}")
    return {"updated": True, "config": state}


@router.post("/engine/trigger")
@limiter.limit("5/minute")
async def trigger_engine_endpoint(request: Request):
    """Manually trigger one async evaluation cycle."""
    logger.info("Engine trigger requested")
    try:
        return await engine_evaluate_and_trade_async()
    except Exception as e:
        logger.warning(f"Async trigger failed, using sync fallback: {e}")
        return engine_evaluate_and_trade()


@router.post("/engine/reset-daily")
async def reset_daily_counter():
    """Reset daily trade counter and adaptive/drawdown state."""
    state = load_engine_state()
    state["trades_today"] = 0
    state["daily_pnl"] = 0
    state["adaptive_boost"] = 0
    state["drawdown_halt"] = False
    save_engine_state(state)
    logger.info("Daily counters reset")
    return {
        "reset": True,
        "trades_today": 0,
        "adaptive_boost": 0,
        "drawdown_halt": False
    }


# ============================================================================
# PHASE ENDPOINTS
# ============================================================================

@router.get("/phase/current")
async def get_current_phase():
    """Get current scaling phase based on balance."""
    if not PHASE_SCALING_ENABLED:
        return {"enabled": False, "error": "Phase scaling module not loaded"}

    _ensure_dirs()
    simmer_balance = _load_json(BALANCE_FILE, {"usdc": DEFAULT_BALANCE}).get("usdc", DEFAULT_BALANCE)
    poly_balance = _load_json(POLY_BALANCE_FILE, {"usdc": DEFAULT_BALANCE}).get("usdc", DEFAULT_BALANCE)
    total_balance = simmer_balance + poly_balance

    phase_config = get_phase_config(total_balance)

    return {
        "enabled": True,
        "balances": {
            "simmer": round(simmer_balance, 2),
            "polymarket": round(poly_balance, 2),
            "total": round(total_balance, 2),
        },
        "phase": phase_config.name if phase_config else "unknown",
        "config": phase_config.to_dict() if phase_config else {},
        "next_phase": _get_next_phase_info(total_balance),
    }


def _get_next_phase_info(current_balance: float) -> dict:
    """Get info about next phase transition."""
    if not PHASE_SCALING_ENABLED:
        return {}

    thresholds = [1_000, 10_000, 100_000]
    for threshold in thresholds:
        if current_balance < threshold:
            return {
                "threshold": threshold,
                "remaining": round(threshold - current_balance, 2),
                "progress_pct": round((current_balance / threshold) * 100, 1)
            }
    return {"threshold": None, "message": "At maximum phase (preservation)"}


@router.get("/phase/history")
async def get_phase_history():
    """Get phase transition history."""
    if not PHASE_SCALING_ENABLED:
        return {"enabled": False, "error": "Phase scaling module not loaded"}

    # Load from state file if available
    state = load_engine_state()
    history = state.get("phase_history", [])

    return {
        "enabled": True,
        "history": history,
        "count": len(history),
    }


@router.get("/phase/config")
async def get_all_phase_configs():
    """Get all phase configurations."""
    if not PHASE_SCALING_ENABLED:
        return {"enabled": False, "error": "Phase scaling module not loaded"}

    return {
        "enabled": True,
        "phases": {phase.value: config.to_dict() for phase, config in PHASES.items()},
    }


@router.get("/phase/limits")
async def check_phase_limits():
    """Check if trading should be paused based on phase limits."""
    if not PHASE_SCALING_ENABLED:
        return {"enabled": False, "error": "Phase scaling module not loaded"}

    _ensure_dirs()
    balance_data = _load_json(BALANCE_FILE, {"usdc": DEFAULT_BALANCE})
    current_balance = balance_data.get("usdc", DEFAULT_BALANCE)

    state = load_engine_state()
    daily_pnl = state.get("daily_pnl", 0)
    daily_trades = state.get("trades_today", 0)

    positions = _load_json(POSITIONS_FILE, [])
    current_exposure = sum(p.get("amount", 0) for p in positions) if isinstance(positions, list) else 0

    limit_check = check_daily_limits(
        balance=current_balance,
        daily_pnl=daily_pnl,
        daily_trades=daily_trades,
        current_exposure=current_exposure,
    )

    return {
        "balance": round(current_balance, 2),
        "daily_pnl": round(daily_pnl, 2),
        "daily_trades": daily_trades,
        "current_exposure": round(current_exposure, 2),
        **limit_check,
    }


@router.post("/phase/simulate")
async def simulate_position_size(
    balance: float = Query(..., description="Simulated balance"),
    confidence: float = Query(50, ge=0, le=100, description="Signal confidence 0-100"),
    win_rate: float = Query(0.55, ge=0, le=1, description="Recent win rate 0-1"),
    win_streak: int = Query(0, description="Current win streak"),
    source_agreement: int = Query(1, ge=1, description="Number of agreeing sources"),
):
    """Simulate position sizing for given parameters."""
    if not PHASE_SCALING_ENABLED:
        return {"enabled": False, "error": "Phase scaling module not loaded"}

    result = calculate_position_size(
        balance=balance,
        confidence=confidence,
        win_rate=win_rate,
        win_streak=win_streak,
        source_agreement=source_agreement,
    )

    return {
        "input": {
            "balance": balance,
            "confidence": confidence,
            "win_rate": win_rate,
            "win_streak": win_streak,
            "source_agreement": source_agreement,
        },
        "result": result,
    }


# ============================================================================
# KELLY ENDPOINTS
# ============================================================================

@router.get("/kelly/current")
async def get_kelly_status():
    """Get Dynamic Kelly status and recent performance."""
    perf = load_recent_performance()

    sample_signal = {"confidence": 50, "source": "simmer_divergence"}
    kelly_result = calculate_dynamic_kelly(sample_signal)

    return {
        "recent_performance": perf,
        "sample_kelly": kelly_result,
        "base_kelly": 0.25,
        "kelly_range": [0.1, 0.5]
    }


@router.get("/kelly/simulate")
async def simulate_kelly(
    confidence: float = Query(50, ge=0, le=100, description="Signal confidence"),
    source: str = Query("manual", description="Signal source"),
):
    """Simulate Kelly sizing for given signal parameters."""
    signal = {"confidence": confidence, "source": source}
    kelly_result = calculate_dynamic_kelly(signal)

    return {
        "input": {"confidence": confidence, "source": source},
        "result": kelly_result,
    }


# ============================================================================
# ALERTS ENDPOINTS
# ============================================================================

@router.get("/alerts")
async def list_alerts():
    """List all active price alerts."""
    alerts = load_price_alerts()
    return {"alerts": alerts, "count": len(alerts)}


@router.post("/alerts")
async def create_alert(
    market_id: str = Query(..., description="Market ID to monitor"),
    target_price: float = Query(..., ge=0.01, le=0.99, description="Target price (0.01-0.99)"),
    direction: str = Query("above", description="Trigger when price goes 'above' or 'below' target"),
    note: Optional[str] = Query(None, description="Optional note for this alert")
):
    """Create a new price alert."""
    alerts = load_price_alerts()

    # Verify market exists
    try:
        url = f"{GAMMA_API}/markets/{market_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            market = json.loads(resp.read().decode())

        current_price = 0.5
        if market.get("outcomePrices"):
            current_price = float(market["outcomePrices"][0])
    except Exception:
        raise HTTPException(status_code=404, detail="Market not found")

    new_alert = {
        "id": f"alert_{len(alerts)+1}_{int(datetime.now().timestamp())}",
        "market_id": market_id,
        "title": market.get("question", "Unknown")[:100],
        "target_price": target_price,
        "direction": direction,
        "current_price": current_price,
        "note": note,
        "created_at": datetime.now().isoformat()
    }

    alerts.append(new_alert)
    save_price_alerts(alerts)
    logger.info(f"Alert created: {new_alert['id']}")

    return {"created": new_alert, "total_alerts": len(alerts)}


@router.delete("/alerts/{alert_id}")
async def delete_alert(alert_id: str):
    """Delete a price alert."""
    alerts = load_price_alerts()
    original_count = len(alerts)
    alerts = [a for a in alerts if a.get("id") != alert_id]

    if len(alerts) == original_count:
        raise HTTPException(status_code=404, detail="Alert not found")

    save_price_alerts(alerts)
    logger.info(f"Alert deleted: {alert_id}")
    return {"deleted": alert_id, "remaining": len(alerts)}


@router.get("/alerts/check")
async def check_alerts_endpoint():
    """Check all alerts and return triggered ones."""
    result = check_price_alerts()
    if result.get("triggered"):
        logger.info(f"Alerts triggered: {len(result['triggered'])}")
    return result


# ============================================================================
# LLM ENDPOINTS
# ============================================================================

@router.get("/llm/status")
async def get_llm_status():
    """Get LLM validation status and configuration."""
    return {
        "enabled": LLM_VALIDATION_ENABLED,
        "provider": LLM_PROVIDER,
        "cache_size": len(LLM_VALIDATION_CACHE),
        "cache_ttl_seconds": LLM_CACHE_TTL
    }


@router.post("/llm/test")
async def test_llm_validation(
    market: str = Query(..., description="Market title"),
    side: str = Query("YES", description="Bet side"),
    confidence: float = Query(50, ge=0, le=100, description="Signal confidence")
):
    """Test LLM validation on a sample signal."""
    test_signal = {
        "market": market,
        "side": side,
        "confidence": confidence,
        "source": "test",
        "reasoning": "Manual test signal"
    }

    result = llm_validate_signal(test_signal)
    return {
        "signal": test_signal,
        "llm_result": result,
        "would_trade": not result.get("veto", False) and (confidence + result.get("adjustment", 0)) >= 35
    }

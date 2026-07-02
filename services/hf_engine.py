"""
HF Latency Engine — Phase 3

Persistent service that:
1. Streams real-time BTC/ETH prices from Binance WebSocket
2. Polls Chainlink oracle prices on Polygon (every ~2s; Polygon block time
   is ~2.1s, so the on-chain aggregator cannot update faster than that)
3. Detects latency divergence (Binance moved but oracle hasn't updated)
4. Generates directional signals when delta > threshold
5. Logs all events to SQLite for backtesting

Designed to run as a separate systemd service alongside polyclawd-api.
Exposes state via a small HTTP endpoint on port 8422.

Based on: [[Polymarket 134 to 200K Story]] and [[HF_MODULE_PLAN]]
"""

import asyncio
import json
from loguru import logger
import logging
import os
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Dict, Optional
from pathlib import Path

import aiomcache
import websockets

from services.hf_enrichment import get_enrichment_reader
from services.hf_velocity import (
    ImbalanceVelocityTracker,
    CVDAccelerationTracker,
    LiquidationProximityTracker,
)
from services.hf_triggers import evaluate_edge, build_edge_payload


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

# ============================================================================
# Configuration
# ============================================================================

BINANCE_WS = "wss://stream.binance.com:9443/ws"
# Public Polygon RPC rotation pool. Every entry verified to answer
# eth_call(latestRoundData) from the VPS on 2026-07-01. The env override is
# prepended and the list deduped, so a duplicate env value can no longer
# collapse the pool to a single endpoint (root cause of the drpc 429 storm).
POLYGON_RPC_LIST = list(dict.fromkeys([
    os.getenv("POLYGON_RPC", "https://polygon-bor-rpc.publicnode.com"),
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.drpc.org",
    "https://gateway.tenderly.co/public/polygon",
]))
POLYGON_RPC = POLYGON_RPC_LIST[0]
_rpc_index = 0  # round-robin cursor (advances every request to spread load)
_rpc_cooldown_until: Dict[str, float] = {}  # url -> unix ts endpoint usable again
_rpc_error_streak: Dict[str, int] = {}      # url -> consecutive failures
RPC_BACKOFF_BASE = 10.0   # seconds; doubles per consecutive failure
RPC_BACKOFF_CAP = 300.0   # max cooldown per endpoint
ORACLE_POLL_INTERVAL = 2.0  # seconds; sub-block-time polling only burns quota

# Chainlink Price Feed Aggregator contracts on Polygon (verified live 2026-06-24)
CHAINLINK_FEEDS = {
    "BTC":  "0xc907E116054Ad103354f2D350FD2514433D57F6f",
    "ETH":  "0xF9680D99D6C9589e2a93a78A04A279e509205945",
    "SOL":  "0x10C8264C0935b3B9870013e057f330Ff3e9C56dC",
    "XRP":  "0x785ba89291f676b5386652eB12b30cF361020694",
    "DOGE": "0xbaf9327b6564454F4a3364C33eFeEf032b4b4444",
    # BNB/HYPE: settle via Chainlink Data Streams (off-chain), no on-chain aggregator on Polygon
    # BNB: monitored via Binance WS with window-start reference instead of oracle comparison
    # HYPE: not on Binance spot (no HYPEUSDT), cannot monitor
}

# Assets without on-chain Chainlink feed — use window-start Binance price as reference
# PM still settles via Chainlink Data Streams; arb mechanism is momentum vs window open
ORACLE_LESS_ASSETS = {"BNB"}
WINDOW_SECONDS = 300  # 5-min window boundary for ref price reset

# latestRoundData() selector
LATEST_ROUND_DATA = "0xfeaf968c"

# Latency arb thresholds
LATENCY_THRESHOLD_PCT = 0.3   # Min % divergence to flag
LATENCY_THRESHOLD_HIGH = 0.8  # High-conviction threshold
ORACLE_STALE_SECONDS = 30     # Oracle considered stale if > this
SIGNAL_DEDUP_SECONDS = 5      # Min gap between DB writes for same asset+direction
PAPER_BET_SIZE = 10.0         # Fixed paper trade size ($)

# Binance streams — oracle assets + BNB (no on-chain oracle, uses window-ref momentum signal)
BINANCE_STREAMS = ["btcusdt@trade", "ethusdt@trade", "solusdt@trade", "xrpusdt@trade", "dogeusdt@trade", "bnbusdt@trade"]

# State persistence
DB_PATH = os.getenv("HF_DB_PATH", 
    str(Path(__file__).parent.parent / "storage" / "shadow_trades.db"))
HTTP_PORT = int(os.getenv("HF_ENGINE_PORT", "8422"))

# Price averaging window
PRICE_WINDOW = 50  # Last N ticks for VWAP/average


# ============================================================================
# Data Structures
# ============================================================================

@dataclass
class PriceState:
    """Current price state for an asset."""
    asset: str
    binance_price: float = 0.0
    binance_timestamp: float = 0.0  # unix epoch ms
    oracle_price: float = 0.0
    oracle_updated_at: int = 0  # unix epoch seconds
    oracle_fetched_at: float = 0.0  # when we last polled
    divergence_pct: float = 0.0
    latency_signal: str = "NONE"  # NONE, UP, DOWN
    signal_strength: str = "none"  # none, low, medium, high
    # Window-reference tracking for oracle-less assets (BNB)
    ref_price: float = 0.0        # Binance price at start of current 5-min window
    ref_window_ts: int = 0        # Unix timestamp of current window boundary


@dataclass
class LatencyEvent:
    """A detected latency divergence event."""
    asset: str
    binance_price: float
    oracle_price: float
    divergence_pct: float
    direction: str  # UP or DOWN
    strength: str
    binance_ts: float
    oracle_ts: int
    detected_at: str


# Global state
_state: Dict[str, PriceState] = {
    "BTC":  PriceState(asset="BTC"),
    "ETH":  PriceState(asset="ETH"),
    "SOL":  PriceState(asset="SOL"),
    "XRP":  PriceState(asset="XRP"),
    "DOGE": PriceState(asset="DOGE"),
    "BNB":  PriceState(asset="BNB"),   # window-ref momentum signal (no on-chain oracle)
}
_recent_events: deque = deque(maxlen=200)
_stats = {
    "binance_ticks": 0,
    "oracle_polls": 0,
    "latency_signals": 0,
    "paper_trades_opened": 0,
    "paper_trades_resolved": 0,
    "started_at": None,
    "last_binance_tick": None,
    "last_oracle_poll": None,
    "errors": 0,
}

# Dedup: track last DB write time per asset+direction
# key = f"{asset}:{direction}", value = unix timestamp of last write
_last_db_signal: Dict[str, float] = {}

# Open paper trades: asset -> {trade_id, market_id, direction, entry_oracle_price, end_time}
_open_paper_trades: Dict[str, dict] = {}


# ============================================================================
# Chainlink Oracle Poller
# ============================================================================

def _pick_rpc() -> str:
    """Round-robin over the pool, skipping endpoints in backoff cooldown."""
    global _rpc_index
    now = time.time()
    n = len(POLYGON_RPC_LIST)
    for _ in range(n):
        url = POLYGON_RPC_LIST[_rpc_index % n]
        _rpc_index = (_rpc_index + 1) % n
        if _rpc_cooldown_until.get(url, 0.0) <= now:
            return url
    # Every endpoint is cooling down — use the one that recovers soonest
    return min(POLYGON_RPC_LIST, key=lambda u: _rpc_cooldown_until.get(u, 0.0))


def _rpc_failed(rpc_url: str) -> None:
    """Apply exponential-backoff cooldown to a failing endpoint."""
    streak = _rpc_error_streak.get(rpc_url, 0) + 1
    _rpc_error_streak[rpc_url] = streak
    cooldown = min(RPC_BACKOFF_BASE * (2 ** (streak - 1)), RPC_BACKOFF_CAP)
    _rpc_cooldown_until[rpc_url] = time.time() + cooldown
    logger.warning(f"RPC {rpc_url} cooling down {cooldown:.0f}s (streak {streak})")


def _rpc_succeeded(rpc_url: str) -> None:
    _rpc_error_streak[rpc_url] = 0
    _rpc_cooldown_until[rpc_url] = 0.0


async def poll_chainlink_oracle(asset: str) -> Optional[Dict]:
    """Poll Chainlink price feed on Polygon via JSON-RPC.

    Rotates across POLYGON_RPC_LIST per request; a failing endpoint gets an
    exponential-backoff cooldown so a rate-limited RPC is never burst-retried.
    At most 2 attempts per poll, 1s apart — the next poll cycle (2s later)
    retries on a different endpoint anyway.
    """
    contract = CHAINLINK_FEEDS.get(asset)
    if not contract:
        return None

    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": contract, "data": LATEST_ROUND_DATA}, "latest"],
        "id": 1,
    })

    for attempt in range(2):
        if attempt:
            await asyncio.sleep(1.0)  # never burst-retry within the same second
        rpc_url = _pick_rpc()
        try:
            proc = await asyncio.create_subprocess_exec(
                "curl", "-s", "-X", "POST", rpc_url,
                "-H", "Content-Type: application/json",
                "-d", payload,
                "--max-time", "5",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8)

            output = stdout.decode().strip()
            if not output:
                logger.warning(f"Empty response from {rpc_url}")
                _rpc_failed(rpc_url)
                continue
            result = json.loads(output)

            # RPC-level error (quota exceeded, unauthorized, etc.)
            if result.get("error"):
                err_msg = result["error"].get("message", "")
                logger.warning(f"RPC error on {rpc_url}: {err_msg}")
                _rpc_failed(rpc_url)
                continue

            hex_data = result.get("result", "")
            if not hex_data or len(hex_data) < 322:
                _rpc_failed(rpc_url)
                continue

            # Decode: (roundId, answer, startedAt, updatedAt, answeredInRound)
            data = hex_data[2:]
            chunks = [data[i:i+64] for i in range(0, len(data), 64)]

            answer = int(chunks[1], 16)
            updated_at = int(chunks[3], 16)

            price = answer / 1e8  # Chainlink uses 8 decimals

            _rpc_succeeded(rpc_url)
            return {
                "price": price,
                "updated_at": updated_at,
                "fetched_at": time.time(),
            }

        except Exception as e:
            logger.error(f"Chainlink poll error ({asset}) on {rpc_url}: {e}")
            _stats["errors"] += 1
            _rpc_failed(rpc_url)
            continue

    return None


async def oracle_poller_loop():
    """Continuously poll Chainlink oracles every ~2s (block-time bound)."""
    logger.info("🔗 Starting Chainlink oracle poller...")
    
    while True:
        for asset in list(CHAINLINK_FEEDS.keys()):
            try:
                result = await poll_chainlink_oracle(asset)
                if result:
                    state = _state[asset]
                    state.oracle_price = result["price"]
                    state.oracle_updated_at = result["updated_at"]
                    state.oracle_fetched_at = result["fetched_at"]
                    
                    _stats["oracle_polls"] += 1
                    _stats["last_oracle_poll"] = datetime.now(timezone.utc).isoformat()
                    
                    # Check divergence
                    _check_divergence(asset)
            except Exception as e:
                logger.error(f"Oracle poller error ({asset}): {e}")
                _stats["errors"] += 1
        
        await asyncio.sleep(ORACLE_POLL_INTERVAL)  # ~block time; faster is wasted quota


# ============================================================================
# Binance WebSocket Consumer
# ============================================================================

async def binance_ws_loop():
    """Connect to Binance and stream real-time trades."""
    streams = "/".join(BINANCE_STREAMS)
    url = f"{BINANCE_WS}/{streams}"
    
    while True:
        try:
            logger.info(f"📡 Connecting to Binance WS: {url}")
            async with websockets.connect(url, ping_interval=20) as ws:
                logger.info("✅ Binance WS connected")
                
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        
                        symbol = data.get("s", "")
                        price = float(data.get("p", 0))
                        ts = data.get("T", 0)  # Trade time in ms
                        
                        # Map to our asset names
                        _symbol_map = {
                            "BTCUSDT": "BTC", "ETHUSDT": "ETH",
                            "SOLUSDT": "SOL", "XRPUSDT": "XRP",
                            "DOGEUSDT": "DOGE", "BNBUSDT": "BNB",
                        }
                        asset = _symbol_map.get(symbol)
                        if not asset:
                            continue
                        
                        state = _state[asset]
                        state.binance_price = price
                        state.binance_timestamp = ts
                        
                        _stats["binance_ticks"] += 1
                        _stats["last_binance_tick"] = datetime.now(timezone.utc).isoformat()
                        
                        # Check divergence on every tick
                        _check_divergence(asset)
                    
                    except (json.JSONDecodeError, ValueError, KeyError):
                        continue
        
        except websockets.ConnectionClosed as e:
            logger.warning(f"Binance WS disconnected: {e}. Reconnecting in 3s...")
            await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"Binance WS error: {e}. Reconnecting in 5s...")
            _stats["errors"] += 1
            await asyncio.sleep(5)


# ============================================================================
# Divergence Detection
# ============================================================================

def _check_divergence(asset: str):
    """Check if prices have diverged significantly.
    
    For oracle assets (BTC/ETH/SOL/XRP/DOGE): Binance vs Chainlink on-chain aggregator.
    For oracle-less assets (BNB): Binance vs window-start reference price.
    """
    state = _state[asset]
    
    if state.binance_price <= 0:
        return

    if asset in ORACLE_LESS_ASSETS:
        # ── Window-reference momentum signal ──
        now_int = int(time.time())
        window_ts = (now_int // WINDOW_SECONDS) * WINDOW_SECONDS
        if state.ref_price <= 0 or state.ref_window_ts != window_ts:
            # New window — reset reference price
            state.ref_price = state.binance_price
            state.ref_window_ts = window_ts
            state.divergence_pct = 0.0
            state.latency_signal = "NONE"
            state.signal_strength = "none"
            return
        divergence = (state.binance_price - state.ref_price) / state.ref_price * 100
        # Pseudo oracle_price for event logging
        state.oracle_price = state.ref_price
        state.oracle_updated_at = window_ts
    else:
        if state.oracle_price <= 0:
            return
        # ── Standard oracle latency arb ──
        divergence = (state.binance_price - state.oracle_price) / state.oracle_price * 100

    state.divergence_pct = round(divergence, 4)
    abs_div = abs(divergence)

    # Staleness check: oracle-less assets use window boundary (always fresh within window)
    now = time.time()
    if asset in ORACLE_LESS_ASSETS:
        oracle_age = 0  # window ref is always fresh
    else:
        oracle_age = now - state.oracle_updated_at if state.oracle_updated_at > 0 else 999

    # Determine signal
    if abs_div >= LATENCY_THRESHOLD_PCT:
        direction = "UP" if divergence > 0 else "DOWN"

        if abs_div >= LATENCY_THRESHOLD_HIGH:
            strength = "high"
        elif abs_div >= LATENCY_THRESHOLD_PCT * 2:
            strength = "medium"
        else:
            strength = "low"

        # Only signal if oracle isn't too stale (otherwise it's not latency, it's a dead feed)
        if oracle_age < ORACLE_STALE_SECONDS:
            state.latency_signal = direction
            state.signal_strength = strength

            # ── Dedup: only write to DB once per SIGNAL_DEDUP_SECONDS per asset+direction ──
            dedup_key = f"{asset}:{direction}"
            last_write = _last_db_signal.get(dedup_key, 0.0)
            if now - last_write < SIGNAL_DEDUP_SECONDS:
                return  # skip — same signal burst, don't spam DB

            _last_db_signal[dedup_key] = now

            event = LatencyEvent(
                asset=asset,
                binance_price=state.binance_price,
                oracle_price=state.oracle_price,
                divergence_pct=round(divergence, 4),
                direction=direction,
                strength=strength,
                binance_ts=state.binance_timestamp,
                oracle_ts=state.oracle_updated_at,
                detected_at=datetime.now(timezone.utc).isoformat(),
            )
            
            _recent_events.append(asdict(event))
            _stats["latency_signals"] += 1
            
            # Log medium/high events
            if strength in ("medium", "high"):
                logger.info(
                    f"🎯 LATENCY SIGNAL [{asset}] {direction} "
                    f"div:{divergence:+.3f}% strength:{strength} "
                    f"binance:{state.binance_price:.2f} oracle:{state.oracle_price:.2f}"
                )
            
            # Persist to DB
            _log_event_to_db(event)

            # ── Auto paper trade on medium+ signals ──
            if strength in ("medium", "high") and asset not in _open_paper_trades:
                _trigger_paper_trade(event)
        else:
            state.latency_signal = "STALE"
            state.signal_strength = "none"
    else:
        state.latency_signal = "NONE"
        state.signal_strength = "none"


# ============================================================================
# Paper Trade Engine
# ============================================================================

_ASSET_NAME_MAP = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
    "XRP": "xrp", "DOGE": "dogecoin", "BNB": "bnb",
}

def _find_active_hf_market(asset: str, prefer_duration: str = "5min") -> Optional[dict]:
    """Query Gamma API for the nearest closing 5-min or 15-min UP/DOWN market for this asset.
    
    Falls back to 15-min if no 5-min market found, and vice versa.
    Returns the market with the soonest end date (i.e., most actionable window).
    """
    import urllib.request, urllib.parse
    asset_lower = asset.lower()
    name = _ASSET_NAME_MAP.get(asset, asset_lower)

    duration_checks = {
        "5min":  lambda slug, q: "updown-5m" in slug or "5-minute" in q or "5min" in q or "5 minute" in q,
        "15min": lambda slug, q: "updown-15m" in slug or "15-minute" in q or "15min" in q or "15 minute" in q,
    }
    # Prefer requested duration, fallback to the other
    order = [prefer_duration, "15min" if prefer_duration == "5min" else "5min"]

    try:
        q = urllib.parse.quote(f"{name} up or down")
        url = f"https://gamma-api.polymarket.com/markets?active=true&closed=false&_q={q}&limit=40&order=endDate&ascending=true"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd-HF/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            markets = json.loads(resp.read().decode())

        for dur in order:
            check = duration_checks[dur]
            for m in markets:
                q_text = (m.get("question") or "").lower()
                slug = (m.get("slug") or "").lower()
                if check(slug, q_text):
                    if name in q_text or asset_lower in q_text or asset_lower in slug:
                        logger.debug(f"Found {dur} market for {asset}: {m.get('slug')}")
                        return m
    except Exception as e:
        logger.debug(f"HF market lookup failed for {asset}: {e}")
    return None


def _trigger_paper_trade(event: LatencyEvent):
    """Find active 5-min market and log a paper trade entry."""
    market = _find_active_hf_market(event.asset)
    if not market:
        logger.debug(f"No 5min market found for {event.asset}, skipping paper trade")
        return

    market_id = market.get("id") or market.get("conditionId", "")
    question = market.get("question", "")
    end_time = market.get("endDate") or market.get("end_date") or ""

    # PM YES price = outcomePrices[0] if list, else bestAsk
    out_prices = market.get("outcomePrices")
    if isinstance(out_prices, list) and len(out_prices) >= 2:
        try:
            yes_price = float(out_prices[0])
        except (ValueError, TypeError):
            yes_price = 0.5
    else:
        yes_price = float(market.get("bestAsk") or market.get("lastTradePrice") or 0.5)

    edge_pct = abs(yes_price - 0.5) * 100  # distance from 50¢

    try:
        db = _get_db()
        cur = db.execute(
            """INSERT INTO hf_paper_trades
               (market_id, asset, direction, trigger_type, strength, confidence,
                edge_pct, bet_size, entry_price, entry_oracle_price,
                market_question, market_end_time, opened_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (market_id, event.asset, event.direction, "latency_arb", event.strength,
             abs(event.divergence_pct),  # confidence = divergence %
             edge_pct, PAPER_BET_SIZE, yes_price, event.oracle_price,
             question, end_time, event.detected_at),
        )
        db.commit()
        trade_id = cur.lastrowid
        _open_paper_trades[event.asset] = {
            "trade_id": trade_id,
            "direction": event.direction,
            "entry_oracle_price": event.oracle_price,
            "end_time": end_time,
            "market_id": market_id,
        }
        _stats["paper_trades_opened"] += 1
        logger.info(
            f"📝 PAPER TRADE #{trade_id} [{event.asset}] {event.direction} "
            f"entry={yes_price:.3f} div={event.divergence_pct:+.3f}% "
            f"closes={end_time}"
        )
    except Exception as e:
        logger.error(f"Paper trade log error: {e}")


def _resolve_from_pm_market(market_id: str, direction: str) -> Optional[str]:
    """Poll Gamma API for actual PM market outcome.
    
    Returns 'WIN', 'LOSS', or None (not yet resolved).
    outcomes: ["Up", "Down"] → outcomePrices[0]="1" means UP won.
    """
    import urllib.request
    try:
        url = f"https://gamma-api.polymarket.com/markets/{market_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd-HF/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            m = json.loads(r.read())

        prices = m.get("outcomePrices")
        if not isinstance(prices, list) or len(prices) < 2:
            return None

        up_price = float(prices[0])
        down_price = float(prices[1])

        # Resolved when one side = 1.0 exactly
        if up_price == 1.0 and down_price == 0.0:
            pm_outcome = "UP"
        elif down_price == 1.0 and up_price == 0.0:
            pm_outcome = "DOWN"
        else:
            return None  # Still trading, not settled

        return "WIN" if pm_outcome == direction else "LOSS"

    except Exception as e:
        logger.debug(f"PM resolution poll error ({market_id}): {e}")
        return None


async def paper_trade_resolution_loop():
    """Every 60s resolve expired paper trades by comparing oracle prices."""
    logger.info("📊 Starting paper trade resolution loop...")
    await asyncio.sleep(30)  # let engine warm up first

    while True:
        try:
            now_ts = time.time()
            now_iso = datetime.now(timezone.utc).isoformat()

            # Resolve open in-memory trades whose window has passed
            for asset, trade in list(_open_paper_trades.items()):
                end_time_str = trade.get("end_time", "")
                if not end_time_str:
                    continue
                try:
                    # Parse end_time (ISO or epoch)
                    if isinstance(end_time_str, (int, float)):
                        end_ts = float(end_time_str)
                    else:
                        from datetime import datetime as _dt
                        dt = _dt.fromisoformat(end_time_str.replace("Z", "+00:00"))
                        end_ts = dt.timestamp()
                except Exception:
                    continue

                if now_ts < end_ts:
                    continue  # not expired yet

                # Market window closed — poll PM for actual outcome
                direction = trade["direction"]
                outcome = _resolve_from_pm_market(trade["market_id"], direction)

                if outcome is None:
                    # Market not yet resolved by PM — check again next cycle
                    # Give up after 15 min past end_ts to avoid stale open trades
                    if now_ts - end_ts > 900:
                        outcome = "UNRESOLVED"
                    else:
                        continue

                pnl = PAPER_BET_SIZE * 0.9 if outcome == "WIN" else (-PAPER_BET_SIZE if outcome == "LOSS" else 0.0)

                try:
                    db = _get_db()
                    db.execute(
                        "UPDATE hf_paper_trades SET outcome=?, pnl=?, resolved_at=? WHERE id=?",
                        (outcome, pnl, now_iso, trade["trade_id"])
                    )
                    db.commit()
                    _stats["paper_trades_resolved"] += 1
                    logger.info(
                        f"✅ RESOLVED #{trade['trade_id']} [{asset}] {direction} "
                        f"→ {outcome} (PM ground truth) pnl=${pnl:+.2f}"
                    )
                except Exception as e:
                    logger.error(f"Resolution DB error: {e}")

                del _open_paper_trades[asset]

        except Exception as e:
            logger.error(f"Resolution loop error: {e}")

        await asyncio.sleep(60)


# ============================================================================
# SQLite Persistence
# ============================================================================

_db_conn = None

def _get_db():
    """Get or create SQLite connection."""
    global _db_conn
    if _db_conn is None:
        _db_conn = sqlite3.connect(DB_PATH)
        _db_conn.execute("PRAGMA journal_mode=WAL")
        _db_conn.execute("PRAGMA busy_timeout=5000")  # 5s: enough for transient locks, fast-fail if stuck
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS hf_latency_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset TEXT NOT NULL,
                binance_price REAL,
                oracle_price REAL,
                divergence_pct REAL,
                direction TEXT,
                strength TEXT,
                binance_ts REAL,
                oracle_ts INTEGER,
                detected_at TEXT
            )
        """)
        _db_conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_hf_events_asset_time 
            ON hf_latency_events(asset, detected_at)
        """)
        # Ensure entry_oracle_price column exists (added post-initial schema)
        try:
            _db_conn.execute("ALTER TABLE hf_paper_trades ADD COLUMN entry_oracle_price REAL")
            _db_conn.commit()
        except Exception:
            pass  # column already exists
        _db_conn.commit()
    return _db_conn


def _log_event_to_db(event: LatencyEvent):
    """Persist latency event to SQLite."""
    try:
        db = _get_db()
        db.execute(
            """INSERT INTO hf_latency_events 
               (asset, binance_price, oracle_price, divergence_pct, 
                direction, strength, binance_ts, oracle_ts, detected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event.asset, event.binance_price, event.oracle_price,
             event.divergence_pct, event.direction, event.strength,
             event.binance_ts, event.oracle_ts, event.detected_at)
        )
        db.commit()
    except Exception as e:
        logger.error(f"DB write error: {e}")


# ============================================================================
# HTTP Status Endpoint
# ============================================================================

async def http_handler(reader, writer):
    """Simple HTTP handler for status queries."""
    try:
        request = await asyncio.wait_for(reader.read(4096), timeout=5)
        request_line = request.decode().split("\r\n")[0]
        path = request_line.split(" ")[1] if " " in request_line else "/"
        
        if path == "/health":
            body = json.dumps({"status": "running", "timestamp": datetime.now(timezone.utc).isoformat()})
        elif path == "/state":
            body = json.dumps({
                "prices": {k: asdict(v) for k, v in _state.items()},
                "stats": _stats,
            })
        elif path == "/events":
            body = json.dumps({
                "events": list(_recent_events)[-50:],
                "total": len(_recent_events),
            })
        elif path == "/signals":
            # Current actionable signals
            signals = []
            for asset, state in _state.items():
                if state.latency_signal in ("UP", "DOWN"):
                    signals.append({
                        "asset": asset,
                        "direction": state.latency_signal,
                        "strength": state.signal_strength,
                        "divergence_pct": state.divergence_pct,
                        "binance_price": state.binance_price,
                        "oracle_price": state.oracle_price,
                    })
            body = json.dumps({"signals": signals, "count": len(signals)})
        elif path == "/paper_trades":
            try:
                db = _get_db()
                rows = db.execute(
                    """SELECT id, asset, direction, strength, confidence, entry_price,
                              entry_oracle_price, market_end_time, outcome, pnl, opened_at, resolved_at
                       FROM hf_paper_trades ORDER BY id DESC LIMIT 50"""
                ).fetchall()
                cols = ["id","asset","direction","strength","confidence","entry_price",
                        "entry_oracle_price","market_end_time","outcome","pnl","opened_at","resolved_at"]
                trades = [dict(zip(cols, r)) for r in rows]
                resolved = [t for t in trades if t["outcome"]]
                wins = sum(1 for t in resolved if t["outcome"] == "WIN")
                body = json.dumps({
                    "open": len(_open_paper_trades),
                    "total": len(trades),
                    "resolved": len(resolved),
                    "wins": wins,
                    "win_rate": round(wins / len(resolved), 3) if resolved else None,
                    "total_pnl": round(sum(t["pnl"] or 0 for t in resolved), 2),
                    "trades": trades,
                })
            except Exception as e:
                body = json.dumps({"error": str(e)})
        else:
            body = json.dumps({
                "service": "Polyclawd HF Latency Engine",
                "version": "1.0.0",
                "endpoints": ["/health", "/state", "/events", "/signals"],
                "phase": "Phase 3",
            })
        
        response = (
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"\r\n{body}"
        )
        writer.write(response.encode())
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def start_http_server():
    """Start the status HTTP server."""
    server = await asyncio.start_server(http_handler, "127.0.0.1", HTTP_PORT)
    logger.info(f"📊 Status server listening on http://127.0.0.1:{HTTP_PORT}")
    async with server:
        await server.serve_forever()


# ============================================================================
# Trigger Evaluation Loop
# ============================================================================

# Velocity trackers (persistent across cycles)
_velocity_trackers = {
    "imbalance": ImbalanceVelocityTracker(window_size=12),
    "cvd": CVDAccelerationTracker(window_size=12),
    "liquidation": LiquidationProximityTracker(price_window=30),
}

# Memcached client for writing edge decisions
_mc_client: Optional[aiomcache.Client] = None


async def _get_mc_client() -> aiomcache.Client:
    global _mc_client
    if _mc_client is None:
        _mc_client = aiomcache.Client("localhost", 11211, pool_size=2)
    return _mc_client


async def trigger_evaluation_loop():
    """Evaluate predictive triggers every ~1 second.

    Reads Virtuoso enrichment data from memcached, updates velocity trackers
    with both enrichment + fast Binance/oracle data, evaluates triggers, and
    writes the edge decision back to memcached for Virtuoso API to read.
    """
    logger.info("🎯 Starting trigger evaluation loop...")
    reader = get_enrichment_reader()

    # Wait for Binance WS to populate initial prices
    await asyncio.sleep(5)

    while True:
        try:
            # Read Virtuoso enrichment data
            enrichment = await reader.read_enrichment("BTCUSDT")

            # Build fast data dict from global _state
            btc = _state["BTC"]
            fast = {
                "symbol": "BTCUSDT",
                "binance_price": btc.binance_price,
                "oracle_price": btc.oracle_price,
                "price_change_3m_pct": 0.0,  # TODO: compute from price history
            }

            # Update velocity trackers from enrichment data
            ob = reader.get_orderbook_depth(enrichment, "BTCUSDT")
            if ob:
                bid_d = ob.get("bid_depth", ob.get("bids_total", 0))
                ask_d = ob.get("ask_depth", ob.get("asks_total", 0))
                if bid_d > 0 and ask_d > 0:
                    _velocity_trackers["imbalance"].update(bid_d, ask_d)

            cvd_data = reader.get_cvd_data(enrichment, "BTCUSDT")
            if cvd_data:
                cvd_val = cvd_data.get("cvd", cvd_data.get("cumulative_delta", 0))
                if cvd_val:
                    _velocity_trackers["cvd"].update(float(cvd_val))

            if btc.binance_price > 0:
                _velocity_trackers["liquidation"].update_price(btc.binance_price)

            # Evaluate triggers
            decision = evaluate_edge(enrichment, fast, _velocity_trackers)

            # Build payload with oracle state
            oracle_state = {
                "binance_price": btc.binance_price,
                "oracle_price": btc.oracle_price,
                "divergence_pct": btc.divergence_pct,
                "latency_signal": btc.latency_signal,
            }
            payload = build_edge_payload(decision, oracle_state)

            # Write to memcached for Virtuoso API
            mc = await _get_mc_client()
            payload_bytes = json.dumps(payload).encode()
            await mc.set(b"polymarket:edge:BTCUSDT", payload_bytes, exptime=30)

            if decision.action == "TRADE":
                logger.info(
                    f"🎯 EDGE SIGNAL: {decision.direction} "
                    f"conf={decision.confidence:.1%} "
                    f"trigger={decision.trigger_type} "
                    f"size=${decision.sizing.get('recommended_usd', 0):.0f}"
                )

        except Exception as e:
            logger.debug(f"Trigger eval error: {e}")

        await asyncio.sleep(1)


# ============================================================================
# Main Entry Point
# ============================================================================

async def main():
    """Run all components concurrently."""
    _stats["started_at"] = datetime.now(timezone.utc).isoformat()
    
    logger.info("=" * 60)
    logger.info("🚀 Polyclawd HF Latency Engine — Phase 3")
    logger.info(f"   Binance streams: {BINANCE_STREAMS}")
    logger.info(f"   Chainlink feeds: {list(CHAINLINK_FEEDS.keys())}")
    logger.info(f"   Latency threshold: {LATENCY_THRESHOLD_PCT}%")
    logger.info(f"   Status port: {HTTP_PORT}")
    logger.info(f"   DB: {DB_PATH}")
    logger.info("=" * 60)
    
    # Initialize DB table
    _get_db()
    
    await asyncio.gather(
        binance_ws_loop(),
        oracle_poller_loop(),
        start_http_server(),
        trigger_evaluation_loop(),
        paper_trade_resolution_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())

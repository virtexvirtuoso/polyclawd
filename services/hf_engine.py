"""
HF Latency Engine — Phase 3

Persistent service that:
1. Streams real-time BTC/ETH prices from Binance WebSocket
2. Polls Chainlink oracle prices on Polygon (every ~500ms)
3. Detects latency divergence (Binance moved but oracle hasn't updated)
4. Generates directional signals when delta > threshold
5. Logs all events to SQLite for backtesting

Designed to run as a separate systemd service alongside polyclawd-api.
Exposes state via a small HTTP endpoint on port 8422.

Based on: [[Polymarket 134 to 200K Story]] and [[HF_MODULE_PLAN]]
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Dict, Optional
from pathlib import Path

import websockets

logger = logging.getLogger("hf_engine")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

# ============================================================================
# Configuration
# ============================================================================

BINANCE_WS = "wss://stream.binance.com:9443/ws"
POLYGON_RPC = os.getenv("POLYGON_RPC", "https://1rpc.io/matic")

# Chainlink Price Feed Aggregator contracts on Polygon
CHAINLINK_FEEDS = {
    "BTC": "0xc907E116054Ad103354f2D350FD2514433D57F6f",
    "ETH": "0xF9680D99D6C9589e2a93a78A04A279e509205945",
}

# latestRoundData() selector
LATEST_ROUND_DATA = "0xfeaf968c"

# Latency arb thresholds
LATENCY_THRESHOLD_PCT = 0.3   # Min % divergence to flag
LATENCY_THRESHOLD_HIGH = 0.8  # High-conviction threshold
ORACLE_STALE_SECONDS = 30     # Oracle considered stale if > this

# Binance streams
BINANCE_STREAMS = ["btcusdt@trade", "ethusdt@trade"]

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
    "BTC": PriceState(asset="BTC"),
    "ETH": PriceState(asset="ETH"),
}
_recent_events: deque = deque(maxlen=200)
_stats = {
    "binance_ticks": 0,
    "oracle_polls": 0,
    "latency_signals": 0,
    "started_at": None,
    "last_binance_tick": None,
    "last_oracle_poll": None,
    "errors": 0,
}


# ============================================================================
# Chainlink Oracle Poller
# ============================================================================

async def poll_chainlink_oracle(asset: str) -> Optional[Dict]:
    """Poll Chainlink price feed on Polygon via JSON-RPC."""
    contract = CHAINLINK_FEEDS.get(asset)
    if not contract:
        return None
    
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": contract, "data": LATEST_ROUND_DATA}, "latest"],
        "id": 1,
    })
    
    try:
        # Use asyncio subprocess to not block
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-X", "POST", POLYGON_RPC,
            "-H", "Content-Type: application/json",
            "-d", payload,
            "--max-time", "5",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
        
        result = json.loads(stdout.decode())
        hex_data = result.get("result", "")
        
        if not hex_data or len(hex_data) < 322:
            return None
        
        # Decode: (roundId, answer, startedAt, updatedAt, answeredInRound)
        data = hex_data[2:]
        chunks = [data[i:i+64] for i in range(0, len(data), 64)]
        
        answer = int(chunks[1], 16)
        updated_at = int(chunks[3], 16)
        
        price = answer / 1e8  # Chainlink uses 8 decimals
        
        return {
            "price": price,
            "updated_at": updated_at,
            "fetched_at": time.time(),
        }
    
    except Exception as e:
        logger.error(f"Chainlink poll error ({asset}): {e}")
        _stats["errors"] += 1
        return None


async def oracle_poller_loop():
    """Continuously poll Chainlink oracles every ~500ms."""
    logger.info("🔗 Starting Chainlink oracle poller...")
    
    while True:
        for asset in ["BTC", "ETH"]:
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
        
        await asyncio.sleep(0.5)  # Poll every 500ms


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
                        if symbol == "BTCUSDT":
                            asset = "BTC"
                        elif symbol == "ETHUSDT":
                            asset = "ETH"
                        else:
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
    """Check if Binance and oracle prices have diverged significantly."""
    state = _state[asset]
    
    if state.binance_price <= 0 or state.oracle_price <= 0:
        return
    
    # Calculate divergence
    divergence = (state.binance_price - state.oracle_price) / state.oracle_price * 100
    state.divergence_pct = round(divergence, 4)
    
    abs_div = abs(divergence)
    
    # Check oracle staleness
    now = time.time()
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
            
            # Log high-strength events
            if strength in ("medium", "high"):
                logger.info(
                    f"🎯 LATENCY SIGNAL [{asset}] {direction} "
                    f"div:{divergence:+.3f}% strength:{strength} "
                    f"binance:{state.binance_price:.2f} oracle:{state.oracle_price:.2f}"
                )
            
            # Persist to DB
            _log_event_to_db(event)
        else:
            state.latency_signal = "STALE"
            state.signal_strength = "none"
    else:
        state.latency_signal = "NONE"
        state.signal_strength = "none"


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
    )


if __name__ == "__main__":
    asyncio.run(main())

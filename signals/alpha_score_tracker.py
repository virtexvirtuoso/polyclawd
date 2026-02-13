"""
Alpha Score Tracker — Snapshots Virtuoso confluence alpha scores + BTC price data.
Stores in SQLite for trend analysis and prediction market signal confirmation.
"""

import sqlite3
import time
import logging
import httpx
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "storage" / "shadow_trades.db"
DASHBOARD_URL = "http://localhost:8002"
COINGECKO_BTC = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true"

SNAPSHOT_INTERVAL = 1800  # 30 minutes


def _get_conn(db_path: str = None) -> sqlite3.Connection:
    """Get SQLite connection with WAL mode and busy timeout."""
    conn = sqlite3.connect(db_path or str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_path: str = None):
    """Create alpha_snapshots table if not exists."""
    conn = _get_conn(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alpha_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            symbol TEXT NOT NULL,
            confluence_score REAL,
            signal_type TEXT,
            technical REAL,
            volume REAL,
            orderflow REAL,
            sentiment REAL,
            orderbook REAL,
            price_structure REAL,
            price REAL,
            change_24h REAL,
            volume_24h REAL,
            source TEXT DEFAULT 'dashboard'
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_alpha_symbol_ts 
        ON alpha_snapshots(symbol, timestamp)
    """)
    # BTC/ETH price snapshots (from ticker/coingecko since they're not in dashboard)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            symbol TEXT NOT NULL,
            price REAL NOT NULL,
            change_24h REAL,
            volume_24h REAL,
            high_24h REAL,
            low_24h REAL,
            bid REAL,
            ask REAL,
            source TEXT DEFAULT 'bybit'
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_price_symbol_ts 
        ON price_snapshots(symbol, timestamp)
    """)
    conn.commit()
    conn.close()


def snapshot_alpha_scores(db_path: str = None) -> dict:
    """Fetch all confluence scores from dashboard and store snapshot."""
    now = time.time()
    results = {"symbols": 0, "scores": {}, "errors": []}

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(f"{DASHBOARD_URL}/api/dashboard/overview")
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        results["errors"].append(f"Dashboard fetch failed: {e}")
        return results

    signals = data.get("signals", [])
    if not signals:
        results["errors"].append("No signals in dashboard overview")
        return results

    conn = _get_conn(db_path)

    for s in signals:
        symbol = s.get("symbol", "")
        score = s.get("confluence_score")
        components = s.get("components", {})

        conn.execute("""
            INSERT INTO alpha_snapshots 
            (timestamp, symbol, confluence_score, signal_type,
             technical, volume, orderflow, sentiment, orderbook, price_structure,
             price, change_24h, volume_24h)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            now, symbol, score, s.get("signal_type"),
            components.get("technical"), components.get("volume"),
            components.get("orderflow"), components.get("sentiment"),
            components.get("orderbook"), components.get("price_structure"),
            s.get("price"), s.get("change_24h"), s.get("volume_24h")
        ))

        results["scores"][symbol] = round(score, 1) if score else None
        results["symbols"] += 1

    conn.commit()
    conn.close()

    results["market_regime"] = data.get("market_regime", "unknown")
    return results


def snapshot_btc_eth(db_path: str = None) -> dict:
    """Snapshot BTC and ETH prices from Virtuoso ticker endpoint."""
    now = time.time()
    results = {"prices": {}, "errors": []}

    for symbol in ["BTCUSDT", "ETHUSDT"]:
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.get(f"{DASHBOARD_URL}/api/market/ticker/{symbol}")
                resp.raise_for_status()
                data = resp.json()

            conn = _get_conn(db_path)
            conn.execute("""
                INSERT INTO price_snapshots
                (timestamp, symbol, price, change_24h, volume_24h, 
                 high_24h, low_24h, bid, ask, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now, symbol, data.get("price"),
                data.get("price_change_percent_24h"),
                data.get("quote_volume_24h"),
                data.get("high_24h"), data.get("low_24h"),
                data.get("bid"), data.get("ask"),
                data.get("exchange", "bybit")
            ))
            conn.commit()
            conn.close()

            results["prices"][symbol] = data.get("price")
        except Exception as e:
            results["errors"].append(f"{symbol}: {e}")

    return results


def run_snapshot(db_path: str = None) -> dict:
    """Run full snapshot — alpha scores + BTC/ETH prices."""
    init_db(db_path)
    alpha = snapshot_alpha_scores(db_path)
    prices = snapshot_btc_eth(db_path)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "alpha_symbols": alpha["symbols"],
        "scores": alpha.get("scores", {}),
        "market_regime": alpha.get("market_regime"),
        "prices": prices.get("prices", {}),
        "errors": alpha.get("errors", []) + prices.get("errors", [])
    }


def get_score_history(symbol: str, hours: int = 24, db_path: str = None) -> list:
    """Get confluence score history for a symbol."""
    init_db(db_path)
    conn = _get_conn(db_path)
    conn.row_factory = sqlite3.Row
    cutoff = time.time() - (hours * 3600)

    rows = conn.execute("""
        SELECT timestamp, confluence_score, signal_type,
               technical, volume, orderflow, sentiment, orderbook, price_structure,
               price, change_24h
        FROM alpha_snapshots
        WHERE symbol = ? AND timestamp > ?
        ORDER BY timestamp DESC
    """, (symbol, cutoff)).fetchall()
    conn.close()

    return [dict(r) for r in rows]


def get_price_history(symbol: str, hours: int = 24, db_path: str = None) -> list:
    """Get price snapshot history for BTC/ETH."""
    init_db(db_path)
    conn = _get_conn(db_path)
    conn.row_factory = sqlite3.Row
    cutoff = time.time() - (hours * 3600)

    rows = conn.execute("""
        SELECT timestamp, price, change_24h, volume_24h, high_24h, low_24h
        FROM price_snapshots
        WHERE symbol = ? AND timestamp > ?
        ORDER BY timestamp DESC
    """, (symbol, cutoff)).fetchall()
    conn.close()

    return [dict(r) for r in rows]


def get_score_delta(symbol: str, hours: int = 2, db_path: str = None) -> dict:
    """Get score change over time period — key signal for prediction markets."""
    history = get_score_history(symbol, hours, db_path)
    if len(history) < 2:
        return {"symbol": symbol, "delta": None, "reason": "insufficient data"}

    latest = history[0]
    oldest = history[-1]

    score_delta = (latest["confluence_score"] or 0) - (oldest["confluence_score"] or 0)

    return {
        "symbol": symbol,
        "current_score": latest["confluence_score"],
        "score_hours_ago": oldest["confluence_score"],
        "delta": round(score_delta, 2),
        "signal_type": latest["signal_type"],
        "hours": hours,
        "snapshots": len(history)
    }


def get_btc_price_delta(hours: int = 2, db_path: str = None) -> dict:
    """BTC price change over period — for prediction market resolution certainty."""
    history = get_price_history("BTCUSDT", hours, db_path)
    if len(history) < 2:
        return {"delta": None, "reason": "insufficient data"}

    latest = history[0]
    oldest = history[-1]
    price_delta = latest["price"] - oldest["price"]
    pct_delta = (price_delta / oldest["price"]) * 100

    return {
        "current_price": latest["price"],
        "price_hours_ago": oldest["price"],
        "delta_usd": round(price_delta, 2),
        "delta_pct": round(pct_delta, 3),
        "hours": hours,
        "snapshots": len(history)
    }


def score_velocity_modifier(symbol: str, hours: int = 2, db_path: str = None) -> dict:
    """Convert score delta into a confidence multiplier [0.7, 1.3].

    Positive delta (score improving) -> multiplier > 1.0
    Negative delta (score deteriorating) -> multiplier < 1.0
    Insufficient data -> multiplier = 1.0 (neutral)

    Args:
        symbol: Crypto symbol (e.g., "BTCUSDT")
        hours: Lookback period for delta calculation
        db_path: Optional DB path override

    Returns:
        Dict with multiplier, delta, symbol, and metadata
    """
    delta_data = get_score_delta(symbol, hours, db_path)
    delta = delta_data.get("delta")

    if delta is None or delta_data.get("snapshots", 0) < 2:
        return {
            "multiplier": 1.0,
            "delta": None,
            "symbol": symbol,
            "reason": "insufficient_data",
            "hours": hours,
        }

    # Normalize delta to [-1, 1] range
    # Typical score deltas are ±10 over 2 hours; 5.0 is the half-range
    NORM_THRESHOLD = 5.0
    normalized = max(-1.0, min(1.0, delta / NORM_THRESHOLD))

    # Linear mapping to [0.7, 1.3]
    multiplier = 1.0 + (normalized * 0.3)

    return {
        "multiplier": round(multiplier, 4),
        "delta": delta,
        "normalized": round(normalized, 4),
        "symbol": symbol,
        "current_score": delta_data.get("current_score"),
        "signal_type": delta_data.get("signal_type"),
        "snapshots": delta_data.get("snapshots"),
        "hours": hours,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = run_snapshot()
    print(f"Snapshot: {result['alpha_symbols']} symbols, BTC=${result['prices'].get('BTCUSDT', 'N/A')}")
    print(f"Scores: {result['scores']}")
    if result['errors']:
        print(f"Errors: {result['errors']}")

#!/usr/bin/env python3
"""
Volume Spike Detector — flags 3x+ volume surges as NO entry signals.

Thesis: Retail FOMO drives volume spikes → YES gets overpriced → best NO entry.
Uses existing signal_snapshots table for historical volume baselines.
"""

import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "storage" / "shadow_trades.db"

# Spike thresholds
SPIKE_RATIO = 3.0       # 3x average = spike
MEGA_SPIKE_RATIO = 10.0  # 10x average = mega spike (extreme FOMO)
MIN_HISTORY_POINTS = 3   # Need at least 3 prior snapshots for reliable baseline
LOOKBACK_HOURS = 48      # Window for computing average volume
MIN_VOLUME = 100         # Ignore markets with trivially low volume


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def get_volume_baseline(market_id: str, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """Get average volume for a market over the lookback window.

    Returns:
        {"avg_volume": float, "data_points": int, "min_volume": int, "max_volume": int}
    """
    close_conn = False
    if conn is None:
        conn = _get_db()
        close_conn = True

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%d %H:%M")

    rows = conn.execute(
        """SELECT volume FROM signal_snapshots 
           WHERE market_id = ? AND volume IS NOT NULL AND volume > 0
           AND (snapshot_date || ' ' || snapshot_time) >= ?
           ORDER BY id DESC""",
        (market_id, cutoff)
    ).fetchall()

    if close_conn:
        conn.close()

    if not rows:
        return {"avg_volume": 0, "data_points": 0, "min_volume": 0, "max_volume": 0}

    volumes = [r["volume"] for r in rows]
    return {
        "avg_volume": sum(volumes) / len(volumes),
        "data_points": len(volumes),
        "min_volume": min(volumes),
        "max_volume": max(volumes),
    }


def detect_spike(market_id: str, current_volume: int, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """Check if current volume is a spike relative to historical baseline.

    Returns:
        {"spike": bool, "ratio": float, "level": str, "avg_volume": float, "data_points": int}
        level: "none" | "spike" (3x) | "mega" (10x)
    """
    if current_volume < MIN_VOLUME:
        logger.debug("Volume too low for spike check: market=%s vol=%d min=%d", market_id, current_volume, MIN_VOLUME)
        return {"spike": False, "ratio": 0, "level": "none", "avg_volume": 0, "data_points": 0}

    baseline = get_volume_baseline(market_id, conn)

    if baseline["data_points"] < MIN_HISTORY_POINTS:
        logger.debug(
            "Insufficient history for spike check: market=%s points=%d need=%d",
            market_id, baseline["data_points"], MIN_HISTORY_POINTS
        )
        return {
            "spike": False, "ratio": 0, "level": "none",
            "avg_volume": baseline["avg_volume"], "data_points": baseline["data_points"],
            "reason": f"Need {MIN_HISTORY_POINTS} snapshots, have {baseline['data_points']}"
        }

    avg = baseline["avg_volume"]
    if avg <= 0:
        return {"spike": False, "ratio": 0, "level": "none", "avg_volume": 0, "data_points": baseline["data_points"]}

    ratio = current_volume / avg

    if ratio >= MEGA_SPIKE_RATIO:
        level = "mega"
    elif ratio >= SPIKE_RATIO:
        level = "spike"
    else:
        level = "none"

    is_spike = level != "none"

    if is_spike:
        logger.info(
            "📈 VOLUME SPIKE: market=%s ratio=%.1fx level=%s current=%d avg=%.0f points=%d",
            market_id[:40], ratio, level, current_volume, avg, baseline["data_points"]
        )
    else:
        logger.debug(
            "No spike: market=%s ratio=%.1fx current=%d avg=%.0f",
            market_id[:40], ratio, current_volume, avg
        )

    return {
        "spike": is_spike,
        "ratio": round(ratio, 2),
        "level": level,
        "avg_volume": round(avg, 0),
        "data_points": baseline["data_points"],
    }


def enrich_signals(signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Enrich a batch of signals with volume spike data.

    Adds to each signal:
        volume_spike: bool
        volume_spike_ratio: float
        volume_spike_level: str ("none"|"spike"|"mega")
    """
    if not signals:
        return signals

    conn = _get_db()
    enriched = 0

    for sig in signals:
        market_id = sig.get("market_id") or sig.get("ticker") or sig.get("id", "")
        volume = sig.get("volume", 0)
        if isinstance(volume, str):
            try:
                volume = int(float(volume))
            except (ValueError, TypeError):
                volume = 0

        if not market_id or volume <= 0:
            sig["volume_spike"] = False
            sig["volume_spike_ratio"] = 0
            sig["volume_spike_level"] = "none"
            continue

        result = detect_spike(market_id, volume, conn)
        sig["volume_spike"] = result["spike"]
        sig["volume_spike_ratio"] = result["ratio"]
        sig["volume_spike_level"] = result["level"]

        if result["spike"]:
            enriched += 1

    conn.close()
    logger.info("Volume spike enrichment: %d/%d signals spiking", enriched, len(signals))
    return signals


def scan_all_spikes(limit: int = 50) -> List[Dict[str, Any]]:
    """Scan the latest signal snapshot for volume spikes across all markets.

    Returns list of spiking markets sorted by ratio (highest first).
    """
    conn = _get_db()

    # Get the most recent snapshot batch
    latest = conn.execute(
        "SELECT snapshot_date, snapshot_time FROM signal_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()

    if not latest:
        conn.close()
        return []

    # Get all markets from the latest snapshot
    rows = conn.execute(
        """SELECT market_id, market, volume, price, side, platform, category
           FROM signal_snapshots 
           WHERE snapshot_date = ? AND snapshot_time = ? AND volume > ?
           ORDER BY volume DESC LIMIT ?""",
        (latest["snapshot_date"], latest["snapshot_time"], MIN_VOLUME, limit)
    ).fetchall()

    spikes = []
    for row in rows:
        result = detect_spike(row["market_id"], row["volume"], conn)
        if result["spike"]:
            spikes.append({
                "market_id": row["market_id"],
                "market": row["market"],
                "platform": row["platform"],
                "category": row["category"],
                "side": row["side"],
                "price": row["price"],
                "current_volume": row["volume"],
                "avg_volume": result["avg_volume"],
                "spike_ratio": result["ratio"],
                "spike_level": result["level"],
            })

    conn.close()
    spikes.sort(key=lambda x: x["spike_ratio"], reverse=True)

    logger.info("Volume spike scan: %d spikes found from %d markets", len(spikes), len(rows))
    return spikes


def get_spike_history(market_id: str, hours: int = 72) -> List[Dict[str, Any]]:
    """Get volume history for a specific market (for debugging/charting)."""
    conn = _get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")

    rows = conn.execute(
        """SELECT snapshot_date, snapshot_time, volume, price
           FROM signal_snapshots
           WHERE market_id = ? AND (snapshot_date || ' ' || snapshot_time) >= ?
           ORDER BY id ASC""",
        (market_id, cutoff)
    ).fetchall()
    conn.close()

    return [dict(r) for r in rows]

#!/usr/bin/env python3
"""
Price Momentum Filter — only bet NO when YES price is rising (retail FOMO).

Thesis: Fading peak optimism, not catching falling knives.
- YES rising 5%+ in 24h → FOMO confirmed → best NO entry (boost)
- YES flat (±5%) → standard signal → allow
- YES falling 5%+ → market self-correcting → skip (no edge left)

Uses signal_snapshots table for price history.
"""

import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "storage" / "shadow_trades.db"

# Momentum thresholds
RISING_THRESHOLD = 0.05    # +5% = rising (FOMO)
FALLING_THRESHOLD = -0.05  # -5% = falling (self-correcting)
LOOKBACK_HOURS = 24        # Window for momentum calculation
MIN_DATA_POINTS = 2        # Need at least 2 snapshots for trend


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def get_price_history(market_id: str, hours: int = LOOKBACK_HOURS, conn: Optional[sqlite3.Connection] = None) -> List[Dict[str, Any]]:
    """Get price snapshots for a market over the lookback window."""
    close_conn = False
    if conn is None:
        conn = _get_db()
        close_conn = True

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    rows = conn.execute(
        """SELECT price, snapshot_time, volume
           FROM signal_snapshots
           WHERE market_id = ? AND price IS NOT NULL AND price > 0
           AND snapshot_time >= ?
           ORDER BY id ASC""",
        (market_id, cutoff)
    ).fetchall()

    if close_conn:
        conn.close()

    return [dict(r) for r in rows]


def calculate_momentum(market_id: str, current_price: float = None, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """Calculate YES price momentum for a market.

    Returns:
        {
            "momentum": float,         # Price change as fraction (-1 to +inf)
            "direction": str,          # "rising" | "flat" | "falling"
            "oldest_price": float,     # Earliest price in window
            "newest_price": float,     # Latest price (or current_price if given)
            "data_points": int,
            "hours_span": float,       # Actual time span of data
            "recommendation": str,     # "enter" | "skip" | "boost"
        }
    """
    history = get_price_history(market_id, LOOKBACK_HOURS, conn)

    if len(history) < MIN_DATA_POINTS and current_price is None:
        logger.debug("Insufficient price history: market=%s points=%d", market_id[:30], len(history))
        return {
            "momentum": 0,
            "direction": "unknown",
            "oldest_price": 0,
            "newest_price": current_price or 0,
            "data_points": len(history),
            "hours_span": 0,
            "recommendation": "enter",  # Allow by default when no data
            "reason": f"Need {MIN_DATA_POINTS} snapshots, have {len(history)}",
        }

    oldest_price = history[0]["price"] if history else 0

    if current_price is not None and current_price > 0:
        newest_price = current_price
    elif history:
        newest_price = history[-1]["price"]
    else:
        newest_price = 0

    if oldest_price <= 0:
        return {
            "momentum": 0, "direction": "unknown",
            "oldest_price": 0, "newest_price": newest_price,
            "data_points": len(history), "hours_span": 0,
            "recommendation": "enter", "reason": "No valid oldest price",
        }

    momentum = (newest_price - oldest_price) / oldest_price

    # Calculate actual time span
    hours_span = 0
    if len(history) >= 2:
        try:
            first_t = datetime.fromisoformat(history[0]["snapshot_time"].replace("Z", "+00:00"))
            last_t = datetime.fromisoformat(history[-1]["snapshot_time"].replace("Z", "+00:00"))
            hours_span = (last_t - first_t).total_seconds() / 3600
        except Exception:
            pass

    if momentum >= RISING_THRESHOLD:
        direction = "rising"
        recommendation = "boost"  # FOMO confirmed — best NO entry
    elif momentum <= FALLING_THRESHOLD:
        direction = "falling"
        recommendation = "skip"   # Market self-correcting
    else:
        direction = "flat"
        recommendation = "enter"  # Standard signal

    logger.debug(
        "Momentum: market=%s dir=%s mom=%.1f%% oldest=%.3f newest=%.3f points=%d span=%.1fh",
        market_id[:30], direction, momentum * 100, oldest_price, newest_price, len(history), hours_span
    )

    if recommendation == "skip":
        logger.info(
            "⬇️ MOMENTUM SKIP: market=%s mom=%.1f%% (%.3f→%.3f) — self-correcting",
            market_id[:30], momentum * 100, oldest_price, newest_price
        )
    elif recommendation == "boost":
        logger.info(
            "📈 MOMENTUM BOOST: market=%s mom=+%.1f%% (%.3f→%.3f) — FOMO rising",
            market_id[:30], momentum * 100, oldest_price, newest_price
        )

    return {
        "momentum": round(momentum, 4),
        "direction": direction,
        "oldest_price": round(oldest_price, 4),
        "newest_price": round(newest_price, 4),
        "data_points": len(history),
        "hours_span": round(hours_span, 1),
        "recommendation": recommendation,
    }


def check_entry(market_id: str, current_price: float, side: str = "NO", conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """Check if momentum allows entry for this side.

    For NO bets:
      - YES rising → boost (1.15x)
      - YES flat → allow (1.0x)
      - YES falling → block

    For YES bets: momentum filter not applied (pass-through).

    Returns:
        {"allow": bool, "multiplier": float, "momentum_data": dict}
    """
    if side != "NO":
        return {"allow": True, "multiplier": 1.0, "momentum_data": None}

    mom = calculate_momentum(market_id, current_price, conn)

    if mom["recommendation"] == "skip":
        return {
            "allow": False,
            "multiplier": 0,
            "momentum_data": mom,
            "reason": f"YES falling {mom['momentum']*100:+.1f}% — market self-correcting",
        }
    elif mom["recommendation"] == "boost":
        return {
            "allow": True,
            "multiplier": 1.15,  # 15% boost on confirmed FOMO
            "momentum_data": mom,
        }
    else:
        return {
            "allow": True,
            "multiplier": 1.0,
            "momentum_data": mom,
        }


def enrich_signals(signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Enrich signals with momentum data. Adds momentum_direction, momentum_pct, momentum_action."""
    if not signals:
        return signals

    conn = _get_db()
    boosted = 0
    skipped = 0

    for sig in signals:
        market_id = sig.get("market_id") or sig.get("ticker") or sig.get("id", "")
        price = sig.get("entry_price") or sig.get("price") or sig.get("market_price", 0)
        if isinstance(price, str):
            try:
                price = float(price)
            except (ValueError, TypeError):
                price = 0
        side = (sig.get("side") or "NO").upper()

        if not market_id or price <= 0:
            sig["momentum_direction"] = "unknown"
            sig["momentum_pct"] = 0
            sig["momentum_action"] = "enter"
            continue

        result = check_entry(market_id, price, side, conn)
        mom = result.get("momentum_data") or {}

        sig["momentum_direction"] = mom.get("direction", "unknown")
        sig["momentum_pct"] = round(mom.get("momentum", 0) * 100, 1)
        sig["momentum_action"] = mom.get("recommendation", "enter")

        if result.get("allow") is False:
            skipped += 1
        elif result.get("multiplier", 1.0) > 1.0:
            boosted += 1

    conn.close()
    logger.info("Momentum enrichment: %d boosted, %d would-skip out of %d signals", boosted, skipped, len(signals))
    return signals

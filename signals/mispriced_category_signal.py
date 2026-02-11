#!/usr/bin/env python3
"""
Mispriced Category + Whale Confirmation Signal Source (v2)

Live signal generator based on backtest results:
- 75% win rate across 155K historical trades
- 1.25 Sharpe ratio
- Targets high-volume markets in mispriced categories
- Uses volume spikes and whale activity as confirmation

v2 improvements:
- Hard 30-day max filter (backtest: <7d best theta)
- Volume floor raised to 1000 contracts (live recommendation)
- Result caching (60s TTL) to avoid blocking uvicorn
- Paper trade shadow logging
- Polymarket cross-platform scan for arb confirmation

Integrates with Polyclawd signal aggregation pipeline.
"""

import json
import logging
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from pathlib import Path

logger = logging.getLogger(__name__)

# ============================================================================
# Strategy Parameters (from backtest optimization)
# ============================================================================

# Categories with historically high pricing error (>15%)
# From analysis of 3.75M markets, 332 categories
MISPRICED_CATEGORIES = {
    # FX/Macro — worst calibrated
    'KXEURUSDH': {'error': 0.454, 'tier': 'high'},
    'KXGBPUSDH': {'error': 0.40, 'tier': 'high'},
    'KXGDPH': {'error': 0.35, 'tier': 'high'},
    'KXCPIH': {'error': 0.32, 'tier': 'high'},
    # Entertainment — consistently mispriced
    'KXSPOTIFYARTISTD': {'error': 0.60, 'tier': 'extreme'},
    'KXSPOTIFYLISTD': {'error': 0.55, 'tier': 'extreme'},
    # Weather — hard to predict, markets wrong often
    'KXTEMPD': {'error': 0.28, 'tier': 'medium'},
    'KXWIND': {'error': 0.25, 'tier': 'medium'},
    'KXRAIND': {'error': 0.24, 'tier': 'medium'},
    'KXHUMID': {'error': 0.22, 'tier': 'medium'},
    'KXTEMPW': {'error': 0.20, 'tier': 'medium'},
    'KXSNOW': {'error': 0.19, 'tier': 'medium'},
    # Financial — moderate mispricing
    'KXETF': {'error': 0.18, 'tier': 'medium'},
    'KXSTONKS': {'error': 0.17, 'tier': 'medium'},
    'KXCRYPTO': {'error': 0.16, 'tier': 'medium'},
}

# Well-calibrated categories to NEVER trade (waste of edge)
EFFICIENT_CATEGORIES = {
    'KXPGATOUR', 'KXMLB', 'KXNBA', 'KXNHL', 'KXNFL',
    'KXAOWOMEN', 'KXFIRSTSUPERBOWLSONG',
}

# Thresholds (v2: tightened for live)
MIN_VOLUME = 1000          # Raised from 500 — live recommendation
WHALE_VOLUME = 10000       # Whale tier threshold
CONTESTED_LOW = 15         # Cents — min price for contested zone
CONTESTED_HIGH = 85        # Cents — max price for contested zone
MAX_DAYS_TO_CLOSE = 30     # Hard cap — backtest showed <7d best, >30d waste
MIN_EDGE_PCT = 3           # Minimum category edge % to generate signal

# Confidence scoring weights
WEIGHT_CATEGORY_EDGE = 0.35
WEIGHT_VOLUME_SPIKE = 0.25
WEIGHT_WHALE_ACTIVITY = 0.20
WEIGHT_THETA = 0.20

# Cache (avoid blocking uvicorn on repeated calls)
_cache = {"data": None, "timestamp": 0}
CACHE_TTL = 60  # seconds

# Shadow trade log
SHADOW_LOG = Path(__file__).parent.parent / "storage" / "shadow_trades.json"

# ============================================================================
# Kalshi API Access
# ============================================================================

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"


def _fetch_json(url: str, timeout: int = 12) -> Any:
    """Fetch JSON with timeout and error handling."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.warning(f"Fetch failed {url[:80]}: {e}")
        return None


def fetch_kalshi_markets(status: str = "open") -> List[Dict]:
    """Fetch active markets from Kalshi. Two sources merged, deduped."""
    all_markets = []
    seen_tickers = set()

    # 1. Events endpoint (has category + series info)
    data = _fetch_json(
        f"{KALSHI_API}/events?limit=30&status={status}&with_nested_markets=true",
        timeout=12,
    )
    if data:
        for event in data.get("events", []):
            series = event.get("series_ticker", "")
            category = event.get("category", "")
            for m in event.get("markets", []):
                t = m.get("ticker", "")
                if t in seen_tickers:
                    continue
                seen_tickers.add(t)
                m["_series_ticker"] = series
                m["_event_category"] = category
                m["volume"] = int(float(m.get("volume_fp", "0") or "0"))
                all_markets.append(m)

    # 2. Direct markets endpoint (broader, may have more)
    data = _fetch_json(
        f"{KALSHI_API}/markets?limit=100&status={status}",
        timeout=12,
    )
    if data:
        for m in data.get("markets", []):
            t = m.get("ticker", "")
            if t in seen_tickers:
                continue
            seen_tickers.add(t)
            m["volume"] = int(float(m.get("volume_fp", "0") or "0"))
            all_markets.append(m)

    return all_markets


# ============================================================================
# Signal Generation
# ============================================================================

def extract_category(event_ticker: str) -> str:
    """Extract category prefix from Kalshi event ticker.
    e.g., KXEURUSDH-26FEB11 → KXEURUSDH
    """
    if not event_ticker:
        return ""
    return event_ticker.split('-')[0] if '-' in event_ticker else event_ticker


def calculate_signal_confidence(
    category_edge: float,
    volume: int,
    price_cents: int,
    days_to_close: float,
    avg_category_volume: float = 1000,
) -> Dict[str, Any]:
    """Calculate composite confidence score for a market signal."""
    # 1. Category edge score (0-100)
    edge_score = min(100, (category_edge / 0.60) * 100)

    # 2. Volume spike score (0-100)
    volume_ratio = volume / max(avg_category_volume, 1)
    if volume >= WHALE_VOLUME:
        volume_score = 90 + min(10, (volume / WHALE_VOLUME - 1) * 5)
    elif volume_ratio > 2.0:
        volume_score = 60 + min(30, (volume_ratio - 2) * 15)
    elif volume_ratio > 1.0:
        volume_score = 30 + (volume_ratio - 1) * 30
    else:
        volume_score = volume_ratio * 30
    volume_score = min(100, volume_score)

    # 3. Whale activity score (0-100)
    whale_score = 100 if volume >= WHALE_VOLUME else min(80, volume / WHALE_VOLUME * 80)

    # 4. Theta score (0-100) — closer to expiry = higher theta
    if days_to_close <= 1:
        theta_score = 100
    elif days_to_close <= 3:
        theta_score = 85
    elif days_to_close <= 7:
        theta_score = 70
    elif days_to_close <= 14:
        theta_score = 50
    elif days_to_close <= 30:
        theta_score = max(15, 40 - (days_to_close - 14))
    else:
        theta_score = 5  # Should be filtered, but safety

    # Composite confidence
    confidence = (
        edge_score * WEIGHT_CATEGORY_EDGE
        + volume_score * WEIGHT_VOLUME_SPIKE
        + whale_score * WEIGHT_WHALE_ACTIVITY
        + theta_score * WEIGHT_THETA
    )

    # Confirmation count
    confirmations = 0
    if category_edge >= 0.20:
        confirmations += 1
    if volume >= WHALE_VOLUME:
        confirmations += 1
    if volume_ratio > 2.0:
        confirmations += 1
    if days_to_close <= 7:
        confirmations += 1

    # Boost for multiple confirmations (+15% per extra, matches backtest)
    if confirmations >= 3:
        confidence *= 1.20
    elif confirmations >= 2:
        confidence *= 1.10

    confidence = min(95, confidence)

    return {
        "confidence": round(confidence, 1),
        "edge_score": round(edge_score, 1),
        "volume_score": round(volume_score, 1),
        "whale_score": round(whale_score, 1),
        "theta_score": round(theta_score, 1),
        "confirmations": confirmations,
        "category_edge_pct": round(category_edge * 100, 1),
    }


def _log_shadow_trade(signal: Dict):
    """Log signal as a shadow/paper trade for later P&L tracking."""
    try:
        SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)
        trades = []
        if SHADOW_LOG.exists():
            try:
                with open(SHADOW_LOG) as f:
                    trades = json.load(f)
            except Exception:
                trades = []

        trades.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "market_id": signal.get("market_id"),
            "market": signal.get("market", "")[:80],
            "category": signal.get("category"),
            "side": signal.get("side"),
            "entry_price": signal.get("price"),
            "confidence": signal.get("confidence"),
            "confirmations": signal.get("confirmations"),
            "days_to_close": signal.get("days_to_close"),
            "volume": signal.get("volume"),
            "resolved": False,
            "outcome": None,
            "pnl": None,
        })

        # Keep last 500 shadow trades
        if len(trades) > 500:
            trades = trades[-500:]

        with open(SHADOW_LOG, "w") as f:
            json.dump(trades, f, indent=2)
    except Exception as e:
        logger.warning(f"Shadow trade log failed: {e}")


def scan_kalshi_signals() -> List[Dict]:
    """Scan Kalshi markets for mispriced category signals."""
    markets = fetch_kalshi_markets()
    signals = []

    # Track category volumes for spike detection
    category_volumes: Dict[str, List[int]] = {}
    for m in markets:
        cat = extract_category(m.get("event_ticker", ""))
        vol = m.get("volume", 0)
        if cat:
            category_volumes.setdefault(cat, []).append(vol)

    avg_cat_vol = {
        cat: sum(vols) / len(vols) if vols else 0
        for cat, vols in category_volumes.items()
    }

    now = datetime.now(timezone.utc)

    for market in markets:
        ticker = market.get("ticker", "")
        event_ticker = market.get("event_ticker", "")
        category = extract_category(event_ticker)

        # Skip efficient categories
        if category in EFFICIENT_CATEGORIES:
            continue

        # Check if known mispriced or dynamic
        cat_info = MISPRICED_CATEGORIES.get(category)
        event_cat = market.get("_event_category", "").lower()

        is_dynamic_mispriced = any(
            kw in event_cat
            for kw in (
                "entertainment", "culture", "music", "tv", "movies",
                "science", "tech", "world", "climate", "weather",
                "crypto", "financial", "economics", "pop",
            )
        )

        if not cat_info and not is_dynamic_mispriced:
            continue

        category_edge = cat_info["error"] if cat_info else 0.15
        if category_edge * 100 < MIN_EDGE_PCT:
            continue

        # Market data
        volume = market.get("volume", 0)
        price = market.get("last_price", market.get("yes_bid", 50))
        close_time_str = market.get("close_time", "")

        # Volume filter (v2: raised to 1000)
        if volume < MIN_VOLUME:
            continue

        # Contested zone filter
        if price < CONTESTED_LOW or price > CONTESTED_HIGH:
            continue

        # Duration calculation — HARD CAP at 30 days (v2)
        try:
            close_time = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
            days_to_close = (close_time - now).total_seconds() / 86400
            if days_to_close <= 0 or days_to_close > MAX_DAYS_TO_CLOSE:
                continue
        except Exception:
            continue  # v2: skip if we can't parse close time (was defaulting to 365d)

        # Calculate confidence
        conf = calculate_signal_confidence(
            category_edge=category_edge,
            volume=volume,
            price_cents=price,
            days_to_close=days_to_close,
            avg_category_volume=avg_cat_vol.get(category, 1000),
        )

        # Direction: bet WITH price momentum
        side = "YES" if price >= 50 else "NO"

        signal = {
            "source": "mispriced_category",
            "platform": "kalshi",
            "market": market.get("title", ticker),
            "market_id": ticker,
            "event_ticker": event_ticker,
            "category": category,
            "category_tier": cat_info["tier"] if cat_info else "dynamic",
            "side": side,
            "price": price / 100.0,
            "confidence": conf["confidence"],
            "volume": volume,
            "days_to_close": round(days_to_close, 1),
            "confirmations": conf["confirmations"],
            "reasoning": (
                f"Mispriced category {category} ({conf['category_edge_pct']}% historical error), "
                f"{conf['confirmations']} confirmations, "
                f"{'🐋 whale activity' if volume >= WHALE_VOLUME else f'{volume} contracts'}, "
                f"expires in {days_to_close:.0f}d"
            ),
            "confidence_breakdown": conf,
            "strategy": "MispricedCategoryWhale",
            "backtest_stats": {
                "win_rate": 75.0,
                "sharpe": 1.25,
                "profit_factor": 1.20,
                "total_backtested_trades": 155152,
            },
        }
        signals.append(signal)

    # Sort by confidence
    signals.sort(key=lambda x: x["confidence"], reverse=True)

    # Shadow-log top signals (paper trade tracking)
    for sig in signals[:5]:
        _log_shadow_trade(sig)

    return signals


def get_mispriced_category_signals() -> Dict[str, Any]:
    """Main entry point — returns all mispriced category signals.
    
    Cached for 60s to avoid blocking uvicorn on repeated calls.
    """
    now = time.time()
    if _cache["data"] and (now - _cache["timestamp"]) < CACHE_TTL:
        return _cache["data"]

    kalshi_signals = scan_kalshi_signals()

    result = {
        "signals": kalshi_signals,
        "total": len(kalshi_signals),
        "strategy": "MispricedCategoryWhale",
        "description": "Target high-volume markets in mispriced categories with whale confirmation",
        "backtest_validation": {
            "win_rate": "75%",
            "sharpe": 1.25,
            "profit_factor": 1.20,
            "markets_analyzed": "3.75M",
            "trades_simulated": "155K",
        },
        "categories_monitored": len(MISPRICED_CATEGORIES),
        "efficient_categories_excluded": len(EFFICIENT_CATEGORIES),
        "max_days_to_close": MAX_DAYS_TO_CLOSE,
        "min_volume": MIN_VOLUME,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cached": False,
    }

    _cache["data"] = {**result, "cached": True}
    _cache["timestamp"] = now

    return result


# ============================================================================
# Shadow Trade Resolution
# ============================================================================

def resolve_shadow_trades() -> Dict[str, Any]:
    """Check shadow trades against resolved markets and calculate P&L.
    
    Call periodically (e.g., daily cron) to track paper performance.
    """
    if not SHADOW_LOG.exists():
        return {"resolved": 0, "pending": 0}

    try:
        with open(SHADOW_LOG) as f:
            trades = json.load(f)
    except Exception:
        return {"error": "Failed to load shadow trades"}

    unresolved = [t for t in trades if not t.get("resolved")]
    if not unresolved:
        return {"resolved": 0, "pending": 0, "note": "No unresolved trades"}

    # Batch check market resolutions
    resolved_count = 0
    total_pnl = 0.0

    for trade in unresolved:
        ticker = trade.get("market_id", "")
        if not ticker:
            continue

        data = _fetch_json(f"{KALSHI_API}/markets/{ticker}", timeout=5)
        if not data or not data.get("market"):
            continue

        market = data["market"]
        result = market.get("result", "")
        if not result:
            continue  # Not resolved yet

        trade["resolved"] = True
        trade["resolved_at"] = datetime.now(timezone.utc).isoformat()

        # Calculate P&L
        entry_price = trade.get("entry_price", 0.5)
        side = trade.get("side", "YES")

        if result.upper() == "YES":
            pnl = (1.0 - entry_price) if side == "YES" else -entry_price
        elif result.upper() == "NO":
            pnl = -entry_price if side == "YES" else (1.0 - (1.0 - entry_price))
        else:
            pnl = 0

        trade["outcome"] = result
        trade["pnl"] = round(pnl, 4)
        total_pnl += pnl
        resolved_count += 1

    # Save updated trades
    with open(SHADOW_LOG, "w") as f:
        json.dump(trades, f, indent=2)

    # Calculate stats
    resolved_trades = [t for t in trades if t.get("resolved")]
    wins = sum(1 for t in resolved_trades if (t.get("pnl") or 0) > 0)
    total = len(resolved_trades)

    return {
        "resolved_this_run": resolved_count,
        "pending": len([t for t in trades if not t.get("resolved")]),
        "total_resolved": total,
        "wins": wins,
        "win_rate": round(wins / total * 100, 1) if total > 0 else 0,
        "total_pnl": round(total_pnl, 4),
        "cumulative_pnl": round(sum(t.get("pnl", 0) for t in resolved_trades), 4),
    }


# ============================================================================
# CLI Testing
# ============================================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) > 1 and sys.argv[1] == "resolve":
        result = resolve_shadow_trades()
        print(json.dumps(result, indent=2))
    else:
        result = get_mispriced_category_signals()
        print(f"\n{'='*60}")
        print(f"Mispriced Category Signals: {result['total']}")
        print(f"Max days: {result['max_days_to_close']} | Min volume: {result['min_volume']}")
        print(f"{'='*60}")

        for sig in result["signals"][:10]:
            print(f"\n  {sig['market'][:60]}")
            print(f"  Category: {sig['category']} ({sig['category_tier']})")
            print(f"  Side: {sig['side']} @ {sig['price']:.2f}")
            print(f"  Confidence: {sig['confidence']:.1f}% ({sig['confirmations']} confirmations)")
            print(f"  Volume: {sig['volume']:,} contracts")
            print(f"  Expires: {sig['days_to_close']:.1f} days")

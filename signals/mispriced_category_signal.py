#!/usr/bin/env python3
"""
Mispriced Category + Whale Confirmation Signal Source (v3)

Live signal generator based on backtest results:
- 75% win rate across 155K historical trades
- 1.25 Sharpe ratio
- Targets high-volume markets in mispriced categories
- Uses volume spikes and whale activity as confirmation

v3 improvements:
- Polymarket scanning (entertainment, crypto, politics — short-dated)
- Paginated Kalshi scan (3 pages × 30 events = 90 events)
- Cross-platform matching for arb confirmation bonus
- SQLite shadow tracker integration
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

# Kalshi categories with historically high pricing error (>15%)
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

# Well-calibrated categories to NEVER trade
EFFICIENT_CATEGORIES = {
    'KXPGATOUR', 'KXMLB', 'KXNBA', 'KXNHL', 'KXNFL',
    'KXAOWOMEN', 'KXFIRSTSUPERBOWLSONG',
}

# Polymarket mispriced topic tags (from backtest: entertainment/novelty avg 15%+ error)
POLYMARKET_MISPRICED_TAGS = {
    'entertainment', 'music', 'crypto', 'pop-culture', 'technology',
    'science', 'weather', 'climate', 'economics', 'culture',
    'tv', 'movies', 'celebrities', 'awards', 'streaming',
    'ai', 'artificial-intelligence', 'space',
}

# Polymarket efficient tags (sports well-calibrated)
POLYMARKET_EFFICIENT_TAGS = {
    'nfl', 'nba', 'mlb', 'nhl', 'soccer', 'tennis', 'golf',
    'formula-1', 'mma', 'boxing', 'cricket',
}

# Thresholds
MIN_VOLUME_KALSHI = 5000        # Contracts
MIN_VOLUME_POLYMARKET = 50000   # Dollars (Polymarket volume is in USD)
WHALE_VOLUME_KALSHI = 10000     # Contracts
WHALE_VOLUME_POLYMARKET = 100000 # Dollars
CONTESTED_LOW = 15              # Cents/pct
CONTESTED_HIGH = 85
MAX_DAYS_TO_CLOSE = 30
MIN_EDGE_PCT = 5

# Confidence scoring weights
WEIGHT_CATEGORY_EDGE = 0.35
WEIGHT_VOLUME_SPIKE = 0.25
WEIGHT_WHALE_ACTIVITY = 0.20
WEIGHT_THETA = 0.20

# Category → crypto symbol mapping for velocity modifier (Strategy 1)
CATEGORY_CRYPTO_MAP = {
    # Polymarket tier names
    "tech": "BTCUSDT",
    "dynamic": "BTCUSDT",
    # Kalshi category prefixes
    "KXCRYPTO": "BTCUSDT",
    "KXETF": "BTCUSDT",
    "KXSTONKS": "BTCUSDT",
    # Crypto keyword matches
    "bitcoin": "BTCUSDT",
    "ethereum": "ETHUSDT",
    "solana": "SOLUSDT",
    "crypto": "BTCUSDT",
}

# Cache
_cache = {"data": None, "timestamp": 0}
CACHE_TTL = 60  # seconds

# Shadow trade log (legacy fallback)
SHADOW_LOG = Path(__file__).parent.parent / "storage" / "shadow_trades.json"

# ============================================================================
# API Access
# ============================================================================

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_API = "https://gamma-api.polymarket.com"


def _fetch_json(url: str, timeout: int = 12) -> Any:
    """Fetch JSON with timeout and error handling."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.warning(f"Fetch failed {url[:80]}: {e}")
        return None


def fetch_kalshi_markets(pages: int = 3, per_page: int = 30, status: str = "open") -> List[Dict]:
    """Fetch active markets from Kalshi with pagination."""
    all_markets = []
    seen_tickers = set()
    cursor = None

    # 1. Events endpoint (paginated, has category info)
    for page in range(pages):
        url = f"{KALSHI_API}/events?limit={per_page}&status={status}&with_nested_markets=true"
        if cursor:
            url += f"&cursor={cursor}"

        data = _fetch_json(url, timeout=12)
        if not data:
            break

        events = data.get("events", [])
        if not events:
            break

        for event in events:
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

        cursor = data.get("cursor")
        if not cursor:
            break

    # 2. Direct markets endpoint (broader coverage)
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

    logger.info(f"Kalshi: fetched {len(all_markets)} markets from {pages} pages")
    return all_markets


def fetch_polymarket_markets(limit: int = 100) -> List[Dict]:
    """Fetch active markets from Polymarket Gamma API."""
    all_markets = []

    # Fetch by volume (most liquid first)
    data = _fetch_json(
        f"{GAMMA_API}/markets?closed=false&limit={limit}&order=volume24hr&ascending=false",
        timeout=12,
    )
    if data and isinstance(data, list):
        all_markets.extend(data)

    # Also fetch by end date (soonest expiry — best theta)
    data2 = _fetch_json(
        f"{GAMMA_API}/markets?closed=false&limit=50&order=endDate&ascending=true",
        timeout=12,
    )
    if data2 and isinstance(data2, list):
        seen = {m.get("id") for m in all_markets}
        for m in data2:
            if m.get("id") not in seen:
                all_markets.append(m)

    logger.info(f"Polymarket: fetched {len(all_markets)} markets")
    return all_markets


# ============================================================================
# Signal Generation
# ============================================================================

def extract_category(event_ticker: str) -> str:
    """Extract category prefix from Kalshi event ticker."""
    if not event_ticker:
        return ""
    return event_ticker.split('-')[0] if '-' in event_ticker else event_ticker


def calculate_signal_confidence(
    category_edge: float,
    volume: int,
    price_cents: int,
    days_to_close: float,
    avg_category_volume: float = 1000,
    whale_threshold: int = 10000,
    category: str = "",
) -> Dict[str, Any]:
    """Calculate composite confidence score for a market signal."""
    # 1. Category edge score (0-100)
    edge_score = min(100, (category_edge / 0.60) * 100)

    # 2. Volume spike score (0-100)
    volume_ratio = volume / max(avg_category_volume, 1)
    if volume >= whale_threshold:
        volume_score = 90 + min(10, (volume / whale_threshold - 1) * 5)
    elif volume_ratio > 2.0:
        volume_score = 60 + min(30, (volume_ratio - 2) * 15)
    elif volume_ratio > 1.0:
        volume_score = 30 + (volume_ratio - 1) * 30
    else:
        volume_score = volume_ratio * 30
    volume_score = min(100, volume_score)

    # 3. Whale activity score (0-100)
    whale_score = 100 if volume >= whale_threshold else min(80, volume / whale_threshold * 80)

    # 4. Theta score (0-100)
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
        theta_score = 5

    # Composite
    confidence = (
        edge_score * WEIGHT_CATEGORY_EDGE
        + volume_score * WEIGHT_VOLUME_SPIKE
        + whale_score * WEIGHT_WHALE_ACTIVITY
        + theta_score * WEIGHT_THETA
    )

    # Confirmations
    confirmations = 0
    if category_edge >= 0.20:
        confirmations += 1
    if volume >= whale_threshold:
        confirmations += 1
    if volume_ratio > 2.0:
        confirmations += 1
    if days_to_close <= 7:
        confirmations += 1

    if confirmations >= 3:
        confidence *= 1.20
    elif confirmations >= 2:
        confidence *= 1.10

    # Strategy 1: Score velocity modifier for crypto-related categories
    velocity_data = {"multiplier": 1.0, "applied": False}
    crypto_symbol = CATEGORY_CRYPTO_MAP.get(category.lower() if category else "")
    if crypto_symbol:
        try:
            from alpha_score_tracker import score_velocity_modifier
            vel = score_velocity_modifier(crypto_symbol, hours=2)
            if vel["multiplier"] != 1.0:
                confidence *= vel["multiplier"]
                velocity_data = {
                    "multiplier": vel["multiplier"],
                    "delta": vel.get("delta"),
                    "symbol": crypto_symbol,
                    "applied": True,
                }
        except Exception:
            pass  # Graceful degradation if alpha tracker unavailable

    confidence = min(95, confidence)

    return {
        "confidence": round(confidence, 1),
        "edge_score": round(edge_score, 1),
        "volume_score": round(volume_score, 1),
        "whale_score": round(whale_score, 1),
        "theta_score": round(theta_score, 1),
        "confirmations": confirmations,
        "category_edge_pct": round(category_edge * 100, 1),
        "velocity_modifier": velocity_data,
    }


def _is_mispriced_polymarket(market: Dict) -> tuple:
    """Check if a Polymarket market is in a mispriced category.
    
    Returns (is_mispriced: bool, edge: float, tier: str)
    """
    tags = set()
    # Extract tags from market data
    for tag_field in ("tags", "categories", "markets_tags"):
        val = market.get(tag_field, [])
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except Exception:
                val = [val]
        if isinstance(val, list):
            for t in val:
                if isinstance(t, str):
                    tags.add(t.lower().strip())
                elif isinstance(t, dict):
                    tags.add(t.get("label", "").lower().strip())

    # Also check slug and question for keywords
    slug = market.get("slug", "").lower()
    question = market.get("question", "").lower()

    # Check if any efficient tags match → skip
    if tags & POLYMARKET_EFFICIENT_TAGS:
        return False, 0, ""

    # Check for sport keywords in question
    sport_keywords = {"nfl", "nba", "mlb", "nhl", "premier league", "champions league", "tennis", "golf"}
    if any(kw in question for kw in sport_keywords):
        return False, 0, ""

    # Check mispriced tags
    matching_tags = tags & POLYMARKET_MISPRICED_TAGS
    if matching_tags:
        # Higher edge for entertainment/novelty
        if matching_tags & {"entertainment", "music", "pop-culture", "celebrities", "awards", "streaming"}:
            return True, 0.25, "entertainment"
        elif matching_tags & {"crypto", "ai", "artificial-intelligence", "technology"}:
            return True, 0.18, "tech"
        elif matching_tags & {"weather", "climate", "science", "space"}:
            return True, 0.22, "science"
        else:
            return True, 0.15, "dynamic"

    # Keyword fallback for untagged markets
    entertainment_kw = {"oscar", "grammy", "emmy", "spotify", "netflix", "movie", "album",
                        "award", "celebrity", "reality tv", "bachelor", "idol"}
    tech_kw = {"bitcoin", "ethereum", "crypto", "ai ", "openai", "google", "apple",
               "tesla", "spacex", "launch", "ipo"}
    weather_kw = {"temperature", "hurricane", "tornado", "earthquake", "flood", "snow",
                  "rainfall", "wildfire"}

    if any(kw in question for kw in entertainment_kw):
        return True, 0.25, "entertainment"
    elif any(kw in question for kw in tech_kw):
        return True, 0.18, "tech"
    elif any(kw in question for kw in weather_kw):
        return True, 0.22, "science"

    return False, 0, ""


def _log_shadow_trade(signal: Dict):
    """Log signal as a shadow/paper trade via SQLite tracker."""
    try:
        from shadow_tracker import log_shadow_trade
        log_shadow_trade(signal)
    except ImportError:
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
            if len(trades) > 500:
                trades = trades[-500:]
            with open(SHADOW_LOG, "w") as f:
                json.dump(trades, f, indent=2)
        except Exception as e:
            logger.warning(f"Shadow trade log failed: {e}")
    except Exception as e:
        logger.warning(f"Shadow tracker log failed: {e}")


# ============================================================================
# Kalshi Scanner
# ============================================================================

def scan_kalshi_signals() -> List[Dict]:
    """Scan Kalshi markets for mispriced category signals."""
    markets = fetch_kalshi_markets(pages=3, per_page=30)
    signals = []

    # Category volume averages
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

        if category in EFFICIENT_CATEGORIES:
            continue

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

        volume = market.get("volume", 0)
        price = market.get("last_price", market.get("yes_bid", 50))
        close_time_str = market.get("close_time", "")

        if volume < MIN_VOLUME_KALSHI:
            continue
        if price < CONTESTED_LOW or price > CONTESTED_HIGH:
            continue

        try:
            close_time = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
            days_to_close = (close_time - now).total_seconds() / 86400
            if days_to_close <= 0 or days_to_close > MAX_DAYS_TO_CLOSE:
                continue
        except Exception:
            continue

        conf = calculate_signal_confidence(
            category_edge=category_edge,
            volume=volume,
            price_cents=price,
            days_to_close=days_to_close,
            avg_category_volume=avg_cat_vol.get(category, 1000),
            whale_threshold=WHALE_VOLUME_KALSHI,
            category=category,
        )

        side = "YES" if price >= 50 else "NO"

        signals.append({
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
                f"[Kalshi] Mispriced {category} ({conf['category_edge_pct']}% error), "
                f"{conf['confirmations']} confirms, "
                f"{'🐋 whale' if volume >= WHALE_VOLUME_KALSHI else f'{volume} contracts'}, "
                f"{days_to_close:.0f}d"
            ),
            "confidence_breakdown": conf,
            "strategy": "MispricedCategoryWhale",
            "backtest_stats": {
                "win_rate": 75.0,
                "sharpe": 1.25,
                "profit_factor": 1.20,
                "total_backtested_trades": 155152,
            },
        })

    return signals


# ============================================================================
# Polymarket Scanner
# ============================================================================

def scan_polymarket_signals() -> List[Dict]:
    """Scan Polymarket for mispriced category signals."""
    markets = fetch_polymarket_markets(limit=100)
    signals = []
    now = datetime.now(timezone.utc)

    # Volume averages for spike detection
    volumes = [float(m.get("volume24hr", 0) or 0) for m in markets if float(m.get("volume24hr", 0) or 0) > 0]
    avg_volume = sum(volumes) / len(volumes) if volumes else 10000

    for market in markets:
        is_mispriced, edge, tier = _is_mispriced_polymarket(market)
        if not is_mispriced:
            continue

        if edge * 100 < MIN_EDGE_PCT:
            continue

        # Volume (Polymarket volume is in USD)
        volume_24h = float(market.get("volume24hr", 0) or 0)
        total_volume = float(market.get("volume", 0) or 0)
        volume = max(volume_24h, total_volume)

        if volume < MIN_VOLUME_POLYMARKET:
            continue

        # Price
        yes_price = 0.5
        outcome_prices = market.get("outcomePrices")
        if outcome_prices:
            try:
                if isinstance(outcome_prices, str):
                    outcome_prices = json.loads(outcome_prices)
                yes_price = float(outcome_prices[0])
            except Exception:
                pass

        price_cents = int(yes_price * 100)
        if price_cents < CONTESTED_LOW or price_cents > CONTESTED_HIGH:
            continue

        # Duration
        end_date_str = market.get("endDate", "")
        if not end_date_str:
            continue

        try:
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            days_to_close = (end_date - now).total_seconds() / 86400
            if days_to_close <= 0 or days_to_close > MAX_DAYS_TO_CLOSE:
                continue
        except Exception:
            continue

        conf = calculate_signal_confidence(
            category_edge=edge,
            volume=int(volume),
            price_cents=price_cents,
            days_to_close=days_to_close,
            avg_category_volume=avg_volume,
            whale_threshold=WHALE_VOLUME_POLYMARKET,
            category=tier,
        )

        side = "YES" if price_cents >= 50 else "NO"
        market_id = market.get("conditionId", market.get("id", ""))
        slug = market.get("slug", "")

        signals.append({
            "source": "mispriced_category",
            "platform": "polymarket",
            "market": market.get("question", "")[:200],
            "market_id": market_id,
            "slug": slug,
            "category": tier,
            "category_tier": tier,
            "side": side,
            "price": yes_price,
            "confidence": conf["confidence"],
            "volume": int(volume),
            "volume_24h": int(volume_24h),
            "days_to_close": round(days_to_close, 1),
            "confirmations": conf["confirmations"],
            "reasoning": (
                f"[Polymarket] Mispriced {tier} ({conf['category_edge_pct']}% error), "
                f"{conf['confirmations']} confirms, "
                f"{'🐋 whale' if volume >= WHALE_VOLUME_POLYMARKET else f'${volume:,.0f} vol'}, "
                f"{days_to_close:.0f}d"
            ),
            "confidence_breakdown": conf,
            "strategy": "MispricedCategoryWhale",
            "url": f"https://polymarket.com/event/{slug}" if slug else None,
            "backtest_stats": {
                "win_rate": 75.0,
                "sharpe": 1.25,
                "profit_factor": 1.20,
                "total_backtested_trades": 155152,
            },
        })

    return signals


# ============================================================================
# Cross-Platform Matching
# ============================================================================

def find_cross_platform_matches(kalshi_signals: List[Dict], poly_signals: List[Dict]) -> List[Dict]:
    """Find markets that appear on both platforms — arb confirmation bonus.
    
    If the same market appears on both platforms with similar pricing,
    it's LESS likely to be mispriced (efficient). If prices diverge,
    it's a stronger signal.
    """
    # Simple title keyword matching (not perfect but catches obvious ones)
    matches = []
    for ks in kalshi_signals:
        k_words = set(ks.get("market", "").lower().split()[:6])
        if len(k_words) < 3:
            continue
        for ps in poly_signals:
            p_words = set(ps.get("market", "").lower().split()[:6])
            overlap = k_words & p_words
            if len(overlap) >= 3:
                price_diff = abs(ks.get("price", 0.5) - ps.get("price", 0.5))
                if price_diff > 0.05:  # >5% price divergence = arb signal
                    # Boost both signals
                    ks["confidence"] = min(95, ks["confidence"] * 1.15)
                    ps["confidence"] = min(95, ps["confidence"] * 1.15)
                    ks["confirmations"] = ks.get("confirmations", 0) + 1
                    ps["confirmations"] = ps.get("confirmations", 0) + 1
                    ks["reasoning"] += f" | ⚡ Cross-platform divergence: {price_diff:.0%}"
                    ps["reasoning"] += f" | ⚡ Cross-platform divergence: {price_diff:.0%}"
                    matches.append({
                        "kalshi": ks.get("market_id"),
                        "polymarket": ps.get("market_id"),
                        "price_divergence": round(price_diff, 3),
                    })

    return matches


# ============================================================================
# Main Entry Points
# ============================================================================

def get_mispriced_category_signals() -> Dict[str, Any]:
    """Main entry point — returns all mispriced category signals from both platforms.
    
    Cached for 60s to avoid blocking uvicorn on repeated calls.
    """
    now = time.time()
    if _cache["data"] and (now - _cache["timestamp"]) < CACHE_TTL:
        return _cache["data"]

    # Scan both platforms
    kalshi_signals = scan_kalshi_signals()
    poly_signals = scan_polymarket_signals()

    # Cross-platform matching
    cross_matches = find_cross_platform_matches(kalshi_signals, poly_signals)

    # Merge and sort
    all_signals = kalshi_signals + poly_signals
    all_signals.sort(key=lambda x: x["confidence"], reverse=True)

    # Shadow-log top signals
    for sig in all_signals[:10]:
        _log_shadow_trade(sig)

    # Save full signal snapshot
    try:
        from shadow_tracker import save_signal_snapshot
        save_signal_snapshot(all_signals, "mispriced_category")
    except Exception:
        pass

    result = {
        "signals": all_signals,
        "total": len(all_signals),
        "kalshi_signals": len(kalshi_signals),
        "polymarket_signals": len(poly_signals),
        "cross_platform_matches": len(cross_matches),
        "cross_matches": cross_matches,
        "strategy": "MispricedCategoryWhale",
        "description": "Target high-volume markets in mispriced categories with whale confirmation (Kalshi + Polymarket)",
        "backtest_validation": {
            "win_rate": "75%",
            "sharpe": 1.25,
            "profit_factor": 1.20,
            "markets_analyzed": "3.75M",
            "trades_simulated": "155K",
        },
        "categories_monitored": len(MISPRICED_CATEGORIES),
        "polymarket_tags_monitored": len(POLYMARKET_MISPRICED_TAGS),
        "efficient_categories_excluded": len(EFFICIENT_CATEGORIES) + len(POLYMARKET_EFFICIENT_TAGS),
        "max_days_to_close": MAX_DAYS_TO_CLOSE,
        "min_volume_kalshi": MIN_VOLUME_KALSHI,
        "min_volume_polymarket": MIN_VOLUME_POLYMARKET,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cached": False,
    }

    _cache["data"] = {**result, "cached": True}
    _cache["timestamp"] = now

    return result


# ============================================================================
# CLI Testing
# ============================================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    result = get_mispriced_category_signals()
    print(f"\n{'='*60}")
    print(f"Mispriced Category Signals: {result['total']}")
    print(f"  Kalshi: {result['kalshi_signals']} | Polymarket: {result['polymarket_signals']}")
    print(f"  Cross-platform matches: {result['cross_platform_matches']}")
    print(f"  Max days: {result['max_days_to_close']} | Vol floor: Kalshi {result['min_volume_kalshi']} / Poly ${result['min_volume_polymarket']:,}")
    print(f"{'='*60}")

    for sig in result["signals"][:15]:
        platform = sig.get("platform", "?")
        print(f"\n  [{platform.upper()[:1]}] {sig['market'][:60]}")
        print(f"  Category: {sig.get('category', '?')} ({sig.get('category_tier', '?')})")
        print(f"  Side: {sig['side']} @ {sig['price']:.2f}")
        print(f"  Confidence: {sig['confidence']:.1f}% ({sig['confirmations']} confirmations)")
        vol_fmt = f"${sig['volume']:,}" if platform == "polymarket" else f"{sig['volume']:,} contracts"
        print(f"  Volume: {vol_fmt}")
        print(f"  Expires: {sig['days_to_close']:.1f} days")

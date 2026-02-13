#!/usr/bin/env python3
"""
Resolution Certainty Scanner

Checks if prediction market outcomes are already knowable from real-time data.
When outcome is ~certain but market hasn't fully priced it → high-confidence signal.

Data Sources (all localhost, zero latency):
- Virtuoso Dashboard API (localhost:8002) — crypto prices + multi-factor scores
- Arena Leaderboard scraper — AI model rankings
- OpenWeatherMap — weather actuals (free tier)

Scan every 5 minutes via watchdog Tier 1.
"""

import json
import logging
import math
import re
import sqlite3
import urllib.request
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

logger = logging.getLogger(__name__)

# ============================================================================
# Configuration
# ============================================================================

VIRTUOSO_API = "http://127.0.0.1:8002/api/dashboard"
POLYCLAWD_API = "http://127.0.0.1:8420/api"
WEATHER_API_KEY = ""  # Set if available, otherwise skip weather markets

STORAGE_DIR = Path(__file__).parent.parent / "storage"
DB_PATH = STORAGE_DIR / "shadow_trades.db"

# Minimum edge to generate a signal
MIN_EDGE = 0.05  # 5%
# Minimum certainty to act
MIN_CERTAINTY = 0.70  # 70% sure — scanner flags opportunities, portfolio filters further

# Crypto symbol mapping: market keywords → Virtuoso symbols
CRYPTO_MAP = {
    "bitcoin": "BTCUSDT", "btc": "BTCUSDT",
    "ethereum": "ETHUSDT", "eth": "ETHUSDT",
    "solana": "SOLUSDT", "sol": "SOLUSDT",
    "dogecoin": "DOGEUSDT", "doge": "DOGEUSDT",
    "xrp": "XRPUSDT", "ripple": "XRPUSDT",
    "sui": "SUIUSDT",
    "aave": "AAVEUSDT",
}

# Company mapping for AI model markets
AI_COMPANY_KEYWORDS = {
    "anthropic": "Anthropic", "claude": "Anthropic",
    "google": "Google", "gemini": "Google", "deepmind": "Google",
    "openai": "OpenAI", "gpt": "OpenAI", "chatgpt": "OpenAI",
    "xai": "xAI", "grok": "xAI",
    "meta": "Meta", "llama": "Meta",
    "deepseek": "DeepSeek",
    "mistral": "Mistral",
}


# ============================================================================
# Data Fetchers
# ============================================================================

def _fetch_json(url: str, timeout: int = 10) -> Optional[Dict]:
    """Fetch JSON from URL."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.warning(f"Fetch failed {url}: {e}")
        return None


def get_crypto_prices() -> Dict[str, Dict]:
    """Get current crypto prices from Virtuoso Dashboard API."""
    data = _fetch_json(f"{VIRTUOSO_API}/symbols")
    if not data:
        return {}

    prices = {}
    for s in data.get("symbols", []):
        symbol = s.get("symbol", "")
        prices[symbol] = {
            "price": s.get("price", 0),
            "change_24h": s.get("change_24h", 0),
            "score": s.get("score", 50),
            "sentiment": s.get("sentiment", "NEUTRAL"),
            "high_24h": s.get("high_24h", 0),
            "low_24h": s.get("low_24h", 0),
        }

    return prices


def get_arena_rankings() -> Dict[str, int]:
    """Get current Arena leaderboard rankings from our tracker."""
    data = _fetch_json(f"{POLYCLAWD_API}/signals/ai-models")
    if not data:
        return {}

    rankings = {}
    for co in data.get("company_rankings", []):
        rankings[co["company"]] = {
            "rank": co["best_rank"],
            "model": co.get("best_model", ""),
            "model_count": co.get("model_count", 0),
        }

    return rankings


# ============================================================================
# Market Parsers
# ============================================================================

def parse_crypto_price_market(title: str) -> Optional[Dict]:
    """
    Parse crypto price prediction markets.
    
    Examples:
    - "Will Bitcoin reach $75,000 in February?"
    - "BTC above $100K by March 2026?"
    - "Ethereum Up or Down on February 13?"
    - "Will ETH be above $3,000 on Feb 15?"
    """
    title_lower = title.lower()

    # Identify crypto asset
    asset = None
    virtuoso_symbol = None
    for keyword, symbol in CRYPTO_MAP.items():
        if keyword in title_lower:
            asset = keyword
            virtuoso_symbol = symbol
            break

    if not asset:
        return None

    result = {"type": "crypto_price", "asset": asset, "symbol": virtuoso_symbol}

    # Parse "Up or Down" markets
    if "up or down" in title_lower:
        # These resolve based on 24h price change direction
        result["subtype"] = "direction"
        # Parse the date
        date_match = re.search(r'(?:on|for)\s+(\w+\s+\d{1,2})', title_lower)
        if date_match:
            result["resolution_date"] = date_match.group(1)
        return result

    # Parse price threshold markets — look for $ followed by number
    price_match = re.search(r'\$([\d,]+(?:\.\d+)?)\s*(?:k|K)?', title)
    if price_match:
        price_str = price_match.group(1).replace(",", "")
        threshold = float(price_str)
        # Handle K suffix (e.g., "$75K")
        if re.search(r'\$[\d,]+[kK]', title):
            threshold *= 1000
        result["threshold"] = threshold
        result["subtype"] = "threshold"

        # Determine direction: "reach", "above", "hit" = needs to go UP
        if any(w in title_lower for w in ["reach", "above", "hit", "over", "exceed", "surpass"]):
            result["direction"] = "above"
        elif any(w in title_lower for w in ["below", "under", "drop", "fall"]):
            result["direction"] = "below"
        else:
            result["direction"] = "above"  # Default

        return result

    return result


def parse_ai_model_market(title: str) -> Optional[Dict]:
    """
    Parse AI model prediction markets.
    
    Examples:
    - "Will Google have the best AI model at the end of February 2026?"
    - "Which company will top Chatbot Arena?"
    - "Will Claude beat GPT-5?"
    """
    title_lower = title.lower()

    if not any(kw in title_lower for kw in [
        "ai model", "arena", "leaderboard", "best model", "#1 model",
        "chatbot arena", "lmsys", "lmarena"
    ]):
        # Check for company/model names
        if not any(kw in title_lower for kw in AI_COMPANY_KEYWORDS):
            return None

    result = {"type": "ai_model"}

    # Identify the company being asked about
    for keyword, company in AI_COMPANY_KEYWORDS.items():
        if keyword in title_lower:
            result["company"] = company
            break

    # Determine market type
    if any(w in title_lower for w in ["best", "#1", "top", "leading", "win"]):
        result["subtype"] = "leader"
    elif any(w in title_lower for w in ["beat", "vs", "versus", "better"]):
        result["subtype"] = "head_to_head"
    else:
        result["subtype"] = "leader"

    return result


# ============================================================================
# Certainty Calculators
# ============================================================================

def calculate_crypto_direction_certainty(
    market: Dict, crypto_data: Dict
) -> Optional[Dict]:
    """
    For "Up or Down" markets: check if we're late enough in the day
    that the direction is locked in.
    
    Example: "ETH Up or Down Feb 13" — if it's 10pm on Feb 13 and ETH
    is up 5%, it's very likely to resolve UP.
    """
    symbol = market.get("symbol")
    if not symbol or symbol not in crypto_data:
        return None

    price_data = crypto_data[symbol]
    change = price_data.get("change_24h", 0)

    # How certain are we about the direction?
    # Larger moves = more certain (unlikely to reverse in remaining hours)
    abs_change = abs(change)

    if abs_change > 10:
        certainty = 0.98
    elif abs_change > 5:
        certainty = 0.95
    elif abs_change > 3:
        certainty = 0.90
    elif abs_change > 1.5:
        certainty = 0.80
    elif abs_change > 0.5:
        certainty = 0.65
    else:
        certainty = 0.50  # Too close to call

    direction = "up" if change > 0 else "down"

    return {
        "certainty": certainty,
        "predicted_outcome": direction,
        "current_price": price_data["price"],
        "change_pct": change,
        "virtuoso_score": price_data.get("score", 50),
        "reasoning": (
            f"{symbol} is {'up' if change > 0 else 'down'} {abs_change:.1f}% in 24h. "
            f"Virtuoso score: {price_data.get('score', 'N/A')}. "
            f"{'High certainty — large move unlikely to reverse.' if certainty > 0.9 else 'Moderate certainty — could still flip.'}"
        ),
    }


def calculate_crypto_threshold_certainty(
    market: Dict, crypto_data: Dict
) -> Optional[Dict]:
    """
    For price threshold markets: compute probability based on
    current price vs threshold and time remaining.
    
    Uses simplified model: distance-to-threshold relative to
    historical daily volatility.
    """
    symbol = market.get("symbol")
    threshold = market.get("threshold")
    direction = market.get("direction", "above")

    if not symbol or not threshold or symbol not in crypto_data:
        return None

    price_data = crypto_data[symbol]
    current_price = price_data["price"]

    if current_price <= 0 or threshold <= 0:
        return None

    # Distance as percentage
    if direction == "above":
        distance_pct = (threshold - current_price) / current_price * 100
        # If already above threshold → certainty based on how far above
        if current_price >= threshold:
            buffer = (current_price - threshold) / threshold * 100
            certainty = min(0.98, 0.80 + buffer * 0.02)
            return {
                "certainty": certainty,
                "predicted_outcome": "yes",
                "current_price": current_price,
                "threshold": threshold,
                "distance_pct": -buffer,
                "reasoning": (
                    f"{symbol} at ${current_price:,.2f}, already above ${threshold:,.2f} "
                    f"by {buffer:.1f}%. Certainty: {certainty:.0%}."
                ),
            }
        else:
            # Below threshold — how likely to reach it?
            # Rough BTC daily volatility: ~3-5%
            daily_vol = 0.04  # 4% daily
            # Assume ~15 days remaining if not parsed
            days_remaining = 15

            # Standard deviations needed
            total_vol = daily_vol * math.sqrt(days_remaining) * 100
            z_score = distance_pct / total_vol if total_vol > 0 else 10

            # Convert to probability (simplified normal CDF)
            # P(reaching threshold) decreases with z-score
            if z_score < 0.5:
                prob_reach = 0.40
            elif z_score < 1.0:
                prob_reach = 0.25
            elif z_score < 1.5:
                prob_reach = 0.15
            elif z_score < 2.0:
                prob_reach = 0.08
            elif z_score < 3.0:
                prob_reach = 0.03
            else:
                prob_reach = 0.01

            certainty = 1.0 - prob_reach  # Certainty of NOT reaching

            return {
                "certainty": certainty,
                "predicted_outcome": "no",
                "current_price": current_price,
                "threshold": threshold,
                "distance_pct": distance_pct,
                "z_score": round(z_score, 2),
                "reasoning": (
                    f"{symbol} at ${current_price:,.2f}, needs +{distance_pct:.1f}% "
                    f"to reach ${threshold:,.2f}. Z-score: {z_score:.1f} "
                    f"({daily_vol*100:.0f}% daily vol × {days_remaining}d). "
                    f"P(reach) ≈ {prob_reach:.0%}, P(no) ≈ {certainty:.0%}."
                ),
            }

    return None


def calculate_ai_model_certainty(
    market: Dict, arena_rankings: Dict
) -> Optional[Dict]:
    """
    For AI model markets: check current Arena leaderboard.
    If the company asked about isn't #1 and there's a big gap, high certainty NO.
    """
    company = market.get("company")
    if not company or not arena_rankings:
        return None

    if company not in arena_rankings:
        # Company not even on Arena → very unlikely to be #1
        return {
            "certainty": 0.97,
            "predicted_outcome": "no",
            "reasoning": f"{company} not found on Arena leaderboard. Extremely unlikely to be #1.",
        }

    rank_data = arena_rankings[company]
    rank = rank_data["rank"]

    # Who's #1?
    leader = min(arena_rankings.items(), key=lambda x: x[1]["rank"])
    leader_name = leader[0]
    leader_rank = leader[1]["rank"]

    if rank == 1:
        # This company IS #1 → high certainty YES
        # Check gap to #2
        sorted_companies = sorted(arena_rankings.items(), key=lambda x: x[1]["rank"])
        if len(sorted_companies) > 1:
            runner_up = sorted_companies[1]
            gap = runner_up[1]["rank"] - rank
            certainty = 0.85 if gap >= 2 else 0.70
        else:
            certainty = 0.80

        return {
            "certainty": certainty,
            "predicted_outcome": "yes",
            "current_rank": rank,
            "leader": leader_name,
            "model": rank_data.get("model", ""),
            "reasoning": (
                f"{company} IS #{rank} on Arena ({rank_data.get('model', '')}). "
                f"Currently leading. Certainty: {certainty:.0%}."
            ),
        }
    else:
        # Not #1 — how far behind?
        if rank <= 3:
            certainty = 0.75  # Close, could still catch up
        elif rank <= 5:
            certainty = 0.88
        elif rank <= 10:
            certainty = 0.95
        else:
            certainty = 0.98

        return {
            "certainty": certainty,
            "predicted_outcome": "no",
            "current_rank": rank,
            "leader": leader_name,
            "leader_model": leader[1].get("model", ""),
            "model": rank_data.get("model", ""),
            "reasoning": (
                f"{company} is #{rank} on Arena ({rank_data.get('model', '')}). "
                f"Leader: {leader_name} #{leader_rank} ({leader[1].get('model', '')}). "
                f"Gap: {rank - leader_rank} positions. "
                f"P({company} overtakes) ≈ {(1-certainty):.0%}."
            ),
        }


# ============================================================================
# Main Scanner
# ============================================================================

def scan_open_markets() -> List[Dict]:
    """
    Get all open markets from shadow trades + paper positions
    and evaluate resolution certainty for each.
    """
    markets = []

    # Get open shadow trades
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM shadow_trades WHERE resolved = 0"
        ).fetchall()
        for r in rows:
            markets.append(dict(r))
        conn.close()
    except Exception as e:
        logger.warning(f"Failed to read shadow trades: {e}")

    # Get open paper positions
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM paper_positions WHERE status = 'open'"
        ).fetchall()
        for r in rows:
            markets.append(dict(r))
        conn.close()
    except Exception as e:
        logger.warning(f"Failed to read paper positions: {e}")

    return markets


def scan_polymarket_active() -> List[Dict]:
    """Fetch active Polymarket markets for resolution scanning."""
    data = _fetch_json(f"{POLYCLAWD_API}/signals/mispriced-category")
    if not data:
        return []
    return data.get("signals", [])


def generate_resolution_signals() -> Dict[str, Any]:
    """
    Main entry point: scan all markets and generate resolution certainty signals.
    
    Returns:
        {
            "timestamp": "...",
            "signals": [...],
            "markets_scanned": N,
            "certainty_signals": N,
            "data_sources": {...}
        }
    """
    now = datetime.now(timezone.utc).isoformat()
    signals = []

    # Fetch data sources
    crypto_data = get_crypto_prices()
    arena_rankings = get_arena_rankings()

    data_sources = {
        "crypto_symbols": len(crypto_data),
        "arena_companies": len(arena_rankings),
        "crypto_prices": {
            s: f"${d['price']:,.2f} ({d['change_24h']:+.1f}%)"
            for s, d in crypto_data.items()
            if s in ("BTCUSDT", "ETHUSDT", "SOLUSDT")
        },
        "arena_leader": min(arena_rankings.items(), key=lambda x: x[1]["rank"])[0] if arena_rankings else "N/A",
    }

    # Scan open positions/trades
    open_markets = scan_open_markets()

    # Also scan active Polymarket signals
    active_signals = scan_polymarket_active()

    # Combine and deduplicate
    all_markets = []
    seen_ids = set()

    for m in open_markets:
        mid = m.get("market_id", "")
        if mid and mid not in seen_ids:
            seen_ids.add(mid)
            all_markets.append({
                "market_id": mid,
                "title": m.get("market") or m.get("market_title", ""),
                "side": m.get("side", ""),
                "entry_price": m.get("entry_price", 0),
                "source": "position",
            })

    for s in active_signals:
        mid = s.get("market_id", "")
        if mid and mid not in seen_ids:
            seen_ids.add(mid)
            all_markets.append({
                "market_id": mid,
                "title": s.get("market", ""),
                "side": s.get("side", ""),
                "entry_price": s.get("price", 0),
                "source": "scan",
            })

    # Evaluate each market
    for market in all_markets:
        title = market.get("title", "")
        if not title:
            continue

        certainty_result = None

        # Try crypto price parsing
        parsed = parse_crypto_price_market(title)
        if parsed:
            if parsed.get("subtype") == "direction":
                certainty_result = calculate_crypto_direction_certainty(parsed, crypto_data)
            elif parsed.get("subtype") == "threshold":
                certainty_result = calculate_crypto_threshold_certainty(parsed, crypto_data)

        # Try AI model parsing
        if not certainty_result:
            parsed = parse_ai_model_market(title)
            if parsed:
                certainty_result = calculate_ai_model_certainty(parsed, arena_rankings)

        if not certainty_result:
            continue

        certainty = certainty_result.get("certainty", 0)
        predicted = certainty_result.get("predicted_outcome", "")

        if certainty < MIN_CERTAINTY:
            continue

        # Calculate edge vs market price
        entry_price = market.get("entry_price", 0)
        current_side = market.get("side", "").upper()

        # Determine signal
        if predicted.upper() in ("YES", "UP"):
            fair_yes = certainty
            fair_no = 1 - certainty
        else:
            fair_yes = 1 - certainty
            fair_no = certainty

        # What side should we be on?
        signal_side = "YES" if fair_yes > 0.5 else "NO"

        # Edge calculation
        if signal_side == "YES":
            market_price = entry_price if current_side == "YES" else (1 - entry_price)
            edge = fair_yes - market_price
        else:
            market_price = entry_price if current_side == "NO" else (1 - entry_price)
            edge = fair_no - market_price

        if edge < MIN_EDGE:
            continue

        signal = {
            "source": "resolution_certainty",
            "market_id": market["market_id"],
            "title": title,
            "signal_side": signal_side,
            "certainty": round(certainty, 3),
            "fair_value": round(fair_yes if signal_side == "YES" else fair_no, 3),
            "market_price": round(market_price, 3),
            "edge_pct": round(edge * 100, 1),
            "predicted_outcome": predicted,
            "reasoning": certainty_result.get("reasoning", ""),
            "existing_side": current_side,
            "existing_price": entry_price,
            "position_status": market.get("source", "scan"),
            "timestamp": now,
        }

        # Add extra data if available
        for key in ("current_price", "threshold", "distance_pct",
                     "z_score", "current_rank", "leader", "virtuoso_score",
                     "change_pct"):
            if key in certainty_result:
                signal[key] = certainty_result[key]

        signals.append(signal)

    # Sort by edge descending
    signals.sort(key=lambda x: x.get("edge_pct", 0), reverse=True)

    return {
        "timestamp": now,
        "signals": signals,
        "markets_scanned": len(all_markets),
        "certainty_signals": len(signals),
        "data_sources": data_sources,
    }


def get_resolution_summary() -> Dict[str, Any]:
    """Quick summary for API endpoint."""
    return generate_resolution_signals()


# ============================================================================
# CLI
# ============================================================================

def main():
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"

    if cmd == "scan":
        result = generate_resolution_signals()
        print(json.dumps(result, indent=2))
    elif cmd == "prices":
        prices = get_crypto_prices()
        for s, d in prices.items():
            print(f"{s:15s} ${d['price']:>12,.4f}  {d['change_24h']:+6.1f}%  score={d['score']:5.1f}")
    elif cmd == "arena":
        rankings = get_arena_rankings()
        for co, d in sorted(rankings.items(), key=lambda x: x[1]["rank"]):
            print(f"#{d['rank']:2d}  {co:12s}  {d['model']}")
    else:
        print(f"Usage: {sys.argv[0]} [scan|prices|arena]")


if __name__ == "__main__":
    main()

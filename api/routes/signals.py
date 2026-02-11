"""Signal aggregation, whale tracking, confidence scoring, and rotation endpoints.

This router consolidates all signal-related endpoints:
- /signals - Aggregated signals from all sources
- /signals/news - News signals (Google News + Reddit)
- /signals/auto-trade - Automated paper trading based on signals
- /volume/spikes - Volume spike detection
- /resolution/* - Markets approaching resolution
- /predictors - Whale accuracy tracking
- /inverse-whale - Inverse whale signals (fade losers)
- /smart-money - Smart money flow analysis
- /confidence/* - Bayesian confidence scoring
- /conflicts/* - Signal conflict analysis
- /rotations - Position rotation history
"""
import json
import logging
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()
logger = logging.getLogger(__name__)

# Constants
GAMMA_API = "https://gamma-api.polymarket.com"
POLYMARKET_DATA_API = "https://data-api.polymarket.com"
DATA_DIR = Path(__file__).parent.parent.parent / "data"
STORAGE_DIR = Path(__file__).parent.parent.parent / "storage"


# ============================================================================
# Helper Functions - Data Access
# ============================================================================

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


def _get_signals_path() -> str:
    """Get path to signals modules directory."""
    return str(Path(__file__).parent.parent.parent / "signals")


# Predictor stats file
PREDICTOR_STATS_FILE = DATA_DIR / "predictor_stats.json"
# Whale config file
WHALE_CONFIG_FILE = DATA_DIR / "whale_config.json"
# Source outcomes for Bayesian scoring
SOURCE_OUTCOMES_FILE = DATA_DIR / "source_outcomes.json"
# Conflict history
CONFLICT_HISTORY_FILE = DATA_DIR / "conflict_history.json"
# Paper trading files
TRADES_FILE = STORAGE_DIR / "trades.json"


# ============================================================================
# Core Helper Functions
# ============================================================================

def load_predictor_stats() -> dict:
    """Load whale predictor statistics."""
    PREDICTOR_STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    return _load_json(PREDICTOR_STATS_FILE, {"predictors": {}, "last_updated": None})


def save_predictor_stats(stats: dict):
    """Save whale predictor statistics."""
    _save_json(PREDICTOR_STATS_FILE, stats)


def load_whale_config() -> dict:
    """Load whale configuration."""
    return _load_json(WHALE_CONFIG_FILE, {"whales": []})


def load_source_outcomes() -> dict:
    """Load signal source win/loss tracking."""
    SOURCE_OUTCOMES_FILE.parent.mkdir(parents=True, exist_ok=True)
    if SOURCE_OUTCOMES_FILE.exists():
        return _load_json(SOURCE_OUTCOMES_FILE, {})
    # Default priors for sources
    defaults = {
        "simmer_divergence": {"wins": 5, "losses": 5, "total": 10},
        "inverse_whale": {"wins": 5, "losses": 5, "total": 10},
        "smart_money": {"wins": 5, "losses": 5, "total": 10},
        "volume_spike": {"wins": 3, "losses": 7, "total": 10},
        "resolution_timing": {"wins": 4, "losses": 6, "total": 10},
        "whale_new_position": {"wins": 5, "losses": 5, "total": 10},
        "news_google": {"wins": 3, "losses": 7, "total": 10},
        "news_reddit": {"wins": 3, "losses": 7, "total": 10},
    }
    _save_json(SOURCE_OUTCOMES_FILE, defaults)
    return defaults


def save_source_outcomes(outcomes: dict):
    """Save source outcomes."""
    _save_json(SOURCE_OUTCOMES_FILE, outcomes)


def get_source_win_rate(source: str) -> float:
    """Get win rate for a signal source."""
    outcomes = load_source_outcomes()
    data = outcomes.get(source, {"wins": 1, "losses": 1, "total": 2})
    if data["total"] == 0:
        return 0.5
    return data["wins"] / data["total"]


def record_outcome(source: str, won: bool, market_title: str = ""):
    """Record a trade outcome for Bayesian learning."""
    outcomes = load_source_outcomes()
    if source not in outcomes:
        outcomes[source] = {"wins": 0, "losses": 0, "total": 0, "history": []}
    outcomes[source]["total"] += 1
    if won:
        outcomes[source]["wins"] += 1
    else:
        outcomes[source]["losses"] += 1
    outcomes[source].setdefault("history", []).append({
        "won": won,
        "market": market_title[:50] if market_title else "",
        "timestamp": datetime.now().isoformat()
    })
    # Keep history manageable
    if len(outcomes[source]["history"]) > 100:
        outcomes[source]["history"] = outcomes[source]["history"][-100:]
    save_source_outcomes(outcomes)


def load_conflict_history() -> dict:
    """Load signal conflict history."""
    CONFLICT_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    return _load_json(CONFLICT_HISTORY_FILE, {"conflicts": [], "source_vs_source": {}})


def fetch_polymarket_positions(address: str, limit: int = 50) -> list:
    """Fetch positions from Polymarket Data API."""
    try:
        url = f"{POLYMARKET_DATA_API}/positions?user={address}&limit={limit}"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}


# ============================================================================
# Volume Spike Detection
# ============================================================================

def scan_volume_spikes(spike_threshold: float = 2.0, use_zscore: bool = True) -> dict:
    """Detect markets with unusual volume spikes using statistical analysis."""
    try:
        url = f"{GAMMA_API}/markets?limit=200&active=true&closed=false&order=volume24hr&ascending=false"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            markets = json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e), "spikes": []}

    # Calculate volume statistics
    volumes = [float(m.get("volume24hr", 0)) for m in markets if m.get("volume24hr", 0) > 0]
    if not volumes:
        return {"spikes": [], "note": "No volume data available"}

    mean_vol = sum(volumes) / len(volumes)
    variance = sum((v - mean_vol) ** 2 for v in volumes) / len(volumes)
    std_vol = variance ** 0.5 if variance > 0 else 1

    spikes = []
    for m in markets:
        vol = float(m.get("volume24hr", 0))
        if vol <= 0:
            continue

        if use_zscore:
            z_score = (vol - mean_vol) / std_vol if std_vol > 0 else 0
            if z_score >= spike_threshold:
                yes_price = 0.5
                if m.get("outcomePrices"):
                    try:
                        yes_price = float(json.loads(m["outcomePrices"])[0])
                    except Exception:
                        pass
                spikes.append({
                    "market_id": m.get("id"),
                    "title": m.get("question", "Unknown"),
                    "current_volume": vol,
                    "z_score": round(z_score, 2),
                    "spike_ratio": round(vol / mean_vol, 2) if mean_vol > 0 else 0,
                    "yes_price": yes_price,
                    "url": f"https://polymarket.com/event/{m.get('slug', m.get('id'))}"
                })
        else:
            ratio = vol / mean_vol if mean_vol > 0 else 0
            if ratio >= spike_threshold:
                yes_price = 0.5
                if m.get("outcomePrices"):
                    try:
                        yes_price = float(json.loads(m["outcomePrices"])[0])
                    except Exception:
                        pass
                spikes.append({
                    "market_id": m.get("id"),
                    "title": m.get("question", "Unknown"),
                    "current_volume": vol,
                    "z_score": round((vol - mean_vol) / std_vol, 2) if std_vol > 0 else 0,
                    "spike_ratio": round(ratio, 2),
                    "yes_price": yes_price,
                    "url": f"https://polymarket.com/event/{m.get('slug', m.get('id'))}"
                })

    spikes.sort(key=lambda x: x.get("z_score", 0), reverse=True)
    return {
        "spikes": spikes[:20],
        "count": len(spikes),
        "mean_volume": round(mean_vol, 2),
        "std_volume": round(std_vol, 2),
        "method": "zscore" if use_zscore else "ratio",
        "threshold": spike_threshold,
        "scan_time": datetime.now().isoformat()
    }


# ============================================================================
# Resolution Timing
# ============================================================================

def scan_resolution_timing(hours_until: int = 48) -> dict:
    """Find markets approaching resolution - volatility opportunities."""
    try:
        url = f"{GAMMA_API}/markets?limit=300&active=true&closed=false"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            markets = json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e), "markets": []}

    now = datetime.now()
    approaching = []

    for m in markets:
        end_date_str = m.get("endDate")
        if not end_date_str:
            continue

        try:
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00").replace("+00:00", ""))
            hours_left = (end_date - now).total_seconds() / 3600

            if 0 < hours_left <= hours_until:
                yes_price = 0.5
                if m.get("outcomePrices"):
                    try:
                        yes_price = float(json.loads(m["outcomePrices"])[0])
                    except Exception:
                        pass

                uncertainty = 1 - abs(yes_price - 0.5) * 2

                approaching.append({
                    "market_id": m.get("id"),
                    "title": m.get("question", "Unknown"),
                    "yes_price": yes_price,
                    "hours_until_resolution": round(hours_left, 1),
                    "end_date": end_date_str,
                    "volume_24h": m.get("volume24hr", 0),
                    "liquidity": m.get("liquidityNum", 0),
                    "uncertainty_score": round(uncertainty, 2),
                    "url": f"https://polymarket.com/event/{m.get('slug', m.get('id'))}",
                    "opportunity": "HIGH" if uncertainty > 0.7 and hours_left < 24 else "MEDIUM" if uncertainty > 0.5 else "LOW"
                })
        except Exception:
            continue

    approaching.sort(key=lambda x: x["hours_until_resolution"])
    return {
        "markets": approaching[:30],
        "count": len(approaching),
        "hours_threshold": hours_until,
        "scan_time": datetime.now().isoformat(),
        "note": "Markets near resolution often see volatility spikes as outcomes become clearer"
    }


# ============================================================================
# Whale Tracking Functions
# ============================================================================

def get_inverse_whale_signals() -> dict:
    """Find positions where losing whales are heavily invested - fade them."""
    stats = load_predictor_stats()
    predictors = stats.get("predictors", {})
    config = load_whale_config()

    losing_whales = []
    for address, data in predictors.items():
        if data.get("total_predictions", 0) >= 10 and data.get("accuracy", 50) < 50:
            losing_whales.append({
                "address": address,
                "name": data.get("name", "Unknown"),
                "accuracy": data.get("accuracy", 0),
                "total_profit": data.get("total_profit", 0)
            })

    if not losing_whales:
        return {"signals": [], "losing_whales": [], "note": "No losing whales identified yet (need more data)"}

    inverse_signals = []
    market_aggregates = {}

    for whale in losing_whales:
        positions = fetch_polymarket_positions(whale["address"], limit=30)
        if isinstance(positions, dict) and positions.get("error"):
            continue

        for p in (positions if isinstance(positions, list) else []):
            if p.get("currentValue", 0) < 100:
                continue

            market_title = p.get("title", "Unknown")
            outcome = p.get("outcome", "").upper()
            value = p.get("currentValue", 0)
            inverse_side = "NO" if outcome == "YES" else "YES"

            market_key = market_title[:50]
            if market_key not in market_aggregates:
                market_aggregates[market_key] = {
                    "title": market_title,
                    "whale_side": outcome,
                    "inverse_side": inverse_side,
                    "total_whale_value": 0,
                    "whale_count": 0,
                    "whales": [],
                    "avg_entry": p.get("avgPrice", 0.5),
                    "current_price": p.get("curPrice", 0.5)
                }

            market_aggregates[market_key]["total_whale_value"] += value
            market_aggregates[market_key]["whale_count"] += 1
            market_aggregates[market_key]["whales"].append({
                "name": whale["name"],
                "accuracy": whale["accuracy"],
                "value": value
            })

    for market_key, data in market_aggregates.items():
        avg_accuracy = sum(w["accuracy"] for w in data["whales"]) / len(data["whales"])
        confidence = min(100, (data["total_whale_value"] / 1000) * (50 - avg_accuracy))

        inverse_signals.append({
            "market": data["title"],
            "whale_side": data["whale_side"],
            "inverse_side": data["inverse_side"],
            "whale_value": round(data["total_whale_value"], 2),
            "whale_count": data["whale_count"],
            "avg_whale_accuracy": round(avg_accuracy, 1),
            "current_price": data["current_price"],
            "confidence_score": round(confidence, 1),
            "action": f"BET {data['inverse_side']} (fade {data['whale_count']} losing whale{'s' if data['whale_count'] > 1 else ''})"
        })

    inverse_signals.sort(key=lambda x: x["confidence_score"], reverse=True)
    return {
        "signals": inverse_signals[:15],
        "count": len(inverse_signals),
        "losing_whales": losing_whales,
        "strategy": "Fade positions where losing whales (accuracy <50%) are heavily invested"
    }


def get_smart_money_flow() -> dict:
    """Calculate net whale buying/selling per market."""
    config = load_whale_config()
    stats = load_predictor_stats()
    predictors = stats.get("predictors", {})

    market_flows = {}

    for whale in config.get("whales", []):
        address = whale["address"]
        name = whale.get("name", "Unknown")

        whale_data = predictors.get(address, {})
        accuracy = whale_data.get("accuracy", 50)
        weight = accuracy / 50

        positions = fetch_polymarket_positions(address, limit=50)
        if isinstance(positions, dict) and positions.get("error"):
            continue

        for p in (positions if isinstance(positions, list) else []):
            value = p.get("currentValue", 0)
            if value < 50:
                continue

            market_title = p.get("title", "Unknown")[:80]
            outcome = p.get("outcome", "").upper()

            if market_title not in market_flows:
                market_flows[market_title] = {
                    "title": market_title,
                    "yes_value": 0,
                    "no_value": 0,
                    "yes_weighted": 0,
                    "no_weighted": 0,
                    "whales_yes": [],
                    "whales_no": [],
                    "current_price": p.get("curPrice", 0.5)
                }

            weighted_value = value * weight

            if outcome == "YES":
                market_flows[market_title]["yes_value"] += value
                market_flows[market_title]["yes_weighted"] += weighted_value
                market_flows[market_title]["whales_yes"].append({"name": name, "value": value, "accuracy": accuracy})
            else:
                market_flows[market_title]["no_value"] += value
                market_flows[market_title]["no_weighted"] += weighted_value
                market_flows[market_title]["whales_no"].append({"name": name, "value": value, "accuracy": accuracy})

    flow_signals = []
    for market, data in market_flows.items():
        net_raw = data["yes_value"] - data["no_value"]
        net_weighted = data["yes_weighted"] - data["no_weighted"]
        total_value = data["yes_value"] + data["no_value"]

        if total_value < 200:
            continue

        if abs(net_weighted) > 500:
            signal_side = "YES" if net_weighted > 0 else "NO"
            conviction = "STRONG" if abs(net_weighted) > 2000 else "MODERATE"
        else:
            signal_side = "NEUTRAL"
            conviction = "WEAK"

        flow_signals.append({
            "market": data["title"],
            "net_flow_raw": round(net_raw, 2),
            "net_flow_weighted": round(net_weighted, 2),
            "yes_total": round(data["yes_value"], 2),
            "no_total": round(data["no_value"], 2),
            "whales_on_yes": len(data["whales_yes"]),
            "whales_on_no": len(data["whales_no"]),
            "current_price": data["current_price"],
            "signal": signal_side,
            "conviction": conviction,
            "action": f"{conviction} {signal_side}" if signal_side != "NEUTRAL" else "No clear signal"
        })

    flow_signals.sort(key=lambda x: abs(x["net_flow_weighted"]), reverse=True)
    return {
        "flows": flow_signals[:20],
        "count": len(flow_signals),
        "note": "Weighted by whale accuracy. Positive = bullish YES, Negative = bullish NO"
    }


# ============================================================================
# Bayesian Confidence Scoring
# ============================================================================

def laplace_smoothed_win_rate(wins: int, total: int, alpha: float = 4.0) -> float:
    """
    Laplace smoothing to prevent overfitting on small samples.
    
    alpha=4 acts as 4 pseudo-observations toward 50% (adds 4 wins + 4 losses).
    This prevents extreme win rates from small samples:
        - 0 wins / 0 total → 50% (not undefined)
        - 1 win / 1 total → 62.5% (not 100%)
        - 10 wins / 10 total → 78% (regression toward mean)
    """
    return (wins + alpha) / (total + 2 * alpha)


def sigmoid_normalize(raw_signal: float, k: float = 0.1, center: float = 50) -> float:
    """
    Sigmoid scaling to handle outliers and normalize to 0-100.
    
    k controls sensitivity (lower = more gradual curve)
    center is the neutral point (usually 50)
    """
    import math
    try:
        return 100 / (1 + math.exp(-k * (raw_signal - center)))
    except OverflowError:
        return 0 if raw_signal < center else 100


def calculate_bayesian_confidence(raw_score: float, source: str, market: str, side: str, all_signals: list) -> dict:
    """Calculate Bayesian-adjusted confidence score (legacy interface)."""
    win_rate = get_source_win_rate(source)
    bayesian_multiplier = win_rate / 0.5 if win_rate > 0 else 1.0
    bayesian_confidence = raw_score * bayesian_multiplier

    # Check for signal agreement
    agreement_count = 0
    agreeing_sources = []
    market_lower = market.lower()[:30]

    for sig in all_signals:
        if sig.get("market", "").lower()[:30] == market_lower:
            if sig.get("side", "").upper() == side.upper() and sig.get("source") != source:
                agreement_count += 1
                agreeing_sources.append(sig.get("source", "unknown"))

    # Composite multiplier: base 1.0 + 0.1 per agreeing source (max 1.5)
    composite_multiplier = min(1.5, 1.0 + (agreement_count * 0.1))
    final_confidence = bayesian_confidence * composite_multiplier

    return {
        "base_confidence": raw_score,
        "win_rate": round(win_rate, 3),
        "bayesian_multiplier": round(bayesian_multiplier, 2),
        "bayesian_confidence": round(bayesian_confidence, 1),
        "agreement_count": agreement_count,
        "agreeing_sources": agreeing_sources,
        "composite_multiplier": round(composite_multiplier, 2),
        "final_confidence": round(min(100, final_confidence), 1)
    }


def calculate_bayesian_confidence_v2(
    raw_scores: dict,      # {source: base_confidence}
    source_stats: dict,    # {source: {wins, total, direction}}
    alpha: float = 4.0,
    max_multiplier: float = 1.8
) -> dict:
    """
    Improved Bayesian confidence with:
    - Laplace smoothing (prevents overfitting on small samples)
    - Weighted average combination (weight by win rate)
    - Disagreement penalty (reduces confidence when sources conflict)
    - Capped multipliers (prevents runaway confidence)
    
    Args:
        raw_scores: Dict of {source_name: base_confidence_score}
        source_stats: Dict of {source_name: {wins, total, direction}}
        alpha: Laplace smoothing parameter (default 4.0)
        max_multiplier: Cap on Bayesian multiplier (default 1.8)
    
    Returns:
        Dict with final_confidence, breakdown, and agreement info
    """
    bayesian_confs = {}
    smoothed_wrs = {}
    directions = {}  # Track YES/NO per source
    
    for source, base in raw_scores.items():
        stats = source_stats.get(source, {"wins": 0, "total": 0})
        wins = stats.get("wins", 0)
        total = stats.get("total", 0)
        
        # Laplace smoothed win rate
        smoothed_wr = laplace_smoothed_win_rate(wins, total, alpha)
        smoothed_wrs[source] = smoothed_wr
        
        # Capped multiplier (prevents runaway from high win rates)
        multiplier = min(smoothed_wr / 0.5, max_multiplier)
        
        # Normalize base to valid range
        normalized_base = min(100, max(0, base))
        
        bayesian_confs[source] = normalized_base * multiplier
        directions[source] = stats.get("direction", "YES")
    
    if not bayesian_confs:
        return {"final_confidence": 50, "breakdown": {}}
    
    # Weighted average (weight = smoothed win rate)
    # Sources with better track records have more influence
    total_weight = sum(smoothed_wrs.values())
    if total_weight > 0:
        weighted_conf = sum(
            bayesian_confs[s] * smoothed_wrs[s] 
            for s in bayesian_confs
        ) / total_weight
    else:
        weighted_conf = sum(bayesian_confs.values()) / len(bayesian_confs)
    
    # Agreement/disagreement check
    unique_directions = set(directions.values())
    agreement_count = len(bayesian_confs)
    has_disagreement = len(unique_directions) > 1
    
    # Agreement multiplier with penalty for conflicts
    if has_disagreement:
        agreement_mult = 0.85  # 15% penalty for conflicting signals
    elif agreement_count >= 3:
        agreement_mult = 1.30  # 30% boost for 3+ agreeing sources
    elif agreement_count == 2:
        agreement_mult = 1.15  # 15% boost for 2 agreeing sources
    else:
        agreement_mult = 1.0   # No adjustment for single source
    
    final_conf = min(100, weighted_conf * agreement_mult)
    
    return {
        "final_confidence": round(final_conf, 1),
        "weighted_base": round(weighted_conf, 1),
        "agreement_multiplier": agreement_mult,
        "has_disagreement": has_disagreement,
        "source_count": agreement_count,
        "breakdown": {
            source: {
                "base": raw_scores[source],
                "bayesian": round(bayesian_confs[source], 1),
                "win_rate": round(smoothed_wrs[source] * 100, 1),
                "direction": directions.get(source, "YES")
            }
            for source in raw_scores
        }
    }


def combined_decision_score(edge_pct: float, confidence: float) -> dict:
    """
    Combined edge + confidence decision metric.
    
    Only bet when |edge| × (confidence/100) > threshold.
    This ensures we need BOTH a meaningful edge AND high confidence.
    
    Thresholds:
        > 5.0: STRONG signal - full position
        > 3.0: MODERATE signal - half position
        ≤ 3.0: WEAK signal - skip or quarter position
    
    Args:
        edge_pct: Edge percentage (positive = YES edge, negative = NO edge)
        confidence: Confidence score (0-100)
    
    Returns:
        Dict with decision metrics and sizing recommendation
    """
    adjusted_edge = abs(edge_pct) * (confidence / 100)
    
    if adjusted_edge > 5.0:
        strength = "strong"
        should_bet = True
        size_multiplier = 1.0
    elif adjusted_edge > 3.0:
        strength = "moderate"
        should_bet = True
        size_multiplier = 0.5
    else:
        strength = "weak"
        should_bet = False
        size_multiplier = 0.25
    
    return {
        "adjusted_edge": round(adjusted_edge, 2),
        "should_bet": should_bet,
        "bet_direction": "YES" if edge_pct > 0 else "NO",
        "strength": strength,
        "size_multiplier": size_multiplier,
        "rationale": f"|{edge_pct:.1f}%| × {confidence:.0f}/100 = {adjusted_edge:.1f}%"
    }


# ============================================================================
# Signal Aggregation
# ============================================================================

def aggregate_all_signals() -> dict:
    """Gather and score all trading signals from EVERY source."""
    all_signals = []

    # 1. Inverse Whale Signals
    try:
        inverse_data = get_inverse_whale_signals()
        for sig in inverse_data.get("signals", [])[:5]:
            all_signals.append({
                "source": "inverse_whale",
                "platform": "polymarket",
                "market": sig.get("market", ""),
                "side": sig.get("inverse_side", ""),
                "confidence": sig.get("confidence_score", 0),
                "value": sig.get("whale_value", 0),
                "reasoning": f"Fade {sig.get('whale_count', 0)} losing whale(s) with {sig.get('avg_whale_accuracy', 0):.0f}% accuracy",
                "price": sig.get("current_price", 0.5)
            })
    except Exception:
        pass

    # 2. Smart Money Flow
    try:
        flow_data = get_smart_money_flow()
        for flow in flow_data.get("flows", [])[:5]:
            if flow.get("conviction") in ["STRONG", "MODERATE"] and flow.get("signal") != "NEUTRAL":
                all_signals.append({
                    "source": "smart_money",
                    "platform": "polymarket",
                    "market": flow.get("market", ""),
                    "side": flow.get("signal", ""),
                    "confidence": abs(flow.get("net_flow_weighted", 0)) / 50,
                    "value": abs(flow.get("net_flow_weighted", 0)),
                    "reasoning": f"{flow.get('conviction')} flow: ${flow.get('net_flow_weighted', 0):+,.0f} weighted",
                    "price": flow.get("current_price", 0.5)
                })
    except Exception:
        pass

    # 3. Volume Spikes
    try:
        volume_data = scan_volume_spikes(2.0, True)
        for spike in volume_data.get("spikes", [])[:5]:
            price = spike.get("yes_price", 0.5)
            side = "YES" if price > 0.5 else "NO"
            all_signals.append({
                "source": "volume_spike",
                "platform": "polymarket",
                "market": spike.get("title", ""),
                "market_id": spike.get("market_id"),
                "side": side,
                "confidence": spike.get("z_score", 0) * 10,
                "value": spike.get("current_volume", 0),
                "reasoning": f"{spike.get('z_score', 0):.1f}σ volume spike ({spike.get('spike_ratio', 0):.1f}x normal)",
                "price": price
            })
    except Exception:
        pass

    # 4. Resolution Timing (HIGH opportunity only)
    try:
        resolution_data = scan_resolution_timing(24)
        for mkt in resolution_data.get("markets", [])[:5]:
            if mkt.get("opportunity") == "HIGH":
                all_signals.append({
                    "source": "resolution_timing",
                    "platform": "polymarket",
                    "market": mkt.get("title", ""),
                    "side": "RESEARCH",
                    "confidence": mkt.get("uncertainty_score", 0) * 30,
                    "value": mkt.get("hours_until_resolution", 0),
                    "reasoning": f"HIGH uncertainty, resolves in {mkt.get('hours_until_resolution', 0):.1f}h",
                    "price": mkt.get("yes_price", 0.5)
                })
    except Exception:
        pass

    # 5. News Signals (Google News + Reddit)
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from news_signal import scan_all_markets_for_news, get_trending_reddit_signals

        # Get active Polymarket markets for news scanning
        try:
            poly_req = urllib.request.Request(
                f"{GAMMA_API}/markets?closed=false&limit=30",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(poly_req, timeout=10) as resp:
                poly_markets = json.loads(resp.read().decode())
        except Exception:
            poly_markets = []

        news_signals = scan_all_markets_for_news(poly_markets[:15])
        for sig in news_signals:
            all_signals.append(sig)

        for category in ["crypto", "politics"]:
            reddit_signals = get_trending_reddit_signals(category)
            for sig in reddit_signals[:2]:
                all_signals.append(sig)
    except Exception:
        pass

    # 6. Edge Signals (from cache)
    try:
        api_path = str(Path(__file__).parent.parent)
        if api_path not in sys.path:
            sys.path.insert(0, api_path)
        from edge_cache import get_edge_signals
        edge_signals = get_edge_signals()
        all_signals.extend(edge_signals)
    except Exception:
        pass

    # 7. Mispriced Category + Whale Confirmation (backtested: 75% WR, 1.25 Sharpe)
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from mispriced_category_signal import get_mispriced_category_signals
        mcw_data = get_mispriced_category_signals()
        for sig in mcw_data.get("signals", [])[:10]:
            all_signals.append(sig)
    except Exception:
        pass

    # Apply Bayesian confidence scoring to all signals
    for sig in all_signals:
        raw_conf = sig.get("confidence", 0)
        bayesian_result = calculate_bayesian_confidence(
            raw_conf,
            sig.get("source", "unknown"),
            sig.get("market", ""),
            sig.get("side", ""),
            all_signals
        )
        sig["raw_confidence"] = raw_conf
        sig["confidence"] = bayesian_result["final_confidence"]
        sig["confidence_breakdown"] = {
            "base": bayesian_result["base_confidence"],
            "source_win_rate": bayesian_result["win_rate"],
            "bayesian_mult": bayesian_result["bayesian_multiplier"],
            "agreement": bayesian_result["agreement_count"],
            "composite_mult": bayesian_result["composite_multiplier"]
        }

    all_signals.sort(key=lambda x: x.get("confidence", 0), reverse=True)

    actionable = [s for s in all_signals if s.get("side") not in ["NEUTRAL", "RESEARCH", "ARB", ""]]
    research = [s for s in all_signals if s.get("side") == "RESEARCH"]
    arb = [s for s in all_signals if s.get("side") == "ARB"]

    source_counts = {}
    for s in all_signals:
        src = s.get("source", "unknown")
        source_counts[src] = source_counts.get(src, 0) + 1

    return {
        "actionable_signals": actionable,
        "research_signals": research,
        "arb_signals": arb,
        "total_signals": len(all_signals),
        "actionable_count": len(actionable),
        "sources": source_counts,
        "scoring_method": "bayesian_composite",
        "generated_at": datetime.now().isoformat()
    }


# ============================================================================
# Endpoints: Signals
# ============================================================================

@router.get("/signals")
async def get_all_signals():
    """Get aggregated signals from all sources."""
    try:
        result = aggregate_all_signals()
        logger.info(f"Signal aggregation: {result.get('total_signals', 0)} signals from {len(result.get('sources', {}))} sources")
        return result
    except Exception as e:
        logger.exception(f"Signal aggregation failed: {e}")
        raise HTTPException(status_code=500, detail="Signal aggregation failed")


@router.get("/signals/mispriced-category")
async def get_mispriced_category_strategy_signals():
    """Get signals from the MispricedCategoryWhale strategy.
    
    Backtested: 75% win rate, 1.25 Sharpe, 155K trades across 4M markets.
    Targets high-volume markets in mispriced categories using volume
    spikes and whale activity as confirmation.
    """
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from mispriced_category_signal import get_mispriced_category_signals
        return get_mispriced_category_signals()
    except Exception as e:
        logger.exception(f"Mispriced category signal scan failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/signals/news")
async def get_news_signals():
    """Get signals specifically from news sources (Google News + Reddit)."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from news_signal import (
            fetch_google_news, fetch_reddit_posts,
            get_trending_reddit_signals, analyze_sentiment
        )

        results = {
            "google_news": {},
            "reddit": {},
            "signals": [],
        }

        # Fetch news for key topics
        for topic in ["bitcoin", "trump", "super bowl"]:
            articles = fetch_google_news(topic, max_results=5)
            results["google_news"][topic] = [
                {
                    "title": a.get("title", "")[:80],
                    "source": a.get("source", ""),
                    "age_minutes": a.get("age_minutes"),
                    "sentiment": analyze_sentiment(a.get("title", ""))
                }
                for a in articles[:3]
            ]

        # Fetch Reddit trending
        for category in ["crypto", "politics"]:
            signals = get_trending_reddit_signals(category)
            results["signals"].extend(signals)

        results["generated_at"] = datetime.now().isoformat()
        logger.info(f"News signals: {len(results['signals'])} signals generated")
        return results

    except Exception as e:
        logger.warning(f"News signals error: {e}")
        return {"error": str(e), "enabled": False}


@router.post("/signals/auto-trade")
async def auto_trade_on_signals(
    max_trades: int = Query(5, ge=1, le=10, description="Max trades to execute"),
    max_per_trade: float = Query(100, ge=10, le=500, description="Max $ per trade"),
    min_confidence: float = Query(10, ge=0, le=100, description="Minimum confidence score"),
    api_key: str = Query(None, description="API key for authentication")
):
    """Automatically paper trade based on all aggregated signals.

    Requires authentication for actual trading execution.
    """
    # Basic auth check
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required for auto-trading")

    try:
        signals = aggregate_all_signals()
        actionable = signals.get("actionable_signals", [])

        # For this router, we only return the analysis - actual execution
        # happens in the trading routes to maintain separation of concerns
        trades_to_execute = []
        trades_skipped = []

        for sig in actionable:
            if len(trades_to_execute) >= max_trades:
                break

            if sig.get("confidence", 0) < min_confidence:
                trades_skipped.append({
                    "market": sig.get("market", "")[:40],
                    "reason": f"Confidence {sig.get('confidence', 0):.1f} below minimum {min_confidence}"
                })
                continue

            confidence = sig.get("confidence", 0)
            size_pct = min(0.05, confidence / 500)
            amount = min(max_per_trade, 10000 * size_pct)

            if amount < 10:
                continue

            trades_to_execute.append({
                "source": sig.get("source"),
                "market": sig.get("market", "")[:50],
                "market_id": sig.get("market_id"),
                "side": sig.get("side"),
                "amount": round(amount, 2),
                "price": sig.get("price", 0.5),
                "confidence": sig.get("confidence"),
                "reasoning": sig.get("reasoning", "")
            })

        logger.info(f"Auto-trade analysis: {len(trades_to_execute)} trades to execute, {len(trades_skipped)} skipped")
        return {
            "signals_found": len(actionable),
            "trades_to_execute": trades_to_execute,
            "trades_skipped": trades_skipped,
            "total_amount": round(sum(t["amount"] for t in trades_to_execute), 2),
            "note": "Use /paper/buy or /simmer/trade to execute these trades"
        }

    except Exception as e:
        logger.exception(f"Auto-trade analysis failed: {e}")
        raise HTTPException(status_code=500, detail="Auto-trade analysis failed")


# ============================================================================
# Endpoints: Volume
# ============================================================================

@router.get("/volume/spikes")
async def get_volume_spikes(
    threshold: float = Query(2.0, ge=1.0, le=5, description="Z-score threshold (2.0 = 2 std devs above mean)"),
    method: str = Query("zscore", description="Detection method: 'zscore' or 'ratio'")
):
    """Detect markets with unusual volume spikes using statistical analysis."""
    use_zscore = method.lower() == "zscore"
    try:
        result = scan_volume_spikes(threshold, use_zscore)
        logger.info(f"Volume spikes: {result.get('count', 0)} spikes detected")
        return result
    except Exception as e:
        logger.exception(f"Volume spike scan failed: {e}")
        raise HTTPException(status_code=500, detail="Volume spike scan failed")


# ============================================================================
# Endpoints: Resolution
# ============================================================================

@router.get("/resolution/approaching")
async def get_approaching_resolution(
    hours: int = Query(48, ge=1, le=168, description="Hours until resolution threshold")
):
    """Find markets approaching resolution - volatility opportunities."""
    try:
        return scan_resolution_timing(hours)
    except Exception as e:
        logger.exception(f"Resolution scan failed: {e}")
        raise HTTPException(status_code=500, detail="Resolution scan failed")


@router.get("/resolution/imminent")
async def get_imminent_resolution():
    """Markets resolving within 24 hours - highest volatility potential."""
    try:
        result = scan_resolution_timing(24)
        high_opp = [m for m in result.get("markets", []) if m.get("opportunity") == "HIGH"]
        return {
            "markets": high_opp,
            "count": len(high_opp),
            "note": "HIGH uncertainty markets resolving within 24h - prime volatility plays"
        }
    except Exception as e:
        logger.exception(f"Imminent resolution scan failed: {e}")
        raise HTTPException(status_code=500, detail="Resolution scan failed")


# ============================================================================
# Endpoints: Cross-Market Correlation
# ============================================================================

@router.get("/correlation/violations")
async def get_correlation_violations(
    min_violation: float = Query(3.0, ge=1.0, le=20.0, description="Minimum violation % to report")
):
    """
    Find probability constraint violations between related markets.
    
    Detects cases where narrower outcomes are priced higher than broader ones:
    - P(Chiefs win Super Bowl) should be <= P(Chiefs win AFC)
    - P(Trump wins election) should be <= P(Trump wins nomination)
    
    Violations indicate mispricing / arbitrage opportunities.
    """
    try:
        from odds.correlation import scan_correlation_arb
        
        # Fetch active markets from Polymarket
        url = f"{GAMMA_API}/markets?limit=200&active=true&closed=false"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            markets = json.loads(resp.read().decode())
        
        if not markets:
            return {"violations": [], "error": "Failed to fetch markets"}
        
        result = scan_correlation_arb(markets, min_violation_pct=min_violation)
        return result
    except Exception as e:
        logger.exception(f"Correlation scan failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/correlation/entities")
async def get_market_entities():
    """
    Get all entities (teams, people) with multiple related markets.
    
    Useful for manually checking correlation constraints.
    """
    try:
        from odds.correlation import group_markets_by_entity
        
        # Fetch active markets from Polymarket
        url = f"{GAMMA_API}/markets?limit=200&active=true&closed=false"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            markets = json.loads(resp.read().decode())
        
        if not markets:
            return {"entities": [], "error": "Failed to fetch markets"}
        
        entity_groups = group_markets_by_entity(markets)
        
        # Helper to extract price
        def get_price(m):
            if m.get("yes_price"):
                return m.get("yes_price")
            prices = m.get("outcomePrices")
            if prices:
                if isinstance(prices, str):
                    try:
                        prices = json.loads(prices)
                    except:
                        return None
                if isinstance(prices, list) and len(prices) > 0:
                    try:
                        return float(prices[0])
                    except:
                        return None
            return None
        
        # Filter to entities with 2+ markets
        multi_market_entities = {
            entity: [
                {
                    "title": m.get("title") or m.get("question"),
                    "price": get_price(m),
                    "id": m.get("id") or m.get("condition_id")
                }
                for m in markets_list
            ]
            for entity, markets_list in entity_groups.items()
            if len(markets_list) >= 2
        }
        
        return {
            "entity_count": len(multi_market_entities),
            "entities": multi_market_entities
        }
    except Exception as e:
        logger.exception(f"Entity scan failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Endpoints: Whale Tracking
# ============================================================================

@router.get("/predictors")
async def get_predictor_stats():
    """Get accuracy statistics for all tracked predictors (whales)."""
    try:
        stats = load_predictor_stats()
        predictors = stats.get("predictors", {})

        leaderboard = []
        for address, data in predictors.items():
            if data.get("total_predictions", 0) > 0:
                leaderboard.append({
                    "address": address,
                    "name": data.get("name", "Unknown"),
                    "accuracy": data.get("accuracy", 0),
                    "total_predictions": data.get("total_predictions", 0),
                    "correct_predictions": data.get("correct_predictions", 0),
                    "total_profit": round(data.get("total_profit", 0), 2),
                    "avg_profit_per_trade": round(data.get("total_profit", 0) / data.get("total_predictions", 1), 2)
                })

        leaderboard.sort(key=lambda x: (x["total_predictions"] >= 10, x["accuracy"]), reverse=True)

        return {
            "leaderboard": leaderboard,
            "count": len(leaderboard),
            "last_updated": stats.get("last_updated"),
            "note": "Accuracy based on resolved positions only"
        }
    except Exception as e:
        logger.exception(f"Predictor stats failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to load predictor stats")


@router.post("/predictors/update")
async def refresh_predictor_stats():
    """Refresh predictor accuracy statistics."""
    # This is a placeholder - the actual update logic would need to be
    # implemented based on the full predictor tracking system
    stats = load_predictor_stats()
    stats["last_updated"] = datetime.now().isoformat()
    save_predictor_stats(stats)
    return {
        "updated": True,
        "predictors_tracked": len(stats.get("predictors", {})),
        "last_updated": stats.get("last_updated")
    }


@router.get("/inverse-whale")
async def inverse_whale_signals():
    """Get signals to fade losing whale positions."""
    try:
        result = get_inverse_whale_signals()
        logger.info(f"Inverse whale: {result.get('count', 0)} signals")
        return result
    except Exception as e:
        logger.exception(f"Inverse whale scan failed: {e}")
        raise HTTPException(status_code=500, detail="Inverse whale scan failed")


@router.get("/smart-money")
async def smart_money_flow():
    """Get net whale flow per market (weighted by accuracy)."""
    try:
        result = get_smart_money_flow()
        logger.info(f"Smart money: {result.get('count', 0)} flow signals")
        return result
    except Exception as e:
        logger.exception(f"Smart money scan failed: {e}")
        raise HTTPException(status_code=500, detail="Smart money scan failed")


# ============================================================================
# Endpoints: Confidence
# ============================================================================

@router.get("/confidence/sources")
async def get_source_statistics():
    """Get win rate statistics for all signal sources."""
    try:
        outcomes = load_source_outcomes()
        stats = []

        for source, data in outcomes.items():
            win_rate = data["wins"] / data["total"] if data["total"] > 0 else 0.5
            stats.append({
                "source": source,
                "wins": data["wins"],
                "losses": data["losses"],
                "total": data["total"],
                "win_rate": round(win_rate * 100, 1),
                "bayesian_multiplier": round(win_rate / 0.5, 2)
            })

        stats.sort(key=lambda x: x["win_rate"], reverse=True)
        return {"sources": stats}
    except Exception as e:
        logger.exception(f"Source statistics failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to load source statistics")


@router.post("/confidence/record")
async def record_trade_outcome(
    source: str = Query(..., description="Signal source"),
    won: bool = Query(..., description="Did the trade win?")
):
    """Record a trade outcome to update source reliability."""
    try:
        record_outcome(source, won)
        return {
            "recorded": True,
            "source": source,
            "outcome": "win" if won else "loss",
            "new_win_rate": round(get_source_win_rate(source) * 100, 1)
        }
    except Exception as e:
        logger.exception(f"Record outcome failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to record outcome")


@router.get("/confidence/market/{market_id}")
async def get_market_confidence(market_id: str):
    """Get confidence scoring for a specific market across all signal sources."""
    try:
        signals = aggregate_all_signals()
        market_signals = []

        for sig in signals.get("actionable_signals", []) + signals.get("research_signals", []):
            if market_id.lower() in sig.get("market_id", "").lower() or \
               market_id.lower() in sig.get("market", "").lower()[:50]:
                market_signals.append({
                    "source": sig.get("source"),
                    "side": sig.get("side"),
                    "confidence": sig.get("confidence"),
                    "raw_confidence": sig.get("raw_confidence"),
                    "breakdown": sig.get("confidence_breakdown"),
                    "reasoning": sig.get("reasoning")
                })

        if not market_signals:
            return {"market_id": market_id, "signals": [], "note": "No active signals for this market"}

        return {
            "market_id": market_id,
            "signals": market_signals,
            "signal_count": len(market_signals),
            "avg_confidence": round(sum(s["confidence"] for s in market_signals) / len(market_signals), 1)
        }
    except Exception as e:
        logger.exception(f"Market confidence failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to get market confidence")


@router.get("/confidence/history")
async def get_confidence_history(limit: int = Query(50, ge=1, le=200)):
    """Get recent trade outcome history for Bayesian learning analysis."""
    try:
        outcomes = load_source_outcomes()
        all_history = []

        for source, data in outcomes.items():
            for entry in data.get("history", []):
                all_history.append({
                    "source": source,
                    "won": entry.get("won"),
                    "market": entry.get("market", ""),
                    "timestamp": entry.get("timestamp")
                })

        all_history.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return {
            "history": all_history[:limit],
            "total_entries": len(all_history)
        }
    except Exception as e:
        logger.exception(f"Confidence history failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to load confidence history")


@router.get("/confidence/calibration")
async def get_calibration_data():
    """Get calibration data for signal sources - comparing predicted vs actual win rates."""
    try:
        outcomes = load_source_outcomes()
        calibration = []

        for source, data in outcomes.items():
            if data.get("total", 0) < 5:
                continue

            actual_win_rate = data["wins"] / data["total"] if data["total"] > 0 else 0.5
            # Expected is 50% (baseline)
            calibration_error = actual_win_rate - 0.5

            calibration.append({
                "source": source,
                "sample_size": data["total"],
                "actual_win_rate": round(actual_win_rate * 100, 1),
                "expected_win_rate": 50.0,
                "calibration_error": round(calibration_error * 100, 1),
                "status": "OVERPERFORMING" if calibration_error > 0.1 else "UNDERPERFORMING" if calibration_error < -0.1 else "WELL_CALIBRATED"
            })

        calibration.sort(key=lambda x: x["calibration_error"], reverse=True)
        return {
            "calibration": calibration,
            "note": "Positive calibration error = source wins more than expected"
        }
    except Exception as e:
        logger.exception(f"Calibration data failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to get calibration data")


# ============================================================================
# Endpoints: Conflicts
# ============================================================================

@router.get("/conflicts/stats")
async def get_conflict_stats():
    """Get conflict resolution statistics and source-vs-source performance."""
    try:
        history = load_conflict_history()

        conflicts = history.get("conflicts", [])
        svs = history.get("source_vs_source", {})

        recent = conflicts[-10:] if conflicts else []

        matchups = []
        for key, data in svs.items():
            total = data["wins"] + data["losses"]
            if total >= 1:
                matchups.append({
                    "matchup": key,
                    "wins": data["wins"],
                    "losses": data["losses"],
                    "total": total,
                    "win_rate": round(data["wins"] / total * 100, 1)
                })

        matchups.sort(key=lambda x: x["total"], reverse=True)

        resolved_conflicts = [c for c in conflicts if c.get("resolved")]
        traded_conflicts = [c for c in conflicts if c.get("traded_side")]

        return {
            "total_conflicts": len(conflicts),
            "resolved_conflicts": len(resolved_conflicts),
            "traded_conflicts": len(traded_conflicts),
            "skipped_conflicts": len(conflicts) - len(traded_conflicts),
            "source_matchups": matchups[:20],
            "recent_conflicts": recent
        }
    except Exception as e:
        logger.exception(f"Conflict stats failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to load conflict stats")


@router.get("/conflicts/active")
async def get_active_conflicts():
    """Get currently active signal conflicts (opposing signals on same market)."""
    try:
        signals = aggregate_all_signals()
        actionable = signals.get("actionable_signals", [])

        market_signals = {}
        for sig in actionable:
            market_key = sig.get("market", "")[:40].lower()
            if market_key not in market_signals:
                market_signals[market_key] = {"YES": [], "NO": []}

            side = sig.get("side", "").upper()
            if side in ["YES", "NO"]:
                market_signals[market_key][side].append({
                    "source": sig.get("source"),
                    "confidence": sig.get("confidence"),
                    "reasoning": sig.get("reasoning", "")[:100]
                })

        conflicts = []
        for market, sides in market_signals.items():
            if sides["YES"] and sides["NO"]:
                yes_conf = sum(s["confidence"] for s in sides["YES"])
                no_conf = sum(s["confidence"] for s in sides["NO"])
                conflicts.append({
                    "market": market,
                    "yes_signals": sides["YES"],
                    "no_signals": sides["NO"],
                    "net_direction": "YES" if yes_conf > no_conf else "NO",
                    "confidence_delta": abs(yes_conf - no_conf)
                })

        conflicts.sort(key=lambda x: x["confidence_delta"])
        return {
            "conflicts": conflicts,
            "count": len(conflicts),
            "note": "Markets where signals disagree - lower delta = more uncertain"
        }
    except Exception as e:
        logger.exception(f"Active conflicts failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to get active conflicts")


# ============================================================================
# Endpoints: Rotations
# ============================================================================

@router.get("/rotations")
async def get_recent_rotations(hours: int = Query(24, ge=1, le=168)):
    """Get recent position rotations."""
    try:
        TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
        trades = _load_json(TRADES_FILE, [])

        cutoff = datetime.now() - timedelta(hours=hours)
        rotations = []

        for trade in trades:
            if trade.get("type") == "SELL" and trade.get("reason", "").startswith("rotation:"):
                try:
                    trade_time = datetime.fromisoformat(trade.get("timestamp", ""))
                    if trade_time > cutoff:
                        rotations.append({
                            "exited_market": trade.get("market", "")[:50],
                            "exited_side": trade.get("side"),
                            "pnl": trade.get("pnl", 0),
                            "reason": trade.get("reason"),
                            "timestamp": trade.get("timestamp")
                        })
                except Exception:
                    pass

        rotations.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

        return {
            "rotations": rotations,
            "count": len(rotations),
            "hours": hours
        }
    except Exception as e:
        logger.exception(f"Rotations fetch failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch rotations")


@router.get("/rotation/candidates")
async def get_rotation_candidates():
    """Get positions that are candidates for rotation based on EV decay."""
    try:
        # This would need access to positions - for now return placeholder
        return {
            "candidates": [],
            "note": "Use /paper-poly/positions to see current positions and their EV scores"
        }
    except Exception as e:
        logger.exception(f"Rotation candidates failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to get rotation candidates")

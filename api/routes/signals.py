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
- /signals/ic-report - IC measurement across all sources
- /signals/ic/{source} - Per-source IC measurement
"""
import json
import os
import logging
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

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
        "election_cross_platform": {"wins": 5, "losses": 5, "total": 10},
        "election_fec_money": {"wins": 4, "losses": 6, "total": 10},
        "election_momentum": {"wins": 3, "losses": 7, "total": 10},
        "election_ie_spending": {"wins": 4, "losses": 6, "total": 10},
        "election_primary": {"wins": 4, "losses": 6, "total": 10},
        "election_narrative": {"wins": 3, "losses": 7, "total": 10},
        "election_poll_divergence": {"wins": 5, "losses": 5, "total": 10},
        "election_efiling": {"wins": 5, "losses": 5, "total": 10},
        "election_wiki_attention": {"wins": 3, "losses": 7, "total": 10},
        "election_gtrends": {"wins": 3, "losses": 7, "total": 10},
        "election_smart_money": {"wins": 4, "losses": 6, "total": 10},
        "election_whale_concentration": {"wins": 3, "losses": 7, "total": 10},
        "election_cash_momentum": {"wins": 4, "losses": 6, "total": 10},
        "election_economic_macro": {"wins": 4, "losses": 6, "total": 10},
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

    # 8. Election signals (13 strategies: arb, FEC, momentum, IE, primary, narrative, polls, eFiling, wiki, trends, smart money, whale, cash momentum)
    try:
        from signals.election_signal import generate_election_signals
        election_sigs = generate_election_signals()
        all_signals.extend(election_sigs[:10])
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

    # Record predictions for IC (Information Coefficient) tracking
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from ic_tracker import record_signal_prediction
        from calibrator import calibrate_confidence
        for sig in all_signals:
            if sig.get("market_id") and sig.get("side") not in ["NEUTRAL", "RESEARCH", ""]:
                # Auto-calibrate confidence before recording
                raw_conf = sig.get("confidence", 0)
                cal_conf = calibrate_confidence(sig.get("source", ""), raw_conf)
                if cal_conf != raw_conf:
                    sig["confidence_raw"] = raw_conf
                    sig["confidence"] = round(cal_conf, 1)
                record_signal_prediction(sig)
    except Exception:
        pass  # Non-critical — IC tracking failure must not block signal generation

    # Boost daily-expiry markets: they resolve fast, accelerating IC feedback loop
    def _sort_key(s):
        conf = s.get("confidence", 0)
        dtc = s.get("days_to_close", 30)
        # Daily markets get +10 confidence boost in sort order (not stored)
        daily_boost = 10 if dtc <= 1.5 else (5 if dtc <= 3 else 0)
        return conf + daily_boost
    all_signals.sort(key=_sort_key, reverse=True)

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


@router.get("/signals/shadow-performance")
async def get_shadow_performance():
    """Get shadow trading performance stats and daily summaries."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from shadow_tracker import get_trade_stats, get_performance_history, get_open_trades
        stats = get_trade_stats()
        history = get_performance_history(30)
        open_trades = get_open_trades()
        return {
            "stats": stats,
            "daily_history": history[:14],
            "open_trades_count": len(open_trades),
            "open_trades": [
                {
                    "market_id": t["market_id"],
                    "market": (t.get("market") or "")[:60],
                    "side": t["side"],
                    "entry_price": t["entry_price"],
                    "confidence": t["confidence"],
                    "category": t["category"],
                }
                for t in open_trades[:20]
            ],
        }
    except Exception as e:
        logger.exception(f"Shadow performance failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/signals/shadow-resolve")
async def trigger_shadow_resolution():
    """Manually trigger shadow trade resolution."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from shadow_tracker import resolve_trades, generate_daily_summary
        resolve_result = resolve_trades(batch_size=20, delay=0.3)
        summary = generate_daily_summary()
        return {"resolution": resolve_result, "daily_summary": summary}
    except Exception as e:
        logger.exception(f"Shadow resolution failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# AI Model Market Tracker
# ============================================================================

@router.get("/signals/ai-models")
async def get_ai_model_tracker():
    """Get Arena leaderboard rankings and AI model market signals."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from ai_model_tracker import get_arena_summary
        return get_arena_summary()
    except Exception as e:
        logger.exception(f"AI model tracker failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/signals/ai-models/trends")
async def get_ai_model_trends(days: int = Query(default=7, ge=1, le=90)):
    """Get Arena score trends over recent days."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from ai_model_tracker import get_score_trends
        return get_score_trends(days=days)
    except Exception as e:
        logger.exception(f"AI model trends failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Paper Portfolio
# ============================================================================

@router.get("/portfolio/status")
async def get_portfolio_status():
    """Get current paper portfolio status — bankroll, positions, P&L."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from paper_portfolio import get_portfolio_status
        return get_portfolio_status()
    except Exception as e:
        logger.exception(f"Portfolio status failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/portfolio/positions")
async def get_portfolio_positions(status: str = Query(default="all")):
    """Get paper positions. Filter by status: all, open, closed."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from paper_portfolio import get_positions
        return get_positions(status=status)
    except Exception as e:
        logger.exception(f"Portfolio positions failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/portfolio/history")
async def get_portfolio_history(limit: int = Query(default=50)):
    """Get closed position history with P&L."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from paper_portfolio import get_position_history
        return get_position_history(limit=limit)
    except Exception as e:
        logger.exception(f"Portfolio history failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/portfolio/process-signals")
async def process_portfolio_signals():
    """Run signal pipeline and auto-open paper positions for qualifying signals."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from paper_portfolio import process_signals
        from mispriced_category_signal import get_mispriced_category_signals
        result = get_mispriced_category_signals(); signals = result.get("signals", [])
        return process_signals(signals)
    except Exception as e:
        logger.exception(f"Portfolio signal processing failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/portfolio/resolve")
async def resolve_portfolio_positions():
    """Auto-resolve stale open positions using Polymarket CLOB / Kalshi APIs."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from paper_portfolio import resolve_open_positions
        return resolve_open_positions()
    except Exception as e:
        logger.exception(f"Portfolio resolve failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/portfolio/positions-live")
async def get_portfolio_positions_live():
    """Get open positions with live market prices and unrealized P&L."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from paper_portfolio import get_live_positions
        return get_live_positions()
    except Exception as e:
        logger.exception(f"Live positions failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/portfolio/archetype-breakdown")
async def get_portfolio_archetype_breakdown():
    """Get win rate and P&L breakdown by market archetype."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from paper_portfolio import get_archetype_breakdown
        return get_archetype_breakdown()
    except Exception as e:
        logger.exception(f"Archetype breakdown failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/portfolio/archetype-pnl-series")
async def get_archetype_pnl_series():
    """Get per-archetype cumulative P&L series for sparklines."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from paper_portfolio import get_archetype_cumulative_pnl
        return get_archetype_cumulative_pnl()
    except Exception as e:
        logger.exception(f"Archetype P&L series failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/portfolio/close/{position_id}")
async def manually_close_position(position_id: int, outcome: str = Query(..., pattern="^(won|lost)$")):
    """Manually close an open position as won or lost."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from paper_portfolio import close_position_by_id
        return close_position_by_id(position_id, outcome)
    except Exception as e:
        logger.exception(f"Manual close failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/portfolio/resolve-log")
async def get_resolve_log(limit: int = Query(default=20)):
    """Get the last N resolved positions with timestamps and close reasons."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from paper_portfolio import get_resolve_log
        return get_resolve_log(limit=limit)
    except Exception as e:
        logger.exception(f"Resolve log failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Equity Curve
# ============================================================================

@router.get("/portfolio/equity-series")
async def get_portfolio_equity_series(hours: int = Query(default=0, ge=0, le=8760)):
    """Time-series equity snapshots (realized + unrealized).

    Each point: {ts, realized_bankroll, unrealized_pnl, total_equity, open_positions, peak_equity, source}
    On first call, auto-backfills from closed trade history. Pass hours=0 (default)
    for all snapshots, or hours=N to limit to the last N hours.
    """
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from paper_portfolio import get_equity_series, backfill_equity_snapshots
        # Auto-backfill on first call — idempotent
        backfill_equity_snapshots()
        return get_equity_series(hours=hours or None)
    except Exception as e:
        logger.exception(f"Equity series failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/portfolio/equity-snapshot")
async def post_portfolio_equity_snapshot():
    """Force-capture a single equity snapshot now. Used by scheduler/debug."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from paper_portfolio import snapshot_equity
        return snapshot_equity()
    except Exception as e:
        logger.exception(f"Equity snapshot failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/portfolio/equity-curve")
async def get_portfolio_equity_curve():
    """Get equity curve data points from paper_portfolio_state table."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        import sqlite3
        from pathlib import Path
        db_path = Path(signals_path).parent / "storage" / "shadow_trades.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT timestamp, bankroll FROM paper_portfolio_state ORDER BY timestamp ASC"
        ).fetchall()
        conn.close()
        # Add initial point
        curve = [{"t": "", "v": 500.0}]
        for r in rows:
            curve.append({"t": r["timestamp"], "v": r["bankroll"]})
        return curve
    except Exception as e:
        logger.exception(f"Equity curve failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Copy-Trade Whale Signals
# ============================================================================

@router.get("/signals/copy-trade")
async def get_copy_trade_data():
    """Get whale copy-trade signals and overlaps."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from copy_trade_watcher import get_copy_trade_signals
        return get_copy_trade_signals()
    except Exception as e:
        logger.exception(f"Copy-trade scan failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/signals/cross-platform-arb")
async def get_cross_platform_arb():
    """Scan for cross-platform arbitrage between Kalshi and Polymarket."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from cross_platform_arb import scan_cross_platform_arb
        return scan_cross_platform_arb()
    except Exception as e:
        logger.exception(f"Cross-platform arb scan failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Resolution Certainty Scanner
# ============================================================================

@router.get("/signals/resolution-certainty")
async def get_resolution_certainty():
    """Scan open markets for near-certain outcomes using real-time data."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from resolution_scanner import get_resolution_summary
        return get_resolution_summary()
    except Exception as e:
        logger.exception(f"Resolution certainty scan failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/signals/ic-report")
async def get_ic_report(window_days: int = Query(30, ge=1, le=365)):
    """IC (Information Coefficient) report across all signal sources.

    Measures Spearman rank correlation between predicted confidence and outcome.
    IC < 0.03 = KILL (noise), IC < 0.05 = WARN (marginal), IC >= 0.05 = OK (alpha).
    """
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from ic_tracker import ic_report
        return ic_report(window_days=window_days)
    except Exception as e:
        logger.exception(f"IC report failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/signals/ic/{source}")
async def get_ic_for_source(source: str, window_days: int = Query(30, ge=1, le=365)):
    """IC measurement for a specific signal source."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from ic_tracker import calculate_ic
        return calculate_ic(source=source, window_days=window_days)
    except Exception as e:
        logger.exception(f"IC calculation for {source} failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Alpha Score Tracker
# ============================================================================

@router.get("/signals/alpha-snapshot")
async def run_alpha_snapshot():
    """Run and return a fresh alpha score + BTC/ETH price snapshot."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from alpha_score_tracker import run_snapshot
        return run_snapshot()
    except Exception as e:
        logger.exception(f"Alpha snapshot failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/signals/alpha-history/{symbol}")
async def get_alpha_history(symbol: str, hours: int = Query(default=24)):
    """Get confluence score history for a symbol."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from alpha_score_tracker import get_score_history, get_score_delta
        return {
            "symbol": symbol,
            "hours": hours,
            "history": get_score_history(symbol, hours),
            "delta_2h": get_score_delta(symbol, 2),
            "delta_6h": get_score_delta(symbol, 6),
            "delta_24h": get_score_delta(symbol, 24),
        }
    except Exception as e:
        logger.exception(f"Alpha history failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/signals/btc-tracker")
async def get_btc_tracker(hours: int = Query(default=24)):
    """Get BTC/ETH price snapshot history with deltas."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from alpha_score_tracker import get_price_history, get_btc_price_delta
        return {
            "btc": {
                "history": get_price_history("BTCUSDT", hours),
                "delta_2h": get_btc_price_delta(2),
                "delta_6h": get_btc_price_delta(6),
                "delta_24h": get_btc_price_delta(24),
            },
            "eth": {
                "history": get_price_history("ETHUSDT", hours),
            }
        }
    except Exception as e:
        logger.exception(f"BTC tracker failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Auto-Calibration
# ============================================================================

@router.get("/signals/calibration")
async def get_calibration_report():
    """Full calibration report — per-source curves, ECE, source weights."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from calibrator import full_calibration_report
        return full_calibration_report()
    except Exception as e:
        logger.exception(f"Calibration report failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/signals/calibration/{source}")
async def get_source_calibration(source: str):
    """Calibration curve for a specific signal source."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from calibrator import build_calibration_curve, get_signal_decay
        return {
            "calibration": build_calibration_curve(source),
            "decay": get_signal_decay(source),
        }
    except Exception as e:
        logger.exception(f"Source calibration failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/signals/source-weights")
async def get_source_weights():
    """Optimal source weights based on IC-squared."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from calibrator import compute_source_weights
        return compute_source_weights()
    except Exception as e:
        logger.exception(f"Source weights failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Weather Scanner
# ============================================================================

@router.get("/signals/weather/ensemble-status")
async def weather_ensemble_status():
    """Ensemble health: per-source status, RMSE, calibration, paper P&L.

    Reads directly from local SQLite tables — no upstream API calls — so it
    returns in <100ms. (The old implementation probed five cities first to
    warm caches, which made the endpoint take 90s+ against slow providers.)
    """
    import asyncio
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from weather_ensemble import get_ensemble_status
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, get_ensemble_status)
    except ImportError as e:
        logger.warning(f"Ensemble status unavailable: {e}")
        raise HTTPException(
            status_code=503,
            detail="weather ensemble status function not available on this server",
        )
    except Exception as e:
        logger.exception(f"Ensemble status failed: {e}")
        raise HTTPException(status_code=500, detail="ensemble status failed")


@router.get("/weather/dashboard")
async def weather_dashboard():
    """Dashboard payload for weather.html.

    Aggregates from paper_positions (filtered to archetype='weather') and
    source_city_rmse to produce the KPIs, equity curve, city/segment/price
    breakdowns, forecast accuracy, empirical std, open positions, and recent
    trades that the page consumes. Pure SQLite reads — sub-100ms.
    """
    import asyncio
    import re
    import sqlite3
    from datetime import datetime, timezone
    from collections import defaultdict
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from paper_portfolio import DB_PATH as _DB_PATH  # type: ignore

        def _classify_market(title: str) -> str:
            t = (title or "").lower()
            if "between" in t:
                return "bracket"
            if "or higher" in t or "or above" in t or "or lower" in t or "or below" in t:
                return "threshold"
            return "exact"

        _CITY_RE = re.compile(r"\bin\s+([A-Za-z][A-Za-z\.\s']+?)\s+(?:be\b|on\b)", re.I)

        def _extract_city(title: str) -> str:
            m = _CITY_RE.search(title or "")
            return m.group(1).strip().lower() if m else "unknown"

        def _price_bucket(p: float) -> str:
            if p < 0.05: return "0-5¢"
            if p < 0.10: return "5-10¢"
            if p < 0.20: return "10-20¢"
            if p < 0.40: return "20-40¢"
            if p < 0.60: return "40-60¢"
            return "60¢+"

        _BUCKET_ORDER = ["0-5¢", "5-10¢", "10-20¢", "20-40¢", "40-60¢", "60¢+"]

        def _horizon_bucket_hours(hours: float) -> str:
            if hours < 6: return "0-6h"
            if hours < 24: return "6-24h"
            if hours < 48: return "24-48h"
            return "48h+"

        def _build():
            conn = sqlite3.connect(_DB_PATH)
            conn.row_factory = sqlite3.Row
            try:
                # ── paper_positions: every weather row, ordered by close ──
                rows = conn.execute("""
                    SELECT id, opened_at, closed_at, market_title, side,
                           entry_price, bet_size, edge_pct, pnl, status
                    FROM paper_positions WHERE archetype = 'weather'
                    ORDER BY COALESCE(closed_at, opened_at) ASC
                """).fetchall()

                resolved_statuses = {"won", "lost", "stopped"}
                resolved = [r for r in rows if r["status"] in resolved_statuses]
                open_rows = [r for r in rows if r["status"] == "open"]

                # ── totals (won/lost/stopped → wins/losses, total) ──
                wins = sum(1 for r in resolved if r["status"] == "won")
                losses = sum(1 for r in resolved if r["status"] in ("lost", "stopped"))
                total_pnl = sum((r["pnl"] or 0) for r in resolved)
                best_trade = max((r["pnl"] or 0) for r in resolved) if resolved else 0
                totals = {
                    "total": len(resolved),
                    "wins": wins,
                    "losses": losses,
                    "total_pnl": round(total_pnl, 2),
                    "best_trade": round(best_trade, 2),
                }

                # ── equity_curve: cumulative pnl by close_date ──
                eq = defaultdict(float)
                for r in resolved:
                    if r["closed_at"]:
                        day = r["closed_at"][:10]
                        eq[day] += (r["pnl"] or 0)
                cum = 0.0
                equity_curve = []
                for day in sorted(eq):
                    cum += eq[day]
                    equity_curve.append({"date": day, "pnl": round(cum, 2)})

                # ── city_pnl & segments & price_buckets & open & recent ──
                city_pnl = defaultdict(float)
                seg = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0, "edge_sum": 0.0})
                buck = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
                for r in resolved:
                    city = _extract_city(r["market_title"])
                    mt = _classify_market(r["market_title"])
                    won = 1 if r["status"] == "won" else 0
                    pnl = r["pnl"] or 0
                    city_pnl[city] += pnl
                    sk = (mt, r["side"] or "?")
                    seg[sk]["n"] += 1
                    seg[sk]["wins"] += won
                    seg[sk]["pnl"] += pnl
                    seg[sk]["edge_sum"] += (r["edge_pct"] or 0)
                    bk = (_price_bucket(r["entry_price"] or 0), r["side"] or "?")
                    buck[bk]["n"] += 1
                    buck[bk]["wins"] += won
                    buck[bk]["pnl"] += pnl

                city_pnl_out = [
                    {"city": c, "pnl": round(p, 2)}
                    for c, p in sorted(city_pnl.items(), key=lambda kv: -kv[1])
                ]
                segments_out = [
                    {
                        "market_type": mt, "side": side,
                        "n": v["n"], "wins": v["wins"],
                        "pnl": round(v["pnl"], 2),
                        "avg_edge": round(v["edge_sum"] / v["n"], 4) if v["n"] else 0,
                    }
                    for (mt, side), v in sorted(seg.items(), key=lambda kv: -kv[1]["n"])
                ]
                buckets_out = [
                    {
                        "bucket": b, "side": side,
                        "n": v["n"], "wins": v["wins"],
                        "pnl": round(v["pnl"], 2),
                    }
                    for (b, side), v in sorted(
                        buck.items(),
                        key=lambda kv: (_BUCKET_ORDER.index(kv[0][0]) if kv[0][0] in _BUCKET_ORDER else 99, kv[0][1])
                    )
                ]

                def _row_to_position(r):
                    return {
                        "market_title": r["market_title"] or "",
                        "market_type": _classify_market(r["market_title"]),
                        "side": r["side"],
                        "entry_price": r["entry_price"],
                        "bet_size": r["bet_size"],
                        "edge_pct": r["edge_pct"],
                        "opened_at": r["opened_at"],
                    }

                open_out = [_row_to_position(r) for r in open_rows]

                recent_out = []
                for r in sorted(resolved, key=lambda x: x["closed_at"] or "", reverse=True)[:50]:
                    recent_out.append({
                        "market_title": r["market_title"] or "",
                        "market_type": _classify_market(r["market_title"]),
                        "side": r["side"],
                        "entry_price": r["entry_price"],
                        "bet_size": r["bet_size"],
                        "pnl": round(r["pnl"] or 0, 2),
                        "edge_pct": r["edge_pct"],
                        "status": r["status"],
                    })

                # ── forecast_accuracy: per-city MAE & bias across all sources ──
                acc_out = []
                for row in conn.execute("""
                    SELECT city, COUNT(*) AS n,
                           AVG(ABS(error_f)) AS mae, AVG(error_f) AS bias
                    FROM source_city_rmse
                    WHERE error_f IS NOT NULL
                    GROUP BY city
                """):
                    acc_out.append({
                        "city": row["city"].lower(),
                        "n": row["n"],
                        "mae": round(row["mae"], 2),
                        "bias": round(row["bias"], 2),
                    })

                # ── empirical_std: per-city × horizon bucket ──
                # horizon = (end-of-target-day - logged_at) in hours
                std_out = []
                stdrows = conn.execute("""
                    SELECT city, target_date, logged_at, error_f
                    FROM source_city_rmse WHERE error_f IS NOT NULL
                """).fetchall()
                horizon_groups = defaultdict(list)
                for r in stdrows:
                    try:
                        td = datetime.fromisoformat(r["target_date"] + "T23:59:59+00:00")
                        la = datetime.fromisoformat(r["logged_at"].replace("Z", "+00:00")
                                                    if r["logged_at"] and "T" in r["logged_at"]
                                                    else (r["logged_at"] or "").replace(" ", "T") + "+00:00")
                        hours = (td - la).total_seconds() / 3600.0
                        if hours < 0:
                            continue
                        bucket = _horizon_bucket_hours(hours)
                        horizon_groups[(r["city"].lower(), bucket)].append(r["error_f"])
                    except Exception:
                        continue
                for (city, bucket), errs in horizon_groups.items():
                    n = len(errs)
                    if n < 3:
                        continue
                    mean = sum(errs) / n
                    var = sum((e - mean) ** 2 for e in errs) / n
                    std_out.append({
                        "city": city,
                        "horizon": bucket,
                        "std_err_f": round(var ** 0.5, 2),
                        "n_dates": n,
                    })

                # ── calibration: count of resolved backtest brackets ──
                cal_row = conn.execute(
                    "SELECT COUNT(*) AS n FROM backtest_brackets WHERE actual_high_f IS NOT NULL"
                ).fetchone()
                calibration = {
                    "method": "Isotonic",
                    "n_brackets": cal_row["n"] if cal_row else 0,
                }

                return {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "totals": totals,
                    "open_positions": open_out,
                    "equity_curve": equity_curve,
                    "city_pnl": city_pnl_out,
                    "segments": segments_out,
                    "price_buckets": buckets_out,
                    "forecast_accuracy": acc_out,
                    "empirical_std": std_out,
                    "recent_trades": recent_out,
                    "calibration": calibration,
                }
            finally:
                conn.close()

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _build)
    except Exception as e:
        logger.exception(f"Weather dashboard failed: {e}")
        raise HTTPException(status_code=500, detail="weather dashboard failed")


@router.get("/signals/weather")
async def scan_weather():
    """Scan weather markets on Kalshi + Polymarket against Open-Meteo forecasts."""
    import asyncio
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from weather_scanner import scan_all_weather
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, scan_all_weather)
    except Exception as e:
        logger.exception(f"Weather scan failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/signals/tweets")
async def scan_tweet_counts():
    """Scan tweet count bracket markets using Monte Carlo vs xtracker data."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from tweet_count_scanner import scan_all_tweet_markets
        return scan_all_tweet_markets()
    except Exception as e:
        logger.exception(f"Tweet count scan failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/signals/scorecard/{strategy}")
async def get_strategy_scorecard(strategy: str):
    """Get calibration scorecard for a learning strategy (tweet_count_mc, weather_ensemble)."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from resolution_logger import get_scorecard, load_resolutions
        card = get_scorecard(strategy)
        n = len(load_resolutions(strategy))
        if card:
            return card
        return {"strategy": strategy, "n": n, "message": f"Need 20+ resolutions for scorecard (have {n})"}
    except Exception as e:
        logger.exception(f"Scorecard failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─── Confidence Redesign: Archetype & Kill Rules ─────────────────────

@router.get("/archetype/classify")
async def classify_market_archetype(title: str = Query(...)):
    """Classify a market title into an archetype."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from mispriced_category_signal import classify_archetype, _check_kill_rules
        archetype = classify_archetype(title)
        return {"title": title, "archetype": archetype}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/archetype/kill-check")
async def check_kill_rules(title: str = Query(...), price_cents: int = Query(...)):
    """Check if a market would be killed by archetype kill rules."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from mispriced_category_signal import classify_archetype, _check_kill_rules
        should_kill, reason, archetype = _check_kill_rules(title, price_cents)
        return {
            "title": title,
            "price_cents": price_cents,
            "archetype": archetype,
            "killed": should_kill,
            "reason": reason,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/archetype/wr-buckets")
async def get_wr_buckets():
    """Get empirical win rates by archetype, side, and price zone from resolved trades."""
    try:
        import sqlite3
        db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), '..', 'storage', 'shadow_trades.db')
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row

        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from mispriced_category_signal import classify_archetype

        # Shadow trades
        shadow = db.execute("SELECT market, side, entry_price, outcome, platform FROM shadow_trades WHERE resolved=1").fetchall()
        # Paper trades
        paper = db.execute("SELECT market_title as market, side, entry_price, status, platform FROM paper_positions WHERE status IN ('won','lost')").fetchall()

        buckets = {}
        for t in shadow:
            arch = classify_archetype(t['market'] or '')
            won = t['side'] == t['outcome']
            side = t['side'] or '?'
            price = t['entry_price'] or 0
            zone = 'cheap' if price < 0.45 else 'mid' if price < 0.65 else 'premium' if price < 0.85 else 'expensive'
            
            key = f"{arch}|{side}|{zone}"
            buckets.setdefault(key, {"wins": 0, "total": 0, "archetype": arch, "side": side, "zone": zone})
            buckets[key]["total"] += 1
            if won:
                buckets[key]["wins"] += 1

        for t in paper:
            arch = classify_archetype(t['market'] or '')
            won = t['status'] == 'won'
            side = t['side']
            price = t['entry_price']
            zone = 'cheap' if price < 0.45 else 'mid' if price < 0.65 else 'premium' if price < 0.85 else 'expensive'
            
            key = f"{arch}|{side}|{zone}"
            buckets.setdefault(key, {"wins": 0, "total": 0, "archetype": arch, "side": side, "zone": zone})
            buckets[key]["total"] += 1
            if won:
                buckets[key]["wins"] += 1

        db.close()

        result = []
        for key, b in sorted(buckets.items(), key=lambda x: -x[1]["total"]):
            wr = b["wins"] / b["total"] * 100 if b["total"] > 0 else 0
            result.append({
                "archetype": b["archetype"],
                "side": b["side"],
                "price_zone": b["zone"],
                "wins": b["wins"],
                "losses": b["total"] - b["wins"],
                "total": b["total"],
                "win_rate": round(wr, 1),
            })

        total_resolved = sum(b["total"] for b in buckets.values())
        total_wins = sum(b["wins"] for b in buckets.values())
        return {
            "total_resolved": total_resolved,
            "total_wr": round(total_wins / total_resolved * 100, 1) if total_resolved else 0,
            "buckets": result,
        }
    except Exception as e:
        logger.exception(f"WR buckets failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/archetype/kill-stats")
async def get_kill_stats():
    """Get stats on how many current signals would be killed by rules."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from mispriced_category_signal import get_mispriced_category_signals
        result = get_mispriced_category_signals()
        signals = result.get('signals', [])
        
        killed = [s for s in signals if s.get('killed')]
        alive = [s for s in signals if not s.get('killed')]
        
        kill_reasons = {}
        for s in killed:
            r = s.get('kill_reason', 'unknown')
            kill_reasons[r] = kill_reasons.get(r, 0) + 1
        
        return {
            "total_signals": len(signals),
            "killed": len(killed),
            "alive": len(alive),
            "kill_rate_pct": round(len(killed) / len(signals) * 100, 1) if signals else 0,
            "kill_reasons": kill_reasons,
            "alive_archetypes": {s.get('archetype', '?'): 0 for s in alive},
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/archetype/calibration")
async def get_calibration_audit():
    """Run calibration audit — check if predicted confidence matches actual WR."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from empirical_confidence import calibration_audit
        return calibration_audit()
    except Exception as e:
        logger.exception(f"Calibration audit failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/archetype/evaluate")
async def evaluate_with_empirical(title: str = Query(...), side: str = Query(...), price: float = Query(...)):
    """Evaluate a market using empirical confidence engine."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from empirical_confidence import calculate_empirical_confidence
        return calculate_empirical_confidence(title, side, price)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



# === Basket Arb Endpoints ===

@router.get("/basket-arb")
async def basket_arb_signals():
    """Get sum-to-one basket arbitrage signals."""
    from signals.basket_arb_scanner import get_basket_arb_signals
    return get_basket_arb_signals()


@router.get("/basket-arb/compression")
async def basket_arb_compression():
    """Check if arb spreads are compressed (bot competition)."""
    from signals.basket_arb_scanner import check_spread_compression, _fetch_events
    import json
    events = _fetch_events(limit=50)
    all_markets = []
    for ev in events:
        all_markets.extend(ev.get("markets", []))
    return check_spread_compression(all_markets)


# === Copy-Trade Watcher Endpoints ===

@router.get("/copy-trade")
async def copy_trade_signals():
    """Get whale overlap signals + whale-only markets."""
    from signals.copy_trade_watcher import get_copy_trade_signals
    return get_copy_trade_signals()


@router.get("/copy-trade/whales")
async def copy_trade_whales():
    """Get discovered whale wallets from recent trades."""
    from signals.copy_trade_watcher import discover_whales
    whales = discover_whales()
    return {"whales": whales, "count": len(whales)}


@router.get("/copy-trade/positions")
async def copy_trade_positions():
    """Get aggregated whale positions by market."""
    from signals.copy_trade_watcher import discover_whales, scan_whale_positions
    whales = discover_whales()
    return scan_whale_positions(whales[:15])


@router.get("/portfolio/risk-guards")
async def get_risk_guards():
    """Get status of all risk guard features — Kelly, correlation cap, time decay windows."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from paper_portfolio import get_kelly_status, get_correlation_status
        from time_decay_optimizer import get_optimal_entry_windows
        # CV Kelly haircut
        cv_kelly_data = {}
        try:
            from cv_kelly import calculate_cv_kelly_haircut
            kelly_info = get_kelly_status()
            cv_kelly_data = calculate_cv_kelly_haircut(kelly_info.get("fraction", 0.125))
        except Exception:
            cv_kelly_data = {"note": "insufficient data"}

        return {
            "kelly": get_kelly_status(),
            "cv_kelly": cv_kelly_data,
            "correlation": get_correlation_status(),
            "time_decay_windows": get_optimal_entry_windows()["windows"][:5],
        }
    except Exception as e:
        logger.exception(f"Risk guards failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/signals/strike-scanner")
async def strike_scanner():
    """Scan crypto strike markets for volatility-based mispricing signals."""
    try:
        signals_path = _get_signals_path()
        if signals_path not in sys.path:
            sys.path.insert(0, signals_path)
        from strike_probability import get_calculator
        calc = get_calculator()
        results = calc.scan_all_strikes()
        return {
            "signals": results,
            "count": len(results),
            "strategy": "price_to_strike",
            "min_edge": "10%",
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
    except Exception as e:
        logger.exception(f"Strike scanner failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Alert Analytics
# ============================================================================

ALERTS_LOG = STORAGE_DIR / "alerts.jsonl"


def _load_alerts(days: int = 30) -> list:
    """Load alert records from JSONL, filtered by recency."""
    if not ALERTS_LOG.exists():
        return []
    cutoff = datetime.utcnow() - timedelta(days=days)
    alerts = []
    try:
        with open(ALERTS_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts = rec.get("ts", "")
                    # Parse ISO timestamp (strip timezone for comparison)
                    if ts and ts[:10] >= cutoff.strftime("%Y-%m-%d"):
                        alerts.append(rec)
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return alerts


def _match_outcomes(alerts: list) -> list:
    """Cross-reference position alerts with paper_positions outcomes."""
    import sqlite3
    db_path = STORAGE_DIR / "shadow_trades.db"
    if not db_path.exists():
        return alerts

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        # Get all resolved positions
        rows = conn.execute("""
            SELECT market_title, side, strategy, status, pnl, entry_price, exit_price,
                   close_reason, opened_at, closed_at
            FROM paper_positions
            WHERE status IN ('won', 'lost', 'void')
        """).fetchall()
        conn.close()

        # Build lookup: (market_title_prefix, side, strategy) -> outcome
        outcomes = {}
        for r in rows:
            key = (r["market_title"][:80] if r["market_title"] else "", r["side"], r["strategy"])
            outcomes[key] = {
                "status": r["status"],
                "pnl": r["pnl"],
                "exit_price": r["exit_price"],
                "close_reason": r["close_reason"],
                "closed_at": r["closed_at"],
            }

        # Enrich position_opened alerts with outcomes
        for a in alerts:
            if a.get("type") == "position_opened":
                market = a.get("market", "")[:80]
                key = (market, a.get("side"), a.get("strategy"))
                if key in outcomes:
                    a["outcome"] = outcomes[key]

    except Exception:
        pass

    return alerts


@router.get("/alerts/stats")
async def alert_stats(days: int = Query(30, ge=1, le=365)):
    """Alert analytics — counts, win rates, conversion rates by alert type."""
    alerts = _load_alerts(days)
    if not alerts:
        return {"status": "ok", "message": "No alerts logged yet", "days": days, "total": 0}

    alerts = _match_outcomes(alerts)

    # ── Counts by type ──
    by_type = {}
    for a in alerts:
        t = a.get("type", "unknown")
        if t not in by_type:
            by_type[t] = {"count": 0, "sent": 0, "failed": 0}
        by_type[t]["count"] += 1
        if a.get("sent"):
            by_type[t]["sent"] += 1
        else:
            by_type[t]["failed"] += 1

    # ── Time buckets (24h, 7d, 30d) ──
    now = datetime.utcnow()
    buckets = {"24h": 0, "7d": 0, "30d": 0}
    for a in alerts:
        ts = a.get("ts", "")
        if not ts:
            continue
        try:
            # Handle both Z and +00:00 suffixes
            ts_clean = ts.replace("+00:00", "").replace("Z", "")
            if "." in ts_clean:
                dt = datetime.fromisoformat(ts_clean)
            else:
                dt = datetime.fromisoformat(ts_clean)
            age = now - dt
            if age.total_seconds() < 86400:
                buckets["24h"] += 1
            if age.days < 7:
                buckets["7d"] += 1
            buckets["30d"] += 1
        except Exception:
            buckets["30d"] += 1

    # ── Position alert performance ──
    opened = [a for a in alerts if a.get("type") == "position_opened"]
    with_outcome = [a for a in opened if "outcome" in a]
    wins = [a for a in with_outcome if a["outcome"]["status"] == "won"]
    losses = [a for a in with_outcome if a["outcome"]["status"] == "lost"]

    # By strategy
    strategy_stats = {}
    for a in with_outcome:
        strat = a.get("strategy", "unknown")
        if strat not in strategy_stats:
            strategy_stats[strat] = {"opened": 0, "won": 0, "lost": 0, "void": 0,
                                     "total_pnl": 0.0, "edges_at_entry": []}
        s = strategy_stats[strat]
        s["opened"] += 1
        status = a["outcome"]["status"]
        s[status] = s.get(status, 0) + 1
        pnl = a["outcome"].get("pnl") or 0
        s["total_pnl"] += float(pnl)
        edge = a.get("edge_pct")
        if edge is not None:
            s["edges_at_entry"].append(float(edge))

    # Compute derived metrics
    for strat, s in strategy_stats.items():
        resolved = s["won"] + s["lost"]
        s["win_rate"] = round(s["won"] / resolved, 3) if resolved else None
        s["avg_pnl"] = round(s["total_pnl"] / resolved, 2) if resolved else None
        s["avg_edge_at_entry"] = round(sum(s["edges_at_entry"]) / len(s["edges_at_entry"]), 1) if s["edges_at_entry"] else None
        del s["edges_at_entry"]  # Don't expose raw list

    # ── Signal alert performance (whale, weather, tweet) ──
    signal_types = {}
    for alert_type in ["whale_wall", "weather_shift", "tweet_pace", "edge_signal"]:
        type_alerts = [a for a in alerts if a.get("type") == alert_type]
        if not type_alerts:
            continue
        # Count unique markets alerted
        markets = set()
        for a in type_alerts:
            m = a.get("market", "")[:80]
            if m:
                markets.add(m)
        signal_types[alert_type] = {
            "total_alerts": len(type_alerts),
            "unique_markets": len(markets),
            "avg_per_day": round(len(type_alerts) / max(days, 1), 1),
        }
        # For whale walls, add avg imbalance
        if alert_type == "whale_wall":
            ratios = [a.get("imbalance_ratio", 0) for a in type_alerts if a.get("imbalance_ratio")]
            if ratios:
                signal_types[alert_type]["avg_imbalance"] = round(sum(ratios) / len(ratios), 1)
                signal_types[alert_type]["max_imbalance"] = round(max(ratios), 1)

    # ── Hourly distribution ──
    hour_dist = [0] * 24
    for a in alerts:
        ts = a.get("ts", "")
        try:
            h = int(ts[11:13])
            hour_dist[h] += 1
        except Exception:
            pass

    total_resolved = len(wins) + len(losses)

    return {
        "status": "ok",
        "days": days,
        "total_alerts": len(alerts),
        "volume": buckets,
        "by_type": by_type,
        "positions": {
            "opened": len(opened),
            "resolved": total_resolved,
            "pending": len(opened) - len(with_outcome),
            "won": len(wins),
            "lost": len(losses),
            "win_rate": round(len(wins) / total_resolved, 3) if total_resolved else None,
            "total_pnl": round(sum(float(a["outcome"].get("pnl", 0)) for a in with_outcome), 2),
        },
        "by_strategy": strategy_stats,
        "signal_alerts": signal_types,
        "hourly_distribution": hour_dist,
        "delivery_rate": round(sum(1 for a in alerts if a.get("sent")) / len(alerts), 3) if alerts else 1.0,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


# ── Election Sentiment ───────────────────────────────────────────────────

_election_cache = {"data": None, "ts": 0, "refreshing": False, "artifacts": None}
_election_core_cache = {"data": None, "ts": 0, "artifacts": None}  # fast core-only cache
# Skeleton cache — strict subset of full/core for fast first paint (~80KB gzipped
# vs ~380KB for /core). Keyed by source timestamp so we rebuild only on refresh.
_election_skeleton_cache = {"source_ts": 0, "artifacts": None}

_ELECTION_FRESH_TTL = 900     # 15 min — serve without refresh
_ELECTION_STALE_TTL = 7200    # 2 hr  — serve stale, refresh in background (extended from 1hr)


def _trim_election_report(report: dict) -> dict:
    """Shallow-trim large lists in the election report for wire transfer.

    Policy_pulse categories contain thousands of markets but the UI only
    renders the top 12 per section. Keep top 50 by volume so CLARITY-relevant
    markets (which rank high by volume) comfortably survive. The Python
    in-memory cache still holds the full data — this trim only runs once per
    cache refresh when we build the wire artifacts.
    """
    if not isinstance(report, dict):
        return report
    trimmed = dict(report)
    insights = report.get("insights")
    if isinstance(insights, dict):
        new_insights = dict(insights)
        pp = insights.get("policy_pulse")
        if isinstance(pp, dict):
            new_pp = dict(pp)
            for cat in ("scotus", "congress", "trade_tariffs", "foreign_policy",
                        "domestic_policy", "macro_economic"):
                lst = pp.get(cat)
                if isinstance(lst, list) and len(lst) > 50:
                    new_pp[cat] = sorted(
                        lst, key=lambda m: (m.get("volume") or 0), reverse=True
                    )[:50]
            new_insights["policy_pulse"] = new_pp
        trimmed["insights"] = new_insights
    return trimmed


def _build_election_skeleton(report: dict) -> dict:
    """Build a minimal above-the-fold payload from a full election report.

    Target: ~80KB gzipped (vs ~380KB for the full /core payload) so first
    paint lands in ~1.1s (basically TTFB). Client sees `skeleton: true` +
    `core_only: true` in the response and fetches /signals/elections in the
    background to hydrate deferred sections (movers, policy_pulse, crypto
    money, gdelt, smart money, etc.).

    Strict subset strategy:
    - Keep all `presidential`, `senate`, `governor` markets (needed for
      hemicycles + state-by-state above-fold views). These are the races
      the page is actually *about*.
    - Keep top-400 by volume from the remaining categories (primary, house,
      other) so tabs have enough data to render meaningfully.
    - Drop `deltas`, `top_movers`, and all of `insights` except `midterm`
      (the only insights field read by above-fold renders).
    """
    if not isinstance(report, dict):
        return report
    markets = report.get("markets") or []
    keep_all = [m for m in markets if m.get("race_category") in ("presidential", "senate", "governor")]
    others = [m for m in markets if m.get("race_category") not in ("presidential", "senate", "governor")]
    top_others = sorted(others, key=lambda m: (m.get("volume") or 0), reverse=True)[:400]
    skel_markets = keep_all + top_others

    insights = report.get("insights") or {}
    skel_insights = {"midterm": insights.get("midterm")}

    return {
        "timestamp": report.get("timestamp"),
        "summary": report.get("summary"),
        "markets": skel_markets,
        "insights": skel_insights,
        "skeleton": True,
        "core_only": True,  # triggers client Phase 2 upgrade fetch
        "full_market_count": len(markets),
    }


def _build_election_artifacts(report: dict) -> dict:
    """Serialize + gzip + hash an election report once, so every cache hit
    just returns pre-built bytes instead of re-encoding a 7MB dict + gzipping
    on every request. Took cache-hit TTFB from ~1.3s to <50ms in benchmarks.
    """
    import gzip as _gzip
    import hashlib as _hashlib
    trimmed = _trim_election_report(report)
    body = json.dumps(trimmed, separators=(",", ":"), default=str).encode("utf-8")
    body_gz = _gzip.compress(body, compresslevel=6)
    etag = '"' + _hashlib.md5(body).hexdigest()[:16] + '"'
    return {"body": body, "body_gz": body_gz, "etag": etag}


def _serve_election_cache(cache_dict: dict, request, max_age: int):
    """Return a FastAPI Response for a cached election report, honoring
    Accept-Encoding (gzip) and If-None-Match (ETag) for zero-copy 304s.
    """
    from fastapi.responses import Response
    if cache_dict.get("artifacts") is None:
        cache_dict["artifacts"] = _build_election_artifacts(cache_dict["data"])
    art = cache_dict["artifacts"]
    etag = art["etag"]
    # 304 Not Modified for warm browsers — skips the body entirely
    inm = request.headers.get("if-none-match") if request else None
    if inm and inm == etag:
        return Response(
            status_code=304,
            headers={
                "ETag": etag,
                "Cache-Control": f"public, max-age={max(0, max_age)}",
                "Vary": "Accept-Encoding",
            },
        )
    accepts_gzip = "gzip" in (request.headers.get("accept-encoding", "") if request else "")
    if accepts_gzip:
        return Response(
            content=art["body_gz"],
            media_type="application/json",
            headers={
                "Content-Encoding": "gzip",
                "ETag": etag,
                "Cache-Control": f"public, max-age={max(0, max_age)}",
                "Vary": "Accept-Encoding",
            },
        )
    return Response(
        content=art["body"],
        media_type="application/json",
        headers={
            "ETag": etag,
            "Cache-Control": f"public, max-age={max(0, max_age)}",
            "Vary": "Accept-Encoding",
        },
    )


async def _refresh_election_cache():
    """Refresh election cache in background thread (non-blocking)."""
    import asyncio
    import time
    if _election_cache["refreshing"]:
        return  # already refreshing, skip
    _election_cache["refreshing"] = True
    try:
        loop = asyncio.get_event_loop()
        from signals.election_tracker import generate_report
        report = await loop.run_in_executor(None, generate_report)
        _election_cache["data"] = report
        _election_cache["ts"] = time.time()
        _election_cache["artifacts"] = None  # invalidate pre-serialized bytes
        # Core cache is a strict subset — also invalidate its artifacts so a
        # future core hit rebuilds from the refreshed data (if the core
        # cache itself is stale, the core handler will pull from the full
        # cache via its existing fallback branch).
        _election_core_cache["artifacts"] = None
        # Skeleton is derived from whichever warm cache exists — force rebuild
        # on next /core hit so it reflects the refreshed data.
        _election_skeleton_cache["artifacts"] = None
        _election_skeleton_cache["source_ts"] = 0
        logger.info("Election cache refreshed in background")
    except Exception as e:
        import traceback
        logger.error("Background election refresh failed: %s\n%s", e, traceback.format_exc())
    finally:
        _election_cache["refreshing"] = False


async def prewarm_election_cache():
    """Pre-warm election cache on startup. Call from lifespan."""
    logger.info("Pre-warming election cache...")
    await _refresh_election_cache()


@router.get("/signals/elections")
async def get_election_markets(request: Request):
    """US election market data from Polymarket + Kalshi with week-over-week deltas."""
    import asyncio
    import time

    now = time.time()
    age = now - _election_cache["ts"] if _election_cache["data"] else float("inf")

    # Fresh cache — return pre-serialized bytes
    if _election_cache["data"] and age < _ELECTION_FRESH_TTL:
        max_age = int(_ELECTION_FRESH_TTL - age)
        return _serve_election_cache(_election_cache, request, max_age)

    # Stale cache — return immediately but kick off background refresh
    if _election_cache["data"] and age < _ELECTION_STALE_TTL:
        asyncio.create_task(_refresh_election_cache())
        return _serve_election_cache(_election_cache, request, 0)

    # No cache or expired beyond stale TTL — must fetch synchronously
    try:
        loop = asyncio.get_event_loop()
        from signals.election_tracker import generate_report
        report = await loop.run_in_executor(None, generate_report)
        _election_cache["data"] = report
        _election_cache["ts"] = now
        _election_cache["artifacts"] = None  # force rebuild on first serve
        return _serve_election_cache(_election_cache, request, _ELECTION_FRESH_TTL)
    except Exception as e:
        import traceback
        logger.error("Election data fetch failed: %s\n%s", e, traceback.format_exc())
        raise HTTPException(status_code=502, detail=str(e))


def _serve_election_skeleton(source_cache: dict, request, max_age: int):
    """Build (or reuse) skeleton artifacts from a source cache and serve them.

    The skeleton is keyed by `source_cache['ts']` so we rebuild only when the
    underlying report changes. Every request beyond the first is a pre-built
    ~80KB gzipped byte blob — effectively free on the server side.
    """
    from fastapi.responses import Response
    source_ts = source_cache.get("ts") or 0
    if (
        _election_skeleton_cache.get("artifacts") is None
        or _election_skeleton_cache.get("source_ts") != source_ts
    ):
        skel = _build_election_skeleton(source_cache["data"])
        _election_skeleton_cache["artifacts"] = _build_election_artifacts(skel)
        _election_skeleton_cache["source_ts"] = source_ts

    art = _election_skeleton_cache["artifacts"]
    etag = art["etag"]
    inm = request.headers.get("if-none-match") if request else None
    if inm and inm == etag:
        return Response(
            status_code=304,
            headers={
                "ETag": etag,
                "Cache-Control": f"public, max-age={max(0, max_age)}",
                "Vary": "Accept-Encoding",
            },
        )
    accepts_gzip = "gzip" in (request.headers.get("accept-encoding", "") if request else "")
    if accepts_gzip:
        return Response(
            content=art["body_gz"],
            media_type="application/json",
            headers={
                "Content-Encoding": "gzip",
                "ETag": etag,
                "Cache-Control": f"public, max-age={max(0, max_age)}",
                "Vary": "Accept-Encoding",
            },
        )
    return Response(
        content=art["body"],
        media_type="application/json",
        headers={
            "ETag": etag,
            "Cache-Control": f"public, max-age={max(0, max_age)}",
            "Vary": "Accept-Encoding",
        },
    )


@router.get("/signals/elections/core")
async def get_election_markets_core(request: Request):
    """Fast skeleton payload for first paint (~80KB gzipped).

    Returns a strict subset of the full election report — only the fields
    needed for above-the-fold renders (summary, core race markets,
    insights.midterm). Client sees `skeleton: true` in the response and
    fetches /signals/elections in the background to hydrate the full list,
    deltas, movers, policy_pulse, etc.
    """
    import asyncio
    import time

    now = time.time()

    # Prefer the full cache as the skeleton source — it's always a superset
    # of /core and gives us the freshest data possible for the skeleton.
    full_age = now - _election_cache["ts"] if _election_cache["data"] else float("inf")
    if _election_cache["data"] and full_age < _ELECTION_STALE_TTL:
        # If stale, kick off a background refresh but still serve the skeleton
        if full_age >= _ELECTION_FRESH_TTL:
            asyncio.create_task(_refresh_election_cache())
        return _serve_election_skeleton(_election_cache, request, max(0, int(300 - min(full_age, 300))))

    # Fallback to the core cache as skeleton source
    core_age = now - _election_core_cache["ts"] if _election_core_cache["data"] else float("inf")
    if _election_core_cache["data"] and core_age < 300:
        return _serve_election_skeleton(_election_core_cache, request, max(0, int(300 - core_age)))

    # Cold start — generate a core-only report (fast, no GDELT/FEC/etc. APIs),
    # cache it, then serve the skeleton derived from it.
    try:
        loop = asyncio.get_event_loop()
        from signals.election_tracker import generate_report
        report = await loop.run_in_executor(None, lambda: generate_report(core_only=True))
        _election_core_cache["data"] = report
        _election_core_cache["ts"] = now
        _election_core_cache["artifacts"] = None  # force rebuild on first serve
        # Also kick off full refresh in background
        asyncio.create_task(_refresh_election_cache())
        return _serve_election_skeleton(_election_core_cache, request, 300)
    except Exception as e:
        logger.error("Election core fetch failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))


# Keywords that identify a policy market as CLARITY Act / crypto regulation relevant.
# NOTE: "kalshi" and "polymarket" are intentionally NOT in this list — they appear
# in resolution boilerplate on virtually every market of that platform, so they
# match everything and pollute the filter. Use ticker prefixes for platform scoping.
_CLARITY_MARKET_KEYWORDS = (
    "clarity act", "cftc", "crypto market structure",
    "crypto regulat", "crypto legislat", "digital asset", "stablecoin",
    "h.r. 3633", "hr 3633", "fit21", "market structure bill",
    "market structure legislation", "cftc oversight", "sec vs cftc",
    # Broader CLARITY-adjacent terms
    "crypto bill", "crypto law", "crypto tax", "capital gains on crypto",
    "cftc vs sec", "cftc authority", "sec authority over crypto",
    "bitcoin etf", "crypto etf", "defi regulat", "genius act",
)

# Passage verbs — only count when co-occurring with a crypto anchor.
# Prevents tariff / AI / immigration bills from matching.
_CLARITY_PASSAGE_VERBS = (
    "signed into law", "become law", "becomes law", "pass the senate",
    "pass the house", "passes the senate", "passes the house",
)
_CLARITY_CRYPTO_ANCHORS = (
    "crypto", "digital asset", "market structure", "stablecoin",
    "cftc", "clarity act", "h.r. 3633", "hr 3633", "fit21",
)

# Kalshi ticker prefixes that are direct CLARITY/crypto-structure hits.
# Check these against market["ticker"] — they carry authority even when the
# question text is written in a way that doesn't mention "CLARITY".
_CLARITY_TICKER_PREFIXES = (
    "KXCRYPTOSTRUCTURE",  # Crypto market structure ladder (FEB1/MAR/.../-27)
    "KXCLARITYACT",       # Direct CLARITY Act series (currently empty but reserved)
    "KXCFTC",             # CFTC authority / SEC-vs-CFTC
    "KXFIT21",            # FIT21 predecessor bill
    "KXCLARITY",          # Legacy catch-all
)

# Noise filter: markets that mention "crypto" but aren't about policy/legislation.
# Also catches sports-event-contract markets that slip in via the "event contract" keyword.
_CLARITY_NOISE_PATTERNS = (
    "attend", "conference", "speak at", "will be at",
    "price of", "reach $", "hit $", "cross $",
    "sports event contract", "sports-event-contract",
    "nfl", "nba", "mlb", "nhl", "ncaa",
)


# Passage-market predicate — used to decide which markets get price-history attached.
def _is_passage_market(q: str) -> bool:
    q = (q or "").lower()
    passage_terms = (
        "signed into law", "become law", "becomes law", "becomes-law",
        "pass the senate", "pass the house", "passes the senate", "passes the house",
        "pass senate", "pass house", "passes senate", "passes house",
    )
    # Broad "clarity act" clause — paired with an action verb or year
    if "clarity act" in q and any(w in q for w in ("sign", "law", "pass", "vote", "2026", "2027")):
        return True
    return any(t in q for t in passage_terms)


def _filter_clarity_markets(policy_pulse: dict) -> list:
    """Extract CLARITY-relevant markets from policy_pulse, deduped by question.

    Live passage markets (the ones the hero chart cares about) also get a
    `price_history` field attached via Polymarket's CLOB prices-history API.
    """
    if not isinstance(policy_pulse, dict):
        return []
    all_markets = []
    for cat in ("scotus", "congress", "trade_tariffs", "foreign_policy",
                "domestic_policy", "macro_economic"):
        for m in (policy_pulse.get(cat) or []):
            all_markets.append(m)

    relevant = []
    seen = set()
    for m in all_markets:
        q = (m.get("question") or "").lower()
        ticker = (m.get("ticker") or "").upper()
        # Match text across question + rules + event title. rules_secondary is
        # a gold mine on Kalshi — it often names the exact bill (CLARITY, FIT21)
        # when the question itself is phrased generically ("crypto market
        # structure bill becomes law").
        rules1 = (m.get("rules_primary") or "").lower()
        rules2 = (m.get("rules_secondary") or "").lower()
        evt_t = (m.get("event_title") or "").lower()
        haystack = " ".join([q, rules1, rules2, evt_t])

        ticker_match = any(ticker.startswith(p) for p in _CLARITY_TICKER_PREFIXES)
        keyword_match = any(kw in haystack for kw in _CLARITY_MARKET_KEYWORDS)
        # Passage verbs only count when a crypto anchor is present in the same
        # haystack — otherwise "becomes law" matches every tariff/AI/immigration bill.
        passage_match = (
            any(v in haystack for v in _CLARITY_PASSAGE_VERBS)
            and any(a in haystack for a in _CLARITY_CRYPTO_ANCHORS)
        )
        if not (ticker_match or keyword_match or passage_match):
            continue
        # Drop noise matches unless the ticker is a direct hit
        if not ticker_match and any(np in q for np in _CLARITY_NOISE_PATTERNS):
            continue
        # Drop SCOTUS sports-event-contract markets (they match "event contract" kw)
        if "scotus" in q and "sports" in q:
            continue
        # Dedupe by (platform, per-market-id). On Kalshi, `id` is the
        # per-market ticker (e.g. KXCRYPTOSTRUCTURE-26JAN-MAY) while
        # `ticker` may be the event-level ticker (KXCRYPTOSTRUCTURE-26JAN)
        # which collapses ladder rungs. Prefer `id` for dedupe so ladder
        # rungs survive. Polymarket rows without id/ticker fall back to
        # question prefix.
        plat = (m.get("platform") or "")
        dedupe_id = (m.get("id") or m.get("ticker") or "").upper() or q[:60]
        key = (plat, dedupe_id)
        if key in seen:
            continue
        seen.add(key)
        # Strip heavy fields we don't need for clarity.html.
        # On Kalshi, prefer the per-market ticker (stored in `id`) so the UI
        # can distinguish ladder rungs like -FEB1 vs -MAY vs -27. The raw
        # event-level ticker is preserved in event_ticker for grouping.
        out_ticker = m.get("ticker")
        if (m.get("platform") == "kalshi") and m.get("id"):
            out_ticker = m.get("id")
        relevant.append({
            "id": m.get("id"),
            "question": m.get("question"),
            "ticker": out_ticker,
            "event_ticker": m.get("event_ticker") or m.get("ticker"),
            "slug": m.get("slug"),
            "platform": m.get("platform"),
            "volume": m.get("volume"),
            "outcomes": m.get("outcomes"),
            "end_date": m.get("end_date"),
            "event_title": m.get("event_title"),
            "rules_secondary": (m.get("rules_secondary") or "")[:400],
        })
    relevant.sort(key=lambda x: -(x.get("volume") or 0))
    relevant = relevant[:60]

    # Attach price history to live Polymarket passage markets (usually 1–3).
    # Kalshi isn't supported yet — needs a different history endpoint.
    try:
        from signals.polymarket_price_history import get_price_history
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        for r in relevant:
            if r.get("platform") != "polymarket":
                continue
            if not _is_passage_market(r.get("question") or ""):
                continue
            ed = r.get("end_date") or ""
            try:
                end_dt = datetime.fromisoformat(ed.replace("Z", "+00:00"))
                if end_dt <= now:
                    continue  # expired
            except Exception:
                continue
            slug = r.get("slug")
            if not slug:
                continue
            pts = get_price_history(slug)
            if pts:
                r["price_history"] = pts
    except Exception as e:
        logger.warning("price history enrichment failed: %s", e)

    return relevant


@router.get("/signals/clarity")
async def get_clarity_widget_data():
    """Lightweight payload for the CLARITY Act tracker widget.

    Returns only CLARITY-relevant markets + crypto industry money overlay.
    Reads from on-disk caches to avoid recomputing the full election overlay.
    Typical payload: &lt;150KB (vs ~3.2MB for /signals/elections).
    """
    import json as _json
    from fastapi.responses import JSONResponse

    out = {
        "timestamp": None,
        "clarity_markets": [],
        "policy_pulse": {"cross_spreads": []},
        "crypto_money": {"fec_pacs": {"committees": [], "grand_total_spend": 0},
                         "lda_clarity": {"clients": [], "total_spend": 0},
                         "fairshake_funders": {"top_funders": [], "top_funders_total": 0},
                         "vote_alignment": {"matched_recipients": 0, "vote_meta": {}}},
        "clarity_bills": {"congress": 119, "bills": []},
        "source": "disk_cache",
    }

    # Bill status overlay (GovTrack — has its own 6h file cache)
    try:
        from signals.congress_bill_tracker import build_clarity_bills_overlay
        out["clarity_bills"] = build_clarity_bills_overlay()
    except Exception as e:
        logger.warning("clarity_bills overlay failed: %s", e)

    # In-memory cache (freshest) → fall back to disk caches
    policy_pulse = None
    if _election_cache.get("data"):
        ins = _election_cache["data"].get("insights") or {}
        policy_pulse = ins.get("policy_pulse")
        out["crypto_money"] = ins.get("crypto_money") or out["crypto_money"]
        out["timestamp"] = _election_cache["data"].get("timestamp")
        out["source"] = "memory_cache"

    if not policy_pulse:
        try:
            pp_path = Path(__file__).parent.parent.parent / "storage" / "policy_pulse_cache.json"
            if pp_path.exists():
                cached = _json.loads(pp_path.read_text())
                policy_pulse = cached.get("policy_pulse") or cached
        except Exception as e:
            logger.warning("policy_pulse cache read failed: %s", e)

    if out["crypto_money"]["fec_pacs"].get("grand_total_spend", 0) == 0:
        try:
            cm_path = Path(__file__).parent.parent.parent / "storage" / "crypto_money_cache.json"
            if cm_path.exists():
                out["crypto_money"] = _json.loads(cm_path.read_text())
        except Exception as e:
            logger.warning("crypto_money cache read failed: %s", e)

    if policy_pulse:
        out["clarity_markets"] = _filter_clarity_markets(policy_pulse)
        # Pass through cross_spreads (lightweight, used for platform compare)
        out["policy_pulse"]["cross_spreads"] = (policy_pulse.get("cross_spreads") or [])[:20]

    return JSONResponse(
        content=out,
        headers={"Cache-Control": "public, max-age=300, stale-while-revalidate=900"},
    )

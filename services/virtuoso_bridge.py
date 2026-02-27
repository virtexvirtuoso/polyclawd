"""
Virtuoso MCP Bridge — Phase 2

Connects Virtuoso's derivatives intelligence to Polymarket short-duration markets.
Maps directional signals (fusion, funding, regime) to 5/15-min "Up" or "Down" bets.

This is the brain that the $134→$200K bot never had.
"""

import json
import subprocess
import logging
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

# Timeout for mcporter calls (seconds)
MCP_TIMEOUT = 20


# ============================================================================
# MCP Call Wrapper
# ============================================================================

def _mcp_call(tool: str, args: dict = None) -> Optional[Dict]:
    """Call a Virtuoso MCP tool via mcporter CLI.
    
    Returns parsed dict or None on error.
    The response is markdown-formatted text, so we parse what we can.
    """
    try:
        import shutil
        mcporter_bin = shutil.which("mcporter") or "/usr/bin/mcporter"
        cmd = [mcporter_bin, "call", f"virtuoso.{tool}"]
        if args:
            cmd += ["--args", json.dumps(args)]
        
        import os
        env = os.environ.copy()
        # Ensure system paths + node available (uvicorn service has restricted PATH)
        for p in ["/usr/bin", "/usr/local/bin", "/usr/lib/node_modules/.bin"]:
            if p not in env.get("PATH", ""):
                env["PATH"] = p + ":" + env.get("PATH", "")
        # mcporter needs HOME to find its config
        if "HOME" not in env:
            env["HOME"] = "/home/linuxuser"
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=MCP_TIMEOUT,
            env=env,
            cwd=env.get("HOME", "/home/linuxuser"),
        )
        
        if result.returncode != 0:
            logger.warning(f"MCP call {tool} failed: {result.stderr[:200]}")
            return None
        
        output = result.stdout.strip()
        if "❌ **Error:**" in output:
            logger.warning(f"MCP {tool} returned error: {output[:200]}")
            return None
        
        return {"raw": output, "tool": tool, "timestamp": datetime.utcnow().isoformat()}
    
    except subprocess.TimeoutExpired:
        logger.warning(f"MCP call {tool} timed out")
        return None
    except Exception as e:
        logger.warning(f"MCP call {tool} exception: {e}")
        return None


# ============================================================================
# Signal Parsers — extract structured data from markdown responses
# ============================================================================

def _parse_fusion_signal(raw: str) -> Dict:
    """Parse fusion signal markdown into structured data."""
    result = {
        "direction": "NEUTRAL",
        "score": 0.0,
        "confidence": 50,
        "entry": "WAIT",
        "win_rate": 50,
        "components": {},
    }
    
    for line in raw.split("\n"):
        line = line.strip()
        if "**Direction:**" in line:
            if "LONG" in line.upper():
                result["direction"] = "LONG"
            elif "SHORT" in line.upper():
                result["direction"] = "SHORT"
            elif "NEUTRAL" in line.upper():
                result["direction"] = "NEUTRAL"
            # Extract strength hint
            if "strong" in line.lower():
                result["strength"] = "strong"
            elif "weak" in line.lower():
                result["strength"] = "weak"
            else:
                result["strength"] = "moderate"
        
        elif "**Score:**" in line:
            try:
                score_str = line.split("**Score:**")[1].strip()
                result["score"] = float(score_str.split()[0])
            except (ValueError, IndexError):
                pass
        
        elif "**Confidence:**" in line:
            try:
                conf_str = line.split("**Confidence:**")[1].strip()
                result["confidence"] = int(conf_str.replace("%", "").split()[0])
            except (ValueError, IndexError):
                pass
        
        elif "**Entry:**" in line:
            if "LONG" in line.upper():
                result["entry"] = "LONG"
            elif "SHORT" in line.upper():
                result["entry"] = "SHORT"
            else:
                result["entry"] = "WAIT"
        
        elif "**Est. Win Rate:**" in line:
            try:
                wr_str = line.split("**Est. Win Rate:**")[1].strip()
                result["win_rate"] = int(wr_str.replace("%", "").split()[0])
            except (ValueError, IndexError):
                pass
        
        elif "FR:" in line and "OI:" in line:
            # Component line like "FR: 0.0 | OI: 0.0 | LSR: 0.0 | CVD: 0.0"
            for part in line.split("|"):
                part = part.strip()
                if ":" in part:
                    key, val = part.split(":", 1)
                    try:
                        result["components"][key.strip()] = float(val.strip())
                    except ValueError:
                        pass
    
    return result


def _parse_regime(raw: str) -> Dict:
    """Parse market regime markdown."""
    result = {
        "bias": "UNKNOWN",
        "confidence": 50,
        "high_volatility_count": 0,
        "recommendation": "",
    }
    
    for line in raw.split("\n"):
        line = line.strip()
        if "**Overall Bias:**" in line:
            bias_part = line.split("**Overall Bias:**")[1].strip()
            result["bias"] = bias_part.split("(")[0].strip().upper()
            if "confidence:" in bias_part:
                try:
                    result["confidence"] = float(bias_part.split("confidence:")[1].replace("%)", "").strip())
                except ValueError:
                    pass
        
        elif "**Recommendation:**" in line:
            result["recommendation"] = line.split("**Recommendation:**")[1].strip()
        
        elif "high_volatility:" in line.lower():
            try:
                result["high_volatility_count"] = int(line.split(":")[-1].strip())
            except ValueError:
                pass
    
    return result


def _parse_kill_switch(raw: str) -> Dict:
    """Parse kill switch status."""
    result = {
        "active": False,
        "trading_allowed": True,
        "state": "MONITORING",
    }
    
    for line in raw.split("\n"):
        line = line.strip()
        if "**State:**" in line:
            if "TRIGGERED" in line.upper() or "KILLED" in line.upper():
                result["active"] = True
                result["trading_allowed"] = False
                result["state"] = "TRIGGERED"
            else:
                result["state"] = "MONITORING"
        
        elif "**Trading Active:**" in line:
            result["trading_allowed"] = "YES" in line.upper() or "✅" in line
    
    return result


def _parse_manipulation(raw: str) -> Dict:
    """Parse manipulation alerts."""
    result = {
        "alerts_active": False,
        "alert_count": 0,
        "details": [],
    }
    
    if "No Manipulation Alerts" in raw or "No suspicious" in raw:
        return result
    
    result["alerts_active"] = True
    # Count alert entries
    for line in raw.split("\n"):
        if "⚠️" in line or "🚨" in line:
            result["alert_count"] += 1
            result["details"].append(line.strip())
    
    return result


# ============================================================================
# Directional Signal — The Core Bridge
# ============================================================================

@dataclass
class DirectionalSignal:
    """Virtuoso-derived directional signal for a Polymarket short-duration market."""
    asset: str  # BTC, ETH
    direction: str  # UP, DOWN, NEUTRAL
    confidence: int  # 0-100
    strength: str  # strong, moderate, weak
    polymarket_side: str  # "Yes" (buy Up) or "No" (buy Down) or "SKIP"
    
    # Component signals
    fusion_direction: str
    fusion_score: float
    fusion_confidence: int
    regime_bias: str
    regime_volatility: str  # high, normal, low
    
    # Risk flags
    kill_switch_active: bool
    manipulation_detected: bool
    should_trade: bool
    skip_reason: Optional[str]
    
    # Sizing hint
    conviction: str  # high, medium, low
    suggested_kelly_fraction: float  # 0.0 to 0.25
    
    timestamp: str


def get_directional_signal(asset: str = "BTC") -> DirectionalSignal:
    """
    Get Virtuoso-powered directional signal for a crypto asset.
    
    Combines:
    - Fusion signal (FR + OI + LSR + CVD) → primary direction
    - Market regime → volatility filter (high vol = bigger oracle lag = trade more)
    - Kill switch + manipulation → circuit breakers
    
    Maps to Polymarket: LONG → buy "Up" side, SHORT → buy "Down" side
    
    Args:
        asset: BTC or ETH
    
    Returns:
        DirectionalSignal with trade recommendation
    """
    symbol = asset.upper()
    if symbol in ("BITCOIN",):
        symbol = "BTC"
    if symbol in ("ETHEREUM",):
        symbol = "ETH"
    
    # Fetch signals in parallel would be ideal but subprocess is blocking
    # so we fetch sequentially — still fast enough for 5-min markets
    fusion_raw = _mcp_call("get_perps_fusion_signal", {"symbol": symbol})
    regime_raw = _mcp_call("get_market_regime")
    kill_raw = _mcp_call("get_kill_switch_status")
    manip_raw = _mcp_call("get_manipulation_alerts")
    
    # Parse responses
    fusion = _parse_fusion_signal(fusion_raw["raw"]) if fusion_raw else {
        "direction": "NEUTRAL", "score": 0.0, "confidence": 50,
        "entry": "WAIT", "strength": "weak", "components": {}
    }
    
    regime = _parse_regime(regime_raw["raw"]) if regime_raw else {
        "bias": "UNKNOWN", "confidence": 50, "high_volatility_count": 0
    }
    
    kill = _parse_kill_switch(kill_raw["raw"]) if kill_raw else {
        "active": False, "trading_allowed": True
    }
    
    manip = _parse_manipulation(manip_raw["raw"]) if manip_raw else {
        "alerts_active": False, "alert_count": 0
    }
    
    # === Decision Logic ===
    
    # 1. Risk gates (hard stops)
    should_trade = True
    skip_reason = None
    
    if kill["active"] or not kill.get("trading_allowed", True):
        should_trade = False
        skip_reason = "Kill switch triggered"
    
    if manip["alerts_active"]:
        should_trade = False
        skip_reason = f"Manipulation detected ({manip['alert_count']} alerts)"
    
    # 2. Direction mapping
    direction = "NEUTRAL"
    if fusion["direction"] == "LONG" or fusion["entry"] == "LONG":
        direction = "UP"
    elif fusion["direction"] == "SHORT" or fusion["entry"] == "SHORT":
        direction = "DOWN"
    
    # 3. Confidence scoring
    confidence = fusion["confidence"]
    
    # Boost confidence if regime supports direction
    if regime["bias"] == "BULLISH" and direction == "UP":
        confidence = min(95, confidence + 10)
    elif regime["bias"] == "BEARISH" and direction == "DOWN":
        confidence = min(95, confidence + 10)
    elif regime["bias"] == "CAUTION":
        confidence = max(20, confidence - 10)
    
    # 4. Volatility assessment (high vol = good for HF)
    hv_count = regime.get("high_volatility_count", 0)
    if hv_count >= 5:
        regime_vol = "high"
    elif hv_count >= 2:
        regime_vol = "normal"
    else:
        regime_vol = "low"
    
    # Low volatility = small oracle lag = skip
    if regime_vol == "low" and confidence < 70:
        should_trade = False
        skip_reason = "Low volatility regime — oracle lag too small for edge"
    
    # 5. Polymarket side mapping
    if direction == "UP":
        poly_side = "Yes"  # Buy the "Up" outcome
    elif direction == "DOWN":
        poly_side = "No"   # Buy the "Down" outcome (or sell "Up")
    else:
        poly_side = "SKIP"
        if should_trade:
            should_trade = False
            skip_reason = "No directional signal (NEUTRAL)"
    
    # 6. Conviction & sizing
    strength = fusion.get("strength", "weak")
    if confidence >= 75 and abs(fusion["score"]) > 0.5:
        conviction = "high"
        kelly = 0.20  # 20% of bankroll
    elif confidence >= 60 and abs(fusion["score"]) > 0.2:
        conviction = "medium"
        kelly = 0.10
    else:
        conviction = "low"
        kelly = 0.05
    
    # Scale down in caution regime
    if regime["bias"] == "CAUTION":
        kelly *= 0.5
    
    return DirectionalSignal(
        asset=symbol,
        direction=direction,
        confidence=confidence,
        strength=strength,
        polymarket_side=poly_side,
        fusion_direction=fusion["direction"],
        fusion_score=fusion["score"],
        fusion_confidence=fusion["confidence"],
        regime_bias=regime["bias"],
        regime_volatility=regime_vol,
        kill_switch_active=kill["active"],
        manipulation_detected=manip["alerts_active"],
        should_trade=should_trade,
        skip_reason=skip_reason,
        conviction=conviction,
        suggested_kelly_fraction=round(kelly, 3),
        timestamp=datetime.utcnow().isoformat(),
    )


# ============================================================================
# Multi-Asset Scan
# ============================================================================

def scan_all_assets() -> Dict:
    """Get directional signals for all supported assets."""
    assets = ["BTC", "ETH"]
    signals = {}
    
    for asset in assets:
        try:
            sig = get_directional_signal(asset)
            signals[asset] = asdict(sig)
        except Exception as e:
            logger.error(f"Signal fetch failed for {asset}: {e}")
            signals[asset] = {"error": str(e), "asset": asset}
    
    # Summary
    tradeable = [s for s in signals.values() if isinstance(s, dict) and s.get("should_trade")]
    
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "signals": signals,
        "summary": {
            "total_assets": len(assets),
            "tradeable": len(tradeable),
            "blocked": len(assets) - len(tradeable),
        },
        "phase": "Phase 2 — Virtuoso Bridge",
    }


# ============================================================================
# Combined: Signal + Market Matching
# ============================================================================

def match_signals_to_markets() -> Dict:
    """
    Match Virtuoso directional signals to available Polymarket HF markets.
    
    This is the full pipeline:
    1. Get directional signal from Virtuoso MCP
    2. Discover available 5/15-min markets
    3. Match signals to markets
    4. Output: which markets to trade, which side, at what conviction
    """
    from odds.hf_scanner import discover_hf_markets
    
    # Get signals
    signals = {}
    for asset in ["BTC", "ETH"]:
        try:
            signals[asset] = get_directional_signal(asset)
        except Exception as e:
            logger.error(f"Signal error {asset}: {e}")
    
    # Get markets
    markets = discover_hf_markets()
    
    # Match
    opportunities = []
    for market in markets:
        signal = signals.get(market.asset)
        if not signal:
            continue
        
        if not signal.should_trade:
            continue
        
        opportunities.append({
            "market": {
                "question": market.question,
                "slug": market.slug,
                "asset": market.asset,
                "duration": market.duration_hint,
                "yes_price": market.yes_price,
                "no_price": market.no_price,
                "liquidity": market.liquidity,
            },
            "signal": {
                "direction": signal.direction,
                "side_to_buy": signal.polymarket_side,
                "confidence": signal.confidence,
                "conviction": signal.conviction,
                "kelly_fraction": signal.suggested_kelly_fraction,
                "fusion_score": signal.fusion_score,
            },
            "expected_edge": _estimate_edge(signal, market),
        })
    
    # Sort by expected edge
    opportunities.sort(key=lambda o: -o["expected_edge"])
    
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "opportunities": opportunities[:30],
        "total_matched": len(opportunities),
        "total_markets": len(markets),
        "signals": {k: asdict(v) for k, v in signals.items()},
    }


def _estimate_edge(signal: DirectionalSignal, market) -> float:
    """Estimate edge for a signal+market pair.
    
    Edge = (estimated_true_prob - market_price) as percentage.
    True prob comes from Virtuoso's confidence + win rate estimate.
    """
    # If signal says UP, we're buying Yes side at market.yes_price
    if signal.direction == "UP":
        market_price = market.yes_price
    elif signal.direction == "DOWN":
        market_price = market.no_price
    else:
        return 0.0
    
    # Estimated true probability based on Virtuoso confidence
    # Fusion at 50% confidence = coin flip, 75% = meaningful edge
    # Map confidence to estimated true prob (conservative)
    if signal.confidence >= 80:
        est_prob = 0.60
    elif signal.confidence >= 70:
        est_prob = 0.56
    elif signal.confidence >= 60:
        est_prob = 0.53
    else:
        est_prob = 0.51
    
    # Boost for high vol regime (bigger moves = more directional certainty)
    if signal.regime_volatility == "high":
        est_prob += 0.03
    
    edge = (est_prob - market_price) * 100
    return round(max(0, edge), 2)


# ============================================================================
# CLI test
# ============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("Virtuoso Bridge — Phase 2")
    print("=" * 60)
    
    for asset in ["BTC", "ETH"]:
        print(f"\n📡 Getting {asset} directional signal...")
        sig = get_directional_signal(asset)
        print(f"  Direction: {sig.direction} | Confidence: {sig.confidence}%")
        print(f"  Fusion: {sig.fusion_direction} (score: {sig.fusion_score})")
        print(f"  Regime: {sig.regime_bias} | Vol: {sig.regime_volatility}")
        print(f"  Trade: {'✅ YES' if sig.should_trade else f'❌ NO ({sig.skip_reason})'}")
        print(f"  Polymarket side: {sig.polymarket_side} | Kelly: {sig.suggested_kelly_fraction}")
    
    print("\n📊 Matching signals to markets...")
    result = match_signals_to_markets()
    print(f"  Matched {result['total_matched']} of {result['total_markets']} markets")
    for opp in result["opportunities"][:5]:
        print(f"  🎯 [{opp['market']['asset']}] {opp['market']['duration']} "
              f"→ Buy {opp['signal']['side_to_buy']} "
              f"({opp['signal']['conviction']}, edge: {opp['expected_edge']}%)")

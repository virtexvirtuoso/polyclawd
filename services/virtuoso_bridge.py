"""
Virtuoso MCP Bridge — Phase 2

Connects Virtuoso's derivatives intelligence to Polymarket short-duration markets.
Maps directional signals (fusion, funding, regime) to 5/15-min "Up" or "Down" bets.

This is the brain that the $134→$200K bot never had.
"""

import json
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict

import httpx
from loguru import logger


# ============================================================================
# REST Transport (Phase B 2026-09-16) — replaces the mcporter subprocess bridge
# ============================================================================
#
# The old path spawned `node /usr/bin/mcporter call virtuoso.<tool>` per call:
# a fresh node process + full MCP session (initialize -> initialized ->
# GET/405 -> tools/call) roughly every 24 s per snapshot round, 24/7
# (~17.6k MCP sessions/day, 100% of virtuoso-mcp-stream traffic; the same
# session-per-call pattern behind the 2026-06-01 MCP memory leak). Every tool
# used here is a thin proxy over a Virtuoso REST endpoint, so we call the
# endpoints directly and share a TTL cache. Tool names and the _mcp_call()
# contract are kept — callers (hf_collector, hf_risk_gate,
# api/routes/markets.py) need no edits.
#
# Endpoint map verified live on the VPS 2026-09-16 (Virtuoso src/mcp/tools/*):
#   get_perps_fusion_signal -> GET :8888 /signals/fusion/{symbol}
#   get_market_regime       -> GET :8002 /api/regime/
#   get_kill_switch_status  -> GET :8002 /api/risk/kill-switch/status
#   get_manipulation_alerts -> GET :8002 /api/manipulation/alerts
#
# TTLs are per endpoint (worst case ~216 calls/h across 2 uvicorn workers,
# target <=300/h and fusion <=60/h): fusion 300 s (signal horizon 1h-24h —
# 5-min staleness immaterial; upstream rewrites ~15 s), regime 300 s (slow
# classification), kill switch 75 s (safety gate — tightest), manipulation
# 150 s. Failures negative-cache 10 s so upstream restart windows
# (:8002 ~06:00/14:00 UTC) are not hammered.

_VIRTUOSO_API = "http://127.0.0.1:8002"    # kill switch / manipulation / regime
_VIRTUOSO_PERPS = "http://127.0.0.1:8888"  # fusion signals (perps tracker)

REST_TIMEOUT = 8.0         # s — upstream calls measured 25-207 ms on 2026-09-16
REST_CACHE_TTL_ERR = 10.0  # s — negative cache for failures
_REST_TTL_OK = (           # (url substring, success TTL s)
    ("/signals/fusion/", 300.0),
    ("/api/regime/", 300.0),
    ("/api/risk/kill-switch/", 75.0),
    ("/api/manipulation/", 150.0),
)
_REST_TTL_DEFAULT = 90.0

_rest_cache: Dict[str, tuple] = {}
_rest_lock = threading.Lock()
_http_client_ref = None
_http_lock = threading.Lock()


def _ttl_for(url: str) -> float:
    for key, ttl in _REST_TTL_OK:
        if key in url:
            return ttl
    return _REST_TTL_DEFAULT


def _shared_http_client():
    global _http_client_ref
    if _http_client_ref is None:
        with _http_lock:
            if _http_client_ref is None:
                _http_client_ref = httpx.Client(timeout=REST_TIMEOUT)
    return _http_client_ref


def _rest_get(url: str):
    """GET a Virtuoso REST endpoint with a shared TTL cache.

    Returns the parsed JSON (dict or list) or None on failure — the same
    contract the old mcporter wrapper had, so every caller keeps its existing
    None handling (risk gates fail open, signal fields fall back to defaults).
    Success caches per endpoint TTL, failures for REST_CACHE_TTL_ERR. Never
    raises.
    """
    now = time.monotonic()
    with _rest_lock:
        hit = _rest_cache.get(url)
        if hit is not None and hit[0] > now:
            return hit[1]
    data = None
    try:
        resp = _shared_http_client().get(url)
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, (dict, list)):
            data = payload
    except Exception as e:  # noqa: BLE001 — transport must never raise
        logger.warning(f"Virtuoso REST {url} failed: {str(e)[:160]}")
    ttl = _ttl_for(url) if data is not None else REST_CACHE_TTL_ERR
    with _rest_lock:
        _rest_cache[url] = (now + ttl, data)
    return data


_TOOL_ENDPOINTS = {
    "get_perps_fusion_signal": _VIRTUOSO_PERPS + "/signals/fusion/{symbol}",
    "get_market_regime": _VIRTUOSO_API + "/api/regime/",
    "get_kill_switch_status": _VIRTUOSO_API + "/api/risk/kill-switch/status",
    "get_manipulation_alerts": _VIRTUOSO_API + "/api/manipulation/alerts",
}


def _mcp_call(tool: str, args: dict = None) -> Optional[Dict]:
    """Fetch a Virtuoso data endpoint (name/contract kept from the mcporter era).

    Returns {"raw": <json str>, "tool": tool, "timestamp": ...} or None —
    identical to the old wrapper, so _parse_* and every caller keep working
    unchanged. raw is now the REST endpoint's JSON instead of the MCP tool's
    markdown rendering of that same JSON. Never raises.
    """
    pattern = _TOOL_ENDPOINTS.get(tool)
    if pattern is None:
        logger.warning(f"REST bridge: no endpoint mapping for tool {tool}")
        return None
    try:
        if "{symbol}" in pattern:
            symbol = "BTC"
            if args and args.get("symbol"):
                symbol = str(args["symbol"]).upper()
            if symbol == "BITCOIN":
                symbol = "BTC"
            elif symbol == "ETHEREUM":
                symbol = "ETH"
            url = pattern.format(symbol=symbol)
        else:
            url = pattern
        data = _rest_get(url)
    except Exception as e:  # noqa: BLE001 — transport must never raise
        logger.warning(f"REST bridge {tool} exception: {e}")
        return None
    if data is None:
        return None
    return {"raw": json.dumps(data), "tool": tool, "timestamp": datetime.utcnow().isoformat()}


# ============================================================================
# Signal Parsers — map Virtuoso REST payloads to bridge dicts (Phase B 2026-09-16)
# ============================================================================

def _load_json(raw):
    """Parse a _mcp_call raw payload (REST JSON) into a dict, else None."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            d = json.loads(raw)
        except ValueError:
            return None
        return d if isinstance(d, dict) else None
    return None


def _parse_fusion_signal(raw: str) -> Dict:
    """Map the fusion REST payload (:8888 /signals/fusion/{coin}) to the
    bridge dict — Phase B 2026-09-16. raw is the endpoint's JSON (the old
    mcporter markdown path is gone — the MCP tool rendered its markdown from
    this same JSON). Output shape identical to the old parser's."""
    result = {
        "direction": "NEUTRAL",
        "score": 0.0,
        "confidence": 50,
        "entry": "WAIT",
        "win_rate": 50,
        "components": {},
    }
    d = _load_json(raw)
    if d is None:
        return result
    sig = d.get("signal")
    if not isinstance(sig, dict):
        sig = d

    def _num(value, default):
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    direction = str(sig.get("direction") or "neutral").upper()
    result["direction"] = direction if direction in ("LONG", "SHORT") else "NEUTRAL"
    result["score"] = _num(sig.get("score"), 0.0)
    result["confidence"] = int(round(_num(sig.get("confidence"), 50)))
    entry = str(sig.get("entry_recommendation") or "WAIT").upper()
    result["entry"] = entry if entry in ("LONG", "SHORT") else "WAIT"
    result["win_rate"] = int(round(_num(sig.get("win_rate_estimate"), 50)))
    result["strength"] = str(sig.get("strength") or "weak")
    result["components"] = {
        "FR": _num(sig.get("funding_contribution"), 0.0),
        "OI": _num(sig.get("oi_contribution"), 0.0),
        "LSR": _num(sig.get("lsr_contribution"), 0.0),
        "CVD": _num(sig.get("cvd_contribution"), 0.0),
    }
    return result


def _parse_regime(raw: str) -> Dict:
    """Map the regime REST payload to the bridge dict — Phase B JSON path.

    KNOWN GAP (deliberately not "fixed" here — it would change trading-gate
    behaviour; flagged in the Phase B report): the live :8002 /api/regime/
    response is a per-symbol dict, and the MCP tool we replaced read
    top-level current_regime/trading_bias keys that don't exist in it — so
    its output, and therefore this parser's output, has been UNKNOWN/0/0
    since the data_connectors tool version went live. This mapper reproduces
    that exactly and will pick up real values if the endpoint grows the
    top-level shape."""
    result = {
        "bias": "UNKNOWN",
        "confidence": 50,
        "high_volatility_count": 0,
        "recommendation": "",
    }
    d = _load_json(raw)
    if d is None:
        return result
    bias = d.get("regime") or d.get("overall_bias") or d.get("current_regime")
    if bias:
        result["bias"] = str(bias).upper()
    try:
        conf = float(d.get("confidence"))
        if 0 < conf <= 1.0:  # 0-1 fraction (old tool rendered it as a %) → scale
            conf *= 100.0
        if 0 < conf <= 100:
            result["confidence"] = int(round(conf))
    except (TypeError, ValueError):
        pass
    try:
        result["high_volatility_count"] = int(d.get("high_volatility_count") or 0)
    except (TypeError, ValueError):
        pass
    rec = d.get("recommendation") or d.get("trading_bias")
    if rec:
        result["recommendation"] = str(rec)
    return result


def _parse_kill_switch(raw: str) -> Dict:
    """Map the kill-switch REST payload to the bridge dict — Phase B JSON path.

    Keyed off is_active, exactly like the MCP tool's markdown rendering was.
    (Display note: the old parser left state="MONITORING" even when halted —
    the tool rendered "HALTED", a word its TRIGGERED/KILLED matcher never
    matched; gates still worked via trading_allowed. This mapper reports the
    truer state; gate outcomes are unchanged.)"""
    result = {
        "active": False,
        "trading_allowed": True,
        "state": "MONITORING",
    }
    d = _load_json(raw)
    if d is None:
        return result
    is_active = bool(d.get("is_active", False))
    result["active"] = is_active
    result["trading_allowed"] = not is_active
    result["state"] = "TRIGGERED" if is_active else "MONITORING"
    return result


def _parse_manipulation(raw: str) -> Dict:
    """Map the manipulation REST payload to the bridge dict — Phase B JSON path.

    :8002 /api/manipulation/alerts returns a JSON list of active alerts
    (live 2026-09-16: []). Some shapes may wrap the list in {"data": [...]}."""
    result = {
        "alerts_active": False,
        "alert_count": 0,
        "details": [],
    }
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError:
            return result
    else:
        return result
    if isinstance(data, dict) and "data" in data:
        data = data.get("data")
    if not isinstance(data, list):
        return result
    details = [str(a) for a in data]
    result["alerts_active"] = bool(details)
    result["alert_count"] = len(details)
    result["details"] = details
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
    logger.info("=" * 60)
    logger.info("Virtuoso Bridge — Phase 2")
    logger.info("=" * 60)
    
    for asset in ["BTC", "ETH"]:
        logger.info(f"\n📡 Getting {asset} directional signal...")
        sig = get_directional_signal(asset)
        logger.info(f"  Direction: {sig.direction} | Confidence: {sig.confidence}%")
        logger.info(f"  Fusion: {sig.fusion_direction} (score: {sig.fusion_score})")
        logger.info(f"  Regime: {sig.regime_bias} | Vol: {sig.regime_volatility}")
        logger.info(f"  Trade: {'✅ YES' if sig.should_trade else f'❌ NO ({sig.skip_reason})'}")
        logger.info(f"  Polymarket side: {sig.polymarket_side} | Kelly: {sig.suggested_kelly_fraction}")
    
    logger.info("\n📊 Matching signals to markets...")
    result = match_signals_to_markets()
    logger.info(f"  Matched {result['total_matched']} of {result['total_markets']} markets")
    for opp in result["opportunities"][:5]:
        logger.info(f"  🎯 [{opp['market']['asset']}] {opp['market']['duration']} "
              f"→ Buy {opp['signal']['side_to_buy']} "
              f"({opp['signal']['conviction']}, edge: {opp['expected_edge']}%)")

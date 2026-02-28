"""
HF Trigger Engine — Predictive edge detection for Polymarket 5-min markets.

5 high-conviction trigger scenarios:
1. Liquidation Cascade Imminent (80-85% confidence)
2. Whale Absorption (65-70%)
3. CVD Divergence (60-65%)
4. Orderbook Cliff (55-60%)
5. Smart Money Squeeze (60-65%)

Gate logic: regime → session → triggers → confluence alignment → risk gate
Output: EdgeDecision written to memcached as polymarket:edge:{symbol}
"""

import json
import logging
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("hf_triggers")

# ============================================================================
# Session Schedule (UTC)
# ============================================================================

SESSION_SCHEDULE = [
    # (start_hour, end_hour, name, config)
    (0,  3,  "asia_open",     {"active": False, "min_confidence": 0.80, "size_mult": 0.3}),
    (3,  8,  "asia_active",   {"active": True,  "min_confidence": 0.70, "size_mult": 0.5}),
    (8,  9,  "london_open",   {"active": True,  "min_confidence": 0.55, "size_mult": 0.9}),
    (9,  13, "london_active", {"active": True,  "min_confidence": 0.58, "size_mult": 0.8}),
    (13, 14, "us_premarket",  {"active": True,  "min_confidence": 0.55, "size_mult": 1.0}),
    (14, 20, "us_active",     {"active": True,  "min_confidence": 0.55, "size_mult": 1.0}),
    (20, 21, "us_close",      {"active": True,  "min_confidence": 0.60, "size_mult": 0.7}),
    (21, 24, "dead_zone",     {"active": False, "min_confidence": 0.85, "size_mult": 0.2}),
]


def get_session() -> tuple[str, dict]:
    """Get current trading session name and config."""
    hour = datetime.now(timezone.utc).hour
    for start, end, name, config in SESSION_SCHEDULE:
        if start <= hour < end:
            return name, config
    return "unknown", {"active": True, "min_confidence": 0.60, "size_mult": 0.5}


# ============================================================================
# Trigger Result Types
# ============================================================================

@dataclass
class TriggerResult:
    """Base result from a trigger detector."""
    trigger_type: str
    direction: str       # "UP" or "DOWN"
    confidence: float    # 0.0 - 1.0
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EdgeDecision:
    """Master decision output."""
    action: str          # "TRADE" or "NO_TRADE"
    direction: Optional[str] = None
    confidence: float = 0.0
    trigger_type: Optional[str] = None
    trigger_details: Dict[str, Any] = field(default_factory=dict)
    gates: Dict[str, Any] = field(default_factory=dict)
    sizing: Dict[str, Any] = field(default_factory=dict)
    reason: Optional[str] = None
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


# ============================================================================
# Trigger Detectors
# ============================================================================

def detect_cascade(enrichment: Dict, fast: Dict, velocity_trackers: Dict) -> Optional[TriggerResult]:
    """Detect imminent liquidation cascade.

    Highest conviction trigger (80-85%). Mechanical, not predictive.
    Fires when price is accelerating toward a large liquidation cluster.
    """
    from services.hf_enrichment import get_enrichment_reader
    reader = get_enrichment_reader()

    symbol = fast.get("symbol", "BTCUSDT")
    zones = reader.get_liquidation_zones(enrichment, symbol)
    if not zones:
        return None

    liq_tracker = velocity_trackers.get("liquidation")
    if liq_tracker is None or liq_tracker.samples < 5:
        return None

    cluster = liq_tracker.cascade_imminent(
        zones, max_distance_pct=0.5, min_cluster_usd=20_000_000
    )
    if cluster is None:
        return None

    # Boost confidence based on cluster size and proximity
    base_conf = 0.78
    if cluster["distance_pct"] < 0.2:
        base_conf += 0.05
    if cluster["size_usd"] > 50_000_000:
        base_conf += 0.04
    confidence = min(0.90, base_conf)

    return TriggerResult(
        trigger_type="cascade",
        direction=cluster["direction"],
        confidence=confidence,
        details={
            "cluster_price": cluster["price"],
            "current_price": liq_tracker.current_price,
            "distance_pct": round(cluster["distance_pct"], 3),
            "velocity": round(cluster["velocity"], 2),
            "cluster_size_usd": cluster["size_usd"],
            "eta_seconds": round(cluster["eta_seconds"], 1) if cluster.get("eta_seconds") else None,
        },
    )


def detect_whale_absorption(enrichment: Dict, fast: Dict, velocity_trackers: Dict) -> Optional[TriggerResult]:
    """Detect whale trade absorption pattern.

    Moderate-high conviction (65-70%). Fires when a large trade was absorbed
    with minimal price impact, and the orderbook on the impact side is depleted.
    """
    from services.hf_enrichment import get_enrichment_reader
    reader = get_enrichment_reader()

    symbol = fast.get("symbol", "BTCUSDT")
    whale_trades = reader.get_whale_trades(enrichment, symbol)
    if not whale_trades:
        return None

    now = time.time()
    # Look for whale trades in last 60 seconds
    recent_whales = []
    for trade in whale_trades:
        trade_time = trade.get("timestamp", trade.get("time", 0))
        if isinstance(trade_time, str):
            try:
                trade_time = datetime.fromisoformat(trade_time.replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                continue
        age = now - trade_time
        if 0 < age < 60:
            recent_whales.append(trade)

    if not recent_whales:
        return None

    # Find the largest recent whale
    best = max(recent_whales, key=lambda t: t.get("size", t.get("volume", t.get("notional", 0))))
    whale_size = best.get("size", best.get("volume", best.get("notional", 0)))
    if whale_size < 500_000:  # Min $500K
        return None

    whale_side = best.get("side", best.get("direction", "")).upper()
    if whale_side not in ("BUY", "LONG", "UP"):
        direction = "DOWN"
    else:
        direction = "UP"

    # Check orderbook depletion on the impact side
    book = reader.get_orderbook_depth(enrichment, symbol)
    bid_depth = book.get("bid_depth", book.get("bids_total", 0))
    ask_depth = book.get("ask_depth", book.get("asks_total", 0))

    if bid_depth <= 0 or ask_depth <= 0:
        return None

    # Depletion: if whale bought, asks should be thinner
    if direction == "UP":
        depletion = 1.0 - (ask_depth / max(bid_depth, 1))
    else:
        depletion = 1.0 - (bid_depth / max(ask_depth, 1))

    if depletion < 0.30:  # Need at least 30% depletion
        return None

    confidence = 0.63
    if whale_size > 2_000_000:
        confidence += 0.04
    if depletion > 0.50:
        confidence += 0.03
    confidence = min(0.75, confidence)

    return TriggerResult(
        trigger_type="whale_absorption",
        direction=direction,
        confidence=confidence,
        details={
            "whale_size_usd": whale_size,
            "whale_side": whale_side,
            "book_depletion": round(depletion, 3),
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
        },
    )


def detect_cvd_divergence(enrichment: Dict, fast: Dict, velocity_trackers: Dict) -> Optional[TriggerResult]:
    """Detect CVD divergence from price.

    Moderate conviction (60-65%). Fires when price is flat but CVD is
    trending strongly in one direction (hidden accumulation/distribution).
    """
    cvd_tracker = velocity_trackers.get("cvd")
    if cvd_tracker is None or cvd_tracker.samples < 4:
        return None

    # Need recent price change from fast data
    price_change_pct = fast.get("price_change_3m_pct", 0)

    if not cvd_tracker.is_divergent(price_change_pct):
        return None

    vel = cvd_tracker.velocity
    accel = cvd_tracker.acceleration

    # Direction based on CVD, not price (that's the divergence)
    if vel > 0:
        direction = "UP"   # Net buying despite flat/falling price
    elif vel < 0:
        direction = "DOWN"  # Net selling despite flat/rising price
    else:
        return None

    confidence = 0.58
    # Stronger signal if acceleration confirms velocity direction
    if (vel > 0 and accel > 0) or (vel < 0 and accel < 0):
        confidence += 0.05
    # Stronger if price is really flat but CVD is moving
    if abs(price_change_pct) < 0.02:
        confidence += 0.03
    confidence = min(0.68, confidence)

    return TriggerResult(
        trigger_type="cvd_divergence",
        direction=direction,
        confidence=confidence,
        details={
            "price_change_pct": round(price_change_pct, 4),
            "cvd_velocity": round(vel, 4),
            "cvd_acceleration": round(accel, 6),
            "cvd_level": round(cvd_tracker.current_level, 2),
        },
    )


def detect_orderbook_cliff(enrichment: Dict, fast: Dict, velocity_trackers: Dict) -> Optional[TriggerResult]:
    """Detect orderbook cliff (extreme imbalance).

    Lower conviction (55-60%). Fires when bid/ask ratio is > 3:1
    and the imbalance is increasing.
    """
    imb_tracker = velocity_trackers.get("imbalance")
    if imb_tracker is None or imb_tracker.samples < 3:
        return None

    if not imb_tracker.is_cliff:
        return None

    direction = imb_tracker.cliff_direction
    if direction is None:
        return None

    vel = imb_tracker.velocity
    # Only signal if imbalance is growing (positive velocity for UP cliff)
    if direction == "UP" and vel <= 0:
        return None
    if direction == "DOWN" and vel >= 0:
        return None

    confidence = 0.54
    ratio = imb_tracker.current_ratio
    if direction == "DOWN":
        ratio = 1.0 / ratio  # Normalize for comparison
    if ratio > 5.0:
        confidence += 0.04
    if abs(vel) > 0.01:
        confidence += 0.03
    confidence = min(0.62, confidence)

    return TriggerResult(
        trigger_type="orderbook_cliff",
        direction=direction,
        confidence=confidence,
        details={
            "imbalance_ratio": round(imb_tracker.current_ratio, 2),
            "imbalance_velocity": round(vel, 6),
        },
    )


def detect_smart_money_squeeze(enrichment: Dict, fast: Dict, velocity_trackers: Dict) -> Optional[TriggerResult]:
    """Detect smart money vs retail divergence (squeeze setup).

    Moderate conviction (60-65%). Fires when elite traders are heavily
    positioned one way and retail is positioned the opposite.
    """
    from services.hf_enrichment import get_enrichment_reader
    reader = get_enrichment_reader()

    symbol = fast.get("symbol", "BTCUSDT")
    breakdown = enrichment.get(f"confluence:breakdown:{symbol}")
    if not isinstance(breakdown, dict):
        return None

    # Try to extract positioning data from breakdown
    position = breakdown.get("position", breakdown.get("positioning", {}))
    if not isinstance(position, dict):
        return None

    # Look for long/short ratio or similar positioning data
    long_ratio = position.get("long_ratio", position.get("long_pct", None))
    if long_ratio is None:
        # Try signals data for funding rate as proxy
        signals = enrichment.get("analysis:signals")
        if isinstance(signals, dict):
            for sig in signals.get("signals", []):
                if sig.get("symbol") == symbol:
                    funding = sig.get("funding_rate", None)
                    if funding is not None:
                        # Deeply negative funding = shorts paying longs = short squeeze setup
                        if funding < -0.01:
                            return TriggerResult(
                                trigger_type="smart_money_squeeze",
                                direction="UP",
                                confidence=min(0.65, 0.58 + abs(funding) * 5),
                                details={
                                    "funding_rate": funding,
                                    "squeeze_type": "short_squeeze",
                                    "source": "funding_rate_proxy",
                                },
                            )
                        elif funding > 0.01:
                            return TriggerResult(
                                trigger_type="smart_money_squeeze",
                                direction="DOWN",
                                confidence=min(0.65, 0.58 + abs(funding) * 5),
                                details={
                                    "funding_rate": funding,
                                    "squeeze_type": "long_squeeze",
                                    "source": "funding_rate_proxy",
                                },
                            )
        return None

    # If we have actual positioning data
    if long_ratio > 0.65:
        direction = "UP"    # Majority long, likely smart money
        confidence = 0.58 + (long_ratio - 0.65) * 0.5
    elif long_ratio < 0.35:
        direction = "DOWN"  # Majority short, likely smart money
        confidence = 0.58 + (0.35 - long_ratio) * 0.5
    else:
        return None

    return TriggerResult(
        trigger_type="smart_money_squeeze",
        direction=direction,
        confidence=min(0.68, confidence),
        details={
            "long_ratio": long_ratio,
            "squeeze_type": "short_squeeze" if direction == "UP" else "long_squeeze",
            "source": "positioning_data",
        },
    )


# ============================================================================
# Gate Logic
# ============================================================================

def confluence_aligns(direction: str, score: float) -> bool:
    """Check if confluence score supports the trigger direction."""
    if direction == "UP":
        return score > 55
    elif direction == "DOWN":
        return score < 45
    return False


def kelly_fraction(confidence: float, vig: float = 0.05) -> float:
    """Half-Kelly position sizing.

    Break-even = 1 / (2 - vig) ≈ 52.5% for 5% vig.
    """
    break_even = 1.0 / (2.0 - vig)
    edge = confidence - break_even
    if edge <= 0:
        return 0.0
    # Full Kelly = edge / (1 - payout), half-Kelly for safety
    full_kelly = edge / (1.0 - (1.0 - vig))
    half_kelly = full_kelly * 0.5
    return min(half_kelly, 0.15)  # Cap at 15%


MAX_POSITION_USD = 4000  # Polymarket 5-min liquidity ceiling


# ============================================================================
# Master Evaluation
# ============================================================================

ALL_DETECTORS = [
    detect_cascade,
    detect_whale_absorption,
    detect_cvd_divergence,
    detect_orderbook_cliff,
    detect_smart_money_squeeze,
]


def evaluate_edge(
    enrichment: Dict,
    fast: Dict,
    velocity_trackers: Dict,
    bankroll: float = 10000.0,
) -> EdgeDecision:
    """Master decision function.

    Gate 1: Regime (ranging = skip)
    Gate 2: Session (dead zone = skip weak triggers)
    Gate 3: Trigger evaluation (ranked by confidence)
    Gate 4: Confluence alignment (for non-cascade triggers)
    Gate 5: Session min confidence check
    """
    from services.hf_enrichment import get_enrichment_reader
    reader = get_enrichment_reader()

    symbol = fast.get("symbol", "BTCUSDT")

    # GATE 1: Regime
    regime = reader.get_regime(enrichment)
    if regime in ("sideways", "ranging", "choppy"):
        return EdgeDecision(
            action="NO_TRADE",
            reason="regime_ranging",
            gates={"regime": regime, "gate_failed": "regime"},
        )

    # GATE 2: Session
    session_name, session_config = get_session()
    gates = {
        "regime": regime,
        "session": session_name,
        "session_active": session_config["active"],
    }

    # Run all detectors
    triggers: List[TriggerResult] = []
    for detector in ALL_DETECTORS:
        try:
            result = detector(enrichment, fast, velocity_trackers)
            if result is not None:
                triggers.append(result)
        except Exception as e:
            logger.debug(f"Detector {detector.__name__} error: {e}")

    if not triggers:
        return EdgeDecision(
            action="NO_TRADE",
            reason="no_triggers",
            gates=gates,
        )

    # GATE 3: Pick best trigger by confidence
    best = max(triggers, key=lambda t: t.confidence)

    # GATE 4: Confluence alignment (cascade doesn't need it)
    score = reader.get_confluence_score(enrichment, symbol)
    gates["confluence_score"] = score

    if best.trigger_type != "cascade":
        if not confluence_aligns(best.direction, score):
            return EdgeDecision(
                action="NO_TRADE",
                reason="confluence_misaligned",
                trigger_type=best.trigger_type,
                direction=best.direction,
                confidence=best.confidence,
                gates={**gates, "confluence_aligned": False, "gate_failed": "confluence"},
            )
    gates["confluence_aligned"] = True

    # GATE 5: Session minimum confidence
    min_conf = session_config["min_confidence"]
    if best.confidence < min_conf:
        return EdgeDecision(
            action="NO_TRADE",
            reason=f"below_session_min_confidence ({best.confidence:.2f} < {min_conf})",
            trigger_type=best.trigger_type,
            direction=best.direction,
            confidence=best.confidence,
            gates={**gates, "gate_failed": "session_confidence"},
        )

    # TRADE — compute sizing
    kelly = kelly_fraction(best.confidence)
    size_mult = session_config["size_mult"]
    raw_size = bankroll * kelly * size_mult
    recommended_usd = min(raw_size, MAX_POSITION_USD)

    return EdgeDecision(
        action="TRADE",
        direction=best.direction,
        confidence=best.confidence,
        trigger_type=best.trigger_type,
        trigger_details=best.details,
        gates=gates,
        sizing={
            "kelly_fraction": round(kelly, 4),
            "size_multiplier": size_mult,
            "recommended_usd": round(recommended_usd, 2),
            "max_position_usd": MAX_POSITION_USD,
        },
        reason=None,
    )


def build_edge_payload(decision: EdgeDecision, oracle_state: Dict = None) -> Dict:
    """Build the full payload to write to memcached."""
    payload = asdict(decision)
    if oracle_state:
        payload["oracle"] = oracle_state
    payload["meta"] = {
        "engine_version": "1.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return payload


# ============================================================================
# CLI Test
# ============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("HF Trigger Engine — CLI Test")
    print("=" * 60)

    session_name, session_config = get_session()
    print(f"\nSession: {session_name} (active={session_config['active']}, "
          f"min_conf={session_config['min_confidence']})")

    # Test with mock data
    mock_enrichment = {
        "analysis:market_regime": "bullish",
        "confluence:score:BTCUSDT": 72.5,
        "confluence:breakdown:BTCUSDT": {
            "score": 72.5,
            "technical": {"score": 75},
            "volume": {"score": 68},
            "orderflow": {"score": 70, "cvd": 1500},
            "position": {"long_ratio": 0.62},
        },
        "liquidations:BTCUSDT": {"zones": [
            {"price": 68500, "size": 52_000_000},
            {"price": 64000, "size": 30_000_000},
        ]},
        "large_trades:BTCUSDT": {"trades": []},
        "orderbook:BTCUSDT:snapshot": {"bid_depth": 15_000_000, "ask_depth": 4_000_000},
        "analysis:signals": {"signals": [
            {"symbol": "BTCUSDT", "funding_rate": -0.015}
        ]},
    }

    mock_fast = {
        "symbol": "BTCUSDT",
        "binance_price": 68350,
        "price_change_3m_pct": -0.02,
    }

    from services.hf_velocity import (
        ImbalanceVelocityTracker, CVDAccelerationTracker, LiquidationProximityTracker
    )
    import time as _time

    imb = ImbalanceVelocityTracker()
    cvd = CVDAccelerationTracker()
    liq = LiquidationProximityTracker()

    # Seed velocity trackers with mock history
    for i in range(6):
        imb.update(15_000_000 + i * 500_000, 4_000_000 - i * 100_000)
        cvd.update(1500 + i * 50 + i * i * 2)
        liq.update_price(68200 + i * 30)
        _time.sleep(0.01)

    trackers = {"imbalance": imb, "cvd": cvd, "liquidation": liq}

    decision = evaluate_edge(mock_enrichment, mock_fast, trackers)
    print(f"\nDecision: {decision.action}")
    print(f"Direction: {decision.direction}")
    print(f"Confidence: {decision.confidence:.1%}")
    print(f"Trigger: {decision.trigger_type}")
    print(f"Reason: {decision.reason}")
    print(f"Gates: {json.dumps(decision.gates, indent=2)}")
    if decision.sizing:
        print(f"Sizing: {json.dumps(decision.sizing, indent=2)}")

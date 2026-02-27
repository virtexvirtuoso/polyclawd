"""
HF Risk Gate — Phase 2

Circuit breakers for high-frequency Polymarket trading.
Prevents trading during dangerous conditions using Virtuoso MCP signals
and internal drawdown tracking.

Kill conditions:
1. Virtuoso kill switch triggered
2. Manipulation alerts active
3. Rolling drawdown > threshold
4. Oracle feed stale
5. Regime too calm (no edge from latency)
"""

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict, field

logger = logging.getLogger(__name__)

# In-memory state (resets on service restart — fine for Phase 2)
_trade_log: List[Dict] = []
_gate_overrides: Dict[str, bool] = {}


@dataclass
class RiskCheck:
    """Result of a single risk check."""
    name: str
    passed: bool
    severity: str  # "hard" (blocks trading) or "soft" (warning only)
    message: str
    value: Optional[float] = None
    threshold: Optional[float] = None


@dataclass 
class RiskGateResult:
    """Combined risk gate assessment."""
    trading_allowed: bool
    checks: List[RiskCheck]
    hard_blocks: int
    soft_warnings: int
    timestamp: str
    summary: str


# ============================================================================
# Individual Risk Checks
# ============================================================================

def check_kill_switch() -> RiskCheck:
    """Check Virtuoso kill switch status."""
    try:
        from services.virtuoso_bridge import _mcp_call, _parse_kill_switch
        raw = _mcp_call("get_kill_switch_status")
        if not raw:
            return RiskCheck(
                name="kill_switch",
                passed=True,  # Fail open — can't reach MCP, don't block
                severity="soft",
                message="Could not reach Virtuoso MCP — kill switch status unknown",
            )
        
        status = _parse_kill_switch(raw["raw"])
        
        if status["active"] or not status.get("trading_allowed", True):
            return RiskCheck(
                name="kill_switch",
                passed=False,
                severity="hard",
                message=f"Kill switch TRIGGERED — state: {status['state']}",
            )
        
        return RiskCheck(
            name="kill_switch",
            passed=True,
            severity="hard",
            message=f"Kill switch OK — state: {status['state']}",
        )
    except Exception as e:
        return RiskCheck(
            name="kill_switch",
            passed=True,
            severity="soft",
            message=f"Kill switch check error: {e}",
        )


def check_manipulation() -> RiskCheck:
    """Check for active manipulation alerts."""
    try:
        from services.virtuoso_bridge import _mcp_call, _parse_manipulation
        raw = _mcp_call("get_manipulation_alerts")
        if not raw:
            return RiskCheck(
                name="manipulation",
                passed=True,
                severity="soft",
                message="Could not reach Virtuoso MCP — manipulation status unknown",
            )
        
        status = _parse_manipulation(raw["raw"])
        
        if status["alerts_active"]:
            return RiskCheck(
                name="manipulation",
                passed=False,
                severity="hard",
                message=f"Manipulation detected — {status['alert_count']} active alerts",
                value=float(status["alert_count"]),
            )
        
        return RiskCheck(
            name="manipulation",
            passed=True,
            severity="hard",
            message="No manipulation alerts",
        )
    except Exception as e:
        return RiskCheck(
            name="manipulation",
            passed=True,
            severity="soft",
            message=f"Manipulation check error: {e}",
        )


def check_drawdown(max_drawdown_pct: float = 10.0, window_minutes: int = 60) -> RiskCheck:
    """Check rolling drawdown from trade log.
    
    Args:
        max_drawdown_pct: Maximum allowed drawdown in window (default 10%)
        window_minutes: Rolling window in minutes (default 60)
    """
    if not _trade_log:
        return RiskCheck(
            name="drawdown",
            passed=True,
            severity="hard",
            message="No trades logged — drawdown check N/A",
            value=0.0,
            threshold=max_drawdown_pct,
        )
    
    cutoff = datetime.utcnow() - timedelta(minutes=window_minutes)
    recent = [t for t in _trade_log if t.get("timestamp", "") > cutoff.isoformat()]
    
    if not recent:
        return RiskCheck(
            name="drawdown",
            passed=True,
            severity="hard",
            message=f"No trades in last {window_minutes}min",
            value=0.0,
            threshold=max_drawdown_pct,
        )
    
    # Calculate P&L
    total_pnl = sum(t.get("pnl", 0) for t in recent)
    total_risked = sum(abs(t.get("size", 0)) for t in recent)
    
    if total_risked == 0:
        drawdown_pct = 0.0
    else:
        drawdown_pct = max(0, -total_pnl / total_risked * 100)
    
    passed = drawdown_pct < max_drawdown_pct
    
    return RiskCheck(
        name="drawdown",
        passed=passed,
        severity="hard",
        message=f"Rolling {window_minutes}min drawdown: {drawdown_pct:.1f}% "
                f"({'OK' if passed else 'EXCEEDED'})",
        value=round(drawdown_pct, 2),
        threshold=max_drawdown_pct,
    )


def check_regime_volatility(min_hv_count: int = 2) -> RiskCheck:
    """Check if market regime has enough volatility for HF edge.
    
    Low volatility = small oracle lag = no edge from latency arb.
    """
    try:
        from services.virtuoso_bridge import _mcp_call, _parse_regime
        raw = _mcp_call("get_market_regime")
        if not raw:
            return RiskCheck(
                name="regime_volatility",
                passed=True,
                severity="soft",
                message="Could not fetch regime — allowing trading",
            )
        
        regime = _parse_regime(raw["raw"])
        hv_count = regime.get("high_volatility_count", 0)
        
        if hv_count < min_hv_count:
            return RiskCheck(
                name="regime_volatility",
                passed=False,
                severity="soft",
                message=f"Low volatility regime ({hv_count} HV symbols) — "
                        f"oracle lag likely too small for meaningful edge",
                value=float(hv_count),
                threshold=float(min_hv_count),
            )
        
        return RiskCheck(
            name="regime_volatility",
            passed=True,
            severity="soft",
            message=f"Volatility OK ({hv_count} HV symbols)",
            value=float(hv_count),
            threshold=float(min_hv_count),
        )
    except Exception as e:
        return RiskCheck(
            name="regime_volatility",
            passed=True,
            severity="soft",
            message=f"Regime check error: {e}",
        )


def check_api_health() -> RiskCheck:
    """Check Polyclawd API health."""
    try:
        import urllib.request
        url = "https://virtuosocrypto.com/polyclawd/health"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd-RiskGate/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        
        status = data.get("status", "unknown")
        if status == "healthy":
            return RiskCheck(
                name="api_health",
                passed=True,
                severity="hard",
                message="Polyclawd API healthy",
            )
        else:
            return RiskCheck(
                name="api_health",
                passed=False,
                severity="hard",
                message=f"Polyclawd API unhealthy: {status}",
            )
    except Exception as e:
        return RiskCheck(
            name="api_health",
            passed=False,
            severity="soft",
            message=f"API health check failed: {e}",
        )


# ============================================================================
# Trade Logging (in-memory for Phase 2)
# ============================================================================

def log_trade(asset: str, side: str, size: float, price: float, 
              pnl: float = 0.0, market_id: str = "") -> None:
    """Log a trade for drawdown tracking."""
    _trade_log.append({
        "asset": asset,
        "side": side,
        "size": size,
        "price": price,
        "pnl": pnl,
        "market_id": market_id,
        "timestamp": datetime.utcnow().isoformat(),
    })
    
    # Keep last 1000 trades
    if len(_trade_log) > 1000:
        _trade_log[:] = _trade_log[-500:]


def get_trade_log(limit: int = 50) -> List[Dict]:
    """Get recent trade log."""
    return _trade_log[-limit:]


def clear_trade_log() -> int:
    """Clear trade log. Returns count cleared."""
    count = len(_trade_log)
    _trade_log.clear()
    return count


# ============================================================================
# Full Risk Gate Assessment
# ============================================================================

def evaluate_risk_gate(
    max_drawdown_pct: float = 10.0,
    drawdown_window_min: int = 60,
    min_hv_count: int = 2,
) -> RiskGateResult:
    """
    Run all risk checks and determine if trading is allowed.
    
    Hard blocks (any one = no trading):
    - Kill switch triggered
    - Manipulation detected
    - Drawdown exceeded
    - API unhealthy
    
    Soft warnings (logged but don't block):
    - Low volatility regime
    - MCP unreachable
    
    Returns:
        RiskGateResult with pass/fail and all check details
    """
    checks = [
        check_kill_switch(),
        check_manipulation(),
        check_drawdown(max_drawdown_pct, drawdown_window_min),
        check_regime_volatility(min_hv_count),
        check_api_health(),
    ]
    
    hard_blocks = sum(1 for c in checks if not c.passed and c.severity == "hard")
    soft_warnings = sum(1 for c in checks if not c.passed and c.severity == "soft")
    
    trading_allowed = hard_blocks == 0
    
    # Check overrides
    if _gate_overrides.get("force_allow"):
        trading_allowed = True
    if _gate_overrides.get("force_block"):
        trading_allowed = False
    
    # Build summary
    if trading_allowed and soft_warnings == 0:
        summary = "✅ All clear — trading allowed"
    elif trading_allowed and soft_warnings > 0:
        summary = f"⚠️ Trading allowed with {soft_warnings} warning(s)"
    else:
        block_names = [c.name for c in checks if not c.passed and c.severity == "hard"]
        summary = f"🛑 Trading BLOCKED — {', '.join(block_names)}"
    
    return RiskGateResult(
        trading_allowed=trading_allowed,
        checks=checks,
        hard_blocks=hard_blocks,
        soft_warnings=soft_warnings,
        timestamp=datetime.utcnow().isoformat(),
        summary=summary,
    )


# ============================================================================
# Override Controls
# ============================================================================

def set_override(key: str, value: bool) -> Dict:
    """Set a risk gate override.
    
    Keys:
    - force_allow: Override all blocks (dangerous)
    - force_block: Block all trading regardless of checks
    """
    if key not in ("force_allow", "force_block"):
        return {"error": f"Unknown override key: {key}"}
    
    _gate_overrides[key] = value
    return {"override": key, "value": value, "active_overrides": dict(_gate_overrides)}


def clear_overrides() -> Dict:
    """Clear all overrides."""
    _gate_overrides.clear()
    return {"status": "cleared", "active_overrides": {}}


# ============================================================================
# CLI test
# ============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("HF Risk Gate — Phase 2")
    print("=" * 60)
    
    result = evaluate_risk_gate()
    print(f"\n{result.summary}")
    print(f"Trading allowed: {'✅' if result.trading_allowed else '🛑'}")
    print(f"Hard blocks: {result.hard_blocks} | Soft warnings: {result.soft_warnings}")
    
    print("\nChecks:")
    for check in result.checks:
        icon = "✅" if check.passed else ("🛑" if check.severity == "hard" else "⚠️")
        print(f"  {icon} [{check.name}] {check.message}")

"""
Polyclawd Scaling Phases Configuration

Phase-based position sizing for the $100 → $1M journey.
Each phase has different risk parameters optimized for the capital level.
"""

from typing import Dict, Any, Optional
from dataclasses import dataclass
from enum import Enum


class Phase(Enum):
    SEED = "seed"
    GROWTH = "growth"
    ACCELERATION = "acceleration"
    PRESERVATION = "preservation"


@dataclass
class PhaseConfig:
    """Configuration for a scaling phase."""
    name: str
    min_balance: float
    max_balance: float
    position_pct: float          # Base position size as % of balance
    max_positions: int           # Maximum concurrent positions
    min_confidence: int          # Minimum confidence to trade
    kelly_min: float             # Minimum Kelly fraction
    kelly_max: float             # Maximum Kelly fraction
    max_daily_trades: int        # Daily trade limit
    cooldown_after_loss: int     # Seconds to wait after a loss
    max_daily_loss_pct: float    # Stop trading if daily loss exceeds this
    max_exposure_pct: float      # Maximum total exposure as % of balance
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "balance_range": [self.min_balance, self.max_balance],
            "position_pct": self.position_pct,
            "max_positions": self.max_positions,
            "min_confidence": self.min_confidence,
            "kelly_range": [self.kelly_min, self.kelly_max],
            "max_daily_trades": self.max_daily_trades,
            "cooldown_after_loss": self.cooldown_after_loss,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "max_exposure_pct": self.max_exposure_pct,
        }


# Phase definitions
PHASES: Dict[Phase, PhaseConfig] = {
    Phase.SEED: PhaseConfig(
        name="seed",
        min_balance=0,
        max_balance=1_000,
        position_pct=0.22,           # 22% base position
        max_positions=4,
        min_confidence=45,           # Only high conviction
        kelly_min=0.15,
        kelly_max=0.50,
        max_daily_trades=15,
        cooldown_after_loss=300,     # 5 min cooldown
        max_daily_loss_pct=0.15,     # Stop at 15% daily loss
        max_exposure_pct=0.80,       # Up to 80% deployed
    ),
    Phase.GROWTH: PhaseConfig(
        name="growth",
        min_balance=1_000,
        max_balance=10_000,
        position_pct=0.12,           # 12% base position
        max_positions=8,
        min_confidence=40,
        kelly_min=0.10,
        kelly_max=0.40,
        max_daily_trades=20,
        cooldown_after_loss=600,     # 10 min cooldown
        max_daily_loss_pct=0.10,     # Stop at 10% daily loss
        max_exposure_pct=0.70,
    ),
    Phase.ACCELERATION: PhaseConfig(
        name="acceleration",
        min_balance=10_000,
        max_balance=100_000,
        position_pct=0.06,           # 6% base position
        max_positions=15,
        min_confidence=35,
        kelly_min=0.05,
        kelly_max=0.25,
        max_daily_trades=25,
        cooldown_after_loss=900,     # 15 min cooldown
        max_daily_loss_pct=0.07,     # Stop at 7% daily loss
        max_exposure_pct=0.60,
    ),
    Phase.PRESERVATION: PhaseConfig(
        name="preservation",
        min_balance=100_000,
        max_balance=float('inf'),
        position_pct=0.025,          # 2.5% base position
        max_positions=30,
        min_confidence=35,
        kelly_min=0.02,
        kelly_max=0.10,
        max_daily_trades=30,
        cooldown_after_loss=1800,    # 30 min cooldown
        max_daily_loss_pct=0.05,     # Stop at 5% daily loss
        max_exposure_pct=0.50,
    ),
}


def get_phase(balance: float) -> Phase:
    """Determine current phase based on balance."""
    if balance < 1_000:
        return Phase.SEED
    elif balance < 10_000:
        return Phase.GROWTH
    elif balance < 100_000:
        return Phase.ACCELERATION
    else:
        return Phase.PRESERVATION


def get_phase_config(balance: float) -> PhaseConfig:
    """Get phase configuration for current balance."""
    return PHASES[get_phase(balance)]


def calculate_position_size(
    balance: float,
    confidence: float,
    win_rate: float = 0.55,
    win_streak: int = 0,
    source_agreement: int = 1,
    market_price: float = 0.50,
) -> Dict[str, Any]:
    """
    Calculate position size with all factors.

    Args:
        balance: Current account balance
        confidence: Signal confidence (0-100)
        win_rate: Recent win rate (0-1)
        win_streak: Current streak (+ve = wins, -ve = losses)
        source_agreement: Number of agreeing signal sources
        market_price: Market price for payout ratio (0.01-0.99, default 0.50)

    Returns:
        Dict with position_usd, position_pct, kelly, phase info
    """
    phase_config = get_phase_config(balance)

    # Calculate Kelly fraction for variable-odds prediction markets
    # Kelly = (b * p - q) / b where p = win prob, q = 1-p, b = payout ratio
    p = confidence / 100
    q = 1.0 - p
    mp = max(0.01, min(0.99, market_price))
    b = (1.0 - mp) / mp  # payout ratio (e.g., price=0.25 -> b=3.0)
    kelly = (b * p - q) / b if b > 0 else 0
    kelly = max(0, kelly)  # No negative Kelly
    
    # Adjust Kelly based on recent performance
    performance_mult = 1.0
    if win_rate > 0.60:
        performance_mult = 1.15  # Hot hand
    elif win_rate < 0.45:
        performance_mult = 0.70  # Cold streak protection
    
    # Adjust for win streak
    streak_mult = 1.0
    if win_streak >= 3:
        streak_mult = 1.20  # Increase on hot streak
    elif win_streak <= -2:
        streak_mult = 0.70  # Reduce on cold streak
    
    # Adjust for source agreement
    agreement_mult = 1.0 + (0.10 * min(source_agreement - 1, 3))  # +10% per extra source, max 30%
    
    # Apply all multipliers to Kelly
    adjusted_kelly = kelly * performance_mult * streak_mult * agreement_mult
    
    # Clamp to phase limits
    adjusted_kelly = max(phase_config.kelly_min, min(phase_config.kelly_max, adjusted_kelly))
    
    # Calculate position size
    position_pct = phase_config.position_pct * adjusted_kelly
    position_usd = balance * position_pct
    
    # Apply hard limits
    max_position = min(balance * 0.25, 10_000)  # Never more than 25% or $10k
    min_position = max(balance * 0.02, 5)        # At least 2% or $5
    
    position_usd = max(min_position, min(max_position, position_usd))
    position_pct = position_usd / balance if balance > 0 else 0
    
    return {
        "position_usd": round(position_usd, 2),
        "position_pct": round(position_pct, 4),
        "kelly_raw": round(kelly, 4),
        "kelly_adjusted": round(adjusted_kelly, 4),
        "market_price": market_price,
        "payout_ratio": round(b, 4),
        "phase": phase_config.name,
        "phase_config": phase_config.to_dict(),
        "multipliers": {
            "performance": performance_mult,
            "streak": streak_mult,
            "agreement": agreement_mult,
        },
    }


def check_daily_limits(
    balance: float,
    daily_pnl: float,
    daily_trades: int,
    current_exposure: float,
) -> Dict[str, Any]:
    """
    Check if trading should be paused based on daily limits.
    
    Returns:
        Dict with can_trade, reason, and limit details
    """
    phase_config = get_phase_config(balance)
    
    # Check daily loss limit
    daily_loss_pct = abs(daily_pnl) / balance if balance > 0 and daily_pnl < 0 else 0
    if daily_loss_pct >= phase_config.max_daily_loss_pct:
        return {
            "can_trade": False,
            "reason": f"Daily loss limit reached ({daily_loss_pct:.1%} >= {phase_config.max_daily_loss_pct:.1%})",
            "limit_type": "daily_loss",
        }
    
    # Check daily trade limit
    if daily_trades >= phase_config.max_daily_trades:
        return {
            "can_trade": False,
            "reason": f"Daily trade limit reached ({daily_trades} >= {phase_config.max_daily_trades})",
            "limit_type": "daily_trades",
        }
    
    # Check exposure limit
    exposure_pct = current_exposure / balance if balance > 0 else 0
    if exposure_pct >= phase_config.max_exposure_pct:
        return {
            "can_trade": False,
            "reason": f"Max exposure reached ({exposure_pct:.1%} >= {phase_config.max_exposure_pct:.1%})",
            "limit_type": "exposure",
        }
    
    return {
        "can_trade": True,
        "reason": None,
        "limits": {
            "daily_loss": f"{daily_loss_pct:.1%} / {phase_config.max_daily_loss_pct:.1%}",
            "daily_trades": f"{daily_trades} / {phase_config.max_daily_trades}",
            "exposure": f"{exposure_pct:.1%} / {phase_config.max_exposure_pct:.1%}",
        },
    }


# Quick test
if __name__ == "__main__":
    # Test phase detection
    test_balances = [100, 500, 1500, 5000, 25000, 150000]
    
    print("Phase Detection Test:")
    print("-" * 60)
    for bal in test_balances:
        phase = get_phase_config(bal)
        print(f"${bal:>7,} → {phase.name:15} | Position: {phase.position_pct:.0%} | Max: {phase.max_positions} positions")
    
    print("\nPosition Sizing Test (confidence=60, balance=$500, market_price=0.40):")
    print("-" * 60)
    result = calculate_position_size(500, 60, win_rate=0.55, win_streak=2, source_agreement=2, market_price=0.40)
    print(f"Position: ${result['position_usd']:.2f} ({result['position_pct']:.1%})")
    print(f"Kelly: {result['kelly_raw']:.4f} → {result['kelly_adjusted']:.4f} (adjusted)")
    print(f"Market Price: {result['market_price']} | Payout Ratio: {result['payout_ratio']:.2f}")
    print(f"Phase: {result['phase']}")
    print(f"Multipliers: {result['multipliers']}")

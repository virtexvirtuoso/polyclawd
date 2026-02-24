#!/usr/bin/env python3
"""
Time Decay Optimizer — Becker-validated duration × volume modifiers for NO bets.

Backtest: 259K Polymarket markets (vol > $100), 344K total resolved.

Key findings:
  - Daily markets (~52% NO WR) ≈ coin flip → penalize
  - 3-7 day markets (65-69% NO WR) = sweet spot → boost
  - 2-4 week markets (62-71% NO WR) = strong → boost
  - Monthly+ (75-87% NO WR) = strongest → max boost
  - Medium volume ($1-10K) consistently best NO WR → retail FOMO zone
  - Whale volume (>$100K) hurts NO WR → smart money on other side

Replaces the simple duration modifier in paper_portfolio.py.
"""

import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# Becker-validated NO win rates by duration × volume
# Format: (duration_key, volume_key) → NO WR from 259K market backtest
BECKER_NO_WR = {
    # Daily (<24h) — near coin flip
    ("<1d", "low"):   0.523, ("<1d", "med"):   0.536, ("<1d", "high"):  0.518, ("<1d", "whale"): 0.537,
    # 1-3 days — slight edge
    ("1-3d", "low"):  0.517, ("1-3d", "med"):  0.576, ("1-3d", "high"): 0.536, ("1-3d", "whale"): 0.520,
    # 3-7 days — sweet spot
    ("3-7d", "low"):  0.535, ("3-7d", "med"):  0.691, ("3-7d", "high"): 0.658, ("3-7d", "whale"): 0.615,
    # 1-2 weeks — strong
    ("1-2w", "low"):  0.644, ("1-2w", "med"):  0.617, ("1-2w", "high"): 0.621, ("1-2w", "whale"): 0.602,
    # 2-4 weeks — strong
    ("2-4w", "low"):  0.615, ("2-4w", "med"):  0.709, ("2-4w", "high"): 0.677, ("2-4w", "whale"): 0.615,
    # 1-3 months — very strong
    ("1-3m", "low"):  0.779, ("1-3m", "med"):  0.804, ("1-3m", "high"): 0.759, ("1-3m", "whale"): 0.753,
    # 3+ months — strongest
    ("3m+", "low"):   0.867, ("3m+", "med"):   0.850, ("3m+", "high"): 0.792, ("3m+", "whale"): 0.804,
}

# Baseline NO WR = 59.1% (overall average)
BASELINE_NO_WR = 0.591

# Bet size multipliers derived from NO WR relative to baseline
# multiplier = clamp(NO_WR / BASELINE, 0.80, 1.25)
# This replaces the old simple duration modifier
MIN_MULTIPLIER = 0.80
MAX_MULTIPLIER = 1.25


def _classify_duration(days_to_close: float) -> str:
    """Map days to close to duration bucket."""
    hours = days_to_close * 24
    if hours < 24:
        return "<1d"
    elif hours < 72:
        return "1-3d"
    elif hours < 168:
        return "3-7d"
    elif hours < 336:
        return "1-2w"
    elif hours < 720:
        return "2-4w"
    elif hours < 2160:
        return "1-3m"
    else:
        return "3m+"


def _classify_volume(volume: float) -> str:
    """Map volume to bucket."""
    if volume < 1000:
        return "low"
    elif volume < 10000:
        return "med"
    elif volume < 100000:
        return "high"
    else:
        return "whale"


def get_time_decay_modifier(
    days_to_close: float,
    volume: float = 5000,
    side: str = "NO",
) -> Dict[str, Any]:
    """Calculate Becker-validated bet size modifier based on duration × volume.

    Args:
        days_to_close: Days until market closes
        volume: Market volume in dollars
        side: "YES" or "NO"

    Returns:
        {
            "multiplier": float,      # Bet size multiplier (0.80 - 1.25)
            "no_wr": float,           # Expected NO win rate from Becker data
            "duration": str,          # Duration bucket
            "volume_bucket": str,     # Volume bucket
            "edge_vs_baseline": float # NO WR - baseline
        }
    """
    dur_key = _classify_duration(days_to_close)
    vol_key = _classify_volume(volume)

    no_wr = BECKER_NO_WR.get((dur_key, vol_key), BASELINE_NO_WR)

    if side == "NO":
        # Boost when NO WR is above baseline, penalize when below
        raw_mult = no_wr / BASELINE_NO_WR
    else:
        # For YES bets, invert: penalize when NO WR is high
        raw_mult = (1 - no_wr) / (1 - BASELINE_NO_WR)

    multiplier = max(MIN_MULTIPLIER, min(MAX_MULTIPLIER, raw_mult))
    edge_vs_baseline = no_wr - BASELINE_NO_WR

    logger.debug(
        "Time decay: dur=%s vol=%s no_wr=%.1f%% mult=%.2f edge=%+.1f%% side=%s",
        dur_key, vol_key, no_wr * 100, multiplier, edge_vs_baseline * 100, side
    )

    if abs(edge_vs_baseline) > 0.10:
        logger.info(
            "Time decay %s: dur=%s vol=%s no_wr=%.1f%% mult=%.2f (%.1f%% vs baseline)",
            "BOOST" if edge_vs_baseline > 0 else "PENALTY",
            dur_key, vol_key, no_wr * 100, multiplier, edge_vs_baseline * 100
        )

    return {
        "multiplier": round(multiplier, 3),
        "no_wr": round(no_wr, 3),
        "duration": dur_key,
        "volume_bucket": vol_key,
        "edge_vs_baseline": round(edge_vs_baseline, 3),
    }


def get_optimal_entry_windows() -> Dict[str, Any]:
    """Return the ranked entry windows for debugging/dashboard.

    Sorted by NO WR descending, with sample sizes from Becker backtest.
    """
    SAMPLE_SIZES = {
        ("<1d", "low"): 20773, ("<1d", "med"): 19585, ("<1d", "high"): 11249, ("<1d", "whale"): 2204,
        ("1-3d", "low"): 7031, ("1-3d", "med"): 25797, ("1-3d", "high"): 34079, ("1-3d", "whale"): 9091,
        ("3-7d", "low"): 7917, ("3-7d", "med"): 16670, ("3-7d", "high"): 17032, ("3-7d", "whale"): 6725,
        ("1-2w", "low"): 5741, ("1-2w", "med"): 12174, ("1-2w", "high"): 11910, ("1-2w", "whale"): 8133,
        ("2-4w", "low"): 3772, ("2-4w", "med"): 6140, ("2-4w", "high"): 5660, ("2-4w", "whale"): 2868,
        ("1-3m", "low"): 271, ("1-3m", "med"): 2470, ("1-3m", "high"): 4260, ("1-3m", "whale"): 3154,
        ("3m+", "low"): 347, ("3m+", "med"): 1963, ("3m+", "high"): 3160, ("3m+", "whale"): 3648,
    }

    windows = []
    for (dur, vol), wr in sorted(BECKER_NO_WR.items(), key=lambda x: x[1], reverse=True):
        n = SAMPLE_SIZES.get((dur, vol), 0)
        windows.append({
            "duration": dur,
            "volume": vol,
            "no_wr": round(wr * 100, 1),
            "sample_size": n,
            "multiplier": round(max(MIN_MULTIPLIER, min(MAX_MULTIPLIER, wr / BASELINE_NO_WR)), 3),
        })

    return {"windows": windows, "baseline_no_wr": round(BASELINE_NO_WR * 100, 1)}

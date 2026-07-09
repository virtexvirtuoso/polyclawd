#!/usr/bin/env python3
"""book_weights.py — Sport-specific book weighting with liquidity gating.

Phase 3 of Cross-Sport Edge Methodology Upgrade.

The problem:
  Global BOOK_WEIGHTS gives Betfair 60% weight for US sports where exchange
  liquidity is thin. Pinnacle is universally sharp but not always present.
  DraftKings/FanDuel are soft on soccer but sharp on US sports (market depth).

The solution:
  1. Sport-specific default weights (informed by market structure, not just
     "sharp" reputation)
  2. Liquidity gate: if a book quotes < MIN_GAMES_PCT of events in a sport,
     zero its weight for that sport
  3. Data-driven recalibration: once we have N≥100 CLV-resolved trades per
     sport, compute per-book Brier scores and update weights

Usage:
  from odds.book_weights import get_weights
  weights = get_weights("baseball_mlb")  # sport-specific + liquidity-gated
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict, Optional

# ── Sport-specific default weights ────────────────────────────────────
# These are PROVISIONAL — will be replaced by data-driven weights after
# sufficient CLV data accumulates (target: N≥100 per sport).
#
# Rationale for defaults:
# - MLB/NBA: Pinnacle is the benchmark. Betfair exchange has volume but
#   less than soccer. DK/FD are sharp on US sports (huge market depth).
# - Soccer: Betfair exchange is the gold standard (deep liquidity).
#   Pinnacle is sharp but has wider spreads on niche leagues.
# - UFC: Small market. Pinnacle is sharpest. Betfair has limited depth.
#   DK/FD are softer (less institutional flow).

SPORT_WEIGHTS: Dict[str, Dict[str, float]] = {
    "baseball_mlb": {
        "pinnacle": 0.40,
        "betfair_ex_uk": 0.10,
        "betfair_ex_eu": 0.10,
        "draftkings": 0.25,
        "fanduel": 0.20,
        "betmgm": 0.10,
        "betrivers": 0.05,
        "williamhill_us": 0.05,
        "williamhill": 0.05,
        "bovada": 0.02,
    },
    "soccer": {
        "pinnacle": 0.20,
        "betfair_ex_uk": 0.35,
        "betfair_ex_eu": 0.35,
        "draftkings": 0.15,
        "fanduel": 0.10,
        "betmgm": 0.05,
        "betrivers": 0.05,
        "williamhill_us": 0.02,
        "williamhill": 0.10,
        "bovada": 0.02,
    },
    "ufc": {
        "pinnacle": 0.45,
        "betfair_ex_uk": 0.10,
        "betfair_ex_eu": 0.10,
        "draftkings": 0.20,
        "fanduel": 0.15,
        "betmgm": 0.10,
        "betrivers": 0.05,
        "williamhill_us": 0.05,
        "williamhill": 0.05,
        "bovada": 0.02,
    },
    "nba": {
        "pinnacle": 0.40,
        "betfair_ex_uk": 0.05,
        "betfair_ex_eu": 0.05,
        "draftkings": 0.25,
        "fanduel": 0.20,
        "betmgm": 0.15,
        "betrivers": 0.05,
        "williamhill_us": 0.05,
        "williamhill": 0.05,
        "bovada": 0.02,
    },
    "nfl": {
        "pinnacle": 0.35,
        "betfair_ex_uk": 0.05,
        "betfair_ex_eu": 0.05,
        "draftkings": 0.25,
        "fanduel": 0.25,
        "betmgm": 0.15,
        "betrivers": 0.05,
        "williamhill_us": 0.05,
        "williamhill": 0.05,
        "bovada": 0.02,
    },
}

# Fallback = current global weights (from sports_edge_common.py)
DEFAULT_WEIGHTS: Dict[str, float] = {
    "pinnacle": 0.35,
    "betfair_ex_uk": 0.30,
    "betfair_ex_eu": 0.30,
    "draftkings": 0.20,
    "fanduel": 0.15,
    "betmgm": 0.10,
    "betrivers": 0.05,
    "williamhill_us": 0.05,
    "williamhill": 0.05,
    "bovada": 0.02,
}

# ── Liquidity gate ────────────────────────────────────────────────────
# If a book quotes fewer than this fraction of events, zero its weight.
# Prevents low-coverage books from injecting noise into consensus.
MIN_GAMES_PCT = 0.25  # book must quote ≥25% of events to contribute

DB_PATH = Path(__file__).resolve().parent.parent / "storage" / "shadow_trades.db"


def _sport_family(sport_key: str) -> str:
    """Map an Odds API sport key to a weight family."""
    if "baseball" in sport_key:
        return "baseball_mlb"
    if "soccer" in sport_key or "fifa" in sport_key:
        return "soccer"
    if "ufc" in sport_key or "mma" in sport_key:
        return "ufc"
    if "basketball" in sport_key or "nba" in sport_key:
        return "nba"
    if "football" in sport_key or "nfl" in sport_key:
        return "nfl"
    return ""


def get_weights(sport_key: str) -> Dict[str, float]:
    """Get sport-specific book weights. Falls back to global default."""
    family = _sport_family(sport_key)
    return dict(SPORT_WEIGHTS.get(family, DEFAULT_WEIGHTS))


def get_weights_with_coverage(sport_key: str, book_coverage: Dict[str, float]) -> Dict[str, float]:
    """Get sport-specific weights, zeroing books below the liquidity gate.

    Args:
        sport_key: e.g. "baseball_mlb", "soccer_fifa_world_cup"
        book_coverage: {book_key: fraction_of_events_quoted} from 0-1

    Returns:
        Adjusted weights (zeroed for low-coverage books, re-normalized).
    """
    base = get_weights(sport_key)
    adjusted = {}
    for book, weight in base.items():
        coverage = book_coverage.get(book, 0.0)
        if coverage < MIN_GAMES_PCT:
            adjusted[book] = 0.0
        else:
            adjusted[book] = weight

    # Re-normalize so remaining weights sum to roughly 1
    total = sum(adjusted.values())
    if total > 0:
        adjusted = {k: v / total for k, v in adjusted.items()}

    return adjusted


# ── Data-driven recalibration ─────────────────────────────────────────
# After N≥100 resolved trades with CLV data per sport, compute per-book
# Brier scores and generate new weights.

MIN_TRADES_FOR_RECALIBRATION = 100


def compute_book_brier_scores(sport_family: str) -> Optional[Dict[str, dict]]:
    """Compute per-book Brier scores from resolved shadow trades.

    This requires the edge_scan_log table to have per-book probabilities logged,
    which is a future enhancement. For now, returns None (insufficient data).

    When data is available, the algorithm:
    1. For each resolved trade, find which books quoted it
    2. Compute Brier score per book: (book_prob - actual_outcome)^2
    3. Lower Brier = sharper book = higher weight

    Returns:
        {book_key: {brier: float, n: int, suggested_weight: float}} or None
    """
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        # Check if we have enough CLV data
        row = conn.execute("""
            SELECT COUNT(*) FROM shadow_trades
            WHERE resolved=1 AND closing_yes_mid IS NOT NULL
            AND strategy LIKE ?
        """, (f"{sport_family}%",)).fetchone()
        n = row[0] if row else 0
        conn.close()

        if n < MIN_TRADES_FOR_RECALIBRATION:
            return None  # Not enough data yet

        # TODO: When edge_scan_log has per-book probabilities,
        # compute Brier per book and return suggested weights.
        return None
    except Exception:
        return None


def recalibrate_weights(sport_family: str) -> Optional[Dict[str, float]]:
    """Attempt to recalibrate weights from data. Returns new weights or None."""
    brier = compute_book_brier_scores(sport_family)
    if brier is None:
        return None

    # Inverse Brier weighting: lower Brier = higher weight
    # w_i = (1/brier_i) / sum(1/brier_j)
    inv_brier = {}
    for book, data in brier.items():
        if data["brier"] > 0 and data["n"] >= 20:
            inv_brier[book] = 1.0 / data["brier"]

    if not inv_brier:
        return None

    total = sum(inv_brier.values())
    return {book: w / total for book, w in inv_brier.items()}


if __name__ == "__main__":
    print("Sport-specific book weights:")
    for sport in ["baseball_mlb", "soccer_fifa_world_cup", "ufc", "nba", "nfl"]:
        weights = get_weights(sport)
        family = _sport_family(sport)
        print(f"\n{sport} (family: {family}):")
        for book, w in sorted(weights.items(), key=lambda x: -x[1]):
            print(f"  {book:20s}: {w:.2f}")

    # Check recalibration readiness
    print("\n\nRecalibration readiness:")
    for family in ["baseball_mlb", "soccer", "ufc"]:
        result = compute_book_brier_scores(family)
        if result is None:
            print(f"  {family}: insufficient data (need {MIN_TRADES_FOR_RECALIBRATION}+ CLV-resolved trades)")
        else:
            print(f"  {family}: ready — {result}")

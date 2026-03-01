#!/usr/bin/env python3
"""
Resolution Logger & Scorecard

Shared by tweet_count_scanner and weather_scanner.
Append-only JSONL logs. Read at scan time for calibration.

Files:
  storage/tweet_resolutions.jsonl
  storage/weather_resolutions.jsonl
"""

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("polyclawd.resolution_logger")

STORAGE = Path(__file__).parent.parent / "storage"

# Strategy → log file
LOG_FILES = {
    "tweet_count_mc": STORAGE / "tweet_resolutions.jsonl",
    "weather_ensemble": STORAGE / "weather_resolutions.jsonl",
}


# ============================================================================
# Write side (called from watchdog/paper_portfolio on resolution)
# ============================================================================

def log_resolution(strategy: str, record: dict):
    """Append one resolution record to the appropriate JSONL file.
    
    Required fields in record:
        market_id, side, mc_prob, market_price, won (bool)
    
    Optional but useful:
        handle, event_slug, bracket, edge_pct, actual_value,
        archetype, confidence, entry_price
    """
    log_file = LOG_FILES.get(strategy)
    if not log_file:
        logger.debug("No log file for strategy %s", strategy)
        return

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "strategy": strategy,
        **record,
    }

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
        logger.info("Logged resolution: %s %s %s → %s",
                     strategy, record.get("market_id", "")[:16],
                     record.get("side", ""), "WIN" if record.get("won") else "LOSS")
    except Exception as e:
        logger.warning("Failed to log resolution: %s", e)


# ============================================================================
# Read side (called from scanners at scan time)
# ============================================================================

def load_resolutions(strategy: str) -> List[dict]:
    """Load all resolution records for a strategy."""
    log_file = LOG_FILES.get(strategy)
    if not log_file or not log_file.exists():
        return []

    records = []
    try:
        with open(log_file, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except Exception as e:
        logger.warning("Failed to load resolutions: %s", e)
    return records


def get_scorecard(strategy: str) -> Optional[dict]:
    """Compute calibration scorecard from resolved data.
    
    Returns None if <20 resolutions (not enough data).
    
    Returns dict with:
        n: total resolutions
        brier: Brier score (lower = better, <0.15 good, >0.25 bad)
        win_rate: fraction of trades that won
        edge_accuracy: mean(predicted_edge - realized_edge)
        by_side: {YES: {n, brier, wr}, NO: {n, brier, wr}}
        by_bracket: {bracket: {n, brier, wr}} (tweet only)
        calibration: [(bin_center, predicted_prob, actual_freq, n)]
    """
    records = load_resolutions(strategy)
    if len(records) < 20:
        return None

    total_brier = 0
    wins = 0
    by_side = {"YES": {"brier": 0, "n": 0, "wins": 0},
               "NO": {"brier": 0, "n": 0, "wins": 0}}
    by_bracket = {}
    cal_bins = {}  # bin → (sum_predicted, sum_actual, n)

    for r in records:
        mc_prob = r.get("mc_prob", 0)
        won = r.get("won", False)
        side = r.get("side", "YES")
        bracket = r.get("bracket", "")

        # Brier score: (predicted_prob - actual_outcome)^2
        actual = 1.0 if won else 0.0
        brier = (mc_prob - actual) ** 2
        total_brier += brier

        if won:
            wins += 1

        # By side
        by_side[side]["brier"] += brier
        by_side[side]["n"] += 1
        if won:
            by_side[side]["wins"] += 1

        # By bracket (tweet markets)
        if bracket:
            if bracket not in by_bracket:
                by_bracket[bracket] = {"brier": 0, "n": 0, "wins": 0}
            by_bracket[bracket]["brier"] += brier
            by_bracket[bracket]["n"] += 1
            if won:
                by_bracket[bracket]["wins"] += 1

        # Calibration bins (0.1 wide)
        bin_key = round(mc_prob * 10) / 10  # 0.0, 0.1, ..., 1.0
        if bin_key not in cal_bins:
            cal_bins[bin_key] = [0.0, 0.0, 0]
        cal_bins[bin_key][0] += mc_prob
        cal_bins[bin_key][1] += actual
        cal_bins[bin_key][2] += 1

    n = len(records)
    brier = total_brier / n
    wr = wins / n

    # Finalize by_side
    for side in by_side:
        s = by_side[side]
        if s["n"] > 0:
            s["brier"] = round(s["brier"] / s["n"], 4)
            s["wr"] = round(s["wins"] / s["n"], 3)
        else:
            s["brier"] = None
            s["wr"] = None

    # Finalize by_bracket
    for b in by_bracket:
        d = by_bracket[b]
        if d["n"] > 0:
            d["brier"] = round(d["brier"] / d["n"], 4)
            d["wr"] = round(d["wins"] / d["n"], 3)

    # Calibration curve
    calibration = []
    for bin_center in sorted(cal_bins.keys()):
        pred_sum, actual_sum, count = cal_bins[bin_center]
        calibration.append((
            bin_center,
            round(pred_sum / count, 3),
            round(actual_sum / count, 3),
            count,
        ))

    return {
        "strategy": strategy,
        "n": n,
        "brier": round(brier, 4),
        "win_rate": round(wr, 3),
        "by_side": by_side,
        "by_bracket": by_bracket if by_bracket else None,
        "calibration": calibration,
        "assessment": _assess(brier, wr, n),
    }


def _assess(brier: float, wr: float, n: int) -> str:
    """Human-readable assessment."""
    if n < 50:
        confidence = "low confidence"
    elif n < 150:
        confidence = "moderate confidence"
    else:
        confidence = "high confidence"

    if brier < 0.12:
        quality = "excellent calibration"
    elif brier < 0.18:
        quality = "good calibration"
    elif brier < 0.25:
        quality = "fair calibration — consider recency weighting"
    else:
        quality = "poor calibration — model needs fixing or edge may not be real"

    return f"{quality} ({confidence}, n={n})"


# ============================================================================
# CLI
# ============================================================================

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")

    strategy = sys.argv[1] if len(sys.argv) > 1 else "tweet_count_mc"
    records = load_resolutions(strategy)
    print(f"\n{strategy}: {len(records)} resolutions logged")

    card = get_scorecard(strategy)
    if card:
        print(f"Brier: {card['brier']}")
        print(f"Win rate: {card['win_rate']:.1%}")
        print(f"Assessment: {card['assessment']}")
        print(f"\nBy side:")
        for side, d in card['by_side'].items():
            if d['n'] > 0:
                print(f"  {side}: n={d['n']} brier={d['brier']} wr={d['wr']:.1%}")
        if card.get('calibration'):
            print(f"\nCalibration curve:")
            print(f"  {'Bin':>5s}  {'Pred':>5s}  {'Actual':>6s}  {'N':>4s}")
            for bc, pred, actual, count in card['calibration']:
                bar = "█" * int(actual * 20)
                print(f"  {bc:>5.1f}  {pred:>5.3f}  {actual:>6.3f}  {count:>4d}  {bar}")
    else:
        print("Need 20+ resolutions for scorecard.")

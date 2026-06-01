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
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("polyclawd.resolution_logger")

STORAGE = Path(__file__).parent.parent / "storage"

# Strategy → log file
#
# Two channels (Option-B split, 2026-04-28):
#   LOG_FILES        — every close (auto-resolve, stop, manual, weather-reeval).
#                       Conflates model accuracy with stop-policy outcome.
#                       Use for stop_policy_outcome Brier ONLY.
#   AUTO_LOG_FILES   — auto-resolved closes only (status=won/lost via market
#                       resolution, never via stop/manual). True model
#                       calibration signal.
#
# Calibration tracker should compute Brier separately for each. Mixing them
# (the bug we hit) makes a calibrated model look RED whenever stops fire.
LOG_FILES = {
    "tweet_count_mc": STORAGE / "tweet_resolutions.jsonl",
    "weather_ensemble": STORAGE / "weather_resolutions.jsonl",
    "options_implied": STORAGE / "options_resolutions.jsonl",
}

AUTO_LOG_FILES = {
    "tweet_count_mc": STORAGE / "tweet_resolutions_auto.jsonl",
    "weather_ensemble": STORAGE / "weather_resolutions_auto.jsonl",
    "options_implied": STORAGE / "options_resolutions_auto.jsonl",
}


def _model_p_yes_from_forecast(entry_forecast_json: str | None,
                               fallback_confidence: float | None,
                               side: str) -> float:
    """Compute the model's P(NO-side-wins) at entry from the stored forecast.

    For weather: use the ensemble mean/std/threshold/comparison to compute
    P(YES) via Φ-CDF, then return 1 - P(YES) for NO bets (or P(YES) for YES).

    Falls back to `fallback_confidence` if the forecast field is absent or
    malformed (which is current behaviour for tweet_count_mc and pre-2026-03
    weather rows). Confidence is a tier value, not a true probability — so
    the fallback is informational only and the calibration metric should be
    interpreted with that caveat.
    """
    if entry_forecast_json:
        try:
            f = json.loads(entry_forecast_json)

            # Schema A — social_count / tweet_count_mc: MC probabilities
            # already pre-computed at signal time.
            if f.get("type") == "tweet_count_mc":
                p_yes = f.get("mc_yes_prob")
                if p_yes is None and f.get("mc_no_prob") is not None:
                    p_yes = 1.0 - float(f["mc_no_prob"])
                if p_yes is not None and 0 <= p_yes <= 1:
                    p_yes = max(0.001, min(0.999, float(p_yes)))
                    return 1 - p_yes if (side or "").upper() == "NO" else p_yes

            # Schema C — options_implied: N(d2) model P(YES) at entry.
            if f.get("type") == "options_implied":
                p_yes = f.get("implied_prob")
                if p_yes is not None and 0 <= p_yes <= 1:
                    p_yes = max(0.001, min(0.999, float(p_yes)))
                    return 1 - p_yes if (side or "").upper() == "NO" else p_yes

            # Schema B — weather: ensemble mean/std/threshold + comparison.
            mean = f.get("forecast_mean_f")
            std = f.get("forecast_std_f")
            thr = f.get("threshold_f")
            comp = f.get("comparison")
            if all(v is not None for v in (mean, std, thr, comp)) and std > 0:
                def cdf(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
                if comp == "between":
                    lo, hi = thr, thr + 2.0
                    p_yes = cdf((hi - mean) / std) - cdf((lo - mean) / std)
                elif comp == "exact":
                    p_yes = cdf((thr + 1 - mean) / std) - cdf((thr - 1 - mean) / std)
                elif comp == "above":
                    p_yes = 1.0 - cdf((thr - mean) / std)
                elif comp == "below":
                    p_yes = cdf((thr - mean) / std)
                else:
                    p_yes = None
                if p_yes is not None and 0 <= p_yes <= 1:
                    p_yes = max(0.001, min(0.999, p_yes))
                    return 1 - p_yes if (side or "").upper() == "NO" else p_yes
        except (json.JSONDecodeError, TypeError, KeyError):
            pass
    return float(fallback_confidence or 0.5)


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


def log_auto_resolution(strategy: str, record: dict):
    """Append an auto-resolution record to `{strategy}_resolutions_auto.jsonl`.

    This file should ONLY receive records where the market itself resolved
    (status='won' or 'lost' via auto-resolve), never manual closes or stops.
    Use this file for true model-calibration Brier scoring.

    Required fields: market_id, side, mc_prob, won, market_price.
    """
    log_file = AUTO_LOG_FILES.get(strategy)
    if not log_file:
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
    except Exception as e:
        logger.warning("Failed to log auto resolution: %s", e)


def log_position_close(pos, won: bool, pnl: float, close_reason: str,
                       closing_line: float | None = None) -> None:
    """Single writer for both outcome and (when applicable) auto resolution logs.

    Called from every position-closing path (stop_evaluator, weather reeval,
    auto-resolve, manual close). Centralizes the calibration instrumentation
    so adding a new closing path can't accidentally leave the metrics stale.

    Behaviour:
      • Always writes to LOG_FILES[strategy] (outcome metric — every close).
      • Also writes to AUTO_LOG_FILES[strategy] (model metric — auto-resolved
        only) when close_reason starts with "auto-resolved:".
      • Recovers mc_prob from pos["entry_forecast_json"] via
        `_model_p_yes_from_forecast()` so both files use the calibrated
        model probability, never the raw confidence tier.
      • No-op for strategies outside LOG_FILES (silent — most archetypes
        aren't calibration-tracked).

    Args:
        pos: sqlite3.Row or dict from paper_positions. Must expose:
             strategy, side, entry_price, market_id, market_title,
             archetype, edge_pct, confidence; optional entry_forecast_json.
        won: True if outcome favors the bet, False otherwise.
        pnl: Realized P&L (signed dollars).
        close_reason: Free-text reason. Auto-resolved entries must use
             prefix "auto-resolved:" so the model metric is fed.
        closing_line: YES-token price at close (for CLV tracking).
    """
    keys = _row_keys(pos)

    def _get(k, default=None):
        return pos[k] if k in keys else default

    # Discord alert — every close, regardless of calibration tracking.
    # Was previously only fired by the dead close_position() path; restored
    # here so all four closing paths surface position resolutions to ops.
    # Wrapped to keep the close path resilient if the webhook fails.
    try:
        from signals.discord_alerts import alert_position_closed
        _exit = closing_line if closing_line is not None else (_get("exit_price") or 0.0)
        alert_position_closed(
            market_title=_get("market_title") or "",
            side=_get("side") or "",
            outcome="won" if won else "lost",
            pnl=pnl,
            entry_price=_get("entry_price") or 0.0,
            exit_price=_exit or 0.0,
            strategy=_get("strategy") or "",
            close_reason=close_reason,
            slug=_get("market_slug") or "",
        )
    except Exception as e:
        logger.warning("Discord position-closed alert failed: %s", e)

    strategy_raw = _get("strategy", "") or ""
    strategy = "weather_ensemble" if strategy_raw == "weather" else strategy_raw
    if strategy not in LOG_FILES:
        return

    side = _get("side") or ""
    mc_prob = _model_p_yes_from_forecast(
        _get("entry_forecast_json"),
        _get("confidence"),
        side,
    )

    record = {
        "market_id": _get("market_id"),
        "market_title": _get("market_title") or "",
        "side": side,
        "mc_prob": round(mc_prob, 4),
        "market_price": round(_get("entry_price") or 0, 4),
        "edge_pct": round(_get("edge_pct") or 0, 4),
        "archetype": _get("archetype") or "",
        "won": bool(won),
        "pnl": round(pnl, 2),
        "close_reason": close_reason,
    }
    if closing_line is not None:
        record["closing_line"] = closing_line

    # Outcome log — every close
    log_resolution(strategy, record)

    # Model log — auto-resolved only (preserves the Apr-28 split)
    if close_reason.startswith("auto-resolved:"):
        log_auto_resolution(strategy, record)


def _row_keys(pos) -> set:
    """Return key set for sqlite3.Row, dict, or anything keys()-supporting."""
    try:
        return set(pos.keys())
    except Exception:
        return set()


# ============================================================================
# Read side (called from scanners at scan time)
# ============================================================================

def load_resolutions(strategy: str) -> List[dict]:
    """Load all resolution records for a strategy (mixed close-types)."""
    return _load_jsonl(LOG_FILES.get(strategy))


def load_auto_resolutions(strategy: str) -> List[dict]:
    """Load only auto-resolved (true model-calibration) records."""
    return _load_jsonl(AUTO_LOG_FILES.get(strategy))


def _load_jsonl(log_file) -> List[dict]:
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
        logger.warning("Failed to load %s: %s", log_file, e)
    return records


def get_auto_scorecard(strategy: str) -> Optional[dict]:
    """Compute model-calibration Brier from auto-resolved records only.

    Returns None if <20 auto resolutions. Otherwise a small dict:
        {n, brier, win_rate, source: 'auto'}
    Use this for the model_calibration_brier metric. Use `get_scorecard`
    for stop_policy_outcome_brier.
    """
    records = load_auto_resolutions(strategy)
    n = len(records)
    if n < 20:
        return None
    total_brier = 0.0
    wins = 0
    for r in records:
        p = float(r.get("mc_prob", 0.5))
        won = bool(r.get("won", False))
        total_brier += (p - (1.0 if won else 0.0)) ** 2
        if won:
            wins += 1
    return {
        "n": n,
        "brier": total_brier / n,
        "win_rate": wins / n,
        "source": "auto",
    }


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

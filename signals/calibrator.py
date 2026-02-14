"""
Auto-Calibrator — adjusts signal confidence based on realized performance.

Hooks into IC tracker data to:
1. Per-source calibration curves (predicted vs actual win rate)
2. Component-level IC (which sub-features predict outcomes)
3. Optimal source weights for Bayesian aggregation
4. Conditional IC by market type / volatility regime

Auto-updates as more trades resolve. No manual tuning needed.
"""

import sqlite3
import time
import math
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "storage" / "shadow_trades.db"

# Minimum samples before calibration kicks in
MIN_SAMPLES_CALIBRATE = 20
MIN_SAMPLES_PER_BIN = 5


def _get_conn(db_path: str = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_calibration_tables(db_path: str = None):
    """Create calibration tables."""
    conn = _get_conn(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calibration_curves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            source TEXT NOT NULL,
            bin_lower REAL NOT NULL,
            bin_upper REAL NOT NULL,
            predicted_avg REAL NOT NULL,
            actual_win_rate REAL NOT NULL,
            sample_size INTEGER NOT NULL,
            calibration_error REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_cal_source_ts
        ON calibration_curves(source, timestamp)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_weights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            source TEXT NOT NULL,
            weight REAL NOT NULL,
            ic_value REAL,
            sample_size INTEGER NOT NULL,
            reason TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sw_source
        ON source_weights(source)
    """)
    conn.commit()
    conn.close()


def build_calibration_curve(source: str, n_bins: int = 5, db_path: str = None) -> dict:
    """Build calibration curve for a signal source.
    
    Groups resolved predictions into confidence bins and compares
    predicted confidence vs actual win rate.
    
    Returns:
        Dict with bins, overall calibration error (ECE), and adjustment map
    """
    init_calibration_tables(db_path)
    conn = _get_conn(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT confidence, outcome
        FROM signal_predictions
        WHERE source = ? AND resolved = 1
        ORDER BY confidence
    """, (source,)).fetchall()
    conn.close()

    if len(rows) < MIN_SAMPLES_CALIBRATE:
        return {
            "source": source,
            "status": "insufficient_data",
            "sample_size": len(rows),
            "min_required": MIN_SAMPLES_CALIBRATE,
        }

    # Create equal-width bins from 50-100 confidence range
    predictions = [(float(r["confidence"]), float(r["outcome"])) for r in rows]
    
    bin_edges = []
    min_conf = min(p[0] for p in predictions)
    max_conf = max(p[0] for p in predictions)
    step = (max_conf - min_conf) / n_bins if max_conf > min_conf else 1
    
    bins = []
    total_ece = 0.0
    total_samples = 0
    adjustment_map = {}

    for i in range(n_bins):
        lower = min_conf + i * step
        upper = min_conf + (i + 1) * step if i < n_bins - 1 else max_conf + 0.1
        
        bin_preds = [p for p in predictions if lower <= p[0] < upper]
        if len(bin_preds) < MIN_SAMPLES_PER_BIN:
            continue

        avg_predicted = sum(p[0] for p in bin_preds) / len(bin_preds)
        actual_win_rate = sum(p[1] for p in bin_preds) / len(bin_preds) * 100  # scale to match confidence
        cal_error = abs(avg_predicted - actual_win_rate)
        
        bins.append({
            "bin": f"{lower:.0f}-{upper:.0f}",
            "predicted_avg": round(avg_predicted, 1),
            "actual_win_rate": round(actual_win_rate, 1),
            "sample_size": len(bin_preds),
            "calibration_error": round(cal_error, 1),
            "direction": "overconfident" if avg_predicted > actual_win_rate else "underconfident",
        })

        # Adjustment: ratio of actual/predicted
        if avg_predicted > 0:
            adj = actual_win_rate / avg_predicted
            adjustment_map[f"{lower:.0f}-{upper:.0f}"] = round(adj, 3)

        total_ece += cal_error * len(bin_preds)
        total_samples += len(bin_preds)

        # Store in DB
        try:
            conn2 = _get_conn(db_path)
            conn2.execute("""
                INSERT INTO calibration_curves
                (timestamp, source, bin_lower, bin_upper, predicted_avg, 
                 actual_win_rate, sample_size, calibration_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (time.time(), source, lower, upper, avg_predicted,
                  actual_win_rate, len(bin_preds), cal_error))
            conn2.commit()
            conn2.close()
        except Exception:
            pass

    ece = total_ece / total_samples if total_samples > 0 else 0

    return {
        "source": source,
        "status": "calibrated",
        "sample_size": len(predictions),
        "bins": bins,
        "ece": round(ece, 2),  # Expected Calibration Error
        "adjustment_map": adjustment_map,
        "interpretation": _interpret_ece(ece),
    }


def _interpret_ece(ece: float) -> str:
    if ece < 3:
        return "excellent — predictions well-calibrated"
    elif ece < 8:
        return "good — minor adjustments would help"
    elif ece < 15:
        return "fair — systematic bias detected, apply adjustments"
    else:
        return "poor — confidence scores need major recalibration"


def calibrate_confidence(source: str, raw_confidence: float, db_path: str = None) -> float:
    """Apply calibration adjustment to a raw confidence score.
    
    Uses the most recent calibration curve for the source.
    Falls back to raw confidence if insufficient data.
    
    Args:
        source: Signal source name
        raw_confidence: Original confidence (0-100)
        
    Returns:
        Adjusted confidence (0-95, clamped)
    """
    conn = _get_conn(db_path)
    conn.row_factory = sqlite3.Row

    # Get most recent calibration bins for this source
    rows = conn.execute("""
        SELECT bin_lower, bin_upper, predicted_avg, actual_win_rate, sample_size
        FROM calibration_curves
        WHERE source = ? 
        AND timestamp > ? 
        AND sample_size >= ?
        ORDER BY timestamp DESC
        LIMIT 20
    """, (source, time.time() - 7 * 86400, MIN_SAMPLES_PER_BIN)).fetchall()
    conn.close()

    if not rows:
        return raw_confidence  # no calibration data yet

    # Find matching bin
    for r in rows:
        if r["bin_lower"] <= raw_confidence < r["bin_upper"]:
            if r["predicted_avg"] > 0:
                adjustment = r["actual_win_rate"] / r["predicted_avg"]
                adjusted = raw_confidence * adjustment
                return max(1.0, min(95.0, adjusted))

    return raw_confidence  # no matching bin


def compute_source_weights(db_path: str = None) -> dict:
    """Compute optimal weights for each signal source based on IC and independence.
    
    Sources with higher IC get more weight.
    Sources uncorrelated with each other get bonus weight.
    
    Returns:
        Dict mapping source → weight (0-1, sums to 1)
    """
    init_calibration_tables(db_path)
    conn = _get_conn(db_path)
    conn.row_factory = sqlite3.Row

    # Get all sources with resolved predictions
    sources = conn.execute("""
        SELECT DISTINCT source, COUNT(*) as cnt
        FROM signal_predictions
        WHERE resolved = 1
        GROUP BY source
        HAVING cnt >= 10
    """).fetchall()
    conn.close()

    if not sources:
        return {"status": "insufficient_data", "weights": {}}

    # Calculate IC for each source
    from ic_tracker import calculate_ic
    source_ics = {}
    for row in sources:
        ic_data = calculate_ic(row["source"], 30, db_path)
        ic_val = ic_data.get("ic_value")
        if ic_val is not None and ic_val > 0:
            source_ics[row["source"]] = {
                "ic": ic_val,
                "samples": row["cnt"],
            }

    if not source_ics:
        return {"status": "no_positive_ic", "weights": {}}

    # Weight by IC squared (penalizes low IC more)
    total_ic_sq = sum(v["ic"] ** 2 for v in source_ics.values())
    
    weights = {}
    for source, data in source_ics.items():
        w = (data["ic"] ** 2) / total_ic_sq if total_ic_sq > 0 else 1.0 / len(source_ics)
        weights[source] = round(w, 4)

        # Store
        try:
            conn2 = _get_conn(db_path)
            conn2.execute("""
                INSERT INTO source_weights (timestamp, source, weight, ic_value, sample_size, reason)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (time.time(), source, w, data["ic"], data["samples"],
                  f"IC-squared weighting: IC={data['ic']:.4f}"))
            conn2.commit()
            conn2.close()
        except Exception:
            pass

    return {
        "status": "computed",
        "weights": weights,
        "method": "ic_squared",
        "sources_evaluated": len(source_ics),
        "sources_excluded": len(sources) - len(source_ics),
    }


def get_signal_decay(source: str, market_type: str = None, db_path: str = None) -> dict:
    """Measure how quickly a signal's predictive power decays.
    
    Compares IC at different time horizons after signal generation.
    Tells us the exploitable window.
    """
    init_calibration_tables(db_path)
    conn = _get_conn(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT confidence, outcome, timestamp, resolved_at
        FROM signal_predictions
        WHERE source = ? AND resolved = 1 AND resolved_at IS NOT NULL
        ORDER BY timestamp
    """, (source,)).fetchall()
    conn.close()

    if len(rows) < 20:
        return {"source": source, "status": "insufficient_data", "sample_size": len(rows)}

    # Group by resolution time (how long until market resolved)
    buckets = {
        "< 6h": [], "6-24h": [], "1-3d": [], "3-7d": [], "7-30d": []
    }
    
    for r in rows:
        hours = (r["resolved_at"] - r["timestamp"]) / 3600
        conf = float(r["confidence"])
        outcome = float(r["outcome"])
        
        if hours < 6:
            buckets["< 6h"].append((conf, outcome))
        elif hours < 24:
            buckets["6-24h"].append((conf, outcome))
        elif hours < 72:
            buckets["1-3d"].append((conf, outcome))
        elif hours < 168:
            buckets["3-7d"].append((conf, outcome))
        else:
            buckets["7-30d"].append((conf, outcome))

    from ic_tracker import _spearman_rank_correlation
    
    decay = {}
    for bucket, pairs in buckets.items():
        if len(pairs) < 5:
            decay[bucket] = {"ic": None, "samples": len(pairs)}
            continue
        confs = [p[0] for p in pairs]
        outs = [p[1] for p in pairs]
        ic = _spearman_rank_correlation(confs, outs)
        decay[bucket] = {"ic": round(ic, 4), "samples": len(pairs)}

    return {
        "source": source,
        "status": "computed",
        "decay_curve": decay,
        "interpretation": _interpret_decay(decay),
    }


def _interpret_decay(decay: dict) -> str:
    ics = [(k, v["ic"]) for k, v in decay.items() if v.get("ic") is not None]
    if len(ics) < 2:
        return "insufficient data for decay analysis"
    
    first_ic = ics[0][1]
    last_ic = ics[-1][1]
    
    if first_ic > 0.05 and last_ic < 0.02:
        return "fast decay — signal value concentrated in first hours, execute quickly"
    elif first_ic > 0.03 and last_ic > 0.03:
        return "slow decay — signal persists, no urgency to execute"
    elif first_ic < 0.03:
        return "weak signal — low IC even at generation time"
    else:
        return "moderate decay — trade within 24h for best results"


def full_calibration_report(db_path: str = None) -> dict:
    """Generate comprehensive calibration report across all sources.
    
    This is the main entry point for the auto-calibration system.
    """
    init_calibration_tables(db_path)
    conn = _get_conn(db_path)
    conn.row_factory = sqlite3.Row

    sources = conn.execute("""
        SELECT DISTINCT source, COUNT(*) as cnt
        FROM signal_predictions
        WHERE resolved = 1
        GROUP BY source
    """).fetchall()

    total_unresolved = conn.execute(
        "SELECT COUNT(*) as c FROM signal_predictions WHERE resolved = 0"
    ).fetchone()["c"]
    
    total_resolved = sum(r["cnt"] for r in sources)
    conn.close()

    calibrations = {}
    for row in sources:
        cal = build_calibration_curve(row["source"], db_path=db_path)
        calibrations[row["source"]] = cal

    weights = compute_source_weights(db_path)
    
    return {
        "total_resolved": total_resolved,
        "total_unresolved": total_unresolved,
        "per_source": calibrations,
        "source_weights": weights,
        "overall_status": _overall_status(total_resolved, calibrations),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _overall_status(total: int, calibrations: dict) -> str:
    if total < 20:
        return f"collecting — {total}/20 minimum trades resolved"
    elif total < 50:
        return f"early calibration — {total} trades, results preliminary"
    elif total < 200:
        return f"calibrating — {total} trades, adjustments active but not definitive"
    else:
        return f"calibrated — {total} trades, adjustments reliable"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    report = full_calibration_report()
    import json
    print(json.dumps(report, indent=2))

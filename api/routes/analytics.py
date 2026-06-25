"""
Analytics read endpoints — surface cron/scanner-produced data that previously
had no API. Read-only; no execution/trading paths. Added 2026-06-22.

Each handler reads a SQLite table written by a background job and returns a
compact, capped, JSON-friendly view. Errors are returned as graceful payloads
(not 500s) so a dashboard widget degrades cleanly.
"""

import sqlite3
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Query
from loguru import logger

router = APIRouter(tags=["Analytics"])

STORAGE = Path(__file__).parent.parent.parent / "storage"


def _query(db_name: str, sql: str, params=()):
    """Run a read-only query against storage/<db_name> and return list[dict]."""
    db = STORAGE / db_name
    if not db.exists():
        raise FileNotFoundError(f"{db_name} not found")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


@router.get("/api/weather/ensemble-accuracy")
async def weather_ensemble_accuracy(
    city: Optional[str] = None,
    days: int = Query(30, ge=1, le=180),
):
    """Forecast-source skill (RMSE/MAE) by source+city from resolved forecasts.

    Backs onto source_city_rmse (signals/weather_ensemble.py). Only resolved
    rows (error_f IS NOT NULL) are scored. Lower RMSE = more reliable source.
    """
    where = "WHERE error_f IS NOT NULL AND logged_at >= datetime('now', ?)"
    params = [f"-{days} days"]
    if city:
        where += " AND city = ?"
        params.append(city)
    sql = f"""
        SELECT source, city,
               COUNT(*) AS n,
               ROUND(SQRT(AVG(error_f * error_f)), 2) AS rmse_f,
               ROUND(AVG(ABS(error_f)), 2) AS mae_f,
               ROUND(AVG(error_f), 2) AS bias_f
        FROM source_city_rmse
        {where}
        GROUP BY source, city
        HAVING n >= 3
        ORDER BY rmse_f ASC
        LIMIT 500
    """
    try:
        rows = _query("shadow_trades.db", sql, params)
        return {"days": days, "city": city, "count": len(rows), "skill": rows}
    except Exception as e:
        logger.exception("weather_ensemble_accuracy failed: {}", e)
        return {"error": str(e), "skill": []}


@router.get("/api/signals/elections/race-prices")
async def election_race_prices(
    state: Optional[str] = None,
    race: Optional[str] = None,
    days: int = Query(30, ge=1, le=365),
):
    """Per-market election odds time-series (Kalshi/Polymarket daily snapshots).

    Backs onto race_prices (signals/election_db.py). `state` matches the market
    key (substring, case-insensitive); `race` is an exact match.
    """
    where = "WHERE date >= date('now', ?)"
    params = [f"-{days} days"]
    if state:
        where += " AND state LIKE ?"
        params.append(f"%{state}%")
    if race:
        where += " AND race = ?"
        params.append(race)
    sql = f"""
        SELECT date, state, race, r_price, d_price, volume, platform
        FROM race_prices
        {where}
        ORDER BY date DESC
        LIMIT 2000
    """
    try:
        rows = _query("election_trends.db", sql, params)
        return {"days": days, "state": state, "race": race, "count": len(rows), "prices": rows}
    except Exception as e:
        logger.exception("election_race_prices failed: {}", e)
        return {"error": str(e), "prices": []}


@router.get("/api/signals/elections/control-history")
async def election_control_history(days: int = Query(90, ge=1, le=730)):
    """Daily party-control probability series (senate/house/presidency + composite).

    Backs onto control_history (signals/election_db.py).
    """
    sql = """
        SELECT date, senate_r, senate_d, house_r, house_d, pres_r, pres_d, composite
        FROM control_history
        WHERE date >= date('now', ?)
        ORDER BY date ASC
        LIMIT 1000
    """
    try:
        rows = _query("election_trends.db", sql, [f"-{days} days"])
        return {"days": days, "count": len(rows), "series": rows}
    except Exception as e:
        logger.exception("election_control_history failed: {}", e)
        return {"error": str(e), "series": []}


@router.get("/api/whale/outcomes")
async def whale_outcomes(
    severity: Optional[str] = None,
    days: int = Query(30, ge=1, le=365),
):
    """Whale-alert precision audit: hit-rate by severity over resolved alerts.

    Backs onto whale_outcomes (signals/whale_outcomes.py, whale_meta.db). Only
    resolved alerts (done=1) are scored. `ts` is a unix epoch float.
    """
    cutoff = time.time() - days * 86400
    where = "WHERE done = 1 AND ts >= ?"
    params = [cutoff]
    if severity:
        where += " AND severity = ?"
        params.append(severity.upper())
    sql = f"""
        SELECT severity,
               COUNT(*) AS n,
               ROUND(AVG(CASE WHEN correct_1h THEN 1.0 ELSE 0.0 END), 3) AS precision_1h,
               ROUND(AVG(CASE WHEN correct_6h THEN 1.0 ELSE 0.0 END), 3) AS precision_6h,
               ROUND(AVG(CASE WHEN correct_res THEN 1.0 ELSE 0.0 END), 3) AS precision_resolved,
               ROUND(AVG(clv_bps), 1) AS avg_clv_bps
        FROM whale_outcomes
        {where}
        GROUP BY severity
        ORDER BY n DESC
    """
    try:
        rows = _query("whale_meta.db", sql, params)
        return {"days": days, "severity": severity, "by_severity": rows}
    except Exception as e:
        logger.exception("whale_outcomes failed: {}", e)
        return {"error": str(e), "by_severity": []}


@router.get("/api/signals/ai-models/history")
async def ai_models_history(
    company: Optional[str] = None,
    days: int = Query(30, ge=1, le=365),
):
    """LMArena leaderboard history — rank/score over time for a company.

    Backs onto arena_snapshots (signals/ai_model_tracker.py, refreshed every 6h).
    With no `company`, returns the available companies (count + latest snapshot)
    so the caller can pick a valid value.
    """
    try:
        if not company:
            rows = _query(
                "ai_model_tracker.db",
                "SELECT company, COUNT(*) AS snapshots, MAX(timestamp) AS latest "
                "FROM arena_snapshots GROUP BY company ORDER BY snapshots DESC",
            )
            return {"companies": rows, "hint": "pass ?company=<name> for its rank/score history"}
        from ai_model_tracker import get_score_history

        return {"company": company, "days": days, "history": get_score_history(company, days)}
    except Exception as e:
        logger.exception("ai_models_history failed: {}", e)
        return {"error": str(e), "history": []}


@router.get("/api/signals/strategy-ic")
async def strategy_ic(
    window_days: int = Query(90, ge=1, le=365),
    min_n: int = Query(30, ge=10, le=1000),
):
    """Information Coefficient per trading strategy, from REALIZED paper trades.

    IC = Spearman rank correlation of pre-trade `confidence` vs realized `pnl`,
    computed over resolved shadow_trades (the decision AND its outcome live in the
    same row, so no lossy predictions<->trades join). Interpretation:
    IC > 0 = confidence has edge; ~0 = noise; < 0 = contra-indicative.
    Thresholds (|IC|): <0.03 KILL, <0.05 WARN, else OK. `min_n` guards small samples
    (default 30 = statistical-validity floor). `significant_bonferroni` corrects the
    p-value for testing k strategies at once.

    NOTE: distinct from the legacy /api/signals/ic-report (prediction-based, empty
    because signal_predictions and shadow_trades track disjoint markets).
    """
    import math
    from collections import defaultdict

    try:
        from scipy.stats import spearmanr
    except Exception as e:  # pragma: no cover
        return {"error": f"scipy unavailable: {e}", "strategies": []}

    try:
        rows = _query(
            "shadow_trades.db",
            "SELECT strategy, confidence, pnl FROM shadow_trades "
            "WHERE resolved = 1 AND confidence IS NOT NULL AND pnl IS NOT NULL "
            "AND timestamp >= datetime('now', ?)",
            [f"-{window_days} days"],
        )
        grouped = defaultdict(lambda: ([], []))
        for r in rows:
            grouped[r["strategy"]][0].append(r["confidence"])
            grouped[r["strategy"]][1].append(r["pnl"])

        results = []
        for strat, (conf, pnl) in grouped.items():
            n = len(conf)
            if n < min_n:
                continue
            ic, p = spearmanr(conf, pnl)
            if ic != ic:  # NaN — e.g. confidence is constant within the strategy
                continue
            se = 1.0 / math.sqrt(n - 1)  # approx SE of a Spearman IC
            status = "KILL" if abs(ic) < 0.03 else ("WARN" if abs(ic) < 0.05 else "OK")
            results.append(
                {
                    "strategy": strat,
                    "n": n,
                    "ic": round(float(ic), 4),
                    "p_value": round(float(p), 4),
                    "ic_se": round(se, 4),
                    "ci95": [round(float(ic) - 1.96 * se, 4), round(float(ic) + 1.96 * se, 4)],
                    "status": status,
                }
            )

        k = len(results)
        for r in results:
            r["significant_bonferroni"] = bool(k and r["p_value"] < 0.05 / k)
        results.sort(key=lambda r: -abs(r["ic"]))
        return {
            "window_days": window_days,
            "min_n": min_n,
            "strategies_tested": k,
            "method": "Spearman(confidence, realized_pnl); |IC|<0.03 KILL, <0.05 WARN; Bonferroni across k strategies",
            "strategies": results,
        }
    except Exception as e:
        logger.exception("strategy_ic failed: {}", e)
        return {"error": str(e), "strategies": []}

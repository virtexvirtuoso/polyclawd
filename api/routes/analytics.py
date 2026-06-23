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

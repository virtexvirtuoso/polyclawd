"""
Weather Ensemble Dashboard API — accuracy + profitability metrics.
Serves data for /static/weather.html dashboard.
"""

from datetime import datetime, timezone, timedelta
from fastapi import APIRouter
from loguru import logger
import sqlite3
import os

router = APIRouter(prefix="/api/weather", tags=["weather-dashboard"])

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "storage", "shadow_trades.db")


def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@router.get("/dashboard")
async def weather_dashboard():
    """Full weather dashboard data: accuracy + profitability."""
    db = _get_db()

    # ── P&L by segment ──
    segments = db.execute("""
        SELECT
            CASE
                WHEN market_title LIKE '%between%' THEN 'bracket'
                WHEN market_title LIKE '%or higher%' OR market_title LIKE '%or below%'
                    OR market_title LIKE '%or above%' THEN 'threshold'
                ELSE 'exact'
            END as market_type,
            side,
            COUNT(*) as n,
            SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) as wins,
            ROUND(SUM(pnl), 2) as pnl,
            ROUND(AVG(pnl), 2) as avg_pnl,
            ROUND(AVG(edge_pct), 4) as avg_edge,
            ROUND(AVG(confidence), 4) as avg_conf
        FROM paper_positions
        WHERE strategy='weather' AND status IN ('won', 'lost')
        GROUP BY market_type, side
        ORDER BY pnl DESC
    """).fetchall()

    # ── P&L by city ──
    city_pnl = db.execute("""
        SELECT
            CASE
                WHEN LOWER(market_title) LIKE '%buenos aires%' THEN 'buenos aires'
                WHEN LOWER(market_title) LIKE '%new york%' THEN 'new york'
                WHEN LOWER(market_title) LIKE '%chicago%' THEN 'chicago'
                WHEN LOWER(market_title) LIKE '%miami%' THEN 'miami'
                WHEN LOWER(market_title) LIKE '%dallas%' THEN 'dallas'
                WHEN LOWER(market_title) LIKE '%atlanta%' THEN 'atlanta'
                WHEN LOWER(market_title) LIKE '%seattle%' THEN 'seattle'
                WHEN LOWER(market_title) LIKE '%toronto%' THEN 'toronto'
                WHEN LOWER(market_title) LIKE '%paris%' THEN 'paris'
                WHEN LOWER(market_title) LIKE '%london%' THEN 'london'
                WHEN LOWER(market_title) LIKE '%wellington%' THEN 'wellington'
                WHEN LOWER(market_title) LIKE '%tokyo%' THEN 'tokyo'
                WHEN LOWER(market_title) LIKE '%seoul%' THEN 'seoul'
                WHEN LOWER(market_title) LIKE '%sydney%' THEN 'sydney'
                ELSE 'other'
            END as city,
            COUNT(*) as n,
            SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) as wins,
            ROUND(SUM(pnl), 2) as pnl,
            ROUND(AVG(entry_price), 3) as avg_entry
        FROM paper_positions
        WHERE strategy='weather' AND status IN ('won', 'lost')
        GROUP BY city
        ORDER BY pnl DESC
    """).fetchall()

    # ── Recent trades ──
    recent = db.execute("""
        SELECT market_title, side, status, entry_price, pnl, edge_pct,
               confidence, opened_at, closed_at,
               CASE
                   WHEN market_title LIKE '%between%' THEN 'bracket'
                   WHEN market_title LIKE '%or higher%' OR market_title LIKE '%or below%' OR market_title LIKE '%or above%' THEN 'threshold'
                   ELSE 'exact'
               END as market_type
        FROM paper_positions
        WHERE strategy='weather' AND status IN ('won', 'lost')
        ORDER BY closed_at DESC
        LIMIT 30
    """).fetchall()

    # ── Open positions ──
    open_pos = db.execute("""
        SELECT market_title, side, entry_price, edge_pct, confidence, opened_at,
            CASE
                WHEN market_title LIKE '%between%' THEN 'bracket'
                WHEN market_title LIKE '%or higher%' OR market_title LIKE '%or below%' OR market_title LIKE '%or above%' THEN 'threshold'
                ELSE 'exact'
            END as market_type
        FROM paper_positions
        WHERE strategy='weather' AND status='open'
        ORDER BY opened_at DESC
    """).fetchall()

    # ── Overall stats ──
    totals = db.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END) as losses,
            ROUND(SUM(pnl), 2) as total_pnl,
            ROUND(AVG(pnl), 2) as avg_pnl,
            ROUND(MIN(pnl), 2) as worst_trade,
            ROUND(MAX(pnl), 2) as best_trade,
            MIN(opened_at) as first_trade,
            MAX(closed_at) as last_trade
        FROM paper_positions
        WHERE strategy='weather' AND status IN ('won', 'lost')
    """).fetchone()

    # ── Equity curve (weather only) ──
    equity = db.execute("""
        SELECT closed_at, pnl
        FROM paper_positions
        WHERE strategy='weather' AND status IN ('won', 'lost') AND closed_at IS NOT NULL
        ORDER BY closed_at
    """).fetchall()

    cumulative = []
    running = 0
    for row in equity:
        running += row["pnl"]
        cumulative.append({"date": row["closed_at"], "pnl": round(running, 2)})

    # ── Win rate by entry price bucket ──
    price_buckets = db.execute("""
        SELECT
            CASE
                WHEN entry_price < 0.15 THEN '<15c'
                WHEN entry_price < 0.30 THEN '15-30c'
                WHEN entry_price < 0.50 THEN '30-50c'
                WHEN entry_price < 0.70 THEN '50-70c'
                ELSE '>70c'
            END as bucket,
            side,
            COUNT(*) as n,
            SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) as wins,
            ROUND(SUM(pnl), 2) as pnl
        FROM paper_positions
        WHERE strategy='weather' AND status IN ('won', 'lost')
        GROUP BY bucket, side
        ORDER BY bucket
    """).fetchall()

    # ── Forecast log (if table exists) ──
    forecast_accuracy = []
    empirical_std = []
    try:
        forecast_accuracy = db.execute("""
            SELECT city, COUNT(*) as n,
                   ROUND(AVG(ABS(avg_err)), 2) as mae,
                   ROUND(AVG(avg_err), 2) as bias,
                   ROUND(AVG(avg_err * avg_err), 2) as mse
            FROM (
                SELECT city, target_date, AVG(forecast_error_f) as avg_err
                FROM forecast_log
                WHERE forecast_error_f IS NOT NULL
                GROUP BY city, target_date
            )
            GROUP BY city
            ORDER BY mae
        """).fetchall()
        
        # Empirical std per city + horizon bucket (for progress tracking)
        empirical_std = db.execute("""
            SELECT city,
                CASE
                    WHEN forecast_horizon_hours < 6 THEN '0-6h'
                    WHEN forecast_horizon_hours < 24 THEN '6-24h'
                    WHEN forecast_horizon_hours < 48 THEN '24-48h'
                    ELSE '48h+'
                END as horizon,
                COUNT(DISTINCT target_date) as n_dates,
                ROUND(AVG(forecast_error_f), 2) as mean_err_f,
                ROUND(SQRT(AVG(forecast_error_f * forecast_error_f) - AVG(forecast_error_f) * AVG(forecast_error_f)), 2) as std_err_f
            FROM forecast_log
            WHERE forecast_error_f IS NOT NULL
            GROUP BY city, horizon
            HAVING n_dates >= 1
            ORDER BY city, horizon
        """).fetchall()
    except Exception:
        pass  # Table doesn't exist yet

    db.close()

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "totals": dict(totals) if totals else {},
        "segments": [dict(r) for r in segments],
        "city_pnl": [dict(r) for r in city_pnl],
        "recent_trades": [dict(r) for r in recent],
        "open_positions": [dict(r) for r in open_pos],
        "equity_curve": cumulative,
        "price_buckets": [dict(r) for r in price_buckets],
        "forecast_accuracy": [dict(r) for r in forecast_accuracy],
        "empirical_std": [dict(r) for r in empirical_std],
    }

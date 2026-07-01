"""Category-level momentum tracking — volume/volatility trends by archetype.
Uses signal_snapshots table from shadow_trades.db.

Tracks rolling 7-day and 30-day volume + volatility trends per market
archetype/category. Computes momentum score as weighted composite of:
  - Volume change % (7d vs prior 7d) × 0.4
  - Volatility change (avg abs price change) × 0.3
  - New-market creation rate × 0.3

Used for: resource allocation (scan heating categories more frequently),
opportunity detection (rising volume = better fills + more mispricing).
"""

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# Default DB path (overridable for testing)
DB_PATH = Path(__file__).parent.parent / "storage" / "shadow_trades.db"


def get_db_path() -> str:
    """Return the configured DB path as string."""
    return str(DB_PATH)


def _query_snapshots_by_period(conn, start_date: str, end_date: str) -> list:
    """Query signal_snapshots aggregated by category for a date range.

    Returns list of dicts with category, total_volume, market_count,
    avg_price, distinct_market_ids.
    """
    rows = conn.execute("""
        SELECT
            COALESCE(NULLIF(TRIM(category), ''), 'uncategorized') AS category,
            SUM(volume) AS total_volume,
            COUNT(*) AS snapshot_count,
            COUNT(DISTINCT market_id) AS market_count,
            AVG(ABS(price - 0.5)) AS avg_abs_deviation
        FROM signal_snapshots
        WHERE snapshot_date >= ? AND snapshot_date < ?
        GROUP BY category
        ORDER BY total_volume DESC
    """, (start_date, end_date)).fetchall()
    return [dict(r) for r in rows]


def _query_new_markets(conn, start_date: str, end_date: str, previous_start: str) -> dict:
    """Count market_ids appearing in [start_date, end_date) that were
    NOT present in [previous_start, start_date). Returns {category: count}."""
    rows = conn.execute("""
        SELECT
            COALESCE(NULLIF(TRIM(category), ''), 'uncategorized') AS category,
            COUNT(DISTINCT s.market_id) AS new_markets
        FROM signal_snapshots s
        WHERE s.snapshot_date >= ? AND s.snapshot_date < ?
          AND s.market_id NOT IN (
              SELECT DISTINCT market_id
              FROM signal_snapshots
              WHERE snapshot_date >= ? AND snapshot_date < ?
                AND market_id IS NOT NULL
          )
        GROUP BY category
    """, (start_date, end_date, previous_start, start_date)).fetchall()
    return {r["category"]: r["new_markets"] for r in rows}


def compute_category_momentum(conn=None) -> list:
    """Compute momentum scores per market category/archetype.

    Returns list of dicts sorted by momentum_score descending:
      {
        "category": str,
        "volume_7d": float,
        "volume_prior_7d": float,
        "volume_change_pct": float,
        "volatility_7d": float,
        "new_markets_7d": int,
        "momentum_score": float
      }

    Args:
        conn: Optional sqlite3.Connection. If None, opens default DB.
    """
    own_conn = False
    if conn is None:
        conn = sqlite3.connect(get_db_path())
        conn.row_factory = sqlite3.Row
        own_conn = True

    try:
        # Determine date boundaries
        date_rows = conn.execute("""
            SELECT MIN(snapshot_date) AS min_d, MAX(snapshot_date) AS max_d
            FROM signal_snapshots
        """).fetchone()
        if not date_rows or not date_rows["min_d"]:
            return []

        max_date = datetime.strptime(date_rows["max_d"], "%Y-%m-%d")
        min_date = datetime.strptime(date_rows["min_d"], "%Y-%m-%d")

        # Compute date windows
        # Today is one day after max_date so that data from max_date is included
        today = (max_date + timedelta(days=1)).strftime("%Y-%m-%d")
        seven_days_ago = (max_date - timedelta(days=7)).strftime("%Y-%m-%d")
        fourteen_days_ago = (max_date - timedelta(days=14)).strftime("%Y-%m-%d")
        thirty_days_ago = (max_date - timedelta(days=30)).strftime("%Y-%m-%d")

        # Also need 30d window for secondary metric
        sixty_days_ago = (max_date - timedelta(days=60)).strftime("%Y-%m-%d")

        # Clamp to available data range
        min_date_str = date_rows["min_d"]
        if seven_days_ago < min_date_str:
            seven_days_ago = min_date_str
        if fourteen_days_ago < min_date_str:
            fourteen_days_ago = min_date_str
        if thirty_days_ago < min_date_str:
            thirty_days_ago = min_date_str
        if sixty_days_ago < min_date_str:
            sixty_days_ago = min_date_str

        # Get all categories with their 7d data
        current_7d = _query_snapshots_by_period(conn, seven_days_ago, today)
        prior_7d = _query_snapshots_by_period(conn, fourteen_days_ago, seven_days_ago)

        # 30d data for additional context
        current_30d = _query_snapshots_by_period(conn, thirty_days_ago, today)
        prior_30d = _query_snapshots_by_period(conn, sixty_days_ago, thirty_days_ago) if thirty_days_ago > min_date_str else []

        # New markets in 7d period (vs prior 7d)
        new_markets_7d = _query_new_markets(conn, seven_days_ago, today, fourteen_days_ago)

        # Build lookup dicts
        cur_7d_map = {r["category"]: r for r in current_7d}
        pri_7d_map = {r["category"]: r for r in prior_7d}
        cur_30d_map = {r["category"]: r for r in current_30d}

        # Gather all unique categories
        all_categories = set()
        for r in current_7d:
            all_categories.add(r["category"])
        for r in prior_7d:
            all_categories.add(r["category"])
        for r in current_30d:
            all_categories.add(r["category"])

        results = []
        for cat in sorted(all_categories):
            c7 = cur_7d_map.get(cat, {})
            p7 = pri_7d_map.get(cat, {})
            c30 = cur_30d_map.get(cat, {})

            vol_7d = c7.get("total_volume", 0) or 0
            vol_prior_7d = p7.get("total_volume", 0) or 0

            # Volume change % (handle zero prior volume)
            if vol_prior_7d > 0:
                vol_change_pct = ((vol_7d - vol_prior_7d) / vol_prior_7d) * 100
            elif vol_7d > 0:
                vol_change_pct = 100.0  # New category emerging
            else:
                vol_change_pct = 0.0

            # Volatility: avg absolute deviation from 0.5 (price dispersion)
            # Higher = more volatile category
            vol_7d_count = c7.get("snapshot_count", 0) or 0
            pri_7d_count = p7.get("snapshot_count", 0) or 0

            # Use avg abs deviation as volatility proxy
            vol_7d_dev = c7.get("avg_abs_deviation", 0) or 0
            pri_7d_dev = p7.get("avg_abs_deviation", 0) or 0

            # Volatility change
            if pri_7d_dev > 0:
                vol_change = ((vol_7d_dev - pri_7d_dev) / pri_7d_dev) * 100
            else:
                vol_change = 0.0

            # New markets in period
            new_ct = new_markets_7d.get(cat, 0)

            # New-market rate: normalize as % of current 7d market count
            cur_market_count = c7.get("market_count", 0) or 1
            new_market_rate = (new_ct / cur_market_count) * 100 if cur_market_count > 0 else 0

            # Normalize components to 0-100 range for compositing
            def _norm_pct(val: float, cap: float = 200.0) -> float:
                """Clamp percentage to [0, 100]."""
                return max(0, min(100, (val + cap) / (2 * cap) * 100))

            # For volume change: positive = growing (good), negative = shrinking
            vol_score = _norm_pct(vol_change_pct, cap=200.0)

            # For volatility change: moderate increase = opportunity
            # but extreme could be noise. Cap at reasonable range.
            vol_scaled = max(-100, min(100, vol_change))
            vol_score_comp = _norm_pct(vol_scaled, cap=100.0)

            # New market rate: higher = more activity
            new_score = min(100, new_market_rate * 5)

            # Composite momentum score
            momentum_score = (
                vol_score * 0.4 +
                vol_score_comp * 0.3 +
                new_score * 0.3
            )

            results.append({
                "category": cat,
                "volume_7d": round(vol_7d, 2),
                "volume_prior_7d": round(vol_prior_7d, 2),
                "volume_change_pct": round(vol_change_pct, 2),
                "volatility_7d": round(vol_7d_dev, 4),
                "new_markets_7d": new_ct,
                "snapshot_count_7d": vol_7d_count,
                "market_count_7d": cur_market_count,
                "momentum_score": round(momentum_score, 2),
                "volume_30d": round(c30.get("total_volume", 0) or 0, 2),
                "last_snapshot_date": today,
            })

        results.sort(key=lambda x: x["momentum_score"], reverse=True)
        return results

    except Exception as e:
        logger.exception(f"Category momentum computation failed: {e}")
        return []
    finally:
        if own_conn:
            conn.close()


def get_momentum_leaderboard(top_n: int = 10) -> dict:
    """Get top-N categories by momentum score with summary metadata.

    Suitable for API consumption.
    """
    results = compute_category_momentum()
    top = results[:top_n]
    bottom = results[-3:] if len(results) > 3 else []

    return {
        "categories": top,
        "total_categories": len(results),
        "top_n": top_n,
        "heating": [r["category"] for r in top if r["momentum_score"] > 60],
        "cooling": [r["category"] for r in bottom if r["momentum_score"] < 40],
        "generated_at": datetime.now().isoformat(),
        "note": "Momentum score = vol_change*0.4 + volatility_change*0.3 + new_market_rate*0.3. Score >60 = heating, <40 = cooling."
    }


if __name__ == "__main__":
    import json
    results = compute_category_momentum()
    print(f"Categories tracked: {len(results)}")
    for r in results[:10]:
        print(f"  {r['category']:20s} score={r['momentum_score']:6.2f}  "
              f"vol_7d={r['volume_7d']:>10,.0f}  "
              f"chg={r['volume_change_pct']:>+7.1f}%  "
              f"new={r['new_markets_7d']}")
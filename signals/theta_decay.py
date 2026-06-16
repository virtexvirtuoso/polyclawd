"""Empirical theta decay curves — how pricing changes as resolution approaches.
Measures where price discovery happens in market lifetime.

Builds per-archetype decay curves from resolved market data:
  - Normalizes time axis to [0, 1] (0 = entry, 1 = resolution)
  - Computes avg absolute price change per decile bucket
  - Computes cumulative price discovery % (what fraction of total price
    movement occurs by each decile)
  - Key output: "last 10% discovery share" — what % of movement happens
    in the final 10% of market lifetime

Stores results in storage/theta_decay.json for API consumption.
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

STORAGE_DIR = Path(__file__).parent.parent / "storage"
THETA_DECAY_FILE = STORAGE_DIR / "theta_decay.json"
DB_PATH = Path(__file__).parent.parent / "storage" / "shadow_trades.db"

# Minimum samples for a meaningful curve
MIN_SAMPLES = 10


def get_db_path() -> str:
    return str(DB_PATH)


def _parse_as_utc(dt_str: str) -> datetime | None:
    """Parse a date/time string and return a timezone-aware UTC datetime."""
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        pass
    try:
        return datetime.strptime(dt_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _resolve_price_history(market_id: str, entry_date: str, resolved_date: str,
                           conn: sqlite3.Connection) -> list:
    """Build a price history sequence for a market from signal_snapshots.

    Returns sorted list of (normalized_time, price) tuples.
    entry_date and resolved_date are ISO-format date/time strings.
    If signal_snapshots has insufficient points, returns basic 2-point path.
    """
    entry_dt = _parse_as_utc(entry_date)
    resolved_dt = _parse_as_utc(resolved_date)
    if entry_dt is None or resolved_dt is None:
        return []

    total_seconds = (resolved_dt - entry_dt).total_seconds()
    if total_seconds <= 0:
        return []  # Invalid time range

    # Query signal_snapshots for intermediate price points
    entry_str = entry_dt.strftime("%Y-%m-%d")
    resolved_str = resolved_dt.strftime("%Y-%m-%d")

    rows = conn.execute("""
        SELECT snapshot_date, snapshot_time, price
        FROM signal_snapshots
        WHERE market_id = ?
          AND snapshot_date >= ?
          AND snapshot_date <= ?
        ORDER BY snapshot_date ASC, snapshot_time ASC
    """, (market_id, entry_str, resolved_str)).fetchall()

    if not rows:
        return []

    # Build (normalized_time, price) pairs
    path = []
    for row in rows:
        row_dt = _parse_as_utc(row["snapshot_time"])
        if row_dt is None:
            row_dt = _parse_as_utc(row["snapshot_date"])
            if row_dt is None:
                continue

        elapsed = (row_dt - entry_dt).total_seconds()
        if elapsed < 0:
            continue
        norm_t = elapsed / total_seconds
        if norm_t > 1.0:
            norm_t = 1.0

        price = row["price"]
        if price is None:
            continue
        path.append((norm_t, price))

    if not path:
        return []

    # Deduplicate: keep only the last price at each normalized time
    path.sort(key=lambda x: x[0])
    deduped = []
    for t, p in path:
        if deduped and abs(deduped[-1][0] - t) < 0.001:
            deduped[-1] = (t, p)  # Update price for same time bin
        else:
            deduped.append((t, p))

    return deduped


def build_decay_curve(archetype: str, n_buckets: int = 10, conn=None) -> dict:
    """Build theta decay curve for a single archetype.

    Args:
        archetype: Market archetype string (e.g. 'price_above', 'sports_winner')
        n_buckets: Number of time buckets (default 10 = deciles)
        conn: Optional sqlite3 connection

    Returns:
        dict with archetype, n_markets, n_resolved, buckets[], last_pctile_discovery
    """
    own_conn = False
    if conn is None:
        conn = sqlite3.connect(get_db_path())
        conn.row_factory = sqlite3.Row
        own_conn = True

    try:
        # Get resolved trades for this archetype
        rows = conn.execute("""
            SELECT market_id, market, snapshot_date, entry_price, exit_price,
                   resolved_at, outcome, side, resolved
            FROM shadow_trades
            WHERE resolved = 1
              AND archetype = ?
              AND entry_price IS NOT NULL
              AND exit_price IS NOT NULL
              AND snapshot_date IS NOT NULL
              AND resolved_at IS NOT NULL
            ORDER BY snapshot_date
        """, (archetype,)).fetchall()

        if len(rows) < MIN_SAMPLES:
            return {
                "archetype": archetype,
                "n_markets": len(rows),
                "n_resolved": len(rows),
                "status": "low_confidence",
                "message": f"Need {MIN_SAMPLES}+ resolved markets for valid curve (have {len(rows)})",
                "buckets": [],
                "last_pctile_discovery": {},
            }

        # Initialize bucket accumulators
        # bucket 0 = [0, 0.1), bucket 1 = [0.1, 0.2), ..., bucket 9 = [0.9, 1.0]
        buckets_abs_change = [[] for _ in range(n_buckets)]
        bucket_labels = [round((i + 1) / n_buckets, 2) for i in range(n_buckets)]

        for row in rows:
            price_history = _resolve_price_history(
                row["market_id"],
                row["snapshot_date"],
                row["resolved_at"],
                conn,
            )

            if not price_history or len(price_history) < 2:
                # Fallback: use just entry and exit prices -> linear interpolation
                norm_buckets = []
                for i in range(n_buckets):
                    t = (i + 1) / n_buckets
                    entry_p = row["entry_price"]
                    exit_p = row["exit_price"]
                    interp_price = entry_p + (exit_p - entry_p) * t
                    abs_change = abs(interp_price - entry_p)
                    norm_buckets.append((t, abs_change))
            else:
                # Map price history into buckets
                entry_price = price_history[0][1]
                norm_buckets = []
                for i in range(n_buckets):
                    bucket_upper = (i + 1) / n_buckets

                    # Find the last price point at or before bucket_upper
                    last_price = entry_price
                    for t, p in price_history:
                        if t <= bucket_upper:
                            last_price = p
                        else:
                            break

                    abs_change = abs(last_price - entry_price)
                    norm_buckets.append((bucket_upper, abs_change))

            for i, (t, abs_change) in enumerate(norm_buckets):
                if i < n_buckets:
                    buckets_abs_change[i].append(abs_change)

        # Compute per-bucket statistics
        bucket_avg_changes = []

        for i in range(n_buckets):
            changes = buckets_abs_change[i]
            avg_change = sum(changes) / len(changes) if changes else 0.0
            bucket_avg_changes.append(avg_change)

        total_avg_change = sum(bucket_avg_changes)

        # Cumulative discovery % with monotonicity enforcement
        max_seen = 0.0
        cumulative_pcts = []
        for i, change in enumerate(bucket_avg_changes):
            running_total = sum(bucket_avg_changes[:i + 1])
            pct = (running_total / total_avg_change) * 100 if total_avg_change > 0 else 0.0
            max_seen = max(max_seen, pct)
            cumulative_pcts.append(round(max_seen, 2))

        bucket_results = []
        for i in range(n_buckets):
            bucket_results.append({
                "bucket": bucket_labels[i],
                "bucket_range": f"{i / n_buckets:.1f}-{bucket_labels[i]:.1f}",
                "avg_abs_price_change": round(bucket_avg_changes[i], 6),
                "cumulative_discovery_pct": cumulative_pcts[i],
                "n_samples": len(buckets_abs_change[i]),
            })

        # Last decile discovery share
        if total_avg_change > 0:
            cum_at_90pct = cumulative_pcts[-2] if len(cumulative_pcts) >= 2 else 0
            discovery_in_last_10pct = cumulative_pcts[-1] - cum_at_90pct
        else:
            discovery_in_last_10pct = 0

        return {
            "archetype": archetype,
            "n_markets": len(rows),
            "n_resolved": len([r for r in rows if r["resolved"] == 1]),
            "status": "valid",
            "buckets": bucket_results,
            "last_pctile_discovery": {
                "pctile": round(1.0 - 1.0 / n_buckets, 1),
                "discovery_share_pct": round(discovery_in_last_10pct, 2),
                "n_samples_last_bucket": len(buckets_abs_change[-1]),
            },
        }

    except Exception as e:
        logger.exception(f"Decay curve build failed for {archetype}: {e}")
        return {
            "archetype": archetype,
            "status": "error",
            "error": str(e),
        }
    finally:
        if own_conn:
            conn.close()


def get_all_decay_curves() -> dict:
    """Build decay curves for all archetypes with sufficient resolved markets.

    Returns dict keyed by archetype with full curve data.
    """
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row

    try:
        archetypes = conn.execute("""
            SELECT archetype, COUNT(*) as cnt
            FROM shadow_trades
            WHERE resolved = 1
              AND archetype IS NOT NULL
              AND archetype != ''
            GROUP BY archetype
            ORDER BY cnt DESC
        """).fetchall()

        results = {}
        for r in archetypes:
            arch = r["archetype"]
            curve = build_decay_curve(arch, conn=conn)
            results[arch] = curve

        return results

    except Exception as e:
        logger.exception(f"All decay curves failed: {e}")
        return {"error": str(e)}
    finally:
        conn.close()


def save_decay_curves(curves: dict = None) -> dict:
    """Save theta decay curves to JSON for API consumption."""
    if curves is None:
        curves = get_all_decay_curves()

    STORAGE_DIR.mkdir(parents=True, exist_ok=True)

    output = {
        "curves": curves,
        "total_archetypes": len(curves),
        "generated_at": datetime.now().isoformat(),
        "min_samples": MIN_SAMPLES,
    }

    with open(THETA_DECAY_FILE, "w") as f:
        json.dump(output, f, indent=2)

    return output


def load_decay_curves() -> dict:
    """Load theta decay curves from JSON."""
    if THETA_DECAY_FILE.exists():
        try:
            with open(THETA_DECAY_FILE) as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load decay curves: {e}")
    return {"curves": {}, "total_archetypes": 0}


def find_largest_last_decile_discovery(curves: dict, top_n: int = 5) -> list:
    """Find archetypes with the largest % of discovery in final decile.

    High values = markets that resolve suddenly (information events).
    Low values = markets that decay gradually (time-based, slow revelation).
    """
    discoveries = []
    for arch, curve in curves.items():
        if isinstance(curve, dict) and curve.get("status") == "valid":
            last = curve.get("last_pctile_discovery", {})
            discoveries.append({
                "archetype": arch,
                "last_decile_discovery_pct": last.get("discovery_share_pct", 0),
                "n_markets": curve.get("n_markets", 0),
            })

    discoveries.sort(key=lambda x: x["last_decile_discovery_pct"], reverse=True)
    return discoveries[:top_n]


if __name__ == "__main__":
    import json
    curves = get_all_decay_curves()
    print(f"Archetypes with decay curves: {len(curves)}")

    for arch, curve in sorted(curves.items()):
        status = curve.get("status", "unknown") if isinstance(curve, dict) else "unknown"
        n = curve.get("n_markets", 0) if isinstance(curve, dict) else 0
        if status == "valid":
            last = curve.get("last_pctile_discovery", {})
            print(f"  {arch:25s} n={n:3d}  last_10pct={last.get('discovery_share_pct', 0):5.1f}%")
        else:
            print(f"  {arch:25s} n={n:3d}  [{status}]")

    save_decay_curves(curves)
    print(f"\nSaved to {THETA_DECAY_FILE}")
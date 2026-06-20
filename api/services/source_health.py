"""
Source Health Registry — Track per-source API health metrics.

Stores last_success, last_error, consecutive_failures, avg_latency in SQLite.
"""

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent.parent
DB_PATH = BASE_DIR / "storage" / "shadow_trades.db"

TRACKED_SOURCES = [
    # Platform / prediction-market sources
    "polymarket_gamma",
    "polymarket_clob",
    "kalshi",
    "manifold",
    "action_network",
    "vegas",
    "espn",
    # Weather forecast APIs (instrumented in signals/weather_ensemble.py
    # via _record_weather_fetch). Gate 4 in services/health_gates.py reads
    # these names — keep WEATHER_FORECAST_SOURCES there in sync.
    "open_meteo",
    "pirate_weather",
    "tomorrow_io",
    "weatherapi",
    "weather_com",
    "visual_crossing",
    "nws",
    # Weather actuals (resolution source — instrumented but excluded from
    # Gate 4 since it doesn't affect forward forecasting).
    "twc_actuals",
]


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _init_table(conn)
    return conn


def _init_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_health (
            source TEXT PRIMARY KEY,
            last_success TEXT,
            last_error TEXT,
            last_error_msg TEXT,
            consecutive_failures INTEGER DEFAULT 0,
            total_successes INTEGER DEFAULT 0,
            total_failures INTEGER DEFAULT 0,
            avg_latency_ms REAL DEFAULT 0,
            last_latency_ms REAL DEFAULT 0,
            circuit_open_until TEXT,
            last_touched TEXT
        )
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(source_health)").fetchall()}
    if "last_touched" not in cols:
        conn.execute("ALTER TABLE source_health ADD COLUMN last_touched TEXT")
        conn.execute(
            "UPDATE source_health SET last_touched = last_success WHERE last_touched IS NULL AND last_success IS NOT NULL"
        )
    if "circuit_backoff_s" not in cols:
        conn.execute("ALTER TABLE source_health ADD COLUMN circuit_backoff_s INTEGER DEFAULT 0")
    conn.commit()


def record_success(source: str, latency_ms: float):
    """Record a successful fetch for a source."""
    logger.debug("source_health: %s SUCCESS latency=%.0fms", source, latency_ms)
    try:
        conn = _get_db()
    except Exception as e:
        logger.debug("source_health: %s record_success skipped (db busy): %s", source, e)
        return
    now = datetime.now(timezone.utc).isoformat()

    row = conn.execute("SELECT * FROM source_health WHERE source=?", (source,)).fetchone()
    if row:
        total = row["total_successes"] + 1
        # Exponential moving average for latency
        old_avg = row["avg_latency_ms"] or latency_ms
        new_avg = old_avg * 0.8 + latency_ms * 0.2
        conn.execute(
            """
            UPDATE source_health SET
                last_success=?, last_touched=?, consecutive_failures=0,
                total_successes=?, avg_latency_ms=?, last_latency_ms=?,
                circuit_open_until=NULL, circuit_backoff_s=0
            WHERE source=?
        """,
            (now, now, total, round(new_avg, 1), round(latency_ms, 1), source),
        )
    else:
        conn.execute(
            """
            INSERT INTO source_health (source, last_success, last_touched, consecutive_failures, total_successes, total_failures, avg_latency_ms, last_latency_ms)
            VALUES (?, ?, ?, 0, 1, 0, ?, ?)
        """,
            (source, now, now, round(latency_ms, 1), round(latency_ms, 1)),
        )

    conn.commit()
    conn.close()


def touch_source(source: str):
    """Heartbeat: update last_touched only — does NOT touch last_success or counters.
    Use from schedulers/watchdogs that want to mark a source as actively monitored
    (e.g. to keep dashboard freshness signals green) without claiming a real fetch.
    Consumers checking 'did we really fetch?' should read last_success; consumers
    checking 'is this source under active surveillance?' should read last_touched."""
    try:
        conn = _get_db()
    except Exception as e:
        logger.debug("source_health: %s touch_source skipped (db busy): %s", source, e)
        return
    now = datetime.now(timezone.utc).isoformat()
    row = conn.execute("SELECT 1 FROM source_health WHERE source=?", (source,)).fetchone()
    if row:
        conn.execute("UPDATE source_health SET last_touched=? WHERE source=?", (now, source))
    else:
        conn.execute(
            "INSERT INTO source_health (source, last_touched, consecutive_failures, total_successes, total_failures, avg_latency_ms, last_latency_ms) VALUES (?, ?, 0, 0, 0, 0, 0)",
            (source, now),
        )
    conn.commit()
    conn.close()
    logger.debug("source_health: %s TOUCHED at %s", source, now)


def record_failure(source: str, error_msg: str):
    """Record a failed fetch for a source."""
    logger.debug("source_health: %s FAILURE error=%s", source, error_msg[:100])
    try:
        conn = _get_db()
    except Exception as e:
        logger.debug("source_health: %s record_failure skipped (db busy): %s", source, e)
        return
    now = datetime.now(timezone.utc).isoformat()

    row = conn.execute("SELECT * FROM source_health WHERE source=?", (source,)).fetchone()
    if row:
        consec = row["consecutive_failures"] + 1
        total_fail = row["total_failures"] + 1
        conn.execute(
            """
            UPDATE source_health SET
                last_error=?, last_error_msg=?,
                consecutive_failures=?, total_failures=?
            WHERE source=?
        """,
            (now, error_msg[:500], consec, total_fail, source),
        )
    else:
        conn.execute(
            """
            INSERT INTO source_health (source, last_error, last_error_msg, consecutive_failures, total_successes, total_failures)
            VALUES (?, ?, ?, 1, 0, 1)
        """,
            (source, now, error_msg[:500]),
        )

    conn.commit()
    conn.close()


def set_circuit_open(source: str, until_iso: str):
    """Mark circuit breaker as open until a given time."""
    logger.warning("source_health: %s CIRCUIT OPEN until %s", source, until_iso)
    conn = _get_db()
    conn.execute("UPDATE source_health SET circuit_open_until=? WHERE source=?", (until_iso, source))
    conn.commit()
    conn.close()


def trip_circuit(source: str, initial_backoff_s: int = 600, max_backoff_s: int = 3600):
    """Trip the circuit breaker with exponential backoff (DB-persisted).

    On first trip: uses initial_backoff_s. On subsequent trips (before a
    success resets it): doubles the previous backoff up to max_backoff_s.
    Replaces per-process _X_blocked globals — state survives restarts and
    is shared across workers.
    """
    try:
        conn = _get_db()
    except Exception as e:
        logger.debug("source_health: %s trip_circuit skipped (db busy): %s", source, e)
        return
    now = datetime.now(timezone.utc)
    row = conn.execute("SELECT circuit_backoff_s FROM source_health WHERE source=?",
                       (source,)).fetchone()
    if row:
        prev = row["circuit_backoff_s"] or 0
        backoff = min(max(prev * 2, initial_backoff_s), max_backoff_s)
    else:
        backoff = initial_backoff_s
    from datetime import timedelta
    until = now + timedelta(seconds=backoff)
    until_iso = until.isoformat()
    conn.execute(
        "UPDATE source_health SET circuit_open_until=?, circuit_backoff_s=? WHERE source=?",
        (until_iso, backoff, source))
    if conn.total_changes == 0:
        conn.execute(
            "INSERT INTO source_health (source, circuit_open_until, circuit_backoff_s,"
            " consecutive_failures, total_successes, total_failures) VALUES (?,?,?,0,0,0)",
            (source, until_iso, backoff))
    conn.commit()
    conn.close()
    logger.warning("source_health: %s CIRCUIT TRIPPED backoff=%ds until %s",
                   source, backoff, until_iso)


def reset_circuit(source: str):
    """Clear circuit breaker and reset backoff to 0 (call on success)."""
    try:
        conn = _get_db()
    except Exception:
        return
    conn.execute(
        "UPDATE source_health SET circuit_open_until=NULL, circuit_backoff_s=0 WHERE source=?",
        (source,))
    conn.commit()
    conn.close()


def is_circuit_open(source: str) -> bool:
    """Check if circuit breaker is currently open for a source."""
    try:
        conn = _get_db()
    except Exception:
        return False
    row = conn.execute("SELECT circuit_open_until FROM source_health WHERE source=?", (source,)).fetchone()
    conn.close()

    if not row or not row["circuit_open_until"]:
        return False

    try:
        until = datetime.fromisoformat(row["circuit_open_until"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) < until:
            logger.debug("source_health: %s circuit still open until %s", source, row["circuit_open_until"])
            return True
        return False
    except Exception:
        return False


def get_source_health(source: str) -> Optional[Dict]:
    """Get health metrics for a single source."""
    conn = _get_db()
    row = conn.execute("SELECT * FROM source_health WHERE source=?", (source,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_source_health() -> List[Dict]:
    """Get health metrics for all tracked sources."""
    conn = _get_db()
    _init_table(conn)
    rows = conn.execute("SELECT * FROM source_health ORDER BY source").fetchall()
    conn.close()

    result = {r["source"]: dict(r) for r in rows}

    # Include all tracked sources even if no data yet
    all_sources = []
    for src in TRACKED_SOURCES:
        if src in result:
            entry = result[src]
            # Add computed fields
            entry["status"] = _compute_status(entry)
            all_sources.append(entry)
        else:
            all_sources.append(
                {
                    "source": src,
                    "status": "unknown",
                    "last_success": None,
                    "last_error": None,
                    "consecutive_failures": 0,
                    "total_successes": 0,
                    "total_failures": 0,
                    "avg_latency_ms": 0,
                }
            )

    return all_sources


def get_last_success_timestamp(source: str) -> Optional[float]:
    """Get Unix timestamp of last successful fetch OR scheduler heartbeat.
    Returns MAX(last_success, last_touched) so sources without resilient_fetch
    instrumentation (polymarket_gamma, polymarket_clob, kalshi — fetched via
    raw urllib in signals/mispriced_category_signal.py) can be kept fresh via
    scheduler-side `touch_source()` heartbeats without bypassing the gate.

    Tradeoff: if a fetcher silently fails while the scheduler keeps heartbeating,
    the gate stays green. Real fix is to wrap those fetchers in resilient_call
    so they update last_success themselves; this is the bridge until that lands.
    """
    conn = _get_db()
    row = conn.execute("SELECT last_success, last_touched FROM source_health WHERE source=?", (source,)).fetchone()
    conn.close()

    if not row:
        return None

    candidates = []
    for col in ("last_success", "last_touched"):
        ts_str = row[col]
        if not ts_str:
            continue
        try:
            candidates.append(datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp())
        except Exception:
            pass
    return max(candidates) if candidates else None


def get_last_touched_timestamp(source: str) -> Optional[float]:
    """Get Unix timestamp of last activity (real fetch OR scheduler heartbeat).
    Use for soft freshness signals (e.g. dashboard 'data age' tags) where a
    scheduler-monitored source counts as fresh even without a recent fetch."""
    conn = _get_db()
    row = conn.execute("SELECT last_touched, last_success FROM source_health WHERE source=?", (source,)).fetchone()
    conn.close()

    if not row:
        return None
    # Prefer last_touched; fall back to last_success for rows pre-fix#2
    ts_str = row["last_touched"] or row["last_success"]
    if not ts_str:
        return None

    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return None


def _compute_status(entry: Dict) -> str:
    """Compute human-readable status from health metrics."""
    if entry.get("circuit_open_until"):
        try:
            until = datetime.fromisoformat(entry["circuit_open_until"].replace("Z", "+00:00"))
            if datetime.now(timezone.utc) < until:
                return "circuit_open"
        except Exception:
            pass

    consec = entry.get("consecutive_failures", 0)
    if consec >= 5:
        return "degraded"
    if consec >= 2:
        return "warning"
    if entry.get("last_success"):
        return "healthy"
    return "unknown"

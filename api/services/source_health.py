"""
Source Health Registry — Track per-source API health metrics.

Stores last_success, last_error, consecutive_failures, avg_latency in SQLite.
"""

import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent.parent
DB_PATH = BASE_DIR / "storage" / "shadow_trades.db"

TRACKED_SOURCES = [
    "polymarket_gamma",
    "polymarket_clob",
    "kalshi",
    "manifold",
    "action_network",
    "vegas",
    "espn",
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
            circuit_open_until TEXT
        )
    """)
    conn.commit()


def record_success(source: str, latency_ms: float):
    """Record a successful fetch for a source."""
    logger.debug("source_health: %s SUCCESS latency=%.0fms", source, latency_ms)
    conn = _get_db()
    now = datetime.now(timezone.utc).isoformat()
    
    row = conn.execute("SELECT * FROM source_health WHERE source=?", (source,)).fetchone()
    if row:
        total = row["total_successes"] + 1
        # Exponential moving average for latency
        old_avg = row["avg_latency_ms"] or latency_ms
        new_avg = old_avg * 0.8 + latency_ms * 0.2
        conn.execute("""
            UPDATE source_health SET
                last_success=?, consecutive_failures=0,
                total_successes=?, avg_latency_ms=?, last_latency_ms=?,
                circuit_open_until=NULL
            WHERE source=?
        """, (now, total, round(new_avg, 1), round(latency_ms, 1), source))
    else:
        conn.execute("""
            INSERT INTO source_health (source, last_success, consecutive_failures, total_successes, total_failures, avg_latency_ms, last_latency_ms)
            VALUES (?, ?, 0, 1, 0, ?, ?)
        """, (source, now, round(latency_ms, 1), round(latency_ms, 1)))
    
    conn.commit()
    conn.close()


def record_failure(source: str, error_msg: str):
    """Record a failed fetch for a source."""
    logger.debug("source_health: %s FAILURE error=%s", source, error_msg[:100])
    conn = _get_db()
    now = datetime.now(timezone.utc).isoformat()
    
    row = conn.execute("SELECT * FROM source_health WHERE source=?", (source,)).fetchone()
    if row:
        consec = row["consecutive_failures"] + 1
        total_fail = row["total_failures"] + 1
        conn.execute("""
            UPDATE source_health SET
                last_error=?, last_error_msg=?,
                consecutive_failures=?, total_failures=?
            WHERE source=?
        """, (now, error_msg[:500], consec, total_fail, source))
    else:
        conn.execute("""
            INSERT INTO source_health (source, last_error, last_error_msg, consecutive_failures, total_successes, total_failures)
            VALUES (?, ?, ?, 1, 0, 1)
        """, (source, now, error_msg[:500]))
    
    conn.commit()
    conn.close()


def set_circuit_open(source: str, until_iso: str):
    """Mark circuit breaker as open until a given time."""
    logger.warning("source_health: %s CIRCUIT OPEN until %s", source, until_iso)
    conn = _get_db()
    conn.execute("UPDATE source_health SET circuit_open_until=? WHERE source=?", (until_iso, source))
    conn.commit()
    conn.close()


def is_circuit_open(source: str) -> bool:
    """Check if circuit breaker is currently open for a source."""
    conn = _get_db()
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
            all_sources.append({
                "source": src,
                "status": "unknown",
                "last_success": None,
                "last_error": None,
                "consecutive_failures": 0,
                "total_successes": 0,
                "total_failures": 0,
                "avg_latency_ms": 0,
            })
    
    return all_sources


def get_last_success_timestamp(source: str) -> Optional[float]:
    """Get Unix timestamp of last successful fetch for staleness checks."""
    conn = _get_db()
    row = conn.execute("SELECT last_success FROM source_health WHERE source=?", (source,)).fetchone()
    conn.close()
    
    if not row or not row["last_success"]:
        return None
    
    try:
        dt = datetime.fromisoformat(row["last_success"].replace("Z", "+00:00"))
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

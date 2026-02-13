"""IC (Information Coefficient) Tracker — measures signal prediction quality via Spearman rank correlation.

Records signal predictions at generation time, resolves them against outcomes,
and computes per-source IC to identify which signal sources carry real alpha.

IC thresholds:
  - KILL < 0.03 (source adds noise, not signal)
  - WARN < 0.05 (marginal, needs more data)
  - OK >= 0.05 (contributing alpha)
"""

import sqlite3
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "storage" / "shadow_trades.db"

IC_KILL = 0.03
IC_WARN = 0.05


def _get_conn(db_path: str = None) -> sqlite3.Connection:
    """Get SQLite connection with WAL mode and busy timeout."""
    conn = sqlite3.connect(db_path or str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_ic_tables(db_path: str = None):
    """Create IC tracking tables if not exists."""
    conn = _get_conn(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            source TEXT NOT NULL,
            market_id TEXT,
            market_title TEXT,
            side TEXT,
            confidence REAL NOT NULL,
            price_at_signal REAL,
            resolved INTEGER DEFAULT 0,
            outcome REAL,
            resolved_at REAL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sp_source_ts
        ON signal_predictions(source, timestamp)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sp_unresolved
        ON signal_predictions(resolved, market_id)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ic_measurements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            source TEXT NOT NULL,
            ic_value REAL NOT NULL,
            sample_size INTEGER NOT NULL,
            window_days INTEGER NOT NULL,
            status TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_ic_source_ts
        ON ic_measurements(source, timestamp)
    """)
    conn.commit()
    conn.close()


def record_signal_prediction(signal: dict, db_path: str = None):
    """Store a signal prediction for later IC calculation.

    Args:
        signal: Dict with keys: source, market_id, market, side, confidence, price
    """
    init_ic_tables(db_path)
    conn = _get_conn(db_path)
    conn.execute("""
        INSERT INTO signal_predictions
        (timestamp, source, market_id, market_title, side, confidence, price_at_signal)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        time.time(),
        signal.get("source", "unknown"),
        signal.get("market_id", ""),
        (signal.get("market", "") or "")[:200],
        signal.get("side", ""),
        signal.get("confidence", 0),
        signal.get("price", 0.5),
    ))
    conn.commit()
    conn.close()


def resolve_prediction(market_id: str, outcome: float, db_path: str = None) -> int:
    """Resolve all unresolved predictions for a market.

    Args:
        market_id: The market identifier
        outcome: 1.0 for YES win, 0.0 for NO win, 0.5 for push/void

    Returns:
        Number of predictions resolved
    """
    init_ic_tables(db_path)
    conn = _get_conn(db_path)
    cursor = conn.execute("""
        UPDATE signal_predictions
        SET resolved = 1, outcome = ?, resolved_at = ?
        WHERE market_id = ? AND resolved = 0
    """, (outcome, time.time(), market_id))
    count = cursor.rowcount
    conn.commit()
    conn.close()
    return count


def resolve_from_shadow_trades(db_path: str = None) -> dict:
    """Auto-resolve predictions using shadow_trades outcomes.

    Matches unresolved predictions against resolved shadow trades by market_id.
    """
    init_ic_tables(db_path)
    conn = _get_conn(db_path)
    conn.row_factory = sqlite3.Row

    # Find unresolved predictions that have matching resolved shadow trades
    rows = conn.execute("""
        SELECT sp.id, sp.market_id, sp.side, st.outcome, st.pnl
        FROM signal_predictions sp
        JOIN shadow_trades st ON sp.market_id = st.market_id
        WHERE sp.resolved = 0 AND st.status = 'resolved'
    """).fetchall()

    resolved_count = 0
    for row in rows:
        # outcome: 1.0 if prediction side matched, 0.0 otherwise
        trade_won = row["pnl"] > 0 if row["pnl"] is not None else None
        if trade_won is None:
            continue
        outcome = 1.0 if trade_won else 0.0
        conn.execute("""
            UPDATE signal_predictions
            SET resolved = 1, outcome = ?, resolved_at = ?
            WHERE id = ?
        """, (outcome, time.time(), row["id"]))
        resolved_count += 1

    conn.commit()
    conn.close()
    return {"resolved": resolved_count, "checked": len(rows)}


def _spearman_rank_correlation(x: list, y: list) -> float:
    """Compute Spearman rank correlation between two lists.

    Uses scipy if available, falls back to manual calculation.
    """
    if len(x) != len(y) or len(x) < 3:
        return 0.0

    try:
        from scipy.stats import spearmanr
        corr, _ = spearmanr(x, y)
        return corr if corr == corr else 0.0  # NaN check
    except ImportError:
        pass

    # Manual fallback: rank and compute
    def _rank(vals):
        indexed = sorted(enumerate(vals), key=lambda t: t[1])
        ranks = [0.0] * len(vals)
        i = 0
        while i < len(indexed):
            j = i
            while j < len(indexed) and indexed[j][1] == indexed[i][1]:
                j += 1
            avg_rank = (i + j - 1) / 2.0 + 1
            for k in range(i, j):
                ranks[indexed[k][0]] = avg_rank
            i = j
        return ranks

    n = len(x)
    rx = _rank(x)
    ry = _rank(y)
    d_sq_sum = sum((rx[i] - ry[i]) ** 2 for i in range(n))
    rho = 1 - (6 * d_sq_sum) / (n * (n * n - 1))
    return rho


def calculate_ic(source: str, window_days: int = 30, db_path: str = None) -> dict:
    """Calculate Information Coefficient for a signal source.

    IC = Spearman rank correlation between predicted confidence and realized outcome.

    Args:
        source: Signal source name
        window_days: Lookback window in days
        db_path: Optional DB path override

    Returns:
        Dict with ic_value, sample_size, status, and metadata
    """
    init_ic_tables(db_path)
    conn = _get_conn(db_path)
    conn.row_factory = sqlite3.Row
    cutoff = time.time() - (window_days * 86400)

    rows = conn.execute("""
        SELECT confidence, outcome
        FROM signal_predictions
        WHERE source = ? AND resolved = 1 AND timestamp > ?
        ORDER BY timestamp
    """, (source, cutoff)).fetchall()
    conn.close()

    if len(rows) < 10:
        return {
            "source": source,
            "ic_value": None,
            "sample_size": len(rows),
            "window_days": window_days,
            "status": "insufficient_data",
            "min_required": 10,
        }

    confidences = [float(r["confidence"]) for r in rows]
    outcomes = [float(r["outcome"]) for r in rows]

    ic = _spearman_rank_correlation(confidences, outcomes)

    # Determine status
    if abs(ic) < IC_KILL:
        status = "KILL"
    elif abs(ic) < IC_WARN:
        status = "WARN"
    else:
        status = "OK"

    # Store measurement
    try:
        conn = _get_conn(db_path)
        conn.execute("""
            INSERT INTO ic_measurements (timestamp, source, ic_value, sample_size, window_days, status)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (time.time(), source, round(ic, 6), len(rows), window_days, status))
        conn.commit()
        conn.close()
    except Exception:
        pass

    return {
        "source": source,
        "ic_value": round(ic, 6),
        "sample_size": len(rows),
        "window_days": window_days,
        "status": status,
        "thresholds": {"kill": IC_KILL, "warn": IC_WARN},
    }


def ic_report(window_days: int = 30, db_path: str = None) -> dict:
    """Generate IC report across all signal sources.

    Returns:
        Dict with per-source IC, aggregate stats, and kill recommendations
    """
    init_ic_tables(db_path)
    conn = _get_conn(db_path)
    conn.row_factory = sqlite3.Row
    cutoff = time.time() - (window_days * 86400)

    # Get all sources with resolved predictions
    sources = conn.execute("""
        SELECT DISTINCT source, COUNT(*) as count
        FROM signal_predictions
        WHERE resolved = 1 AND timestamp > ?
        GROUP BY source
    """, (cutoff,)).fetchall()

    # Total unresolved
    unresolved = conn.execute("""
        SELECT COUNT(*) as count FROM signal_predictions WHERE resolved = 0
    """).fetchone()["count"]

    conn.close()

    source_ics = {}
    kill_list = []
    warn_list = []

    for row in sources:
        source = row["source"]
        ic_data = calculate_ic(source, window_days, db_path)
        source_ics[source] = ic_data

        if ic_data["status"] == "KILL":
            kill_list.append(source)
        elif ic_data["status"] == "WARN":
            warn_list.append(source)

    # Aggregate IC (average of non-None ICs)
    valid_ics = [v["ic_value"] for v in source_ics.values() if v["ic_value"] is not None]
    avg_ic = sum(valid_ics) / len(valid_ics) if valid_ics else None

    return {
        "window_days": window_days,
        "sources": source_ics,
        "aggregate_ic": round(avg_ic, 6) if avg_ic is not None else None,
        "total_resolved": sum(r["count"] for r in sources),
        "total_unresolved": unresolved,
        "kill_list": kill_list,
        "warn_list": warn_list,
        "recommendations": {
            "kill": [f"Remove '{s}' — IC below {IC_KILL}" for s in kill_list],
            "warn": [f"Monitor '{s}' — IC below {IC_WARN}" for s in warn_list],
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

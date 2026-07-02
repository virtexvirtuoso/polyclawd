"""Restart-proof wall-clock gating for scheduler tasks.

Replaces in-memory tick counters (wiped by health-check restarts) with
last-run timestamps persisted in SQLite. A task runs when
``now - last_run >= interval_secs``; the timestamp records at gate time, so a
crash between gate and task skips that occurrence until the next interval — an
accepted trade-off that also prevents crash-loop re-execution of a failing task.
"""

import logging
import pathlib
import time

from db import connect as db_connect

logger = logging.getLogger("task_state")

STATE_DB = pathlib.Path(__file__).resolve().parent.parent / "storage" / "scheduler_state.db"

_SCHEMA = "CREATE TABLE IF NOT EXISTS task_last_run (name TEXT PRIMARY KEY, last_run REAL NOT NULL)"


def _open(db_path):
    path = STATE_DB if db_path is None else db_path
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = db_connect(path)
    conn.execute(_SCHEMA)
    return conn


def should_run(name: str, interval_secs: float, *, now: float | None = None, db_path=None) -> bool:
    """True if ``name`` hasn't run in the last ``interval_secs``; records the run."""
    ts = time.time() if now is None else now
    conn = _open(db_path)
    try:
        row = conn.execute("SELECT last_run FROM task_last_run WHERE name = ?", (name,)).fetchone()
        if row is not None and ts - row[0] < interval_secs:
            return False
        conn.execute(
            "INSERT INTO task_last_run (name, last_run) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET last_run = excluded.last_run",
            (name, ts),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def should_run_safe(name: str, interval_secs: float, **kw) -> bool:
    """should_run, but never raises — a broken state DB must not kill the
    scheduler's tick loops. Fails CLOSED (task skipped this tick)."""
    try:
        return should_run(name, interval_secs, **kw)
    except Exception:
        logger.warning("task_state gate failed for %s — skipping this tick", name, exc_info=True)
        return False


def seed(names, *, now: float | None = None, db_path=None) -> int:
    """Insert last_run rows for ``names`` that have none (no-burst deploys).

    Never overwrites an existing row. Returns how many rows were inserted.
    """
    ts = time.time() if now is None else now
    conn = _open(db_path)
    try:
        inserted = 0
        for name in names:
            cur = conn.execute(
                "INSERT OR IGNORE INTO task_last_run (name, last_run) VALUES (?, ?)",
                (name, ts),
            )
            inserted += cur.rowcount
        conn.commit()
        return inserted
    finally:
        conn.close()

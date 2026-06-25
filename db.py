"""Central SQLite connection factory — always sets busy_timeout (and WAL).

Use this instead of ``sqlite3.connect()`` for every connection to the shared
databases (shadow_trades.db / hf_latency.db / whale_meta.db). Without a
``busy_timeout`` a connection raises ``SQLITE_BUSY`` immediately when another
writer holds the lock — and across the API's 2 uvicorn workers + the scheduler +
the HF engine those writes are then silently lost (the ``_run_safe`` wrappers
swallow the exception). ``busy_timeout`` makes the connection wait instead.

Drop-in replacement::

    from db import connect as db_connect
    conn = db_connect(path, timeout=5)          # == sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row              # set row_factory yourself as before

``connect()`` additionally runs ``PRAGMA busy_timeout=5000`` and (best-effort)
``PRAGMA journal_mode=WAL``.

NOTE: importing this module requires the repo root on ``sys.path``. That holds
for the ``api`` package (run via ``uvicorn api.main:app`` and under pytest) but
NOT for modules executed standalone (``python signals/foo.py``), which set
``sys.path`` manually. Migrating those is gated on the packaging fix — see
tasks/todo.md.
"""
import sqlite3

BUSY_TIMEOUT_MS = 5000


def connect(database, timeout: float = 5.0, *, wal: bool = True, **kwargs) -> sqlite3.Connection:
    """Open a SQLite connection with busy_timeout (and WAL) applied.

    Signature-compatible with ``sqlite3.connect``; extra kwargs are forwarded.
    """
    conn = sqlite3.connect(str(database), timeout=timeout, **kwargs)
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    if wal:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            # read-only / unusual connections may reject WAL; busy_timeout still applied
            pass
    return conn

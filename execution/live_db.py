"""Live-money persistence layer — new live_* tables in the shared SQLite DB.

All `paper_*` tables pre-exist; we add four NEW `live_*` tables without
touching the existing schema. Every public function accepts a `conn` so
callers can inject a test DB (tmp file or :memory:).
"""

import sqlite3
from pathlib import Path

# Canonical prod DB path — same file the rest of the app uses.
DB_PATH = Path(__file__).parent.parent / "storage" / "shadow_trades.db"

# ---------------------------------------------------------------------------
# Column allowlists (I3) — guard CRUD helpers against unknown kwarg keys
# ---------------------------------------------------------------------------

_ALLOWED_LIVE_POSITIONS = frozenset(
    {
        "opened_at",
        "market_id",
        "market_slug",
        "market_title",
        "token_id",
        "side",
        "entry_price",
        "shares",
        "cost_usd",
        "status",
        "closed_at",
        "exit_price",
        "pnl",
        "close_reason",
        "fee_paid_total",
        "archetype",
    }
)

_ALLOWED_LIVE_FILLS = frozenset(
    {
        "ts",
        "position_id",
        "order_id",
        "side",
        "liquidity",
        "price",
        "shares",
        "usd",
        "fee_paid",
        "fair_price",
        "slippage_vs_fair",
    }
)

_ALLOWED_LIVE_EQUITY_SNAPSHOTS = frozenset(
    {
        "ts",
        "onchain_balance",
        "realized_pnl",
        "unrealized_pnl",
        "total_equity",
        "open_positions",
        "peak_equity",
        "fees_paid_cumulative",
    }
)

_ALLOWED_LIVE_PORTFOLIO_STATE = frozenset(
    {
        "ts",
        "bankroll",
        "deployed_usd",
        "realized_pnl",
        "governor_state",
        "daily_loss",
        "ramp_stage",
    }
)

_ALLOWED_LIVE_OPEN_ORDERS = frozenset(
    {
        "client_order_ref",
        "order_id",
        "token_id",
        "side",
        "price",
        "size",
        "status",
        "ts",
    }
)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS live_positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at       TEXT,
    market_id       TEXT,
    market_slug     TEXT,
    market_title    TEXT,
    token_id        TEXT,
    side            TEXT,
    entry_price     REAL,
    shares          REAL,
    cost_usd        REAL,
    status          TEXT,
    closed_at       TEXT,
    exit_price      REAL,
    pnl             REAL,
    close_reason    TEXT,
    fee_paid_total  REAL DEFAULT 0,
    archetype       TEXT DEFAULT 'weather'
);

CREATE TABLE IF NOT EXISTS live_fills (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT,
    position_id      INTEGER,
    order_id         TEXT,
    side             TEXT,
    liquidity        TEXT,
    price            REAL,
    shares           REAL,
    usd              REAL,
    fee_paid         REAL,
    fair_price       REAL,
    slippage_vs_fair REAL
);

CREATE TABLE IF NOT EXISTS live_equity_snapshots (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                    TEXT,
    onchain_balance       REAL,
    realized_pnl          REAL,
    unrealized_pnl        REAL,
    total_equity          REAL,
    open_positions        INTEGER,
    peak_equity           REAL,
    fees_paid_cumulative  REAL
);

CREATE TABLE IF NOT EXISTS live_portfolio_state (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT,
    bankroll        REAL,
    deployed_usd    REAL,
    realized_pnl    REAL,
    governor_state  TEXT,
    daily_loss      REAL,
    ramp_stage      TEXT
);

CREATE TABLE IF NOT EXISTS live_open_orders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_ref  TEXT UNIQUE,
    order_id          TEXT,
    token_id          TEXT,
    side              TEXT,
    price             REAL,
    size              REAL,
    status            TEXT,
    ts                TEXT
);

CREATE INDEX IF NOT EXISTS idx_live_positions_market_status
    ON live_positions(market_id, status);

CREATE INDEX IF NOT EXISTS idx_live_fills_position_id
    ON live_fills(position_id);

CREATE INDEX IF NOT EXISTS idx_live_open_orders_order_id
    ON live_open_orders(order_id);
"""


def init_live_tables(conn: sqlite3.Connection) -> None:
    """Create all four live_* tables and indexes (idempotent — IF NOT EXISTS).

    Uses executescript, which issues an implicit COMMIT before running — no
    extra conn.commit() is needed or issued after this call.
    """
    conn.executescript(_DDL)


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    """Return a sqlite3 connection with WAL mode, busy_timeout, and tables ensured.

    Creates the parent directory if it does not exist (needed for local dev
    where storage/ may be absent).

    check_same_thread=False is required because the live paper engine and the
    FastAPI layer share this connection across async contexts (C1/M1).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    init_live_tables(conn)
    return conn


# ---------------------------------------------------------------------------
# CRUD helpers — all accept conn to stay test-injectable.
# Pass commit=False when the caller manages the surrounding transaction.
# ---------------------------------------------------------------------------


def insert_position(conn: sqlite3.Connection, commit: bool = True, **fields) -> int:
    """Insert a new live_positions row. Returns the new rowid (position id).

    Raises ValueError if any key in *fields* is not in the allowed column set.
    """
    unknown = set(fields) - _ALLOWED_LIVE_POSITIONS
    if unknown:
        raise ValueError(f"insert_position: unknown column(s): {unknown}")
    cols = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    sql = f"INSERT INTO live_positions ({cols}) VALUES ({placeholders})"
    cur = conn.execute(sql, list(fields.values()))
    if commit:
        conn.commit()
    return cur.lastrowid


def record_fill(conn: sqlite3.Connection, commit: bool = True, **fields) -> int:
    """Insert a new live_fills row. Returns the new rowid (fill id).

    Raises ValueError if any key in *fields* is not in the allowed column set.
    """
    unknown = set(fields) - _ALLOWED_LIVE_FILLS
    if unknown:
        raise ValueError(f"record_fill: unknown column(s): {unknown}")
    cols = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    sql = f"INSERT INTO live_fills ({cols}) VALUES ({placeholders})"
    cur = conn.execute(sql, list(fields.values()))
    if commit:
        conn.commit()
    return cur.lastrowid


def snapshot_equity(conn: sqlite3.Connection, commit: bool = True, **fields) -> int:
    """Insert a new live_equity_snapshots row. Returns the new rowid.

    Raises ValueError if any key in *fields* is not in the allowed column set.
    """
    unknown = set(fields) - _ALLOWED_LIVE_EQUITY_SNAPSHOTS
    if unknown:
        raise ValueError(f"snapshot_equity: unknown column(s): {unknown}")
    cols = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    sql = f"INSERT INTO live_equity_snapshots ({cols}) VALUES ({placeholders})"
    cur = conn.execute(sql, list(fields.values()))
    if commit:
        conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# live_open_orders — idempotency / restart-safety for the executor (Phase F)
# ---------------------------------------------------------------------------


def record_open_order(conn: sqlite3.Connection, commit: bool = True, **fields) -> int:
    """Insert a new live_open_orders row. Returns the new rowid.

    The client_order_ref column has a UNIQUE constraint — inserting a duplicate
    ref raises sqlite3.IntegrityError, which the executor relies on (together
    with get_open_order_by_ref) to guarantee no double-submit on restart.

    Raises ValueError if any key in *fields* is not in the allowed column set.
    """
    unknown = set(fields) - _ALLOWED_LIVE_OPEN_ORDERS
    if unknown:
        raise ValueError(f"record_open_order: unknown column(s): {unknown}")
    cols = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    sql = f"INSERT INTO live_open_orders ({cols}) VALUES ({placeholders})"
    cur = conn.execute(sql, list(fields.values()))
    if commit:
        conn.commit()
    return cur.lastrowid


def get_open_order_by_ref(conn: sqlite3.Connection, client_order_ref: str) -> dict | None:
    """Return the live_open_orders row for *client_order_ref* as a dict, or None.

    Works whether or not conn.row_factory is sqlite3.Row (falls back to mapping
    columns by cursor description) so tests using a bare sqlite3.connect still
    get a dict.
    """
    cur = conn.execute(
        "SELECT * FROM live_open_orders WHERE client_order_ref = ? LIMIT 1",
        (client_order_ref,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    try:
        return dict(row)
    except (TypeError, ValueError):
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def update_open_order_status(conn: sqlite3.Connection, order_id: str, status: str, commit: bool = True) -> int:
    """Update the status column for the row(s) matching *order_id*.

    Returns the number of rows updated. Used to mark a resting order
    live → filled / cancelled for restart safety.
    """
    cur = conn.execute(
        "UPDATE live_open_orders SET status = ? WHERE order_id = ?",
        (status, order_id),
    )
    if commit:
        conn.commit()
    return cur.rowcount


def get_state(conn: sqlite3.Connection) -> dict | None:
    """Return the latest live_portfolio_state row as a dict, or None."""
    cur = conn.execute("SELECT * FROM live_portfolio_state ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    if row is None:
        return None
    return dict(row)


def set_state(conn: sqlite3.Connection, commit: bool = True, **fields) -> int:
    """Insert a new live_portfolio_state row. Returns the new rowid.

    Raises ValueError if any key in *fields* is not in the allowed column set.
    """
    unknown = set(fields) - _ALLOWED_LIVE_PORTFOLIO_STATE
    if unknown:
        raise ValueError(f"set_state: unknown column(s): {unknown}")
    cols = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    sql = f"INSERT INTO live_portfolio_state ({cols}) VALUES ({placeholders})"
    cur = conn.execute(sql, list(fields.values()))
    if commit:
        conn.commit()
    return cur.lastrowid

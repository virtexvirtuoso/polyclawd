"""
Theta-inversion paper-shadow runner.

Designed to be invoked from cron every 5 minutes on the VPS. Reads new
signal_snapshots entries for source='mispriced_category', computes H0/H1/H2
composite confidences from the raw_json sub-scores, and writes to
theta_shadow_log. Idempotent — re-running is safe (UNIQUE(snapshot_id)).

Side effects: write-only on theta_shadow_log table. Does NOT touch
signal_predictions, signal_snapshots, paper_positions, or source_weights.
Producer/trader code is unchanged.

Exit codes:
  0 — normal (any number of rows processed, including 0)
  1 — fatal error (DB unreachable, schema missing, etc.)

Usage:
  python3 shadow_runner.py [--db /path/to/shadow_trades.db]
                          [--source mispriced_category]
                          [--limit 1000]
                          [--dry-run]
                          [--verbose]
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

# Composite weights — must match producer signals/mispriced_category_signal.py:265-268
WEIGHT_EDGE = 0.35
WEIGHT_VOLUME = 0.25
WEIGHT_WHALE = 0.20
WEIGHT_THETA = 0.20

DEFAULT_DB = "/var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db"
DEFAULT_SOURCE = "mispriced_category"
DEFAULT_LIMIT = 1000  # safety cap per run

log = logging.getLogger("theta_shadow")


def composite_h0(edge: float, vol: float, whale: float, theta: float) -> float:
    return (
        edge * WEIGHT_EDGE
        + vol * WEIGHT_VOLUME
        + whale * WEIGHT_WHALE
        + theta * WEIGHT_THETA
    )


def composite_h1_drop(edge: float, vol: float, whale: float, theta: float) -> float:
    """Drop theta entirely, renormalize remaining weights to sum to 1.0."""
    total = WEIGHT_EDGE + WEIGHT_VOLUME + WEIGHT_WHALE
    return (
        edge * WEIGHT_EDGE
        + vol * WEIGHT_VOLUME
        + whale * WEIGHT_WHALE
    ) / total


def composite_h2_invert(edge: float, vol: float, whale: float, theta: float) -> float:
    """Invert theta direction: replace theta_score with (100 - theta_score)."""
    return (
        edge * WEIGHT_EDGE
        + vol * WEIGHT_VOLUME
        + whale * WEIGHT_WHALE
        + (100.0 - theta) * WEIGHT_THETA
    )


def ensure_schema(conn: sqlite3.Connection, sql_dir: Path) -> None:
    """Apply all .sql migrations in sql_dir. Idempotent (CREATE IF NOT EXISTS)."""
    for sql_file in sorted(sql_dir.glob("*.sql")):
        log.debug("applying %s", sql_file.name)
        conn.executescript(sql_file.read_text())
    conn.commit()


def fetch_new_snapshots(
    conn: sqlite3.Connection, source: str, limit: int
) -> list[sqlite3.Row]:
    """Return signal_snapshots rows not yet present in theta_shadow_log."""
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT ss.id, ss.market_id, ss.snapshot_time, ss.raw_json
        FROM signal_snapshots ss
        LEFT JOIN theta_shadow_log tsl ON tsl.snapshot_id = ss.id
        WHERE ss.source = ?
          AND tsl.id IS NULL
        ORDER BY ss.id ASC
        LIMIT ?
        """,
        (source, limit),
    ).fetchall()


def process_row(row: sqlite3.Row) -> dict | None:
    """Parse a snapshot row into a theta_shadow_log insert dict.

    Returns None if the raw_json is missing or malformed — caller should
    log and skip rather than crash.
    """
    try:
        rj = json.loads(row["raw_json"])
    except Exception as e:
        log.warning("snapshot %s: malformed raw_json: %s", row["id"], e)
        return None

    br = rj.get("confidence_breakdown") or {}
    try:
        edge = float(br.get("edge_score", 0))
        vol = float(br.get("volume_score", 0))
        whale = float(br.get("whale_score", 0))
        theta = float(br.get("theta_score", 0))
    except (TypeError, ValueError) as e:
        log.warning("snapshot %s: bad sub-scores: %s", row["id"], e)
        return None

    return {
        "snapshot_id": int(row["id"]),
        "market_id": row["market_id"],
        "snapshot_ts": row["snapshot_time"],
        "processed_at": time.time(),
        "edge_score": edge,
        "volume_score": vol,
        "whale_score": whale,
        "theta_score": theta,
        "h0_confidence": composite_h0(edge, vol, whale, theta),
        "h1_confidence": composite_h1_drop(edge, vol, whale, theta),
        "h2_confidence": composite_h2_invert(edge, vol, whale, theta),
        "confirmations": int(br.get("confirmations", 0)),
        "category": rj.get("category"),
        "days_to_close": rj.get("days_to_close"),
        "price_at_signal": rj.get("price"),
        "raw_json_truncated": row["raw_json"][:200] if row["raw_json"] else None,
    }


def insert_batch(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Insert rows. Uses INSERT OR IGNORE so retries on partial-failure are safe.

    Returns the actual number of rows inserted (changes() after the batch),
    not the number attempted — important because executemany's rowcount is
    unreliable with OR IGNORE.
    """
    if not rows:
        return 0
    before = conn.execute("SELECT COUNT(*) FROM theta_shadow_log").fetchone()[0]
    conn.executemany(
        """
        INSERT OR IGNORE INTO theta_shadow_log
        (snapshot_id, market_id, snapshot_ts, processed_at,
         edge_score, volume_score, whale_score, theta_score,
         h0_confidence, h1_confidence, h2_confidence,
         confirmations, category, days_to_close, price_at_signal,
         raw_json_truncated)
        VALUES (:snapshot_id, :market_id, :snapshot_ts, :processed_at,
                :edge_score, :volume_score, :whale_score, :theta_score,
                :h0_confidence, :h1_confidence, :h2_confidence,
                :confirmations, :category, :days_to_close, :price_at_signal,
                :raw_json_truncated)
        """,
        rows,
    )
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM theta_shadow_log").fetchone()[0]
    return after - before


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Theta-inversion paper-shadow runner")
    ap.add_argument("--db", default=DEFAULT_DB, help="Path to shadow_trades.db")
    ap.add_argument("--source", default=DEFAULT_SOURCE)
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--dry-run", action="store_true", help="Compute but do not insert")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument(
        "--sql-dir",
        default=str(Path(__file__).parent / "sql"),
        help="Migration directory",
    )
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    db_path = Path(args.db)
    if not db_path.exists():
        log.error("DB not found: %s", db_path)
        return 1

    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        ensure_schema(conn, Path(args.sql_dir))

        snapshots = fetch_new_snapshots(conn, args.source, args.limit)
        log.info("found %d new snapshots to process", len(snapshots))

        records = [r for r in (process_row(s) for s in snapshots) if r is not None]
        log.info("parsed %d valid rows (skipped %d)", len(records), len(snapshots) - len(records))

        if args.dry_run:
            log.info("dry-run: skipping insert. First 3 records:")
            for r in records[:3]:
                log.info("  %s", {k: r[k] for k in ("snapshot_id", "h0_confidence", "h1_confidence", "h2_confidence", "theta_score")})
            return 0

        inserted = insert_batch(conn, records)
        log.info("inserted %d rows into theta_shadow_log", inserted)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

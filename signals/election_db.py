#!/usr/bin/env python3
"""SQLite-backed election trend storage for fast time-series queries."""

import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from loguru import logger

DB_PATH = Path(__file__).parent.parent / "storage" / "election_trends.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS race_prices (
            date       TEXT NOT NULL,
            state      TEXT NOT NULL,
            race       TEXT NOT NULL DEFAULT 'senate',
            r_price    REAL NOT NULL,
            d_price    REAL NOT NULL,
            volume     REAL DEFAULT 0,
            platform   TEXT DEFAULT '',
            PRIMARY KEY (date, state, race)
        );

        CREATE TABLE IF NOT EXISTS control_history (
            date       TEXT PRIMARY KEY,
            senate_r   REAL, senate_d   REAL,
            house_r    REAL, house_d    REAL,
            pres_r     REAL, pres_d     REAL,
            composite  REAL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_race_prices_state ON race_prices(state, date);
        CREATE INDEX IF NOT EXISTS idx_race_prices_date ON race_prices(date);
    """)
    conn.commit()
    conn.close()


init_db()


def store_snapshot(snapshot: dict, deduper):
    """Write snapshot data into SQLite tables.

    Args:
        snapshot: Full snapshot dict from snapshot_elections()
        deduper: The _dedupe_state_races function (passed to avoid circular import)
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = _get_conn()

    # ── Control probabilities ──
    pc = snapshot.get("summary", {}).get("party_control", {})
    composite = snapshot.get("summary", {}).get("composite_score", 0)
    conn.execute("""
        INSERT OR REPLACE INTO control_history
        (date, senate_r, senate_d, house_r, house_d, pres_r, pres_d, composite)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        date_str,
        pc.get("senate", {}).get("republican", 0),
        pc.get("senate", {}).get("democrat", 0),
        pc.get("house", {}).get("republican", 0),
        pc.get("house", {}).get("democrat", 0),
        pc.get("presidency", {}).get("republican", 0),
        pc.get("presidency", {}).get("democrat", 0),
        composite,
    ))

    # ── Per-state race prices (senate + governor) ──
    markets = snapshot.get("markets", [])
    for race_type in ("senate", "governor"):
        state_races = deduper(markets, race_type)
        for st, info in state_races.items():
            conn.execute("""
                INSERT OR REPLACE INTO race_prices
                (date, state, race, r_price, d_price, volume, platform)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                date_str, st, race_type,
                info["r_price"], info["d_price"],
                info.get("volume", 0), info.get("platform", ""),
            ))

    # ── Primary markets (store leader/runner-up prices instead of R/D) ──
    for m in markets:
        if m.get("race_category") != "primary" or not m.get("state"):
            continue
        outs = sorted(m.get("outcomes", []), key=lambda o: o["price"], reverse=True)
        if len(outs) < 2:
            continue
        # Use r_price for leader, d_price for runner-up (repurposed columns)
        state_key = m["state"] + "_" + m["question"][:40].replace(" ", "_")
        conn.execute("""
            INSERT OR REPLACE INTO race_prices
            (date, state, race, r_price, d_price, volume, platform)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            date_str, state_key, "primary",
            outs[0]["price"], outs[1]["price"],
            m.get("volume", 0) or 0, m.get("platform", ""),
        ))

    conn.commit()
    conn.close()
    logger.info("Election DB: stored {} date for {}", date_str,
                len(snapshot.get("markets", [])))


def query_control_history(days: int = 30) -> list[dict]:
    """Get control probability history for last N days."""
    conn = _get_conn()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT * FROM control_history WHERE date >= ? ORDER BY date", (cutoff,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def query_race_history(state: str = None, race: str = "senate",
                       days: int = 30) -> dict[str, list[dict]]:
    """Get price history for races. Returns {state: [{date, r_price, d_price, volume}]}.

    If state is None, returns all states.
    """
    conn = _get_conn()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    if state:
        rows = conn.execute(
            "SELECT * FROM race_prices WHERE state=? AND race=? AND date>=? ORDER BY date",
            (state, race, cutoff),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM race_prices WHERE race=? AND date>=? ORDER BY date",
            (race, cutoff),
        ).fetchall()
    conn.close()

    by_state = {}
    for r in rows:
        by_state.setdefault(r["state"], []).append({
            "date": r["date"],
            "r_price": r["r_price"],
            "d_price": r["d_price"],
            "volume": r["volume"],
        })
    return by_state


def query_days_of_data() -> int:
    """Count distinct dates in the database."""
    conn = _get_conn()
    count = conn.execute("SELECT COUNT(DISTINCT date) FROM control_history").fetchone()[0]
    conn.close()
    return count


def backfill_from_json(snapshot_dir: Path, deduper):
    """Backfill SQLite from existing JSON snapshots."""
    import json
    count = 0
    for f in sorted(snapshot_dir.glob("*.json")):
        try:
            with open(f) as fh:
                snap = json.load(fh)
            date_str = f.stem  # e.g., "2026-04-07"
            # Temporarily override current date for store
            conn = _get_conn()
            pc = snap.get("summary", {}).get("party_control", {})
            composite = snap.get("summary", {}).get("composite_score", 0)
            conn.execute("""
                INSERT OR REPLACE INTO control_history
                (date, senate_r, senate_d, house_r, house_d, pres_r, pres_d, composite)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                date_str,
                pc.get("senate", {}).get("republican", 0),
                pc.get("senate", {}).get("democrat", 0),
                pc.get("house", {}).get("republican", 0),
                pc.get("house", {}).get("democrat", 0),
                pc.get("presidency", {}).get("republican", 0),
                pc.get("presidency", {}).get("democrat", 0),
                composite,
            ))
            markets = snap.get("markets", [])
            for race_type in ("senate", "governor"):
                state_races = deduper(markets, race_type)
                for st, info in state_races.items():
                    conn.execute("""
                        INSERT OR REPLACE INTO race_prices
                        (date, state, race, r_price, d_price, volume, platform)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        date_str, st, race_type,
                        info["r_price"], info["d_price"],
                        info.get("volume", 0), info.get("platform", ""),
                    ))
            # Primary markets
            for m in markets:
                if m.get("race_category") != "primary" or not m.get("state"):
                    continue
                outs = sorted(m.get("outcomes", []), key=lambda o: o["price"], reverse=True)
                if len(outs) < 2:
                    continue
                state_key = m["state"] + "_" + m["question"][:40].replace(" ", "_")
                conn.execute("""
                    INSERT OR REPLACE INTO race_prices
                    (date, state, race, r_price, d_price, volume, platform)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    date_str, state_key, "primary",
                    outs[0]["price"], outs[1]["price"],
                    m.get("volume", 0) or 0, m.get("platform", ""),
                ))
            conn.commit()
            conn.close()
            count += 1
        except Exception as e:
            logger.warning("Backfill failed for {}: {}", f.name, e)
    logger.info("Backfilled {} snapshots into election DB", count)
    return count

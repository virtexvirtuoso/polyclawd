#!/usr/bin/env python3
"""price_movement.py — Unified cross-sport price movement tracking.

Phase 4a of Cross-Sport Edge Methodology Upgrade.

Stores timestamped consensus fair values + soft book prices every scan cycle.
From this time series, Phase 4b classifies movement patterns and Phase 4c
determines optimal entry timing.

Tables:
  price_movement_log — one row per (sport, event_id, participant, market_type, timestamp)
  
Usage:
  from odds.price_movement import log_price_snapshot, get_movement, classify_movement
  
  # During edge scan:
  log_price_snapshot("baseball_mlb", game_id, "Yankees", "moneyline",
                     consensus_fair=0.55, best_soft=0.50, american_odds=-130)
  
  # Before alerting:
  movement = get_movement("baseball_mlb", game_id, "Yankees", "moneyline")
  classification = classify_movement(movement)
  # → "sharp_lead" / "converging" / "diverging" / "stable"
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional

DB_PATH = Path(__file__).resolve().parent.parent / "storage" / "shadow_trades.db"

_INIT_DONE = False


def _init_table(conn: sqlite3.Connection):
    global _INIT_DONE
    if _INIT_DONE:
        return
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS price_movement_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT NOT NULL,
            event_id TEXT NOT NULL,
            participant TEXT NOT NULL,
            market_type TEXT NOT NULL DEFAULT 'moneyline',
            snapshot_at TEXT NOT NULL,
            consensus_fair REAL,
            best_soft_implied REAL,
            spread_pp REAL,
            american_odds INTEGER,
            poly_price REAL,
            commence_time TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pml_lookup
            ON price_movement_log(sport, event_id, participant, market_type, snapshot_at);
    """)
    _INIT_DONE = True


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    _init_table(conn)
    return conn


def log_price_snapshot(
    sport: str,
    event_id: str,
    participant: str,
    market_type: str,
    consensus_fair: Optional[float] = None,
    best_soft_implied: Optional[float] = None,
    american_odds: Optional[int] = None,
    poly_price: Optional[float] = None,
    commence_time: Optional[str] = None,
) -> bool:
    """Log a single price observation. Called during edge scans."""
    try:
        conn = _get_conn()
        now = datetime.now(timezone.utc).isoformat()
        spread = None
        if consensus_fair is not None and best_soft_implied is not None:
            spread = round((consensus_fair - best_soft_implied) * 100, 2)
        conn.execute(
            """INSERT INTO price_movement_log
               (sport, event_id, participant, market_type, snapshot_at,
                consensus_fair, best_soft_implied, spread_pp, american_odds,
                poly_price, commence_time)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (sport, event_id[:60], participant[:80], market_type, now,
             consensus_fair, best_soft_implied, spread, american_odds,
             poly_price, commence_time),
        )
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def log_edge_prices(edge, sport: str, event_id: str) -> bool:
    """Log prices from a sec.Edge object during edge scans."""
    return log_price_snapshot(
        sport=sport,
        event_id=event_id,
        participant=getattr(edge, "participant", ""),
        market_type=getattr(edge, "market_type", "moneyline"),
        consensus_fair=getattr(edge, "book_prob", None),
        best_soft_implied=None,  # soft is the other side of the edge
        american_odds=getattr(edge, "american_odds", None),
        poly_price=getattr(edge, "poly_price", None),
        commence_time=getattr(edge, "commence_time", None),
    )


@dataclass
class PriceSnapshot:
    snapshot_at: str
    consensus_fair: Optional[float]
    best_soft_implied: Optional[float]
    spread_pp: Optional[float]
    american_odds: Optional[int]
    poly_price: Optional[float]
    hours_to_event: Optional[float]


def get_movement(
    sport: str,
    event_id: str,
    participant: str,
    market_type: str = "moneyline",
    lookback_hours: float = 12.0,
) -> List[PriceSnapshot]:
    """Get the price time series for an event/participant."""
    try:
        conn = _get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()
        rows = conn.execute(
            """SELECT snapshot_at, consensus_fair, best_soft_implied, spread_pp,
                      american_odds, poly_price, commence_time
               FROM price_movement_log
               WHERE sport=? AND event_id=? AND participant=? AND market_type=?
                 AND snapshot_at >= ?
               ORDER BY snapshot_at ASC""",
            (sport, event_id, participant, market_type, cutoff),
        ).fetchall()
        conn.close()

        snapshots = []
        for r in rows:
            hours_to = None
            if r["commence_time"]:
                try:
                    ct = datetime.fromisoformat(r["commence_time"].replace("Z", "+00:00"))
                    st = datetime.fromisoformat(r["snapshot_at"].replace("Z", "+00:00"))
                    hours_to = (ct - st).total_seconds() / 3600
                except (ValueError, TypeError):
                    pass
            snapshots.append(PriceSnapshot(
                snapshot_at=r["snapshot_at"],
                consensus_fair=r["consensus_fair"],
                best_soft_implied=r["best_soft_implied"],
                spread_pp=r["spread_pp"],
                american_odds=r["american_odds"],
                poly_price=r["poly_price"],
                hours_to_event=hours_to,
            ))
        return snapshots
    except Exception:
        return []


# ── Phase 4b: Movement classifiers ───────────────────────────────────

def classify_movement(snapshots: List[PriceSnapshot]) -> dict:
    """Classify the price movement pattern from a time series.
    
    Returns:
        {
            "pattern": "sharp_lead" | "converging" | "diverging" | "stable" | "insufficient",
            "consensus_delta_pp": float,  # total consensus move over the window
            "spread_trend": "widening" | "narrowing" | "flat",
            "n_snapshots": int,
            "hours_covered": float,
        }
    """
    if len(snapshots) < 3:
        return {"pattern": "insufficient", "n_snapshots": len(snapshots)}

    # Extract consensus fair and spread time series
    fairs = [s.consensus_fair for s in snapshots if s.consensus_fair is not None]
    spreads = [s.spread_pp for s in snapshots if s.spread_pp is not None]

    if len(fairs) < 3:
        return {"pattern": "insufficient", "n_snapshots": len(snapshots)}

    # Time coverage
    try:
        t0 = datetime.fromisoformat(snapshots[0].snapshot_at.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(snapshots[-1].snapshot_at.replace("Z", "+00:00"))
        hours = (t1 - t0).total_seconds() / 3600
    except (ValueError, TypeError):
        hours = 0

    # Consensus delta
    consensus_delta = (fairs[-1] - fairs[0]) * 100  # in pp

    # Spread trend (are edges widening or narrowing?)
    spread_trend = "flat"
    if len(spreads) >= 3:
        first_half = spreads[:len(spreads)//2]
        second_half = spreads[len(spreads)//2:]
        avg_first = sum(first_half) / len(first_half)
        avg_second = sum(second_half) / len(second_half)
        if avg_second - avg_first > 1.0:
            spread_trend = "widening"
        elif avg_first - avg_second > 1.0:
            spread_trend = "narrowing"

    # Poly price convergence check (for markets without soft book data, e.g. weather)
    # If poly_price is moving toward consensus_fair, the edge is shrinking.
    poly_converging = False
    polys = [s.poly_price for s in snapshots if s.poly_price is not None]
    if len(polys) >= 3 and len(fairs) >= 3:
        # Compute edge (fair - poly) at start and end
        edge_start = fairs[0] - polys[0]
        edge_end = fairs[-1] - polys[-1]
        # If |edge| is shrinking by more than 1pp, it's converging
        if abs(edge_start) - abs(edge_end) > 0.015:  # 1pp shrinkage
            poly_converging = True

    # Pattern classification
    abs_delta = abs(consensus_delta)
    if abs_delta < 1.0 and not poly_converging:
        pattern = "stable"
    elif spread_trend == "narrowing":
        pattern = "converging"  # edge closing — soft catching up to sharp
    elif spread_trend == "widening":
        pattern = "diverging"  # edge growing — sharp moving away from soft
    elif poly_converging:
        pattern = "converging"  # edge closing — poly converging on fair (no soft book)
    else:
        pattern = "sharp_lead"  # sharp moved, spread unchanged

    return {
        "pattern": pattern,
        "consensus_delta_pp": round(consensus_delta, 2),
        "spread_trend": spread_trend,
        "n_snapshots": len(snapshots),
        "hours_covered": round(hours, 1),
    }


# ── Phase 4d: DK lag profiling ───────────────────────────────────────

def dk_lag_profile(sport: str, lookback_days: int = 30) -> Optional[dict]:
    """Measure how quickly DraftKings converges toward Pinnacle after a sharp move.
    
    Requires price_movement_log entries with both consensus_fair (Pinnacle-weighted)
    and best_soft_implied (DK/FD) populated.
    
    Returns:
        {
            "avg_lag_hours": float,
            "median_lag_hours": float,
            "n_convergence_events": int,
        }
    or None if insufficient data.
    """
    try:
        conn = _get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        rows = conn.execute(
            """SELECT event_id, participant, snapshot_at, consensus_fair, best_soft_implied, spread_pp
               FROM price_movement_log
               WHERE sport=? AND snapshot_at >= ?
                 AND consensus_fair IS NOT NULL AND best_soft_implied IS NOT NULL
               ORDER BY event_id, participant, snapshot_at ASC""",
            (sport, cutoff),
        ).fetchall()
        conn.close()

        if len(rows) < 10:
            return None

        # Group by event+participant, find convergence events
        from collections import defaultdict
        series = defaultdict(list)
        for r in rows:
            key = f"{r['event_id']}|{r['participant']}"
            series[key].append({
                "t": r["snapshot_at"],
                "spread": r["spread_pp"],
            })

        lag_hours = []
        for key, points in series.items():
            if len(points) < 3:
                continue
            # Find where spread goes from >3pp to <1pp (convergence)
            for i, p in enumerate(points):
                if p["spread"] is not None and p["spread"] > 3.0:
                    # Look forward for convergence
                    for j in range(i + 1, len(points)):
                        if points[j]["spread"] is not None and points[j]["spread"] < 1.0:
                            try:
                                t0 = datetime.fromisoformat(p["t"].replace("Z", "+00:00"))
                                t1 = datetime.fromisoformat(points[j]["t"].replace("Z", "+00:00"))
                                hours = (t1 - t0).total_seconds() / 3600
                                if 0 < hours < 24:
                                    lag_hours.append(hours)
                            except (ValueError, TypeError):
                                pass
                            break

        if not lag_hours:
            return None

        lag_hours.sort()
        return {
            "avg_lag_hours": round(sum(lag_hours) / len(lag_hours), 1),
            "median_lag_hours": round(lag_hours[len(lag_hours) // 2], 1),
            "n_convergence_events": len(lag_hours),
        }
    except Exception:
        return None


if __name__ == "__main__":
    # Show current data
    try:
        conn = _get_conn()
        row = conn.execute("SELECT COUNT(*) as n FROM price_movement_log").fetchone()
        print(f"Price movement log: {row['n']} snapshots")
        
        # Show per-sport breakdown
        rows = conn.execute("""
            SELECT sport, COUNT(*) as n, 
                   MIN(snapshot_at) as earliest,
                   MAX(snapshot_at) as latest
            FROM price_movement_log
            GROUP BY sport
        """).fetchall()
        for r in rows:
            print(f"  {r['sport']}: {r['n']} snapshots ({r['earliest'][:10]} to {r['latest'][:10]})")
        conn.close()
    except Exception as e:
        print(f"No data yet: {e}")

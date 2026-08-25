#!/usr/bin/env python3
"""
Backfill weather_resolutions.jsonl from paper_positions.

Phase 2C of the calibration outcome-path fix (see vault note
Calibration-Metric-Outcome-Path-Fix-Apr2026). The legacy outcome JSONL
stopped receiving entries on 2026-04-13 because the only writer
(close_position()) became unreachable. This script reconstructs the file
from the source-of-truth (the paper_positions table) so the calibration
metric has signal immediately, instead of waiting weeks for fresh closes.

Scope:
  • Only weather positions (strategy IN ('weather','weather_ensemble'))
  • Only post-2026-04-28 closes (when entry_forecast_json started being
    populated). Pre-Apr-28 rows would fall back to confidence-tier and
    reproduce the original bug — they're skipped intentionally.
  • Idempotent: truncates the target file before writing.

Run from project root:
  python3 scripts/backfill_outcome_resolutions.py             # dry-run
  python3 scripts/backfill_outcome_resolutions.py --apply     # write
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from signals.resolution_logger import (  # noqa: E402
    LOG_FILES, AUTO_LOG_FILES, _model_p_yes_from_forecast,
)

DB_PATH = ROOT / "storage" / "shadow_trades.db"
OUTCOME_FILE = LOG_FILES["weather_ensemble"]
AUTO_FILE = AUTO_LOG_FILES["weather_ensemble"]
CUTOFF = "2026-04-28T00:00:00+00:00"


def load_existing_auto_market_ids() -> set:
    """Auto-resolved closes already in the auto file — preserve them, don't double-write."""
    if not AUTO_FILE.exists():
        return set()
    ids = set()
    with open(AUTO_FILE) as f:
        for line in f:
            try:
                rec = json.loads(line)
                if rec.get("market_id"):
                    ids.add(rec["market_id"])
            except json.JSONDecodeError:
                continue
    return ids


def fetch_closes() -> list[dict]:
    """Closed weather positions post-cutoff, ordered chronologically."""
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute("PRAGMA busy_timeout=8000")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, market_id, market_title, side, entry_price, bet_size,
               edge_pct, archetype, strategy, confidence, entry_forecast_json,
               status, closed_at, exit_price, pnl, close_reason, closing_line
        FROM paper_positions
        WHERE strategy IN ('weather', 'weather_ensemble')
          AND status IN ('won', 'lost', 'stopped', 'displaced')
          AND closed_at >= ?
        ORDER BY closed_at ASC
        """,
        (CUTOFF,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def build_record(row: dict) -> dict:
    side = row["side"] or ""
    mc_prob = _model_p_yes_from_forecast(
        row.get("entry_forecast_json"),
        row.get("confidence"),
        side,
    )
    won_inferred = (row["status"] == "won") or (
        row["status"] in ("stopped", "displaced") and (row.get("pnl") or 0) > 0
    )
    return {
        "ts": row["closed_at"],
        "strategy": "weather_ensemble",
        "market_id": row["market_id"],
        "market_title": row["market_title"] or "",
        "side": side,
        "mc_prob": round(mc_prob, 4),
        "market_price": round(row["entry_price"] or 0, 4),
        "edge_pct": round(row["edge_pct"] or 0, 4),
        "archetype": row["archetype"] or "",
        "won": won_inferred,
        "pnl": round(row["pnl"] or 0, 2),
        "close_reason": row["close_reason"] or "",
        "closing_line": row.get("closing_line"),
        "_backfilled": True,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually write the file (default: dry-run preview)")
    args = ap.parse_args()

    rows = fetch_closes()
    print(f"Found {len(rows)} closed weather positions since {CUTOFF}")
    if not rows:
        return

    # Stats by close_reason class
    classes = {}
    forecast_present = 0
    for r in rows:
        cr = r["close_reason"] or ""
        cls = (cr.split(":", 1)[0] or "(empty)").strip()
        classes[cls] = classes.get(cls, 0) + 1
        if r.get("entry_forecast_json"):
            forecast_present += 1
    print(f"Close-reason classes: {dict(sorted(classes.items(), key=lambda x: -x[1]))}")
    print(f"With entry_forecast_json: {forecast_present}/{len(rows)} "
          f"({100*forecast_present/len(rows):.0f}%)")

    records = [build_record(r) for r in rows]
    wins = sum(1 for r in records if r["won"])
    brier = sum((r["mc_prob"] - (1.0 if r["won"] else 0.0)) ** 2 for r in records) / len(records)
    print(f"Reconstructed: n={len(records)}, wr={wins/len(records):.1%}, "
          f"brier={brier:.3f}")

    if not args.apply:
        print("\nDRY RUN — re-run with --apply to write")
        print(f"Would write to: {OUTCOME_FILE}")
        print("First 3 records preview:")
        for r in records[:3]:
            print(f"  {json.dumps(r)[:200]}")
        return

    # Backup if existing file present
    if OUTCOME_FILE.exists():
        backup = OUTCOME_FILE.with_suffix(
            f".jsonl.bak.{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        )
        os.rename(OUTCOME_FILE, backup)
        print(f"Existing file backed up: {backup.name}")

    OUTCOME_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTCOME_FILE, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    print(f"Wrote {len(records)} records to {OUTCOME_FILE}")


if __name__ == "__main__":
    main()

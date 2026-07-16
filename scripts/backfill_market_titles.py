#!/usr/bin/env python3
"""backfill_market_titles.py — repair hex/empty market_title rows in live_positions.

Alert System Overhaul plan, Task 3.4. Selects rows whose title is empty,
equals the market_id, or is a long space-less token (hex/token id), resolves
each via odds.gamma_title.resolve_title, and (with --apply) writes the fix.

Default is DRY RUN: prints planned updates, writes nothing.

Usage:
    venv/bin/python3 scripts/backfill_market_titles.py            # dry run
    venv/bin/python3 scripts/backfill_market_titles.py --apply    # write fixes
    venv/bin/python3 scripts/backfill_market_titles.py --db path/to.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from odds.gamma_title import resolve_title  # noqa: E402

DEFAULT_DB = Path(__file__).resolve().parent.parent / "storage" / "shadow_trades.db"

# Bad-title predicate per plan: empty, id-as-title, or long space-less token.
_SELECT_BAD = (
    "SELECT id, market_id, market_title FROM live_positions "
    "WHERE market_title = '' "
    "   OR market_title IS NULL "
    "   OR market_title = market_id "
    "   OR (length(market_title) > 60 AND market_title NOT LIKE '% %')"
)


def backfill(db_path: Path, apply: bool = False) -> dict:
    """Resolve bad titles; update rows only when apply=True. Returns stats."""
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    stats = {"candidates": 0, "resolved": 0, "unresolved": 0, "updated": 0}
    try:
        rows = con.execute(_SELECT_BAD).fetchall()
        stats["candidates"] = len(rows)
        for row in rows:
            title = resolve_title(row["market_id"], db_path=db_path)
            if title:
                stats["resolved"] += 1
                action = "UPDATE" if apply else "WOULD UPDATE"
                print(f"{action} id={row['id']}: {row['market_id'][:20]}... -> {title[:70]!r}")
                if apply:
                    con.execute(
                        "UPDATE live_positions SET market_title = ? WHERE id = ?",
                        (title, row["id"]),
                    )
                    stats["updated"] += 1
            else:
                stats["unresolved"] += 1
                print(
                    f"UNRESOLVED id={row['id']}: market_id={row['market_id'][:40]}... "
                    f"(not a 0x condition id, or Gamma lookup failed)"
                )
        if apply:
            con.commit()
    finally:
        con.close()

    mode = "APPLIED" if apply else "DRY RUN (use --apply to write)"
    print(
        f"\n{mode}: {stats['candidates']} candidates, {stats['resolved']} resolved, "
        f"{stats['unresolved']} unresolved, {stats['updated']} updated"
    )
    return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="path to shadow_trades.db")
    parser.add_argument("--apply", action="store_true", help="write updates (default: dry run)")
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"ERROR: DB not found: {args.db}")
        return 1
    backfill(args.db, apply=args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

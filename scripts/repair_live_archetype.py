#!/usr/bin/env python3
"""Repair archetype on live_positions rows mislabelled by the hardcoded literal.

Background
----------
Until 2026-08-21, ``live_position_tracker.record_real_fill`` hardcoded
``archetype="weather"`` for EVERY live position, discarding the ``category``
the executor already knew. The canary gate is pre-registered on per-strategy
CI thresholds, so a ledger where every row says "weather" cannot be evaluated
as designed.

Recovery is possible without a redeploy because ``client_order_ref`` encodes
the originating strategy and survives in ``live_open_orders``:

    live_positions.id
      <- live_fills.position_id
      -> live_fills.order_id
      -> live_open_orders.order_id
      -> live_open_orders.client_order_ref  ("sw-<date>-<cond>-<idx>")

DRY RUN BY DEFAULT. Pass --apply to write. Always take a DB copy first.

    python3 scripts/repair_live_archetype.py --db storage/shadow_trades.db
    python3 scripts/repair_live_archetype.py --db storage/shadow_trades.db --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

# client_order_ref prefix -> the category the executor would pass today.
# Extend this map as new strategies gain live executors; an unmapped prefix is
# reported and skipped rather than guessed at.
PREFIX_TO_CATEGORY = {
    "sw": "smart_wallet",
    "wx": "weather_resolution",
    "sc": "soccer_match_3way",
}


def classify(client_order_ref: str) -> str | None:
    """Return the category for a client_order_ref, or None if unrecognised."""
    if not client_order_ref or "-" not in client_order_ref:
        return None
    return PREFIX_TO_CATEGORY.get(client_order_ref.split("-", 1)[0])


def collect(conn: sqlite3.Connection) -> list[dict]:
    """One row per live position, with its recovered category (or None)."""
    rows = conn.execute(
        """
        SELECT p.id            AS position_id,
               p.market_title  AS market_title,
               p.archetype     AS archetype,
               p.status        AS status,
               p.opened_at     AS opened_at,
               (SELECT o.client_order_ref
                  FROM live_fills f
                  JOIN live_open_orders o ON o.order_id = f.order_id
                 WHERE f.position_id = p.id
                   AND f.side = 'BUY'
                   AND COALESCE(o.client_order_ref, '') <> ''
                 ORDER BY f.id ASC
                 LIMIT 1)      AS client_order_ref
          FROM live_positions p
         ORDER BY p.id
        """
    ).fetchall()

    out = []
    for r in rows:
        ref = r["client_order_ref"] or ""
        out.append({
            "position_id": r["position_id"],
            "market_title": (r["market_title"] or "")[:44],
            "archetype": r["archetype"],
            "status": r["status"],
            "opened_at": r["opened_at"],
            "client_order_ref": ref,
            "recovered": classify(ref),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="path to shadow_trades.db")
    ap.add_argument("--apply", action="store_true",
                    help="write the repaired archetypes (default: dry run)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    rows = collect(conn)
    if not rows:
        print("no live_positions rows found")
        return 0

    changed, unrecoverable, already_ok = [], [], []
    for r in rows:
        if r["recovered"] is None:
            unrecoverable.append(r)
        elif r["recovered"] != r["archetype"]:
            changed.append(r)
        else:
            already_ok.append(r)

    print("%-4s %-8s %-14s %-14s %s" % ("id", "status", "archetype", "-> recovered", "market"))
    print("-" * 88)
    for r in changed:
        print("%-4s %-8s %-14s %-14s %s" % (
            r["position_id"], r["status"], r["archetype"], r["recovered"], r["market_title"]))
    for r in unrecoverable:
        print("%-4s %-8s %-14s %-14s %s  [ref=%s]" % (
            r["position_id"], r["status"], r["archetype"], "UNRECOVERABLE",
            r["market_title"], r["client_order_ref"] or "(none)"))

    print()
    print("to repair    : %d" % len(changed))
    print("already ok   : %d" % len(already_ok))
    print("unrecoverable: %d  (no client_order_ref -- pre-dates the open-orders "
          "ledger or was closed manually)" % len(unrecoverable))

    if not args.apply:
        print("\nDRY RUN -- nothing written. Re-run with --apply to commit.")
        return 0

    if not changed:
        print("\nnothing to write")
        return 0

    with conn:
        for r in changed:
            conn.execute(
                "UPDATE live_positions SET archetype = ? WHERE id = ?",
                (r["recovered"], r["position_id"]),
            )
    print("\nWROTE %d row(s)" % len(changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())

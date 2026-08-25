#!/usr/bin/env python3
"""
Backfill resolution_price (and closing_line where missing) on stopped paper_positions.

Why: 379 stopped weather rows have resolution_price=NULL, blocking the held-to-resolution
counterfactual. Per Trade-Review-585-Consensus-May2026.md action #1 (unanimous DO-NOW).

Semantic: resolution_price uses the SAME bet-perspective convention as the auto-resolved
path (signals/paper_portfolio.py::resolve_open_positions): 1.0 if (outcome == side) else 0.0.

Scope: only rows where status='stopped' AND resolution_price IS NULL. Idempotent.

Run from project root:
  python3 scripts/backfill_resolution_price.py             # dry-run (default)
  python3 scripts/backfill_resolution_price.py --apply     # write
  python3 scripts/backfill_resolution_price.py --limit 20  # sample
"""
import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from config.polymarket_urls import CLOB_API  # polyproxy: central URL config

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "storage" / "shadow_trades.db"

UA = {"User-Agent": "Mozilla/5.0"}

def _fetch(url: str, timeout: int = 10):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return None

def resolve_polymarket(market_id: str):
    """Mirror of signals/paper_portfolio.py::_resolve_polymarket without side dep.
    Returns (outcome, closing_line) where outcome in {'YES','NO',None}."""
    data = _fetch(f"{CLOB_API}/markets/{market_id}")
    closing_line = None

    if data and data.get("closed"):
        tokens = data.get("tokens", [])
        if tokens and len(tokens) >= 2:
            closing_line = float(tokens[0].get("price", 0.5))
            for t in tokens:
                if t.get("winner") is True:
                    name = (t.get("outcome") or "").strip().upper()
                    if name in ("YES", "NO"):
                        return name, closing_line
            first_won = tokens[0].get("winner") is True
            if any(t.get("winner") is True for t in tokens):
                return ("YES" if first_won else "NO"), closing_line
            yes_p = float(tokens[0].get("price", 0.5))
            if yes_p > 0.95:
                return "YES", closing_line
            if yes_p < 0.05:
                return "NO", closing_line

    if data and not data.get("closed"):
        tokens = data.get("tokens", [])
        if tokens and len(tokens) >= 2:
            yes_p = float(tokens[0].get("price", 0.5))
            closing_line = yes_p
            end_date = data.get("end_date_iso") or ""
            if end_date:
                try:
                    end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                    hours_past = (datetime.now(timezone.utc) - end_dt).total_seconds() / 3600
                    if hours_past >= 24:
                        threshold = 0.95
                    elif hours_past >= 12:
                        threshold = 0.975
                    elif hours_past >= 6:
                        threshold = 0.99
                    else:
                        threshold = None
                    if threshold is not None:
                        if yes_p > threshold:
                            return "YES", closing_line
                        if yes_p < (1 - threshold):
                            return "NO", closing_line
                except Exception:
                    pass

    return None, closing_line

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write to DB (default: dry-run)")
    parser.add_argument("--limit", type=int, default=0, help="Cap number of rows processed")
    parser.add_argument("--sleep", type=float, default=0.25, help="Sleep between API calls")
    args = parser.parse_args()

    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.execute("PRAGMA busy_timeout=8000")
    conn.row_factory = sqlite3.Row

    q = """SELECT id, market_id, market_slug, side, entry_price, bet_size, closing_line
           FROM paper_positions
           WHERE status='stopped' AND resolution_price IS NULL"""
    if args.limit > 0:
        q += f" LIMIT {args.limit}"
    rows = conn.execute(q).fetchall()
    print(f"[scope] {len(rows)} stopped rows with resolution_price=NULL")

    stats = {"resolved_yes": 0, "resolved_no": 0, "unresolved": 0,
             "would_have_won": 0, "would_have_lost": 0,
             "counterfactual_pnl_delta": 0.0}
    updates = []

    for i, r in enumerate(rows, 1):
        outcome, closing_line = resolve_polymarket(r["market_id"])
        if outcome is None:
            stats["unresolved"] += 1
            if i % 25 == 0:
                print(f"  [{i}/{len(rows)}] unresolved so far={stats['unresolved']}")
            time.sleep(args.sleep)
            continue

        won = (outcome == r["side"])
        resolution_price = 1.0 if won else 0.0

        if won:
            if r["side"] == "YES":
                pnl_held = r["bet_size"] * (1.0 / r["entry_price"] - 1.0)
            else:
                pnl_held = r["bet_size"] * (1.0 / (1.0 - r["entry_price"]) - 1.0)
        else:
            pnl_held = -r["bet_size"]

        stats[f"resolved_{outcome.lower()}"] += 1
        if won:
            stats["would_have_won"] += 1
        else:
            stats["would_have_lost"] += 1
        stats["counterfactual_pnl_delta"] += pnl_held

        new_closing_line = r["closing_line"] if r["closing_line"] is not None else closing_line
        updates.append((resolution_price, new_closing_line, r["id"]))

        if i % 25 == 0:
            print(f"  [{i}/{len(rows)}] resolved YES={stats['resolved_yes']} NO={stats['resolved_no']} unresolved={stats['unresolved']}")
        time.sleep(args.sleep)

    print()
    print("=== RESULTS ===")
    print(f"  resolved YES outcome:  {stats['resolved_yes']}")
    print(f"  resolved NO outcome:   {stats['resolved_no']}")
    print(f"  unresolved (skipped):  {stats['unresolved']}")
    print(f"  would-have-won:        {stats['would_have_won']}")
    print(f"  would-have-lost:       {stats['would_have_lost']}")
    print(f"  counterfactual hold-to-res PnL: ${stats['counterfactual_pnl_delta']:+,.2f}")
    print(f"  rows ready to update:  {len(updates)}")

    if not args.apply:
        print("\n[DRY-RUN] No DB writes. Re-run with --apply to commit.")
        return

    print(f"\n[APPLY] Writing {len(updates)} rows...")
    conn.executemany(
        "UPDATE paper_positions SET resolution_price=?, closing_line=COALESCE(closing_line, ?) WHERE id=?",
        updates,
    )
    conn.commit()
    print("[APPLY] done.")
    conn.close()

if __name__ == "__main__":
    main()

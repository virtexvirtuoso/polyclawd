#!/usr/bin/env python3
"""Populate the soccer / UFC / World-Cup edge caches read by the dashboard routes.

Run on a cron cadence (soccer-match ~6h, futures ~1x/day, ufc daily + fight-card
days). Computes via the engines (threadpool-safe enrichment) and writes through
the StorageService so the API routes only ever SERVE cached results — keeping
the event loop free and Odds-API credit spend on a controlled cadence.

Usage:
    venv/bin/python scripts/sports_edge_scan.py                 # all
    venv/bin/python scripts/sports_edge_scan.py soccer_match    # one or more
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from odds.soccer_match_edge import get_soccer_match_summary
from odds.soccer_futures_edge import get_soccer_futures_summary, get_soccer_wc_board_summary
from odds.ufc_edge import get_ufc_summary
from api.deps import get_storage_service

CACHE = {
    "soccer_match": ("soccer_match_edges.json", get_soccer_match_summary),
    "soccer_futures": ("soccer_futures_edges.json", get_soccer_futures_summary),
    "soccer_wc_board": ("soccer_wc_board.json", get_soccer_wc_board_summary),
    "ufc": ("ufc_edges.json", get_ufc_summary),
}

try:
    from odds.rate_limiter import CREDIT_FLOOR  # single source of truth (5,000)
except Exception:  # pragma: no cover
    CREDIT_FLOOR = 5_000  # don't start a scan with fewer than this many Odds-API credits


async def _credits_ok() -> bool:
    try:
        from odds.the_odds_api import get_credit_status

        rem = get_credit_status().get("remaining")
        return rem is None or rem > CREDIT_FLOOR
    except Exception:
        return True  # unknown → allow; the engines degrade gracefully on quota errors


async def run(which=None):
    if not await _credits_ok():
        print("credit floor reached — skipping scan")
        return
    storage = get_storage_service()
    for key in which or list(CACHE):
        fname, fn = CACHE[key]
        try:
            summary = await fn()
            summary["cached"] = True
            await storage.save(fname, summary)
            print(f"{key}: {summary.get('total_edges', 0)} edges -> {fname}")
        except Exception as e:
            print(f"{key}: scan failed: {e}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a in CACHE] or None
    asyncio.run(run(args))

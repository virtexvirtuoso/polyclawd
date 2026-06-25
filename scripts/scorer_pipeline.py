#!/usr/bin/env python3
"""
scorer_pipeline.py — end-to-end PAPER pipeline for goalscorer prop edges.

Spec: 02-Projects/Polyclawd/Development/prop-edge-system-spec.md (§3-§6).
Composes the validated modules into one flow:

    fetch event-odds  →  find_scorer_edges (§3+§4)  →  size_slate (§Step4, B14/B17)
        →  record_positions (PAPER ledger, §5)  →  optional Telegram alert
    [separate pass]  resolve_open_positions  →  paper P&L / CLV report

PAPER-ONLY. US sportsbooks have no bet-placement API; "going live" is a human
placing the alerted bets manually and is OUT OF SCOPE here. Nothing in this file
moves real money. Not wired into the live Polyclawd aggregation or deployed to
the VPS — that is gated on the CLV gate (§6 Step 3) confirming.

Usage:
  python3 scripts/scorer_pipeline.py --scan      # fetch → edges → size → record (+ --alert)
  python3 scripts/scorer_pipeline.py --resolve   # settle open positions via ESPN/API-Football
  python3 scripts/scorer_pipeline.py --report    # paper P&L summary
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from odds.scorer_edge import ScorerSportConfig, find_scorer_edges  # noqa: E402
from odds.scorer_sizing import SizingConfig, size_slate  # noqa: E402
from signals import scorer_paper_portfolio as pp  # noqa: E402
from signals import scorer_resolution_fetch as rf  # noqa: E402


def scan(events_odds, edge_config, sizing_config, db, alert=False, lineup_checker=None, now=None):
    """edges → sized → recorded. `events_odds` is injectable (tests pass synthetic);
    a live fetch helper lives in scorer_resolution_fetch / the logger.
    `lineup_checker`/`now` are injectable for tests and for a real lineup source."""
    edges = find_scorer_edges(events_odds, edge_config, lineup_checker=lineup_checker, now=now)
    tradeable = [e for e in edges if getattr(e, "tradeable", False)]
    sized = size_slate(edges, sizing_config)
    n = pp.record_positions(sized, db)
    if alert and sized:
        pp.send_alert(sized, send=True)
    return {"edges": len(edges), "tradeable": len(tradeable), "sized": len(sized), "recorded": n}


def resolve(db, league_slug="fifa.world", af_key=None):
    """Settle open paper positions off FINAL event sets (two-source when API-Football
    key present, else ESPN-only flagged). Network only in this pass, never in --scan."""
    resolver = rf.make_resolver(league_slug=league_slug, af_key=af_key or os.getenv("APIFOOTBALL_KEY"))
    return pp.resolve_open_positions(db, resolver)


def main():
    ap = argparse.ArgumentParser(description="Goalscorer prop PAPER pipeline (spec §3-§6).")
    ap.add_argument("--db", default="scorer_paper.db")
    ap.add_argument("--sport", default="soccer_fifa_world_cup")
    ap.add_argument("--league-slug", default="fifa.world", help="ESPN soccer league slug for resolution")
    ap.add_argument("--bankroll", type=float, default=10000.0)
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--resolve", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--alert", action="store_true", help="send PAPER Telegram alert on --scan")
    args = ap.parse_args()

    db = pp.db_connect(args.db)

    if args.scan:
        events = rf.fetch_live_event_odds(args.sport)  # live; needs ODDS_API_KEY
        ec = ScorerSportConfig(sport_key=args.sport)
        sc = SizingConfig(bankroll=args.bankroll)
        print("[scan]", scan(events, ec, sc, db, alert=args.alert))
    if args.resolve:
        print("[resolve]", resolve(db, league_slug=args.league_slug))
    if args.report or not (args.scan or args.resolve):
        rep = pp.portfolio_report(db)
        print("[report]", rep)

    db.close()


if __name__ == "__main__":
    main()

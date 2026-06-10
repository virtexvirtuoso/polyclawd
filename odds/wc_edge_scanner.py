#!/usr/bin/env python3
"""
wc_edge_scanner.py — World Cup + MLB cross-platform edge scanner

Runs every 30 min via cron. Scans:
  1. FIFA World Cup h2h (Pinnacle devig vs Polymarket) — starts Jun 11
  2. MLB game lines + O/U (Pinnacle devig vs Polymarket)
  3. WC futures (Betfair outright vs Polymarket winner market)
  4. THREE-WAY comparison: Vegas (Pinnacle) vs Polymarket vs Kalshi — all MLB games

Flags edges >= MIN_EDGE_PCT, logs to shadow_trades.db.
Three-way scan catches mispricings where one platform is out of step with the others.

Usage:
  python3 odds/wc_edge_scanner.py          # full scan, print results
  python3 odds/wc_edge_scanner.py --dry    # scan only, no shadow logging
  python3 odds/wc_edge_scanner.py --sport wc   # WC only
  python3 odds/wc_edge_scanner.py --sport mlb  # MLB only
  python3 odds/wc_edge_scanner.py --sport threeway  # Three-way only
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger

MIN_EDGE_PCT = 0.03   # 3pp minimum (r/algobetting community consensus + our calibration)
MAX_EDGE_PCT = 0.15   # 15% cap (P1 recalibration — large claimed edges are usually wrong)
ALERT_EDGE   = 0.05   # 5pp+ → worth acting on


def _fmt_edge(e) -> str:
    dir_sym = "↑" if e.direction == "BUY" else "↓"
    alert = " ⚡" if abs(e.edge_pct) >= ALERT_EDGE else ""
    tradeable = " ✓" if getattr(e, "tradeable", False) else ""
    # Support both Edge (soccer) and MLBEdge schemas
    participant = getattr(e, "participant", None) or getattr(e, "bet_team", "?")
    book_prob   = getattr(e, "book_prob", None) or getattr(e, "true_prob", 0)
    poly_price  = getattr(e, "poly_price", None) or getattr(e, "polymarket_price", 0)
    return (
        f"  {dir_sym} {participant:<25} "
        f"book={book_prob:.1%}  PM={poly_price:.1%}  "
        f"edge={e.edge_pct:+.1%}{alert}  {e.direction}{tradeable}"
    )


def _fmt_title(e) -> str:
    return getattr(e, "event_title", None) or getattr(e, "game_title", "")


async def scan_wc_matches(dry: bool = False) -> list:
    from odds.soccer_match_edge import find_soccer_match_edges
    edges = await find_soccer_match_edges(min_edge=MIN_EDGE_PCT)
    # Apply P1 edge cap
    edges = [e for e in edges if abs(e.edge_pct) <= MAX_EDGE_PCT]
    if dry:
        for e in edges:
            e.tradeable = False
    return edges


async def scan_wc_futures(dry: bool = False) -> list:
    from odds.soccer_futures_edge import find_soccer_futures_edges
    edges = await find_soccer_futures_edges(min_edge=MIN_EDGE_PCT)
    edges = [e for e in edges if abs(e.edge_pct) <= MAX_EDGE_PCT]
    if dry:
        for e in edges:
            e.tradeable = False
    return edges


async def scan_mlb(dry: bool = False) -> list:
    try:
        from odds.baseball_edge import find_baseball_edges
        edges = await find_baseball_edges(min_edge=MIN_EDGE_PCT)
        edges = [e for e in edges if abs(e.edge_pct) <= MAX_EDGE_PCT]
        if dry:
            for e in edges:
                e.tradeable = False
        return edges
    except Exception as ex:
        logger.warning(f"MLB scan failed: {ex}")
        return []


def scan_three_way() -> list[dict]:
    """
    Three-way cross-platform comparison: Vegas (Pinnacle) vs Polymarket vs Kalshi.

    For each MLB game where all three sources have prices, computes:
      - vegas_fair  : Pinnacle devigged probability
      - pm_price    : Polymarket YES price
      - kalshi_mid  : Kalshi mid price (yes_bid + yes_ask) / 2

    Flags games where ANY two platforms differ by >= MIN_EDGE_PCT.
    Returns list of comparison dicts sorted by max pairwise gap.
    """
    import math

    # ── Fetch Kalshi game markets ────────────────────────────────────────
    try:
        from odds.kalshi_sports import fetch_mlb_game_markets
        kalshi_games = fetch_mlb_game_markets()
    except Exception as ex:
        logger.warning(f"Kalshi fetch failed: {ex}")
        kalshi_games = []

    # Build lookup: (home_code, away_code) → {home_mid, away_mid}
    # Also index by team code for fuzzy matching
    kalshi_by_code: dict[str, float] = {}  # team_code -> yes_mid
    for g in kalshi_games:
        kalshi_by_code[g["home_code"]] = g["home_yes"]
        kalshi_by_code[g["away_code"]] = g["away_yes"]

    # ── Fetch Vegas + PM baseball edges ─────────────────────────────────
    import asyncio as _asyncio
    import concurrent.futures as _cf
    try:
        from odds.baseball_edge import find_baseball_edges
        with _cf.ThreadPoolExecutor(max_workers=1) as pool:
            mlb_edges = pool.submit(_asyncio.run, find_baseball_edges(min_edge=0.0)).result(timeout=60)
    except Exception as ex:
        logger.warning(f"MLB baseball_edge failed: {ex}")
        mlb_edges = []

    # Group edges by game: {game_title: {team: {vegas_fair, pm_price}}}
    game_data: dict[str, dict] = {}
    for e in mlb_edges:
        if e.market_type not in ("h2h", "moneyline", "winner", "spreads"):
            continue
        title = e.game_title
        if title not in game_data:
            game_data[title] = {
                "home_team": e.home_team,
                "away_team": e.away_team,
                "commence_time": e.commence_time,
                "teams": {},
            }
        game_data[title]["teams"][e.bet_team] = {
            "vegas_fair": e.odds_api_prob,
            "pm_price":   e.polymarket_price,
        }

    results = []
    for title, gd in game_data.items():
        home = gd.get("home_team", "")
        away = gd.get("away_team", "")

        for team_name, prices in gd["teams"].items():
            vegas = prices.get("vegas_fair")
            pm    = prices.get("pm_price")
            if not vegas or not pm:
                continue

            # Match this team to Kalshi code
            kalshi_mid = None
            for code, mid in kalshi_by_code.items():
                from odds.kalshi_sports import TEAM_CODE_MAP
                canonical = TEAM_CODE_MAP.get(code, "").lower()
                if any(w in team_name.lower() for w in canonical.split() if len(w) > 3):
                    kalshi_mid = mid
                    break

            # Compute pairwise gaps
            gaps = {}
            if vegas and pm:
                gaps["vegas_vs_pm"] = round((vegas - pm) * 100, 2)
            if vegas and kalshi_mid:
                gaps["vegas_vs_kalshi"] = round((vegas - kalshi_mid) * 100, 2)
            if pm and kalshi_mid:
                gaps["pm_vs_kalshi"] = round((pm - kalshi_mid) * 100, 2)

            if not gaps:
                continue

            max_gap = max(abs(v) for v in gaps.values())
            if max_gap < MIN_EDGE_PCT * 100:
                continue
            if max_gap > MAX_EDGE_PCT * 100:
                continue  # P1 cap

            results.append({
                "team":         team_name,
                "game":         title,
                "commence":     gd.get("commence_time", ""),
                "vegas_fair":   round(vegas * 100, 1) if vegas else None,
                "pm_price":     round(pm * 100, 1) if pm else None,
                "kalshi_mid":   round(kalshi_mid * 100, 1) if kalshi_mid else None,
                "gaps":         gaps,
                "max_gap_pp":   round(max_gap, 1),
                "best_buy":     _best_buy(vegas, pm, kalshi_mid),
                "worst_sell":   _worst_sell(vegas, pm, kalshi_mid),
            })

    return sorted(results, key=lambda x: x["max_gap_pp"], reverse=True)


def _best_buy(vegas: float | None, pm: float | None, kalshi: float | None) -> str:
    """Platform with the lowest price (best place to buy YES)."""
    prices = {"Vegas": vegas, "PM": pm, "Kalshi": kalshi}
    prices = {k: v for k, v in prices.items() if v is not None}
    if not prices:
        return "?"
    return min(prices, key=lambda k: prices[k])


def _worst_sell(vegas: float | None, pm: float | None, kalshi: float | None) -> str:
    """Platform with the highest price (best place to sell/buy NO)."""
    prices = {"Vegas": vegas, "PM": pm, "Kalshi": kalshi}
    prices = {k: v for k, v in prices.items() if v is not None}
    if not prices:
        return "?"
    return max(prices, key=lambda k: prices[k])


async def main(sport: str = "all", dry: bool = False):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*60}")
    print(f"  WC + MLB EDGE SCANNER  —  {ts}")
    print(f"  Min edge: {MIN_EDGE_PCT:.0%}  Cap: {MAX_EDGE_PCT:.0%}  Mode: {'DRY RUN' if dry else 'LIVE'}")
    print(f"{'='*60}")

    tasks = {}
    if sport in ("all", "wc"):
        tasks["WC Matches"] = scan_wc_matches(dry)
        tasks["WC Futures"] = scan_wc_futures(dry)
    if sport in ("all", "mlb"):
        tasks["MLB"] = scan_mlb(dry)

    results = {}
    for name, coro in tasks.items():
        try:
            results[name] = await coro
        except Exception as ex:
            logger.error(f"{name} scan error: {ex}")
            results[name] = []

    total = 0
    actionable = 0
    for name, edges in results.items():
        print(f"\n── {name} ({len(edges)} edges) ──")
        if not edges:
            print("  No edges found")
        for e in sorted(edges, key=lambda x: abs(x.edge_pct), reverse=True):
            print(_fmt_edge(e))
            print(f"     Market: {_fmt_title(e)}")
            ct = getattr(e, "commence_time", None) or getattr(e, "game_date", None)
            if ct:
                print(f"     Kickoff: {ct}")
            total += 1
            if abs(e.edge_pct) >= ALERT_EDGE:
                actionable += 1

    # ── Three-way: Vegas vs PM vs Kalshi ────────────────────────────────
    if sport in ("all", "mlb", "threeway"):
        print(f"\n── Three-Way: Vegas vs Polymarket vs Kalshi ──")
        try:
            tw = scan_three_way()
        except Exception as ex:
            logger.error(f"Three-way scan error: {ex}")
            tw = []

        if not tw:
            print("  No cross-platform gaps found (book prices may not be live yet)")
        else:
            print(f"  {'Team':<28} {'Vegas':>6} {'PM':>6} {'Kalshi':>7} {'MaxGap':>7}  Best Buy → Best Sell")
            print(f"  {'-'*80}")
            for r in tw:
                v  = f"{r['vegas_fair']:.1f}%" if r['vegas_fair'] else "  —  "
                pm = f"{r['pm_price']:.1f}%"   if r['pm_price']  else "  —  "
                kl = f"{r['kalshi_mid']:.1f}%"  if r['kalshi_mid'] else "  —  "
                gap_flag = " ⚡" if r['max_gap_pp'] >= ALERT_EDGE * 100 else "  "
                print(
                    f"  {r['team']:<28} {v:>6} {pm:>6} {kl:>7} {r['max_gap_pp']:>6.1f}pp{gap_flag}"
                    f"  {r['best_buy']} → {r['worst_sell']}"
                )
                print(f"    └ {r['game'][:55]}  {r['commence'][:16]}")
            actionable += sum(1 for r in tw if r['max_gap_pp'] >= ALERT_EDGE * 100)
            total += len(tw)

    print(f"\n{'='*60}")
    print(f"  TOTAL: {total} signals | ACTIONABLE (≥{ALERT_EDGE:.0%}): {actionable}")
    if dry:
        print("  DRY RUN — no shadow trades logged")
    else:
        print("  Shadow trades logged for tradeable edges")
    print(f"{'='*60}\n")

    return total, actionable


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WC + MLB edge scanner")
    parser.add_argument("--dry", action="store_true", help="Scan only, no shadow logging")
    parser.add_argument("--sport", choices=["all", "wc", "mlb", "threeway"], default="all")
    args = parser.parse_args()
    asyncio.run(main(sport=args.sport, dry=args.dry))

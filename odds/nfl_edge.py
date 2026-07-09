#!/usr/bin/env python3
"""nfl_edge.py — NFL game-level edge detection engine.

Phase 5 of Cross-Sport Edge Methodology Upgrade.

Uses the same shared infrastructure as baseball/soccer/UFC:
  - sports_edge_common (consensus devig, edge dataclass, shadow logging)
  - book_weights (NFL-specific Pinnacle/DK/FD weights)
  - price_movement (snapshot logging)
  - odds_api_fetch (credit-gated fetching)

NFL is a 2-way market (spread is the dominant market, but moneyline + totals
are also traded). Polymarket has NFL game markets during season.

Target: ready by Aug 2026, NFL season opens Sep 2026.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

from loguru import logger

try:
    from . import sports_edge_common as sec
    from .odds_api_fetch import get_games_with_markets, upcoming_window
except ImportError:
    import sports_edge_common as sec
    from odds_api_fetch import get_games_with_markets, upcoming_window


NFL_SPORT_KEY = "americanfootball_nfl"


def _nfl_weights():
    try:
        from odds.book_weights import get_weights
        return get_weights("nfl")
    except ImportError:
        return None


CFG = sec.SportConfig(
    name="nfl",
    odds_api_sport_keys=[NFL_SPORT_KEY],
    polymarket_tag="nfl",
    market_model="2way",
    featured_markets=["h2h", "spreads", "totals"],
    shadow_strategy="nfl_moneyline",
    archetype="sports_single_game",
    min_minutes_to_start=60,
)


# ── NFL team aliases (Odds API → Polymarket matching) ─────────────
NFL_TEAM_ALIASES: Dict[str, List[str]] = {
    "Arizona Cardinals": ["Arizona Cardinals", "Cardinals"],
    "Atlanta Falcons": ["Atlanta Falcons", "Falcons"],
    "Baltimore Ravens": ["Baltimore Ravens", "Ravens"],
    "Buffalo Bills": ["Buffalo Bills", "Bills"],
    "Carolina Panthers": ["Carolina Panthers", "Panthers"],
    "Chicago Bears": ["Chicago Bears", "Bears"],
    "Cincinnati Bengals": ["Cincinnati Bengals", "Bengals"],
    "Cleveland Browns": ["Cleveland Browns", "Browns"],
    "Dallas Cowboys": ["Dallas Cowboys", "Cowboys"],
    "Denver Broncos": ["Denver Broncos", "Broncos"],
    "Detroit Lions": ["Detroit Lions", "Lions"],
    "Green Bay Packers": ["Green Bay Packers", "Packers"],
    "Houston Texans": ["Houston Texans", "Texans"],
    "Indianapolis Colts": ["Indianapolis Colts", "Colts"],
    "Jacksonville Jaguars": ["Jacksonville Jaguars", "Jaguars"],
    "Kansas City Chiefs": ["Kansas City Chiefs", "Chiefs"],
    "Las Vegas Raiders": ["Las Vegas Raiders", "Raiders"],
    "Los Angeles Chargers": ["Los Angeles Chargers", "Chargers", "LA Chargers"],
    "Los Angeles Rams": ["Los Angeles Rams", "Rams", "LA Rams"],
    "Miami Dolphins": ["Miami Dolphins", "Dolphins"],
    "Minnesota Vikings": ["Minnesota Vikings", "Vikings"],
    "New England Patriots": ["New England Patriots", "Patriots"],
    "New Orleans Saints": ["New Orleans Saints", "Saints"],
    "New York Giants": ["New York Giants", "Giants", "NY Giants"],
    "New York Jets": ["New York Jets", "Jets", "NY Jets"],
    "Philadelphia Eagles": ["Philadelphia Eagles", "Eagles"],
    "Pittsburgh Steelers": ["Pittsburgh Steelers", "Steelers"],
    "San Francisco 49ers": ["San Francisco 49ers", "49ers"],
    "Seattle Seahawks": ["Seattle Seahawks", "Seahawks"],
    "Tampa Bay Buccaneers": ["Tampa Bay Buccaneers", "Buccaneers", "Bucs"],
    "Tennessee Titans": ["Tennessee Titans", "Titans"],
    "Washington Commanders": ["Washington Commanders", "Commanders"],
}


def compute_nfl_edges(game: Dict, ev: Dict, min_edge: float = 0.03) -> List[sec.Edge]:
    """Pure (no network): weighted consensus devig for NFL game edges."""
    home = game.get("home_team", "")
    away = game.get("away_team", "")
    commence = game.get("commence_time", "")
    _w = _nfl_weights()

    edges: List[sec.Edge] = []

    # ── MONEYLINE (2-way) ──────────────────────────────────────
    true = sec.consensus_devig_2way(game, "h2h", weights=_w)
    if len(true) >= 2:
        best = sec.consensus_best_odds(game, "h2h", weights=_w)
        for team, true_prob in true.items():
            american_odds = best.get(team, 0)
            # Find Polymarket price for this team
            poly_price = 0.0
            ml_market_id = None
            event_title = ev.get("title", "") if ev else f"{away} vs. {home}"
            # Try matching event → market
            if ev:
                for m in ev.get("markets", []):
                    q = m.get("question", "")
                    if sec._name_in(q, team, NFL_TEAM_ALIASES):
                        p0 = sec.price0(m)
                        if sec.VALID_PRICE(p0):
                            poly_price = p0
                            ml_market_id = m.get("id")
                            break

            if poly_price > 0:
                edge = true_prob - poly_price
                if abs(edge) >= min_edge:
                    edges.append(sec.Edge(
                        event_title=event_title,
                        participant=team,
                        market_type="moneyline",
                        market_model="2way",
                        book_prob=true_prob,
                        american_odds=american_odds,
                        poly_price=poly_price,
                        edge_pct=edge,
                        direction="BUY" if edge > 0 else "SELL",
                        commence_time=commence,
                        poly_market_id=ml_market_id,
                        poly_event_id=ev.get("id") if ev else None,
                    ))

    # ── SPREADS (2-way per point) ──────────────────────────────
    spread_consensus = sec.consensus_devig_spreads(game, weights=_w)
    spread_odds = sec.consensus_best_spread_odds(game, weights=_w)
    for abs_point, team_probs in spread_consensus.items():
        odds_map = spread_odds.get(abs_point, {})
        for team, true_prob in team_probs.items():
            american_odds, point = odds_map.get(team, (0, 0.0))
            # Polymarket spread matching would go here (similar to baseball)
            # For now, edges are logged against consensus only

    # ── TOTALS (Over/Under) ────────────────────────────────────
    total_consensus = sec.consensus_devig_totals(game, weights=_w)
    total_odds = sec.consensus_best_total_odds(game, weights=_w)
    for pt, sides in total_consensus.items():
        odds_pair = total_odds.get(pt, (0, 0))
        # Polymarket total matching would go here

    return edges


async def find_nfl_edges(min_edge: float = 0.03) -> List[sec.Edge]:
    """Full pipeline: fetch Odds API + Polymarket → compute edges → enrich."""
    poly = await sec.fetch_polymarket_events_by_tag_async("nfl")
    cf, ct = upcoming_window(24 * 7)  # next week of games
    raw = await get_games_with_markets(
        NFL_SPORT_KEY, "h2h,spreads,totals", CFG.regions, CFG.bookmakers,
        commence_from=cf, commence_to=ct,
    )
    edges: List[sec.Edge] = []
    for game in raw or []:
        try:
            teams = list(sec.consensus_devig_2way(game, "h2h", weights=_nfl_weights()).keys())
            if len(teams) < 2:
                best = sec.sharp_odds_per_outcome(game, "h2h")
                teams = list(best.keys())
            if len(teams) < 2:
                continue

            ev = sec.match_event_by_participants(teams, poly, NFL_TEAM_ALIASES)
            game_edges = compute_nfl_edges(game, ev, min_edge)
            edges.extend(game_edges)
        except Exception as e:
            logger.debug(f"NFL edge error: {e}")
            continue

    # Enrich executable edges
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=4) as pool:
        for edge in edges:
            if edge.poly_market_id:
                idx = sec.outcome_index_for(
                    next((m for m in (ev or {}).get("markets", [])
                          if m.get("id") == edge.poly_market_id), {}),
                    edge.participant,
                )
                await loop.run_in_executor(pool, sec.enrich_executable_edge, edge, idx)

    # Shadow log tradeable edges
    for edge in edges:
        sec.log_shadow(edge, CFG, days_to_close=7.0)

    return edges


if __name__ == "__main__":
    import json
    result = asyncio.run(find_nfl_edges())
    summary = sec.summarize(result, CFG)
    print(json.dumps(summary, indent=2))

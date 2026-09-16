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
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional
from loguru import logger

try:
    from . import sports_edge_common as sec
    from .odds_api_fetch import get_games_with_markets, upcoming_window
    from . import nfl_strength
    from . import nfl_situational
except ImportError:
    import sports_edge_common as sec
    from odds_api_fetch import get_games_with_markets, upcoming_window
    import nfl_strength
    import nfl_situational


NFL_SPORT_KEY = "americanfootball_nfl"
NFL_PRESEASON_KEY = "americanfootball_nfl_preseason"
NFL_SPORT_KEYS = [NFL_SPORT_KEY, NFL_PRESEASON_KEY]


def _nfl_weights():
    try:
        from odds.book_weights import get_weights
        return get_weights("nfl")
    except ImportError:
        return None


CFG = sec.SportConfig(
    name="nfl",
    odds_api_sport_keys=NFL_SPORT_KEYS,
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
    """Pure (no network): weighted consensus devig for NFL game edges.

    Attaches the team-strength overlay (Elo) to each moneyline edge via
    nfl_strength.strength(). Ratings are cached hourly; if the cache is cold
    this is the only network touch in the pure function (acceptable — it is
    called once per game, not per edge).
    """
    home = game.get("home_team", "")
    away = game.get("away_team", "")
    commence = game.get("commence_time", "")
    _w = _nfl_weights()

    # Situational overlay (rest days, QB, weather) — computed once per game.
    # Cached internally; degrades to neutral if data unavailable.
    situ = None
    try:
        if home and away and commence:
            home_city = nfl_situational.TEAM_CITY.get(home)
            situ = nfl_situational.situational(home, away, commence, home_city)
    except Exception as e:
        logger.debug(f"NFL situational overlay error ({home} vs {away}): {e}")
        situ = None

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
                ev_title = ev.get("title", "")
                for m in ev.get("markets", []):
                    q = m.get("question", "")
                    # Moneyline market question == event title (both team names).
                    # Skip spread/total markets ("Spread: X (-3.5)", "O/U 41.5").
                    if q.strip() != ev_title.strip():
                        continue
                    if sec._name_in(q, team, NFL_TEAM_ALIASES):
                        idx = _nfl_outcome_index(m, team)
                        p = sec.price_at(m, idx)
                        if sec.VALID_PRICE(p):
                            poly_price = p
                            ml_market_id = m.get("id")
                            break

            if poly_price > 0:
                edge = true_prob - poly_price
                if abs(edge) >= min_edge:
                    # Team-strength overlay (Elo) — computed per-edge with the
                    # actual book home prob so strength_agree is meaningful.
                    # Ratings are cached hourly; cheap after first call.
                    ov = None
                    try:
                        if home and away:
                            book_home = true_prob if team == home else (1.0 - true_prob)
                            s = nfl_strength.strength(book_home, home, away)
                            # Neutral (no season data / preseason): no overlay.
                            if s.get("strength_edge_pct") is None:
                                ov = None
                            else:
                                # strength_edge_pct is signed vs home; flip for away.
                                sign = 1.0 if team == home else -1.0
                                ov = {
                                    "elo_home": s["elo_home"],
                                    "elo_away": s["elo_away"],
                                    "strength_home_prob": s["strength_home_prob"],
                                    "strength_edge_pct": round(s["strength_edge_pct"] * sign, 4),
                                    "strength_agree": s["strength_agree"],
                                    "strength_confidence": s["strength_confidence"],
                                }
                                # Merge situational overlay (per-team signed)
                                if situ:
                                    s_sign = 1.0 if team == home else -1.0
                                    ov["situational_edge_pct"] = round(
                                        (situ.get("situational_edge_pct") or 0.0) * s_sign, 4)
                                    ov["home_rest_days"] = situ.get("home_rest_days")
                                    ov["away_rest_days"] = situ.get("away_rest_days")
                                    ov["home_qb"] = situ.get("home_qb")
                                    ov["away_qb"] = situ.get("away_qb")
                    except Exception as e:
                        logger.debug(f"NFL strength overlay error ({home} vs {away}): {e}")
                        ov = None
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
                        home_team=home,
                        away_team=away,
                        **({k: v for k, v in ov.items() if v is not None} if ov else {}),
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


def _is_game_event(ev: Dict) -> bool:
    """True only for a single-game moneyline event (not a futures / season-series
    / prop market). tag_slug=nfl returns 450+ events, most of which are futures
    ("Pro Football: 2027 Champion", "Season Series Winner", MVP, draft, etc.).
    Matching a per-game vegas prob against a season-series or futures price is a
    category mismatch that produces garbage signals, so we restrict matching to
    pure game events: short titles like "Falcons vs. Steelers" with a real
    moneyline market.
    """
    title = (ev.get("title") or "").strip()
    if not title:
        return False
    # Futures / series / props carry these markers
    lowered = title.lower()
    for marker in (
        "season series", "champion", "championship", "winner", "mvp",
        "draft", "week 1", "starting qb", "retire", "trade", "sign",
        "playoff", "postseason", "undefeated", "cover athlete",
        "pro football:", "where will", "who will", "will ", "wedding",
        "banned", "cba", "supplemental", "rostered", "leave",
    ):
        if marker in lowered:
            return False
    # Must look like a head-to-head game: exactly two teams around " vs "
    if " vs " not in title and " vs. " not in title:
        return False
    # Must carry a moneyline (h2h) market: outcomes are the two team names,
    # not Over/Under or a spread with a point line.
    for m in ev.get("markets", []) or []:
        outcomes = m.get("outcomes") or []
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except Exception:
                outcomes = []
        if len(outcomes) == 2 and all(
            o not in ("Over", "Under") and "-" not in str(o)
            for o in outcomes
        ):
            return True
    return False


def _nfl_outcome_index(market: Dict, team: str) -> int:
    """Index of `team` in a moneyline market's outcomes, alias-aware.

    Polymarket moneyline outcomes use short names ("Patriots", "49ers") while
    the Odds API uses full names ("New England Patriots"). outcome_index_for()
    does exact-match and falls back to 0, which would give BOTH teams the same
    price. Resolve via the NFL alias table instead.
    """
    raw = market.get("outcomes", "[]")
    arr = json.loads(raw) if isinstance(raw, str) else raw
    aliases = NFL_TEAM_ALIASES.get(team, [team])
    for i, nm in enumerate(arr or []):
        for a in aliases:
            if sec._norm(str(nm)) == sec._norm(a):
                return i
    return 0


async def find_nfl_edges(min_edge: float = 0.03, window_hours: int = 24 * 42) -> List[sec.Edge]:
    """Full pipeline: fetch Odds API + Polymarket → compute edges → enrich.

    window_hours defaults to 42 days (6 weeks) because the NFL season is a
    fixed weekly schedule and the opening slate is listed weeks ahead — a
    7-day window (the generic default) returns zero games in the offseason /
    pre-season gap.
    """
    poly = await sec.fetch_polymarket_events_by_tag_async("nfl")
    # Only pure game events — never futures / season-series / props
    poly = [e for e in poly if _is_game_event(e)]
    cf, ct = upcoming_window(window_hours)
    edges: List[sec.Edge] = []
    # Scan both regular season AND preseason (preseason is a separate Odds API
    # sport key; during Aug the only live/soon NFL games are preseason).
    for sport_key in NFL_SPORT_KEYS:
        raw = await get_games_with_markets(
            sport_key, "h2h,spreads,totals", CFG.regions, CFG.bookmakers,
            commence_from=cf, commence_to=ct,
        )
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
                logger.debug(f"NFL edge error ({sport_key}): {e}")
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


async def get_nfl_edge_summary(min_edge: float = 0.03) -> Dict:
    """NFL edge summary for `/api/vegas/edge` (sport=americanfootball_nfl).

    Mirrors get_baseball_edge_summary() / get_soccer_edge_summary() shape so
    the MCP `polyclawd_vegas_edge` tool returns real game-to-game edges
    instead of the old hardcoded Super-Bowl-season mapping.
    """
    edges = await find_nfl_edges(min_edge=min_edge)
    return sec.summarize(edges, CFG)


if __name__ == "__main__":
    import json
    result = asyncio.run(find_nfl_edges())
    summary = sec.summarize(result, CFG)
    print(json.dumps(summary, indent=2))

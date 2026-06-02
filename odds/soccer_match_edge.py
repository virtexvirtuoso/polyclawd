"""
Soccer per-match 3-way edge engine (NEW — on the shared sports_edge_common core).

Compares Shin-devigged sportsbook h2h (Home/Draw/Away) to Polymarket per-match
events, which expose the 3-way as three binary markets:
  "Will <Home> win on <date>?"  /  "Will <A> vs <B> end in a draw?"  /  "Will <Away> win on <date>?"

World-Cup-first: soccer_fifa_world_cup match h2h lights up June 11.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

from loguru import logger

try:
    from . import sports_edge_common as sec
    from .odds_api_fetch import get_games_with_markets, SOCCER_MATCH_KEYS, upcoming_window
except ImportError:  # pragma: no cover
    import sports_edge_common as sec
    from odds_api_fetch import get_games_with_markets, SOCCER_MATCH_KEYS, upcoming_window

CFG = sec.SportConfig(
    name="soccer_match",
    odds_api_sport_keys=list(SOCCER_MATCH_KEYS.values()),
    polymarket_tag="soccer",
    market_model="3way",
    featured_markets=["h2h"],
    shadow_strategy="soccer_match_3way",
)


def devig_three_way(outcomes: List[Dict]) -> Dict[str, float]:
    """{outcome_name: true_prob} via Shin over Home/Draw/Away."""
    names = [o["name"] for o in outcomes]
    implied = [sec.american_to_implied_prob(int(o["price"])) for o in outcomes]
    return dict(zip(names, sec.devig_shin(implied)))


def map_legs(ev: Dict, home: str, away: str) -> Dict[str, Tuple[str, float, int]]:
    """Map home/draw/away → (condition_id, poly_yes_price, yes_outcome_index)
    from a Polymarket match event's three binary markets (date-suffix stripped)."""
    legs: Dict[str, Tuple[str, float, int]] = {}
    for m in ev.get("markets", []):
        ql = sec.strip_trailing_date(m.get("question", "")).lower()
        cid = m.get("conditionId", m.get("id"))
        idx = sec.outcome_index_for(m, "Yes")
        price = sec.price0(m)
        if "end in a draw" in ql:
            legs["draw"] = (cid, price, idx)
        elif "win" in ql and sec._name_in(ql, home, CFG.team_aliases) and not sec._name_in(ql, away, CFG.team_aliases):
            legs["home"] = (cid, price, idx)
        elif "win" in ql and sec._name_in(ql, away, CFG.team_aliases) and not sec._name_in(ql, home, CFG.team_aliases):
            legs["away"] = (cid, price, idx)
    return legs


def compute_match_edges(game: Dict, ev: Dict, min_edge: float = 0.03) -> List[sec.Edge]:
    """Pure (no network): sharp-book devig + leg mapping → edges (un-enriched)."""
    home, away = game.get("home_team", ""), game.get("away_team", "")
    best = sec.sharp_odds_per_outcome(game, "h2h")
    if len(best) < 3:
        return []
    true = devig_three_way([{"name": n, "price": p} for n, p in best.items()])
    draw_name = next((n for n in true if sec._norm(n) == "draw"), None)
    legs = map_legs(ev, home, away)

    edges: List[sec.Edge] = []
    for key, name in (("home", home), ("away", away), ("draw", draw_name)):
        if name is None or key not in legs:
            continue
        cid, price, idx = legs[key]
        if not sec.VALID_PRICE(price):
            continue
        tp = true.get(name, 0.0)
        edge = tp - price
        if abs(edge) < min_edge:
            continue
        edges.append(
            sec.Edge(
                event_title=ev.get("title", ""),
                participant=name,
                market_type=key,
                market_model="3way",
                book_prob=tp,
                american_odds=int(best.get(name, 0)),
                poly_price=price,
                edge_pct=edge,
                direction="BUY" if edge > 0 else "SELL",
                commence_time=game.get("commence_time", ""),
                poly_market_id=cid,
                poly_event_id=ev.get("id"),
            )
        )
        # stash the matched outcome index for enrichment
        edges[-1].point_value = None
        edges[-1]._oi = idx  # type: ignore[attr-defined]
    return edges


async def find_soccer_match_edges(min_edge: float = 0.03) -> List[sec.Edge]:
    poly = await sec.fetch_polymarket_events_by_tag_async("soccer")
    edges: List[sec.Edge] = []
    # Pull matches in the next 7 days. The credit cost is the same regardless of
    # window (1 unit/league); the window's value is bounding order-book enrichment
    # during the World Cup (104 matches) without dropping near-term tradeable edges.
    cf, ct = upcoming_window(24 * 7)
    raws = await asyncio.gather(
        *[
            get_games_with_markets(k, "h2h", CFG.regions, CFG.bookmakers, commence_from=cf, commence_to=ct)
            for k in CFG.odds_api_sport_keys
        ],
        return_exceptions=True,
    )
    for key, raw in zip(CFG.odds_api_sport_keys, raws):
        if isinstance(raw, Exception):
            logger.warning(f"soccer_match: {key} fetch raised: {raw}")
            continue
        for game in raw or []:
            try:
                home, away = game.get("home_team", ""), game.get("away_team", "")
                if not home or not away or sec.is_stale_event(game.get("commence_time", ""), CFG.min_minutes_to_start):
                    continue
                ev = sec.match_event_by_participants([home, away], poly, CFG.team_aliases)
                if not ev:
                    continue
                edges.extend(compute_match_edges(game, ev, min_edge))
            except Exception as e:
                logger.warning(f"soccer_match: skipped a {key} game: {e}")

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as pool:
        for e in edges:
            await loop.run_in_executor(pool, sec.enrich_executable_edge, e, getattr(e, "_oi", 0))
            if e.tradeable:
                await loop.run_in_executor(pool, sec.log_shadow, e, CFG)
    return edges


async def get_soccer_match_summary() -> Dict:
    return sec.summarize(await find_soccer_match_edges(), CFG)

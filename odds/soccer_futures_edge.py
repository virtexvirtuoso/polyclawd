"""
Soccer futures / outrights edge engine (NEW — replaces the futures role of the
legacy odds/soccer_edge.py, on the shared sports_edge_common core).

Compares Shin-devigged sportsbook outright odds (The Odds API *_winner keys) to
Polymarket binary "Will X win ...?" markets. World-Cup-first.

Cutover note: this supersedes soccer_edge.py's futures logic. Retiring the old
file + rewiring api/routes/markets.py is deferred until the in-flight WIP on
those files is committed/stashed.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List

from loguru import logger

try:
    from . import sports_edge_common as sec
    from .odds_api_fetch import (get_games_with_markets, extract_outright_field,
                                 SOCCER_OUTRIGHT_KEYS)
except ImportError:  # pragma: no cover
    import sports_edge_common as sec
    from odds_api_fetch import (get_games_with_markets, extract_outright_field,
                                SOCCER_OUTRIGHT_KEYS)

# Aliases for World Cup nations where book vs Polymarket naming diverges.
WC_ALIASES: Dict[str, List[str]] = {
    "United States": ["United States", "USA", "US"],
    "Turkey": ["Turkey", "Türkiye"],
    "South Korea": ["South Korea", "Korea Republic"],
    "Ivory Coast": ["Ivory Coast", "Côte d'Ivoire"],
}

WC_CFG = sec.SportConfig(
    name="soccer_futures",
    odds_api_sport_keys=[SOCCER_OUTRIGHT_KEYS["worldcup"]],
    polymarket_tag="world-cup",
    market_model="outright",
    featured_markets=["outrights"],
    team_aliases=WC_ALIASES,
    shadow_strategy="soccer_futures",
)


def edges_from_field(event_title_match: str, odds_field: List[Dict],
                     poly_events: List[Dict], cfg: sec.SportConfig,
                     min_edge: float = 0.03) -> List[sec.Edge]:
    """Pure (no network): Shin-devig the outright field, match each team to a
    Polymarket 'Will X win ...?' binary, emit edges. Executable enrichment is
    applied later by the async caller."""
    if not odds_field:
        return []
    names = [o["name"] for o in odds_field]
    implied = [sec.american_to_implied_prob(int(o["price"])) for o in odds_field]
    true = dict(zip(names, sec.devig_shin(implied)))

    ev = next((e for e in poly_events
               if event_title_match.lower() in e.get("title", "").lower()), None)
    if not ev:
        return []

    edges: List[sec.Edge] = []
    for m in ev.get("markets", []):
        q = m.get("question", "")
        ql = q.lower()
        if "win" not in ql:
            continue
        for name, tp in true.items():
            if not sec._name_in(q, name, cfg.team_aliases):
                continue
            price = sec.price0(m)
            if not sec.VALID_PRICE(price):
                continue
            edge = tp - price
            if abs(edge) < min_edge:
                continue
            american = int(next((o["price"] for o in odds_field if o["name"] == name), 0))
            edges.append(sec.Edge(
                event_title=ev.get("title", ""), participant=name,
                market_type="outright", market_model="outright",
                book_prob=tp, american_odds=american, poly_price=price, edge_pct=edge,
                direction="BUY" if edge > 0 else "SELL", commence_time="",
                point_value=None,
                poly_market_id=m.get("conditionId", m.get("id")),
                poly_event_id=ev.get("id"),
            ))
            break  # one team per market
    return edges


async def find_soccer_futures_edges(min_edge: float = 0.03) -> List[sec.Edge]:
    poly = await sec.fetch_polymarket_events_by_tag_async("world-cup")
    poly += await sec.fetch_polymarket_events_by_tag_async("soccer")
    raw = await get_games_with_markets(WC_CFG.odds_api_sport_keys[0],
                                       markets="outrights", regions="eu,uk",
                                       bookmakers=WC_CFG.bookmakers)
    field = extract_outright_field(raw)
    edges = edges_from_field("World Cup Winner", field, poly, WC_CFG, min_edge)

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as pool:
        for e in edges:
            # outright markets are binary "Will X win"; YES index resolved from the market
            await loop.run_in_executor(pool, sec.enrich_executable_edge, e, 0)
            if e.tradeable:
                await loop.run_in_executor(pool, sec.log_shadow, e, WC_CFG)
    return edges


async def get_soccer_futures_summary() -> Dict:
    return sec.summarize(await find_soccer_futures_edges(), WC_CFG)

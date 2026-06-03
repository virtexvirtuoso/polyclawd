"""
UFC / MMA fight edge engine (NEW — on the shared sports_edge_common core).

Moneyline: Shin/2-way devigged sportsbook h2h (fighter A vs B) vs the Polymarket
fight event's ML market (outcomes = fighter names). Fighters are derived from the
h2h OUTCOMES, not home_team/away_team (which The Odds API leaves empty for MMA).

Method/round props: The Odds API does not reliably expose MMA method-of-victory /
round markets, so Polymarket prop markets are LISTED for manual review
(no_api_line=True) and never auto-edged.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

from loguru import logger

try:
    from . import sports_edge_common as sec
    from .odds_api_fetch import get_games_with_markets, MMA_SPORT_KEYS, upcoming_window
except ImportError:  # pragma: no cover
    import sports_edge_common as sec
    from odds_api_fetch import get_games_with_markets, MMA_SPORT_KEYS, upcoming_window

CFG = sec.SportConfig(
    name="ufc",
    odds_api_sport_keys=[MMA_SPORT_KEYS["ufc"]],
    polymarket_tag="ufc",
    market_model="2way",
    featured_markets=["h2h"],
    shadow_strategy="ufc_moneyline",
)

_PROP_MARKERS = ("KO or TKO", "submission", "Go the Distance", "Rounds")


def ml_market(ev: Dict, fighter: str) -> Optional[Tuple[int, str, float]]:
    """Find the fight's moneyline market (question == event title) and return
    (outcome_index, condition_id, yes_price_for_fighter). None if not found."""
    title = ev.get("title", "")
    for m in ev.get("markets", []):
        if m.get("question", "") != title:
            continue
        idx = sec.outcome_index_for(m, fighter)
        raw = m.get("outcomePrices", "[]")
        import json

        arr = json.loads(raw) if isinstance(raw, str) else raw
        try:
            return idx, m.get("conditionId", m.get("id")), float(arr[idx])
        except (ValueError, TypeError, IndexError):
            return None
    return None


def compute_ufc_edges(fight: Dict, ev: Dict, min_edge: float = 0.03) -> List[sec.Edge]:
    """Pure (no network): weighted consensus 2-way ML edges + prop listings (un-enriched)."""
    # True probabilities from weighted consensus across books.
    # Falls back to single sharp book if consensus has insufficient data.
    true = sec.consensus_devig_2way(fight, "h2h")
    if len(true) < 2:
        best = sec.sharp_odds_per_outcome(fight, "h2h")
        if len(best) < 2:
            return []
        names = list(best.keys())
        pa, pb = sec.devig_two_way(best[names[0]], best[names[1]])
        true = {names[0]: pa, names[1]: pb}
    # Raw odds for display only
    best = sec.consensus_best_odds(fight, "h2h") or sec.sharp_odds_per_outcome(fight, "h2h")

    edges: List[sec.Edge] = []
    for fighter in true:
        mm = ml_market(ev, fighter)
        if not mm:
            continue
        idx, cid, price = mm
        if not sec.VALID_PRICE(price):
            continue
        edge = true[fighter] - price
        if abs(edge) >= min_edge:
            e = sec.Edge(
                event_title=ev.get("title", ""),
                participant=fighter,
                market_type="moneyline",
                market_model="2way",
                book_prob=true[fighter],
                american_odds=int(best.get(fighter, 0)),
                poly_price=price,
                edge_pct=edge,
                direction="BUY" if edge > 0 else "SELL",
                commence_time=fight.get("commence_time", ""),
                poly_market_id=cid,
                poly_event_id=ev.get("id"),
            )
            e._oi = idx  # type: ignore[attr-defined]
            edges.append(e)

    # Prop markets: list for manual review (no Odds API line).
    title = ev.get("title", "")
    for m in ev.get("markets", []):
        q = m.get("question", "")
        if q == title:
            continue
        if any(marker in q for marker in _PROP_MARKERS):
            edges.append(
                sec.Edge(
                    event_title=title,
                    participant=q[:70],
                    market_type="prop",
                    market_model="2way",
                    book_prob=0.0,
                    american_odds=0,
                    poly_price=sec.price0(m),
                    edge_pct=0.0,
                    direction="REVIEW",
                    commence_time=fight.get("commence_time", ""),
                    poly_market_id=m.get("conditionId", m.get("id")),
                    poly_event_id=ev.get("id"),
                    no_api_line=True,
                )
            )
    return edges


async def find_ufc_edges(min_edge: float = 0.03) -> List[sec.Edge]:
    poly = await sec.fetch_polymarket_events_by_tag_async("ufc")
    # Fights happen on weekly cards — pull only the next 7 days of bouts.
    cf, ct = upcoming_window(24 * 7)
    raw = await get_games_with_markets(
        MMA_SPORT_KEYS["ufc"], "h2h", CFG.regions, CFG.bookmakers, commence_from=cf, commence_to=ct
    )
    edges: List[sec.Edge] = []
    for fight in raw or []:
        try:
            # Extract fighter names from any available book
            fighters = sec.consensus_devig_2way(fight, "h2h")
            if len(fighters) < 2:
                best = sec.sharp_odds_per_outcome(fight, "h2h")
                fighters = list(best.keys())
            else:
                fighters = list(fighters.keys())
            if len(fighters) < 2:
                continue
            ev = sec.match_event_by_participants(fighters, poly, CFG.team_aliases)
            if not ev or "fighter b" in ev.get("title", "").lower():  # placeholder guard
                continue
            edges.extend(compute_ufc_edges(fight, ev, min_edge))
        except Exception as e:
            logger.warning(f"ufc: skipped a fight: {e}")

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as pool:
        for e in edges:
            if e.no_api_line:
                continue
            await loop.run_in_executor(pool, sec.enrich_executable_edge, e, getattr(e, "_oi", 0))
            if e.tradeable:
                await loop.run_in_executor(pool, sec.log_shadow, e, CFG)
    return edges


async def get_ufc_summary() -> Dict:
    return sec.summarize(await find_ufc_edges(), CFG)

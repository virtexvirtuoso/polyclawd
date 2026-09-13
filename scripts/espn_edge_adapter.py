#!/usr/bin/env python3
"""espn_edge_adapter.py — ESPN/DK edge dicts → sport_edge_alerts objects.

Bridges odds.espn_odds.find_nfl_us_edges() output (dicts comparing DK
devigged win prob to PM-US full-game-winner mids) into the attribute
objects signals.sport_edge_alerts.run_sport_edge_alerts() consumes.

Depth + executable price come from the PM-US order book (long side walks
offers; short side walks bids at 1 − price), and the edge is recomputed
NET of taker fees against the executable price — not the mid. Only edges
that clear the book (tradeable: slippage ≤ 50bps, fillable ≥ $15) survive.

Origin: 2026-09-12 — Odds API deactivated (billing issue, since Aug 30);
the scheduled NFL edge scan needs a Vegas anchor. ESPN/DK moneylines are
free and already wired (odds/espn_odds.py).
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Dict, List, Optional

from loguru import logger

# Per-scan book cache: both teams of a game share one slug, and the adapter
# sees both sides' edge dicts — cache avoids double-fetching the same book
# (28 calls → 14) and softens gateway 429 pressure during game day.
_BOOK_CACHE: Dict[str, Optional[Dict]] = {}


def _executable_for_edge(slug: str, pm_side: Optional[str]) -> Optional[Dict]:
    """Executable buy info for one edge's team from the PM-US book.

    Long side: buy YES by walking offers. Short side: buy that team's YES
    = sell the long instrument → walk bids at 1 − price (same convention
    as nfl_fast_move_monitor._executable_for_team).
    """
    from signals.nfl_fast_move_monitor import _fetch_us_book, _executable_edge_from_book
    if slug not in _BOOK_CACHE:
        _BOOK_CACHE[slug] = _fetch_us_book(slug)
    book = _BOOK_CACHE[slug]
    if not book:
        return None
    if pm_side == "short":
        ex = _executable_edge_from_book(book, "NO")
        if ex and ex.get("available"):
            ex = dict(ex)
            ex["executable_price"] = round(1.0 - ex["executable_price"], 4)
            ex["best_price"] = round(1.0 - ex["best_price"], 4)
        return ex
    return _executable_edge_from_book(book, "YES")


def espn_edges_to_alerts(edges: List[Dict]) -> List[SimpleNamespace]:
    """Convert edge dicts to alert objects. Per-edge failures are skipped
    (logged at debug), never raised — an adapter bug must not kill the
    scheduled scan task."""
    try:
        from execution.fee_model import taker_fee_fraction
    except Exception:
        taker_fee_fraction = None
    out: List[SimpleNamespace] = []
    for e in edges:
        try:
            ex = _executable_for_edge(e["polymarket_slug"], e.get("pm_side"))
            if not ex or not ex.get("available") or not ex.get("tradeable"):
                continue
            px = float(ex["executable_price"])
            prob = float(e["espn_prob"]) / 100.0
            fee = taker_fee_fraction(px, "polymarket", "sports") if taker_fee_fraction else 0.0
            net = prob - px - fee
            if net <= 0:
                # Overpriced vs DK → the OPPONENT carries the buy edge, and
                # find_nfl_us_edges emits both sides, so nothing is lost.
                # Also avoids a confusing "BUY NO @ 72¢" alert line.
                continue
            out.append(SimpleNamespace(
                event_title=e.get("game") or "",
                event_id=e.get("polymarket_slug") or e.get("game"),
                participant=e.get("team") or "",
                direction="YES",
                executable_edge=round(net, 4),
                executable_price=px,
                fillable_usd=float(ex.get("fillable_usd") or 0.0),
                prob_source=e.get("prob_source"),
            ))
        except Exception as ex_:
            logger.debug(f"espn_edge_adapter: {e.get('game')} / {e.get('team')} failed: {ex_}")
    return out
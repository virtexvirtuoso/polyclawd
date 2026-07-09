"""edge_sizing.py — Sport-agnostic position sizing (PAPER-only).

Generalizes scorer_sizing.py Kelly + correlation-aware sizing for ALL sports.
Accepts sec.Edge objects from any sport engine.

Sizing pipeline (same logic as scorer_sizing, adapted for sec.Edge):
  1. Keep only tradeable edges
  2. Per-leg fractional Kelly from (book_prob, executable_price)
  3. Correlation haircut 1/sqrt(n) within each event cluster
  4. Per-leg cap min(kelly_stake, per_leg_max_stake)
  5. Per-event exposure cap (proportional scale-down)
  6. Daily slate cap (proportional scale-down)

PAPER-mode only. Deterministic, no network calls, no side effects.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

try:
    from odds.sports_edge_common import Edge
except ImportError:
    from sports_edge_common import Edge


@dataclass
class SizingConfig:
    bankroll: float = 10_000.0
    kelly_fraction: float = 0.5         # half-Kelly
    per_event_cap_pct: float = 0.05     # max exposure to one event
    per_leg_max_stake: float = 200.0    # absolute per-leg ceiling
    daily_cap_pct: float = 0.15         # max total daily exposure


@dataclass
class SizedEdge:
    """One sized leg — carries the edge + sizing details."""
    sport: str
    event_title: str
    participant: str
    market_type: str
    direction: str
    book_prob: float
    executable_price: float
    edge_pct: float
    net_edge_pct: Optional[float]
    raw_kelly_fraction: float
    correlation_haircut: float
    stake: float
    binding_constraint: str


def _raw_kelly(p: float, decimal_odds: float) -> float:
    """Standard Kelly f = (b*p - q) / b, clamped at 0."""
    b = decimal_odds - 1.0
    if b <= 0:
        return 0.0
    q = 1.0 - p
    f = (b * p - q) / b
    return max(f, 0.0)


def _exec_to_decimal(exec_price: float, direction: str) -> Optional[float]:
    """Convert Polymarket executable price to decimal odds for the held side."""
    if exec_price is None or exec_price <= 0 or exec_price >= 1:
        return None
    # BUY YES at exec_price → decimal odds = 1 / exec_price
    # BUY NO (SELL YES) at exec_price → held token = NO at (1 - exec_price)
    if direction.upper() in ("BUY", "YES"):
        return 1.0 / exec_price
    else:
        return 1.0 / (1.0 - exec_price)


def size_slate(edges: list[Edge], config: SizingConfig, sport: str = "") -> list[SizedEdge]:
    """Size a slate of sec.Edge objects. Returns SizedEdge per tradeable edge."""
    bankroll = config.bankroll
    per_event_cap = config.per_event_cap_pct * bankroll
    daily_cap = config.daily_cap_pct * bankroll

    # Group tradeable edges by event (correlation cluster)
    clusters: OrderedDict[str, list[Edge]] = OrderedDict()
    for e in edges:
        if not e.tradeable or e.executable_price is None:
            continue
        clusters.setdefault(e.event_title, []).append(e)

    sized: list[SizedEdge] = []

    for _title, legs in clusters.items():
        n = len(legs)
        haircut = 1.0 / math.sqrt(n)

        event_bets: list[SizedEdge] = []
        for e in legs:
            dec = _exec_to_decimal(e.executable_price, e.direction)
            if dec is None:
                continue

            f_raw = _raw_kelly(e.book_prob, dec)
            frac_stake = f_raw * config.kelly_fraction * bankroll * haircut

            if frac_stake <= config.per_leg_max_stake:
                stake = frac_stake
                binding = "kelly_haircut" if n > 1 else "kelly"
            else:
                stake = config.per_leg_max_stake
                binding = "per_leg_cap"

            event_bets.append(SizedEdge(
                sport=sport or "unknown",
                event_title=e.event_title,
                participant=e.participant,
                market_type=e.market_type,
                direction=e.direction,
                book_prob=round(e.book_prob, 4),
                executable_price=round(e.executable_price, 4),
                edge_pct=round(e.edge_pct * 100, 1),
                net_edge_pct=round(e.net_edge_pct * 100, 1) if e.net_edge_pct else None,
                raw_kelly_fraction=round(f_raw, 4),
                correlation_haircut=round(haircut, 4),
                stake=round(stake, 2),
                binding_constraint=binding,
            ))

        # Per-event cap
        event_total = sum(b.stake for b in event_bets)
        if event_total > per_event_cap and event_total > 0:
            scale = per_event_cap / event_total
            for b in event_bets:
                b.stake = round(b.stake * scale, 2)
                b.binding_constraint = "per_event_cap"

        sized.extend(event_bets)

    # Daily cap
    slate_total = sum(b.stake for b in sized)
    if slate_total > daily_cap and slate_total > 0:
        scale = daily_cap / slate_total
        for b in sized:
            b.stake = round(b.stake * scale, 2)
            b.binding_constraint = "daily_cap"

    return sized

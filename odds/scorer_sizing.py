#!/usr/bin/env python3
"""scorer_sizing.py — POSITION SIZING for the goalscorer prop system (PAPER-only).

Spec: 02-Projects/Polyclawd/Development/prop-edge-system-spec.md §6 Build-Order
Step 4 ("position sizing: correlation-aware, per-match cap"). Resolves two
external-review blindspots:

  B14 (CORRELATION). Props *within a match* are NOT independent outcomes — they
  share the same game state (one team blitzing, a red card, a blowout, weather)
  so several scorer YES legs on the same fixture move together. Summing each
  leg's full Kelly stake as if independent over-bets the correlated cluster and
  understates true exposure (the spec's own independent-unit note, §6 line 183:
  "the independent unit is the match, not the prop"). We therefore (a) apply a
  per-leg correlation HAIRCUT of 1/sqrt(n) to the n legs of a match — the
  standard variance-reduction-of-a-correlated-basket shrink that recovers plain
  Kelly at n=1 and shrinks toward an equal-risk split as n grows — and (b)
  enforce a hard per-match exposure cap so a single fixture can never exceed
  `per_match_cap_pct` of bankroll regardless of how many legs it has.

  B17 (STAKE CAPS). On soft books the binding constraint is almost never the
  Kelly fraction — it is the book's max-stake / our own per-book ceiling. So the
  *final* stake on any leg is `min(kelly_stake, per_book_max_stake)`, not the
  Kelly stake. A whole-slate daily cap (`daily_cap_pct`) sits on top as an
  account-survival rail (account-survival layer, §6 Step 5).

SCOPE: sizing math ONLY. PAPER-mode — this module never touches capital, never
executes, never makes a network call, and never imports the live aggregation or
the running logger. It is pure and deterministic: same `ScorerEdge` inputs +
same `SizingConfig` → same output. The input is the `ScorerEdge` dataclass from
`odds/scorer_edge.py`; only edges with `tradeable == True` are sized (speculative
/ unconfirmed-lineup edges get nothing).

Ordering of the caps (each is a clamp, applied in this order so the tightest
constraint always wins):
  1. raw Kelly fraction per leg (from edge + decimal price), × kelly_fraction
  2. × correlation haircut 1/sqrt(n) within each match              [B14]
  3. per-leg ceiling: min(kelly_stake, per_book_max_stake)          [B17]
  4. per-match cap: scale a match's legs down proportionally if their
     sum exceeds per_match_cap_pct × bankroll                       [B14]
  5. daily cap: scale the whole slate down proportionally if the
     grand total exceeds daily_cap_pct × bankroll                   [survival]
"""

from __future__ import annotations

import math
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from typing import Optional

from odds.scorer_edge import ScorerEdge


# ── config ────────────────────────────────────────────────────────────────────
@dataclass
class SizingConfig:
    """PAPER-mode sizing parameters. All caps are *fractions of bankroll* except
    `per_book_max_stake`, which is an absolute currency ceiling per leg (B17)."""

    bankroll: float = 10_000.0
    kelly_fraction: float = 0.5  # half-Kelly — variance/ruin control on a soft edge
    per_match_cap_pct: float = 0.03  # max exposure to one fixture (correlated cluster, B14)
    per_book_max_stake: float = 200.0  # absolute per-leg ceiling — usually binds (B17)
    daily_cap_pct: float = 0.15  # max total slate exposure (account-survival rail)


# ── sized output ────────────────────────────────────────────────────────────────
@dataclass
class SizedBet:
    """One sized leg. Carries the intermediate quantities so the sizing is
    auditable (you can see which cap bound) — PAPER bookkeeping only."""

    event_title: str
    player: str
    commence_time: str  # carried from the edge for downstream resolution timing
    best_soft_book: str
    best_soft_price: float
    decimal_odds: float
    edge_pct: float
    raw_kelly_fraction: float  # full Kelly f, clamped >= 0 (before kelly_fraction)
    fractional_kelly_stake: float  # f * kelly_fraction * bankroll (pre-haircut/caps)
    correlation_haircut: float  # 1/sqrt(n) applied for this leg's match cluster (B14)
    stake: float  # FINAL stake after all caps (B14 + B17 + daily)
    binding_constraint: str  # which clamp set the final stake (for audit)


# ── decimal-odds helper ─────────────────────────────────────────────────────────
def _to_decimal_odds(price) -> Optional[float]:
    """The soft price you'd bet, as decimal odds (b = decimal - 1 for Kelly).
    Mirrors scorer_edge.to_implied auto-detection: |price| >= 100 → american,
    else already decimal. Returns None for unusable prices."""
    if price is None:
        return None
    try:
        v = float(price)
    except (TypeError, ValueError):
        return None
    if abs(v) >= 100:  # american
        iv = int(round(v))
        return 1.0 + (iv / 100.0 if iv > 0 else 100.0 / abs(iv))
    if v <= 1.0:  # invalid decimal odds (must pay out > stake)
        return None
    return v


# ── raw Kelly ─────────────────────────────────────────────────────────────────
def _raw_kelly_fraction(p: float, decimal_odds: float) -> float:
    """Standard Kelly f = (b*p - q) / b, with b = decimal_odds - 1, q = 1 - p.
    Clamped at 0 (never stake a negative/zero edge). `p` is the consensus fair
    YES probability (the edge engine's haircut consensus); the price is the soft
    book's offered decimal odds."""
    b = decimal_odds - 1.0
    if b <= 0:
        return 0.0
    q = 1.0 - p
    f = (b * p - q) / b
    return f if f > 0 else 0.0


# ── slate sizer ─────────────────────────────────────────────────────────────────
def size_slate(edges: list[ScorerEdge], config: SizingConfig) -> list[SizedBet]:
    """Size a full slate of `ScorerEdge` rows into `SizedBet` rows (PAPER).

    Pipeline (see module docstring for the B14/B17 rationale of each step):
      1. keep only tradeable edges (speculative ones get nothing)
      2. per-leg fractional Kelly from (consensus_fair, soft decimal odds)
      3. correlation haircut 1/sqrt(n) within each match cluster      [B14]
      4. per-leg cap min(kelly_stake, per_book_max_stake)             [B17]
      5. per-match exposure cap (proportional scale-down)             [B14]
      6. daily slate cap (proportional scale-down)            [survival rail]

    Deterministic and side-effect-free. Returns one SizedBet per tradeable edge,
    grouped by match (input order preserved within each match).
    """
    bankroll = config.bankroll
    per_match_cap = config.per_match_cap_pct * bankroll
    daily_cap = config.daily_cap_pct * bankroll

    # ── group tradeable edges by match (event_title = correlated cluster, B14) ──
    # OrderedDict keeps first-seen match order stable → deterministic output.
    clusters: "OrderedDict[str, list[ScorerEdge]]" = OrderedDict()
    for e in edges:
        if not e.tradeable:  # speculative / unconfirmed lineup → not sized
            continue
        clusters.setdefault(e.event_title, []).append(e)

    sized: list[SizedBet] = []

    for _title, legs in clusters.items():
        n = len(legs)
        # B14: 1/sqrt(n) shrink — at n=1 it's 1.0 (plain Kelly); n correlated legs
        # are treated as ~1 unit of independent risk shared across them, not n.
        haircut = 1.0 / math.sqrt(n)

        match_bets: list[SizedBet] = []
        for e in legs:
            decimal_odds = _to_decimal_odds(e.best_soft_price)
            if decimal_odds is None:
                continue  # unusable price → cannot size this leg

            f_raw = _raw_kelly_fraction(e.consensus_fair, decimal_odds)
            frac_kelly_stake = f_raw * config.kelly_fraction * bankroll
            haircut_stake = frac_kelly_stake * haircut

            # B17: the per-book max almost always binds before Kelly does.
            if haircut_stake <= config.per_book_max_stake:
                stake = haircut_stake
                binding = "kelly_haircut" if n > 1 else "kelly"
            else:
                stake = config.per_book_max_stake
                binding = "per_book_cap"

            match_bets.append(
                SizedBet(
                    event_title=e.event_title,
                    player=e.player,
                    commence_time=getattr(e, "commence_time", ""),
                    best_soft_book=e.best_soft_book,
                    best_soft_price=e.best_soft_price,
                    decimal_odds=decimal_odds,
                    edge_pct=e.edge_pct,
                    raw_kelly_fraction=f_raw,
                    fractional_kelly_stake=frac_kelly_stake,
                    correlation_haircut=haircut,
                    stake=stake,
                    binding_constraint=binding,
                )
            )

        # B14: hard per-match exposure cap. If the cluster's legs together exceed
        # per_match_cap, scale them ALL down proportionally so the fixture's total
        # risk == the cap (preserves the relative Kelly weighting between legs).
        match_total = sum(b.stake for b in match_bets)
        if match_total > per_match_cap and match_total > 0:
            scale = per_match_cap / match_total
            for b in match_bets:
                b.stake *= scale
                b.binding_constraint = "per_match_cap"

        sized.extend(match_bets)

    # ── daily cap across the whole slate (account-survival rail, §6 Step 5) ──
    # Proportional scale-down preserves every leg's relative size.
    slate_total = sum(b.stake for b in sized)
    if slate_total > daily_cap and slate_total > 0:
        scale = daily_cap / slate_total
        for b in sized:
            b.stake *= scale
            b.binding_constraint = "daily_cap"

    return sized

#!/usr/bin/env python3
"""
prop_clv_shapes.py — outcome-shape handlers for the generalized prop CLV harness.

Spec: 02-Projects/Polyclawd/Development/Prop-CLV-Generalized-Spec.md §3

extract_bets(event_json, PropConfig) -> list[Bet], one Bet per gradable
(participant, market, line) — the side we'd actually bet.

  yes_no       : bet YES. fair = phase0.new_sharp_yes (Betfair-weighted SHIN) * haircut,
                 soft = lowest raw soft YES. EXACTLY reproduces scorer_paper_logger.
  over_under   : per (player, market, line); pick the side (over/under) with max edge
                 vs sharp consensus; one Bet per prop.
  two_way      : h2h moneyline via sports_edge_common.consensus_devig_2way; one Bet per
                 outcome (the gate's edge>=min_edge flag selects the +edge side).

A Bet with edge >= config.min_edge is "selected"; the rest are the control group.
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import phase0_prop_falsification as P  # noqa: E402  (to_implied, new_sharp_yes, devig)

# sports_edge_common lives in odds/ of the full repo; guard so yes_no/over_under
# still work where it's absent (e.g. the self-contained VPS scorer dir).
try:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from odds import sports_edge_common as SEC  # noqa: E402
    _HAVE_SEC = True
except Exception:  # pragma: no cover
    SEC = None
    _HAVE_SEC = False

from prop_clv_config import HAIRCUTS  # noqa: E402


@dataclass(frozen=True)
class Bet:
    participant: str
    market: str
    line: float | None
    side: str
    consensus_fair: float   # 0..1, devigged sharp prob of our side
    soft_book: str
    soft_implied: float     # 0..1, raw soft implied of our side
    n_sharp: int

    @property
    def edge_pct(self) -> float:
        return (self.consensus_fair - self.soft_implied) * 100.0


def extract_bets(ev: dict, config) -> list:
    if isinstance(ev, dict) and "data" in ev and "bookmakers" not in ev:
        ev = ev["data"]
    bets = []
    prop_keys = [m for m, s in config.markets if s in ("yes_no", "over_under")]
    if prop_keys:
        bets += _extract_props(ev, config, prop_keys)
    for mkey, shape in config.markets:
        if shape == "two_way":
            bets += _extract_two_way(ev, config, mkey)
    return bets


# ── prop markets: yes_no + over_under ─────────────────────────────────────────
def _parse_props(ev: dict, market_keys: list):
    """{(participant, market, line): {book: {side: implied}}}  side in yes/no/over/under."""
    out: dict = defaultdict(lambda: defaultdict(dict))
    for bk in ev.get("bookmakers", []):
        bkey = bk.get("key", "")
        for mk in bk.get("markets", []):
            mkey = mk.get("key", "")
            if mkey not in market_keys:
                continue
            for o in mk.get("outcomes", []):
                name = (o.get("name") or "").strip()
                desc = (o.get("description") or "").strip()
                imp = P.to_implied(o.get("price"), "american")
                if imp is None:
                    continue
                low = name.lower()
                point = o.get("point")
                if low in ("over", "under"):
                    participant, side = desc, low
                elif low in ("yes", "no"):
                    participant, side = (desc or name), low
                else:  # yes-implied market (anytime scorer): name=player, side=yes
                    participant, side, point = name, "yes", None
                if not participant:
                    continue
                out[(participant, mkey, point)][bkey][side] = imp
    return out


def _book_devig(sides: dict) -> dict:
    """One book's devigged probs per side. 2-way if both present, else raw."""
    if "over" in sides and "under" in sides:
        po, pu = sides["over"], sides["under"]
        t = po + pu
        return {"over": po / t, "under": pu / t} if t > 0 else {}
    if "yes" in sides and "no" in sides:
        return {"yes": P.devig_shin_yes(sides["yes"], sides["no"]),
                "no": P.devig_shin_yes(sides["no"], sides["yes"])}
    return {k: v for k, v in sides.items()}  # one-sided: raw implied


def _sharp_fair(books: dict, sharp_books: tuple) -> dict:
    """Weighted sharp consensus devigged prob per side."""
    num: dict = defaultdict(float)
    den: dict = defaultdict(float)
    for bkey, w in sharp_books:
        sides = books.get(bkey)
        if not sides:
            continue
        for side, p in _book_devig(sides).items():
            num[side] += w * p
            den[side] += w
    return {s: num[s] / den[s] for s in num if den[s] > 0}


def _soft_best(books: dict, soft_books: frozenset) -> dict:
    """Lowest raw implied (= best price for the buyer) per side across soft books."""
    best: dict = {}
    for bkey, sides in books.items():
        if bkey not in soft_books:
            continue
        for side, imp in sides.items():
            if side not in best or imp < best[side][1]:
                best[side] = (bkey, imp)
    return best


def _extract_props(ev: dict, config, prop_keys: list) -> list:
    parsed = _parse_props(ev, prop_keys)
    bets = []
    for (participant, mkey, line), books in parsed.items():
        shape = config.shape_of(mkey)
        n_sharp = sum(1 for b, _ in config.sharp_books if b in books)
        soft = _soft_best(books, config.soft_books)

        if shape == "yes_no":
            # EXACT scorer reproduction: fair via new_sharp_yes*haircut, soft = raw lowest yes
            fair = P.new_sharp_yes(books)
            if fair is None or "yes" not in soft:
                continue
            fair *= HAIRCUTS.get(mkey, 1.0)
            sb, si = soft["yes"]
            bets.append(Bet(participant, mkey, None, "yes", fair, sb, si, n_sharp))
            continue

        # over_under: pick the side with the max edge (our bet); one Bet per prop
        fair_by_side = _sharp_fair(books, config.sharp_books)
        cands = []
        for side in ("over", "under"):
            if side in fair_by_side and side in soft:
                sb, si = soft[side]
                cands.append(Bet(participant, mkey, line, side, fair_by_side[side], sb, si, n_sharp))
        if cands:
            bets.append(max(cands, key=lambda b: b.edge_pct))
    return bets


# ── two-way h2h (UFC / soccer match moneyline) ────────────────────────────────
def _extract_two_way(ev: dict, config, mkey: str) -> list:
    if not _HAVE_SEC:
        print(f"  [warn] sports_edge_common unavailable — skipping two_way market {mkey}")
        return []
    fair = SEC.consensus_devig_2way(ev, mkey)  # {team: true_prob} weighted consensus
    if not fair:
        return []
    soft: dict = {}
    n_sharp = 0
    for bk in ev.get("bookmakers", []):
        bkey = bk.get("key", "")
        is_sharp = any(bkey == b for b, _ in config.sharp_books)
        for mk in bk.get("markets", []):
            if mk.get("key") != mkey:
                continue
            if is_sharp:
                n_sharp += 1
            if bkey not in config.soft_books:
                continue
            for o in mk.get("outcomes", []):
                if o.get("price") is None:
                    continue
                imp = SEC.american_to_implied_prob(int(o["price"]))
                t = o.get("name")
                if t and (t not in soft or imp < soft[t][1]):
                    soft[t] = (bkey, imp)
    bets = []
    for team, fairp in fair.items():
        if team not in soft:
            continue
        sb, si = soft[team]
        bets.append(Bet(team, mkey, None, team, fairp, sb, si, n_sharp))
    return bets

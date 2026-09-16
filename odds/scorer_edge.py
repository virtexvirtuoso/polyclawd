#!/usr/bin/env python3
"""scorer_edge.py — anytime-goalscorer prop EDGE ENGINE (detection only).

Spec: 02-Projects/Polyclawd/Development/prop-edge-system-spec.md §3 + §4.

WHAT THIS IS
------------
Clean, self-contained edge-DETECTION module for `player_goal_scorer_anytime`
soccer props. It takes ALREADY-FETCHED The-Odds-API event-odds dicts and emits
`ScorerEdge` rows. The validated mechanics (§3) are lifted from the throwaway
scripts (phase0_prop_falsification.py, scorer_paper_logger.py) into clean module
code — this module does NOT import those scripts.

WHAT THIS IS NOT
----------------
Position sizing, Kelly, execution, the SQLite logger, and the live API fetch are
intentionally OUT of scope (gated in the spec until CLV is proven). The fetch is
INJECTABLE so callers/tests pass synthetic data and burn no API credits.

Core mechanics (§3):
  • Consensus anchor (§3.1) = Betfair-weighted mean of each sharp book's RAW YES
    implied prob. Degrades gracefully when a sharp book is absent (Pinnacle is
    missing from ~20% of matches — never hard-required).
  • Single-sided de-vig (§3.2, RESOLVED): a FLAT haircut on the consensus fair
    only. The soft entry price stays RAW. Data is YES-only — there is no NO side,
    so no two-way devig is attempted.
  • Lineup gate (§4.3): an optional injected `lineup_checker`. tradeable only when
    the player is CONFIRMED starting AND the match is inside the tradeable window.
    Unchecked (None) or benched (False) → speculative, shown-not-tradeable.
    Edges flagged >1h out are speculative, not tradeable.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

SCORER_MARKET = "player_goal_scorer_anytime"

# ── §3.1 Sharp consensus weights (corrected 2026-06-30) ──────────────────────
# betfair_ex_uk + betfair_ex_eu are the SAME exchange liquidity pool.
# Old weights (0.40+0.40) gave Betfair 8x Pinnacle → wildly inflated fair values.
# Corrected: merge Betfair to single 0.50 weight, raise Pinnacle, add WH.
SOCCER_PROP_SHARP_WEIGHTS: dict = {
    "betfair_ex_uk": 0.25,  # ─┐ same pool — together = 0.50
    "betfair_ex_eu": 0.25,  # ─┘
    "pinnacle": 0.35,        # genuine sharp; absent ~20% of matches
    "williamhill": 0.15,     # best European fixed-odds anchor
}
# GUARD: betfair combined weight must never exceed 0.55 (old bug was 0.80)
assert SOCCER_PROP_SHARP_WEIGHTS.get("betfair_ex_uk", 0) + SOCCER_PROP_SHARP_WEIGHTS.get("betfair_ex_eu", 0) <= 0.55, \
    "SCORER BUG: Betfair double-count — betfair_ex_uk + betfair_ex_eu combined weight > 0.55 (regression from 2026-07-08)"

SOFT_BOOKS: tuple = ("draftkings", "fanduel", "betrivers", "onexbet", "skybet")

# ── §3.2 single-sided de-vig: flat haircut on the consensus fair only ─────────
# Goalscorer vig is empirically ~4.4% and FLAT across probability buckets (n=48,
# Pinnacle two-way), so a flat factor de-vigs as well as Shin without a NO side.
# The raw consensus carries a ~+1.2pp positive bias that would flow into fake
# edge; this removes it. PROVISIONAL — refit on resolution data. Applies to the
# fair-value anchor ONLY; the soft entry price stays RAW.
GOALSCORER_YES_HAIRCUT = 0.958

# Outcome `name` values that mark a side rather than a player.
_SIDE_NAMES = {"yes", "no", "over", "under"}


# ── price handling (§3): american OR decimal → raw implied prob ───────────────
def american_to_implied_prob(odds: int) -> float:
    """Raw implied probability (vig included) of an American-format price."""
    odds = int(odds)
    return (100.0 / (odds + 100.0)) if odds > 0 else (abs(odds) / (abs(odds) + 100.0))


def to_implied(price, odds_format: str = "auto") -> Optional[float]:
    """Convert a book price to raw implied prob.

    The Odds API is queried with oddsFormat=american, so American is the norm.
    Auto-detect: abs(price) >= 100 → american, else treat as decimal (1/price).
    Returns None for unusable prices.
    """
    if price is None:
        return None
    try:
        v = float(price)
    except (TypeError, ValueError):
        return None
    fmt = odds_format
    if fmt == "auto":
        fmt = "american" if abs(v) >= 100 else "decimal"
    if fmt == "american":
        return american_to_implied_prob(int(round(v)))
    if v <= 1.0:  # invalid decimal odds
        return None
    return 1.0 / v


# ── canonical player name (§4.2) ──────────────────────────────────────────────
def _canonical_player(name: str) -> str:
    """Canonicalize a player name: strip accents (NFKD), lowercase, drop
    punctuation, and collapse an initial form ("E. Haaland") to the surname."""
    name = unicodedata.normalize("NFKD", name or "")
    name = "".join(c for c in name if not unicodedata.combining(c)).lower().strip()
    name = re.sub(r"[^\w\s]", "", name)
    parts = name.split()
    # "x last" (single-letter initial + surname) → "last"
    return parts[1] if len(parts) == 2 and len(parts[0]) == 1 else name


# ── config (§4.1) ─────────────────────────────────────────────────────────────
@dataclass
class ScorerSportConfig:
    sport_key: str  # "soccer_fifa_world_cup", "soccer_epl"
    market: str = SCORER_MARKET
    sharp_weights: dict = field(default_factory=lambda: dict(SOCCER_PROP_SHARP_WEIGHTS))
    soft_books: tuple = SOFT_BOOKS
    regions: str = "us,uk,eu"  # must include uk/eu for Betfair exchange
    min_edge_pp: float = 5.0  # placeholder — recalibrate on CLV, not realized W/L
    max_edge_pp: float = 25.0  # cap — residual Betfair mismatch creates phantom 40pp+ edges
    kickoff_window_hours: int = 6  # FETCH window (cost gate), NOT tradeable window
    tradeable_window_hours: float = 1.0  # only post-lineup edges are tradeable (§4.3)
    prop_ttl_seconds: int = 1800


# ── edge row (§4.2) ───────────────────────────────────────────────────────────
@dataclass
class ScorerEdge:
    sport_key: str
    event_title: str
    commence_time: str
    player: str  # canonical form (_canonical_player)
    consensus_fair: float  # Betfair-weighted consensus YES, haircut applied (§3.1/§3.2)
    best_soft_book: str
    best_soft_price: float  # the price you'd bet (american or decimal as supplied)
    best_soft_implied: float  # RAW (vig included) — soft price is never haircut
    edge_pct: float  # (consensus_fair - best_soft_implied) * 100, in pp
    player_confirmed_starting: Optional[bool] = None  # None=unchecked, True, False
    tradeable: bool = False  # True only if not-benched AND within tradeable_window
    # CLV (filled by a close snapshot, §5) — not computed here.
    soft_close_implied: Optional[float] = None
    clv_soft_move_pp: Optional[float] = None


# ── lineup gate default (§4.3) ────────────────────────────────────────────────
def _lineup_stub(player: str, event: dict) -> Optional[bool]:  # noqa: ARG001
    """Default lineup checker: unchecked. Returns None so edges show as
    speculative (player_confirmed_starting=None) rather than benched."""
    return None


# ── internal parse: one event → {player_canon: {book: raw_yes_implied}} ───────
def _parse_event(ev: dict, config: ScorerSportConfig):
    """Parse a The-Odds-API event-odds dict into raw YES implied probs keyed by
    canonical player then book. Goalscorer data is YES-only: each scorer outcome
    has name="Yes" + description=<player>. Tolerates a raw historical wrapper."""
    if isinstance(ev, dict) and "data" in ev and "bookmakers" not in ev:
        ev = ev["data"]
    home = ev.get("home_team", "?")
    away = ev.get("away_team", "?")
    title = f"{home} vs {away}"
    commence = ev.get("commence_time", "")

    # canonical_player -> raw_player_display
    raw_name: dict = {}
    # canonical_player -> {book_key: (raw_yes_implied, raw_price)}
    books: dict = defaultdict(dict)

    for bk in ev.get("bookmakers", []):
        bkey = bk.get("key", "")
        for mk in bk.get("markets", []):
            if mk.get("key", "") != config.market:
                continue
            for out in mk.get("outcomes", []):
                name = (out.get("name") or "").strip()
                desc = (out.get("description") or "").strip()
                low = name.lower()
                if low in _SIDE_NAMES:
                    if low not in ("yes", "over") or not desc:
                        # NO/Under side, or a malformed YES with no player → skip.
                        continue
                    player_disp = desc  # YES side: player is in description
                else:
                    # name = player directly (yes-implied market, no side label)
                    player_disp = name
                price = out.get("price")
                imp = to_implied(price, "auto")
                if imp is None:
                    continue
                pc = _canonical_player(player_disp)
                if not pc:
                    continue
                raw_name.setdefault(pc, player_disp)
                books[pc][bkey] = (imp, price)

    return title, commence, raw_name, books


# ── §3.1 consensus anchor ─────────────────────────────────────────────────────
def _consensus_fair(book_yes: dict, weights: dict) -> Optional[float]:
    """Betfair-weighted mean of each sharp book's RAW YES implied prob.
    Degrades gracefully — only books actually present contribute; weights are
    renormalized over the present books. Returns None if no sharp book is present.
    (Pinnacle absent ~20% of matches → never hard-required.)

    `book_yes` maps book_key -> (raw_implied, raw_price)."""
    num = den = 0.0
    for bkey, w in weights.items():
        v = book_yes.get(bkey)
        if v is None:
            continue
        num += w * v[0]
        den += w
    return (num / den) if den > 0 else None


# ── best soft (lowest raw implied = best price for the buyer) ─────────────────
def _best_soft(book_yes: dict, soft_books) -> Optional[tuple]:
    """Lowest RAW implied YES across the soft books → (book_key, raw_implied,
    raw_price). `book_yes` maps book_key -> (raw_implied, raw_price)."""
    best = None
    for bkey, v in book_yes.items():
        if bkey not in soft_books:
            continue
        imp = v[0]
        if best is None or imp < best[1]:
            best = (bkey, imp, v[1])
    return best


def _commence_dt(commence_time: str) -> Optional[datetime]:
    if not commence_time:
        return None
    try:
        dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── §4.3 algorithm ────────────────────────────────────────────────────────────
def find_scorer_edges(
    events_odds: list[dict],
    config: ScorerSportConfig,
    lineup_checker: Optional[Callable[[str, dict], Optional[bool]]] = None,
    now: Optional[datetime] = None,
    started_buffer_minutes: float = 0.0,
) -> list[ScorerEdge]:
    """Compute goalscorer edges from ALREADY-FETCHED event-odds dicts.

    Args:
        events_odds: list of The-Odds-API /events/{id}/odds shaped dicts
            (top-level home_team/away_team/commence_time; bookmakers[].markets[]
            .outcomes[] where a scorer outcome carries name="Yes" + description=
            <player> + price). NO live fetch happens here — the fetch is the
            caller's job, keeping tests credit-free.
        config: ScorerSportConfig.
        lineup_checker: optional (player_canonical, event_dict) -> Optional[bool]
            (None=unchecked, True=confirmed starting, False=confirmed benched).
            Defaults to `_lineup_stub` (always None / unchecked).
        now: injectable "current time" (UTC). Defaults to datetime.now(UTC).
        started_buffer_minutes: treat matches kicking off within this many minutes
            (or already started) as in-play poison and skip them.

    Returns:
        list[ScorerEdge] with edge_pct >= config.min_edge_pp, one per player.
    """
    if lineup_checker is None:
        lineup_checker = _lineup_stub
    if now is None:
        now = datetime.now(timezone.utc)

    out: list[ScorerEdge] = []
    for ev in events_odds:
        title, commence, raw_name, books = _parse_event(ev, config)

        # Skip in-play/started — commence_time <= now + buffer (in-play poison).
        cdt = _commence_dt(commence)
        if cdt is not None:
            mins_to_kick = (cdt - now).total_seconds() / 60.0
            if mins_to_kick <= started_buffer_minutes:
                continue
            within_tradeable = mins_to_kick <= config.tradeable_window_hours * 60.0
        else:
            mins_to_kick = None
            within_tradeable = False  # no time → cannot confirm tradeable window

        for pc, book_yes in books.items():
            fair = _consensus_fair(book_yes, config.sharp_weights)
            if fair is None:  # no sharp book present → skip (§4.3 step 4)
                continue
            fair *= GOALSCORER_YES_HAIRCUT  # §3.2 — haircut the anchor, not the soft

            soft = _best_soft(book_yes, config.soft_books)
            if soft is None:
                continue
            soft_book, soft_implied, soft_price = soft

            edge_pct = (fair - soft_implied) * 100.0
            if edge_pct < config.min_edge_pp:
                continue
            if edge_pct > config.max_edge_pp:
                continue

            confirmed = lineup_checker(pc, ev)
            # tradeable ONLY when the player is CONFIRMED starting AND the match is
            # inside the tradeable window. An unchecked (None) or benched (False)
            # lineup leaves the edge speculative/shown-not-tradeable (§3.4, §4.3:
            # "Edges flagged >1h out are speculative, not tradeable"; confirmed XI
            # publishes ~60 min pre-kickoff). This is stricter than the §4.2
            # shorthand `confirmed is not False` — a scorer prop on an unconfirmed
            # player is worthless if benched, so None must not be tradeable.
            tradeable = (confirmed is True) and within_tradeable

            out.append(
                ScorerEdge(
                    sport_key=config.sport_key,
                    event_title=title,
                    commence_time=commence,
                    player=pc,
                    consensus_fair=fair,
                    best_soft_book=soft_book,
                    best_soft_price=soft_price,
                    best_soft_implied=soft_implied,
                    edge_pct=edge_pct,
                    player_confirmed_starting=confirmed,
                    tradeable=tradeable,
                )
            )
    return out

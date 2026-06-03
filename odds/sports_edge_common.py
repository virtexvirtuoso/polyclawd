"""
Generic, sport-agnostic edge core for Polyclawd sports engines.

The proven baseball logic (devig, sharp-book selection, Polymarket matching,
executable-edge enrichment, shadow logging) lifted out and parameterized by a
`SportConfig`, so soccer / UFC / (later) NFL / NBA engines supply ONLY their
Polymarket market-shape mapper.

Design rules baked in (from the 2026-06-02 multi-discipline review):
  - Sharp-book reference (Pinnacle) — NOT best-of-all-books (which invents edge).
  - Shin devig for 3-way / outright fields (proportional under-prices favorites).
  - Executable-edge enrichment uses the matched outcome's index (never hardcoded 0).
  - Shadow logging is gated on `tradeable` and logs the EXECUTABLE price, fee-adjusted.
  - Unicode-normalized, alias-aware participant matching; resolved-market price guards.

I/O-bound helpers import their heavy deps (poly_executable_edge, shadow_tracker)
lazily inside the function so this module stays import-light for unit tests.
"""
from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests
from loguru import logger

POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"
POLY_WINNER_FEE = 0.02  # Polymarket charges ~2% on winnings; edge must clear it.

# Sharp books in preference order. Pinnacle first (sharpest); exchanges next.
SHARP_BOOKS: Tuple[str, ...] = ("pinnacle", "betfair_ex_eu", "betfair_ex_uk", "williamhill")

# ─── Weighted Consensus Devig ───────────────────────────────
# Per-book devig → weighted average across books. Same approach as baseball_edge.py
# to avoid the best-of-all vig collapse artifact.
BOOK_WEIGHTS: Dict[str, float] = {
    "pinnacle":        0.35,
    "betfair_ex_uk":   0.30,
    "betfair_ex_eu":   0.30,
    "draftkings":      0.20,
    "fanduel":         0.15,
    "betmgm":          0.10,
    "betrivers":       0.05,
    "caesars":         0.05,
    "williamhill_us":  0.05,
    "williamhill":     0.05,
    "bovada":          0.02,
}
# Comma-separated list for The Odds API `bookmakers` param (overrides regions,
# still costs 1 credit unit). Kept in sync with BOOK_WEIGHTS keys.
CONSENSUS_BOOKMAKERS = ",".join(BOOK_WEIGHTS.keys())

# Reject Polymarket prices that indicate an effectively-resolved/illiquid market.
def VALID_PRICE(p: float) -> bool:
    return 0.02 <= p <= 0.98


# ─────────────────────────────────────────────────────────────────────
# American odds / devig
# ─────────────────────────────────────────────────────────────────────
def american_to_implied_prob(odds: int) -> float:
    """Standard moneyline → implied probability (with vig)."""
    odds = int(odds)
    return (100.0 / (odds + 100.0)) if odds > 0 else (abs(odds) / (abs(odds) + 100.0))


def devig_two_way(odds_a: int, odds_b: int) -> Tuple[float, float]:
    """Proportional devig of a clean 2-way market → (pa, pb) summing to 1."""
    pa, pb = american_to_implied_prob(odds_a), american_to_implied_prob(odds_b)
    t = pa + pb
    return pa / t, pb / t


def devig_multiway(probs: List[float]) -> List[float]:
    """Proportional devig (normalize to sum 1). Kept for callers that want it."""
    t = sum(probs)
    return [p / t for p in probs] if t > 0 else list(probs)


def devig_shin(implied: List[float], iters: int = 50) -> List[float]:
    """Shin (1992/1993) devig: Newton-solve for insider proportion z, return true
    probs summing to 1. Favors the favorite more than proportional normalization.
    Degenerate inputs fall back to proportional."""
    s = sum(implied)
    if s <= 0 or len(implied) < 2:
        return [p / s for p in implied] if s > 0 else list(implied)
    n = len(implied)
    z = 0.0
    for _ in range(iters):
        roots = [(z * z + 4 * (1 - z) * (p * p) / s) ** 0.5 for p in implied]
        f = sum(roots) - 2.0 - z * (n - 2.0)
        df = sum(((2 * z - 4 * (p * p) / s) / (2 * r) if r > 0 else 0.0)
                 for p, r in zip(implied, roots)) - (n - 2.0)
        if abs(df) < 1e-12:
            break
        z = min(0.999, max(0.0, z - f / df))
    true = [(((z * z + 4 * (1 - z) * (p * p) / s) ** 0.5) - z) / (2 * (1 - z)) if z < 1 else p / s
            for p in implied]
    t = sum(true)
    return [x / t for x in true] if t > 0 else [p / s for p in implied]


def consensus_devig_2way(game: Dict, market_key: str = "h2h") -> Dict[str, float]:
    """Per-book devig → weighted consensus for 2-way markets (UFC, MLB moneyline).
    Returns {outcome: true_prob} or {} if no weighted book has both sides."""
    weighted: Dict[str, float] = {}
    total_w = 0.0
    for bk in game.get("bookmakers", []):
        w = BOOK_WEIGHTS.get(bk.get("key", ""), 0.0)
        if w <= 0.0:
            continue
        for mk in bk.get("markets", []):
            if mk.get("key") != market_key:
                continue
            outs = mk.get("outcomes", [])
            if len(outs) < 2:
                continue
            names = [o.get("name") for o in outs]
            prices = [o.get("price") for o in outs]
            if any(n is None or p is None for n, p in zip(names, prices)):
                continue
            implied = [american_to_implied_prob(int(p)) for p in prices]
            total = sum(implied)
            probs = [ip / total for ip in implied]
            for nm, pr in zip(names, probs):
                weighted[nm] = weighted.get(nm, 0.0) + w * pr
            total_w += w
            break
    if total_w == 0.0 or len(weighted) < 2:
        return {}
    return {nm: v / total_w for nm, v in weighted.items()}


def consensus_devig_3way(game: Dict, market_key: str = "h2h") -> Dict[str, float]:
    """Per-book Shin devig → weighted consensus for 3-way markets (soccer).
    Returns {outcome: true_prob} or {} if no weighted book has all 3 sides."""
    weighted: Dict[str, float] = {}
    total_w = 0.0
    for bk in game.get("bookmakers", []):
        w = BOOK_WEIGHTS.get(bk.get("key", ""), 0.0)
        if w <= 0.0:
            continue
        for mk in bk.get("markets", []):
            if mk.get("key") != market_key:
                continue
            outs = mk.get("outcomes", [])
            if len(outs) < 3:
                continue
            names = [o.get("name") for o in outs]
            prices = [o.get("price") for o in outs]
            if any(n is None or p is None for n, p in zip(names, prices)):
                continue
            implied = [american_to_implied_prob(int(p)) for p in prices]
            probs = devig_shin(implied)
            for nm, pr in zip(names, probs):
                weighted[nm] = weighted.get(nm, 0.0) + w * pr
            total_w += w
            break
    if total_w == 0.0 or len(weighted) < 3:
        return {}
    return {nm: v / total_w for nm, v in weighted.items()}


def consensus_best_odds(game: Dict, market_key: str = "h2h") -> Dict[str, int]:
    """Raw American odds from the highest-weighted single book that has all outcomes.
    Display/line-movement only — true probs come from consensus_devig_*."""
    best_w = -1.0
    best: Dict[str, int] = {}
    for bk in game.get("bookmakers", []):
        w = BOOK_WEIGHTS.get(bk.get("key", ""), 0.0)
        if w <= 0.0 or w <= best_w:
            continue
        for mk in bk.get("markets", []):
            if mk.get("key") != market_key:
                continue
            pair: Dict[str, int] = {}
            for o in mk.get("outcomes", []):
                name, price = o.get("name"), o.get("price")
                if name is not None and price is not None:
                    pair[name] = int(price)
            if len(pair) >= 2:
                best_w = w
                best = pair
            break
    return best


def sharp_odds_per_outcome(game: Dict, market_key: str = "h2h") -> Dict[str, int]:
    """Best-available American odds per outcome from a SINGLE sharp book.

    Prefers Pinnacle, then the configured sharp set. Falls back to the book with
    the lowest overround (closest to fair). This avoids the best-of-all-books
    artifact that synthetically deflates the overround and invents BUY edges.
    """
    by_book: Dict[str, Dict[str, int]] = {}
    for bk in game.get("bookmakers", []):
        key = bk.get("key", "")
        for mk in bk.get("markets", []):
            if mk.get("key") != market_key:
                continue
            for o in mk.get("outcomes", []):
                name, price = o.get("name"), o.get("price")
                if name is not None and price is not None:
                    by_book.setdefault(key, {})[name] = int(price)
    for sb in SHARP_BOOKS:
        if sb in by_book and len(by_book[sb]) >= 2:
            return by_book[sb]
    if not by_book:
        return {}
    return min(by_book.values(),
               key=lambda d: sum(american_to_implied_prob(v) for v in d.values()))


# ─────────────────────────────────────────────────────────────────────
# String / matching helpers
# ─────────────────────────────────────────────────────────────────────
_DATE_TAIL = re.compile(r"\s+on\s+\d{4}-\d{2}-\d{2}\s*\??$", re.I)


def strip_trailing_date(q: str) -> str:
    """'Will Houston Dynamo win on 2026-03-07?' → 'Will Houston Dynamo win'."""
    return _DATE_TAIL.sub("", q or "").strip()


def _norm(s: str) -> str:
    """Lowercase + strip diacritics (NFKD) so 'Türkiye' matches 'Turkey'-family aliases."""
    return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                   if not unicodedata.combining(c)).lower().strip()


def _name_in(text: str, name: str, aliases: Dict[str, List[str]]) -> bool:
    t = _norm(text)
    for a in aliases.get(name, [name]):
        if _norm(a) in t:
            return True
    return False


def match_event_by_participants(names: List[str], events: List[Dict],
                                aliases: Optional[Dict[str, List[str]]] = None) -> Optional[Dict]:
    """Find the event whose title contains all participants (alias/unicode aware)."""
    aliases = aliases or {}
    for ev in events:
        title = ev.get("title", "")
        if " vs" not in title.lower():
            continue
        if all(_name_in(title, n, aliases) for n in names):
            return ev
    return None


def outcome_index_for(market: Dict, want: str) -> int:
    """Index of `want` ('Yes'/'Over'/fighter name) in market.outcomes. Default 0."""
    raw = market.get("outcomes", "[]")
    arr = json.loads(raw) if isinstance(raw, str) else raw
    for i, nm in enumerate(arr or []):
        if _norm(str(nm)) == _norm(want):
            return i
    return 0


def price0(market: Dict) -> float:
    """First outcomePrice (the YES / Over / first-listed side). 0.0 on parse failure."""
    raw = market.get("outcomePrices", "[]")
    arr = json.loads(raw) if isinstance(raw, str) else raw
    try:
        return float(arr[0])
    except (ValueError, TypeError, IndexError):
        return 0.0


def is_stale_event(commence_time: str, min_minutes: int = 30) -> bool:
    """True if the event starts in <min_minutes or has already started / is unparseable."""
    if not commence_time:
        return True
    try:
        gt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        return (gt - datetime.now(timezone.utc)).total_seconds() / 60 < min_minutes
    except (ValueError, TypeError):
        return True


# ─────────────────────────────────────────────────────────────────────
# Config + Edge
# ─────────────────────────────────────────────────────────────────────
@dataclass
class SportConfig:
    name: str
    odds_api_sport_keys: List[str]
    polymarket_tag: str
    market_model: str                       # "2way" | "3way" | "outright"
    featured_markets: List[str]
    regions: str = "eu"
    bookmakers: str = CONSENSUS_BOOKMAKERS  # weighted consensus books; overrides regions (1 credit unit)
    team_aliases: Dict[str, List[str]] = field(default_factory=dict)
    shadow_strategy: str = ""
    archetype: str = "sports_single_game"
    min_minutes_to_start: int = 30


@dataclass
class Edge:
    event_title: str
    participant: str
    market_type: str
    market_model: str
    book_prob: float
    american_odds: int
    poly_price: float
    edge_pct: float
    direction: str
    commence_time: str
    point_value: Optional[float] = None
    poly_market_id: Optional[str] = None
    poly_event_id: Optional[str] = None
    executable_price: Optional[float] = None
    executable_edge: Optional[float] = None
    book_spread: Optional[float] = None
    slippage_bps: Optional[float] = None
    tradeable: bool = False
    no_api_line: bool = False
    live_book: bool = False


# ─────────────────────────────────────────────────────────────────────
# Polymarket fetch (paginated)
# ─────────────────────────────────────────────────────────────────────
def fetch_polymarket_events_by_tag(tag: str, page_size: int = 100, max_pages: int = 8) -> List[Dict]:
    """Gamma /events paginated by offset until a short/empty page — avoids the
    silent 100/200-row truncation that would drop World Cup matches."""
    out: List[Dict] = []
    for page in range(max_pages):
        try:
            resp = requests.get(
                f"{POLYMARKET_GAMMA}/events",
                params={"closed": "false", "tag_slug": tag,
                        "limit": str(page_size), "offset": str(page * page_size)},
                timeout=30,
            )
            data = resp.json()
        except Exception as e:
            logger.warning(f"gamma fetch {tag} page {page} failed: {e}")
            break
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        if len(data) < page_size:
            break
    return out


async def fetch_polymarket_events_by_tag_async(tag: str, **kw) -> List[Dict]:
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as pool:
        return await loop.run_in_executor(pool, lambda: fetch_polymarket_events_by_tag(tag, **kw))


# ─────────────────────────────────────────────────────────────────────
# Executable-edge enrichment + shadow logging (lazy heavy imports)
# ─────────────────────────────────────────────────────────────────────
def enrich_executable_edge(edge: Edge, outcome_index: int, target_usd: float = 100.0) -> None:
    """Walk the Polymarket order book for the MATCHED outcome; fill executable
    fields in-place. Book side follows the matched token (index 0 → YES, else NO).
    Non-fatal: leaves fields None / tradeable False if the book can't be fetched.
    NOTE: this makes a blocking network call — callers in async context must run
    it via run_in_executor (see engines) to avoid blocking the event loop."""
    try:
        from . import poly_executable_edge as pee
    except ImportError:  # pragma: no cover
        import poly_executable_edge as pee
    if not edge.poly_market_id:
        return
    side = "YES" if outcome_index == 0 else "NO"
    # P3.4: prefer the live WS book when POLY_WS_CONSUME=1 (REST fallback otherwise).
    _tid = None
    _book = None
    import os as _os
    if _os.environ.get("POLY_WS_CONSUME") == "1":
        try:
            from api.services import poly_ws_reader as _pwr
            _toks = pee.condition_id_to_token_ids(edge.poly_market_id)
            if _toks:
                _tid = _toks[outcome_index if outcome_index in (0, 1) else 0]
                _book = _pwr.get_live_orderbook_sync(_tid)
                if _book is not None:
                    edge.live_book = True
        except Exception:
            _tid = None
            _book = None
    try:
        ex = pee.executable_edge(edge.book_prob, side, token_id=_tid, condition_id=edge.poly_market_id,
                                 outcome_index=outcome_index, target_usd=target_usd, book=_book)
    except Exception:
        ex = {"available": False}
    if ex.get("available"):
        edge.executable_price = ex["executable_price"]
        edge.executable_edge = ex["executable_edge"]
        edge.book_spread = ex["spread"]
        edge.slippage_bps = ex["slippage_bps"]
        edge.tradeable = ex["tradeable"]


def fee_adjusted_edge(edge: Edge) -> Optional[float]:
    """Executable edge net of the Polymarket ~2% winner fee. None if not enriched."""
    if edge.executable_edge is None or edge.executable_price is None:
        return None
    return edge.executable_edge - POLY_WINNER_FEE * edge.executable_price


def log_shadow(edge: Edge, cfg: SportConfig, days_to_close: float = 7.0) -> bool:
    """Log to the shadow tracker ONLY when the edge is tradeable AND +EV after fees,
    using the EXECUTABLE price as entry (not the midpoint). Returns True if logged."""
    if not edge.poly_market_id or not edge.tradeable or edge.executable_price is None:
        return False
    fae = fee_adjusted_edge(edge)
    if fae is None or fae <= 0:
        return False
    try:
        from signals.shadow_tracker import log_shadow_trade
    except Exception:
        return False
    try:
        log_shadow_trade({
            "market_id": edge.poly_market_id,
            "market": f"{edge.event_title[:180]} — {edge.participant} {edge.market_type}",
            "platform": "polymarket",
            "side": "YES" if edge.direction == "BUY" else "NO",
            "price": edge.executable_price,        # executable, not midpoint
            "confidence": min(85.0, abs(fae) * 1500),
            "days_to_close": days_to_close, "volume": 0, "confirmations": 1,
            "reasoning": (f"{cfg.name}: book {edge.book_prob * 100:.0f}% vs exec "
                          f"{edge.executable_price * 100:.1f}¢ "
                          f"(exec edge {edge.executable_edge * 100:+.1f}%, fee-adj {fae * 100:+.1f}%)"),
            "archetype": cfg.archetype, "strategy": cfg.shadow_strategy,
            "category": cfg.name.split("_")[0], "category_tier": "sports",
            "midpoint_price": edge.poly_price,     # for CLV at resolution
        })
        return True
    except Exception as e:
        logger.debug(f"{cfg.name} shadow log failed: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────
# Summary (mirrors baseball get_*_edge_summary JSON shape)
# ─────────────────────────────────────────────────────────────────────
def summarize(edges: List[Edge], cfg: SportConfig) -> Dict:
    edges = sorted(edges, key=lambda e: abs(e.edge_pct), reverse=True)
    return {
        "source": f"the_odds_api_{cfg.name}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_edges": len(edges),
        "edges": [{
            "event": e.event_title, "participant": e.participant,
            "market_type": e.market_type, "point_value": e.point_value,
            "book_prob": round(e.book_prob * 100, 1),
            "american_odds": f"{e.american_odds:+d}",
            "polymarket_price": round(e.poly_price * 100, 1),
            "edge_pct": round(e.edge_pct * 100, 1), "direction": e.direction,
            "commence_time": e.commence_time, "market_id": e.poly_market_id,
            "event_id": e.poly_event_id,
            "executable_price": (round(e.executable_price * 100, 1) if e.executable_price is not None else None),
            "executable_edge": (round(e.executable_edge * 100, 1) if e.executable_edge is not None else None),
            "book_spread_pct": (round(e.book_spread * 100, 1) if e.book_spread is not None else None),
            "slippage_bps": e.slippage_bps, "tradeable": e.tradeable,
            "no_api_line": e.no_api_line, "live_book": e.live_book,
        } for e in edges],
        "top_opportunities": [{
            "event": e.event_title, "participant": e.participant,
            "edge": f"{e.edge_pct * 100:+.1f}%",
            "action": f"{e.direction} at {e.poly_price * 100:.0f}¢",
        } for e in edges[:5] if not e.no_api_line],
    }

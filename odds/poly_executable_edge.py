"""
poly_executable_edge.py — Shared executable-edge enrichment for ALL Polymarket
edge engines (baseball, weather, options, soccer, cross-platform-arb, ...).

WHY THIS EXISTS
---------------
Every edge engine historically computed:

    edge = true_prob - GAMMA_MIDPOINT_PRICE   (outcomePrices / last price)

The Gamma midpoint is NOT the price you can trade at. To BUY an outcome you lift
the best ASK, walked across depth for your size — so the real edge is:

    executable_edge = true_prob - VWAP_fill_price(side, $size)

A midpoint "edge" routinely evaporates after spread + slippage (Polymarket books
are often only a handful of levels deep). This module wraps the order-book
machinery that ALREADY exists in `polymarket_clob.py` (`size_to_book`) so any
engine can attach a slippage-adjusted edge + tradeability verdict with one call.

DESIGN
------
- Non-fatal: if the book can't be fetched, returns {"available": False, ...} and
  the caller simply keeps its midpoint edge. Never raises into a scanner.
- Stateless, read-only. Belongs to the Scanner layer (no execution side effects).
- Outcome→token mapping is pushed to the caller when it isn't a clean Yes/No:
  pass `outcome_index` (0 = first token, 1 = second) when the market is
  Over/Under or team-vs-team; otherwise the Yes/No heuristic on `side` is used.
"""

from typing import Dict, Optional, Tuple
import json

try:  # match the dual-import pattern used across odds/*
    from . import polymarket_clob as clob
except ImportError:  # pragma: no cover
    import polymarket_clob as clob

GAMMA_API = "https://gamma-api.polymarket.com"


def _is_yes(side: str) -> bool:
    """Best-effort map of a side label to the YES (index-0) token."""
    s = str(side).strip().lower()
    return s in ("yes", "buy", "over", "true", "y", "o") or s.startswith("yes")


def condition_id_to_token_ids(condition_id: str) -> Optional[Tuple[str, str]]:
    """Map a Polymarket conditionId -> (token0, token1) via Gamma clobTokenIds.

    Token order matches the market's `outcomes` array (index 0 = first outcome,
    e.g. "Yes" / "Over" / home team). Returns None if not found.
    """
    if not condition_id:
        return None
    try:
        url = f"{GAMMA_API}/markets?condition_ids={condition_id}"
        data = clob._resilient_urlopen("polymarket_gamma", url, timeout=10)
        if not data:
            return None
        m = data[0] if isinstance(data, list) else data
        toks = m.get("clobTokenIds", "[]")
        if isinstance(toks, str):
            toks = json.loads(toks)
        if isinstance(toks, list) and len(toks) >= 2:
            return toks[0], toks[1]
    except Exception:
        return None
    return None


def executable_edge(
    true_prob: float,
    side: str,
    *,
    token_id: Optional[str] = None,
    condition_id: Optional[str] = None,
    slug: Optional[str] = None,
    outcome_index: Optional[int] = None,
    target_usd: float = 100.0,
    max_slip_bps: float = 50.0,
    min_usd: float = 15.0,
    max_spread: float = 0.05,
) -> Dict:
    """Compute the order-book-executable edge for a single signal.

    Provide ONE of: `token_id`, `condition_id` (+ optional `outcome_index`),
    or `slug`. `true_prob` is the model's fair probability (0..1) for the side
    you would BUY; `side` labels that side ("YES"/"Over"/team/...).

    Returns a dict (always; never raises):
      available        : bool   — was an order book fetched?
      executable_price : float? — VWAP fill price for target_usd (decimal 0..1)
      best_price       : float? — top-of-book ask
      executable_edge  : float? — true_prob - executable_price
      spread           : float? — best_ask - best_bid
      slippage_bps     : float? — VWAP vs best_price
      fillable_usd     : float? — USD actually fillable within slip/spread caps
      tradeable        : bool   — book ok AND executable_edge > 0
      reason           : str    — "full" | "resized" | "skip:<why>"
    """
    tid = token_id
    if not tid and condition_id:
        toks = condition_id_to_token_ids(condition_id)
        if toks:
            idx = outcome_index if outcome_index is not None else (0 if _is_yes(side) else 1)
            idx = 0 if idx not in (0, 1) else idx
            tid = toks[idx]

    fe = clob.size_to_book(
        token_id=tid,
        market_slug=slug if not tid else None,
        side="YES" if (outcome_index in (None,) and _is_yes(side)) or outcome_index == 0 else "NO",
        target_usd=target_usd,
        max_slip_bps=max_slip_bps,
        min_usd=min_usd,
        max_spread=max_spread,
    )

    # Book unavailable (no token / no book) -> let caller keep its midpoint edge.
    if fe.reason.startswith("skip:no_token") or fe.reason.startswith("skip:no_book"):
        return {
            "available": False,
            "executable_price": None,
            "best_price": None,
            "executable_edge": None,
            "spread": None,
            "slippage_bps": None,
            "fillable_usd": None,
            "tradeable": False,
            "reason": fe.reason,
        }

    exec_price = fe.avg_price if fe.ok else (fe.best_price or None)
    exec_edge = round(true_prob - exec_price, 4) if exec_price else None
    return {
        "available": True,
        "executable_price": exec_price,
        "best_price": fe.best_price or None,
        "executable_edge": exec_edge,
        "spread": fe.spread,
        "slippage_bps": fe.slippage_bps,
        "fillable_usd": fe.actual_usd,
        "tradeable": bool(fe.ok and exec_edge is not None and exec_edge > 0),
        "reason": fe.reason,
    }


def poly_price_move(
    token_id: Optional[str] = None,
    *,
    condition_id: Optional[str] = None,
    outcome_index: int = 0,
) -> Dict:
    """Polymarket's OWN recent price movement for a token, via CLOB price-history.

    A persistent (API-backed) momentum signal — unlike the in-memory line-movement
    tracker, it survives restarts and reflects Polymarket price drift (not the
    bookmaker line). Returns drift over ~1h and ~6h in percentage points.
    Non-fatal: returns {"available": False} on any gap. NOTE: CLOB price-history
    keys on the **token_id**, not the conditionId.

    Returns: {available, last, move_1h_pp, move_6h_pp, n_points}
    """
    tid = token_id
    if not tid and condition_id:
        toks = condition_id_to_token_ids(condition_id)
        if toks:
            idx = outcome_index if outcome_index in (0, 1) else 0
            tid = toks[idx]
    if not tid:
        return {"available": False, "last": None, "move_1h_pp": None, "move_6h_pp": None, "n_points": 0}
    try:
        hist = clob.get_price_history(tid, interval="6h", fidelity=60)
    except Exception:
        hist = []
    closes = [h.get("close") for h in (hist or []) if h.get("close")]
    if len(closes) < 2:
        return {
            "available": False,
            "last": (round(closes[-1], 4) if closes else None),
            "move_1h_pp": None,
            "move_6h_pp": None,
            "n_points": len(closes),
        }
    last = closes[-1]
    return {
        "available": True,
        "last": round(last, 4),
        "move_1h_pp": round((last - closes[-2]) * 100, 2),
        "move_6h_pp": round((last - closes[0]) * 100, 2),
        "n_points": len(closes),
    }

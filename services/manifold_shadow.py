"""Manifold leading-indicator shadow logger (#3).

Manifold (play-money, faster) often leads the real-money market. When Manifold's
prob diverges from Polymarket's by >= MIN_EDGE_PP, we log a directional bet on the
RESOLVABLE Polymarket market (buy the side Manifold implies is underpriced) as a
shadow trade tagged strategy="manifold_lead". Resolution + poly_delta accrue via the
normal shadow pipeline. Overlaps without a Polymarket condition_id are skipped
(unresolvable -> would pollute the scoreboard as permanently-open trades).

Called from scheduler (see services/scheduler.py task_manifold_shadow).
"""
import json
import urllib.request

from loguru import logger

MIN_EDGE_PP = 8.0
GAMMA_EVENTS = "https://gamma-api.polymarket.com/events?closed=false&limit=200"


def _edge_to_signal(edge: dict) -> dict:
    """Map a Manifold-vs-Polymarket overlap to a directional shadow signal.

    direction "YES": Manifold > PM -> buy YES at the PM YES price.
    direction "NO":  Manifold < PM -> buy NO at (1 - PM YES price).
    """
    direction = edge.get("direction", "YES")
    poly_yes = edge.get("polymarket_price", 50.0) / 100.0
    entry = poly_yes if direction == "YES" else round(1.0 - poly_yes, 4)
    return {
        "market_id": edge.get("polymarket_id", ""),
        "market": (edge.get("polymarket_title", "") or "")[:200],
        "platform": "polymarket",
        "side": direction,
        "price": round(entry, 4),
        "confidence": round(min(0.95, 0.5 + abs(edge.get("edge_pct", 0.0)) / 100.0), 4),
        "confirmations": 1,
        "days_to_close": 14,
        "volume": 0,
        "category": "manifold_lead",
        "strategy": "manifold_lead",
        "reasoning": (
            f"Manifold {edge.get('manifold_prob')}% vs PM {edge.get('polymarket_price')}% "
            f"(edge {edge.get('edge_pct')}pp) -> buy {direction}"
        ),
    }


def run_once(min_edge: float = MIN_EDGE_PP) -> dict:
    """Fetch Manifold<->Polymarket overlaps and shadow-log resolvable edges."""
    from odds.manifold import find_polymarket_overlaps
    from signals.shadow_tracker import log_shadow_trade

    try:
        req = urllib.request.Request(GAMMA_EVENTS, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            poly_events = json.loads(resp.read().decode())
    except Exception as e:
        logger.debug("manifold_shadow: poly events fetch failed: {}", e)
        return {"logged": 0, "skipped_no_cid": 0}

    overlaps = find_polymarket_overlaps(poly_events)
    logged, skipped = 0, 0
    for o in overlaps:
        if abs(o.get("edge_pct", 0.0)) < min_edge:
            continue
        if not o.get("polymarket_id"):
            skipped += 1  # unresolvable without a condition_id
            continue
        try:
            if log_shadow_trade(_edge_to_signal(o)):
                logged += 1
        except Exception as e:
            logger.debug("manifold_shadow log failed: {}", e)
    if logged or skipped:
        logger.info("manifold_shadow: logged {} edges ({} skipped, no cid)", logged, skipped)
    return {"logged": logged, "skipped_no_cid": skipped}

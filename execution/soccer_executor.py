"""Soccer edge → live execution bridge.

Mirrors the smart-wallet pattern: takes tradeable soccer edges (already enriched
with token_id via sports_edge_common.enrich_executable_edge) and routes them
through the hybrid maker→taker executor.

Called from scheduler.task_soccer_match_scan when live_config.mode() == "LIVE".
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("soccer_executor")

# ── sizing ────────────────────────────────────────────────────────────────
_SOCCER_LIVE_SIZE_USD = 10.0  # per-leg cap (same as weather / baseball game edges)
_SOCCER_MIN_EXECUTABLE_EDGE = 0.05  # 5pp net after fees — gate before taker


def execute_tradeable_soccer_edges(edges: list) -> dict:
    """Fire live orders for every tradeable soccer edge.

    Args:
        edges: list of sports_edge_common.Edge objects (already enriched).

    Returns:
        {"filled": int, "dropped": int, "skipped": int, "errors": int}
    """
    from execution import live_config, live_db, live_executor
    from execution.risk_governor import RiskGovernor

    if live_config.mode() != "LIVE":
        return {"filled": 0, "dropped": 0, "skipped": 0, "errors": 0, "reason": "not LIVE mode"}

    stats = {"filled": 0, "dropped": 0, "skipped": 0, "errors": 0}
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    conn = live_db.connect()
    try:
        governor = RiskGovernor(conn, mode="LIVE")

        for edge in edges:
            if not edge.tradeable or not edge.poly_market_id:
                continue

            # Resolve token_id from condition_id + outcome_index
            # direction="BUY" → we think YES is underpriced → buy YES token (index 0)
            # direction="SELL" → we think YES is overpriced → buy NO token (index 1)
            outcome_index = 0 if edge.direction == "BUY" else 1
            try:
                from odds.poly_executable_edge import condition_id_to_token_ids

                toks = condition_id_to_token_ids(edge.poly_market_id)
                if not toks or len(toks) < 2:
                    logger.warning("soccer_exec: no token_ids for %s", edge.poly_market_id[:16])
                    stats["dropped"] += 1
                    continue
                token_id = toks[outcome_index]
            except Exception as exc:
                logger.warning("soccer_exec: token resolution failed: %s", exc)
                stats["errors"] += 1
                continue

            # CLOB side is always BUY (we buy the token we want exposure to)
            clob_side = "BUY"
            fair_price = edge.book_prob if edge.direction == "BUY" else (1.0 - edge.book_prob)

            # ── Close-time window gate ──────────────────────────────────
            # Soccer: only enter 30min-7h before kickoff.
            commence_str = edge.commence_time or ""
            mins_to_close = None
            if commence_str:
                try:
                    from datetime import datetime, timezone
                    cdt = datetime.fromisoformat(commence_str.replace("Z", "+00:00"))
                    mins_to_close = (cdt - datetime.now(timezone.utc)).total_seconds() / 60.0
                except Exception:
                    pass
            if mins_to_close is not None:
                from execution.live_config import in_close_window
                ok, reason = in_close_window(mins_to_close, "soccer")
                if not ok:
                    stats["dropped"] += 1
                    logger.info("soccer_exec: %s — %s", client_order_ref, reason)
                    continue

            # ── Velocity filter ──────────────────────────────────────
            # Block entry if edge is collapsing (soft book converging on sharp).
            from execution.live_config import velocity_check
            vel_ok, vel_reason = velocity_check(
                sport="soccer",
                event_id=edge.poly_event_id or edge.event_title[:40],
                participant=edge.participant,
                market_type=edge.market_type,
            )
            if not vel_ok:
                stats["dropped"] += 1
                logger.info("soccer_exec: %s — %s", client_order_ref, vel_reason)
                continue

            # Executable edge check (net after fees)
            exec_edge = edge.executable_edge or edge.edge_pct
            if exec_edge < _SOCCER_MIN_EXECUTABLE_EDGE:
                stats["dropped"] += 1
                continue

            # Tick size
            tick_size = getattr(edge, "tick_size", None)
            if tick_size is None:
                try:
                    from execution import clob_client

                    tick_size = clob_client.get_tick_size(token_id)
                except Exception:
                    tick_size = 0.01

            # Size: tiered by executable edge, capped by fillable book depth
            from execution.live_config import tiered_size_usd
            tiered = tiered_size_usd(exec_edge, category="soccer")
            fillable = edge.fillable_usd or tiered  # if no fillable info, use tiered
            size_usd = min(tiered, fillable)
            if size_usd <= 0:
                stats["dropped"] += 1
                continue

            # Build unique ref
            participant_slug = edge.participant.lower().replace(" ", "_")[:20]
            market_slug = edge.market_type[:3]  # home/draw/away
            client_order_ref = f"sc-{date_str}-{participant_slug}-{market_slug}"

            intent = {
                "size_usd": size_usd,
                "market_id": token_id,
                "token_id": token_id,
                "side": clob_side,
                "category": "soccer_match_3way",
            }

            decision = governor.check(intent)
            if not decision.allowed:
                logger.info("soccer_exec: governor denied %s: %s", client_order_ref, decision.reason)
                stats["dropped"] += 1
                continue

            try:
                result = live_executor.execute_intent(
                    conn,
                    governor,
                    token_id=token_id,
                    side=clob_side,
                    fair_price=fair_price,
                    size_usd=size_usd,
                    tick_size=tick_size,
                    neg_risk=False,  # soccer match markets are standard
                    net_edge_taker=exec_edge,
                    client_order_ref=client_order_ref,
                    category="soccer_match_3way",
                    market_title=(f"{edge.event_title} — {edge.participant} {edge.market_type}".strip(" —"))[:120],
                )
                action = result.get("action", "unknown")
                if action in ("maker_filled", "taker_filled"):
                    stats["filled"] += 1
                    logger.info("soccer_exec: %s → %s", client_order_ref, action)
                else:
                    stats["dropped"] += 1
                    logger.info("soccer_exec: %s → %s (%s)", client_order_ref, action, result.get("reason", ""))
            except Exception as exc:
                logger.warning("soccer_exec: execute_intent failed for %s: %s", client_order_ref, exc)
                stats["errors"] += 1
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return stats

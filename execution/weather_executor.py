"""Weather resolution-edge → live execution bridge.

Takes TWC resolution-source edges (from weather_resolution_edge.py) and routes
them through the hybrid maker→taker executor. Mirrors soccer_executor pattern.

Called from scheduler.task_resolution_edge_scan when live_config.mode() == "LIVE".

Weather markets are bracket format ("between 92-93°F") — we buy NO when TWC
forecast is above the bracket (TWC says 96°F, bracket is 92-93°F, market
prices YES at 0.265 → buy NO at 0.735).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("weather_executor")

# ── sizing ────────────────────────────────────────────────────────────────
_WEATHER_LIVE_SIZE_USD = 10.0  # per-leg cap
_WEATHER_MIN_EXECUTABLE_EDGE = 0.05  # 5pp net after fees


def execute_tradeable_weather_edges(signals: list) -> dict:
    """Fire live orders for every tradeable weather resolution edge.

    Args:
        signals: list of dicts from weather_resolution_edge._scan_polymarket_edges().
                 Each must have: condition_id, direction, edge_pp, market_price.

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

        for sig in signals:
            condition_id = sig.get("condition_id", "")
            direction = sig.get("direction", "")
            edge_pp = sig.get("edge_pp", 0)
            market_price = sig.get("market_price", 0)
            city = sig.get("city", "unknown")
            market_title = sig.get("market_title", "")

            if not condition_id:
                stats["dropped"] += 1
                continue

            # Resolve token_id from condition_id
            # direction="buy_no" → YES is overpriced → buy NO token (index 1)
            # direction="buy_yes" → YES is underpriced → buy YES token (index 0)
            outcome_index = 1 if direction == "buy_no" else 0
            try:
                from odds.poly_executable_edge import condition_id_to_token_ids

                toks = condition_id_to_token_ids(condition_id)
                if not toks or len(toks) < 2:
                    logger.warning("weather_exec: no token_ids for %s", condition_id[:16])
                    stats["dropped"] += 1
                    continue
                token_id = toks[outcome_index]
            except Exception as exc:
                logger.warning("weather_exec: token resolution failed: %s", exc)
                stats["errors"] += 1
                continue

            # CLOB side is always BUY (we buy the token we want exposure to)
            clob_side = "BUY"

            # Fair price: what we think the token is worth
            # For buy_no: we buy NO at (1 - market_price), fair = (1 - twc_implied_prob)
            # For buy_yes: we buy YES at market_price, fair = twc_implied_prob
            twc_implied = sig.get("twc_implied_prob", 0.5)
            if direction == "buy_no":
                fair_price = 1.0 - twc_implied
            else:
                fair_price = twc_implied

            # Executable edge check (net after fees)
            exec_edge = abs(edge_pp) / 100.0
            if exec_edge < _WEATHER_MIN_EXECUTABLE_EDGE:
                stats["dropped"] += 1
                continue

            # Tick size
            tick_size = 0.01  # weather markets use 0.01 tick

            # Size
            size_usd = _WEATHER_LIVE_SIZE_USD
            if size_usd <= 0:
                stats["dropped"] += 1
                continue

            # Build unique ref
            city_slug = city.lower().replace(" ", "_")[:15]
            client_order_ref = f"wx-{date_str}-{city_slug}-{outcome_index}"

            intent = {
                "size_usd": size_usd,
                "market_id": token_id,
                "token_id": token_id,
                "side": clob_side,
            }

            decision = governor.check(intent)
            if not decision.allowed:
                logger.info("weather_exec: governor denied %s: %s", client_order_ref, decision.reason)
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
                    neg_risk=False,
                    net_edge_taker=exec_edge,
                    client_order_ref=client_order_ref,
                    category="weather_resolution",
                    market_title=(market_title or "")[:120],
                )
                action = result.get("action", "unknown")
                if action in ("maker_filled", "taker_filled"):
                    stats["filled"] += 1
                    logger.info("weather_exec: %s → %s (edge=%.1fpp)", client_order_ref, action, edge_pp)
                else:
                    stats["dropped"] += 1
                    logger.info("weather_exec: %s → %s (%s)", client_order_ref, action, result.get("reason", ""))
            except Exception as exc:
                logger.warning("weather_exec: execute_intent failed for %s: %s", client_order_ref, exc)
                stats["errors"] += 1
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return stats

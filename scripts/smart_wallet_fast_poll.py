"""
smart_wallet_fast_poll.py
─────────────────────────
Lightweight 90-second smart wallet fill scanner.

Decoupled from the heavy whale_scanner (~5 min cycle). Only fetches PM trades
for the last 3 minutes, filters to tracked smart wallet addresses, and passes
fills to smart_wallet_alert.scanner_hook().

At 90s poll cadence:
  - Same-poll fills: ≤ 90s clustering (flash convergence)
  - Adjacent-poll fills: ≤ 3 min clustering (strong)
  - Skip-one-poll fills: ≤ 4.5 min clustering (good)

This gives us genuinely instantaneous convergence detection for live events
compared to the 5-min effective cycle of the full whale scanner.

Called by: scheduler.py task_smart_wallet_fast()
"""
from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# How far back to look for trades each poll (2x poll interval for overlap safety)
_LOOKBACK_SECS = 180   # 3 minutes
_SMART_WALLET_MIN_USD = 1000  # minimum fill size to consider (raised from $500 2026-06-25)

# Live execution config for smart wallet signals
# Only "entry" alerts are wired — refire has -8.37% avg CLV (2026-06-25 audit, n=70)
_SW_LIVE_ALERT_TYPES = {"entry"}
_SW_LIVE_SIZE_USD = 25.0  # conservative start; raise after live validation


def _route_live_smart_wallet(fired: list) -> None:
    """Route qualifying smart wallet entry signals to the live executor.

    Only runs when POLYCLAWD_MODE=LIVE. Only wires 'entry' alert_type.
    Uses maker-only path (net_edge_taker=0.0) to avoid 5% taker fee on
    signals where we don't have a precise taker-edge calculation.

    Calibration basis (2026-06-25): entry alerts 62.1% WR, +6.82% avg CLV, n=177.
    Refire excluded: 48.6% WR, -8.37% avg CLV.
    """
    try:
        from execution import live_config
        if live_config.mode() != "LIVE":
            return
    except Exception:
        return

    from datetime import datetime, timezone

    for rec in fired:
        if rec.get("alert_type") not in _SW_LIVE_ALERT_TYPES:
            continue

        condition_id = rec.get("market", "")
        outcome_index = rec.get("outcome_index", 0)  # 0=YES token, 1=NO token
        price_at_alert = float(rec.get("price_at_alert") or 0)
        if not condition_id or price_at_alert <= 0:
            continue

        try:
            from odds.poly_executable_edge import condition_id_to_token_ids
            token_ids = condition_id_to_token_ids(condition_id)
            if not token_ids or len(token_ids) < 2:
                logger.warning("sw_live: no token_ids for condition %s", condition_id[:16])
                continue
            token_id = token_ids[0] if outcome_index == 0 else token_ids[1]
        except Exception as exc:
            logger.warning("sw_live: token resolution failed for %s: %s", condition_id[:16], exc)
            continue

        try:
            from execution import clob_client, live_db, live_executor
            from execution.risk_governor import RiskGovernor

            tick_size = clob_client.get_tick_size(token_id)
        except Exception as exc:
            logger.warning("sw_live: clob setup failed: %s", exc)
            continue

        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        client_order_ref = f"sw-{date_str}-{condition_id[:16]}-{outcome_index}"

        intent = {"size_usd": _SW_LIVE_SIZE_USD, "market_id": token_id, "token_id": token_id, "side": "BUY"}

        conn = live_db.connect()
        try:
            governor = RiskGovernor(conn, mode="LIVE")
            decision = governor.check(intent)
            if not decision.allowed:
                logger.info("sw_live: governor denied %s: %s", client_order_ref, decision.reason)
                continue

            result = live_executor.execute_intent(
                conn,
                governor,
                token_id=token_id,
                side="BUY",
                fair_price=price_at_alert,
                size_usd=_SW_LIVE_SIZE_USD,
                tick_size=tick_size,
                neg_risk=bool(rec.get("neg_risk", False)),
                net_edge_taker=0.0,  # maker-only; no taker fallback until taker edge is computed
                client_order_ref=client_order_ref,
                category=rec.get("category") or "smart_wallet",
            )
            logger.info("sw_live: %s → %s", client_order_ref, result.get("action"))
        except Exception as exc:
            logger.warning("sw_live: execute_intent failed for %s: %s", client_order_ref, exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass


def run() -> dict:
    """Fetch recent PM trades, filter to smart wallets, fire alerts.

    Returns dict with summary stats for logging.
    """
    now = int(time.time())
    since = now - _LOOKBACK_SECS

    # --- Load smart wallet ledger ---
    try:
        from signals.whale_wallets import get_meta_db, get_smart_wallets
        meta_conn = get_meta_db()
        smart = get_smart_wallets(meta_conn)
    except Exception as e:
        logger.warning("smart_wallet_fast_poll: wallet ledger unavailable: %s", e)
        return {"error": str(e)}

    if not smart:
        meta_conn.close()
        return {"smart_wallets": 0, "fills": 0}

    smart_addrs = set(smart.keys())

    # --- Fetch recent PM trades (lightweight — last 3 min only) ---
    try:
        from signals.whale_scanner import fetch_pm_trades_since, fetch_gamma_by_condition
    except ImportError as e:
        meta_conn.close()
        logger.warning("smart_wallet_fast_poll: import error: %s", e)
        return {"error": str(e)}

    try:
        trades = fetch_pm_trades_since(since)
    except Exception as e:
        meta_conn.close()
        logger.warning("smart_wallet_fast_poll: trade fetch failed: %s", e)
        return {"error": str(e)}

    # Filter to smart wallet trades only
    sw_trades = [t for t in trades if t.get("proxyWallet") in smart_addrs]

    if not sw_trades:
        meta_conn.close()
        return {"smart_wallets": len(smart), "trades_scanned": len(trades), "fills": 0}

    # Fetch gamma metadata for markets these wallets traded
    cids = list({t.get("conditionId") for t in sw_trades if t.get("conditionId")})
    try:
        gamma = fetch_gamma_by_condition(cids)
    except Exception as e:
        gamma = {}
        logger.warning("smart_wallet_fast_poll: gamma fetch failed: %s", e)

    # Pass to scanner_hook (handles accumulation, dedup, alert firing, convergence)
    try:
        from scripts.smart_wallet_alert import scanner_hook
        fired = scanner_hook(meta_conn, sw_trades, gamma, smart)
    except Exception as e:
        logger.warning("smart_wallet_fast_poll: scanner_hook failed: %s", e)
        fired = []
    finally:
        try:
            meta_conn.close()
        except Exception:
            pass

    # Route entry-type alerts to live executor (no-op in PAPER mode)
    if fired:
        try:
            _route_live_smart_wallet(fired)
        except Exception as exc:
            logger.warning("smart_wallet_fast_poll: live routing failed: %s", exc)

    logger.info(
        "smart_wallet_fast_poll: %d trades scanned, %d sw fills, %d alerts fired",
        len(trades), len(sw_trades), len(fired),
    )
    return {
        "smart_wallets": len(smart),
        "trades_scanned": len(trades),
        "sw_fills": len(sw_trades),
        "alerts_fired": len(fired),
    }

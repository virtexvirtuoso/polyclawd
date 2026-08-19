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

# ── Exit cooldown: don't re-enter a market within 2h of stop-loss exit ────────
_EXIT_COOLDOWN_SECS = 7200  # 2 hours
_EXIT_COOLDOWN_FILE = "/tmp/sw_exit_cooldown.json"


def _is_in_exit_cooldown(token_id: str) -> bool:
    """Return True if this token was recently stopped out and is in cooldown."""
    import json, os, time

    try:
        if not os.path.exists(_EXIT_COOLDOWN_FILE):
            return False
        data = json.loads(open(_EXIT_COOLDOWN_FILE).read())
        ts = data.get(token_id, 0)
        return (time.time() - ts) < _EXIT_COOLDOWN_SECS
    except Exception:
        return False


def register_exit_cooldown(token_id: str) -> None:
    """Record that a position was closed — block re-entry for cooldown window."""
    import json, os, time

    try:
        data = {}
        if os.path.exists(_EXIT_COOLDOWN_FILE):
            data = json.loads(open(_EXIT_COOLDOWN_FILE).read())
        data[token_id] = time.time()
        open(_EXIT_COOLDOWN_FILE, "w").write(json.dumps(data))
    except Exception:
        pass


# How far back to look for trades each poll (2x poll interval for overlap safety)
_LOOKBACK_SECS = 180  # 3 minutes
_SMART_WALLET_MIN_USD = 1000  # minimum fill size to consider (raised from $500 2026-06-25)

# Live execution config for smart wallet signals
# Only "entry" alerts are wired — refire has -8.37% avg CLV (2026-06-25 audit, n=70)
_SW_LIVE_ALERT_TYPES = {"entry"}
# Dynamic sizing: fraction of bankroll so it scales with wins.
# At $40 bankroll → $10/trade; $80 → $15/trade (cap).
# Floored at $5 so we don't get stuck in sub-$5 noise.
_SW_LIVE_FRACTION = 0.25  # 25% of bankroll per trade
_SW_LIVE_MIN_USD = 5.0  # floor — $5 minimum even at tiny bankroll
_SW_LIVE_MAX_USD = 15.0  # cap — safety limit per trade

# Category gate — only follow smart wallets into approved market verticals.
# Gamma sometimes returns category=None (e.g. entertainment/pop-culture markets).
# In that case we require a positive slug/question keyword match to proceed.
_ALLOWED_CATEGORIES = {"sports", "crypto", "politics", "weather", "finance", "economics"}
_ALLOWED_SLUG_KEYWORDS = (
    # Sports
    "mlb",
    "nfl",
    "nba",
    "nhl",
    "ufc",
    "mls",
    "wc",
    "fwc",
    "fifwc",
    "soccer",
    "football",
    "basketball",
    "baseball",
    "tennis",
    "golf",
    "nascar",
    "boxing",
    "mma",
    "hockey",
    # Crypto / finance
    "btc",
    "eth",
    "bitcoin",
    "ethereum",
    "crypto",
    "sol",
    "xrp",
    "doge",
    "fed-rate",
    "gdp",
    "inflation",
    "cpi",
    # Politics
    "trump",
    "biden",
    "election",
    "senate",
    "congress",
    "president",
    "democrat",
    "republican",
    "vote",
)


def _route_live_smart_wallet(fired: list, gamma: dict) -> None:
    """Route qualifying smart wallet entry signals to the live executor.

    Only runs when POLYCLAWD_MODE=LIVE. Only wires 'entry' alert_type.
    Uses hybrid maker+taker path: maker-first, taker fallback if net_edge_taker >= min_taker_edge. On
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

    logger.info("sw_live: routing %d fired alerts to live executor", len(fired))

    from datetime import datetime, timezone

    # Dynamic sizing: compute once per sweep based on current bankroll
    try:
        from execution.live_db import connect as _ldb_connect

        _ldb = _ldb_connect()
        row = _ldb.execute("SELECT bankroll FROM live_portfolio_state ORDER BY id DESC LIMIT 1").fetchone()
        bankroll = row["bankroll"] if row else 0.0
        _ldb.close()
    except Exception:
        bankroll = 0.0
    size_usd = max(_SW_LIVE_MIN_USD, min(_SW_LIVE_MAX_USD, bankroll * _SW_LIVE_FRACTION))
    logger.info(
        "sw_live: bankroll=$%.2f → size=$%.2f (frac=%.0f%% floor=$%.0f cap=$%.0f)",
        bankroll,
        size_usd,
        _SW_LIVE_FRACTION * 100,
        _SW_LIVE_MIN_USD,
        _SW_LIVE_MAX_USD,
    )

    for rec in fired:
        if rec.get("alert_type") not in _SW_LIVE_ALERT_TYPES:
            continue

        condition_id = rec.get("market", "")
        outcome_index = rec.get("outcome_index", 0)  # 0=YES token, 1=NO token
        price_at_alert = float(rec.get("price_at_alert") or 0)
        if not condition_id or price_at_alert <= 0:
            continue

        # Category gate: only execute in approved market verticals (Option B).
        # Blocks pop-culture/entertainment markets like the 2026-07-01 Rihanna incident.
        # Uses Gamma API category first, falls back to the rec dict's category (pre-classified
        # by smart_wallet_alert.py), then slug keyword matching.
        gm_data = gamma.get(condition_id, {})
        mkt_category = (gm_data.get("category") or "").lower().strip()
        mkt_slug = (gm_data.get("slug") or "").lower()
        mkt_question = (gm_data.get("question") or "").lower()
        if mkt_category:
            if mkt_category not in _ALLOWED_CATEGORIES:
                logger.info(
                    "sw_live: blocked — category '%s' not in allowlist for %s, skipping",
                    mkt_category,
                    condition_id[:16],
                )
                continue
        else:
            # Fallback 1: use rec dict's pre-classified category (populated by smart_wallet_alert.py)
            rec_category = (rec.get("category") or "").lower().strip()
            if rec_category in _ALLOWED_CATEGORIES:
                pass  # allowed
            # Fallback 2: keyword match in slug/question
            elif any(kw in mkt_slug or kw in mkt_question for kw in _ALLOWED_SLUG_KEYWORDS):
                pass  # allowed
            else:
                logger.info(
                    "sw_live: blocked — no category (rec='%s') + no known pattern (slug='%s') for %s, skipping",
                    rec_category,
                    mkt_slug[:40],
                    condition_id[:16],
                )
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

        # Exit cooldown guard: skip re-entry if we recently stopped out of this market
        if _is_in_exit_cooldown(token_id):
            logger.info("sw_live: %s in exit cooldown (stopped within 2h), skipping", token_id[:16])
            continue

        try:
            from execution import clob_client, live_db, live_executor
            from execution.risk_governor import RiskGovernor

            tick_size = clob_client.get_tick_size(token_id)
        except Exception as exc:
            logger.warning("sw_live: clob setup failed: %s", exc)
            continue

        # Use live BBO instead of stale alert price to avoid maker post-only rejection.
        # If BBO fetch fails, fall back to price_at_alert.
        # Also compute net_edge_taker from the ask side so the taker fallback
        # fires after the maker window if the signal is still fresh.
        net_edge_taker = 0.0
        try:
            from odds.polymarket_clob import get_orderbook

            book = get_orderbook(token_id)
            if book and getattr(book, "bids", None):
                live_bid = float(book.bids[0].price)
                # Post AT the bid — still a resting maker order (does not cross
                # the ask), but gets queue priority over bid-1-tick.
                entry_price = round(live_bid, 2)
                entry_price = max(0.01, min(0.99, entry_price))
            else:
                entry_price = price_at_alert
            # Taker edge: smart wallet entry vs current ask minus ~2% taker fee.
            # Goes negative if price has moved past their fill => taker gate blocks it.
            if book and getattr(book, "asks", None):
                live_ask = float(book.asks[0].price)
                net_edge_taker = round(price_at_alert - live_ask - 0.02, 4)
        except Exception:
            entry_price = price_at_alert

        # Suppress if market has moved too far from alert price (>15pp drift = stale signal)
        drift = abs(entry_price - price_at_alert)
        if drift > 0.15:
            logger.info(
                "sw_live: price drifted %.2f→%.2f (%.0fpp), skipping %s",
                price_at_alert,
                entry_price,
                drift * 100,
                condition_id[:16],
            )
            continue

        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        client_order_ref = f"sw-{date_str}-{condition_id[:16]}-{outcome_index}"

        # Dedup gate: block re-entry if this ref already exists in live_open_orders
        # (catches cancelled maker → taker re-fire on next poll, e.g. 2026-07-01 Rihanna pos #7)
        try:
            from execution import live_db as _live_db

            _chk_conn = _live_db.connect()
            try:
                row = _chk_conn.execute(
                    "SELECT status FROM live_open_orders WHERE client_order_ref = ? LIMIT 1", (client_order_ref,)
                ).fetchone()
                if row:
                    logger.info(
                        "sw_live: dedup — ref %s already in live_open_orders (status=%s), skipping",
                        client_order_ref,
                        row[0],
                    )
                    continue
            finally:
                _chk_conn.close()
        except Exception as _dup_exc:
            logger.warning("sw_live: dedup check failed: %s", _dup_exc)

        # Extract event_id for correlation guard (bypassed if gamma doesn't have it)
        event_id = ""
        if condition_id in gamma:
            gm = gamma[condition_id]
            event_id = str(gm.get("eventId") or gm.get("event_id") or "")

        intent = {
            "size_usd": size_usd,
            "market_id": token_id,
            "token_id": token_id,
            "side": "BUY",
            "event_id": event_id,
            "category": "smart_wallet",
        }

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
                fair_price=entry_price,
                size_usd=size_usd,
                tick_size=tick_size,
                neg_risk=bool(rec.get("neg_risk", False)),
                net_edge_taker=net_edge_taker,  # positive when fresh; taker fires after maker window if edge >= min_taker_edge
                client_order_ref=client_order_ref,
                category="smart_wallet",
                market_title=(gm_data.get("question") or rec.get("question") or "")[:120],
            )
            action = result.get("action")
            logger.info(
                "sw_live: %s → %s (entry=%.2f, alert=%.2f)", client_order_ref, action, entry_price, price_at_alert
            )
            # Instant Telegram alert on any fill
            if action in ("maker_filled", "taker_filled"):
                try:
                    from scripts.alert_formatter import send_telegram

                    liq = result.get("liquidity", action)
                    fill_price = result.get("price", entry_price)
                    usd = result.get("usd", 0.0)
                    fee = result.get("fee_paid", 0.0)
                    market_name = rec.get("question") or rec.get("market", token_id[:16])
                    # Enrich with full market name from gamma data
                    gm = gamma.get(condition_id, {})
                    gm_question = gm.get("question") or ""
                    gm_slug = gm.get("slug") or ""
                    display_name = gm_question or market_name
                    # Enrich with category + edge info from gamma data
                    gm = gamma.get(condition_id, {})
                    gm_category = gm.get("category") or ""
                    gm_slug = gm.get("slug") or ""
                    gm_question = gm.get("question") or ""
                    # Use the most descriptive name available
                    display_name = gm_question or market_name
                    emoji = "✅" if action == "maker_filled" else "⚡"
                    lines = [
                        f"{emoji} <b>LIVE FILL</b> ({liq.upper()})",
                        f"Market: {display_name}",
                        f"Side: BUY | Price: {fill_price:.2f} | Size: ${usd:.2f}",
                        f"Fee: ${fee:.4f} | Ref: {client_order_ref}",
                    ]
                    send_telegram("\n".join(lines))
                except Exception as tg_exc:
                    logger.warning("sw_live: telegram fill alert failed: %s", tg_exc)
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
            _route_live_smart_wallet(fired, gamma)
        except Exception as exc:
            logger.warning("smart_wallet_fast_poll: live routing failed: %s", exc)

    logger.info(
        "smart_wallet_fast_poll: %d trades scanned, %d sw fills, %d alerts fired",
        len(trades),
        len(sw_trades),
        len(fired),
    )
    return {
        "smart_wallets": len(smart),
        "trades_scanned": len(trades),
        "sw_fills": len(sw_trades),
        "alerts_fired": len(fired),
    }

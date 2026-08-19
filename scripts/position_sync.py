"""position_sync.py — Auto-detect manual Polymarket positions and register them.

Polls the PM data API for our deposit wallet, diffs against live_positions,
and auto-registers any positions not already tracked. Fires a Telegram alert
for each new position found.

Called by: scheduler.py task_position_sync() every 5 min.
"""
from __future__ import annotations

import logging
import time
import urllib.request
import json

logger = logging.getLogger(__name__)

_RESOLUTION_ALERT_CACHE_FILE = "/tmp/resolution_alert_cache.json"
_REDEMPTION_CACHE_FILE = "/tmp/redemption_attempt_cache.json"

def _already_resolution_alerted(position_id: int) -> bool:
    import json, os
    try:
        if not os.path.exists(_RESOLUTION_ALERT_CACHE_FILE):
            return False
        data = json.loads(open(_RESOLUTION_ALERT_CACHE_FILE).read())
        return str(position_id) in data
    except Exception:
        return False

def _mark_resolution_alerted(position_id: int):
    import json, os
    try:
        data = {}
        if os.path.exists(_RESOLUTION_ALERT_CACHE_FILE):
            data = json.loads(open(_RESOLUTION_ALERT_CACHE_FILE).read())
        data[str(position_id)] = True
        open(_RESOLUTION_ALERT_CACHE_FILE, "w").write(json.dumps(data))
    except Exception:
        pass



def _already_redemption_attempted(position_id: int) -> bool:
    import json, os
    try:
        if not os.path.exists(_REDEMPTION_CACHE_FILE):
            return False
        data = json.loads(open(_REDEMPTION_CACHE_FILE).read())
        return str(position_id) in data
    except Exception:
        return False

def _mark_redemption_attempted(position_id: int):
    import json, os
    try:
        data = {}
        if os.path.exists(_REDEMPTION_CACHE_FILE):
            data = json.loads(open(_REDEMPTION_CACHE_FILE).read())
        data[str(position_id)] = True
        open(_REDEMPTION_CACHE_FILE, "w").write(json.dumps(data))
    except Exception:
        pass

def _try_redeem_position(market_id: str, position_id: int) -> str:
    """Attempt on-chain redemption of settled YES tokens via SDK.

    Uses condition_id = market_id (standard Polymarket markets).
    One-shot per position (cache-guarded). Returns "ok", "skipped", or "error:…".
    Failure is non-fatal — resolution DB update has already committed.
    """
    if _already_redemption_attempted(position_id):
        return "skipped_already_attempted"
    _mark_redemption_attempted(position_id)
    try:
        from execution.clob_client import _get_client
        client = _get_client()
        handle = client.redeem_positions(condition_id=market_id)
        handle.wait()
        logger.info("position_sync: redeemed pos %d market %s", position_id, market_id[:16])
        return "ok"
    except Exception as exc:
        logger.warning("position_sync: redeem failed for pos %d: %s", position_id, exc)
        return f"error:{exc}"

_DEPOSIT_WALLET = "0xa495c42d60521ee28e1da237c0bab560d5095777"
_PM_POSITIONS_URL = f"https://data-api.polymarket.com/positions?user={_DEPOSIT_WALLET}&sizeThreshold=0.01"


def _fetch_pm_positions() -> list[dict]:
    """Fetch open positions via SDK (typed, no raw REST parsing)."""
    try:
        from execution.clob_client import _get_client
        client = _get_client()
        positions = list(client.list_positions(size_threshold=0.01))
        result = []
        for p in positions:
            result.append({
                "asset":        str(getattr(p, "asset",         "") or ""),
                "conditionId":  str(getattr(p, "condition_id",  "") or ""),
                "slug":         str(getattr(p, "slug",          "") or ""),
                "title":        str(getattr(p, "title",         "") or ""),
                "avgPrice":     float(getattr(p, "avg_price",   0) or 0),
                "size":         float(getattr(p, "size",        0) or 0),
                "initialValue": float(getattr(p, "initial_value", 0) or 0),
                "curPrice":     float(getattr(p, "cur_price",   0) or 0),
                "cashPnl":      float(getattr(p, "cash_pnl",    0) or 0),
                "redeemable":   bool(getattr(p,  "redeemable",  False)),
            })
        return result
    except Exception as exc:
        logger.warning("position_sync: SDK list_positions failed (%s), falling back to REST", exc)
        # Fallback to raw REST
        try:
            req = urllib.request.Request(_PM_POSITIONS_URL, headers={"User-Agent": "polyclawd/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode()) or []
        except Exception as exc2:
            logger.warning("position_sync: REST fallback also failed: %s", exc2)
            return []


def _fetch_gamma_market(market_id: str) -> dict:
    """Fetch a single market from Gamma API to check resolution."""
    try:
        url = f"https://gamma-api.polymarket.com/markets/{market_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode()) or {}
    except Exception as exc:
        logger.debug("position_sync: gamma fetch failed for %s: %s", market_id[:16], exc)
        return {}


def _sdk_token_price_map() -> dict:
    """token_id -> (cur_price, redeemable) from SDK open positions. {} on failure."""
    out = {}
    try:
        from execution.clob_client import _get_client
        client = _get_client()
        for page in client.list_positions(size_threshold=0.001):
            for sdk_pos in page.items:
                t = str(getattr(sdk_pos, "token_id", "") or "")
                if t:
                    out[t] = (getattr(sdk_pos, "cur_price", None),
                              bool(getattr(sdk_pos, "redeemable", False)))
    except Exception as exc:
        logger.debug("position_sync: sdk position map failed: %s", exc)
    return out


def _fetch_redeem_assets() -> set:
    """Asset (token) ids with a REDEEM row in our wallet's data-api activity."""
    try:
        url = f"https://data-api.polymarket.com/activity?user={_DEPOSIT_WALLET}&limit=500"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        acts = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
        return {str(a.get("asset") or "") for a in acts if a.get("type") == "REDEEM"}
    except Exception as exc:
        logger.debug("position_sync: redeem activity fetch failed: %s", exc)
        return set()


def check_resolutions(conn) -> list[dict]:
    """Check all open live_positions for market resolution.

    If outcomePrices = ["1","0"] or ["0","1"], the market has settled.
    Marks position closed and fires Telegram alert.
    Returns list of resolved position dicts.
    """
    from datetime import datetime, timezone

    rows = conn.execute(
        "SELECT id, market_id, market_title, token_id, side, entry_price, "
        "shares, cost_usd, fee_paid_total, archetype, opened_at "
        "FROM live_positions WHERE status='open'"
    ).fetchall()

    sdk_price_map = _sdk_token_price_map()
    redeem_assets = _fetch_redeem_assets()

    resolved = []
    for row in rows:
        pos_id = row[0]
        market_id = row[1] or ""
        market_title = row[2] or row[1] or "Unknown"
        side = row[4] or "BUY"
        entry_price = float(row[5] or 0)
        shares = float(row[6] or 0)
        cost_usd = float(row[7] or 0)
        fee_total = float(row[8] or 0)

        if not market_id:
            continue
        if _already_resolution_alerted(pos_id):
            continue

        token_id = str(row[3] or market_id)
        if token_id not in sdk_price_map and token_id in redeem_assets:
            # Position gone from wallet + a REDEEM event exists → it WON and
            # was redeemed on-chain; the books never heard (pos 8/12, Jul 15).
            pnl = round((1.0 - entry_price) * shares - fee_total, 4)
            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE live_positions SET status='closed', closed_at=?, exit_price=1.0, "
                "pnl=?, close_reason='redeemed_detected' WHERE id=?",
                (now_iso, pnl, pos_id))
            conn.commit()
            resolved.append({"id": pos_id, "market_title": market_title, "pnl": pnl,
                             "entry_price": entry_price, "exit_price": 1.0, "shares": shares,
                             "opened_at": row[10], "result_emoji": "\U0001f3c6", "result_label": "WIN (redeemed)"})
            _mark_resolution_alerted(pos_id)
            continue

        # Resolution strategy:
        # 1. Try SDK list_positions() — cross-reference token_id to get cur_price + redeemable
        #    (handles Endgame/PM-US markets where decimal token_id breaks get_market())
        # 2. Fall back to get_market(hex_token_id) then Gamma REST
        p0, p1 = None, None
        try:
            from execution.clob_client import _get_client
            client = _get_client()

            if token_id in sdk_price_map:
                cur_price, redeemable = sdk_price_map[token_id]
                if cur_price is not None and redeemable:
                    # Resolved: cur_price=0→NO, cur_price≥0.99→YES
                    if float(cur_price) >= 0.99:
                        p0, p1 = 1.0, 0.0
                    elif float(cur_price) <= 0.01:
                        p0, p1 = 0.0, 1.0
            else:
                # Not in SDK positions — may have already been redeemed or is CLOB market
                # Fall back to get_market with hex token_id
                token_hex = hex(int(token_id)) if token_id.isdigit() else token_id
                try:
                    mkt_obj = client.get_market(id=token_hex)
                    outcome_prices = getattr(mkt_obj, "outcome_prices", None)
                    if outcome_prices and len(outcome_prices) >= 2:
                        p0 = float(outcome_prices[0])
                        p1 = float(outcome_prices[1])
                except Exception:
                    mkt = _fetch_gamma_market(token_hex)
                    prices_raw = mkt.get("outcomePrices")
                    if isinstance(prices_raw, list) and len(prices_raw) >= 2:
                        try:
                            p0 = float(prices_raw[0])
                            p1 = float(prices_raw[1])
                        except (ValueError, TypeError):
                            pass
        except Exception as _sdk_exc:
            logger.debug("position_sync: SDK resolution check failed: %s", _sdk_exc)

        if p0 is None or p1 is None:
            continue
        if not ((p0 == 1.0 and p1 == 0.0) or (p0 == 0.0 and p1 == 1.0)):
            continue  # not settled

        # Determine WIN/LOSS — we always buy YES (BUY side = YES token)
        yes_won = (p0 == 1.0)
        won = yes_won  # BUY = YES position

        if won:
            pnl = round((1.0 - entry_price) * shares - fee_total, 4)
            exit_price = 1.0
            result_emoji = "🏆"
            result_label = "WIN"
        else:
            pnl = round(-cost_usd, 4)
            exit_price = 0.0
            result_emoji = "💀"
            result_label = "LOSS"

        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE live_positions SET status='closed', closed_at=?, exit_price=?, "
            "pnl=?, close_reason='resolution' WHERE id=?",
            (now_iso, exit_price, pnl, pos_id)
        )
        conn.commit()
        logger.info("position_sync: resolution %s for %s | pnl=%+.4f", result_label, market_title[:50], pnl)
        _mark_resolution_alerted(pos_id)

        # Auto-redeem WIN positions to recover USDC on-chain (non-fatal if fails)
        if won:
            _try_redeem_position(market_id, pos_id)

        # CLV tracking: closing_price - fill_price (positive = bought below closing line)
        # Non-fatal; Gate 2 depends on this but resolution logic must not block.
        token_id_for_clv = row[3]  # row[3] = token_id
        try:
            from execution.clob_client import _get_client as _clv_client
            from execution.live_db import insert_fill_clv
            ltp = _clv_client().get_last_trade_price(token_id=token_id_for_clv)
            closing_price = round(float(ltp.price), 6)
            clv_pp = round(closing_price - entry_price, 6)
            insert_fill_clv(
                conn,
                position_id=pos_id,
                token_id=token_id_for_clv,
                fill_price=entry_price,
                closing_price=closing_price,
                clv_pp=clv_pp,
                resolved_at=now_iso,
            )
            logger.info(
                "position_sync: CLV pos %d fill=%.4f close=%.4f clv=%+.4f",
                pos_id, entry_price, closing_price, clv_pp,
            )
        except Exception as _clv_exc:
            logger.debug("position_sync: CLV tracking failed (non-fatal): %s", _clv_exc)

        resolved.append({
            "pos_id": pos_id,
            "market_title": market_title,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "shares": shares,
            "pnl": pnl,
            "won": won,
            "result_emoji": result_emoji,
            "result_label": result_label,
            "opened_at": row[10] if len(row) > 10 else "",
        })

    return resolved


def check_wallet_balance(conn) -> None:
    """Alert if CLOB collateral balance is below $5 (can't fill min order).

    Uses get_balance_allowance(asset_type='COLLATERAL') — the actual USDC in
    the exchange, NOT the portfolio/positions API which only shows open positions.
    """
    _MIN_BALANCE_USD = 5.0
    _CACHE_FILE = "/tmp/low_balance_alerted.txt"
    import os, time
    try:
        from execution.clob_client import _get_client
        client = _get_client()
        bal_obj = client.get_balance_allowance(asset_type="COLLATERAL")
        raw = getattr(bal_obj, "balance", None)
        if raw is None:
            return
        balance = float(raw) / 1e6
    except Exception as exc:
        logger.debug("position_sync: balance check failed: %s", exc)
        return

    if balance < _MIN_BALANCE_USD:
        now = time.time()
        last_ts = 0.0
        try:
            if os.path.exists(_CACHE_FILE):
                last_ts = float(open(_CACHE_FILE).read().strip())
        except Exception:
            pass
        if now - last_ts > 3600:
            open(_CACHE_FILE, "w").write(str(now))
            try:
                from scripts.alert_formatter import send_telegram
                send_telegram(
                    f"⚠️ <b>LOW WALLET BALANCE</b>\n"
                    f"CLOB balance: ${balance:.2f} USDC\n"
                    f"Minimum order requires ~$3. Top up deposit wallet: {_DEPOSIT_WALLET[:10]}..."
                )
            except Exception as tg_exc:
                logger.warning("position_sync: balance alert failed: %s", tg_exc)
    else:
        if os.path.exists(_CACHE_FILE):
            try:
                os.remove(_CACHE_FILE)
            except Exception:
                pass

def _get_tracked_token_ids(conn) -> set[str]:
    rows = conn.execute(
        "SELECT token_id FROM live_positions WHERE status='open'"
    ).fetchall()
    tracked = {r[0] for r in rows}
    # Also exclude tokens with active open orders (executor placed, not yet filled)
    order_rows = conn.execute(
        "SELECT token_id FROM live_open_orders WHERE status='live'"
    ).fetchall()
    tracked |= {r[0] for r in order_rows}
    return tracked


def sync_positions(conn) -> list[dict]:
    """Fetch PM positions, register any untracked ones.

    Returns list of newly registered position dicts.
    """
    from datetime import datetime, timezone

    pm_positions = _fetch_pm_positions()
    if not pm_positions:
        return []

    tracked = _get_tracked_token_ids(conn)
    new_positions = []

    for pos in pm_positions:
        token_id = str(pos.get("asset", ""))
        if not token_id or token_id in tracked:
            continue

        # Skip resolved markets
        if pos.get("redeemable") or float(pos.get("size", 0)) <= 0:
            continue

        market_id = str(pos.get("conditionId", ""))
        market_slug = str(pos.get("slug", ""))
        market_title = str(pos.get("title", market_slug))
        entry_price = float(pos.get("avgPrice", 0))
        shares = float(pos.get("size", 0))
        cost_usd = float(pos.get("initialValue", entry_price * shares))

        conn.execute(
            "INSERT INTO live_positions "
            "(opened_at, market_id, market_slug, market_title, token_id, side, "
            "entry_price, shares, cost_usd, status, fee_paid_total, archetype) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                datetime.now(timezone.utc).isoformat(),
                market_id,
                market_slug,
                market_title,
                token_id,
                "BUY",
                entry_price,
                shares,
                cost_usd,
                "open",
                0.0,
                "manual",
            ),
        )
        conn.commit()
        logger.info("position_sync: registered manual position %s @ %.2f", market_title, entry_price)

        new_positions.append({
            "title": market_title,
            "token_id": token_id,
            "entry_price": entry_price,
            "shares": shares,
            "cost_usd": cost_usd,
            "cur_price": float(pos.get("curPrice", entry_price)),
            "cash_pnl": float(pos.get("cashPnl", 0)),
        })

    return new_positions


def sync_open_orders(conn) -> dict:
    """Reconcile live_open_orders against CLOB.

    Fetches current open orders from CLOB via SDK, compares against DB rows
    with status='live'. Any DB-live orders NOT in CLOB are marked 'cancelled'
    (filled externally, expired, or cancelled elsewhere).

    Returns: {'reconciled': N, 'cancelled_stale': N}
    """
    try:
        from execution.clob_client import _get_client
        client = _get_client()
        clob_orders = list(client.list_open_orders())
        clob_ids = {str(getattr(o, "id", "") or "") for o in clob_orders}
    except Exception as exc:
        logger.warning("position_sync: sync_open_orders CLOB fetch failed: %s", exc)
        return {"reconciled": 0, "cancelled_stale": 0, "error": str(exc)}

    db_rows = conn.execute(
        "SELECT id, order_id, token_id FROM live_open_orders WHERE status='live'"
    ).fetchall()

    cancelled_stale = 0
    for row in db_rows:
        db_order_id = str(row[1] or "")
        if db_order_id and db_order_id not in clob_ids:
            conn.execute(
                "UPDATE live_open_orders SET status='cancelled' WHERE id=?",
                (row[0],)
            )
            cancelled_stale += 1
            logger.info("position_sync: stale order %s not in CLOB → marked cancelled", db_order_id[:16])

    if cancelled_stale:
        conn.commit()

    return {"reconciled": len(db_rows), "cancelled_stale": cancelled_stale}


def run() -> dict:
    """Entry point called by scheduler. Returns summary dict."""
    try:
        from execution import live_db
        conn = live_db.connect()
    except Exception as exc:
        logger.warning("position_sync: db connect failed: %s", exc)
        return {"new": 0, "error": str(exc)}

    try:
        # Check for market resolutions first
        resolved = check_resolutions(conn)
        if resolved:
            try:
                from scripts.alert_formatter import send_telegram
                for r in resolved:
                    pnl_str = f"${r['pnl']:+.2f}"
                    # Time held
                    opened = r.get("opened_at", "")
                    time_held = ""
                    if opened:
                        try:
                            from datetime import datetime, timezone
                            opened_dt = datetime.fromisoformat(opened)
                            held_h = (datetime.now(timezone.utc) - opened_dt).total_seconds() / 3600
                            time_held = f" | Held {held_h:.1f}h"
                        except Exception:
                            pass
                    lines = [
                        f"{r['result_emoji']} <b>POSITION RESOLVED — {r['result_label']}</b>",
                        f"Market: {r['market_title']}",
                        f"Entry: {r['entry_price']:.2f} → Exit: {r['exit_price']:.2f} | Shares: {r['shares']:.1f}{time_held}",
                        f"PnL: {pnl_str}",
                    ]
                    send_telegram("\n".join(lines))
            except Exception as tg_exc:
                logger.warning("position_sync: resolution alert failed: %s", tg_exc)

        # Check wallet balance and sync bankroll to governor
        check_wallet_balance(conn)

        # Sync bankroll: CLOB liquid + deployed cost = true bankroll
        try:
            from execution.clob_client import _get_client
            from execution import live_db
            from execution.risk_governor import RiskGovernor
            from execution import live_config
            clob_bal_raw = _get_client().get_balance_allowance(asset_type="COLLATERAL").balance
            clob_liquid = float(clob_bal_raw) / 1e6
            # deployed = sum of cost_usd for open live positions
            deployed_row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM live_positions WHERE status='open'"
            ).fetchone()
            deployed = float(deployed_row[0] or 0)
            true_bankroll = clob_liquid + deployed
            # Update governor in-memory + persist
            gov_conn = live_db.connect()
            gov = RiskGovernor(gov_conn, mode=live_config.mode())
            gov.set_bankroll(true_bankroll)
            # Also sync deployed_usd so governor cap math reflects manual positions
            gov.set_deployed(deployed)
            gov_conn.close()
            logger.info("position_sync: bankroll synced → $%.2f (liquid $%.2f + deployed $%.2f)",
                        true_bankroll, clob_liquid, deployed)
        except Exception as bk_exc:
            logger.debug("position_sync: bankroll sync failed: %s", bk_exc)

        # Reconcile open orders vs CLOB
        order_sync = sync_open_orders(conn)
        if order_sync.get("cancelled_stale", 0):
            logger.info("position_sync: cancelled %d stale orders", order_sync["cancelled_stale"])

        new = sync_positions(conn)
        if new:
            try:
                from scripts.alert_formatter import send_telegram
                for p in new:
                    pnl = p["cash_pnl"]
                    pnl_str = f"${pnl:+.2f}" if pnl else "~$0.00"
                    lines = [
                        f"\U0001f4e1 <b>MANUAL POSITION DETECTED</b>",
                        f"Market: {p['title']}",
                        f"Side: BUY | Entry: {p['entry_price']:.2f} | Now: {p['cur_price']:.2f}",
                        f"Shares: {p['shares']:.1f} | Cost: ${p['cost_usd']:.2f} | PnL: {pnl_str}",
                        f"<i>Registered in live_positions — stop evaluator now active.</i>",
                    ]
                    send_telegram("\n".join(lines))
            except Exception as tg_exc:
                logger.warning("position_sync: telegram alert failed: %s", tg_exc)

        return {"new": len(new), "resolved": len(resolved), "cancelled_stale": order_sync.get("cancelled_stale", 0), "titles": [p["title"] for p in new]}
    except Exception as exc:
        logger.error("position_sync: sync failed: %s", exc)
        return {"new": 0, "error": str(exc)}
    finally:
        try:
            conn.close()
        except Exception:
            pass

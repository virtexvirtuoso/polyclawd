"""
Stop-Loss Evaluator — Phase 1 of Adaptive Exit System

Conservative fixed stops for now. Will transition to learned/adaptive
exits once position_price_log has enough resolved trade trajectories.

Conservative defaults (Phase 1):
- Exit if unrealized loss > 50% of bet size
- Exit if edge flipped negative (signal reversal)

Called from scheduler tick_5min().
"""

import sqlite3
import json
import urllib.request
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from loguru import logger
from config.polymarket_urls import CLOB_API  # polyproxy: central URL config

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "storage" / "shadow_trades.db"

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

# ── UNIVERSAL STOP THRESHOLD ──────────────────────────────────────────────
# Applies to ALL live trades regardless of strategy/archetype.
# Checked FIRST in both evaluate_stops() and evaluate_stops_urgent().
# No strategy-specific override can exceed this — it's the hard floor.
# Env override: UNIVERSAL_MAX_LOSS_PCT for rapid rollback without redeploy.
UNIVERSAL_MAX_LOSS_PCT = float(os.getenv("UNIVERSAL_MAX_LOSS_PCT", "0.40"))

# ── PRE-RESOLUTION WARNING ─────────────────────────────────────────────────
# Fire a Telegram warning when a position is within this many hours of
# resolution and has an unrealized loss above this threshold.
# Gives you a window to manually exit before the market resolves at 0.
# MUST default below UNIVERSAL_MAX_LOSS_PCT: at 0.40/0.40 the universal stop
# (checked first, with `continue`) closed every qualifying position before the
# warning could evaluate true — the warning branch was structurally dead code
# (Alert System Overhaul 2026-07-16, Task 0.2 hypothesis d / Task 2.0).
PRE_RESOLVE_WARN_HOURS = float(os.getenv("PRE_RESOLVE_WARN_HOURS", "6.0"))
PRE_RESOLVE_WARN_LOSS_PCT = float(os.getenv("PRE_RESOLVE_WARN_LOSS_PCT", "0.30"))

# ── Stop Config ──────────────────────────────────────────────────────────
# 2026-04-27 weather recalibration. Diagnostic on n=440 weather trades:
#   * Model is calibrated (mean P(NO) 0.80 vs realized 0.75; CLV +36¢/contract).
#   * 30% standard stop fired 151× and cost ~$66k of expected P&L.
#   * Live books are 7–10pp wide with $25–50 of depth per 5pp slice. A single
#     $500 market order moves the price 5–10pp, generating fake "20–30% losses"
#     that revert when the maker refills.
#   * 70% of winner-dips ≥20% mean-revert in <30 min; 94% within 1h.
# Solution: weather positions further than 6h from resolution skip the
# threshold-based stop and defer to reeval_weather_positions(), which has
# fresh ensemble data. The threshold here is a HARD-CAP safety net only.
# Inside 6h, the urgent path (25% with a 2h dead zone, calibrated 2026-04-23)
# remains in charge.
#
# 2026-05-15 weather HARD-CAP widening: live tracker showed mid-life HARD-CAP
# (0.50) was firing ~10×/day at ~$2.5k/day cost despite the defer-to-reeval
# logic — the reeval doesn't always close fast enough to prevent 50% drawdowns.
# ENSEMBLE_AUDIT_2026-05-14 stop-sensitivity backtest on 375 resolved stops
# found held-WR rises monotonically with loss-bucket depth: 30-40% wins 68.9%
# held, 50-70% wins 73.9%, >=70% wins 93.1%. Same widening recommendation
# applies to BOTH URGENT and HARD-CAP paths. Widened 0.50 -> 0.70.
# Env override: WEATHER_HARD_CAP_MAX_LOSS_PCT for rapid rollback without redeploy.
# See ENSEMBLE_AUDIT_2026-05-15_02_Stop-Sensitivity-Backtest.md

STOP_CONFIG = {
    "default": {
        "max_loss_pct": 0.50,      # hard-cap safety net (any strategy)
        "edge_floor": -0.02,       # exit if current edge < -2pp (signal flipped)
        "defer_to_reeval_above_h": 0,   # 0 = no deferral, threshold rules
    },
    # Weather: defer mid-life stops to the model-aware path
    "weather": {
        "max_loss_pct": float(os.getenv("WEATHER_HARD_CAP_MAX_LOSS_PCT", "0.70")),  # widened 2026-05-15 from 0.50; env-tunable for rollback
        "edge_floor": -0.02,
        "defer_to_reeval_above_h": 6.0,  # >6h to close: skip threshold stop,
                                          # let reeval (ensemble-aware) decide
    },
    "tweet_count_mc": {
        "max_loss_pct": 0.50,
        "edge_floor": -0.02,
    },
}

# ── Phase 1.5: Info-Lock Post-Lock Stop Tier (toggle-gated) ──────────────
# Once a position enters its "info-lock window" (the final hours before the
# resolution data is physically finalized), price volatility collapses. Tight
# stops in that window catch capitulation without touching mid-session dips.
#
# Empirical backtest (2026-03-17 → 2026-04-10, n=40 live-stopped weather
# trades + 14 weather winners): `(info_lock=3h, post_lock_max_loss=12%)` is
# optimal at +$1,247 net. Winners show max post-lock drawdown of 10.4%, so a
# 12% threshold leaves a 1.6pp safety margin.
#
# Disabled by default. Enable via POST /api/engine/stop-curve.
POST_LOCK_CONFIG = {
    "weather": {
        "info_lock_before_close_h": 3.0,
        "max_loss_pct": 0.12,
    },
}

# Cooldown: don't re-alert on same position within N minutes
ALERT_COOLDOWN_MINUTES = 60
_ALERT_CACHE_FILE = Path('/tmp/stop_alert_cache.json')

def _load_alert_cache() -> dict:
    try:
        if _ALERT_CACHE_FILE.exists():
            raw = json.loads(_ALERT_CACHE_FILE.read_text())
            return {k: datetime.fromisoformat(v) for k, v in raw.items()}
    except Exception:
        pass
    return {}

def _save_alert_cache(cache: dict) -> None:
    try:
        _ALERT_CACHE_FILE.write_text(json.dumps({k: v.isoformat() for k, v in cache.items()}))
    except Exception:
        pass

def _db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn

def _fetch_url(url, timeout=10):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug("Stop evaluator fetch failed {}: {}", url, e)
        return None

def _fetch_price(pos):
    """Fetch current YES token price. Returns (position_id, price_or_None)."""
    market_id = pos["market_id"]
    platform = pos.get("platform") or "kalshi"

    if platform == "polymarket" or market_id.startswith("0x"):
        data = _fetch_url(f"{CLOB_API}/markets/{market_id}")
        if data:
            tokens = data.get("tokens", [])
            if tokens:
                return (pos["id"], float(tokens[0].get("price", 0)))
    else:
        data = _fetch_url(f"{KALSHI_API}/markets/{market_id}")
        if data:
            market = data.get("market", data)
            # Fractional markets null legacy cents fields; *_dollars first
            cp = market.get("last_price_dollars")
            if cp not in (None, ""):
                return (pos["id"], float(cp))
            cp = market.get("last_price")
            if cp and cp > 1:
                cp = cp / 100
            return (pos["id"], cp)

    return (pos["id"], None)

def _compute_unrealized_pnl(side, entry_price, current_yes_price, bet_size):
    """Compute unrealized P&L if we sold at current price."""
    if side == "YES":
        # Bought YES at entry_price, current value = current_yes_price
        return bet_size * (current_yes_price / entry_price - 1)
    else:
        # Bought NO at (1 - entry_price), current NO value = (1 - current_yes_price)
        no_entry = 1 - entry_price
        no_current = 1 - current_yes_price
        return bet_size * (no_current / no_entry - 1) if no_entry > 0 else 0

def _get_config(strategy):
    """Get stop config for a strategy, falling back to defaults."""
    return STOP_CONFIG.get(strategy, STOP_CONFIG["default"])

def _load_engine_state():
    """Read engine state once per tick; used to check toggles."""
    try:
        from api.routes.engine import load_engine_state
        return load_engine_state() or {}
    except Exception:
        return {}

def _post_lock_threshold(strategy, pos, now):
    """
    Return the post-lock max_loss_pct if the position is INSIDE its info-lock
    window and the engine toggle is on. Otherwise return None.

    info_lock window = [closed_at_estimate - info_lock_before_close_h, closed_at_estimate]
    closed_at_estimate = _parse_market_date(title) (conservative 23:59 UTC).
    """
    cfg = POST_LOCK_CONFIG.get(strategy)
    if not cfg:
        return None
    target = _parse_market_date(pos.get("market_title") or "")
    if not target:
        return None
    lock_at = target.timestamp() - (cfg["info_lock_before_close_h"] * 3600)
    if now.timestamp() < lock_at:
        return None
    return float(cfg["max_loss_pct"])

def _should_alert(position_id):
    """Check cooldown — avoid spamming alerts for same position."""
    now = datetime.now(timezone.utc)
    cache = _load_alert_cache()
    last = cache.get(position_id)
    if last and (now - last).total_seconds() < ALERT_COOLDOWN_MINUTES * 60:
        return False
    cache[position_id] = now
    _save_alert_cache(cache)
    return True

try:
    from odds.gamma_title import outcome_label as _side_label
except Exception:  # alert cosmetics must never break this module
    def _side_label(_token_id):
        return "?"


def _display_title(raw_title, market_id):
    """Best human-readable title for alerts (Task 3.3, hex-ID fix).

    Returns the row title unless it is empty or hex-like, in which case the
    Gamma title resolver is tried (cached in shadow_trades.db; returns None
    for non-0x ids and on any error — it never raises). Last resort is the
    raw title (if any) or the truncated market_id.
    """
    title = (raw_title or "").strip()
    if title and not title.startswith("0x"):
        return title
    try:
        from odds.gamma_title import resolve_title
        resolved = resolve_title(market_id)
    except Exception:
        resolved = None
    return resolved or title or str(market_id or "")[:24]

# The executor's own action names. Anything else is not something it said.
_EXECUTOR_ACTIONS = ("maker_closed", "taker_closed", "partial_closed", "held_remainder")

# Shares are compared with this tolerance when deciding "all sold" (the executor
# itself uses 1e-9; a float residue never counts as a position left open).
_SHARES_EPS = 1e-6


def _num(value, default=0.0):
    """A finite float from *value*, else *default*. Never raises."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if f == f and abs(f) != float("inf") else default


def _fire_stop(live_pos_row, result, mark, reason):
    """The one alert for a live close: the same text to Discord and to Telegram.

    Reads result["action"] and the executor's own figures (shares_sold,
    exit_price, pnl, fee_paid) and says what actually happened:
      * every share sold          -> CLOSED
      * some, but not all, sold   -> PARTLY CLOSED, and how many shares remain
      * none sold (held_remainder)-> NOT SOLD, and how many shares are still held
    "How many were sold" is judged from the shares, not from the action's name, so
    a held_remainder (or a short taker fill named partial_closed) is never reported
    as a close (#17). exit_failed, exit_unknown and anything else that is not one
    of the executor's four actions send nothing here: the tick wrapper pages those.

    Both channels are always attempted. Returns a list of failure strings, one per
    channel that failed (empty when both were sent); never raises for a channel."""
    action = result.get("action")
    if action not in _EXECUTOR_ACTIONS:
        return []
    held = _num(live_pos_row.get("shares"))
    sold = _num(result.get("shares_sold"))
    remaining = max(0.0, held - sold)
    price = _num(result.get("exit_price"), mark)
    pnl = _num(result.get("pnl"))
    fee = _num(result.get("fee_paid"))
    entry = _num(live_pos_row.get("entry_price"))
    title = _display_title(live_pos_row.get("market_title"),
                           str(live_pos_row.get("market_id") or ""))[:70]
    side = _side_label(live_pos_row.get("token_id"))
    figures = (f"Side: {side} | Entry: {entry:.4f} -> Exit: {price:.4f} "
               f"(stop price {mark:.4f})\n"
               f"PnL: ${pnl:+.2f} | Fee: ${fee:.4f}\n"
               f"Reason: {reason}")
    if sold <= _SHARES_EPS:
        head = f"🛑 STOP-LOSS NOT SOLD — {title}"
        text = (f"{head}\nThe stop fired but no shares were sold: all {held:.2f} shares "
                f"are still held.\n{figures}")
        if result.get("hard_stop_blocked"):
            text += "\nThe hard stop could not execute (position below the exchange minimum)."
        color_red = False
    elif remaining <= _SHARES_EPS:
        head = f"🛑 STOP-LOSS CLOSED — {title}"
        text = f"{head}\nSold {sold:.2f} of {held:.2f} shares.\n{figures}"
        color_red = True
    else:
        head = f"🛑 STOP-LOSS PARTLY CLOSED — {title}"
        text = (f"{head}\nThe position is not fully closed: sold {sold:.2f} of {held:.2f} "
                f"shares, {remaining:.2f} shares remain open.\n{figures}")
        if result.get("hard_stop_blocked"):
            text += "\nThe rest could not be sold (below the exchange minimum)."
        color_red = False

    failures = []

    def _failed(channel, exc):
        logger.error("_fire_stop: {} alert failed for position {} ({}): {}: {}",
                     channel, live_pos_row.get("id"), action, type(exc).__name__, exc)
        failures.append(f"{type(exc).__name__}: {exc} [{channel}]")

    try:
        from signals.discord_alerts import _send, COLOR_ORANGE, COLOR_RED
        if not _send([{
            "title": head[:250],
            "description": text,
            "color": COLOR_RED if color_red else COLOR_ORANGE,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "Stop-Loss Monitor"},
        }], alert_type="stop_loss", alert_meta={
            "market": title, "action": action, "shares_sold": sold,
            "shares_remaining": remaining, "pnl": pnl, "reason": reason,
        }):
            raise RuntimeError("the Discord sender reported failure")
    except Exception as exc:
        _failed("discord", exc)
    try:
        from scripts.openclaw_alerts import alert_openclaw
        if not alert_openclaw(text, parse_mode=None):
            raise RuntimeError("the Telegram sender reported failure")
    except Exception as exc:
        _failed("telegram", exc)
    return failures


def _close_live_position_early(live_pos_row, mark, reason, *, hard_cap_frac):
    """Sell a live position through execute_exit, at the mark it is given.

    The mark is the price the stop decided on (the best bid of the held token).
    It is the only price used: nothing here fetches an order book or re-marks.
    (execute_exit prices its maker leg at this mark; its taker leg is priced by
    the exchange call, not by this function.)

    Always returns a dict, in three stages with separate failure handling:
      1. before the sale (checking the mark, tick size, database, governor): any
         exception returns {"action": "exit_failed", "error": "<Type>: <msg>",
         "stage": "pre"}. No sale was attempted.
      2. the sale, the single execute_exit call: an exception PROPAGATES. A sale
         was attempted and its outcome is unknown; the caller labels that
         exit_unknown. If execute_exit returns without one of its four actions the
         result is {"action": "exit_unknown", "error": ..., "raw": repr[:200]}.
      3. after the sale (log line, share-count bookkeeping, alerts, cooldown): an
         exception is logged at ERROR and recorded in result["post_error"]; the
         result still reports what execute_exit returned.

    Parameters
    ----------
    live_pos_row : dict
        Row from live_positions (id, market_id, token_id, entry_price, shares,
        cost_usd, neg_risk, ...).
    mark : float
        The price to sell at; the same number the stop decided on.
    reason : str
        Human-readable trigger reason.
    hard_cap_frac : float
        Loss fraction at which execute_exit treats the stop as genuine and crosses
        taker for what the maker leg did not sell; below it the remainder is held.
    """
    market_id = str(live_pos_row.get("market_id") or "")
    position_id = live_pos_row.get("id")

    # ---- stage 1: before the sale --------------------------------------------
    conn = None
    try:
        from execution import live_config, live_db, risk_governor
        from execution.live_executor import execute_exit

        if not _usable_price(mark):
            raise ValueError(f"mark is not a usable price: {mark!r}")
        mark_price = float(mark)
        token_id = live_pos_row.get("token_id") or ""

        # Get tick_size for the token — default 0.01 if unknown.
        try:
            from execution.clob_client import get_tick_size
            tick_size = get_tick_size(token_id) if token_id else 0.01
        except Exception:
            tick_size = 0.01

        # I4: derive category and strategy label from archetype so non-weather
        # live strategies (when added) route through the correct fee tier.
        archetype = live_pos_row.get("archetype") or "weather"
        conn = live_db.connect()
        gov = risk_governor.RiskGovernor(conn, mode=live_config.mode())
    except Exception as exc:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        logger.error("_close_live_position_early: position {} failed BEFORE the sale, "
                     "nothing was sold: {}: {}", position_id, type(exc).__name__, exc)
        return {"position_id": position_id, "action": "exit_failed",
                "error": f"{type(exc).__name__}: {exc}", "stage": "pre"}

    # ---- stage 2: the sale ---------------------------------------------------
    try:
        exit_result = execute_exit(
            conn,
            gov,
            position_row=live_pos_row,
            mark_price=mark_price,
            tick_size=tick_size,
            hard_cap_frac=hard_cap_frac,
            reason=reason,
            category=archetype,
        )
    except Exception as exc:
        logger.error("_close_live_position_early: the sale call for position {} RAISED, "
                     "outcome unknown: {}: {}", position_id, type(exc).__name__, exc)
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass

    exit_action = exit_result.get("action") if isinstance(exit_result, dict) else None
    if exit_action not in _EXECUTOR_ACTIONS:
        logger.error("_close_live_position_early: position {} sale call returned no usable "
                     "action, outcome unknown: {!r}", position_id, exit_result)
        return {"position_id": position_id, "action": "exit_unknown",
                "error": "executor returned no action", "raw": repr(exit_result)[:200],
                "current_price": mark_price}

    # ---- stage 3: after the sale. Nothing below may change what the sale did. ----
    post_errors = []

    def _post_failed(step, exc):
        logger.error("_close_live_position_early: {} failed AFTER the sale of position {} "
                     "(action {}): {}: {}", step, position_id, exit_action,
                     type(exc).__name__, exc)
        post_errors.append(f"{type(exc).__name__}: {exc}")

    pnl = _num(exit_result.get("pnl"))
    shares_sold = _num(exit_result.get("shares_sold"))
    exit_price = _num(exit_result.get("exit_price"), mark_price)
    try:
        result = {
            "position_id": position_id,
            "market_title": _display_title(live_pos_row.get("market_title"), market_id),
            "side": live_pos_row.get("side", "BUY"),
            "outcome": _side_label(live_pos_row.get("token_id")),
            "entry_price": live_pos_row.get("entry_price", 0.0),
            "current_price": mark_price,
            "pnl": round(pnl, 4),
            "bet_size": live_pos_row.get("cost_usd", 0.0),
            "reason": reason,
            "strategy": archetype,
            "live": True,
            "action": exit_action,
            "shares_sold": shares_sold,
            "exit_price": exit_price,
            "fee_paid": _num(exit_result.get("fee_paid")),
        }
        for extra in ("liquidity", "min_size_blocked", "hard_stop_blocked"):
            if extra in exit_result:
                result[extra] = exit_result[extra]
    except Exception as exc:
        _post_failed("building the result", exc)
        result = {"position_id": position_id, "action": exit_action, "live": True,
                  "current_price": mark_price, "pnl": round(pnl, 4),
                  "shares_sold": shares_sold, "exit_price": exit_price, "reason": reason}

    try:
        logger.info(
            "LIVE STOP-LOSS: market={} action={} shares={:.2f} @ {:.4f} pnl={:+.4f} reason={}",
            market_id, exit_action, shares_sold, exit_price, pnl, reason,
        )
    except Exception as exc:
        _post_failed("logging the sale", exc)

    # Partial fill: update shares_held in live_positions
    if exit_action == "partial_closed":
        try:
            orig_shares = float(live_pos_row.get("shares", 0))
            remaining = max(0.0, orig_shares - shares_sold)
            conn_upd = live_db.connect()
            try:
                conn_upd.execute(
                    "UPDATE live_positions SET shares=? WHERE id=?",
                    (remaining, position_id)
                )
                conn_upd.commit()
            finally:
                conn_upd.close()
            logger.info(
                "_close_live_position_early: partial_closed shares {} -> {} remaining",
                orig_shares, remaining
            )
        except Exception as exc:
            _post_failed("the partial shares update", exc)

    # One alert, both channels. held_remainder is announced as what it is.
    try:
        for failure in _fire_stop(live_pos_row, result, mark_price, reason):
            post_errors.append(failure)
    except Exception as exc:
        _post_failed("the stop alert", exc)

    # Register exit cooldown — prevent re-entry within 2h on same token
    if exit_action in ("maker_closed", "taker_closed", "partial_closed"):
        try:
            token_id_str = live_pos_row.get("token_id", "")
            if token_id_str:
                from scripts.smart_wallet_fast_poll import register_exit_cooldown
                register_exit_cooldown(token_id_str)
        except Exception as exc:
            _post_failed("registering the exit cooldown", exc)

    if post_errors:
        result["post_error"] = "; ".join(post_errors)
    return result


# Tolerance for float noise only (about 1e-16 on these values). Every real gap
# between a loss and a bound is orders of magnitude larger: on the 0.01 price
# grid the smallest is about 0.002, and an off-grid average fill price such as
# 0.33333 gives a 6e-6 gap. 1e-9 must never be widened to absorb a real one.
EPS = 1e-9


def _usable_price(price):
    """True for a real prediction-market price: a finite number in (0, 1].

    Strict on purpose. bool is refused. Decimal is refused (it is not an int or
    a float subclass, so the isinstance check fails): the order-book parser
    produces floats, and a Decimal here means something else wrote the book.
    numpy float64 is accepted because it subclasses float; numpy float32 and
    other numpy scalars are refused for the same reason as Decimal."""
    return bool(isinstance(price, (int, float)) and not isinstance(price, bool)
                and 0 < price <= 1 and price == price)


def _stop_decision(entry: float, bid: float, ask: float) -> tuple[str, dict]:
    """The stop arithmetic and nothing else: no I/O, no globals except
    UNIVERSAL_MAX_LOSS_PCT and EPS (and the SPREAD_ALLOW_PCT cap). Returns
    ("ok" | "spread_held" | "exit", display figures). The caller guarantees
    entry > 0 and finite and bid and ask usable prices.

    The decision uses the unrounded numbers; the figures are rounded to 4
    places for display only. EPS absorbs float noise: 1 - 0.171/0.285 is
    0.39999999999999991 and counts as a 40% loss, while 1 - 0.20/0.33333 is
    0.399994 and does not."""
    from execution import live_config
    loss_raw = 1 - bid / entry
    base = UNIVERSAL_MAX_LOSS_PCT
    if loss_raw < base - EPS:
        return "ok", {"loss_pct": round(loss_raw, 4)}
    spread_raw = max(0.0, ask - bid) / entry          # crossed book gives 0
    threshold_raw = base + min(spread_raw, live_config.SPREAD_ALLOW_PCT)
    figures = {"loss_pct": round(loss_raw, 4), "spread_pct": round(spread_raw, 4),
               "threshold": round(threshold_raw, 4)}
    if loss_raw < threshold_raw - EPS:
        return "spread_held", figures
    return "exit", figures


def evaluate_live_stops(*, dry_run: bool = False, db_path=None) -> list[dict]:
    """One pass over open live positions. The mark is the best bid of the held
    token, and the same mark goes to the exit. Nothing re-prices it.
    Every open position gets exactly one entry in the result.

    The stop always fires by a loss of UNIVERSAL_MAX_LOSS_PCT +
    live_config.SPREAD_ALLOW_PCT at the best bid (0.40 + 0.10 = 0.50 with the
    defaults): the spread allowance can delay a sale between the two, never
    past the sum. UNIVERSAL_MAX_LOSS_PCT can be overridden from the environment,
    and the bound moves with it.

    Actions: exempt, bad_row, unpriced, ok, spread_held, would_exit (dry run),
    the exit call's own action (maker_closed, taker_closed, partial_closed,
    held_remainder), exit_failed (the exit returned nothing, or failed before
    the sale: no sale happened as far as it knows), exit_unknown (the exit
    raised, or returned something that does not say what happened: a sale may
    have happened) and error (this position could not be evaluated, and no sale
    was attempted)."""
    from execution import live_config, live_db
    from odds.polymarket_clob import get_orderbook
    armed = live_config.live_armed()
    if not armed and not dry_run:
        return []
    exempt = live_config.stop_exempt_ids()      # a bad value must raise here, loudly
    conn = live_db.connect(db_path) if db_path else live_db.connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM live_positions WHERE status='open' AND shares > 0 ORDER BY id")]
    finally:
        conn.close()                            # never held across order-book fetches
    out = []
    for row in rows:
        # "error" means exactly this: the position could not be evaluated and
        # NO sale was attempted. Once the exit is called, a failure is
        # exit_unknown or exit_failed, never error. Every path through this
        # try that does not leave a sale to attempt ends in `continue`.
        try:
            if int(row["id"]) in exempt:
                out.append({"id": row["id"], "action": "exempt"})
                continue
            try:
                entry = float(row.get("entry_price"))
            except (TypeError, ValueError):
                entry = 0.0
            if not entry > 0 or entry == float("inf"):      # also NaN and -inf
                out.append({"id": row["id"], "action": "bad_row"})
                continue
            token_id = row.get("token_id")
            if not token_id or not str(token_id).strip():
                out.append({"id": row["id"], "action": "bad_row"})
                continue
            book = get_orderbook(token_id)
            if not book or not book.bids or not book.asks:
                out.append({"id": row["id"], "action": "unpriced"})
                continue
            bid_prices = [lvl.price for lvl in book.bids]
            ask_prices = [lvl.price for lvl in book.asks]
            # A book with any corrupt level is not trusted at all, and the sort
            # order of the levels is not trusted either.
            if not all(_usable_price(p) for p in bid_prices + ask_prices):
                out.append({"id": row["id"], "action": "unpriced"})
                continue
            bid, ask = float(max(bid_prices)), float(min(ask_prices))
            mark = bid
            decision, figures = _stop_decision(entry, bid, ask)
            if decision == "ok":
                out.append({"id": row["id"], "action": "ok", "mark": mark, **figures})
                continue
            detail = {"id": row["id"], "mark": mark, **figures}
            if decision == "spread_held":
                out.append({**detail, "action": "spread_held"})
                continue
            if dry_run:
                out.append({**detail, "action": "would_exit"})
                continue
        except Exception as exc:                # one bad row must not stop the rest
            out.append({"id": row.get("id"), "action": "error",
                        "error": f"{type(exc).__name__}: {exc}"})
            continue
        # The sale. Its own try: from here on a failure is not "could not evaluate".
        try:
            # hard_cap_frac stays UNIVERSAL_MAX_LOSS_PCT, so every stop exit is at or over the hard cap
            info = _close_live_position_early(row, mark, f"live stop {figures['loss_pct']:.0%}",
                                              hard_cap_frac=UNIVERSAL_MAX_LOSS_PCT)
        except Exception as exc:
            out.append({**detail, "action": "exit_unknown",
                        "error": f"{type(exc).__name__}: {exc}"})
            continue
        if not info:
            out.append({**detail, "action": "exit_failed"})
        elif isinstance(info, dict):
            merged = {**detail, **info}
            if not merged.get("action"):        # a result that does not say what happened
                merged["action"] = "exit_unknown"   # is not a confirmed exit
                if not merged.get("error"):
                    merged["error"] = "exit result has no action"
            out.append(merged)
        else:
            out.append({**detail, "action": "exit_unknown",
                        "error": f"exit returned {repr(info)[:120]}"})
    return out


def _close_position_early(conn, pos, current_yes_price, unrealized_pnl, reason):
    """
    Close a position at current market price (early exit).
    Status = 'stopped' to distinguish from won/lost/void.
    """
    pnl = round(unrealized_pnl, 2)
    exit_price = current_yes_price

    conn.execute("""
        UPDATE paper_positions
        SET status = 'stopped',
            closed_at = ?,
            exit_price = ?,
            pnl = ?,
            close_reason = ?
        WHERE id = ?
    """, (
        datetime.now(timezone.utc).isoformat(),
        round(exit_price, 4),
        pnl,
        f"stop-loss: {reason}",
        pos["id"],
    ))

    # Update bankroll
    from signals.paper_portfolio import _get_bankroll, _save_state
    bankroll = _get_bankroll(conn) + pnl
    _save_state(conn, bankroll, pnl)

    # Calibration outcome log — pnl<0 since stops are by definition losses,
    # but `won` here means "did the bet's directional thesis hold?" — at stop
    # time we exit before resolution so the answer is unknown. Log as won=False
    # (the stop converted a possibly-correct bet into a realized loss). The
    # outcome metric measures stop-policy effectiveness, not model accuracy —
    # stops dragging this Brier higher than the auto Brier is the signal.
    try:
        from signals.resolution_logger import log_position_close
        log_position_close(pos, won=False, pnl=pnl,
                           close_reason=f"stop-loss: {reason}",
                           closing_line=current_yes_price)
    except Exception as e:
        logger.warning("Stop-loss resolution log failed: {}", e)

    logger.info(
        "STOP-LOSS: {} | {} @ {:.0%} → {:.0%} | P&L ${:+.2f} | reason: {}",
        (pos["market_title"] or "")[:50], pos["side"],
        pos["entry_price"], exit_price, pnl, reason,
    )

    return {
        "position_id": pos["id"],
        "market_title": pos["market_title"],
        "side": pos["side"],
        "entry_price": pos["entry_price"],
        "current_price": current_yes_price,
        "pnl": pnl,
        "bet_size": pos["bet_size"],
        "reason": reason,
        "strategy": pos["strategy"] or "",
    }

def _send_discord_alert(stop_info):
    """Send Discord alert for a stop-loss trigger."""
    try:
        from signals.discord_alerts import _send, _portfolio_context, COLOR_ORANGE, COLOR_RED

        entry = stop_info["entry_price"]
        current = stop_info["current_price"]
        side = stop_info.get("outcome") or stop_info["side"]
        bet_size = stop_info["bet_size"]
        pnl = stop_info["pnl"]
        full_loss = -bet_size
        saved = abs(full_loss) - abs(pnl)

        ctx = _portfolio_context()

        fields = [
            {"name": "Side", "value": f"**{side}**", "inline": True},
            {"name": "Entry → Exit", "value": f"{entry:.0%} → {current:.0%}", "inline": True},
            {"name": "Strategy", "value": stop_info["strategy"] or "—", "inline": True},
            {"name": "Loss (stopped)", "value": f"**-${abs(pnl):,.2f}**", "inline": True},
            {"name": "Loss (if held)", "value": f"-${abs(full_loss):,.2f}", "inline": True},
            {"name": "Saved", "value": f"**+${saved:,.2f}**" if saved > 0 else "—", "inline": True},
            {"name": "Reason", "value": stop_info["reason"], "inline": False},
        ]

        _send([{
            "title": f"🛑 STOP-LOSS — {(stop_info['market_title'] or '?')[:70]}",
            "description": f"Position closed early to limit loss",
            "color": COLOR_RED,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": f"Stop-Loss Monitor · 💰 ${ctx['bankroll']:,.0f} · {ctx['record']}"},
        }], alert_type="stop_loss", alert_meta={
            "market": (stop_info["market_title"] or "")[:200],
            "side": side, "entry": entry, "exit": current,
            "pnl": pnl, "saved": saved, "reason": stop_info["reason"],
        })
    except Exception as e:
        logger.warning("Stop-loss Discord alert failed: {}", e)

def _write_heartbeat(conn, positions_checked, warnings_fired):
    """Record proof-of-life for the stop evaluator (Task 2.1, decision D3).

    INSERT OR REPLACE a single row (id=1) so the scheduler-side silence
    alarm can detect a dead evaluator from the DB — restart-proof, and
    written even on empty books (zero open positions is a healthy run).
    Never raises: a heartbeat failure must not break stop evaluation.
    """
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS stop_heartbeat("
            " id INTEGER PRIMARY KEY,"
            " ts INTEGER,"
            " positions_checked INTEGER,"
            " warnings_fired INTEGER)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO stop_heartbeat"
            " (id, ts, positions_checked, warnings_fired)"
            " VALUES (1, strftime('%s','now'), ?, ?)",
            (positions_checked, warnings_fired),
        )
        conn.commit()
    except Exception as e:
        logger.warning("Stop heartbeat write failed: {}", e)

def _send_stop_close_telegram(stop_info):
    """Send a 🛑 stop-close alert through the hardened Telegram sender.

    Universal-stop closes previously went ONLY to Discord — Telegram (the
    primary channel) never saw them (Task 2.0). Plain text: parse_mode=None
    is the format least likely to 400 on arbitrary market titles.
    """
    try:
        from scripts.openclaw_alerts import alert_openclaw
        title = (stop_info.get("market_title") or "?")[:70]
        msg = (
            f"🛑 STOP-LOSS CLOSED — {title}\n"
            f"Side: {stop_info.get('outcome') or stop_info.get('side', '?')} | "
            f"Entry: {stop_info.get('entry_price') or 0:.0%} → "
            f"Exit: {stop_info.get('current_price') or 0:.0%}\n"
            f"PnL: ${stop_info.get('pnl') or 0:+.2f} | "
            f"Bet: ${stop_info.get('bet_size') or 0:.2f}\n"
            f"Reason: {stop_info.get('reason', '')}"
        )
        alert_openclaw(msg, parse_mode=None)
    except Exception as e:
        logger.warning("Stop-loss Telegram alert failed: {}", e)

def evaluate_stops():
    """
    Main entry point. Check all open positions against stop criteria.
    Close positions that breach stops. Returns list of stopped positions.
    """
    conn = _db()
    rows = conn.execute(
        "SELECT id, market_id, market_title, platform, side, entry_price, "
        "bet_size, edge_pct, strategy, opened_at, confidence, "
        "archetype, entry_forecast_json "
        "FROM paper_positions WHERE status = 'open'"
    ).fetchall()

    if not rows:
        _write_heartbeat(conn, 0, 0)
        conn.close()
        return []

    positions = [dict(r) for r in rows]

    # Parallel price fetch
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_fetch_price, positions))

    price_map = {pid: price for pid, price in results if price is not None}
    stopped = []
    warnings_fired = 0

    # Load engine state once per tick for toggle checks
    engine_state = _load_engine_state()
    post_lock_enabled = bool(engine_state.get("post_lock_stops_enabled", False))
    # Allow runtime overrides of the weather post-lock params
    if post_lock_enabled:
        POST_LOCK_CONFIG["weather"] = {
            "info_lock_before_close_h": float(engine_state.get("post_lock_weather_hours", 3.0)),
            "max_loss_pct": float(engine_state.get("post_lock_weather_max_loss", 0.12)),
        }
    now = datetime.now(timezone.utc)

    for pos in positions:
        # kalshi_weather_fade is hold-to-resolution by design (validated eve
        # entry, settles next day); thin overnight Kalshi last_price would
        # fire noise stops. See Weather-Edge-Analysis-Jun2026 sec 10.
        if (pos["archetype"] or "") == "kalshi_weather_fade":
            continue
        pid = pos["id"]
        current_yes_price = price_map.get(pid)
        if current_yes_price is None:
            continue

        strategy = pos["strategy"] or ""
        config = _get_config(strategy)
        side = pos["side"]
        entry_price = pos["entry_price"]
        bet_size = pos["bet_size"]

        # Compute unrealized P&L
        unrealized = _compute_unrealized_pnl(side, entry_price, current_yes_price, bet_size)
        loss_pct = abs(unrealized) / bet_size if unrealized < 0 else 0

        # ── UNIVERSAL STOP CHECK (applies to ALL strategies) ────────────────
        # Checked FIRST, before any strategy-specific config. No strategy can
        # override this — it's the hard floor for every live trade.
        if unrealized < 0 and loss_pct >= UNIVERSAL_MAX_LOSS_PCT:
            reason = (f"UNIVERSAL STOP loss {loss_pct:.0%} >= {UNIVERSAL_MAX_LOSS_PCT:.0%} "
                      f"threshold (all strategies)")
            result = _close_position_early(conn, pos, current_yes_price,
                                           unrealized, reason)
            conn.commit()
            stopped.append(result)
            _send_discord_alert(result)
            _send_stop_close_telegram(result)
            continue

        # ── PRE-RESOLUTION WARNING ──────────────────────────────────────────
        # If a position is within PRE_RESOLVE_WARN_HOURS of resolution and
        # has an unrealized loss above PRE_RESOLVE_WARN_LOSS_PCT, fire a
        # Telegram warning so you can manually exit before it resolves at 0.
        # This catches the "went to 0 in one tick" failure mode.
        if unrealized < 0 and loss_pct >= PRE_RESOLVE_WARN_LOSS_PCT:
            target_date = _parse_market_date(pos.get("market_title") or "")
            if target_date is not None:
                hours_to_close = (target_date - now).total_seconds() / 3600
                if 0 < hours_to_close <= PRE_RESOLVE_WARN_HOURS:
                    try:
                        from scripts.alert_formatter import send_telegram
                        _warn_side = side
                        lines = [
                            f"⚠️ <b>PRE-RESOLUTION WARNING</b>",
                            f"Market: {_display_title(pos.get('market_title'), pos['market_id'])[:60]}",
                            f"Entry: {entry_price:.0%} → Current: {current_yes_price:.0%}",
                            f"Loss: {loss_pct:.0%} | {hours_to_close:.1f}h to resolution",
                            f"Side: {_warn_side} | Bet: ${bet_size:.2f}",
                        ]
                        send_telegram("\n".join(lines))
                        warnings_fired += 1
                    except Exception:
                        logger.warning("pre-resolve warning telegram failed (mkt=%s)", pos.get('market_title','')[:40], exc_info=True)

        # ── Time-to-resolution gate (2026-04-27) ──
        # Strategies with `defer_to_reeval_above_h > 0` skip the threshold-
        # based stop while still far from resolution and rely on the
        # model-aware reeval path (e.g. reeval_weather_positions). Only the
        # 50% hard cap fires in the deferred window — that catches the
        # "lost the full amount at resolution" failure mode at half cost.
        defer_h = float(config.get("defer_to_reeval_above_h", 0))
        if defer_h > 0:
            target_date = _parse_market_date(pos.get("market_title") or "")
            if target_date is not None:
                hours_to_close = (target_date - now).total_seconds() / 3600
                if hours_to_close > defer_h:
                    HARD_CAP = 0.50
                    if unrealized < 0 and loss_pct >= HARD_CAP:
                        reason = (f"HARD-CAP loss {loss_pct:.0%} >= {HARD_CAP:.0%} "
                                  f"({hours_to_close:.1f}h to close, deferred "
                                  f"to reeval otherwise)")
                        result = _close_position_early(conn, pos, current_yes_price,
                                                       unrealized, reason)
                        conn.commit()
                        stopped.append(result)
                        _send_discord_alert(result)
                    continue  # skip threshold + edge_floor while deferred

        # ── Check 1: Max loss percentage ──
        if unrealized < 0 and loss_pct >= config["max_loss_pct"]:
            reason = f"loss {loss_pct:.0%} >= {config['max_loss_pct']:.0%} threshold"
            result = _close_position_early(conn, pos, current_yes_price, unrealized, reason)
            conn.commit()
            stopped.append(result)
            _send_discord_alert(result)
            continue

        # ── Check 1b: Post-lock tight stop (toggle-gated) ──
        # Inside the info-lock window (final hours before resolution), winners
        # don't dip past ~10% drawdown (backtest n=14 weather wins, max 10.4%).
        # A 12% threshold saves +$1,247 net on 26-trade backtest sample.
        if post_lock_enabled and unrealized < 0:
            post_thr = _post_lock_threshold(strategy, pos, now)
            if post_thr is not None and loss_pct >= post_thr:
                reason = (f"POST-LOCK loss {loss_pct:.0%} >= {post_thr:.0%} threshold "
                          f"({strategy}, inside info-lock window)")
                result = _close_position_early(conn, pos, current_yes_price, unrealized, reason)
                conn.commit()
                stopped.append(result)
                _send_discord_alert(result)
                continue

        # ── Check 2: Edge decay stop ──
        # If the market has moved against us enough that our original edge
        # has flipped negative, close the position. This catches signal
        # deterioration before the max_loss_pct threshold is hit.
        # Weather positions get more sophisticated forecast-drift stops in
        # weather_scanner.py:reeval_weather_positions() (runs every 5 min).
        entry_edge = pos.get("edge_pct") or 0
        if entry_edge > 1:
            entry_edge = entry_edge / 100  # normalize percentage to decimal
        edge_floor = config["edge_floor"]

        if side == "YES":
            # Our fair value = entry_price + entry_edge
            fair_value = entry_price + entry_edge
            current_edge = fair_value - current_yes_price
        else:
            # NO side: our fair NO value = (1 - entry_price) + entry_edge
            no_fair = (1 - entry_price) + entry_edge
            no_current = 1 - current_yes_price
            current_edge = no_fair - no_current

        if current_edge < edge_floor and unrealized < 0:
            reason = (f"edge decay: {entry_edge:+.1%} → {current_edge:+.1%} "
                      f"(floor {edge_floor:+.1%})")
            result = _close_position_early(conn, pos, current_yes_price, unrealized, reason)
            conn.commit()
            stopped.append(result)
            _send_discord_alert(result)
            continue

    _write_heartbeat(conn, len(positions), warnings_fired)
    conn.close()

    if stopped:
        logger.info("Stop evaluator: {} positions stopped", len(stopped))
    else:
        logger.debug("Stop evaluator: all {} positions within limits", len(positions))

    return stopped

def _parse_market_date(title):
    """Extract target date from market title like '...on April 8?'."""
    import re
    m = re.search(r'on\s+(January|February|March|April|May|June|July|August|'
                  r'September|October|November|December)\s+(\d{1,2})', title)
    if not m:
        return None
    month_str, day_str = m.group(1), m.group(2)
    months = {"January": 1, "February": 2, "March": 3, "April": 4,
              "May": 5, "June": 6, "July": 7, "August": 8,
              "September": 9, "October": 10, "November": 11, "December": 12}
    now = datetime.now(timezone.utc)
    year = now.year
    try:
        target = datetime(year, months[month_str], int(day_str), 23, 59,
                          tzinfo=timezone.utc)
    except ValueError:
        return None
    # If target is far in the past, it was last year (shouldn't happen)
    if (now - target).days > 180:
        target = target.replace(year=year + 1)
    return target

# Urgent stop config — tighter thresholds for positions resolving soon.
# Calibrated 2026-04-23: 14d counterfactual showed 15% weather stop cost $1,857
# (57/91 correct, 34/91 cut winners). Widened to 25% with a 2h dead zone before
# close. See 02-Projects/Polyclawd/Strategy/Urgent-Stop-Calibration-Apr2026.md
#
# Calibrated 2026-05-06: realized-resolution segmentation on 73 post-Apr-23
# weather URGENT 25% trades showed [2-4h] neutral (-$385 / 25 trades) but
# [4-6h] bleeding (+$5,476 / 48 trades, 43.8% would-have-won). Added
# max_hours_to_close=4.0 (env-tunable) to skip the bleeder slice. See
# 02-Projects/Polyclawd/Strategy/Urgent-Stop-Window-Narrowing-PreReg-May2026.md
#
# Calibrated 2026-05-15: ENSEMBLE_AUDIT_2026-05-14 stop-sensitivity backtest
# (~/Desktop/polyclawd/scratch/sensitivity.py, n=375 resolved weather stops)
# found held-WR rises monotonically with loss-bucket depth: 30-40% bucket wins
# 68.9% held, 50-70% wins 73.9%, >=70% bucket wins 93.1%. Stop systematically
# liquidates winners. Threshold widened 0.25 -> 0.70 (URGENT path, weather).
# Recovers ~+$87k on the 375-row cohort; worst per-trade DD $379. The 0.50
# hard-cap in STOP_CONFIG["weather"] remains as a second-layer safety net.
# Env override: URGENT_WEATHER_MAX_LOSS_PCT for rapid rollback without redeploy.
# See ENSEMBLE_AUDIT_2026-05-15_02_Stop-Sensitivity-Backtest.md
URGENT_STOP_CONFIG = {
    "default":        {"max_loss_pct": 0.30, "min_hours_to_close": 0.0},
    "weather":        {"max_loss_pct": float(os.getenv("URGENT_WEATHER_MAX_LOSS_PCT", "0.40")), "min_hours_to_close": 2.0,
                       "max_hours_to_close": float(os.getenv("URGENT_MAX_HOURS_TO_CLOSE_WEATHER", "4.0"))},
    "tweet_count_mc": {"max_loss_pct": 0.30, "min_hours_to_close": 0.0},
}

URGENT_HOURS = 6  # positions resolving within this window get urgent checks

def evaluate_stops_urgent():
    """
    Fast-path: only check positions resolving within URGENT_HOURS.
    Uses tighter thresholds since there's less time to recover.
    Called from 60-second tick.
    """
    conn = _db()
    rows = conn.execute(
        "SELECT id, market_id, market_title, platform, side, entry_price, "
        "bet_size, edge_pct, strategy, opened_at, confidence, "
        "archetype, entry_forecast_json "
        "FROM paper_positions WHERE status = 'open'"
    ).fetchall()

    if not rows:
        conn.close()
        return []

    now = datetime.now(timezone.utc)
    urgent_positions = []
    for r in rows:
        pos = dict(r)
        target_date = _parse_market_date(pos["market_title"] or "")
        if target_date:
            hours_left = (target_date - now).total_seconds() / 3600
            if hours_left <= URGENT_HOURS:
                pos["_hours_left"] = round(hours_left, 1)
                urgent_positions.append(pos)

    if not urgent_positions:
        conn.close()
        return []

    logger.debug("Urgent stop check: {} positions within {}h of resolution",
                 len(urgent_positions), URGENT_HOURS)

    # Parallel price fetch
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_fetch_price, urgent_positions))

    price_map = {pid: price for pid, price in results if price is not None}
    stopped = []

    # Load engine state once for post-lock toggle
    engine_state = _load_engine_state()
    post_lock_enabled = bool(engine_state.get("post_lock_stops_enabled", False))
    if post_lock_enabled:
        POST_LOCK_CONFIG["weather"] = {
            "info_lock_before_close_h": float(engine_state.get("post_lock_weather_hours", 3.0)),
            "max_loss_pct": float(engine_state.get("post_lock_weather_max_loss", 0.12)),
        }

    for pos in urgent_positions:
        if (pos["archetype"] or "") == "kalshi_weather_fade":
            continue  # hold-to-resolution; see evaluate_stops()
        pid = pos["id"]
        current_yes_price = price_map.get(pid)
        if current_yes_price is None:
            continue

        strategy = pos["strategy"] or ""
        config = URGENT_STOP_CONFIG.get(strategy, URGENT_STOP_CONFIG["default"])
        side = pos["side"]
        entry_price = pos["entry_price"]
        bet_size = pos["bet_size"]

        unrealized = _compute_unrealized_pnl(side, entry_price, current_yes_price, bet_size)
        loss_pct = abs(unrealized) / bet_size if unrealized < 0 else 0

        # ── UNIVERSAL STOP CHECK (urgent path) ─────────────────────────────
        # Applies to ALL strategies regardless of archetype. Checked before
        # any strategy-specific config in the urgent path too.
        if unrealized < 0 and loss_pct >= UNIVERSAL_MAX_LOSS_PCT:
            reason = (f"UNIVERSAL STOP (urgent) loss {loss_pct:.0%} >= {UNIVERSAL_MAX_LOSS_PCT:.0%} "
                      f"({pos['_hours_left']}h to resolution)")
            result = _close_position_early(conn, pos, current_yes_price, unrealized, reason)
            conn.commit()
            stopped.append(result)
            _send_discord_alert(result)
            _send_stop_close_telegram(result)
            continue

        # Post-lock check BEFORE urgent threshold (tighter takes precedence)
        if post_lock_enabled and unrealized < 0:
            post_thr = _post_lock_threshold(strategy, pos, now)
            if post_thr is not None and loss_pct >= post_thr:
                reason = (f"POST-LOCK URGENT loss {loss_pct:.0%} >= {post_thr:.0%} "
                          f"({strategy}, {pos['_hours_left']}h to resolution)")
                result = _close_position_early(conn, pos, current_yes_price, unrealized, reason)
                conn.commit()
                stopped.append(result)
                _send_discord_alert(result)
                continue

        # Dead-zone gate (lower) + window-ceiling (upper). Skip PnL stop if
        # hours_left is inside the last N hours (noise dominates signal) OR
        # above the ceiling (segment-level realized data 2026-05-06 showed
        # the upper band fires on noise too — see Urgent-Stop-Window-
        # Narrowing-PreReg-May2026.md). Edge-decay exit below still fires.
        min_hours = config.get("min_hours_to_close", 0.0)
        max_hours = config.get("max_hours_to_close")  # None = no upper bound
        hours_left = pos["_hours_left"]
        pnl_stop_suppressed = (
            hours_left < min_hours
            or (max_hours is not None and hours_left > max_hours)
        )

        if not pnl_stop_suppressed and unrealized < 0 and loss_pct >= config["max_loss_pct"]:
            reason = (f"URGENT loss {loss_pct:.0%} >= {config['max_loss_pct']:.0%} "
                      f"threshold ({pos['_hours_left']}h to resolution)")
            result = _close_position_early(conn, pos, current_yes_price, unrealized, reason)
            conn.commit()
            stopped.append(result)
            _send_discord_alert(result)
            continue

        # ── Edge decay (urgent) ──
        entry_edge = pos.get("edge_pct") or 0
        if entry_edge > 1:
            entry_edge = entry_edge / 100
        # Tighter edge floor for urgent: 0pp (any negative edge = close)
        urgent_edge_floor = 0.0
        if side == "YES":
            fair_value = entry_price + entry_edge
            current_edge = fair_value - current_yes_price
        else:
            no_fair = (1 - entry_price) + entry_edge
            no_current = 1 - current_yes_price
            current_edge = no_fair - no_current

        if current_edge < urgent_edge_floor and unrealized < 0:
            reason = (f"URGENT edge decay: {entry_edge:+.1%} → {current_edge:+.1%} "
                      f"({pos['_hours_left']}h to resolution)")
            result = _close_position_early(conn, pos, current_yes_price, unrealized, reason)
            conn.commit()
            stopped.append(result)
            _send_discord_alert(result)
            continue

    conn.close()

    if stopped:
        logger.info("Urgent stop evaluator: {} positions stopped", len(stopped))

    return stopped

if __name__ == "__main__":
    results = evaluate_stops()
    if results:
        for r in results:
            print(f"STOPPED: {r['market_title'][:60]} | {r['side']} | P&L ${r['pnl']:+.2f} | {r['reason']}")
    else:
        print("All positions within stop limits")

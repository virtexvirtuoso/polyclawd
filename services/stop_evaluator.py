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

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "storage" / "shadow_trades.db"
CLOB_API = "https://clob.polymarket.com"
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


# ---------------------------------------------------------------------------
# Phase G — Live-position exit routing
# ---------------------------------------------------------------------------

def _get_live_position(market_id):
    """Return the first open live_positions row for *market_id*, or None.

    Uses a fresh live_db connection each call — stop_evaluator runs on its
    own conn (paper_positions) and we keep the two schemas separate.
    Never raises: returns None on any error or when in PAPER mode.
    """
    conn = None
    try:
        from execution import live_config, live_db
        if live_config.mode() != "LIVE":
            return None
        conn = live_db.connect()
        cur = conn.execute(
            "SELECT * FROM live_positions WHERE market_id = ? AND status = 'open' LIMIT 1",
            (market_id,),
        )
        row = cur.fetchone()
        return dict(row) if row is not None else None
    except Exception as exc:
        logger.debug("_get_live_position: {} — treating as no live position", exc)
        return None
    finally:
        if conn is not None:
            conn.close()


def _close_live_position_early(live_pos_row, current_yes_price, reason, hard_cap_frac=0.50):
    """Route a genuine stop on a LIVE position through execute_exit.

    Called ONLY when a stop GENUINELY fires (hard-cap or near-resolution).
    The weather noise-stop deferral in evaluate_stops() still suppresses
    non-genuine stops before this function is ever reached.

    Parameters
    ----------
    live_pos_row : dict
        Row from live_positions (with id, market_id, token_id, entry_price,
        shares, cost_usd, neg_risk).
    current_yes_price : float
        Current mark price (mid from order book or last price).
    reason : str
        Human-readable trigger reason.
    hard_cap_frac : float
        Loss fraction that qualifies this as a genuine hard stop vs. a
        noise stop that should be held to resolution.
        Weather default = 0.70 (STOP_CONFIG["weather"]["max_loss_pct"]).
        Non-weather default = 0.50 (STOP_CONFIG["default"]["max_loss_pct"]).

    Returns stop_info dict or None on error.
    """
    try:
        from execution import live_config, live_db, risk_governor
        from execution.live_executor import execute_exit
        from odds.polymarket_clob import get_orderbook

        token_id = live_pos_row.get("token_id") or ""
        market_id = str(live_pos_row.get("market_id") or "")

        # Use CLOB order-book mid as the mark price where available; fall back
        # to the price already computed by the stop-evaluator loop.
        mark_price = current_yes_price
        if token_id:
            try:
                book = get_orderbook(token_id)
                if book is not None and getattr(book, "mid_price", None):
                    mark_price = book.mid_price
            except Exception:
                pass

        # Get tick_size for the token — default 0.01 if unknown.
        try:
            from execution.clob_client import get_tick_size
            tick_size = get_tick_size(token_id) if token_id else 0.01
        except Exception:
            tick_size = 0.01

        # I4: derive category and strategy label from archetype so non-weather
        # live strategies (when added) route through the correct fee tier.
        # Weather is the only LIVE-eligible strategy today; this is future-proofing.
        archetype = live_pos_row.get("archetype") or "weather"
        conn = live_db.connect()
        try:
            gov = risk_governor.RiskGovernor(conn, mode=live_config.mode())

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

            pnl = exit_result.get("pnl", 0.0)
            exit_action = exit_result.get("action")
            logger.info(
                "LIVE STOP-LOSS: market={} action={} shares={:.2f} @ {:.4f} pnl={:+.4f} reason={}",
                market_id,
                exit_action,
                exit_result.get("shares_sold", 0.0),
                exit_result.get("exit_price", mark_price),
                pnl,
                reason,
            )

            # Partial fill: update shares_held in live_positions
            if exit_action == "partial_closed":
                try:
                    shares_sold = exit_result.get("shares_sold", 0.0)
                    orig_shares = float(live_pos_row.get("shares", 0))
                    remaining = max(0.0, orig_shares - shares_sold)
                    conn_upd = live_db.connect()
                    conn_upd.execute(
                        "UPDATE live_positions SET shares=? WHERE id=?",
                        (remaining, live_pos_row.get("id"))
                    )
                    conn_upd.commit()
                    conn_upd.close()
                    logger.info(
                        "_close_live_position_early: partial_closed shares {} -> {} remaining",
                        orig_shares, remaining
                    )
                except Exception as upd_exc:
                    logger.warning("_close_live_position_early: partial shares update failed: {}", upd_exc)

            # Instant Telegram alert on any real exit (not held_remainder)
            if exit_action in ("maker_closed", "taker_closed", "partial_closed"):
                try:
                    from scripts.alert_formatter import send_telegram
                    exit_price = exit_result.get("exit_price", mark_price)
                    shares_sold = exit_result.get("shares_sold", 0.0)
                    fee = exit_result.get("fee_paid", 0.0)
                    entry_price = live_pos_row.get("entry_price", 0.0)
                    market_title = _display_title(
                        live_pos_row.get("market_title"), market_id)
                    pnl_emoji = "🟢" if pnl >= 0 else "🔴"
                    liq = exit_result.get("liquidity") or ("taker" if "taker" in exit_action else "maker")
                    label = "PARTIAL EXIT" if exit_action == "partial_closed" else "LIVE EXIT"
                    lines = [
                        f"{pnl_emoji} <b>{label}</b> ({liq.upper()}) — {reason}",
                        f"Market: {market_title}",
                        f"Entry: {entry_price:.2f} → Exit: {exit_price:.2f} | Shares: {shares_sold:.2f}",
                        f"PnL: ${pnl:+.2f} | Fee: ${fee:.4f}",
                    ]
                    send_telegram("\n".join(lines))
                except Exception as tg_exc:
                    logger.warning("_close_live_position_early: telegram exit alert failed: {}", tg_exc)

            # Register exit cooldown — prevent re-entry within 2h on same token
            if exit_action in ("maker_closed", "taker_closed", "partial_closed"):
                try:
                    token_id_str = live_pos_row.get("token_id", "")
                    if token_id_str:
                        from scripts.smart_wallet_fast_poll import register_exit_cooldown
                        register_exit_cooldown(token_id_str)
                except Exception:
                    pass

            return {
                "position_id": live_pos_row.get("id"),
                "market_title": _display_title(
                    live_pos_row.get("market_title"), market_id),
                "side": live_pos_row.get("side", "BUY"),
                "entry_price": live_pos_row.get("entry_price", 0.0),
                "current_price": mark_price,
                "pnl": round(pnl, 4),
                "bet_size": live_pos_row.get("cost_usd", 0.0),
                "reason": reason,
                "strategy": archetype,
                "live": True,
                "action": exit_result.get("action"),
            }
        finally:
            conn.close()
    except Exception as exc:
        logger.error("_close_live_position_early: {} — skipping live exit", exc)
        return None


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
        side = stop_info["side"]
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
            f"Side: {stop_info.get('side', '?')} | "
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
            live_pos = _get_live_position(pos["market_id"])
            if live_pos is not None:
                result = _close_live_position_early(
                    live_pos, current_yes_price, reason,
                    hard_cap_frac=UNIVERSAL_MAX_LOSS_PCT,
                )
            else:
                result = _close_position_early(conn, pos, current_yes_price,
                                               unrealized, reason)
                if result is not None:
                    conn.commit()
            if result is not None:
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
                        lines = [
                            f"⚠️ <b>PRE-RESOLUTION WARNING</b>",
                            f"Market: {_display_title(pos.get('market_title'), pos['market_id'])[:60]}",
                            f"Entry: {entry_price:.0%} → Current: {current_yes_price:.0%}",
                            f"Loss: {loss_pct:.0%} | {hours_to_close:.1f}h to resolution",
                            f"Side: {side} | Bet: ${bet_size:.2f}",
                        ]
                        send_telegram("\n".join(lines))
                        warnings_fired += 1
                    except Exception:
                        pass

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
                        live_pos = _get_live_position(pos["market_id"])
                        if live_pos is not None:
                            result = _close_live_position_early(
                                live_pos, current_yes_price, reason,
                                hard_cap_frac=HARD_CAP,
                            )
                        else:
                            result = _close_position_early(conn, pos, current_yes_price,
                                                           unrealized, reason)
                            if result is not None:
                                conn.commit()
                        if result is not None:
                            stopped.append(result)
                            _send_discord_alert(result)
                    continue  # skip threshold + edge_floor while deferred

        # ── Check 1: Max loss percentage ──
        if unrealized < 0 and loss_pct >= config["max_loss_pct"]:
            reason = f"loss {loss_pct:.0%} >= {config['max_loss_pct']:.0%} threshold"
            live_pos = _get_live_position(pos["market_id"])
            if live_pos is not None:
                result = _close_live_position_early(
                    live_pos, current_yes_price, reason,
                    hard_cap_frac=config["max_loss_pct"],
                )
            else:
                result = _close_position_early(conn, pos, current_yes_price, unrealized, reason)
                if result is not None:
                    conn.commit()
            if result is not None:
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
                live_pos = _get_live_position(pos["market_id"])
                if live_pos is not None:
                    result = _close_live_position_early(
                        live_pos, current_yes_price, reason,
                        hard_cap_frac=post_thr,
                    )
                else:
                    result = _close_position_early(conn, pos, current_yes_price, unrealized, reason)
                    if result is not None:
                        conn.commit()
                if result is not None:
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
            live_pos = _get_live_position(pos["market_id"])
            if live_pos is not None:
                result = _close_live_position_early(
                    live_pos, current_yes_price, reason,
                    hard_cap_frac=config.get("edge_floor_hard_cap_frac", 0.50),
                )
            else:
                result = _close_position_early(conn, pos, current_yes_price, unrealized, reason)
                if result is not None:
                    conn.commit()
            if result is not None:
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
            live_pos = _get_live_position(pos["market_id"])
            if live_pos is not None:
                result = _close_live_position_early(
                    live_pos, current_yes_price, reason,
                    hard_cap_frac=UNIVERSAL_MAX_LOSS_PCT,
                )
            else:
                result = _close_position_early(conn, pos, current_yes_price, unrealized, reason)
                if result is not None:
                    conn.commit()
            if result is not None:
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
                live_pos = _get_live_position(pos["market_id"])
                if live_pos is not None:
                    result = _close_live_position_early(
                        live_pos, current_yes_price, reason,
                        hard_cap_frac=post_thr,
                    )
                else:
                    result = _close_position_early(conn, pos, current_yes_price, unrealized, reason)
                    if result is not None:
                        conn.commit()
                if result is not None:
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
            live_pos = _get_live_position(pos["market_id"])
            if live_pos is not None:
                result = _close_live_position_early(
                    live_pos, current_yes_price, reason,
                    hard_cap_frac=config["max_loss_pct"],
                )
            else:
                result = _close_position_early(conn, pos, current_yes_price, unrealized, reason)
                if result is not None:
                    conn.commit()
            if result is not None:
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
            live_pos = _get_live_position(pos["market_id"])
            if live_pos is not None:
                result = _close_live_position_early(
                    live_pos, current_yes_price, reason,
                    hard_cap_frac=0.50,
                )
            else:
                result = _close_position_early(conn, pos, current_yes_price, unrealized, reason)
                if result is not None:
                    conn.commit()
            if result is not None:
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

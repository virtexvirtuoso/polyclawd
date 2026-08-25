#!/usr/bin/env python3
"""
Smart Wallet Entry/Exit Alert — shadow-first.
=============================================

Fires a dedicated alert when a graduated smart wallet accumulates >= $500 of
BUY (Entry) or SELL (Exit) flow in a single market inside a TRUE rolling 4h
window, independent of the anonymous whale-flow pipeline.

Shadow-first posture (this build):
  * Every fire writes a row to `smart_wallet_shadows` with `price_at_alert`.
  * Telegram delivery is OFF unless env SMART_WALLET_ALERT_SEND=1, so we collect
    >= 50 resolved shadows (scope Gate 2) and confirm the +47%/68% follow-PnL
    holds at live alert-time prices BEFORE any real-money follow.

Design decisions baked in from the 2026-06-23 follow-through backtest:
  * The accumulator stores TIMESTAMPED FILLS and re-sums the 4h window each
    update — NOT a running total. A running total can never age out partial old
    flow ("4h rolling" would be a lie); a wallet dribbling $50/3h59m would
    accumulate forever. See research/smart_wallet_followthru_SPEC.md.
  * Resolution of shadows (resolve_shadows) must use the COMPLETE rule — no
    single PM positions field encodes win/loss (realizedPnl misses unredeemed
    winners; redeemable means resolved-not-won; curPrice collapses post-
    resolution). v1 resolver is a placeholder that wants a market-settlement
    source (CLOB /markets/{cid} or on-chain UMA); see resolve_shadows().

Scanner hook (one guarded call) lives in signals/whale_scanner.py.
"""

from __future__ import annotations

import html
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Callable, Optional
from config.polymarket_urls import clob_url, data_url  # polyproxy: central URL config

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from signals.alert_dispatch import TIER_DIGEST, dispatch  # noqa: E402
# Canonical project shadow DB (NOT the stray 0-byte repo-root file) — matches
# signals/shadow_tracker.py and services/scheduler.py.
SHADOW_DB = BASE_DIR / "storage" / "shadow_trades.db"

# --- alert parameters (validated by the follow-through backtest) -----------
ACCUM_WINDOW = 4 * 3600  # 4h rolling window
CONVERGENCE_WINDOW = 15 * 60  # fallback window when close_time unavailable
CONVERGENCE_MIN_WALLETS = 2   # ≥2 distinct wallets needed
THRESHOLD = 1000.0  # $ cumulative that triggers an alert (raised from $500 2026-06-25 — sub-$1K too noisy)
REFIRE_MULT = 2.0  # re-alert when cumulative doubles
MIN_ALERT_MARKET_VOL = 100_000.0
NEAR_SETTLED_HI = 0.90  # suppress when held outcome priced >= this
NEAR_SETTLED_LO = 0.10  # ...or <= this (no edge near resolution)

# Telegram delivery is live. Per-wallet CLV gate still filters noise.
_SEND_ENABLED = os.environ.get("SMART_WALLET_ALERT_SEND", "1") == "1"

# Per-wallet CLV gate: only fire if wallet has >= this many resolved shadows
# with positive average CLV. New wallets get a free pass until they accumulate
# enough data, then are gated.
CLV_GATE_MIN_SHADOWS = 4   # need at least this many resolved before gating
CLV_GATE_MIN_CLV = 0.0     # must have positive avg CLV to keep firing
PRICE_GATE_MAX = 0.60  # suppress delivery of BUY follows priced >= this: measured 2026-07-06
                       # (n=198 resolved ex-near-settled) >=60c = -27c/$ entries, -40c/$ refires,
                       # below breakeven in EVERY wallet/month slice; <60c = +35c/$. Shadows still log.
EXEC_TARGET_USD = 100.0  # reference stake for order-book executable-price grading at alert time
KELLY_SHRINK = 0.5       # multiply the band's measured edge before sizing (edge-overestimation guard)
KELLY_FRACTION = 0.5     # half-Kelly
KELLY_CAP = 0.05         # never hint above 5% of bankroll
KELLY_MIN_BAND_N = 30    # band needs this many resolved shadows before hinting
FADE_MIN_N = 10          # resolved shadows before a wallet can be fade-classified
FADE_MAX_CLV = -0.15     # avg CLV at/below this => deliver inverted FADE alerts


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def init_accum(conn) -> None:
    """Rolling-window accumulator (lives in whale_meta.db). One row per
    (wallet, market, direction); `fills_json` is the pruned 4h fill list."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS smart_wallet_accum (
            wallet       TEXT,
            market       TEXT,
            direction    TEXT,
            fills_json   TEXT,
            total_usd    REAL,
            num_fills    INTEGER,
            first_seen   INTEGER,
            last_seen    INTEGER,
            alert_fired  INTEGER DEFAULT 0,
            fired_total  REAL DEFAULT 0,
            PRIMARY KEY (wallet, market, direction)
        )""")
    conn.commit()


def init_shadows(conn) -> None:
    """Shadow trade log (lives in shadow_trades.db). One row per fired alert."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS smart_wallet_shadows (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet         TEXT,
            market         TEXT,
            title          TEXT,
            direction      TEXT,
            outcome        TEXT,
            outcome_index  INTEGER,
            price_at_alert REAL,
            cumulative_usd REAL,
            num_fills      INTEGER,
            alert_type     TEXT,
            ts_alert       INTEGER,
            resolved       INTEGER DEFAULT 0,
            outcome_result TEXT,
            closing_price  REAL,
            clv            REAL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sw_shadows_open ON smart_wallet_shadows(resolved, ts_alert)")
    # T1-A: category column migration
    try:
        conn.execute("ALTER TABLE smart_wallet_shadows ADD COLUMN category TEXT")
    except Exception:
        pass
    # Executable-price grading columns (2026-07-06): book snapshot at alert time
    for _col in ("executable_ask REAL", "exec_best_ask REAL",
                 "exec_fillable_usd REAL", "exec_spread REAL", "clv_exec REAL"):
        try:
            conn.execute(f"ALTER TABLE smart_wallet_shadows ADD COLUMN {_col}")
        except Exception:
            pass
    # T1-C: convergence dedup table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS smart_wallet_convergence_dedup (
            market     TEXT,
            direction  TEXT,
            alerted_at INTEGER,
            n_wallets  INTEGER,
            total_usd  REAL,
            PRIMARY KEY (market, direction)
        )""")
    conn.commit()


# --------------------------------------------------------------------------- #
# Accumulator (true rolling window)
# --------------------------------------------------------------------------- #
def _accumulate(conn, wallet: str, market: str, direction: str, usd: float, price: float, now: int):
    """Add a fill, prune the 4h window, persist. Returns
    (total_usd, num_fills, alert_fired, fired_total) AFTER the update."""
    row = conn.execute(
        "SELECT fills_json, alert_fired, fired_total, first_seen "
        "FROM smart_wallet_accum WHERE wallet=? AND market=? AND direction=?",
        (wallet, market, direction),
    ).fetchone()
    if row:
        fills = json.loads(row["fills_json"] or "[]")
        alert_fired = row["alert_fired"] or 0
        fired_total = row["fired_total"] or 0.0
        first_seen = row["first_seen"] or now
    else:
        fills, alert_fired, fired_total, first_seen = [], 0, 0.0, now

    fills.append([int(now), float(usd), float(price)])
    cutoff = now - ACCUM_WINDOW
    fills = [f for f in fills if f[0] >= cutoff]
    total = sum(f[1] for f in fills)

    # If the last fire fell out of the window, the dedup period is over —
    # allow a fresh accumulation to fire again.
    if alert_fired and alert_fired < cutoff:
        alert_fired, fired_total = 0, 0.0

    conn.execute(
        "INSERT INTO smart_wallet_accum "
        "(wallet, market, direction, fills_json, total_usd, num_fills, "
        " first_seen, last_seen, alert_fired, fired_total) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(wallet, market, direction) DO UPDATE SET "
        " fills_json=excluded.fills_json, total_usd=excluded.total_usd, "
        " num_fills=excluded.num_fills, last_seen=excluded.last_seen, "
        " alert_fired=excluded.alert_fired, fired_total=excluded.fired_total",
        (
            wallet,
            market,
            direction,
            json.dumps(fills),
            total,
            len(fills),
            first_seen,
            int(now),
            alert_fired,
            fired_total,
        ),
    )
    return total, len(fills), alert_fired, fired_total


def _decide(total: float, alert_fired: int, fired_total: float) -> Optional[str]:
    if alert_fired == 0 and total >= THRESHOLD:
        return "first"
    if alert_fired > 0 and total >= fired_total * REFIRE_MULT:
        return "refire"
    return None


def _mark_fired(conn, wallet, market, direction, now: int, total: float) -> None:
    conn.execute(
        "UPDATE smart_wallet_accum SET alert_fired=?, fired_total=? WHERE wallet=? AND market=? AND direction=?",
        (int(now), float(total), wallet, market, direction),
    )


def _gates_suppress(m: dict, fill_price: float = None, outcome_index: int = None) -> bool:
    """True = suppress this alert. Applied BEFORE marking fired, so a gated
    crossing can still fire later when conditions clear."""
    vol = m.get("volume") or 0.0
    price = m.get("price")
    if vol < MIN_ALERT_MARKET_VOL:
        return True
    if price is not None and (price >= NEAR_SETTLED_HI or price <= NEAR_SETTLED_LO):
        return True
    # Fallback: use fill price (always present) — convert to YES-equivalent so
    # NO-token trades near settlement are caught even if metadata price is None.
    # e.g., draw YES=6% → whale buys NO at 94¢ → yes_eq=0.06 → suppressed.
    if fill_price is not None:
        yes_eq = (1.0 - fill_price) if outcome_index == 1 else fill_price
        if yes_eq >= NEAR_SETTLED_HI or yes_eq <= NEAR_SETTLED_LO:
            return True
    return False


def _clv_gate_suppress(shadow_conn, wallet: str) -> bool:
    """True = suppress Telegram delivery for this wallet.

    Logic: if the wallet has >= CLV_GATE_MIN_SHADOWS resolved shadows,
    check avg CLV. If avg CLV <= CLV_GATE_MIN_CLV, suppress.
    Wallets with < CLV_GATE_MIN_SHADOWS resolved shadows get a free pass
    (not enough data to gate them yet — shadow logging still captures them).
    """
    row = shadow_conn.execute("""
        SELECT COUNT(*) as n, AVG(clv) as avg_clv
        FROM smart_wallet_shadows
        WHERE wallet=? AND resolved=1 AND clv IS NOT NULL
    """, (wallet,)).fetchone()
    if not row or row["n"] < CLV_GATE_MIN_SHADOWS:
        return False  # free pass — not enough data
    return (row["avg_clv"] or 0.0) <= CLV_GATE_MIN_CLV


def _fade_gate_stats(shadow_conn, wallet: str):
    """Fade qualification: our own graded follows of this wallet lose reliably
    (n >= FADE_MIN_N resolved ex-near-settled shadows, avg CLV <= FADE_MAX_CLV).
    Distinct from the mute gate (_clv_gate_suppress): mildly-negative wallets
    stay muted; reliably-negative ones become an inverted signal. Returns
    {"n", "clv"} or None."""
    try:
        row = shadow_conn.execute(
            """SELECT COUNT(*) AS n, AVG(clv) AS c FROM smart_wallet_shadows
               WHERE wallet=? AND resolved=1 AND clv IS NOT NULL
                 AND (near_settled=0 OR near_settled IS NULL)""",
            (wallet,),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if not (row and (row["n"] or 0) >= FADE_MIN_N and (row["c"] or 0) <= FADE_MAX_CLV):
        return None
    # Conflict rule (2026-07-10): NEVER fade a wallet whose FULL history passes
    # the sign-randomization skill gate — our shadow sample (n≈10) is noise next
    # to their n≈1,000 record. First 3 fades before this rule resolved
    # -0.84/-0.84/-0.49 per $1: all four faded wallets were skill-positive.
    # Fail CLOSED (no fade) if the check itself errors.
    try:
        from signals.whale_wallets import get_meta_db, skill_gate_ok

        w = get_meta_db().execute(
            "SELECT skill_n, skill_ret, skill_p FROM pm_wallets WHERE wallet=?",
            (wallet,),
        ).fetchone()
        if w and skill_gate_ok(
            {"skill_n": w["skill_n"], "skill_ret": w["skill_ret"], "skill_p": w["skill_p"]}
        ):
            return None
    except Exception:  # noqa: BLE001
        return None
    return {"n": row["n"], "clv": row["c"]}


def _price_band_gate_suppress(rec: dict) -> bool:
    """True = suppress Telegram delivery for BUY follows priced >= PRICE_GATE_MAX.

    Following smart money into favorites loses (see PRICE_GATE_MAX comment);
    the edge lives below 60c. Same pattern as the wallet CLV gate: delivery
    suppressed, shadow logging continues so the band stays measured."""
    if rec.get("direction") != "BUY":
        return False
    px = rec.get("price_at_alert")
    return px is not None and px >= PRICE_GATE_MAX


# --------------------------------------------------------------------------- #
# Convergence detection (T1-C)
# --------------------------------------------------------------------------- #

def _convergence_window(close_time: str) -> int:
    """Adaptive window based on market time remaining.

    - Live / near-expiry (< 2h): 5 min  — wallets react to same game moment
    - Same-day game (< 24h):    15 min  — game-day research consensus
    - Pre-game / macro (24h+):  30 min  — longer research cycles
    """
    if not close_time:
        return CONVERGENCE_WINDOW
    try:
        from datetime import datetime, timezone
        end_ts = datetime.fromisoformat(close_time.replace("Z", "+00:00")).timestamp()
        remaining = end_ts - time.time()
        if remaining < 7200:    # < 2 hours
            return 5 * 60
        elif remaining < 86400: # < 24 hours
            return 15 * 60
        else:
            return 30 * 60
    except Exception:
        return CONVERGENCE_WINDOW


def _fmt_span(span_secs: int) -> str:
    if span_secs < 60:
        return f"{span_secs}s"
    elif span_secs < 3600:
        return f"{span_secs // 60} min"
    else:
        h, m = divmod(span_secs, 3600)
        return f"{h}h {m // 60}m" if m else f"{h}h"


def _convergence_tier(span_secs: int) -> tuple:
    """(emoji, description) based on fill clustering tightness."""
    if span_secs < 300:   # < 5 min
        return "⚡", "Flash convergence — same game state"
    elif span_secs < 900:  # < 15 min
        return "🟢", "Tight convergence — same time window"
    elif span_secs < 1800: # < 30 min
        return "🟡", "Broad convergence — multiple triggers possible"
    else:
        return "📊", "Gradual accumulation"


def _check_convergence(shadow_conn, market: str, direction: str, title: str, now: int,
                       close_time: str = "") -> None:
    """Fire a convergence alert if ≥2 distinct smart wallets have alerted on
    the same market+direction within the adaptive window."""
    # Skip if market already closed — no actionable edge.
    # Grace period: allow 45 min past close_time to cover soccer ET/penalties
    # (PM close_time is set to kickoff + ~105 min; real final whistle may be up to
    #  120 min + ~30 min of stoppage + penalties).
    CLOSE_GRACE_SECS = 45 * 60
    if close_time:
        try:
            close_ts = datetime.fromisoformat(close_time.replace("Z", "+00:00")).timestamp()
            if now > close_ts + CLOSE_GRACE_SECS:
                return
        except Exception:
            pass

    window_secs = _convergence_window(close_time)
    window = now - window_secs

    rows = shadow_conn.execute("""
        SELECT wallet, cumulative_usd, ts_alert, price_at_alert, outcome_index
        FROM smart_wallet_shadows
        WHERE market=? AND direction=? AND ts_alert >= ?
        ORDER BY ts_alert ASC
    """, (market, direction, window)).fetchall()

    # Deduplicate by wallet (keep first occurrence per wallet in time order)
    wallets: dict = {}
    timestamps = []
    prices = []
    outcome_indices = []
    for r in rows:
        if r["wallet"] not in wallets:
            wallets[r["wallet"]] = r["cumulative_usd"] or 0
            timestamps.append(r["ts_alert"])
            if r["price_at_alert"] is not None:
                prices.append(float(r["price_at_alert"]))
            if r["outcome_index"] is not None:
                outcome_indices.append(r["outcome_index"])

    if len(wallets) < CONVERGENCE_MIN_WALLETS:
        return

    # Dedup: don't re-fire for same convergence event unless wallet count grew
    dedup = shadow_conn.execute(
        "SELECT alerted_at, n_wallets FROM smart_wallet_convergence_dedup WHERE market=? AND direction=?",
        (market, direction),
    ).fetchone()

    n = len(wallets)
    if dedup and dedup["alerted_at"] >= window and dedup["n_wallets"] >= n:
        return

    total_usd = sum(wallets.values())
    span_secs = (max(timestamps) - min(timestamps)) if len(timestamps) > 1 else 0
    avg_price = (sum(prices) / len(prices)) if prices else None
    # Most common outcome_index among fills
    outcome_index = max(set(outcome_indices), key=outcome_indices.count) if outcome_indices else None

    shadow_conn.execute("""
        INSERT INTO smart_wallet_convergence_dedup (market, direction, alerted_at, n_wallets, total_usd)
        VALUES (?,?,?,?,?)
        ON CONFLICT(market, direction) DO UPDATE SET
            alerted_at=excluded.alerted_at,
            n_wallets=excluded.n_wallets,
            total_usd=excluded.total_usd
    """, (market, direction, now, n, total_usd))
    shadow_conn.commit()

    if _SEND_ENABLED:
        msg = _format_convergence(market, direction, title, n, total_usd,
                                  span_secs=span_secs, avg_price=avg_price,
                                  outcome_index=outcome_index)
        try:
            from scripts.alert_formatter import send_telegram
            send_telegram(msg)
        except Exception:
            print(f"SMART_WALLET_ALERT convergence send failed: {sys.exc_info()[1]}", file=sys.stderr)


def _format_convergence(market: str, direction: str, title: str, n_wallets: int, total_usd: float,
                        span_secs: int = 0, avg_price: float = None, outcome_index: int = None) -> str:
    tier_emoji, tier_desc = _convergence_tier(span_secs)
    # Escaped: the literal "<" here previously broke Telegram's HTML parser
    # ("can't parse entities" 400 -> degraded plain-text fallback with raw
    # <b> tags visible, which is exactly the bug this line fixes).
    span_str = html.escape(_fmt_span(span_secs) if span_secs > 0 else "< 1 min", quote=False)

    # What they're actually betting (YES/NO token, not BUY/SELL direction)
    is_no = outcome_index == 1
    side_token = "NO" if is_no else "YES"
    side_dot = "🔴" if is_no else "🟢"

    price_part = f" · avg {avg_price * 100:.0f}¢" if avg_price is not None else ""

    # Escape dynamic content so stray & < > in market titles can't break
    # Telegram's HTML parser (which would 400 -> degrade to plain text).
    title_safe = html.escape(title or market[:50], quote=False)

    return (
        f"🔥 <b>Smart Wallet Convergence</b>\n"
        f"\n"
        f"<b>{title_safe}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{side_dot} <b>{n_wallets} wallets · {side_token} · within {span_str}</b>\n"
        f"Combined: <b>${total_usd:,.0f}</b>{price_part}\n"
        f"\n"
        f"{tier_emoji} {tier_desc}"
    )


# --------------------------------------------------------------------------- #
# Main entry
# --------------------------------------------------------------------------- #
def check_and_fire(
    meta_conn,
    shadow_conn,
    smart_fills: list,
    meta_for: Callable[[str], dict],
    *,
    send: Optional[bool] = None,
    now: Optional[int] = None,
) -> list:
    """Process this cycle's smart-wallet fills; fire + shadow-log crossings.

    smart_fills: list of dicts {wallet, market, direction, usd, price,
                 outcome, outcome_index, name, title}
    meta_for(market) -> {volume, price, title, close_time}
    """
    if now is None:
        now = int(time.time())
    if send is None:
        send = _SEND_ENABLED
    fired = []
    for f in smart_fills:
        total, nfills, af, ft = _accumulate(
            meta_conn, f["wallet"], f["market"], f["direction"], f["usd"], f["price"], now
        )
        kind = _decide(total, af, ft)
        if not kind:
            continue
        m = meta_for(f["market"]) or {}
        if _gates_suppress(m, fill_price=f.get("price"), outcome_index=f.get("outcome_index")):
            continue
        _mark_fired(meta_conn, f["wallet"], f["market"], f["direction"], now, total)
        alert_type = "refire" if kind == "refire" else "entry" if f["direction"] == "BUY" else "exit"
        # Suppress bot wallets from Telegram delivery (still shadow-log for CLV tracking)
        is_bot = bool(f.get("is_bot"))
        rec = {
            "wallet": f["wallet"],
            "name": f.get("name") or f["wallet"][:8],
            "market": f["market"],
            "title": f.get("title") or m.get("title") or f["market"],
            "direction": f["direction"],
            "outcome": f.get("outcome") or "",
            "outcome_index": f.get("outcome_index"),
            "price_at_alert": float(f["price"]),
            "cumulative_usd": round(total, 2),
            "num_fills": nfills,
            "alert_type": alert_type,
            "ts_alert": int(now),
            "market_volume": m.get("volume"),
            "close_time": m.get("close_time"),
            "wallet_wr": f.get("wallet_wr"),
            "wallet_pnl": f.get("wallet_pnl"),
            "wallet_trades": f.get("wallet_trades"),
            "category": f.get("source_category"),
        }
        fade = _fade_gate_stats(shadow_conn, f["wallet"]) if f["direction"] == "BUY" else None
        if fade:
            # Inverted signal: shadow keeps the wallet's own side/price so the
            # resolution machinery is untouched; fade edge = AVG(-clv) of
            # alert_type='fade' rows. No exec snapshot / Kelly hint — those are
            # calibrated for the FOLLOW side.
            rec["alert_type"] = "fade"
            rec["fade_n"], rec["fade_clv"] = fade["n"], fade["clv"]
        else:
            rec.update(_executable_snapshot(f["market"], f.get("outcome_index"), f["direction"]))
        _log_shadow(shadow_conn, rec)
        _check_convergence(shadow_conn, rec["market"], rec["direction"], rec["title"], now,
                           close_time=rec.get("close_time") or "")
        deliver = send and not is_bot and (
            rec["alert_type"] == "fade"  # fades bypass band/CLV gates (they'd self-suppress)
            or (not _price_band_gate_suppress(rec)
                and not _clv_gate_suppress(shadow_conn, f["wallet"])))
        if deliver:
            if rec["alert_type"] != "fade":
                rec["size_hint"] = _kelly_hint(shadow_conn, rec.get("price_at_alert"), rec["direction"])
            try:
                from scripts.alert_formatter import send_telegram

                # Relevance split (2026-08-20, restored 2026-08-21 after the change
                # was clobbered by a polyclawd-deploy from the stale Mac tree).
                # One wallet taking a position is monitoring, not a decision ->
                # tier-3 digest. Convergence (>=2 wallets), fades and exits still
                # page. NOTE: execution is NOT affected either way — fired.append()
                # sits outside this block, so the executor sees every record.
                _msg = _format_alert(rec)
                if rec["alert_type"] in ("entry", "refire"):
                    _kind = "add" if rec["alert_type"] == "refire" else "entry"
                    try:
                        _line = (f"{_kind}: {rec['title'][:70]} — {rec['outcome']} "
                                 f"@ {(rec.get('price_at_alert') or 0) * 100:.0f}¢, "
                                 f"${(rec.get('cumulative_usd') or 0):,.0f} "
                                 f"({rec.get('num_fills') or 0} fills)")
                    except Exception:  # noqa: BLE001 — never lose the event to formatting
                        _line = _msg
                    dispatch("wallet_moves", _line, TIER_DIGEST)
                else:
                    send_telegram(_msg)
            except Exception:  # noqa: BLE001 - delivery must never break the scan
                print(f"SMART_WALLET_ALERT per-wallet send failed: {sys.exc_info()[1]}", file=sys.stderr)
        fired.append(rec)
    meta_conn.commit()
    shadow_conn.commit()
    return fired


def _kelly_hint(shadow_conn, price, direction):
    """Paper-calibrated half-Kelly size hint from the alert's own price band.

    q = price + KELLY_SHRINK x band mean CLV (resolved ex-near-settled shadows);
    full Kelly for a binary BUY at p with win prob q is q - (1-q)*p/(1-p).
    Printed at KELLY_FRACTION x that, capped at KELLY_CAP. The shrink+half+cap
    stack is deliberate: the systematic risk is edge OVERestimation, not
    estimation variance. Returns None when the band lacks data or edge.
    """
    if direction != "BUY" or price is None or price >= PRICE_GATE_MAX:
        return None
    lo, hi = ((0.0, 0.20) if price < 0.20 else
              (0.20, 0.40) if price < 0.40 else (0.40, PRICE_GATE_MAX))
    try:
        row = shadow_conn.execute(
            """SELECT COUNT(*) AS n, AVG(clv) AS e FROM smart_wallet_shadows
               WHERE resolved=1 AND clv IS NOT NULL
                 AND (near_settled=0 OR near_settled IS NULL)
                 AND direction='BUY' AND price_at_alert >= ? AND price_at_alert < ?""",
            (lo, hi)).fetchone()
    except Exception:  # noqa: BLE001 - a hint must never break delivery
        return None
    if not row or (row["n"] or 0) < KELLY_MIN_BAND_N or (row["e"] or 0) <= 0:
        return None
    q = min(0.99, price + KELLY_SHRINK * row["e"])
    f_full = q - (1 - q) * price / (1 - price)
    f = min(KELLY_CAP, KELLY_FRACTION * f_full)
    if f < 0.002:
        return None
    return (f"size ≤ {f*100:.1f}% bankroll — half-Kelly on shrunk "
            f"{int(lo*100)}–{int(hi*100)}¢ band edge (n={row['n']}, paper-calibrated)")


def _executable_snapshot(cid: str, outcome_index, direction: str) -> dict:
    """Order-book executable price for an EXEC_TARGET_USD BUY at alert time.

    Grades the alert against what was actually fillable (spread + depth), not
    the fill/mid price — a shadow edge means nothing if the book only offered
    a worse price. Non-fatal: {} on any failure; clv_exec then stays NULL and
    grading falls back to price_at_alert only.
    """
    if direction != "BUY":
        return {}
    try:
        from odds.poly_executable_edge import executable_edge

        r = executable_edge(
            0.5,  # true_prob unused here — we only want the book prices
            "YES" if (outcome_index or 0) == 0 else "NO",
            condition_id=cid,
            outcome_index=outcome_index,
            target_usd=EXEC_TARGET_USD,
        )
        if not r.get("available"):
            return {}
        return {
            "executable_ask": r.get("executable_price"),
            "exec_best_ask": r.get("best_price"),
            "exec_fillable_usd": r.get("fillable_usd"),
            "exec_spread": r.get("spread"),
        }
    except Exception:  # noqa: BLE001 - book fetch must never break the scan
        return {}


def _log_shadow(conn, rec: dict) -> None:
    conn.execute(
        "INSERT INTO smart_wallet_shadows "
        "(wallet, market, title, direction, outcome, outcome_index, "
        " price_at_alert, cumulative_usd, num_fills, alert_type, ts_alert, category, "
        " executable_ask, exec_best_ask, exec_fillable_usd, exec_spread) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            rec["wallet"],
            rec["market"],
            rec["title"],
            rec["direction"],
            rec["outcome"],
            rec["outcome_index"],
            rec["price_at_alert"],
            rec["cumulative_usd"],
            rec["num_fills"],
            rec["alert_type"],
            rec["ts_alert"],
            rec.get("category"),
            rec.get("executable_ask"),
            rec.get("exec_best_ask"),
            rec.get("exec_fillable_usd"),
            rec.get("exec_spread"),
        ),
    )


def _format_alert(rec: dict) -> str:
    head = {
        "fade": "🔻 <b>FADE Signal</b>",
        "entry": "🧠 <b>Smart Wallet Entry</b>",
        "exit": "🧠 <b>Smart Wallet Exit</b> ⚠️",
        "refire": "🧠 <b>Smart Wallet — Adding</b>",
    }.get(rec["alert_type"], "🧠 <b>Smart Wallet</b>")
    # outcome_index: 0=YES token, 1=NO token
    is_no = rec.get("outcome_index") == 1
    is_exit = rec["direction"] != "BUY"
    fill_cents = rec["price_at_alert"] * 100
    cents_display = f"~{fill_cents:.1f}¢"

    # Plain-English side label: what position does this wallet actually hold?
    # Buying NO = betting the NO outcome (Under, Won't happen, etc.)
    # Buying YES = betting the YES outcome
    if is_exit:
        side = f"Sold {'NO' if is_no else 'YES'}"
        action = "Exited"
    else:
        side = f"{'NO' if is_no else 'YES'}"
        action = "Accumulated"

    # Wallet stats line
    wr = rec.get("wallet_wr")
    pnl = rec.get("wallet_pnl")
    trades = rec.get("wallet_trades")
    stats_parts = []
    if wr is not None:
        stats_parts.append(f"{wr*100:.0f}% WR")
    if trades is not None:
        stats_parts.append(f"{trades} trades")
    if pnl is not None:
        stats_parts.append(f"${pnl:,.0f} lifetime")
    stats_line = "   " + " · ".join(stats_parts) if stats_parts else ""

    return (
        f"{head}\n"
        f"\n"
        f"<b>{rec['name']}</b>{stats_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{rec['title']}\n"
        f"\n"
        f"{action} <b>${rec['cumulative_usd']:,.0f}</b> → <b>{side} @ {cents_display}</b>  ({rec['num_fills']} fills)"
        + (f"\n📐 {rec['size_hint']}" if rec.get("size_hint") else "")
        + ((f"\n🔻 Our graded follows of this wallet run {rec['fade_clv']*100:+.0f}¢/$1 "
            f"(n={rec['fade_n']}) — consider <b>{'YES' if is_no else 'NO'} @ ~"
            f"{(1 - rec['price_at_alert'])*100:.0f}¢</b> (fading their {side})")
           if rec["alert_type"] == "fade" else "")
    )


# --------------------------------------------------------------------------- #
# Scanner integration: aggregate this cycle's trades -> smart fills
# --------------------------------------------------------------------------- #
def fills_from_trades(trades: list, smart: dict) -> list:
    """Collapse raw PM trades into per-(wallet, market, side) fills, restricted
    to graduated smart wallets. price = $-weighted avg of the cycle's fills."""
    agg = {}
    for t in trades:
        w = t.get("proxyWallet")
        if not w or w not in smart:
            continue
        cid = t.get("conditionId")
        side = (t.get("side") or "").upper()
        if not cid or side not in ("BUY", "SELL"):
            continue
        size = t.get("size") or 0
        price = t.get("price") or 0
        usd = size * price
        if usd <= 0:
            continue
        k = (w, cid, side)
        a = agg.setdefault(
            k,
            {
                "usd": 0.0,
                "pxw": 0.0,
                "outcome": t.get("outcome") or "",
                "outcome_index": t.get("outcomeIndex"),
                "title": (t.get("title") or "")[:80],
                "name": smart[w].get("name") or t.get("name") or w[:8],
            },
        )
        a["usd"] += usd
        a["pxw"] += usd * price
    out = []
    for (w, cid, side), a in agg.items():
        px = a["pxw"] / a["usd"] if a["usd"] else 0.0
        sw = smart.get(w, {})
        out.append(
            {
                "wallet": w,
                "market": cid,
                "direction": side,
                "usd": a["usd"],
                "price": px,
                "outcome": a["outcome"],
                "outcome_index": a["outcome_index"],
                "name": a["name"],
                "title": a["title"],
                "wallet_wr": sw.get("win_rate"),
                "wallet_pnl": sw.get("net_pnl"),
                "wallet_trades": sw.get("closed_positions") or sw.get("closed"),
                "source_category": sw.get("source_category"),
                "is_bot": sw.get("is_bot", 0),
            }
        )
    return out


def run_from_scan(meta_conn, shadow_conn, trades: list, gamma: dict, smart: dict) -> list:
    """Called once per PM sweep from whale_scanner. gamma maps cid -> market
    metadata (volumeNum, liquidityNum, last_price, question, endDate)."""
    fills = fills_from_trades(trades, smart)
    if not fills:
        return []

    def meta_for(cid):
        g = gamma.get(cid) or {}
        return {
            "volume": g.get("volumeNum") or 0.0,
            "price": g.get("last_price"),
            "title": (g.get("question") or "")[:80],
            "close_time": g.get("endDate") or "",
        }

    return check_and_fire(meta_conn, shadow_conn, fills, meta_for)


def scanner_hook(meta_conn, trades: list, gamma: dict, smart: dict) -> list:
    """One-call entry for whale_scanner: ensures tables exist, opens the shadow
    DB, runs the sweep, and never raises into the scan loop."""
    try:
        init_accum(meta_conn)
        sconn = sqlite3.connect(str(SHADOW_DB))
        sconn.row_factory = sqlite3.Row
        try:
            init_shadows(sconn)
            return run_from_scan(meta_conn, sconn, trades, gamma, smart)
        finally:
            sconn.close()
    except Exception:  # noqa: BLE001 - the alert must never break the scan
        return []


# --------------------------------------------------------------------------- #
# Shadow resolution (cron)
# --------------------------------------------------------------------------- #
def resolve_shadows(shadow_conn, settle: Callable[[dict], Optional[float]]) -> int:
    """Backfill outcome + CLV for resolved shadows.

    settle(shadow_row) -> the held outcome's settled value (1.0 won / 0.0 lost)
    or None if not yet resolved. Returns the number newly resolved.
    """
    rows = shadow_conn.execute(
        "SELECT id, wallet, market, outcome_index, price_at_alert, executable_ask FROM smart_wallet_shadows WHERE resolved=0"
    ).fetchall()
    n = 0
    for r in rows:
        settled = settle(dict(r))
        if settled is None:
            continue
        clv = settled - (r["price_at_alert"] or 0.0)
        exec_ask = r["executable_ask"] if "executable_ask" in r.keys() else None
        clv_exec = (settled - exec_ask) if exec_ask is not None else None
        shadow_conn.execute(
            "UPDATE smart_wallet_shadows SET resolved=1, outcome_result=?, closing_price=?, clv=?, clv_exec=? WHERE id=?",
            ("WIN" if settled > 0.5 else "LOSS", settled, clv, clv_exec, r["id"]),
        )
        n += 1
    shadow_conn.commit()
    return n


def settle_via_market_resolution(row: dict) -> Optional[float]:
    """GROUND TRUTH settlement: the market's own resolution from CLOB.

    Returns 1.0 if the held outcome won, 0.0 if it lost, None if the market has
    not resolved yet (leave the shadow unresolved — it will be picked up later).

    Replaces settle_via_wallet_positions as the primary settler (2026-08-21).
    That function graded from the alerting wallet's `realizedPnl`, which is
    booked TRADING pnl, not resolution payout: a whale who buys at 15c and
    trims at 18c books rp>0 and was graded a WIN with closing_price=1.0 on a
    market that never resolved. Cheap positions trim up easily, expensive ones
    trim down, which is why the stored win rate ran INVERSE to entry price
    (85% at 10-20c, 34% at 80-90c). Re-grading 1,250 shadows against CLOB
    resolution found the stored labels agreed with reality **41.6%** of the
    time — worse than a coin flip — and flipped the measured edge from a
    claimed +1.43/$ to -0.179/$ (95% CI [-0.253,-0.055], clustered by wallet).

    Assumption (verified): CLOB `tokens[]` is ordered to match outcome_index.
    Cross-checking by outcome NAME against by INDEX gave 0 disagreements in
    853 rows, and the by-index reading yields a plausible 33.9% true win rate
    at a 40.3c mean price (the inverse would be 66% at 40c — impossible).
    """
    import urllib.request

    cid, oidx = row.get("market"), row.get("outcome_index")
    if not cid or oidx is None:
        return None
    try:
        req = urllib.request.Request(
            clob_url(f"/markets/{cid}"),
            headers={"User-Agent": "Polyclawd/2.0"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
    except Exception:  # noqa: BLE001 — unreachable market = not yet resolvable
        return None
    if not isinstance(data, dict) or not data.get("closed"):
        return None  # still open — do NOT guess
    tokens = data.get("tokens") or []
    try:
        idx = int(oidx)
    except (TypeError, ValueError):
        return None
    if idx < 0 or idx >= len(tokens):
        return None
    winner = tokens[idx].get("winner")
    if winner is None:
        return None  # closed but not yet finalised
    return 1.0 if winner else 0.0


def settle_via_wallet_positions(row: dict) -> Optional[float]:
    """DEPRECATED 2026-08-21 — DO NOT USE FOR GRADING. Retained for reference.

    Grades from the wallet's realizedPnl (booked trading pnl), NOT resolution
    payout, so it mislabels trimmed-but-unresolved positions as WINs. Measured
    agreement with actual market outcomes: 41.6%. Use
    settle_via_market_resolution() instead.

    COMPLETE-RULE settlement from the alerting wallet's own position for the
    market (the wallet bought it, so it still holds the row). Validated in the
    2026-06-23 follow-through backtest. No single PM field works alone:
      realizedPnl>0 -> redeemed winner; realizedPnl<0 -> sold at a loss;
      currentValue>=50% of cost (resolved) -> unredeemed winner;
      currentValue<1% (resolved) -> collapsed loser; else still open -> None.
    UPGRADE PATH: a fully survivorship-free run wants market-level settlement
    (CLOB /markets/{cid} resolution or on-chain UMA) instead of the wallet's
    surviving position; Gamma condition_ids misses ~40% of older markets.
    """
    import urllib.request

    wallet, cid, oidx = row.get("wallet"), row.get("market"), row.get("outcome_index")
    if not wallet or not cid:
        return None
    try:
        req = urllib.request.Request(
            data_url(f"/positions?user={wallet}&limit=500"),
            headers={"User-Agent": "Polyclawd/2.0"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            positions = json.loads(resp.read().decode())
    except Exception:  # noqa: BLE001
        return None
    now = time.time()
    for p in positions if isinstance(positions, list) else positions.get("data", []):
        if p.get("conditionId") != cid or p.get("outcomeIndex") != oidx:
            continue
        iv = float(p.get("initialValue") or 0)
        cv = float(p.get("currentValue") or 0)
        rp = float(p.get("realizedPnl") or 0)
        end = p.get("endDate")
        ended = False
        if end:
            try:
                from datetime import datetime

                ended = datetime.fromisoformat(str(end).replace("Z", "+00:00")).timestamp() < now
            except Exception:  # noqa: BLE001
                ended = False
        resolved = bool(p.get("redeemable")) or ended or abs(rp) > 0.01
        if not resolved or iv <= 0:
            return None
        if rp > 0.01:
            return 1.0
        if rp < -0.01:
            return 0.0
        if cv >= 0.5 * iv:
            return 1.0
        if cv < 0.01 * max(iv, 1):
            return 0.0
        return None
    return None  # position no longer present -> can't resolve from wallet


def main() -> None:
    """Cron entry: resolve outstanding shadows and print a calibration summary
    (scope Gate 2: >=50 resolved before any real-money follow)."""
    conn = sqlite3.connect(str(SHADOW_DB))
    conn.row_factory = sqlite3.Row
    init_shadows(conn)
    n = resolve_shadows(conn, settle_via_market_resolution)
    row = conn.execute(
        "SELECT COUNT(*) c, "
        " SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END) res, "
        " SUM(CASE WHEN resolved=1 AND outcome_result='WIN' THEN 1 ELSE 0 END) wins, "
        " AVG(CASE WHEN resolved=1 THEN clv END) mean_clv "
        "FROM smart_wallet_shadows"
    ).fetchone()
    res = row["res"] or 0
    wr = (row["wins"] / res) if res else 0.0
    print(
        f"smart-wallet shadows: {row['c']} logged, {res} resolved "
        f"(+{n} this run), WR@alert={wr:.1%}, mean CLV={row['mean_clv'] or 0:+.3f}"
    )
    print("Gate 2 (>=50 resolved): " + ("MET" if res >= 50 else f"{res}/50"))
    conn.close()


if __name__ == "__main__":
    main()

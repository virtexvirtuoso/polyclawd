#!/usr/bin/env python3
"""
Pre-trade health gates — state-based circuit breakers.

Philosophy:
  A static count cap is the wrong abstraction. A healthy system shouldn't
  be artificially limited; an unhealthy system shouldn't trade at all.
  Each gate reads the system's current state and refuses new entries when
  a measured condition is unsafe. Recovery is automatic when the
  triggering condition clears — no manual reset.

Current gates (weather):
  • Recent-WR collapse: last 20 closed weather trades, WR < 20% (n ≥ 10)
  • Loss streak: 5 consecutive weather losses, within 2h cooldown
  • Auto-Brier drift: last 20 auto-resolved closes, Brier > 0.25
  • Source coverage: ≥4 of 7 weather forecast APIs circuit-open or
    consecutive_failures ≥ 3 (instrumented via signals/weather_ensemble.py
    _record_weather_fetch into the source_health table)

See vault: Health-Gates-Framework-Apr2026, Source-Health-Schema-Audit-2026-05-01.
"""
from __future__ import annotations
from db import connect as db_connect

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("polyclawd.health_gates")

DB_PATH = Path(__file__).parent.parent / "storage" / "shadow_trades.db"
AUTO_LOG_PATH = Path(__file__).parent.parent / "storage" / "weather_resolutions_auto.jsonl"

# In-process cache — avoid hammering DB on every signal evaluation.
# Recovery latency is bounded by this TTL.
_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL_S = 30

# Dedupe transition logging — only emit when state changes.
_last_logged_state: dict[str, str] = {}


def _conn() -> sqlite3.Connection:
    c = db_connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def weather_health_check() -> dict:
    """Pre-trade health gate for the weather archetype.

    Returns:
        {
          'state': 'GREEN' | 'RED',
          'reasons': list[str],   # populated only when RED
          'resume_when': str|None # human-readable recovery condition
        }

    Cached for `_CACHE_TTL_S`. Falls open (GREEN) on DB error so a transient
    storage hiccup doesn't halt all trading.
    """
    now_ts = time.time()
    cached = _cache.get("weather")
    if cached and now_ts - cached[0] < _CACHE_TTL_S:
        return cached[1]

    reasons: list[str] = []
    resume_when: str | None = None

    try:
        conn = _conn()
        rows = conn.execute(
            "SELECT pnl, closed_at FROM paper_positions "
            "WHERE archetype='weather' AND status <> 'open' "
            "AND closed_at IS NOT NULL "
            "ORDER BY closed_at DESC LIMIT 20"
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.warning("weather_health_check DB error: %s — falling open (GREEN)", e)
        return {"state": "GREEN", "reasons": [], "resume_when": None}

    # ── Gate 1: recent WR collapse ────────────────────────────────
    # Window: last 20 closed weather trades (any close type).
    # Trigger: WR < 20% with n ≥ 10 (avoid noise).
    # Recovery: rolling WR climbs back to 30% as old losses age out.
    if len(rows) >= 10:
        wins = sum(1 for r in rows if (r["pnl"] or 0) > 0)
        wr = wins / len(rows)
        if wr < 0.20:
            reasons.append(f"WR collapse: {wr:.0%} on last {len(rows)} closes (floor 20%)")
            resume_when = "rolling WR ≥ 30%"

    # ── Gate 2: loss streak ───────────────────────────────────────
    # Window: last 5 closed weather trades.
    # Trigger: all 5 are losses (pnl < 0).
    # Cooldown: 2h after the most recent close. Auto-resumes on time.
    if len(rows) >= 5:
        last_5 = rows[:5]
        if all((r["pnl"] or 0) < 0 for r in last_5):
            try:
                last_close = datetime.fromisoformat(
                    str(last_5[0]["closed_at"]).replace("Z", "+00:00")
                )
                hours_since = (datetime.now(timezone.utc) - last_close).total_seconds() / 3600
                if hours_since < 2.0:
                    rem = 2.0 - hours_since
                    reasons.append(f"5-loss streak; cooldown {rem:.1f}h remaining")
                    resume_when = (
                        f"{resume_when} + 2h cooldown ({rem:.1f}h left)"
                        if resume_when else f"2h cooldown ({rem:.1f}h left)"
                    )
            except Exception as e:
                logger.debug("loss-streak cooldown calc failed: %s", e)

    # ── Gate 3: auto-Brier drift ──────────────────────────────────
    # Window: last 20 auto-resolved weather closes (model accuracy only —
    # excludes stops/displaces, which are stop-policy outcomes not model errors).
    # Trigger: Brier > 0.25 with n ≥ 20.
    # Recovery: rolling Brier ≤ 0.25 as new auto-resolutions slide the window.
    # mc_prob is the model's P(chosen-side wins), so Brier per row is
    # (mc_prob − won_int)². See signals/resolution_logger.py::_model_p_yes_from_forecast.
    auto_rows: list[dict] = []
    try:
        if AUTO_LOG_PATH.exists():
            with AUTO_LOG_PATH.open() as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        auto_rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except Exception as e:
        logger.debug("auto-Brier read failed: %s — gate disabled this tick", e)
    auto_window = auto_rows[-20:]
    if len(auto_window) >= 20:
        brier = sum(
            ((r.get("mc_prob") or 0.5) - (1.0 if r.get("won") else 0.0)) ** 2
            for r in auto_window
        ) / len(auto_window)
        if brier > 0.25:
            reasons.append(f"Auto-Brier drift: {brier:.3f} on last 20 auto-resolved (floor 0.25)")
            resume_msg = "auto-Brier ≤ 0.25"
            resume_when = f"{resume_when} + {resume_msg}" if resume_when else resume_msg

    # ── Gate 4: weather forecast source coverage ──────────────────
    # Source: source_health table, populated by signals/weather_ensemble.py
    # _record_weather_fetch(). A source counts as "degraded" if its circuit
    # is open OR it has ≥3 consecutive failures. Trigger when ≥4 of the 7
    # forecast sources are degraded — at that point the surviving model's
    # biases dominate ensemble residual variance, so directional drift starts
    # mattering. Sources with no row yet (never called) don't count as degraded.
    # Recovery: degraded count drops back to ≤3.
    # See vault: Source-Health-Schema-Audit-2026-05-01 §4.
    WEATHER_FORECAST_SOURCES = (
        "open_meteo", "pirate_weather", "tomorrow_io", "weatherapi",
        "weather_com", "visual_crossing", "nws",
    )
    try:
        from api.services import source_health as _sh
        degraded: list[str] = []
        for src in WEATHER_FORECAST_SOURCES:
            if _sh.is_circuit_open(src):
                degraded.append(f"{src}(circuit-open)")
                continue
            health = _sh.get_source_health(src)
            if health and (health.get("consecutive_failures") or 0) >= 3:
                degraded.append(f"{src}(failing×{health['consecutive_failures']})")
        if len(degraded) >= 4:
            reasons.append(
                f"Source coverage: {len(degraded)}/{len(WEATHER_FORECAST_SOURCES)} "
                f"weather APIs degraded — {', '.join(degraded)}"
            )
            resume_msg = "weather coverage restored (≤3 degraded)"
            resume_when = f"{resume_when} + {resume_msg}" if resume_when else resume_msg
    except Exception as e:
        logger.debug("source-coverage gate read failed: %s — falling open", e)

    state = "RED" if reasons else "GREEN"
    result = {"state": state, "reasons": reasons, "resume_when": resume_when}
    _cache["weather"] = (now_ts, result)

    # Log only on state transition.
    if state != _last_logged_state.get("weather"):
        if state == "RED":
            logger.warning("weather health → RED: %s", "; ".join(reasons))
        elif _last_logged_state.get("weather") == "RED":
            logger.info("weather health → GREEN (recovered)")
        _last_logged_state["weather"] = state

    return result


def clear_cache(archetype: str | None = None) -> None:
    """Force the next check to recompute. For tests and manual ops."""
    if archetype is None:
        _cache.clear()
    else:
        _cache.pop(archetype, None)

#!/usr/bin/env python3
"""
alert_governor.py — Shared escalation-aware alert dedup (state-based, not time-based).

Design: vault 02-Projects/Polyclawd/Strategy/Alert-Governor-And-TWAP-Detection-Plan-2026-07-10.md
(§2 design, §7 critic amendments C1-C9).

Core rule (Mr. V, 2026-07-10): never let a time window mute a BETTER alert.
Suppress only the *same edge state*; fire instantly on escalation:
  - new leg / outcome            -> fire
  - direction flip               -> fire
  - magnitude >= last + delta    -> fire (labeled UPGRADE, with delta line)
  - stale >= rearm_min (capped)  -> fire once (fallback re-arm)
  - otherwise                    -> suppress

Concurrency (C1): all read-decide-write inside BEGIN IMMEDIATE. Lock contention
means a peer process is deciding the same entity -> retry once, then SUPPRESS
(the peer's alert covers it). Fail-open (fire) only on unexpected errors —
a duplicate alert is cheaper than a missed one.

Hard rule (blind spot #6): this module gates SENDS only. Callers must do all
shadow/control/audit logging BEFORE calling govern().

State lives in shadow_trades.db (WAL) -> survives restarts (blind spot #1).
Only valid for processes sharing that DB, i.e. VPS scheduler/cron (C9).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

DB_PATH = Path(__file__).resolve().parent.parent / "storage" / "shadow_trades.db"

# Per-pipeline config (blind spot #2: units differ per pipeline — no globals).
# magnitude is a float in the pipeline's own units (pp, cents, $...).
# mode: "enforce" -> suppress blocks the send; "shadow" -> decisions logged only,
# should_send stays True (blind spot #7: shadow-first rollout for G2 pipelines).
CONFIG: Dict[str, Dict] = {
    # G1 — enforced from birth
    "mlb_run":            {"delta": 5.0, "rearm_min": 45,  "max_rearms": 2, "mode": "enforce"},
    "mlb_line_drift":     {"delta": 5.0, "rearm_min": 45,  "max_rearms": 2, "mode": "enforce"},
    "mlb_edge_inversion": {"delta": None, "rearm_min": None, "max_rearms": 0, "mode": "enforce"},  # once per trade
    # Soccer (2026-08-21, downgraded to SHADOW after the /qa audit).
    # The earlier claim that this "inherits params proven on MLB" was WRONG: the
    # units match but the market topology does not. MLB is 2-way, so legs are
    # exact complements and a falling leg ALWAYS has a rising complement to
    # satisfy the one-directional upgrade test (magnitude >= last + delta).
    # Soccer is 3-way: a 7pp fall splits (e.g. +4/+3) and both complements fall
    # below LINE_DRIFT_PP=5.0, so no escalation candidate enters drift_legs.
    # Combined with max_rearms=2 that silences a leg permanently after ~90 min —
    # i.e. the last 25-40 min of a match. Shadow until measured.
    "soccer_line_drift":  {"delta": 5.0, "rearm_min": 45,  "max_rearms": 2, "mode": "enforce",
                          "delta_mode": "abs"},
    # G2 — registered here as they get wired (shadow first)
}

# delta_mode (added 2026-08-21):
#   "increase" (default) — escalate only when magnitude GROWS by >= delta.
#       Correct where magnitude is an EDGE size: an edge shrinking is a
#       de-escalation and must not re-page (mlb_run).
#   "abs"                — escalate on movement of >= delta in EITHER direction.
#       Required where magnitude is a PROBABILITY LEVEL in a market with more
#       than two outcomes. In a 2-way market (mlb_line_drift: verified, all 18
#       leg-pairs sum to exactly 100.0) a falling leg always has a rising
#       complement that trips the increase-only test, so "increase" suffices.
#       Soccer is 3-way: a 7pp fall splits (e.g. +4/+3) and BOTH complements
#       land below LINE_DRIFT_PP=5.0, so no leg ever trips it and the alert
#       goes permanently silent after max_rearms.
_DEFAULT = {"delta": 5.0, "rearm_min": 45, "max_rearms": 2, "mode": "shadow",
            "delta_mode": "increase"}


@dataclass
class Leg:
    outcome: str            # leg identifier (team name, asset, wallet...)
    magnitude: float        # pipeline units (edge pp, level pp, $...)
    direction: str = ""     # BUY/SELL, up/down, YES/NO...


@dataclass
class Verdict:
    action: str                       # fire | fire_upgrade | suppress
    should_send: bool
    mode: str = "enforce"
    reasons: List[str] = field(default_factory=list)
    delta_line: str = ""              # "last alerted 8pp @ 01:14 → now 14pp"

    def decorate(self, msg: str) -> str:
        if self.action == "fire_upgrade" and self.delta_line:
            return f"⬆ <b>UPGRADE</b> — {self.delta_line}\n{msg}"
        return msg


def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _ensure_tables(con: sqlite3.Connection) -> None:
    con.executescript("""
        CREATE TABLE IF NOT EXISTS alert_governor_state (
            pipeline   TEXT NOT NULL,
            entity     TEXT NOT NULL,
            outcome    TEXT NOT NULL DEFAULT '',
            magnitude  REAL,                -- NULL = unknown (seeded rows): suppress until rearm/flip
            direction  TEXT DEFAULT '',
            ts_alerted INTEGER NOT NULL,
            rearms     INTEGER NOT NULL DEFAULT 0,
            fires      INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (pipeline, entity, outcome)
        );
        CREATE TABLE IF NOT EXISTS alert_governor_log (
            ts INTEGER, pipeline TEXT, entity TEXT, action TEXT,
            reasons TEXT, mode TEXT
        );
    """)


def _seed_from_mlb_run_alert_state(con: sqlite3.Connection) -> None:
    """C4: import live in-slate cooldowns so first post-deploy pass doesn't
    re-alert every active game. Seeded magnitude is NULL (unknown) -> those legs
    suppress until direction flip or rearm elapses, preserving old behavior."""
    n = con.execute("SELECT COUNT(*) FROM alert_governor_state WHERE pipeline='mlb_run'").fetchone()[0]
    if n:
        return
    try:
        rows = con.execute("SELECT game_id, last_sent_ts, last_signature FROM mlb_run_alert_state").fetchall()
    except sqlite3.OperationalError:
        return  # table absent (fresh install / tests)
    now = int(time.time())
    for r in rows:
        try:
            ts = int(datetime.fromisoformat(r["last_sent_ts"]).timestamp())
        except (TypeError, ValueError):
            ts = now
        for part in (r["last_signature"] or "").split("|"):
            if ":" not in part:
                continue
            direction, outcome = part.split(":", 1)
            con.execute(
                "INSERT OR IGNORE INTO alert_governor_state "
                "(pipeline, entity, outcome, magnitude, direction, ts_alerted) "
                "VALUES ('mlb_run', ?, ?, NULL, ?, ?)",
                (r["game_id"], outcome, direction, ts),
            )


def _escalates(now_mag: float, last_mag: float, delta: float, mode: str) -> bool:
    """True when movement since the last alert counts as an escalation.

    "abs" measures movement in either direction; "increase" only counts growth.
    See the delta_mode note on _DEFAULT for why this is per-pipeline and not
    a global switch — flipping mlb_run to "abs" would re-page on SHRINKING
    edges, which is a de-escalation.
    """
    if mode == "abs":
        return abs(now_mag - last_mag) >= delta
    return now_mag >= last_mag + delta


def govern(pipeline: str, entity: str, legs: List[Leg],
           db_path: Optional[Path] = None, _now: Optional[float] = None) -> Verdict:
    """Decide fire/suppress for one alert (possibly multi-leg, C5: state per leg).

    Callers MUST complete audit/shadow logging before calling this.
    Returns fail-open (fire) on unexpected internal errors; suppresses on
    persistent lock contention (a peer is handling the same entity).
    """
    cfg = CONFIG.get(pipeline, _DEFAULT)
    mode = cfg["mode"]
    now = int(_now if _now is not None else time.time())
    if not legs:
        return Verdict("suppress", mode != "enforce", mode, ["no-legs"])

    try:
        con = _connect(db_path or DB_PATH)
    except Exception as ex:
        print(f"[governor] connect failed, fail-open: {ex}", flush=True)
        return Verdict("fire", True, mode, [f"error:{ex}"])

    try:
        for attempt in (1, 2):
            try:
                con.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError:
                if attempt == 2:
                    # C1: contention = a peer holds this decision -> suppress, never double-send
                    _log_best_effort(db_path or DB_PATH, now, pipeline, entity, "suppress", "lock-contention", mode)
                    return Verdict("suppress", mode != "enforce", mode, ["lock-contention"])
                time.sleep(0.25)

        _ensure_tables(con)
        if pipeline == "mlb_run":
            _seed_from_mlb_run_alert_state(con)

        delta = cfg.get("delta")
        rearm_min = cfg.get("rearm_min")
        max_rearms = cfg.get("max_rearms", 0)
        delta_mode = cfg.get("delta_mode", "increase")

        reasons: List[str] = []
        deltas: List[str] = []
        any_new_or_flip = False
        any_upgrade = False
        any_rearm = False

        rows = {}
        for leg in legs:
            row = con.execute(
                "SELECT magnitude, direction, ts_alerted, rearms FROM alert_governor_state "
                "WHERE pipeline=? AND entity=? AND outcome=?",
                (pipeline, entity, leg.outcome),
            ).fetchone()
            rows[leg.outcome] = row
            if row is None:
                any_new_or_flip = True
                reasons.append(f"new:{leg.outcome}")
            elif leg.direction and row["direction"] and leg.direction != row["direction"]:
                any_new_or_flip = True
                reasons.append(f"flip:{leg.outcome}:{row['direction']}->{leg.direction}")
            elif delta is not None and row["magnitude"] is not None \
                    and _escalates(leg.magnitude, float(row["magnitude"]), delta, delta_mode):
                any_upgrade = True
                at = datetime.fromtimestamp(row["ts_alerted"], tz=timezone.utc).strftime("%H:%M")
                deltas.append(f"{leg.outcome}: {float(row['magnitude']):.0f} @ {at} → {leg.magnitude:.0f}")
                reasons.append(f"upgrade:{leg.outcome}")
            elif rearm_min and (now - row["ts_alerted"]) >= rearm_min * 60 \
                    and row["rearms"] < max_rearms:  # C7: capped
                any_rearm = True
                reasons.append(f"rearm:{leg.outcome}")

        if any_new_or_flip or any_upgrade or any_rearm:
            action = "fire_upgrade" if (any_upgrade and not any_new_or_flip) else "fire"
            rearm_only = any_rearm and not any_new_or_flip and not any_upgrade
            for leg in legs:
                prev = rows[leg.outcome]
                rearms = (prev["rearms"] + 1) if (rearm_only and prev is not None) else 0
                con.execute(
                    "INSERT OR REPLACE INTO alert_governor_state "
                    "(pipeline, entity, outcome, magnitude, direction, ts_alerted, rearms, fires) "
                    "VALUES (?,?,?,?,?,?,?,COALESCE((SELECT fires+1 FROM alert_governor_state "
                    "WHERE pipeline=? AND entity=? AND outcome=?),1))",
                    (pipeline, entity, leg.outcome, leg.magnitude, leg.direction, now, rearms,
                     pipeline, entity, leg.outcome),
                )
        else:
            action = "suppress"

        con.execute(
            "INSERT INTO alert_governor_log (ts, pipeline, entity, action, reasons, mode) VALUES (?,?,?,?,?,?)",
            (now, pipeline, entity, action, ",".join(reasons) or "same-state", mode),
        )
        con.commit()

        should_send = True if mode == "shadow" else (action != "suppress")
        return Verdict(action, should_send, mode, reasons, "; ".join(deltas))

    except Exception as ex:
        # Blind spot #9: governor bugs must not kill alerts — fail open.
        try:
            con.rollback()
        except Exception:
            pass
        print(f"[governor] internal error, fail-open: {ex}", flush=True)
        return Verdict("fire", True, mode, [f"error:{ex}"])
    finally:
        try:
            con.close()
        except Exception:
            pass


def _log_best_effort(db_path: Path, ts: int, pipeline: str, entity: str,
                     action: str, reasons: str, mode: str) -> None:
    try:
        con = _connect(db_path)
        _ensure_tables(con)
        con.execute(
            "INSERT INTO alert_governor_log (ts, pipeline, entity, action, reasons, mode) VALUES (?,?,?,?,?,?)",
            (ts, pipeline, entity, action, reasons, mode),
        )
        con.commit()
        con.close()
    except Exception:
        pass


def purge_stale(db_path: Optional[Path] = None, max_age_days: int = 7) -> int:
    """Blind spot #8: drop entity states older than max_age_days. Call from a daily task."""
    try:
        con = _connect(db_path or DB_PATH)
        _ensure_tables(con)
        cutoff = int(time.time()) - max_age_days * 86400
        cur = con.execute("DELETE FROM alert_governor_state WHERE ts_alerted < ?", (cutoff,))
        con.execute("DELETE FROM alert_governor_log WHERE ts < ?", (cutoff,))
        con.commit()
        n = cur.rowcount
        con.close()
        return n
    except Exception as ex:
        print(f"[governor] purge failed: {ex}", flush=True)
        return 0

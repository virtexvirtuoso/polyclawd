#!/usr/bin/env python3
"""
alert_dispatch.py — tiered alert dispatch queue (Phase 5 of the 2026-07-16
alert-system overhaul; docs/plans/2026-07-16-alert-system-overhaul.md).

Extends the `signals/alert_governor.py` pattern: state in shadow_trades.db
(WAL, cross-process, restart-proof — the health-check cron restarts the
scheduler ~every 15 min, so timing decisions live in DB timestamps, never
process memory).

Tiers:
  1 (TIER_CRITICAL) — send immediately; on failure enqueue for redelivery
                      on the next drain (D2: the queue IS the durable retry).
  2 (TIER_BATCH)    — enqueue; drain() flushes rows older than 15 min as ONE
                      message per pipeline group.
  3 (TIER_DIGEST)   — enqueue; drain_digest() (2x-daily cron) flushes ALL rows
                      as one sectioned digest. Exempt from the 6h sweep (own
                      15h cap) — plan Change 3, critic finding 2026-07-22.
  4 (TIER_SUPPRESS) — never send; log to alert_suppressed_log only.

shadow=True (rollout mode): enqueue with shadow=1; drain() only RECORDS what
it would have batched (alert_shadow_log) — it never sends shadow rows. The
caller keeps its direct send during shadow.

Delivery semantics are at-least-once, explicitly: rows are deleted AFTER the
send returns ok — a crash in between duplicates rather than drops (F6).
Requeued tier-1 messages carry a "(redelivery)" prefix so an
ambiguous-timeout duplicate is self-explaining. Rows older than 6h are
dropped to the suppressed log (F2: no infinite replay if drain stalls).
On sqlite lock contention, enqueue fails OPEN to a direct send (F4: a
duplicate is cheaper than a missed alert).
"""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timezone
from itertools import groupby
from pathlib import Path
from typing import Optional

from scripts.openclaw_alerts import alert_openclaw

# Batch/digest sends are forced parse_mode=None (see _batch_text) -- combining
# independently-authored HTML fragments under one HTML parse risks one bad
# fragment breaking the whole message. But callers (mlb_live_monitor.py,
# cross_sport_drift.py, soccer_live_monitor.py) hard-code <b>/<i> tags into
# their message text unconditionally, with no awareness they might get
# force-plain-texted here. Without stripping, those tags show up literally
# in the delivered Telegram message (found 2026-08-19, message #42709 -- same
# underlying mismatch class as the smart_wallet_alert.py convergence bug
# fixed earlier the same day, different code path).
_HTML_TAG_RE = re.compile(r"<[^>]+>")

DB_PATH = Path(__file__).resolve().parent.parent / "storage" / "shadow_trades.db"

TIER_CRITICAL, TIER_BATCH, TIER_DIGEST, TIER_SUPPRESS = 1, 2, 3, 4

BATCH_WINDOW_SEC = 15 * 60
MAX_AGE_SEC = 6 * 3600
# Tier-3 digest rows must survive from the morning flush to the 23:30 ET one —
# exempt from the 6h sweep, with a 15h cap so a dead flusher can't hoard forever.
MAX_AGE_DIGEST_SEC = 15 * 3600

# Intended routing per vault Alert-Router-Plan-80pct-Actionable §3 (2026-07-22).
# Callers pass tier explicitly; this map documents the plan for pipelines that
# adopt dispatch() without local tier logic. Edge-carrying odds_moved/run_scored
# events stay tier 1 AT THE CALLER (plan Changes 1-2) — the map holds the
# no-edge default.
DEFAULT_TIERS = {
    "entry": 1, "close": 1, "fade": 1, "new_edge": 1, "convergence": 1,
    "graduation": 1, "prop_edge": 1, "weather_fade": 1, "scanner_state_change": 1,
    "odds_moved": 3, "run_scored": 3, "hf_scan": 3, "wallet_moves": 3,
    "rising_wallets": 3, "leaderboard_wallets": 3, "ufc_drift": 3, "credit": 3,
    "soccer_scorers": 3,
    "spend_limit": 4, "agent_chat": 4, "timeout": 4, "stub": 4,
}


def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _ensure_tables(con: sqlite3.Connection) -> None:
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS alert_queue (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         INTEGER NOT NULL,
            pipeline   TEXT NOT NULL,
            tier       INTEGER NOT NULL,
            dedup_key  TEXT NOT NULL DEFAULT '',
            message    TEXT NOT NULL,
            parse_mode TEXT,
            shadow     INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS alert_suppressed_log (
            ts INTEGER, pipeline TEXT, dedup_key TEXT, message TEXT, reason TEXT
        );
        CREATE TABLE IF NOT EXISTS alert_shadow_log (
            ts INTEGER, pipeline TEXT, n_events INTEGER, message TEXT, tier INTEGER
        );
    """)
    # Migration (Gate 1 queryability): pre-2026-07-22 shadow logs lack `tier`.
    cols = [r[1] for r in con.execute("PRAGMA table_info(alert_shadow_log)")]
    if "tier" not in cols:
        con.execute("ALTER TABLE alert_shadow_log ADD COLUMN tier INTEGER")


def _begin_immediate(con: sqlite3.Connection) -> bool:
    """Governor-style: retry once on contention, then give up (caller decides)."""
    for attempt in (1, 2):
        try:
            con.execute("BEGIN IMMEDIATE")
            return True
        except sqlite3.OperationalError:
            if attempt == 2:
                return False
            time.sleep(0.25)
    return False


def _try_enqueue(db_path: Path, now: int, pipeline: str, tier: int, dedup_key: str,
                 message: str, parse_mode, shadow: int) -> Optional[bool]:
    """True = inserted, False = dedup-ignored, None = contention/error (fail open)."""
    try:
        con = _connect(db_path)
    except Exception as ex:  # noqa: BLE001
        print(f"[dispatch] connect failed: {ex}", flush=True)
        return None
    try:
        _ensure_tables(con)
        if not _begin_immediate(con):
            return None
        if dedup_key:
            dup = con.execute(
                "SELECT 1 FROM alert_queue WHERE pipeline=? AND dedup_key=? LIMIT 1",
                (pipeline, dedup_key)).fetchone()
            if dup:
                con.commit()
                return False
        con.execute(
            "INSERT INTO alert_queue (ts, pipeline, tier, dedup_key, message, parse_mode, shadow) "
            "VALUES (?,?,?,?,?,?,?)",
            (now, pipeline, tier, dedup_key, message, parse_mode, shadow))
        con.commit()
        return True
    except Exception as ex:  # noqa: BLE001 — dispatch bugs must not kill alerts
        print(f"[dispatch] enqueue failed: {ex}", flush=True)
        try:
            con.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass


def _suppress_log(con: sqlite3.Connection, ts: int, pipeline: str, dedup_key: str,
                  message: str, reason: str) -> None:
    con.execute(
        "INSERT INTO alert_suppressed_log (ts, pipeline, dedup_key, message, reason) "
        "VALUES (?,?,?,?,?)", (ts, pipeline, dedup_key, message, reason))


def dispatch(pipeline: str, message: str, tier: int, *,
             parse_mode=None, dedup_key: str = "", shadow: bool = False,
             db_path=None) -> bool:
    """Tier 1 -> alert_openclaw() immediately; if that returns False, ALSO
               enqueue for redelivery on next drain (D2: queue = durable retry).
    Tier 2 -> INSERT INTO alert_queue(ts, pipeline, tier, dedup_key, message);
              duplicate (pipeline, dedup_key) within open batch is ignored.
    Tier 3 -> enqueue for the 2x-daily digest (drain_digest); same fail-open
              contention semantics as tier 2; exempt from the 6h sweep.
    Tier 4 -> INSERT INTO alert_suppressed_log only. Returns True.
    shadow=True (rollout mode): enqueue with shadow=1 + log, but drain() only
              RECORDS what it would have batched (alert_shadow_log) — it never
              sends shadow rows. Caller keeps its direct send during shadow.
    On sqlite lock contention: fail OPEN to direct send (F4)."""
    path = Path(db_path) if db_path else DB_PATH
    now = int(time.time())

    if shadow:
        # Never send here — the caller is still direct-sending during shadow.
        _try_enqueue(path, now, pipeline, tier, dedup_key, message, parse_mode, shadow=1)
        return True

    if tier == TIER_SUPPRESS:
        try:
            con = _connect(path)
            try:
                _ensure_tables(con)
                _suppress_log(con, now, pipeline, dedup_key, message, "tier4")
                con.commit()
            finally:
                con.close()
        except Exception as ex:  # noqa: BLE001 — suppression is best-effort
            print(f"[dispatch] suppress-log failed: {ex}", flush=True)
        return True

    if tier in (TIER_BATCH, TIER_DIGEST):
        # REAL FIX (2026-08-19, message #42709): tier-2/3 are ALWAYS delivered
        # parse_mode=None (drain()/drain_digest() hardcode it), but the
        # formatters (mlb_live_monitor.py, cross_sport_drift.py,
        # soccer_live_monitor.py) embed <b>/<i> tags assuming HTML for their
        # tier-1 direct sends. Normalize at the choke point where the
        # parse-mode decision is made: strip tags at enqueue so the queue
        # stores exactly what will actually be sent (plain text), instead of
        # HTML that gets force-plain-texted downstream and shows raw tags.
        # The _batch_text() strip below is now a redundant safety net.
        message = _HTML_TAG_RE.sub("", message)
        queued = _try_enqueue(path, now, pipeline, tier, dedup_key, message, parse_mode, shadow=0)
        if queued is None:  # F4: contention -> fail open to direct send
            return bool(alert_openclaw(message, parse_mode=parse_mode))
        return True

    # TIER_CRITICAL (and any unknown tier: fail open to immediate send)
    ok = bool(alert_openclaw(message, parse_mode=parse_mode))
    if not ok:
        _try_enqueue(path, now, pipeline, TIER_CRITICAL, dedup_key, message, parse_mode, shadow=0)
    return ok


def _batch_text(pipeline: str, rows: list) -> str:
    fmt = lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M")  # noqa: E731
    header = (f"📨 {pipeline} — {len(rows)} events "
              f"({fmt(min(r['ts'] for r in rows))}–{fmt(max(r['ts'] for r in rows))})")
    # Row messages are authored by individual formatters (mlb_live_monitor.py,
    # cross_sport_drift.py, soccer_live_monitor.py) that hard-code <b>/<i>
    # tags assuming parse_mode=HTML. This batcher always sends parse_mode=None
    # (see callers below), so raw tags must be stripped or they show up
    # literally in Telegram (found 2026-08-19, message #42709).
    body = [_HTML_TAG_RE.sub("", r["message"]) for r in rows]
    return "\n".join([header] + body)


def _delete_ids(con: sqlite3.Connection, ids: list) -> None:
    con.executemany("DELETE FROM alert_queue WHERE id=?", [(i,) for i in ids])
    con.commit()


def drain(db_path=None, now=None, force=False) -> int:
    """Called every 5-min tick. Flushes tier-2 rows older than 15 min as ONE
    message per pipeline group; resends queued tier-1 redeliveries first.
    DB timestamps drive timing -> restart-proof. Returns messages sent."""
    path = Path(db_path) if db_path else DB_PATH
    if now is None:
        now = time.time()
    elif isinstance(now, datetime):
        now = now.timestamp()
    now = int(now)
    cutoff = now + 10**9 if force else now - BATCH_WINDOW_SEC

    try:
        con = _connect(path)
    except Exception as ex:  # noqa: BLE001
        print(f"[dispatch] drain connect failed: {ex}", flush=True)
        return 0
    try:
        _ensure_tables(con)
        if not _begin_immediate(con):
            return 0  # a peer holds the DB — next tick retries

        # F2: drop stale rows to the suppressed log — no infinite replay.
        # Tier-3 digest rows are EXEMPT from the 6h sweep (they must survive to
        # the 23:30 flush) and instead get a 15h cap (plan Change 3).
        stale_pred = "(tier != ? AND ts < ?) OR (tier = ? AND ts < ?)"
        stale_args = (TIER_DIGEST, now - MAX_AGE_SEC, TIER_DIGEST, now - MAX_AGE_DIGEST_SEC)
        stale = con.execute(
            f"SELECT * FROM alert_queue WHERE {stale_pred}", stale_args).fetchall()
        for r in stale:
            reason = "expired>6h" if r["tier"] != TIER_DIGEST else "expired>15h"
            _suppress_log(con, now, r["pipeline"], r["dedup_key"], r["message"], reason)
        if stale:
            con.execute(f"DELETE FROM alert_queue WHERE {stale_pred}", stale_args)

        # Shadow rows past the window: record what WOULD have been batched, never send.
        shadow_rows = con.execute(
            "SELECT * FROM alert_queue WHERE shadow=1 AND ts <= ? ORDER BY pipeline, tier, ts",
            (cutoff,)).fetchall()
        for (pipeline, tier), grp in groupby(shadow_rows, key=lambda r: (r["pipeline"], r["tier"])):
            grp = list(grp)
            con.execute(
                "INSERT INTO alert_shadow_log (ts, pipeline, n_events, message, tier) "
                "VALUES (?,?,?,?,?)",
                (now, pipeline, len(grp), _batch_text(pipeline, grp), tier))
        if shadow_rows:
            con.execute("DELETE FROM alert_queue WHERE shadow=1 AND ts <= ?", (cutoff,))

        # Read work items, then COMMIT so the write lock isn't held across HTTP sends.
        redeliveries = con.execute(
            "SELECT * FROM alert_queue WHERE shadow=0 AND tier=? ORDER BY ts",
            (TIER_CRITICAL,)).fetchall()
        due = con.execute(
            "SELECT * FROM alert_queue WHERE shadow=0 AND tier=? AND ts <= ? "
            "ORDER BY pipeline, ts", (TIER_BATCH, cutoff)).fetchall()
        con.commit()

        sent = 0
        # Tier-1 redeliveries first — overdue criticals beat batches (F6: at-least-once,
        # delete only AFTER the send returns ok; failures stay queued for next tick).
        for r in redeliveries:
            msg = r["message"] if r["message"].startswith("(redelivery)") \
                else f"(redelivery) {r['message']}"
            if alert_openclaw(msg, parse_mode=r["parse_mode"]):
                _delete_ids(con, [r["id"]])
                sent += 1

        # Tier-2 batches: one combined plain-text message per pipeline group.
        for pipeline, grp in groupby(due, key=lambda r: r["pipeline"]):
            grp = list(grp)
            if alert_openclaw(_batch_text(pipeline, grp), parse_mode=None):
                _delete_ids(con, [r["id"] for r in grp])
                sent += 1
        return sent
    except Exception as ex:  # noqa: BLE001 — drain bugs must not kill the tick task
        print(f"[dispatch] drain error: {ex}", flush=True)
        try:
            con.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass


def _is_noise(row) -> bool:
    """True for no-edge tier-3 rows that are pure heartbeat (no actionable
    signal). 2026-08-24: filtered from digest body, counted in summary only."""
    msg = row["message"] or ""
    return "no PM gap" in msg


def drain_digest(db_path=None, now=None) -> int:
    """2x-daily digest flusher (plan Change 3; cron 10:00 / 23:30 ET). Flushes
    ALL non-shadow tier-3 rows regardless of age as ONE combined message with a
    section per pipeline. Deliberately NOT drain(force=True) — that would also
    flush tier-2 batches early and break their 15-min semantics. At-least-once:
    rows are deleted only after the send returns ok.

    2026-08-24: 'no PM gap' noise rows are counted in the summary header but
    excluded from the detail body — every line in the digest is now actionable.
    When there are zero signal rows, a one-line heartbeat is sent instead of an
    empty body."""
    path = Path(db_path) if db_path else DB_PATH
    if now is None:
        now = time.time()
    elif isinstance(now, datetime):
        now = now.timestamp()
    now = int(now)
    try:
        con = _connect(path)
    except Exception as ex:  # noqa: BLE001
        print(f"[dispatch] digest connect failed: {ex}", flush=True)
        return 0
    try:
        _ensure_tables(con)
        if not _begin_immediate(con):
            return 0  # a peer holds the DB — the next cron slot retries
        rows = con.execute(
            "SELECT * FROM alert_queue WHERE shadow=0 AND tier=? ORDER BY pipeline, ts",
            (TIER_DIGEST,)).fetchall()
        con.commit()
        if not rows:
            return 0

        noise_rows = [r for r in rows if _is_noise(r)]
        signal_rows = [r for r in rows if not _is_noise(r)]
        n_total = len(rows)
        n_noise = len(noise_rows)
        n_signal = len(signal_rows)

        label = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%b %d %H:%M UTC")

        if not signal_rows:
            # Heartbeat only — system alive, nothing actionable.
            header = (f"📊 Digest — {label} — {n_total} events | "
                      f"{n_noise} noise | 0 signals\n"
                      f"(No actionable edges this window. System watching.)")
            if alert_openclaw(header, parse_mode=None):
                _delete_ids(con, [r["id"] for r in rows])
                return 1
            return 0

        # Summary header + detail sections for signal rows only.
        header = (f"📊 Digest — {label} — {n_total} events | "
                  f"{n_noise} noise | {n_signal} signals")
        sections = [header]
        for pipeline, grp in groupby(signal_rows, key=lambda r: r["pipeline"]):
            sections.append(_batch_text(pipeline, list(grp)))
        if alert_openclaw("\n\n".join(sections), parse_mode=None):
            _delete_ids(con, [r["id"] for r in rows])
            return 1
        return 0
    except Exception as ex:  # noqa: BLE001 — digest bugs must not kill the cron
        print(f"[dispatch] digest error: {ex}", flush=True)
        try:
            con.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass

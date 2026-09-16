#!/usr/bin/env python3
"""
entry_reasoning_check.py — assert the invariant "every live position has a
documented entry trigger".

Every live_positions row should have a matching live_entry_reasoning row
explaining WHY the trade was taken. Four write paths must all comply:

    smart_wallet_fast_poll.py -> execute_intent -> record_real_fill(reasoning=)
    soccer_executor.py        -> execute_intent -> record_real_fill(reasoning=)
    weather_executor.py       -> execute_intent -> record_real_fill(reasoning=)
    position_sync.py          -> record_entry_reasoning(trigger_source='position_sync')

Born 2026-08-25 from the "why was this trade taken?" incident: position #13
(Cincinnati Open, Fritz vs Nakashima) could not be explained after the fact.
The reasoning plumbing exists now; this proves it keeps working, and catches
any FUTURE write path that forgets.

READ-ONLY against the ledger — the DB is opened with mode=ro so this can never
write to shadow_trades.db. Only its own state file is written.

Exit codes: 0 = healthy (or alert sent), 1 = violation found but send failed.

    venv/bin/python3 scripts/entry_reasoning_check.py            # report only
    venv/bin/python3 scripts/entry_reasoning_check.py --send     # alert on violations
    venv/bin/python3 scripts/entry_reasoning_check.py --test-alert  # prove the alert path
"""

import argparse
import json
import logging
import os
import sqlite3
import time
import sys
from datetime import datetime, timezone

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

from scripts.alert_formatter import send_telegram  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("entry_reasoning_check")

BASE_DIR = _BASE_DIR
DB_PATH = os.path.join(BASE_DIR, "storage", "shadow_trades.db")
STATE_FILE = os.path.join(BASE_DIR, "storage", "entry_reasoning_check_state.json")

# Positions predating the reasoning plumbing could never have a row — expected
# gaps, not failures. Counted and reported explicitly, never silently suppressed
# (a monitor that hides its exclusions is indistinguishable from a broken one).
#
# BOUNDARY IS AN ID, NOT A TIMESTAMP (fixed 2026-08-26 after review REJECTED the
# first version). The original compared `opened_at >= "2026-08-22T01:40:00"` as
# STRINGS. `live_positions.opened_at` is TEXT with mixed formats — production
# already holds both "2026-06-27 01:14:00" (len 19, space) and ISO-T (len 32) —
# and at index 10 ' ' (0x20) < 'T' (0x54), so ANY same-day space-separated
# timestamp sorted BELOW the cutoff and was silently reclassified as "legacy".
# NULL/""/date-only/epoch-as-text did the same. The monitor then printed
# "OK — invariant holds" over real violations: a false all-clear, which is the
# exact failure this script exists to prevent.
# (Fleet ledger 2026-08-18 warns about precisely this 'T'-vs-' ' comparison.)
#
# `id` is INTEGER PRIMARY KEY AUTOINCREMENT and monotonic, so an id boundary is
# immune to timestamp format drift. Ids 1-13 are exactly the pre-feature set.
LEGACY_MAX_POSITION_ID = 13


def load_state() -> dict:
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, STATE_FILE)


# SQLite transient errors that warrant a retry rather than a crash.
# "unable to open database file" fires when WAL checkpoint holds a lock;
# "database is locked" fires when another writer is mid-transaction.
_RETRYABLE_ERRORS = {"unable to open database file", "database is locked"}

# Max retries with exponential backoff: 0.5s, 1.0s, 2.0s, 4.0s = 7.5s total worst case.
_MAX_RETRIES = 4
_BASE_BACKOFF = 0.5


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, sqlite3.OperationalError):
        msg = str(exc).lower()
        return any(needle in msg for needle in _RETRYABLE_ERRORS)
    return False


def query(db_path: str) -> tuple[list[dict], list[dict], int]:
    """Return (violations, legacy_gaps, covered_count). Read-only."""
    last_exc = None
    for attempt in range(_MAX_RETRIES):
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """
                    SELECT p.id, p.market_title, p.archetype, p.status, p.opened_at,
                           (SELECT COUNT(*) FROM live_entry_reasoning r
                             WHERE r.position_id = p.id) AS reasoning_rows
                      FROM live_positions p
                     ORDER BY p.id
                    """
                ).fetchall()
            finally:
                conn.close()
            break  # success
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if _is_retryable(exc) and attempt < _MAX_RETRIES - 1:
                backoff = _BASE_BACKOFF * (2 ** attempt)
                logger.warning(
                    "SQLite transient error (attempt %d/%d): %s — retrying in %.1fs",
                    attempt + 1, _MAX_RETRIES, exc, backoff,
                )
                time.sleep(backoff)
                continue
            raise  # non-retryable or exhausted retries
    else:
        raise last_exc  # should never reach here, but satisfy type checker

    violations, legacy, covered = [], [], 0
    for r in rows:
        rec = {
            "id": r["id"],
            "title": (r["market_title"] or "")[:48],
            "archetype": r["archetype"],
            "status": r["status"],
            "opened_at": r["opened_at"] or "",
        }
        if r["reasoning_rows"] > 0:
            covered += 1
        elif rec["id"] > LEGACY_MAX_POSITION_ID:
            # Fails CLOSED: anything after the boundary with no reasoning row is a
            # violation regardless of how (or whether) opened_at is formatted.
            violations.append(rec)
        else:
            legacy.append(rec)
    return violations, legacy, covered


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--send", action="store_true", help="send a Telegram alert on violations")
    ap.add_argument("--test-alert", action="store_true",
                    help="send one harmless test alert to prove the delivery path, then exit")
    args = ap.parse_args()

    if args.test_alert:
        ok = send_telegram(
            "🧪 <b>entry_reasoning_check</b> — install test\n\n"
            "Alert path verified. This monitor asserts that every live position "
            "has a documented entry trigger.\n"
            "If you see this once at install, delivery works."
        )
        logger.info("test alert sent=%s", ok)
        return 0 if ok else 1

    violations, legacy, covered = query(args.db)
    total = covered + len(violations) + len(legacy)

    # Always log a full summary line — a watcher that only speaks on failure is
    # indistinguishable from a dead one.
    logger.info(
        "positions=%d covered=%d violations=%d legacy_id_le_%s=%d",
        total, covered, len(violations), str(LEGACY_MAX_POSITION_ID), len(legacy),
    )
    for r in legacy:
        logger.info("  legacy (expected, pre-feature): #%s %s [%s]",
                    r["id"], r["title"], r["opened_at"][:10])

    if not violations:
        logger.info("OK — invariant holds: every post-feature position has an entry trigger")
        return 0

    for r in violations:
        logger.error("VIOLATION: position #%s (%s, %s) has NO live_entry_reasoning row",
                     r["id"], r["title"], r["archetype"])

    if not args.send:
        logger.info("(--send not passed; no alert dispatched)")
        return 0

    # Alert once per position id, not once per run.
    state = load_state()
    alerted = state.setdefault("alerted", {})
    fresh = [r for r in violations if str(r["id"]) not in alerted]
    if not fresh:
        logger.info("all %d violation(s) already alerted — staying quiet", len(violations))
        return 0

    lines = [
        "⚠️ <b>Live position with NO entry trigger</b>",
        "",
        f"{len(fresh)} position(s) written without a live_entry_reasoning row — "
        "we cannot explain why these trades were taken.",
        "",
    ]
    for r in fresh:
        lines.append(f"• #{r['id']} {r['title']} — archetype <code>{r['archetype']}</code> ({r['status']})")
    lines += [
        "",
        "A write path is not passing <code>reasoning=</code>. Check which executor "
        "produced it via <code>live_open_orders.client_order_ref</code>.",
    ]

    if not send_telegram("\n".join(lines)):
        logger.error("alert FAILED to send for %d violation(s)", len(fresh))
        return 1

    now = datetime.now(timezone.utc).isoformat()
    for r in fresh:
        alerted[str(r["id"])] = {"alerted_at": now, "archetype": r["archetype"]}
    save_state(state)
    logger.info("alerted on %d new violation(s)", len(fresh))
    return 0


if __name__ == "__main__":
    # MONITOR_FAILURE_ALERT 2026-08-26: previously an unexpected exception (missing
    # DB, "database is locked", permissions) landed as a traceback in the log file,
    # cron exited non-zero, and nobody was told -- the invariant silently stopped
    # being checked. A dead watcher is a quiet watcher. Alert on the way out.
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - last line of defence before cron
        logger.exception("entry_reasoning_check FAILED")
        try:
            send_telegram(
                "\u26a0\ufe0f <b>entry_reasoning_check CRASHED</b>\n\n"
                "The live-position entry-trigger invariant is NO LONGER BEING CHECKED.\n\n"
                f"<code>{type(exc).__name__}: {str(exc)[:300]}</code>"
            )
        except Exception:
            logger.error("could not deliver crash alert")
        sys.exit(1)

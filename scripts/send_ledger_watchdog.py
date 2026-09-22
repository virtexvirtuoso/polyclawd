#!/usr/bin/env python3
"""Daily failure watchdog over the fleet send ledger (logs/telegram_sent.jsonl).

Every delivery attempt through alert_openclaw()/send_telegram() appends one
JSON line: {ts, caller, channel, ok, parse_mode, len[, err]}. This watchdog
scans the last N hours and alerts (plain text — never Markdown, per the
kalshi_fade 400 incident) ONLY when failures exist. Silent on clean days.

Born from the 2026-07 audits: kalshi_fade dropped 24 consecutive daily reports
and the whale drain delivered into a dead consumer for ~3 weeks — both
invisible because nothing watched the failure channel.

Since 2026-09-21 this module also carries the digest-liveness and
shadow-stall dead-man's switches (check_digest_liveness / check_shadow_stall):
they run on EVERY invocation, before the ledger checks below — a missing
ledger must not silence them. Vault plan:
Plans/Alert-Durability-And-Refire-Reaudit-2026-09-21.md.

Usage:
    python3 scripts/send_ledger_watchdog.py            # alert if failures
    python3 scripts/send_ledger_watchdog.py --dry      # print instead of send
    python3 scripts/send_ledger_watchdog.py --hours 48
    python3 scripts/send_ledger_watchdog.py --hours 1 --min-rate 0.10
        # hourly mode (Task 5.4, 2026-07-16 overhaul): alarm ONLY when the
        # failure rate over the window is >= --min-rate AND failures >= 3
        # (MIN_ALARM_FAILS) — an isolated blip stays silent, a burst alarms.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


MIN_ALARM_FAILS = 3  # --min-rate mode: below this many failures, never alarm


def ledger_path() -> Path:
    return Path(os.environ.get("POLYCLAWD_LEDGER_PATH") or BASE / "logs" / "telegram_sent.jsonl")


def degraded_state_path() -> Path:
    return Path(os.environ.get("POLYCLAWD_DEGRADED_STATE_PATH") or BASE / "logs" / "degraded_watchdog_state.json")


def check_degraded(path: Path, dry: bool) -> None:
    """Surface silent HTML-escaping bugs the failure-rate watchdog above can
    never see: entries logged ok=true but with err starting 'degraded:'
    (Telegram rejected the HTML — 'can't parse entities' — so the sender
    retried plain and it delivered "successfully", just ugly: raw <b> tags,
    collapsed formatting). Born from the 2026-08-19 smart_wallet_alert.py
    unescaped-'<' bug, which this exact class of check would have caught
    the same hour instead of waiting for Mr. V to flag it visually.

    State-tracked by last-seen ts (logs/degraded_watchdog_state.json) —
    ignores --hours entirely, scans the whole ledger, alerts once per NEW
    occurrence, then goes quiet on that same backlog. Safe to call every
    run (hourly + daily); cheap, and catches the bug within the hour."""
    state_path = degraded_state_path()
    last_ts = ""
    if state_path.exists():
        try:
            last_ts = json.loads(state_path.read_text()).get("last_ts", "")
        except (json.JSONDecodeError, OSError):
            pass

    found: dict = defaultdict(lambda: {"n": 0, "err": ""})
    newest_ts = last_ts
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        err = rec.get("err", "")
        if not err.startswith("degraded:"):
            continue
        ts = rec.get("ts", "")
        if last_ts and ts <= last_ts:
            continue
        f = found[rec.get("caller", "unknown")]
        f["n"] += 1
        f["err"] = err
        if ts > newest_ts:
            newest_ts = ts

    if not found:
        print("degraded check: no new HTML-escape fallbacks since last run")
        return

    lines = [
        f"⚠️ FORMATTING BUG: {sum(f['n'] for f in found.values())} alert(s) silently "
        f"degraded to plain text (unescaped HTML — check Telegram for raw <b> tags)"
    ]
    for caller, f in sorted(found.items(), key=lambda kv: -kv[1]["n"]):
        lines.append(f"  {caller}: {f['n']}x — {f['err']}")
    text = "\n".join(lines)

    print(text)
    if not dry:
        from scripts.openclaw_alerts import alert_openclaw

        alert_openclaw(text, parse_mode=None)
        state_path.write_text(json.dumps({"last_ts": newest_ts}))
    else:
        print(f"[dry] would advance degraded state to last_ts={newest_ts}")


# --- Digest liveness + shadow-stall dead-man's switches --------------------- #
# (2026-09-21 durability plan, vault Plans/Alert-Durability-And-Refire-
# Reaudit-2026-09-21.md — review + ultraplan pass-2 sections.)
#
# The tier-3 digest (drain_digest, cron 10:00/23:30 ET via
# ~/bin/alert-digest-drain.sh) died silently twice in Sep 2026: Sep 5-13
# (sqlite3.Row .get() crash — every flush logged 'digest error'), and the
# wallet-feed blackout (Aug 25 -> Sep 19) left the queue empty while the
# pipeline above it was dead. "No digest" is indistinguishable from "nothing
# happened" — these checks make silence page.
#
# Keyed on the flusher's OWN log, NOT the send ledger: quiet flushes
# (sent_batches=0) write no ledger entry, and kalshi_fade_report /
# mlb_prop_gate2_report share the 14:00-UTC cron window and would mask a dead
# digest (both flaws proven in the 2026-09-21 pre-execution review).

DIGEST_STALE_H = 26.0  # flushes <=10.5h apart; 26h = two missed flushes
DIGEST_ERROR_WINDOW_H = 26.0  # any error line newer than this pages
QUEUE_MAX = 60  # healthy max ~35 at observed ~80 alerts/day
SHADOW_STALL_H = 36.0  # largest legit inter-alert gap ever: 27.5h (Jun 29)
REPAGE_COOLDOWN_H = 12.0

_TS_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+(.*)$")
_ERROR_REST_RE = re.compile(r"^\[dispatch\] digest error:")
_HEARTBEAT_REST_RE = re.compile(r"^(?:sent_batches=|\[dispatch\] digest error:)")


def _digest_log_path() -> Path:
    return Path(os.environ.get("POLYCLAWD_DIGEST_LOG_PATH") or Path.home() / "logs" / "alert-digest.log")


def _queue_db_path() -> Path:
    return Path(os.environ.get("POLYCLAWD_QUEUE_DB_PATH") or BASE / "storage" / "shadow_trades.db")


def _shadow_db_path() -> Path:
    return Path(os.environ.get("POLYCLAWD_SHADOW_DB_PATH") or BASE / "storage" / "shadow_trades.db")


def _liveness_state_path() -> Path:
    return Path(os.environ.get("POLYCLAWD_LIVENESS_STATE_PATH") or BASE / "logs" / "digest_liveness_state.json")


def _parse_z_ts(stamp: str) -> float:
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def _parse_digest_log(path: Path) -> tuple:
    """(last_heartbeat_ts, last_error_ts) from the flusher log; 0.0 = none.

    The crash case stamps the error line itself (the trailing bare
    'sent_batches=0' carries no ts of its own). The wrapper's 'ERROR: cd
    failed' line is neither heartbeat nor error: cron fired but drain_digest
    did not run, so a persistently broken wrapper goes stale and pages via
    the staleness prong, while transient lock contention ('digest connect
    failed') self-heals silently at the next flush.
    """
    heartbeat = error = 0.0
    try:
        text = path.read_text()
    except OSError:
        return heartbeat, error
    for line in text.splitlines():
        m = _TS_LINE_RE.match(line)
        if not m:
            continue
        ts = _parse_z_ts(m.group(1))
        if not ts:
            continue
        rest = m.group(2)
        if _ERROR_REST_RE.match(rest):
            error = max(error, ts)
        if _HEARTBEAT_REST_RE.match(rest):
            heartbeat = max(heartbeat, ts)
    return heartbeat, error


def _load_liveness_state() -> dict:
    try:
        return json.loads(_liveness_state_path().read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_liveness_state(state: dict) -> None:
    try:
        _liveness_state_path().write_text(json.dumps(state))
    except OSError:
        pass  # state is best-effort; losing it only risks one re-page


def _cooldown_open(pages: dict, key: str, now: float, dry: bool) -> bool:
    """True when `key` may page (cooldown elapsed); records the page unless dry."""
    if now - pages.get(key, 0.0) < REPAGE_COOLDOWN_H * 3600:
        return False
    if not dry:
        pages[key] = now
    return True


def _count_digest_queue():
    """Non-shadow tier-3 rows awaiting the digest; None = DB unreadable (skip)."""
    try:
        con = sqlite3.connect(f"file:{_queue_db_path()}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return None
    try:
        row = con.execute("SELECT COUNT(*) FROM alert_queue WHERE shadow=0 AND tier=3").fetchone()
        return int(row[0])
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()


def check_digest_liveness(dry: bool = False) -> list:
    """Dead-man's switch for the tier-3 digest flusher. Three prongs:

      1. STALE — no timestamped heartbeat in the flusher log within 26h (or
         log missing with no fresher state) -> cron lost / script deleted.
      2. CRASH — a '[dispatch] digest error:' line within the error window
         (the Sep 5-13 sqlite3.Row class). Re-pages each cooldown while live.
      3. QUEUE — >QUEUE_MAX non-shadow tier-3 rows in alert_queue while
         heartbeats look fresh -> silent swallow (the class 1-2 cannot see).

    A fresh 'sent_batches=0' with no error is a legitimately quiet day and
    never pages. Each prong re-pages at most once per REPAGE_COOLDOWN_H.
    Returns alert messages; the caller prints/sends them.
    """
    now = time.time()
    log_hb, log_err = _parse_digest_log(_digest_log_path())
    state = _load_liveness_state()
    pages = state.get("pages", {})
    heartbeat = max(log_hb, state.get("heartbeat_ts", 0.0))
    hb_age_h = (now - heartbeat) / 3600.0 if heartbeat else None
    alerts = []

    if hb_age_h is None or hb_age_h > DIGEST_STALE_H:
        if _cooldown_open(pages, "digest_stale", now, dry):
            seen = f"last heartbeat {hb_age_h:.1f}h ago" if hb_age_h is not None else "no heartbeat ever recorded"
            alerts.append(
                f"🚨 DIGEST STALE: flusher heartbeat stale — {seen} "
                f"(threshold {DIGEST_STALE_H:.0f}h). The 10:00/23:30 ET "
                "digests are NOT going out: check the alert-digest-drain "
                "crontab entries and ~/bin/alert-digest-drain.sh."
            )

    if log_err and now - log_err < DIGEST_ERROR_WINDOW_H * 3600:
        if _cooldown_open(pages, "digest_error", now, dry):
            alerts.append(
                "🚨 DIGEST CRASH: flusher logged '[dispatch] digest error:' "
                f"within the last {DIGEST_ERROR_WINDOW_H:.0f}h (last "
                f"{datetime.fromtimestamp(log_err, timezone.utc):%Y-%m-%dT%H:%M:%SZ}) "
                "— same class as the Sep 5-13 sqlite3.Row outage."
            )

    queued = _count_digest_queue()
    if queued is not None and queued > QUEUE_MAX:
        if _cooldown_open(pages, "digest_queue", now, dry):
            alerts.append(
                f"🚨 DIGEST QUEUE BACKLOG: {queued} non-shadow tier-3 rows "
                f"pending in alert_queue (threshold {QUEUE_MAX}) while the "
                "flusher reports fresh heartbeats — drain_digest is "
                "swallowing rows without erroring."
            )

    if not dry:
        new_state = {"heartbeat_ts": heartbeat, "pages": pages}
        if new_state != state:
            _save_liveness_state(new_state)
    hb_desc = f"{hb_age_h:.1f}h ago" if hb_age_h is not None else "never"
    err_desc = f"{(now - log_err) / 3600.0:.1f}h ago" if log_err else "never"
    q_desc = str(queued) if queued is not None else "n/a"
    print(f"digest liveness: heartbeat {hb_desc} | last error {err_desc} | tier-3 queue {q_desc}")
    return alerts


def check_shadow_stall(dry: bool = False) -> list:
    """Page when the smart-wallet shadow feed logs nothing for >36h.

    Calibrated 2026-09-21 over the full smart_wallet_shadows history: largest
    legitimate inter-alert gap 27.5h (2026-06-29), next 11.6h — a 26h
    threshold would have false-paged Jun 29. 36h sits 8.5h above the worst
    legit gap and would have caught the Aug 25 -> Sep 19 blackout (592h gap)
    within ~1.5 days. Empty/unreadable DB -> skip (cannot judge).
    """
    try:
        con = sqlite3.connect(f"file:{_shadow_db_path()}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return []
    try:
        row = con.execute("SELECT MAX(ts_alert) FROM smart_wallet_shadows").fetchone()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()
    if not row or row[0] is None:
        print("shadow stall: no shadows logged yet — skip")
        return []
    now = time.time()
    age_h = (now - int(row[0])) / 3600.0
    if age_h <= SHADOW_STALL_H:
        print(f"shadow stall: last shadow {age_h:.1f}h ago (threshold {SHADOW_STALL_H:.0f}h) — ok")
        return []
    state = _load_liveness_state()
    pages = state.get("pages", {})
    if not _cooldown_open(pages, "shadow_stall", now, dry):
        return []
    if not dry:
        state["pages"] = pages
        _save_liveness_state(state)
    print(f"shadow stall: last shadow {age_h:.1f}h ago (threshold {SHADOW_STALL_H:.0f}h)")
    return [
        f"🚨 SHADOW FEED STALL: no smart-wallet shadow logged for {age_h:.1f}h "
        f"(threshold {SHADOW_STALL_H:.0f}h; largest legit gap on record 27.5h) "
        "— scanner or dispatch likely dead. The Aug 25 -> Sep 19 blackout ran "
        "18+ days unnoticed."
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--dry", action="store_true", help="print report, never send")
    ap.add_argument(
        "--min-rate",
        type=float,
        default=None,
        help=f"alarm only if failure rate >= this fraction AND failures >= {MIN_ALARM_FAILS} (hourly mode)",
    )
    args = ap.parse_args()

    # Digest liveness + shadow stall (2026-09-21 durability plan): run on EVERY
    # invocation, BEFORE the ledger early-return below — a missing ledger must
    # not silence the dead-man's switches. A crash here must never block the
    # failure-rate logic, hence the broad except.
    for _check in (check_digest_liveness, check_shadow_stall):
        try:
            for _msg in _check(args.dry):
                print(_msg)
                if not args.dry:
                    from scripts.openclaw_alerts import alert_openclaw

                    alert_openclaw(_msg, parse_mode=None)
        except Exception as exc:  # noqa: BLE001
            print(f"{_check.__name__} failed: {exc}")

    path = ledger_path()
    if not path.exists():
        print("no ledger yet — nothing to watch")
        return

    # Runs every invocation (hourly + daily) regardless of --hours/--min-rate —
    # state-tracked separately, so this doesn't interact with the failure-rate
    # logic below. See check_degraded() docstring for why this check exists.
    check_degraded(path, args.dry)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    total = 0
    fails: dict = defaultdict(lambda: {"n": 0, "last_err": ""})
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
            ts = datetime.fromisoformat(rec["ts"])
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        if ts < cutoff:
            continue
        total += 1
        if not rec.get("ok"):
            f = fails[rec.get("caller", "unknown")]
            f["n"] += 1
            if rec.get("err"):
                f["last_err"] = rec["err"]

    if not fails:
        print(f"clean: {total} deliveries in last {args.hours:.0f}h, 0 failures")
        return

    n_failed = sum(f["n"] for f in fails.values())
    rate = n_failed / total if total else 0.0

    if args.min_rate is not None and (n_failed < MIN_ALARM_FAILS or rate < args.min_rate):
        print(
            f"below threshold: {n_failed}/{total} failed ({100 * rate:.0f}%) "
            f"in last {args.hours:.0f}h — no alarm "
            f"(need rate >= {100 * args.min_rate:.0f}% and >= {MIN_ALARM_FAILS} failures)"
        )
        return

    prefix = "🚨 " if args.min_rate is not None else ""
    lines = [
        f"{prefix}SEND LEDGER: {n_failed} failed "
        f"deliveries in last {args.hours:.0f}h ({total} attempts, {100 * rate:.0f}% failed)"
    ]
    for caller, f in sorted(fails.items(), key=lambda kv: -kv[1]["n"]):
        suffix = f" — {f['last_err']}" if f["last_err"] else ""
        lines.append(f"  {caller}: {f['n']} failed{suffix}")
    text = "\n".join(lines)

    print(text)
    if not args.dry:
        from scripts.openclaw_alerts import alert_openclaw

        alert_openclaw(text, parse_mode=None)


if __name__ == "__main__":
    main()

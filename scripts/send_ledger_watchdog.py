#!/usr/bin/env python3
"""Daily failure watchdog over the fleet send ledger (logs/telegram_sent.jsonl).

Every delivery attempt through alert_openclaw()/send_telegram() appends one
JSON line: {ts, caller, channel, ok, parse_mode, len[, err]}. This watchdog
scans the last N hours and alerts (plain text — never Markdown, per the
kalshi_fade 400 incident) ONLY when failures exist. Silent on clean days.

Born from the 2026-07 audits: kalshi_fade dropped 24 consecutive daily reports
and the whale drain delivered into a dead consumer for ~3 weeks — both
invisible because nothing watched the failure channel.

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
import sys
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

#!/usr/bin/env python3
"""shadow_compare_report.py — 48h shadow-vs-direct comparison for dispatch rollout.

Alert-System-Overhaul plan, Task 5.3 Step 1→2 gate: the four low-risk pipelines
(whale_resolutions, rising_wallets, leaderboard_wallets, graduation) currently
send DIRECT and also enqueue shadow rows. Before flipping them to enforce
(dispatch-only), compare what the batcher WOULD have sent against what the
direct path actually sent. LLM-free; delivers via the hardened send path.

Usage (VPS):
    venv/bin/python3 scripts/shadow_compare_report.py [--hours 48] [--dry]
Scheduled one-shot: systemd-run --on-calendar="2026-07-18 13:03" (see plan).
"""

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "storage" / "shadow_trades.db"
LEDGER = BASE / "logs" / "telegram_sent.jsonl"
# pipeline -> owning caller script (ledger `caller` field)
PIPELINES = {
    "whale_resolutions": "whale_resolution_tracker.py",
    "rising_wallets": "pm_leaderboard_scraper.py",
    "leaderboard_wallets": "pm_leaderboard_scraper.py",
    "graduation": "pm_leaderboard_scraper.py",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=48.0)
    ap.add_argument("--dry", action="store_true", help="print, don't send")
    args = ap.parse_args()
    cutoff = time.time() - args.hours * 3600

    con = sqlite3.connect(str(DB))
    shadow = defaultdict(lambda: [0, 0])  # pipeline -> [batches, events]
    for pipe, n in con.execute(
        # alert_queue rows are single events (no n_events col); shadow_log rows
        # are drained batches carrying their event count.
        "SELECT pipeline, 1 FROM alert_queue WHERE shadow=1 AND ts>=?"
        " UNION ALL SELECT pipeline, n_events FROM alert_shadow_log WHERE ts>=?",
        (cutoff, cutoff),
    ):
        shadow[pipe][0] += 1
        shadow[pipe][1] += int(n or 1)

    direct = defaultdict(lambda: [0, 0])  # caller -> [ok, fail]
    watched = set(PIPELINES.values())
    try:
        for line in open(LEDGER):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("caller") in watched:
                from datetime import datetime

                try:
                    ts = datetime.fromisoformat(r["ts"]).timestamp()
                except (KeyError, ValueError):
                    continue
                if ts >= cutoff:
                    direct[r["caller"]][0 if r.get("ok") else 1] += 1
    except FileNotFoundError:
        pass

    out = [f"📊 SHADOW DISPATCH COMPARE — last {args.hours:.0f}h (Task 5.3 gate)"]
    for pipe, caller in PIPELINES.items():
        b, e = shadow.get(pipe, (0, 0))
        out.append(f"• {pipe}: shadow {b} batch(es)/{e} event(s) | direct via {caller}")
    for caller, (ok, fail) in sorted(direct.items()):
        out.append(f"• direct sends {caller}: {ok} ok, {fail} failed")
    if not any(v[0] for v in shadow.values()):
        out.append("⚠️ No shadow batches captured — do NOT flip to enforce; check wiring first.")
    else:
        out.append(
            "Next: if batch counts look sane and nothing critical was delayed, "
            "flip these 4 pipelines to enforce (dispatch-only) per plan 5.3 Step 2."
        )
    text = "\n".join(out)
    print(text)
    if not args.dry:
        sys.path.insert(0, str(BASE))
        from scripts.openclaw_alerts import alert_openclaw

        print(f"[send] ok={alert_openclaw(text, parse_mode=None)}")


if __name__ == "__main__":
    main()

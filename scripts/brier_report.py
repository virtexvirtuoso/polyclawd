#!/usr/bin/env python3
"""Daily shadow-trade Brier report — LLM-free replacement for the OpenClaw
'shadow-trade-daily-resolve' agent cron. The resolution itself is already done
continuously by the VPS scheduler (task_shadow_resolution); this only computes
the Brier score off the LIVE VPS DB and pushes a summary to Telegram when the
resolved count has grown since the last report.

Run from VPS cron:
    set -a && . ~/.config/polyclawd/alerts.env && set +a
    venv/bin/python3 scripts/brier_report.py --send
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "storage" / "shadow_trades.db"
STATE = Path(os.path.expanduser("~/.config/polyclawd/brier_state.json"))

Q = """
SELECT COUNT(*) n,
       ROUND(AVG(POWER((confidence/100.0) - CASE WHEN side=outcome THEN 1.0 ELSE 0.0 END,2)),4) brier,
       SUM(CASE WHEN side=outcome THEN 1 ELSE 0 END) wins,
       SUM(CASE WHEN side!=outcome THEN 1 ELSE 0 END) losses
FROM shadow_trades
WHERE resolved=1 AND outcome IN ('YES','NO') AND side IN ('YES','NO')
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="push to Telegram (Bot API, no LLM)")
    ap.add_argument("--force", action="store_true", help="send even if no new resolutions")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB), timeout=10)
    n, brier, wins, losses = conn.execute(Q).fetchone()
    conn.close()
    n = n or 0
    brier = brier if brier is not None else 0.0
    wins, losses = wins or 0, losses or 0
    wr = (wins / (wins + losses) * 100) if (wins + losses) else 0.0

    try:
        last = json.loads(STATE.read_text()).get("n", 0)
    except Exception:
        last = 0
    new = n - last

    # Only speak when something resolved (matches the original "only if new").
    if new <= 0 and not args.force:
        print(f"no new resolutions (n={n}); silent")
        return

    flag = ""
    if brier > 0.25:
        flag = " ⚠️ degrading (>0.25)"
    elif brier < 0.15:
        flag = " ✅ improving (<0.15)"

    text = (
        f"📐 Shadow Brier — daily\n"
        f"Resolved: {n} (+{new} today)\n"
        f"Brier: {brier:.4f}{flag} (target <0.20)\n"
        f"Record: {wins}W-{losses}L | WR {wr:.0f}%"
    )
    print(text)

    if args.send:
        try:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps({"n": n}))
        except Exception as e:
            print(f"[state] write failed: {e}")
        try:
            sys.path.insert(0, str(BASE))
            from scripts.openclaw_alerts import alert_openclaw
            print(f"[send] telegram ok={alert_openclaw(text)}")
        except Exception as e:
            print(f"[send] failed: {e}")


if __name__ == "__main__":
    main()

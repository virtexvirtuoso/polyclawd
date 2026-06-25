#!/usr/bin/env python3
"""Weekly MLB prop-edge Gate-2 calibration report — LLM-free replacement for the
OpenClaw agent cron. Fetches the props/alerts API and pushes a compact plain-text
summary to Telegram. Below 100 resolved shadows: one accumulating line.

Run from VPS cron:
    set -a && . ~/.config/polyclawd/alerts.env && set +a
    venv/bin/python3 scripts/mlb_prop_gate2_report.py --send
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
API = "https://virtuosocrypto.com/polyclawd/api/baseball/props/alerts?limit=500"
TARGET = 100


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="push to Telegram (Bot API, no LLM)")
    args = ap.parse_args()

    try:
        req = urllib.request.Request(API, headers={"User-Agent": "polyclawd-cron/1.0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read().decode())
    except Exception as e:
        text = f"⚾ MLB prop Gate-2: report fetch failed — {e}"
        print(text)
        _maybe_send(args.send, text)
        return

    s = d.get("summary", {})
    resolved = s.get("resolved", 0)
    scans = d.get("scan_count_24h", 0)

    if resolved < TARGET:
        text = (f"⚾ MLB prop Gate-2: {resolved}/{TARGET} resolved "
                f"({s.get('won',0)}W-{s.get('lost',0)}L, open {s.get('open',0)}, "
                f"scans/24h {scans}) — accumulating, no action.")
        print(text)
        _maybe_send(args.send, text)
        return

    # Gate-2 reached: full calibration read.
    L = [
        f"⚾ MLB prop Gate-2 — {resolved}/{TARGET} RESOLVED ✅",
        f"Record: {s.get('won',0)}W-{s.get('lost',0)}L | hit {s.get('hit_rate_pct',0)}%",
        f"CLV: {s.get('avg_clv_pp',0):+.1f}pp avg | {s.get('clv_positive_pct',0):.0f}% positive (n={s.get('clv_n',0)})",
        f"Scans/24h: {scans} | open {s.get('open',0)}, void {s.get('void',0)}",
        "Calibration (edge bucket → realized hit %):",
    ]
    holds = True
    for c in sorted(d.get("calibration", []), key=lambda x: x.get("edge_bucket", 0)):
        eb, n, hit = c.get("edge_bucket", 0), c.get("n", 0), c.get("realized_hit_pct", 0)
        L.append(f"  +{eb}pp: {hit:.0f}% (n={n})")
        # Monotonicity sanity: higher edge bucket should not realize a lower hit rate.
        if eb >= 20 and hit < 50:
            holds = False
    L.append("Calibration: monotone/holds ✅" if holds else "⚠️ Calibration NOT monotone — review")
    text = "\n".join(L)
    print(text)
    _maybe_send(args.send, text)


def _maybe_send(send, text):
    if not send:
        return
    try:
        sys.path.insert(0, str(BASE))
        from scripts.openclaw_alerts import alert_openclaw
        print(f"[send] telegram ok={alert_openclaw(text)}")
    except Exception as e:
        print(f"[send] failed: {e}")


if __name__ == "__main__":
    main()

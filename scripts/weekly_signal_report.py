#!/usr/bin/env python3
"""Weekly Signal Calibration + IC + CLV + Guard report — LLM-free replacement
for the OpenClaw agent cron. Aggregates shadow-trade stats, paper portfolio,
CLV/meta-model APIs, IC + calibration modules, guard-block counts, and the
Kalshi-fade tiers, then pushes a compact plain-text summary to Telegram.

Run from VPS cron:
    set -a && . ~/.config/polyclawd/alerts.env && set +a
    venv/bin/python3 scripts/weekly_signal_report.py --send
"""

import argparse
import json
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "storage" / "shadow_trades.db"
LOG = BASE / "logs" / "polyclawd.log"
CLV = "http://localhost:8420/api/clv"
META = "http://localhost:8420/api/meta-model"
FADE = "https://virtuosocrypto.com/polyclawd/api/weather/kalshi-fade/dashboard"


def _get(url, timeout=20):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polyclawd-cron/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--send", action="store_true", help="push to Telegram (Bot API, no LLM)"
    )
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB), timeout=10)
    conn.row_factory = sqlite3.Row

    total, resolved, wins = conn.execute(
        "SELECT COUNT(*), SUM(resolved), "
        "SUM(CASE WHEN resolved=1 AND outcome=side THEN 1 ELSE 0 END) FROM shadow_trades"
    ).fetchone()

    arch = conn.execute(
        "SELECT archetype, COUNT(*) c, SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END) r, "
        "SUM(CASE WHEN resolved=1 AND outcome=side THEN 1 ELSE 0 END) w "
        "FROM shadow_trades GROUP BY archetype ORDER BY c DESC LIMIT 6"
    ).fetchall()

    pstatus = {
        r["status"]: r["c"]
        for r in conn.execute(
            "SELECT status, COUNT(*) c FROM paper_positions GROUP BY status"
        )
    }
    bankroll_row = conn.execute(
        "SELECT bankroll FROM paper_portfolio_state ORDER BY id DESC LIMIT 1"
    ).fetchone()
    bankroll = bankroll_row["bankroll"] if bankroll_row else 0.0

    fade = conn.execute(
        "SELECT strategy, COUNT(*) n, SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) w, "
        "SUM(CASE WHEN status IN ('won','lost') THEN 1 ELSE 0 END) r, "
        "printf('%.2f', SUM(COALESCE(pnl,0))) p "
        "FROM paper_positions WHERE archetype='kalshi_weather_fade' GROUP BY strategy"
    ).fetchall()

    legacy = conn.execute(
        "SELECT COUNT(*) n, SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) w, "
        "printf('%.2f', SUM(COALESCE(pnl,0))) p FROM paper_positions "
        "WHERE archetype='weather' AND status IN ('won','lost','stopped')"
    ).fetchone()
    conn.close()

    clv = _get(CLV)
    meta = (_get(META) or {}).get("stats", {})

    # Guard blocks (grep the log; cheap).
    try:
        guards = (
            subprocess.run(
                [
                    "grep",
                    "-c",
                    "-E",
                    "GUARD 2c|GUARD 2d|Meta gate|agreement gate",
                    str(LOG),
                ],
                capture_output=True,
                text=True,
            ).stdout.strip()
            or "0"
        )
    except Exception:
        guards = "?"

    # IC + calibration modules.
    try:
        sys.path.insert(0, str(BASE))
        from signals.ic_tracker import ic_report
        from signals.calibrator import full_calibration_report

        ic = ic_report(90)
        cal = full_calibration_report()
    except Exception as e:
        ic, cal = {"_err": str(e)}, {}

    wr = (wins / resolved * 100) if resolved else 0.0

    L = ["📈 Weekly Signal + IC Calibration"]
    L.append(f"Shadows: {total} total, {resolved} resolved, WR {wr:.0f}% ({wins}W)")
    L.append(
        f"Paper: ${bankroll:,.0f} bankroll | open {pstatus.get('open', 0)}, won {pstatus.get('won', 0)}, stopped {pstatus.get('stopped', 0)}"
    )
    if clv:
        L.append(
            f"CLV: {clv.get('total_trades', 0)} trades, avg {clv.get('avg_clv', 0)}%, +rate {clv.get('positive_clv_rate', 0)}%"
        )
    if meta:
        L.append(
            f"Meta-model: acc {meta.get('accuracy', 0):.1f}%, n={meta.get('n_trades', 0)}, thr {meta.get('threshold', 0)}"
        )
    else:
        L.append("Meta-model: n/a")

    # Kalshi fade (separate from legacy weather, never blended).
    for r in fade:
        L.append(f"Fade [{r['strategy']}]: {r['w']}/{r['r']} W ${r['p']} (n={r['n']})")
    if legacy and legacy["n"]:
        L.append(
            f"Legacy weather (RETIRED): {legacy['w']}W/{legacy['n']} ${legacy['p']} — winding down"
        )

    L.append(f"Guard blocks (log): {guards}")

    if "_err" not in ic:
        kill = ", ".join(ic.get("kill_list", [])) or "none"
        warn = ", ".join(ic.get("warn_list", [])) or "none"
        L.append(
            f"IC(90d) agg {ic.get('aggregate_ic', 0):+.3f} | KILL: {kill} | WARN: {warn}"
        )
    per = (cal or {}).get("per_source", {})
    if per:
        calib = sum(1 for v in per.values() if v.get("status") == "calibrated")
        L.append(f"Calibration: {calib}/{len(per)} sources calibrated")

    L.append("Archetype drift (resolved WR):")
    for a in arch:
        nm = a["archetype"] or "(none)"
        awr = (a["w"] / a["r"] * 100) if a["r"] else 0.0
        L.append(f"  {nm}: {awr:.0f}% ({a['w']}/{a['r']} of {a['c']})")

    text = "\n".join(L)
    print(text)
    print(f"\n[len={len(text)} chars]")

    if args.send:
        try:
            sys.path.insert(0, str(BASE))
            from scripts.openclaw_alerts import alert_openclaw

            # plain text — archetype/strategy names contain underscores that
            # break Telegram's Markdown parser (HTTP 400).
            print(f"[send] telegram ok={alert_openclaw(text, parse_mode=None)}")
        except Exception as e:
            print(f"[send] failed: {e}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""alert_acceptance_check.py — 24h post-deploy acceptance scorecard (§6 gate).

Alert-System-Overhaul plan Phase 6: after 24h live, score the success criteria
and push a PASS/FAIL verdict to Telegram. LLM-free, delivers via the hardened
send path.

Criteria scored:
  1. Effective delivery >= 99%  (a FAIL row immediately followed <90s by an ok
     row from the same caller counts as delivered-via-fallback)
  2. Delivered alert volume <= 20 messages / 24h
  3. Zero hex/empty market titles in live_positions
  4. Stop heartbeat fresh (< 30 min)
  5. Dispatch queue not stuck (no non-shadow row older than 30 min)

Usage: venv/bin/python3 scripts/alert_acceptance_check.py [--hours 24] [--dry]
"""
import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "storage" / "shadow_trades.db"
LEDGER = BASE / "logs" / "telegram_sent.jsonl"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    cutoff = time.time() - args.hours * 3600

    rows = []
    for line in open(LEDGER):
        try:
            r = json.loads(line)
            r["_t"] = datetime.fromisoformat(r["ts"]).timestamp()
        except (ValueError, KeyError):
            continue
        if r["_t"] >= cutoff:
            rows.append(r)

    fails = 0
    delivered = 0
    for i, r in enumerate(rows):
        if r.get("ok"):
            delivered += 1
            continue
        rescued = any(
            n.get("ok") and n.get("caller") == r.get("caller") and 0 <= n["_t"] - r["_t"] < 90
            for n in rows[i + 1 : i + 4]
        )
        if not rescued:
            fails += 1
    attempts_eff = delivered + fails
    rate = 100.0 * delivered / attempts_eff if attempts_eff else 100.0

    con = sqlite3.connect(str(DB))
    hexes = con.execute(
        "SELECT COUNT(*) FROM live_positions WHERE market_title='' OR market_title LIKE '0x%'"
    ).fetchone()[0]
    hb = con.execute("SELECT ts FROM stop_heartbeat WHERE id=1").fetchone()
    hb_age = int(time.time() - hb[0]) if hb else -1
    stuck = con.execute(
        "SELECT COUNT(*) FROM alert_queue WHERE shadow=0 AND ts < ?", (time.time() - 1800,)
    ).fetchone()[0]

    checks = [
        (rate >= 99.0, f"effective delivery {rate:.1f}% ({delivered}/{attempts_eff}, {fails} unrescued fails) — target >=99%"),
        (delivered <= 20, f"delivered volume {delivered}/24h — target <=20"),
        (hexes == 0, f"hex/empty titles in live_positions: {hexes} — target 0"),
        (0 <= hb_age < 1800, f"stop heartbeat age: {hb_age}s — target <30min"),
        (stuck == 0, f"stuck queue rows (>30min, non-shadow): {stuck} — target 0"),
    ]
    ok_all = all(c[0] for c in checks)
    out = [f"{'✅ PASS' if ok_all else '❌ FAIL'} — ALERT OVERHAUL 24h ACCEPTANCE (§6)"]
    out += [("✓ " if ok else "✗ ") + txt for ok, txt in checks]
    if not ok_all:
        out.append("Action: investigate failed criteria before enforce-mode flip (plan Phase 6 / vault doc §6).")
    text = "\n".join(out)
    print(text)
    if not args.dry:
        sys.path.insert(0, str(BASE))
        from scripts.openclaw_alerts import alert_openclaw
        print(f"[send] ok={alert_openclaw(text, parse_mode=None)}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Kalshi Weather Fade — daily paper-shadow report. LLM-free replacement for the
OpenClaw agent cron. Aggregates 5 VPS-side sources, applies the review-trigger
flags, and pushes a compact plain-text report to Telegram via the Bot API.

(The original agent prompt also read a LOCAL Mac decision-calendar todo.md as a
6th source; that file is not reachable from the VPS, so it is omitted here.)

Run from VPS cron (modules are slow — allow a few minutes):
    set -a && . ~/.config/polyclawd/alerts.env && set +a
    venv/bin/python3 scripts/kalshi_fade_report.py --send
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DASH = "https://virtuosocrypto.com/polyclawd/api/weather/kalshi-fade/dashboard"
SHEET = "https://virtuosocrypto.com/polyclawd/kalshi_fade_sheet.json"
ENSEMBLE = BASE / "data" / "ensemble_snapshots.jsonl"
PM_QUOTES = BASE / "data" / "pm_maker_shadow_quotes.jsonl"
STATE = Path(os.path.expanduser("~/.config/polyclawd/kalshi_fade_state.json"))
DATE_CAP = 400.0  # $ exposure cap per event date


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "polyclawd-cron/1.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def _maker(module_args):
    """Run a maker-shadow module and parse its trailing JSON object. Returns {}
    on any failure (slow/err) — the report degrades gracefully."""
    try:
        out = subprocess.run(
            [sys.executable, "-m", *module_args],
            cwd=str(BASE),
            capture_output=True,
            text=True,
            timeout=300,
        ).stdout
        i, j = out.find("{"), out.rfind("}")
        return json.loads(out[i : j + 1]) if i >= 0 and j > i else {}
    except Exception:
        return {}


def _lines(p):
    try:
        with open(p, "rb") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="push to Telegram (Bot API, no LLM)")
    args = ap.parse_args()

    try:
        dash = _get(DASH)
    except Exception as e:
        dash = {"_err": str(e)}
    try:
        sheet = _get(SHEET)
    except Exception:
        sheet = {}

    totals = dash.get("totals", {})
    tiers = {t["tier"]: t for t in dash.get("tier_breakdown", [])}
    open_pos = dash.get("open_positions", [])

    # Exposure by event date (open positions) vs cap.
    exp_by_date = defaultdict(float)
    for p in open_pos:
        exp_by_date[p.get("event_date", "?")] += p.get("bet_size", 0) or 0
    over_cap = {d: v for d, v in exp_by_date.items() if v > DATE_CAP}

    # Skip census from the sheet.
    cands = sheet.get("candidates", []) or []
    census = Counter(c.get("skip_reason") or "n/a" for c in cands)
    entered = sheet.get("entered", 0)

    # Maker shadows.
    ks = _maker(["signals.kalshi_maker_shadow", "--hours", "26"])
    pm = _maker(["signals.pm_maker_shadow", "evaluate", "--hours", "26"])

    def maker_line(label, m):
        j, im = m.get("join", {}), m.get("improve", {})
        if not j and not im:
            return f"{label}: n/a"
        jr = (j.get("fill_rate") or 0) * 100
        ir = (im.get("fill_rate") or 0) * 100
        ev = j.get("ev_per_dollar")
        evs = f"{ev:+.3f}" if ev is not None else "n/a"
        return f"{label}: join {jr:.0f}% / improve {ir:.0f}% fill | ev/$ {evs}"

    def adverse(m):
        j = m.get("join", {})
        return (j.get("pnl") or 0) != 0 and (j.get("ev_per_dollar") or 0) < 0

    # Recorder liveness vs stored state.
    ens_n, pmq_n = _lines(ENSEMBLE), _lines(PM_QUOTES)
    try:
        st = json.loads(STATE.read_text())
    except Exception:
        st = {}
    ens_grew = ens_n > st.get("ensemble", -1)
    pmq_grew = pmq_n > st.get("pm_quotes", -1)
    recorder_dead = (st.get("ensemble") is not None and not ens_grew) or (
        st.get("pm_quotes") is not None and not pmq_grew
    )

    zero_nights = st.get("zero_entry_nights", 0)
    zero_nights = zero_nights + 1 if entered == 0 else 0

    # ── Build report ──────────────────────────────────────────────────────
    L = ["🌡️ Kalshi Weather Fade — paper shadow"]
    ln = tiers.get("kalshi_fade_longshot_no", {})
    fy = tiers.get("kalshi_fade_favorite_yes", {})
    L.append(f"Entries last scan: {entered} (open: longshot {ln.get('open', 0)}, favorite {fy.get('open', 0)})")
    if exp_by_date:
        worst = max(exp_by_date.items(), key=lambda x: x[1])
        L.append(f"Exposure: {len(exp_by_date)} dates, peak ${worst[1]:.0f} ({worst[0]}) vs ${DATE_CAP:.0f} cap")
    L.append(
        f"Resolved: {totals.get('wins', 0)}W-{totals.get('losses', 0)}L | "
        f"net ${totals.get('total_pnl', 0):+.2f} (fees ${totals.get('fees_paid', 0):.2f}) | "
        f"WR {totals.get('win_rate', 0):.0f}%"
    )
    L.append(f"Open: {totals.get('open_positions', 0)} pos, ${totals.get('open_exposure', 0):.0f} exposure")
    if census:
        top = ", ".join(f"{k} {v}" for k, v in census.most_common(4))
        L.append(f"Skips ({sum(census.values())}): {top}")
    L.append(maker_line("KS maker", ks))
    L.append(maker_line("PM maker", pm))
    L.append(
        f"Recorders: ensemble {ens_n}{'↑' if ens_grew else '⚠️FLAT'} | pm_quotes {pmq_n}{'↑' if pmq_grew else '⚠️FLAT'}"
    )

    flags = []
    if recorder_dead:
        flags.append("🛑 RECORDER DEAD (data loss)")
    if adverse(ks):
        flags.append("⚠️ KS ADVERSE SELECTION (ev/$<0 settled)")
    if adverse(pm):
        flags.append("⚠️ PM ADVERSE SELECTION (ev/$<0 settled)")
    if over_cap:
        flags.append("⚠️ over date-cap: " + ", ".join(f"{d} ${v:.0f}" for d, v in over_cap.items()))
    if zero_nights >= 2:
        flags.append(f"⚠️ {zero_nights} nights 0 entries")
    if (ln.get("n", 0) >= 30) and (ln.get("win_rate", 100) < 90):
        flags.append(f"⚠️ longshot WR {ln.get('win_rate'):.0f}% (<90% at n={ln.get('n')})")
    if flags:
        L.append("FLAGS: " + " | ".join(flags))
        L.append("Verdict: NEEDS REVIEW")
    else:
        L.append("Verdict: shadow on track")

    text = "\n".join(L)
    print(text)

    # Persist state (skip if just printing? keep state on real --send runs).
    if args.send:
        try:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(
                json.dumps(
                    {
                        "ensemble": ens_n,
                        "pm_quotes": pmq_n,
                        "zero_entry_nights": zero_nights,
                    }
                )
            )
        except Exception as e:
            print(f"[state] write failed: {e}")
        try:
            sys.path.insert(0, str(BASE))
            from scripts.openclaw_alerts import alert_openclaw

            # parse_mode=None: report body has unbalanced '_' (pm_quotes, ev/$) —
            # Markdown mode 400s and the send drops silently (24 straight days to 2026-07-06)
            print(f"[send] telegram ok={alert_openclaw(text, parse_mode=None)}")
        except Exception as e:
            print(f"[send] failed: {e}")


if __name__ == "__main__":
    main()

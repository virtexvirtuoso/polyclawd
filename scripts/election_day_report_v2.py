#!/usr/bin/env python3
"""Election-day report v2: same data as election_day_report.py, HTML layout.

Summary first, then gaps/movers, then monospace race tables. Reuses the v1
collectors and parsers; never writes the delta-baseline state file.

  venv/bin/python3 scripts/election_day_report_v2.py --force            # print only
  POLYCLAWD_TOPIC=316212 venv/bin/python3 scripts/election_day_report_v2.py --force --send
"""
import argparse
import html
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scripts.election_day_report as e  # noqa: E402

MAX_MSG = 3800


def esc(s):
    return html.escape(str(s), quote=False)


def pre(lines):
    return "<pre>" + esc("\n".join(lines)) + "</pre>"


def dl(cur, key, prev, first, sign):
    out = []
    for base in (prev, first):
        if cur is not None and key in base and abs(cur - base[key]) >= 0.0005:
            out.append(f"{100 * sign * (cur - base[key]):+.1f}")
        else:
            out.append("")
    return out


def render_blocks(data, state, now, errors, missing):
    prev, first = state.get("prev", {}), state.get("first", {})
    snap, flags = {}, []
    blocks = []

    # ---- control / balance of power ----
    ctl = []
    summary_ctl = []
    for label, kt, ps in (("House", "CONTROLH-2026", "which-party-will-win-the-house-in-2026"),
                          ("Senate", "CONTROLS-2026", "which-party-will-win-the-senate-in-2026")):
        kd, pd = e._d_of(e.k_sides(data[("K", kt)])), e._d_of(e.pm_sides(data[("PM", ps)]))
        key, cons = f"control|{label.lower()}", e._mean([kd, pd])
        if cons is not None:
            snap[key] = cons
        d1, d2 = dl(cons, key, prev, first, 1)
        ctl.append(f"{label:<7}{e.fmt(kd):>6}{e.fmt(pd):>6}{d1:>7}{d2:>7}")
        summary_ctl.append(f"{label} Dem {e.fmt(kd)} K | {e.fmt(pd)} PM")
        if kd is not None and pd is not None and abs(kd - pd) >= e.GAP_FLAG:
            flags.append(f"{label} control: Dem {e.fmt(kd)} K vs {e.fmt(pd)} PM ({100 * abs(kd - pd):.1f} pts)")

    bop_k, bop_p = {}, {}
    for m in data[("K", "KXBALANCEPOWERCOMBO-27FEB")]:
        suf = (m.get("ticker") or "").rsplit("-", 1)[-1]
        if suf in e.BOP_LABEL:
            bop_k[suf] = e.k_quote(m)["p"]
    for m in data[("PM", "balance-of-power-2026-midterms")]:
        g = re.search(r"\b([DR]) Senate,\s*([DR]) House", m.get("question") or "")
        if g:
            bop_p[g.group(2) + g.group(1)] = e.pm_quote(m)["p"]
    bop = [f"{label:<18}{e.fmt(bop_k.get(suf)):>6}{e.fmt(bop_p.get(suf)):>6}"
           for suf, label in e.BOP_LABEL.items() if suf in bop_k or suf in bop_p]

    # ---- races ----
    races = {}
    for kind, states in (("senate", e.SENATE), ("governor", e.GOVERNOR),
                         ("house", list(e.HOUSE_EVENTS.values()))):
        for st in states:
            r = e.build_race(kind, st, e.k_sides(data.get(("K", f"{kind}|{st}"), [])),
                             e.pm_sides(data.get(("PM", f"{kind}|{st}"), [])))
            if not r:
                continue
            races[r["key"]] = r
            if r["cons_r"] is not None:
                snap[r["key"]] = r["cons_r"]
            if r["gap"] and r["gap"][0] >= e.GAP_FLAG and not r["called"]:
                g, pt = r["gap"]
                kq, pq = r["k"][pt], r["p"][pt]
                thin = " (thin book)" if "last" in (kq["src"], pq["src"]) else ""
                flags.append(f"{r['st']} {kind}: {pt} {e.fmt(kq['p'])} K vs {e.fmt(pq['p'])} PM "
                             f"({100 * g:.1f} pts, PM vol ${pq['vol'] / 1e3:,.0f}k){thin}")

    moves = sorted(((abs(v - prev[k]), k) for k, v in snap.items() if k in prev), reverse=True)
    moves = [k for d, k in moves if d >= 0.01][:8]
    called = [r for r in races.values() if r["called"]]

    # ---- header + summary ----
    t = now.astimezone(e.ET)
    head = f"<b>🗳 2026 MIDTERMS</b> · {t:%a %b %-d, %-I:%M %p} ET"
    if state.get("prev_ts"):
        head += f"\n<i>run #{state.get('runs', 0) + 1} · Δ vs {e._et_label(state['prev_ts'])} · day vs {e._et_label(state['first_ts'])}</i>"
    else:
        head += "\n<i>first run, no deltas yet</i>"
    top = (f"Biggest move: {esc(e._key_label(moves[0]))} {e.fmt(prev[moves[0]])} → {e.fmt(snap[moves[0]])}"
           if moves else "Biggest move: none ≥1 pt")
    summ = summary_ctl + [f"Cross-venue gaps ≥{100 * e.GAP_FLAG:.0f} pts: {len(flags)}",
                          f"Resolved: {len(called)}", top]
    blocks.append(head + "\n\n<b>Summary</b>\n" + pre(summ)
                  + "\n<i>K = Kalshi · PM = Polymarket · mids of bid/ask · Δ = vs last run · Day = vs first run</i>")

    blocks.append("<b>Control (Dem win %)</b>\n" + pre([f"{'':<7}{'K':>6}{'PM':>6}{'Δ':>7}{'Day':>7}"] + ctl))
    if bop:
        blocks.append("<b>Balance of power</b>\n" + pre([f"{'':<18}{'K':>6}{'PM':>6}"] + bop
                      + ["", "Seats (Kalshi buckets, %)", e.seat_line("Senate", data[("K", "KXDSENATESEATS-27")], e.SENATE_SEAT_THRESHOLDS),
                         e.seat_line("House", data[("K", "KXDHOUSESEATS-27")], e.HOUSE_SEAT_THRESHOLDS)]))

    if flags:
        blocks.append(f"<b>⚠️ Cross-venue gaps ≥{100 * e.GAP_FLAG:.0f} pts</b>\n" + "\n".join(f"• {esc(f)}" for f in flags))
    if called:
        rows = [f"• {esc(r['st'])} {r['kind']}: " + esc(", ".join(f"{pt} on {v}" for pt, v in r["called"]))
                for r in sorted(called, key=lambda r: (r["kind"], r["st"]))]
        blocks.append("<b>✅ Resolved</b>\n" + "\n".join(rows))
    if moves:
        rows = [f"• {esc(e._key_label(k))}  {e.fmt(prev[k])} → {e.fmt(snap[k])} ({100 * (snap[k] - prev[k]):+.1f})" for k in moves]
        blocks.append("<b>📈 Movers since last run</b>\n" + "\n".join(rows))

    for kind, title in (("senate", "Senate"), ("governor", "Governor"), ("house", "House")):
        rs = [r for r in races.values() if r["kind"] == kind]
        comp = sorted((r for r in rs if not r["called"] and r["lead_p"] < e.SAFE),
                      key=lambda r: (e.POLL_CLOSE[r["st"][:2]], r["st"]))
        if kind == "house":
            lead = {pt: sum(r["leader"] == pt for r in rs) for pt in ("D", "R")}
            hdr = f"<b>{title}</b> · {len(rs)} key districts, Kalshi only · leading D {lead['D']} / R {lead['R']}"
            cols = f"{'':<6}{'Close':<7}{'Leader':<16}{'K':>5}{'Δ':>6}{'Day':>6}"
        else:
            hdr = f"<b>{title}</b> · competitive races, leader win %"
            cols = f"{'':<6}{'Close':<7}{'Leader':<16}{'K':>5}{'PM':>6}{'Δ':>6}{'Day':>6}"
        rows = [cols]
        for r in comp:
            ldr = r["leader"]
            sign = 1 if ldr == "R" else -1
            kv = e.fmt(r["k"][ldr]["p"]) if ldr in r["k"] else "—"
            pv = e.fmt(r["p"][ldr]["p"]) if ldr in r["p"] else "—"
            lock = "*" if now >= e._close_dt(r["st"]) else " "
            who = f"{ldr} {r['name']}".strip()[:14]
            d1, d2 = dl(r["cons_r"], r["key"], prev, first, sign)
            mid = f"{kv:>5}" if kind == "house" else f"{kv:>5}{pv:>6}"
            rows.append(f"{r['st']:<6}{e._close_label(r['st']) + lock:<7}{who:<16}{mid}{d1:>6}{d2:>6}")
        if len(rows) == 1:
            rows = ["(none)"]
        tail = []
        for party in ("R", "D"):
            safe = sorted(r["st"] for r in rs if not r["called"] and r["lead_p"] >= e.SAFE and r["leader"] == party)
            if safe:
                tail.append(f"<i>Safe {party} (≥{e.SAFE:.0%}):</i> {esc(' '.join(safe))}")
        blocks.append(hdr + "\n" + pre(rows) + ("\n" + "\n".join(tail) if tail else ""))

    foot = []
    if errors:
        foot.append("Failed: " + esc(", ".join(errors[:12])) + (f" +{len(errors) - 12} more" if len(errors) > 12 else ""))
    if missing:
        foot.append("No market: " + esc(", ".join(missing[:15])) + (f" +{len(missing) - 15} more" if len(missing) > 15 else ""))
    if foot:
        blocks.append("<i>Data issues</i>\n" + "\n".join(foot))
    else:
        blocks.append("<i>* = polls closed · all events fetched</i>")
    return blocks, snap


def pack(blocks, limit=MAX_MSG):
    msgs, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) + 2 > limit:
            msgs.append(cur)
            cur = b
        else:
            cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        msgs.append(cur)
    return msgs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    if not args.force and not e.WINDOW[0] <= now <= e.WINDOW[1]:
        print("[skip] outside the election-day window")
        return
    data, errors, missing = e.collect()
    blocks, _ = render_blocks(data, e._load_state(), now, errors, missing)
    msgs = pack(blocks)
    for m in msgs:
        print(m, "\n-----")
    if args.send:
        from scripts.openclaw_alerts import alert_openclaw
        for m in msgs:
            print(f"[send] telegram ok={alert_openclaw(m, parse_mode='HTML')}")


if __name__ == "__main__":
    main()

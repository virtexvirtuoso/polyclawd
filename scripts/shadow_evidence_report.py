#!/usr/bin/env python3
"""Shadow-week evidence report (plan task 1.12, "Evidence for D9b").

Reads scheduler journal lines (standard input, or the file given as an argument) and prints
the evidence table for the entry-gate shadow (`sw_live RECORD` / `sw_live DRYRUN`) and the stop
shadow (`live_stops ...`). Pure text processing: no project imports, no network, no database.

    journalctl -u polyclawd-scheduler --since "7 days ago" --no-pager | python3 scripts/shadow_evidence_report.py

Exit 0 = EVIDENCE: PASS, 1 = EVIDENCE: FAIL, 2 = usage error or empty input.

Lines are parsed as generic `key=value` maps: unknown fields are ignored, nothing depends on
field position. Timestamps come from the Python log prefix (`2026-11-12 03:30:44,421 [name]`)
when there is one, else from the journald prefix (`Nov 12 03:30:44` or ISO). All times are
read as UTC (the VPS runs in UTC); day boundaries are UTC days. Rows of the evidence table that
need other sources (reconciliation report, saved pre-flight output, stop controls) are not
covered here.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

PRICE_CEILING = 0.60  # the entry gate's price band (smart_wallet_alert.PRICE_GATE_MAX)
SPREAD_ALLOW = 0.10  # (ask - bid) / ask above this is "wide_spread"
SPREAD_ROUNDING = 1e-4  # logged prices carry 4 decimals
REQUIRED_REASONS = ("price_band", "bot_wallet", "clv_muted", "wide_spread", "unpriced_book")
RECORD_REQUIRED = ("verdict", "reason", "price", "is_bot", "clv_muted", "spread_pct")
DRYRUN_REQUIRED = ("records", "would_route", "filtered")
KNOWN_KINDS = {
    "sw_live": {"RECORD", "DRYRUN", "ARMED"},
    "live_stops": {"DRYRUN", "ARMED", "DETAIL", "SKIPPED", "FAILED"},
}
SHOW_MAX = 10

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
MARKER = re.compile(r"\b(sw_live|live_stops) ([A-Za-z_]+)")
KV = re.compile(r"(\w+)=(\S*)")
PY_TS = re.compile(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})[,.](\d{1,6}) *(?:\[|\|)")
ISO_TS = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:[.,]\d+)?([+-]\d{2}:?\d{2}|Z)?")
JD_TS = re.compile(r"^([A-Z][a-z]{2}) +(\d{1,2}) (\d{2}):(\d{2}):(\d{2})")
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


# --------------------------------------------------------------------------- parsing


def _parse_time(text, default_year):
    """UTC datetime from the line prefix, or None."""
    m = PY_TS.search(text)
    if m:
        y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
        return datetime(y, mo, d, h, mi, s)
    m = ISO_TS.match(text)
    if m:
        y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
        t = datetime(y, mo, d, h, mi, s)
        off = m.group(7)
        if off and off != "Z":
            sign = 1 if off[0] == "+" else -1
            digits = off[1:].replace(":", "")
            t -= sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        return t
    m = JD_TS.match(text)
    if m and m.group(1) in MONTHS:
        return datetime(default_year, MONTHS[m.group(1)], int(m.group(2)),
                        int(m.group(3)), int(m.group(4)), int(m.group(5)))
    return None


def _num(v):
    """A float, or None for '-' / missing / not a number."""
    if v is None or v == "-":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _ids(v):
    return [int(x) for x in re.findall(r"\d+", v or "")]


def _filtered_counts(v):
    """{'price_band': 2, ...} from `filtered=a:1,b:2`; '-' is none; None if unreadable."""
    if v is None:
        return None
    if v == "-":
        return {}
    out = {}
    for part in v.split(","):
        name, sep, n = part.rpartition(":")
        if not sep or not n.isdigit() or not name:
            return None
        out[name] = out.get(name, 0) + int(n)
    return out


def parse_lines(lines):
    """Return a dict of parsed material. Lines that are neither sw_live nor live_stops are skipped."""
    cleaned = [ANSI.sub("", ln.rstrip("\n")) for ln in lines]
    default_year = datetime.now(timezone.utc).year
    for ln in cleaned:
        m = PY_TS.search(ln)
        if m:
            default_year = int(m.group(1))
            break

    events, no_time, unrecognised, malformed = [], [], [], []
    total = 0
    for n, ln in enumerate(cleaned, 1):
        if not ln.strip():
            continue
        total += 1
        m = MARKER.search(ln)
        if not m:
            continue
        system, kind = m.group(1), m.group(2)
        ts = _parse_time(ln[:m.start()], default_year)
        if ts is None:
            no_time.append((n, ln))
            continue
        if kind not in KNOWN_KINDS[system]:
            unrecognised.append((n, ln))
            continue
        kv = dict(KV.findall(ln[m.end():]))
        events.append({"n": n, "ts": ts, "system": system, "kind": kind, "kv": kv, "raw": ln})

    records, dryruns, armed = [], [], []
    stop_dry, stop_detail, stop_skipped, stop_failed = [], [], [], []
    for e in events:
        key = (e["system"], e["kind"])
        kv = e["kv"]
        if key == ("sw_live", "RECORD"):
            if any(k not in kv for k in RECORD_REQUIRED):
                malformed.append((e["n"], e["raw"]))
            else:
                records.append(e)
        elif key == ("sw_live", "DRYRUN"):
            if any(k not in kv for k in DRYRUN_REQUIRED) or _filtered_counts(kv["filtered"]) is None \
                    or not kv["records"].isdigit() or not kv["would_route"].isdigit():
                malformed.append((e["n"], e["raw"]))
            else:
                dryruns.append(e)
        elif e["kind"] == "ARMED":
            armed.append(e)
        elif key == ("live_stops", "DRYRUN"):
            if "would_exit" not in kv or "spread_held" not in kv:
                malformed.append((e["n"], e["raw"]))
            else:
                stop_dry.append(e)
        elif key == ("live_stops", "DETAIL"):
            stop_detail.append(e)
        elif key == ("live_stops", "SKIPPED"):
            stop_skipped.append(e)
        elif key == ("live_stops", "FAILED"):
            stop_failed.append(e)
    return dict(total=total, events=events, no_time=no_time, unrecognised=unrecognised,
                malformed=malformed, records=records, dryruns=dryruns, armed=armed,
                stop_dry=stop_dry, stop_detail=stop_detail, stop_skipped=stop_skipped,
                stop_failed=stop_failed)


# --------------------------------------------------------------------------- report


class Report:
    def __init__(self):
        self.out = []
        self.failing = []

    def text(self, s=""):
        self.out.append(s)

    def head(self, s):
        self.out.append("")
        self.out.append(f"== {s}")

    def check(self, ok, name, detail, warn=False):
        status = "PASS" if ok else ("WARN" if warn else "FAIL")
        if status == "FAIL":
            self.failing.append(name)
        self.out.append(f"[{status}] {name}: {detail}")


def _day(ts):
    return ts.strftime("%Y-%m-%d")


def _show(rep, items, label):
    for n, ln in items[:SHOW_MAX]:
        rep.text(f"    {label} line {n}: {ln[:300]}")
    if len(items) > SHOW_MAX:
        rep.text(f"    ... and {len(items) - SHOW_MAX} more")


def build_report(parsed, min_days=7.0, max_gap_minutes=10.0):
    rep = Report()
    P = parsed
    events = P["events"]

    rep.text("SHADOW EVIDENCE REPORT")
    rep.text(f"input: {P['total']} lines, {len(events)} sw_live/live_stops lines with a parseable time")
    rep.text(f"lines without a parseable time: {len(P['no_time'])}")
    _show(rep, P["no_time"], "no-time")
    rep.text(f"unrecognised sw_live/live_stops line kinds: {len(P['unrecognised'])}")
    _show(rep, P["unrecognised"], "unrecognised")
    rep.text(f"malformed lines (a field a check needs is missing): {len(P['malformed'])}")
    _show(rep, P["malformed"], "malformed")

    # ---- window
    rep.head("WINDOW")
    if events:
        first = min(e["ts"] for e in events)
        last = max(e["ts"] for e in events)
        span = last - first
        days = span.total_seconds() / 86400.0
        rep.text(f"first: {first:%Y-%m-%d %H:%M:%S} UTC")
        rep.text(f"last:  {last:%Y-%m-%d %H:%M:%S} UTC")
        hours = int(span.total_seconds() // 3600)
        rep.check(days >= min_days, "window",
                  f"span {days:.2f} days ({hours} h); at least {min_days:g} days required")
    else:
        rep.check(False, "window", "no sw_live or live_stops line with a parseable time")

    # ---- entry-gate shadow
    rep.head("ENTRY-GATE SHADOW (sw_live RECORD, per UTC day)")
    records = P["records"]
    per_day = defaultdict(lambda: {"records": 0, "would_route": 0, "filtered": Counter()})
    reason_totals = Counter()
    for e in records:
        d = per_day[_day(e["ts"])]
        d["records"] += 1
        if e["kv"]["verdict"] == "would_route":
            d["would_route"] += 1
        else:
            reason = e["kv"]["reason"] if e["kv"]["reason"] != "-" else "(none)"
            d["filtered"][reason] += 1
            reason_totals[reason] += 1
    reasons = sorted(reason_totals)
    if per_day:
        rep.text("day         records  would_route  filtered  " + "  ".join(reasons))
        for day in sorted(per_day):
            d = per_day[day]
            rep.text(f"{day}  {d['records']:7d}  {d['would_route']:11d}  {sum(d['filtered'].values()):8d}  "
                     + "  ".join(f"{d['filtered'][r]:>{len(r)}d}" for r in reasons))
        tot_rec = sum(d["records"] for d in per_day.values())
        tot_wr = sum(d["would_route"] for d in per_day.values())
        rep.text(f"total       {tot_rec:7d}  {tot_wr:11d}  {sum(reason_totals.values()):8d}  "
                 + "  ".join(f"{reason_totals[r]:>{len(r)}d}" for r in reasons))
    else:
        rep.text("no sw_live RECORD lines")

    # ---- each gate seen
    rep.head("EACH GATE WAS SEEN TO WORK (filtered RECORD lines per reason)")
    for reason in REQUIRED_REASONS:
        n = reason_totals.get(reason, 0)
        rep.check(n > 0, f"seen:{reason}", f"seen {n} times" if n else
                  "NOT SEEN (inject one synthetic record through the hook, as the plan says)")

    # ---- never routes a filtered record
    rep.head("SHADOW NEVER ROUTES A FILTERED RECORD (would_route RECORD lines, from the logged fields)")
    wr = [e for e in records if e["kv"]["verdict"] == "would_route"]
    rep.text(f"would_route records checked: {len(wr)}")
    probes = [
        ("price_at_or_over_ceiling", lambda kv: (_num(kv["price"]) is not None and _num(kv["price"]) >= PRICE_CEILING)),
        ("is_bot", lambda kv: kv["is_bot"] == "true"),
        ("clv_muted", lambda kv: kv["clv_muted"] == "true"),
        ("spread_pct_over_allowance", lambda kv: (_num(kv["spread_pct"]) is not None and _num(kv["spread_pct"]) > SPREAD_ALLOW)),
        ("derived_spread_over_allowance", lambda kv: _derived_spread_over(kv)),
        ("unpriced_field", lambda kv: any(_num(kv.get(k)) is None for k in ("price", "bid", "ask", "spread_pct"))),
        ("flag_not_logged", lambda kv: kv["is_bot"] not in ("true", "false") or kv["clv_muted"] not in ("true", "false")),
    ]
    for name, fn in probes:
        bad = [(e["n"], e["raw"]) for e in wr if fn(e["kv"])]
        rep.check(not bad, f"never_routes:{name}", f"{len(bad)} of {len(wr)} would_route records")
        _show(rep, bad, "offending")
    rep.text("(this row counts only together with the 'each gate was seen' rows above)")

    # ---- consistency
    rep.head("CONSISTENCY (sw_live DRYRUN summaries)")
    bad_sum = []
    sum_by_day = Counter()
    for e in P["dryruns"]:
        kv = e["kv"]
        recs, w = int(kv["records"]), int(kv["would_route"])
        fil = sum(_filtered_counts(kv["filtered"]).values())
        sum_by_day[_day(e["ts"])] += recs
        if recs != w + fil:
            bad_sum.append((e["n"], e["raw"]))
    rep.check(not bad_sum, "summary_adds_up",
              f"{len(P['dryruns'])} summaries; {len(bad_sum)} where records != would_route + filtered")
    _show(rep, bad_sum, "bad summary")
    rec_by_day = Counter({day: d["records"] for day, d in per_day.items()})
    for day in sorted(set(rec_by_day) | set(sum_by_day)):
        a, b = rec_by_day.get(day, 0), sum_by_day.get(day, 0)
        rep.check(a == b, f"record_lines_vs_summaries:{day}",
                  f"{a} RECORD lines, summaries report {b}" + ("" if a == b else
                  " (can straddle midnight or a restart)"), warn=True)

    # ---- diagnostics from the extra fields
    rep.head("DIAGNOSTICS (gamma and order-book fields)")
    with_gamma = [e for e in records if "gamma" in e["kv"]]
    if with_gamma:
        by_day = defaultdict(lambda: [0, 0])
        for e in with_gamma:
            by_day[_day(e["ts"])][0] += 1
            if e["kv"]["gamma"] == "missing":
                by_day[_day(e["ts"])][1] += 1
        for day in sorted(by_day):
            rep.text(f"{day}  records with gamma logged {by_day[day][0]}, gamma=missing {by_day[day][1]}")
    else:
        rep.text("gamma=missing per day: not logged")
    unpriced = [e for e in records if e["kv"]["reason"] == "unpriced_book"]
    if any("book" in e["kv"] for e in unpriced) or (not unpriced and any("book" in e["kv"] for e in records)):
        c = Counter(e["kv"].get("book", "not logged") for e in unpriced)
        rep.text("unpriced_book records by book=: " + (", ".join(f"{k}:{v}" for k, v in sorted(c.items())) or "none"))
    else:
        rep.text("unpriced_book records by book=: not logged")

    # ---- stop shadow
    rep.head("STOP SHADOW (live_stops)")
    dry = sorted(P["stop_dry"], key=lambda e: (e["ts"], e["n"]))
    rep.text(f"live_stops DRYRUN lines: {len(dry)}")
    if len(dry) == 0:
        rep.check(False, "stop_gap", "no live_stops DRYRUN line at all")
    else:
        longest, a, b = timedelta(0), dry[0]["ts"], dry[0]["ts"]
        for x, y in zip(dry, dry[1:]):
            g = y["ts"] - x["ts"]
            if g > longest:
                longest, a, b = g, x["ts"], y["ts"]
        mins = longest.total_seconds() / 60.0
        rep.check(mins < max_gap_minutes, "stop_gap",
                  f"longest gap between DRYRUN lines {mins:.1f} min, from {a:%Y-%m-%d %H:%M:%S} "
                  f"to {b:%Y-%m-%d %H:%M:%S}; under {max_gap_minutes:g} min required")
    rep.text(f"live_stops SKIPPED lines: {len(P['stop_skipped'])} (reported, do not fail)")
    rep.text(f"live_stops FAILED lines: {len(P['stop_failed'])}")
    ticks = sorted([e for e in events if e["system"] == "live_stops" and e["kind"] in ("DRYRUN", "ARMED", "FAILED")],
                   key=lambda e: (e["ts"], e["n"]))
    run = longest_run = 0
    for e in ticks:
        run = run + 1 if e["kind"] == "FAILED" else 0
        longest_run = max(longest_run, run)
    top = max((int(e["kv"]["consecutive"]) for e in P["stop_failed"]
               if e["kv"].get("consecutive", "").isdigit()), default=0)
    rep.text(f"longest run of consecutive FAILED ticks: {longest_run} (largest consecutive= logged: {top})")

    ids_day = defaultdict(lambda: {"ticks": 0, "would_exit": Counter(), "spread_held": Counter()})
    for e in dry:
        d = ids_day[_day(e["ts"])]
        d["ticks"] += 1
        for i in _ids(e["kv"]["would_exit"]):
            d["would_exit"][i] += 1
        for i in _ids(e["kv"]["spread_held"]):
            d["spread_held"][i] += 1
    for day in sorted(ids_day):
        d = ids_day[day]
        ids = sorted(set(d["would_exit"]) | set(d["spread_held"]))
        rep.text(f"{day}  ticks {d['ticks']}, would_exit position-ticks {sum(d['would_exit'].values())}, "
                 f"spread_held position-ticks {sum(d['spread_held'].values())}, distinct ids {ids}")
        for i in ids:
            rep.text(f"    id={i}: would_exit on {d['would_exit'][i]} ticks, spread_held on {d['spread_held'][i]} ticks")

    rep.check(not P["armed"], "no_armed_lines",
              f"{len(P['armed'])} ARMED lines (the shadow week is supposed to be all dry run)")
    if P["armed"]:
        rep.text("!!! ARMED LINES IN THE WINDOW !!!")
        _show(rep, [(e["n"], e["raw"]) for e in P["armed"]], "ARMED")

    rep.head("NOT COVERED BY THIS REPORT")
    rep.text("not covered by this report: the pre-flight output row, the reconciliation row, the "
             "production controls for the stop (1.2 control output), the root-cause write-up")

    rep.text("")
    if rep.failing:
        rep.text(f"EVIDENCE: FAIL ({len(rep.failing)} failing checks: {', '.join(rep.failing)})")
    else:
        rep.text("EVIDENCE: PASS")
    return rep


def _derived_spread_over(kv):
    bid, ask = _num(kv.get("bid")), _num(kv.get("ask"))
    if bid is None or ask is None or ask <= 0:
        return False
    return (ask - bid) / ask > SPREAD_ALLOW + SPREAD_ROUNDING


# --------------------------------------------------------------------------- main


def main(argv=None, stdin=None, out=None):
    ap = argparse.ArgumentParser(description="Evidence table for the shadow week (plan task 1.12).")
    ap.add_argument("file", nargs="?", help="journal lines file (default: standard input)")
    ap.add_argument("--min-days", type=float, default=7.0)
    ap.add_argument("--max-gap-minutes", type=float, default=10.0)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    out = out or sys.stdout
    try:
        if args.file:
            with open(args.file, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        else:
            lines = (stdin or sys.stdin).readlines()
    except OSError as exc:
        print(f"cannot read input: {exc}", file=sys.stderr)
        return 2
    if not any(ln.strip() for ln in lines):
        print("empty input: nothing to report on", file=sys.stderr)
        return 2
    rep = build_report(parse_lines(lines), args.min_days, args.max_gap_minutes)
    out.write("\n".join(rep.out) + "\n")
    return 1 if rep.failing else 0


if __name__ == "__main__":
    sys.exit(main())

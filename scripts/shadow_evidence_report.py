#!/usr/bin/env python3
"""Shadow-week evidence report (plan task 1.12, "Evidence for D9b").

Reads scheduler journal lines (standard input, or the file given as an argument) and prints
the evidence table for the entry-gate shadow (`sw_live RECORD` / `sw_live DRYRUN`) and the stop
shadow (`live_stops ...`). Pure text processing: no project imports, no network, no database.

    journalctl -u polyclawd-scheduler --since "7 days ago" --no-pager | python3 scripts/shadow_evidence_report.py

Exit 0 = EVIDENCE: PASS, 1 = EVIDENCE: FAIL, 2 = usage error or empty input.

Reads one small value from the repository when it is run inside it (TICK_FAILED_PAGE_AFTER from
services/live_stop_shadow.py, parsed with `ast`, nothing imported); away from the repo it uses a
stated default.

Lines are parsed as generic `key=value` maps: unknown fields are ignored, nothing depends on
field position. Timestamps come from the Python log prefix (`2026-11-12 03:30:44,421 [name]`)
when there is one, else from the journald prefix (`Nov 12 03:30:44` or ISO). All times are
read as UTC (the VPS runs in UTC); day boundaries are UTC days. Rows of the evidence table that
need other sources (reconciliation report, saved pre-flight output, stop controls) are not
covered here.
"""

from __future__ import annotations

import argparse
import ast
import math
import os
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
SHOW_MAX = 10
# The two scheduler tasks that call run_live_stop_tick(source); a test fails if these names
# stop appearing in services/scheduler.py.
STOP_SOURCES = ("stop_evaluator", "stop_evaluator_urgent")
PAGE_AFTER_DEFAULT = 3  # used only when services/live_stop_shadow.py cannot be read here
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Every kind of line the emitting code can write that starts with sw_live or live_stops, built
# from the emitters at the commit that added this table (scripts/smart_wallet_fast_poll.py
# `_live_record_line`, `_live_stage1`, `_route_live_smart_wallet`, `_route_live_stage2`,
# `_check_governor_rails`, `_alert_min_size_skip`; services/live_stop_shadow.py
# `run_live_stop_tick`, `_load_state`, `_update_unpriced`, `_send_pages`). A line is matched by
# its system word and the text right after it. The category says what the report does:
#   record         one RECORD line per fired alert: the entry-gate table and every gate check
#   dryrun         `sw_live DRYRUN ... records=.. would_route=.. filtered=..` summary
#   filter_error   `sw_live DRYRUN error=<Type> stage=filter`: a batch of records went unlogged
#   armed          `sw_live ARMED` / `live_stops ARMED`: the shadow week must be all dry run
#   armed_path     stage 2 of the router; it runs only when armed, so any of them is a FAIL
#   router_warning stage 1 warnings (an archetype or record error, an unknown reason): counted, WARN
#   stop_dry       `live_stops DRYRUN`: one per stop tick, per source
#   stop_detail / stop_skipped / stop_failed   the other live_stops tick lines
#   stop_warning   housekeeping errors of the stop tick (state file, post-processing, a page): counted, WARN
# Anything else that starts with sw_live or live_stops is "unrecognised": a WARN, never silent.
_ARMED_PATH = ("routing ", "bankroll=", "dedup", "governor denied ", "clob setup failed",
               "GOVERNOR RAIL BLOCKING", "SIZING DEADLOCK", "deadlock tripwire failed",
               "governor-rail tripwire failed", "min-size skip alert failed",
               "telegram fill alert failed", "execute_intent failed", "sw-\\S+ \u2192 ")
LINE_KINDS = (
    ("sw_live", re.compile(r" RECORD\b"), "record"),
    ("sw_live", re.compile(r" DRYRUN error=\S+ stage=filter\b"), "filter_error"),
    ("sw_live", re.compile(r" DRYRUN\b"), "dryrun"),
    ("sw_live", re.compile(r" ARMED\b"), "armed"),
    ("sw_live", re.compile(r": (?:" + "|".join(_ARMED_PATH) + ")"), "armed_path"),
    ("sw_live", re.compile(r": (?:archetype lookup failed|archetype gate errored|record error type=|"
                           r"\d+ record\(s\) with a reason outside)"), "router_warning"),
    ("live_stops", re.compile(r" DRYRUN\b"), "stop_dry"),
    ("live_stops", re.compile(r" ARMED\b"), "armed"),
    ("live_stops", re.compile(r" DETAIL\b"), "stop_detail"),
    ("live_stops", re.compile(r" SKIPPED\b"), "stop_skipped"),
    ("live_stops", re.compile(r" FAILED\b"), "stop_failed"),
    ("live_stops", re.compile(r" (?:post-processing failed|state file |could not write state file|page \S+ failed)"),
     "stop_warning"),
)

# `scanner_hook` (scripts/smart_wallet_alert.py) catches every exception of a sweep, prints
#   SMART_WALLET_ALERT scanner_hook FAILED:
#   <traceback>
# to stderr (the journal: one journald entry per printed line, each with the journald prefix) and
# returns [], so the router never runs and the sweep leaves no sw_live line. Only the first line
# carries the text below and no sw_live marker; the traceback lines are not read.
HOOK_FAILED = re.compile(r"SMART_WALLET_ALERT scanner_hook FAILED")

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
MARKER = re.compile(r"\b(sw_live|live_stops)(?=[ :])")
KV = re.compile(r"(\w+)=(\S*)")
PY_TS = re.compile(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})[,.](\d{1,6}) *(?:\[|\|)")
ISO_TS = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:[.,]\d+)?([+-]\d{2}:?\d{2}|Z)?")
JD_TS = re.compile(r"^([A-Z][a-z]{2}) +(\d{1,2}) (\d{2}):(\d{2}):(\d{2})")
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


# --------------------------------------------------------------------------- parsing


def _parse_time(text, default_year):
    """(UTC datetime, source) from the line prefix, or (None, None). The source is "python"
    (the Python log prefix), "iso" (an ISO journald prefix) or "journald" (a prefix without a
    year: the year is assumed)."""
    m = PY_TS.search(text)
    if m:
        y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
        return datetime(y, mo, d, h, mi, s), "python"
    m = ISO_TS.match(text)
    if m:
        y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
        t = datetime(y, mo, d, h, mi, s)
        off = m.group(7)
        if off and off != "Z":
            sign = 1 if off[0] == "+" else -1
            digits = off[1:].replace(":", "")
            t -= sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        return t, "iso"
    m = JD_TS.match(text)
    if m and m.group(1) in MONTHS:
        return datetime(default_year, MONTHS[m.group(1)], int(m.group(2)),
                        int(m.group(3)), int(m.group(4)), int(m.group(5))), "journald"
    return None, None


def _num(v):
    """A finite float, or None for '-' / missing / not a number / nan / inf."""
    if v is None or v == "-":
        return None
    try:
        f = float(v)
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def _bad_num(v):
    """A value that is there but is not a number (a '-' is the emitter's way to say 'missing')."""
    return v is not None and v != "-" and _num(v) is None


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


def _classify(system, rest):
    """The LINE_KINDS category of a line, or None (unrecognised)."""
    for sysname, pattern, category in LINE_KINDS:
        if sysname == system and pattern.match(rest):
            return category
    return None


def _summary_ok(kv):
    return (all(k in kv for k in DRYRUN_REQUIRED) and kv["records"].isdigit() and kv["would_route"].isdigit()
            and _filtered_counts(kv["filtered"]) is not None
            and (kv.get("gamma_missing") is None or kv["gamma_missing"].isdigit()))


def _usable(system, category, kv):
    """False when a line of a recognised kind lacks a field a check needs or holds a value that
    does not parse (B1). `arch=` and the other diagnostic fields are never needed."""
    if category == "record":
        if any(k not in kv for k in RECORD_REQUIRED):
            return False
        if kv["verdict"] not in ("would_route", "filtered"):
            return False
        if kv["verdict"] == "would_route" and kv["reason"] != "-":
            return False
        return not any(_bad_num(kv.get(k)) for k in ("price", "bid", "ask", "spread_pct"))
    if category == "dryrun":
        return _summary_ok(kv)
    if category == "filter_error":
        return bool(kv.get("error"))
    if category == "armed":
        return _summary_ok(kv) if system == "sw_live" else bool(kv.get("source"))
    if category == "stop_dry":
        return bool(kv.get("source")) and "would_exit" in kv and "spread_held" in kv
    if category == "stop_skipped":
        return bool(kv.get("source"))
    if category == "stop_failed":
        return bool(kv.get("source")) and kv.get("consecutive", "").isdigit()
    return True


def parse_lines(lines):
    """Return a dict of parsed material. Lines that are neither sw_live nor live_stops are skipped."""
    cleaned = [ANSI.sub("", ln.rstrip("\n")) for ln in lines]
    default_year, year_source = datetime.now(timezone.utc).year, "the current UTC year"
    for ln in cleaned:
        m = PY_TS.search(ln)
        if m:
            default_year, year_source = int(m.group(1)), "the first line with a Python log prefix"
            break

    out = {k: [] for k in ("events", "no_time", "unrecognised", "malformed", "records", "dryruns", "armed",
                           "filter_errors", "router_warnings", "stop_warnings", "stop_dry", "stop_detail",
                           "stop_skipped", "stop_failed", "hook_failures", "hook_untimed")}
    by_category = {"record": "records", "dryrun": "dryruns", "filter_error": "filter_errors",
                   "router_warning": "router_warnings", "stop_warning": "stop_warnings",
                   "stop_dry": "stop_dry", "stop_detail": "stop_detail", "stop_skipped": "stop_skipped",
                   "stop_failed": "stop_failed"}
    total = 0
    time_sources = Counter()
    backward_steps, previous = 0, None
    for n, ln in enumerate(cleaned, 1):
        if not ln.strip():
            continue
        total += 1
        m = MARKER.search(ln)
        if not m:
            h = HOOK_FAILED.search(ln)
            if h:
                ts, _src = _parse_time(ln[:h.start()], default_year)
                out["hook_failures" if ts is not None else "hook_untimed"].append(
                    {"n": n, "ts": ts, "raw": ln})
            continue
        system, rest = m.group(1), ln[m.end():]
        category = _classify(system, rest)
        if category is None:
            out["unrecognised"].append((n, ln))
            continue
        ts, source = _parse_time(ln[:m.start()], default_year)
        kv = dict(KV.findall(rest))
        e = {"n": n, "ts": ts, "system": system, "kind": category, "kv": kv, "raw": ln}
        usable = _usable(system, category, kv)
        if not usable:
            out["malformed"].append((n, ln))
        if category in ("armed", "armed_path"):
            out["armed"].append(e)  # an ARMED line fails the report whatever else is wrong with it
        if ts is None:
            out["no_time"].append((n, ln))
            continue
        time_sources[source] += 1
        if previous is not None and ts < previous - timedelta(hours=24):
            backward_steps += 1
        previous = ts
        out["events"].append(e)
        if usable and category in by_category:
            out[by_category[category]].append(e)
    out.update(total=total, time_sources=time_sources, year_used=default_year, year_source=year_source,
               backward_steps=backward_steps)
    return out


def page_threshold(repo=REPO):
    """(TICK_FAILED_PAGE_AFTER, where it came from): read with `ast` from
    services/live_stop_shadow.py, else the default."""
    rel = os.path.join("services", "live_stop_shadow.py")
    try:
        with open(os.path.join(repo, rel), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "TICK_FAILED_PAGE_AFTER"
                                                     for t in node.targets):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, int):
                    return node.value.value, f"read from {rel}"
        return PAGE_AFTER_DEFAULT, f"default {PAGE_AFTER_DEFAULT} (TICK_FAILED_PAGE_AFTER not found in {rel})"
    except (OSError, SyntaxError):
        return PAGE_AFTER_DEFAULT, f"default {PAGE_AFTER_DEFAULT} ({rel} not readable here)"



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


def _longest_gap(first, stamps, last):
    """(longest gap, start, end) over the window start, the sorted `stamps` and the window end."""
    marks = [first] + sorted(stamps) + [last]
    longest, a, b = timedelta(0), marks[0], marks[0]
    for x, y in zip(marks, marks[1:]):
        if y - x > longest:
            longest, a, b = y - x, x, y
    return longest, a, b


def _show(rep, items, label):
    for n, ln in items[:SHOW_MAX]:
        rep.text(f"    {label} line {n}: {ln[:300]}")
    if len(items) > SHOW_MAX:
        rep.text(f"    ... and {len(items) - SHOW_MAX} more")


def build_report(parsed, min_days=7.0, max_gap_minutes=10.0, max_filter_errors=5, allow_empty_days=0,
                 page_after=None, max_hook_failure_share=0.10):
    rep = Report()
    P = parsed
    events = P["events"]
    if page_after is None:
        page_after = page_threshold()

    rep.text("SHADOW EVIDENCE REPORT")
    rep.text(f"input: {P['total']} lines, {len(events)} sw_live/live_stops lines with a parseable time")
    rep.text(f"lines without a parseable time: {len(P['no_time'])}")
    _show(rep, P["no_time"], "no-time")
    rep.text(f"scanner_hook FAILED lines without a parseable time: {len(P['hook_untimed'])} (counted in "
             f"hook_failures; they do not fail untimed_lines: a traceback line may lack the log prefix)")
    _show(rep, [(e["n"], e["raw"]) for e in P["hook_untimed"]], "hook-untimed")
    rep.text(f"unrecognised sw_live/live_stops line kinds: {len(P['unrecognised'])}")
    _show(rep, P["unrecognised"], "unrecognised")
    rep.text(f"malformed lines (a field a check needs is missing): {len(P['malformed'])}")
    _show(rep, P["malformed"], "malformed")

    rep.head("LINES")
    rep.check(not P["malformed"], "malformed_lines",
              f"{len(P['malformed'])} recognised sw_live/live_stops lines cannot be used (a needed field "
              f"missing or a value that does not parse); up to {SHOW_MAX} are printed above")
    rep.check(not P["no_time"], "untimed_lines",
              f"{len(P['no_time'])} recognised lines have no parseable time, so they cannot be counted in a "
              f"day or a gap")
    rep.check(not P["unrecognised"], "unrecognised_lines",
              f"{len(P['unrecognised'])} sw_live/live_stops lines of a kind this report does not know (a new "
              f"emitter line should be looked at); up to {SHOW_MAX} are printed above", warn=True)
    rw, sw_ = P["router_warnings"], P["stop_warnings"]
    rep.check(not rw and not sw_, "emitter_warnings",
              f"{len(rw)} router warning lines (archetype or record errors, unknown reasons) and {len(sw_)} "
              f"stop-tick housekeeping errors (state file, post-processing, page); up to {SHOW_MAX} are printed",
              warn=True)
    _show(rep, [(e["n"], e["raw"]) for e in rw + sw_], "warning")

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
        ts_src = P["time_sources"]
        rep.text(f"time taken from the Python log prefix: {ts_src['python']} lines; from an ISO journald prefix: "
                 f"{ts_src['iso']}; from a journald prefix without a year: {ts_src['journald']}")
        rep.check(ts_src["journald"] == 0, "year_inferred",
                  f"{ts_src['journald']} lines took their time from a journald prefix without a year; the year "
                  f"{P['year_used']} was used ({P['year_source']})", warn=True)
        rep.check(P["backward_steps"] == 0, "time_steps_backwards",
                  f"{P['backward_steps']} times the timestamps, in input order, step back by more than 24 hours "
                  f"(a year boundary in journald-only input looks like this)", warn=True)
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

    if events:
        entry_days = {_day(e["ts"]) for e in records + P["dryruns"]}
        d0, d1 = first.date(), last.date()
        between = [(d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, (d1 - d0).days)]
        empty = [d for d in between if d not in entry_days]
        if not empty:
            rep.check(True, "entry_shadow_daily_presence",
                      f"every whole UTC day between the first and the last day of the window ({len(between)} days) "
                      f"has a sw_live RECORD or DRYRUN line")
        elif len(empty) <= allow_empty_days:
            rep.check(True, "entry_shadow_daily_presence",
                      f"{len(empty)} empty days allowed by flag: "
                      + ", ".join(empty))
        else:
            rep.check(False, "entry_shadow_daily_presence",
                      f"{len(empty)} whole UTC days between the first and the last day have no sw_live RECORD or "
                      f"DRYRUN line (allowed by flag: {allow_empty_days}): " + ", ".join(empty))

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
    rep.check(bool(wr), "would_route_seen",
              f"{len(wr)} would_route records" if wr else
              "no would_route record in the window: the filters rejected everything, so the checks under "
              "'never routes a filtered record' had nothing to check", warn=True)
    probes = [
        # The router filters at a true price >= 0.60 and logs the price with 4 decimals, so a
        # would_route line logged `0.6000` can only come from a true price in [0.59995, 0.6):
        # it passed rightly. Only a logged price strictly above the ceiling is a failure.
        ("price_at_or_over_ceiling", lambda kv: (_num(kv["price"]) is not None and _num(kv["price"]) > PRICE_CEILING)),
        ("is_bot", lambda kv: kv["is_bot"] == "true"),
        ("clv_muted", lambda kv: kv["clv_muted"] == "true"),
        ("spread_pct_over_allowance", lambda kv: (_num(kv["spread_pct"]) is not None and _num(kv["spread_pct"]) > SPREAD_ALLOW)),
        ("derived_spread_over_allowance", lambda kv: _derived_spread_over(kv)),
        ("unpriced_field", lambda kv: any(_num(kv.get(k)) is None for k in ("price", "bid", "ask", "spread_pct"))),
        ("flag_not_logged", lambda kv: kv["is_bot"] not in ("true", "false") or kv["clv_muted"] not in ("true", "false")),
    ]
    for name, fn in probes:
        bad = [(e["n"], e["raw"]) for e in wr if fn(e["kv"])]
        note = (" (logged to 4 decimals; 0.6000 can be a price just under the ceiling)"
                if name == "price_at_or_over_ceiling" else "")
        rep.check(not bad, f"never_routes:{name}", f"{len(bad)} of {len(wr)} would_route records{note}")
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
    rep.check(bool(P["dryruns"]), "summaries_present",
              f"{len(P['dryruns'])} sw_live DRYRUN summary lines" if P["dryruns"] else
              "no sw_live DRYRUN summary line at all: summary_adds_up had nothing to check")
    rep.check(not bad_sum, "summary_adds_up",
              f"{len(P['dryruns'])} summaries; {len(bad_sum)} where records != would_route + filtered")
    _show(rep, bad_sum, "bad summary")
    rec_by_day = Counter({day: d["records"] for day, d in per_day.items()})
    for day in sorted(set(rec_by_day) | set(sum_by_day)):
        a, b = rec_by_day.get(day, 0), sum_by_day.get(day, 0)
        rep.check(a == b, f"record_lines_vs_summaries:{day}",
                  f"{a} RECORD lines, summaries report {b}" + ("" if a == b else
                  " (can straddle midnight or a restart)"), warn=True)

    errs = Counter(e["kv"]["error"] for e in P["filter_errors"])
    n_err = sum(errs.values())
    by_type = ", ".join(f"{k}:{v}" for k, v in sorted(errs.items())) or "none"
    rep.check(n_err == 0, "filter_stage_errors",
              f"{n_err} `sw_live DRYRUN error=<Type> stage=filter` lines (a whole batch of records went "
              f"unlogged each time), by type: {by_type}; FAIL above {max_filter_errors}",
              warn=n_err <= max_filter_errors)

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
    # `arch=` is not in the emitter yet (a later commit adds it): test lines append it by hand.
    with_arch = [e for e in records if "arch" in e["kv"]]
    if with_arch:
        counts = Counter(e["kv"].get("arch", "not logged") for e in wr)
        other = sum(v for k, v in counts.items() if k not in ("ok", "abstain"))
        rep.text(f"would_route records by archetype check: ok {counts['ok']}, abstain {counts['abstain']}"
                 + (f", other {other}" if other else ""))
        every_abstain = bool(wr) and all(e["kv"].get("arch") == "abstain" for e in wr)
        rep.check(not every_abstain, "archetype_gate_had_data",
                  "the archetype gate never had data to judge a routed record: its table may be empty"
                  if every_abstain else
                  f"{counts['ok']} of {len(wr)} would_route records were judged by the archetype gate",
                  warn=True)
    else:
        rep.text("archetype_gate_had_data: not logged (no RECORD line carries arch=)")
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
    by_source = defaultdict(list)
    for e in dry:
        by_source[e["kv"]["source"]].append(e)
    lines_by_source = defaultdict(list)  # a line of any tick kind, per source
    for e in dry + P["stop_skipped"] + P["stop_failed"]:
        lines_by_source[e["kv"]["source"]].append(e)
    others = sorted(set(lines_by_source) - set(STOP_SOURCES))
    if others:
        rep.text("stop tick lines from source names the scheduler is not expected to use: "
                 + ", ".join(f"{k} ({len(lines_by_source[k])})" for k in others))
    for src in STOP_SOURCES:
        xs = by_source.get(src, [])
        rep.check(bool(xs), f"stop_source_present[{src}]",
                  f"{len(xs)} DRYRUN lines" if xs else
                  "no live_stops DRYRUN line from this source (a task that only skips or fails has never evaluated)")
        ys = lines_by_source.get(src, [])
        if not ys:
            continue
        # A skipped tick is a live task: when the two stop tasks collide, one logs SKIPPED and
        # does not evaluate, because the other is evaluating at that moment. So "the task is
        # alive" is judged from every kind of tick line of that source, and "the positions were
        # evaluated" (below) from DRYRUN lines of any source. Both include the two ends of the
        # window: a source that stops or starts partway is a gap even when the other keeps the
        # window open.
        longest, a, b = _longest_gap(first, [e["ts"] for e in ys], last)
        mins = longest.total_seconds() / 60.0
        rep.check(mins < max_gap_minutes, f"stop_task_alive[{src}]",
                  f"longest gap between tick lines (DRYRUN, SKIPPED or FAILED) of this source {mins:.1f} min, "
                  f"from {a:%Y-%m-%d %H:%M:%S} to {b:%Y-%m-%d %H:%M:%S} (window ends included); "
                  f"under {max_gap_minutes:g} min required")
    if events:
        longest, a, b = _longest_gap(first, [e["ts"] for e in dry], last)
        mins = longest.total_seconds() / 60.0
        rep.check(mins < max_gap_minutes, "stop_evaluation_gap",
                  f"longest gap between DRYRUN lines of any source {mins:.1f} min, from {a:%Y-%m-%d %H:%M:%S} "
                  f"to {b:%Y-%m-%d %H:%M:%S} (window ends included; a SKIPPED tick is not an evaluation); "
                  f"under {max_gap_minutes:g} min required")
    skipped = Counter(e["kv"]["source"] for e in P["stop_skipped"])
    rep.text(f"live_stops SKIPPED lines: {len(P['stop_skipped'])} (reported, do not fail); by source: "
             + (", ".join(f"{k}:{v}" for k, v in sorted(skipped.items())) or "none"))
    rep.text(f"live_stops FAILED lines: {len(P['stop_failed'])}")
    ticks = sorted([e for e in events if e["system"] == "live_stops"
                    and e["kind"] in ("stop_dry", "armed", "stop_failed")], key=lambda e: (e["ts"], e["n"]))
    run = longest_run = 0
    for e in ticks:
        run = run + 1 if e["kind"] == "stop_failed" else 0
        longest_run = max(longest_run, run)
    top = max((int(e["kv"]["consecutive"]) for e in P["stop_failed"]), default=0)
    rep.text(f"longest run of consecutive FAILED ticks: {longest_run} (largest consecutive= logged: {top})")
    threshold, where = page_after
    rep.check(top < threshold, "stop_failed_streak",
              f"largest consecutive= on a FAILED line {top}; the stop pages at {threshold} in a row ({where})")
    n_failed, n_dry = len(P["stop_failed"]), len(dry)
    share = n_failed / (n_failed + n_dry) if (n_failed + n_dry) else 0.0
    rep.check(share <= 0.01, "stop_failed_share",
              f"{n_failed} FAILED lines of {n_failed + n_dry} DRYRUN plus FAILED lines ({n_dry} DRYRUN, "
              f"{n_failed} FAILED = {share:.1%}); more than 1% warns", warn=True)

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
              f"{len(P['armed'])} ARMED or armed-path lines (sw_live ARMED, live_stops ARMED, and the stage 2 "
              f"lines `sw_live: routing`, `bankroll=`, `dedup`, `execute_intent failed` and the like); the "
              f"shadow week is supposed to be all dry run")
    if P["armed"]:
        rep.text("!!! ARMED LINES IN THE WINDOW !!!")
        _show(rep, [(e["n"], e["raw"]) for e in P["armed"]], "ARMED")

    # ---- sweeps that failed before the router ran
    rep.head("SCANNER HOOK FAILURES (SMART_WALLET_ALERT scanner_hook FAILED)")
    hook_all = P["hook_failures"] + P["hook_untimed"]
    n_hook = len(hook_all)
    per_hook_day = Counter(_day(e["ts"]) for e in P["hook_failures"])
    for day in sorted(per_hook_day):
        rep.text(f"{day}  scanner_hook failures {per_hook_day[day]}")
    if P["hook_untimed"]:
        rep.text(f"(no day) scanner_hook failures {len(P['hook_untimed'])}")
    n_sum = len(P["dryruns"])
    hook_share = n_hook / (n_hook + n_sum) if (n_hook + n_sum) else 0.0
    rep.check(n_hook == 0, "hook_failures",
              f"{n_hook} scanner_hook FAILED lines against {n_sum} sw_live DRYRUN summaries "
              f"({hook_share:.1%} of summaries plus failures); each is a sweep whose records, if any, never "
              f"reached the entry-filter log; FAIL above {max_hook_failure_share:.0%}",
              warn=hook_share <= max_hook_failure_share)
    _show(rep, [(e["n"], e["raw"]) for e in hook_all], "hook failure")

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
    ap.add_argument("--max-filter-errors", type=int, default=5,
                    help="FAIL when more `sw_live DRYRUN error=.. stage=filter` lines than this (default 5)")
    ap.add_argument("--allow-empty-days", type=int, default=0, metavar="N",
                    help="accept N whole UTC days with no entry-shadow line, knowingly (default 0)")
    ap.add_argument("--max-hook-failure-share", type=float, default=0.10, metavar="X",
                    help="FAIL when scanner_hook failures exceed this share of (DRYRUN summaries + failures) "
                         "(default 0.10); any failure below that is a WARN")
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
    rep = build_report(parse_lines(lines), args.min_days, args.max_gap_minutes,
                       args.max_filter_errors, args.allow_empty_days,
                       max_hook_failure_share=args.max_hook_failure_share)
    out.write("\n".join(rep.out) + "\n")
    return 1 if rep.failing else 0


if __name__ == "__main__":
    sys.exit(main())

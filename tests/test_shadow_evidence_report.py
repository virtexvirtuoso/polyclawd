"""Task 1.12 part B: scripts/shadow_evidence_report.py turns a week of journal lines into the
evidence table for D9b.

Input lines are built from what the emitting code produces: the entry-gate RECORD line comes
from `smart_wallet_fast_poll._live_record_line`, the filtered records and the DRYRUN summary
from the real `_route_live_smart_wallet`, and every `live_stops` line from the real
`run_live_stop_tick`. Only the log prefix (timestamp, logger name, level) is added here, so
a format change in an emitter breaks these tests.
"""

import io
import logging
import re
import threading
from datetime import datetime, timedelta

import pytest

from execution import live_config
from scripts import shadow_evidence_report as rpt
from scripts import smart_wallet_fast_poll as sw
from services import live_stop_shadow as shadow
from services import stop_evaluator

T0 = datetime(2026, 11, 5, 0, 0, 0)
SW_LOGGER = "scripts.smart_wallet_fast_poll"
ALL_REASONS = ("price_band", "bot_wallet", "clv_muted", "wide_spread", "unpriced_book")


# --------------------------------------------------------------------------- line builders


def pre(ts, name=SW_LOGGER, level="INFO"):
    return f"{ts:%Y-%m-%d %H:%M:%S},{ts.microsecond // 1000:03d} [{name}] {level}: "


def record_msg(verdict, reason=None, price=0.40, is_bot=False, clv_muted=False,
               bid=0.39, ask=0.40, spread=0.025, extra="", gamma=None, book=None):
    """A real RECORD line from the emitter. With neither gamma nor book given, the two
    trailing diagnostic fields are cut off again: that is a line from before they existed."""
    rec = {"alert_type": "entry", "price": price, "wallet": "0xabc", "is_bot": is_bot,
           "clv_muted": clv_muted, "market": "0xmarket0123456789", "asset": "123456789012345678900000AAAA"}
    info = {"category": "sports", "bid": bid, "ask": ask, "spread_pct": spread}
    if gamma is not None:
        info["gamma"] = gamma
    if book is not None:
        info["book"] = book
    line = sw._live_record_line(rec, verdict, reason, info)
    if gamma is None and book is None:
        line = re.sub(r" gamma=\S+ book=\S+$", "", line)
    return line + extra


def record_line(ts, *a, **k):
    return pre(ts) + record_msg(*a, **k)


def summary_msg(records, would_route, filtered):
    f = ",".join(f"{k}:{v}" for k, v in sorted(filtered.items())) or "-"
    return f"sw_live DRYRUN mode=PAPER armed=false records={records} would_route={would_route} filtered={f}"


def summary_line(ts, *a):
    return pre(ts) + summary_msg(*a)


@pytest.fixture
def stop_msgs(monkeypatch, caplog, tmp_path):
    """Run the real stop tick with a stubbed evaluation and return the live_stops messages."""
    caplog.set_level(logging.INFO, logger="live_stops")
    monkeypatch.setattr(shadow, "_PAGE_STATE", {})
    monkeypatch.setattr(shadow, "_TICK_LOCK", threading.Lock())
    monkeypatch.setattr(shadow, "_PAGE_LOCK", threading.Lock())
    shadow.reset_state_for_tests()

    def run(entries=(), armed=False, fail=None, hold_lock=False, source="stop_evaluator_urgent"):
        caplog.clear()

        def fake_eval(dry_run=True):
            if fail:
                raise fail
            return list(entries)

        monkeypatch.setattr(stop_evaluator, "evaluate_live_stops", fake_eval)
        monkeypatch.setattr(live_config, "live_armed", lambda: armed)
        if hold_lock:
            shadow._TICK_LOCK.acquire()
        try:
            shadow.run_live_stop_tick(source, now=1_800_000_000.0,
                                      state_path=tmp_path / "state.json")
        finally:
            if hold_lock:
                shadow._TICK_LOCK.release()
        return [r.getMessage() for r in caplog.records if r.name == "live_stops"]

    yield run
    shadow.reset_state_for_tests()


ENTRIES = [
    {"id": 26, "action": "would_exit", "mark": 0.08, "loss_pct": 0.6667, "spread_pct": 0.0417, "threshold": 0.4417},
    {"id": 27, "action": "spread_held", "mark": 0.30, "loss_pct": 0.45, "spread_pct": 0.08, "threshold": 0.48},
    {"id": 28, "action": "ok"},
]


def stop_lines(ts, msgs):
    return [pre(ts, "live_stops") + m for m in msgs]


SOURCES = rpt.STOP_SOURCES


def clean_week(stop_msgs, days=7, step_min=5, omit_reason=None, tail=(), sources=SOURCES,
               entry_days=None, would_route=True):
    """Journal lines for `days` days: a stop DRYRUN every step_min minutes from each source in
    `sources`, plus one daily batch of entry-gate records (every required reason, one would_route)
    and its summary on each day in `entry_days` (default: every day)."""
    ticks = [stop_msgs(entries=ENTRIES, source=src) for src in sources]
    lines = []
    ts = T0
    end = T0 + timedelta(days=days)
    while ts <= end:
        for tick in ticks:
            lines += stop_lines(ts, tick)
        ts += timedelta(minutes=step_min)
    for d in range(days) if entry_days is None else entry_days:
        t = T0 + timedelta(days=d, hours=12)
        filtered = {}
        for r in ALL_REASONS:
            if r == omit_reason:
                continue
            lines.append(record_line(t, "filtered", r, price=0.65 if r == "price_band" else 0.40))
            filtered[r] = 1
        if would_route:
            lines.append(record_line(t, "would_route"))
        lines.append(summary_line(t, len(filtered) + int(would_route), int(would_route), filtered))
    lines += list(tail)
    # chronological like a journal (a stable sort; a line without a time prefix goes last)
    lines.sort(key=lambda ln: ln[:23] if ln[:2] == "20" else "9999")
    return lines


def run(lines, *args):
    out = io.StringIO()
    rc = rpt.main(list(args), stdin=io.StringIO("\n".join(lines) + "\n"), out=out)
    return rc, out.getvalue()


def checks_failing(text):
    return re.findall(r"^\[FAIL\] (\S+):", text, re.M)


# --------------------------------------------------------------------------- the plan's cases


def test_clean_seven_day_input_passes(stop_msgs):
    rc, out = run(clean_week(stop_msgs))
    assert rc == 0, out
    assert out.rstrip().splitlines()[-1] == "EVIDENCE: PASS"
    assert "not covered by this report" in out


def test_three_day_input_fails_on_the_window(stop_msgs):
    rc, out = run(clean_week(stop_msgs, days=3))
    assert rc == 1 and checks_failing(out) == ["window"]
    assert "span 3.00 days" in out


def test_min_days_option(stop_msgs):
    rc, out = run(clean_week(stop_msgs, days=3), "--min-days", "3")
    assert rc == 0, out


def test_would_route_at_061_fails_and_prints_the_line(stop_msgs):
    bad = pre(T0 + timedelta(hours=5), ) + record_msg("would_route", price=0.61, bid=0.60, ask=0.61, spread=0.0164)
    rc, out = run(clean_week(stop_msgs, tail=[bad]))
    assert rc == 1 and "never_routes:price_at_or_over_ceiling" in checks_failing(out)
    assert "price=0.6100" in out  # the offending line is printed


def test_would_route_at_exactly_060_fails_too():
    lines = [record_line(T0, "would_route", price=0.60, bid=0.59, ask=0.60, spread=0.0167)]
    rc, out = run(lines)
    assert "never_routes:price_at_or_over_ceiling" in checks_failing(out)


def test_would_route_bot_muted_and_wide_spread_fail_each(stop_msgs):
    t = T0 + timedelta(hours=5)
    bad = [record_line(t, "would_route", is_bot=True),
           record_line(t, "would_route", clv_muted=True),
           record_line(t, "would_route", bid=0.36, ask=0.40, spread=0.1001)]
    rc, out = run(clean_week(stop_msgs, tail=bad))
    failing = checks_failing(out)
    assert "never_routes:is_bot" in failing
    assert "never_routes:clv_muted" in failing
    assert "never_routes:spread_pct_over_allowance" in failing
    assert rc == 1


def test_spread_exactly_010_is_not_a_failure(stop_msgs):
    edge = record_line(T0 + timedelta(hours=5), "would_route", bid=0.36, ask=0.40, spread=0.1000)
    rc, out = run(clean_week(stop_msgs, tail=[edge]))
    assert rc == 0, out


def test_derived_spread_catches_a_lying_spread_field():
    """spread_pct logged as 0.05 but the logged bid and ask give 0.25: judged from the fields."""
    lines = [record_line(T0, "would_route", bid=0.30, ask=0.40, spread=0.05)]
    rc, out = run(lines)
    assert "never_routes:derived_spread_over_allowance" in checks_failing(out)


def test_would_route_with_missing_price_bid_ask_or_spread_fails():
    for kw in (dict(price=None), dict(bid=None), dict(ask=None), dict(spread=None)):
        rc, out = run([record_line(T0, "would_route", **kw)])
        assert "never_routes:unpriced_field" in checks_failing(out), kw


def test_one_sided_book_dash_is_missing_for_that_field_only(stop_msgs):
    """A filtered record with ask=- parses; a would_route with ask=- fails; bid stays usable."""
    filt = record_line(T0 + timedelta(hours=5), "filtered", "unpriced_book", ask=None, spread=None,
                       gamma="present", book="one_sided")
    rc, out = run(clean_week(stop_msgs, tail=[filt]))
    assert rc == 0, out
    assert "unpriced_book records by book=: not logged:7, one_sided:1" in out


def test_required_reason_never_seen_fails_naming_it(stop_msgs):
    rc, out = run(clean_week(stop_msgs, omit_reason="clv_muted"))
    assert rc == 1 and checks_failing(out) == ["seen:clv_muted"]
    assert "NOT SEEN" in out


def test_summary_that_does_not_add_up_fails(stop_msgs):
    bad = summary_line(T0 + timedelta(hours=6), 10, 1, {"price_band": 2})
    rc, out = run(clean_week(stop_msgs, tail=[bad]))
    assert "summary_adds_up" in checks_failing(out)


def test_record_lines_vs_summary_mismatch_is_only_a_warning(stop_msgs):
    extra = summary_line(T0 + timedelta(hours=6), 3, 3, {})
    rc, out = run(clean_week(stop_msgs, tail=[extra]))
    assert rc == 0 and re.search(r"^\[WARN\] record_lines_vs_summaries:2026-11-05", out, re.M)


def test_stop_gap_of_12_minutes_fails_with_times(stop_msgs):
    """Rewritten in fix-6: the gap is per source."""
    tick = stop_msgs(entries=ENTRIES)
    skipped = stop_msgs(hold_lock=True)
    lines = []
    for m in (0, 5, 10, 15, 20, 32, 37):  # no DRYRUN between 00:20 and 00:32
        lines += stop_lines(T0 + timedelta(minutes=m), tick)
    lines += stop_lines(T0 + timedelta(minutes=26), skipped)  # a SKIPPED line inside the hole
    rc, out = run(lines, "--min-days", "0")
    assert "stop_gap[stop_evaluator_urgent]" in checks_failing(out)
    assert "12.0 min" in out and "00:20:00" in out and "00:32:00" in out
    assert "SKIPPED lines: 1" in out


def test_stop_gap_of_9_minutes_passes(stop_msgs):
    tick = stop_msgs(entries=ENTRIES)
    lines = []
    for m in (0, 5, 14, 19):
        lines += stop_lines(T0 + timedelta(minutes=m), tick)
    rc, out = run(lines, "--min-days", "0")
    assert "stop_gap[stop_evaluator_urgent]" not in checks_failing(out)
    assert "9.0 min" in out


def test_max_gap_minutes_option(stop_msgs):
    tick = stop_msgs(entries=ENTRIES)
    lines = stop_lines(T0, tick) + stop_lines(T0 + timedelta(minutes=9), tick)
    rc, out = run(lines, "--min-days", "0", "--max-gap-minutes", "5")
    assert "stop_gap[stop_evaluator_urgent]" in checks_failing(out)


def test_no_dryrun_line_at_all_fails_every_source_as_missing():
    rc, out = run([record_line(T0, "filtered", "price_band")], "--min-days", "0")
    failing = checks_failing(out)
    assert all(f"stop_source_present[{s}]" in failing for s in SOURCES)


def test_armed_line_fails_and_is_printed_loudly(stop_msgs):
    armed = stop_msgs(entries=[{"id": 28, "action": "ok"}], armed=True)
    assert any("live_stops ARMED" in m for m in armed)
    rc, out = run(clean_week(stop_msgs, tail=stop_lines(T0 + timedelta(hours=1), armed)))
    assert "no_armed_lines" in checks_failing(out)
    assert "ARMED LINES IN THE WINDOW" in out and "live_stops ARMED" in out


def test_sw_live_armed_line_also_fails():
    line = pre(T0) + "sw_live ARMED records=1 would_route=1 filtered=-"
    rc, out = run([line], "--min-days", "0")
    assert "no_armed_lines" in checks_failing(out)


def test_skipped_and_failed_are_counted_and_failed_runs_measured(stop_msgs):
    failed = stop_msgs(fail=RuntimeError("database is locked"))
    assert "live_stops FAILED" in failed[0]
    ok = stop_msgs(entries=ENTRIES)
    lines = []
    for i, m in enumerate([ok, failed, failed, failed, ok, failed]):
        lines += stop_lines(T0 + timedelta(minutes=5 * i), m)
    rc, out = run(lines, "--min-days", "0")
    assert "FAILED lines: 4" in out
    assert "longest run of consecutive FAILED ticks: 3" in out


def test_per_day_position_ticks_and_distinct_ids(stop_msgs):
    tick = stop_msgs(entries=ENTRIES)
    lines = []
    for m in range(0, 20, 5):
        lines += stop_lines(T0 + timedelta(minutes=m), tick)
    rc, out = run(lines, "--min-days", "0")
    assert "2026-11-05  ticks 4, would_exit position-ticks 4, spread_held position-ticks 4, distinct ids [26, 27]" in out
    assert "id=26: would_exit on 4 ticks, spread_held on 0 ticks" in out
    assert "id=27: would_exit on 0 ticks, spread_held on 4 ticks" in out


# --------------------------------------------------------------------------- prefixes and time


def test_journald_and_ansi_prefixed_lines_parse_like_bare_ones(stop_msgs):
    bare = clean_week(stop_msgs, days=1)
    jd = [f"Nov 05 {ln[11:19]} vps polyclawd-scheduler[1234]: " + ln for ln in bare]
    ansi = ["\x1b[32m" + ln + "\x1b[0m" for ln in bare]
    base = run(bare, "--min-days", "1")
    for variant in (jd, ansi):
        got = run(variant, "--min-days", "1")
        assert got == base


def test_journald_prefix_alone_gives_the_time_and_year_from_other_lines(stop_msgs):
    tick = stop_msgs(entries=ENTRIES)
    lines = [f"Nov 05 00:{m:02d}:00 vps polyclawd-scheduler[1]: [live_stops] INFO: {tick[0]}" for m in (0, 5, 10)]
    # Rewritten in fix-6: the year donor is a Python-prefixed line that is not evidence, so it does
    # not widen the window (the per-source gap now includes the ends of the window).
    lines.insert(0, pre(T0 - timedelta(days=1)) + "some other log line")
    rc, out = run(lines, "--min-days", "0")
    assert "longest gap between DRYRUN lines 5.0 min, from 2026-11-05 00:00:00 to 2026-11-05 00:05:00" in out


def test_iso_journald_prefix_with_offset_is_converted_to_utc(stop_msgs):
    tick = stop_msgs(entries=ENTRIES)
    lines = ["2026-11-05T02:00:00+0200 vps polyclawd-scheduler[1]: " + tick[0],
             "2026-11-05T02:05:00+0200 vps polyclawd-scheduler[1]: " + tick[0]]
    rc, out = run(lines, "--min-days", "0")
    assert "from 2026-11-05 00:00:00 to 2026-11-05 00:05:00" in out


def test_lines_without_a_parseable_time_are_counted_and_not_used(stop_msgs):
    tick = stop_msgs(entries=ENTRIES)
    lines = [tick[0], "no time here but " + tick[0]] + stop_lines(T0, tick)
    rc, out = run(lines, "--min-days", "0")
    assert "lines without a parseable time: 2" in out
    assert "live_stops DRYRUN lines: 1" in out


def test_unknown_line_kinds_are_counted_and_reported(stop_msgs):
    """Rewritten in fix-6: `live_stops could not write state file` and `sw_live: clob setup failed`
    are emitted lines with their own kind now; only genuinely unknown kinds are unrecognised."""
    lines = stop_lines(T0, stop_msgs(entries=ENTRIES)) + [
        pre(T0) + "sw_live BANANA records=1",
        pre(T0, "live_stops") + "live_stops BANANA x=1",
        pre(T0) + "sw_live: a line nobody wrote yet",
        pre(T0) + "sw_live_other something",  # not the marker word
    ]
    rc, out = run(lines, "--min-days", "0")
    assert "unrecognised sw_live/live_stops line kinds: 3" in out
    assert "sw_live BANANA" in out


def test_empty_input_exit_2():
    rc, out = run([""])
    assert rc == 2 and out == ""
    assert rpt.main([], stdin=io.StringIO(""), out=io.StringIO()) == 2


def test_usage_error_exit_2():
    assert rpt.main(["--no-such-flag"], stdin=io.StringIO("x\n"), out=io.StringIO()) == 2


def test_missing_file_exit_2(tmp_path):
    assert rpt.main([str(tmp_path / "nope.log")], out=io.StringIO()) == 2


def test_reads_a_file_argument(tmp_path, stop_msgs):
    p = tmp_path / "j.log"
    p.write_text("\n".join(clean_week(stop_msgs)) + "\n")
    out = io.StringIO()
    assert rpt.main([str(p)], out=out) == 0


def test_day_boundaries_are_utc_days(stop_msgs):
    a = pre(datetime(2026, 11, 5, 23, 59, 59)) + record_msg("filtered", "price_band")
    b = pre(datetime(2026, 11, 6, 0, 0, 0)) + record_msg("filtered", "price_band")
    rc, out = run([a, b], "--min-days", "0")
    assert re.search(r"^2026-11-05 +1 ", out, re.M) and re.search(r"^2026-11-06 +1 ", out, re.M)


# --------------------------------------------------------------------------- generic key=value


def test_unknown_extra_fields_are_ignored_and_field_order_does_not_matter(stop_msgs):
    msg = record_msg("would_route", extra=" gamma=present book=ok future_field=whatever")
    shuffled = "sw_live RECORD " + " ".join(sorted(msg.split(" ")[2:], reverse=True))
    lines = [pre(T0) + msg, pre(T0 + timedelta(minutes=1)) + shuffled]
    p = rpt.parse_lines(lines)
    assert len(p["records"]) == 2 and not p["malformed"]


def test_record_missing_a_needed_field_is_malformed_and_reported():
    msg = record_msg("filtered", "price_band").replace(" clv_muted=false", "")
    rc, out = run([pre(T0) + msg], "--min-days", "0")
    assert "malformed lines (a field a check needs is missing): 1" in out
    assert "line 1" in out


def test_new_reasons_are_counted_without_a_closed_list(stop_msgs):
    lines = [record_line(T0 + timedelta(minutes=i), "filtered", r)
             for i, r in enumerate(["crossed_book", "crossed_book", "archetype_error", "price_band"])]
    rc, out = run(lines, "--min-days", "0")
    table = out.split("== EACH GATE")[0]
    assert "archetype_error" in table and "crossed_book" in table
    assert re.search(r"^total +4 +0 +4 ", table, re.M)


def test_gamma_and_book_diagnostics_when_logged_and_when_not():
    logged = [
        record_line(T0, "filtered", "no_category", gamma="missing", book="none"),
        record_line(T0, "filtered", "price_band", gamma="present", book="ok"),
        record_line(T0, "filtered", "unpriced_book", gamma="present", book="empty"),
        record_line(T0, "filtered", "unpriced_book", gamma="present", book="one_sided"),
        record_line(T0, "filtered", "unpriced_book", gamma="present", book="empty"),
    ]
    rc, out = run(logged, "--min-days", "0")
    assert "2026-11-05  records with gamma logged 5, gamma=missing 1" in out
    assert "unpriced_book records by book=: empty:2, one_sided:1" in out
    rc, out = run([record_line(T0, "filtered", "unpriced_book")], "--min-days", "0")
    assert "gamma=missing per day: not logged" in out
    assert "unpriced_book records by book=: not logged" in out


# --------------------------------------------------------------------------- emitters


def test_real_router_output_parses_and_adds_up(caplog, monkeypatch):
    """Drive the real `_route_live_smart_wallet` with records the alert gate already refused."""
    monkeypatch.setenv("POLYCLAWD_MODE", "PAPER")
    caplog.set_level(logging.INFO, logger=SW_LOGGER)
    fired = [
        {"wallet": "0xa", "market": "0xm1", "asset": "tok1", "alert_type": "entry", "price": 0.7,
         "live_eligible": False, "live_filter_reason": "price_band", "is_bot": False, "clv_muted": False},
        {"wallet": "0xb", "market": "0xm2", "asset": "tok2", "alert_type": "entry", "price": 0.3,
         "live_eligible": False, "live_filter_reason": "bot_wallet", "is_bot": True, "clv_muted": False},
        {"wallet": "0xc", "market": "0xm3", "asset": "tok3", "alert_type": "entry", "price": 0.3,
         "live_eligible": False, "live_filter_reason": "clv_muted", "is_bot": False, "clv_muted": True},
    ]
    sw._route_live_smart_wallet(fired, {})
    msgs = [r.getMessage() for r in caplog.records if r.name == SW_LOGGER]
    lines = [pre(T0 + timedelta(seconds=i)) + m for i, m in enumerate(msgs)]
    p = rpt.parse_lines(lines)
    assert len(p["records"]) == 3 and len(p["dryruns"]) == 1 and not p["malformed"] and not p["unrecognised"]
    d = p["dryruns"][0]["kv"]
    assert d["records"] == "3" and d["would_route"] == "0"
    assert rpt._filtered_counts(d["filtered"]) == {"bot_wallet": 1, "clv_muted": 1, "price_band": 1}
    rc, out = run(lines, "--min-days", "0")
    assert "summary_adds_up" not in checks_failing(out)
    assert "[PASS] seen:price_band: seen 1 times" in out


def test_real_stop_ticks_parse_every_kind(stop_msgs):
    dry = stop_msgs(entries=ENTRIES)
    skipped = stop_msgs(hold_lock=True)
    failed = stop_msgs(fail=OSError("disk"))
    lines = []
    for i, m in enumerate([dry, skipped, failed]):
        lines += stop_lines(T0 + timedelta(minutes=i), m)
    p = rpt.parse_lines(lines)
    assert len(p["stop_dry"]) == 1 and len(p["stop_detail"]) == 2
    assert len(p["stop_skipped"]) == 1 and len(p["stop_failed"]) == 1
    assert not p["unrecognised"] and not p["malformed"] and not p["armed"]
    assert rpt._ids(p["stop_dry"][0]["kv"]["would_exit"]) == [26]


def test_loguru_style_prefix_is_accepted(stop_msgs):
    tick = stop_msgs(entries=ENTRIES)
    line = f"2026-11-05 00:00:00.123 | INFO     | services.live_stop_shadow:run_live_stop_tick:261 - {tick[0]}"
    assert len(rpt.parse_lines([line])["stop_dry"]) == 1


# --------------------------------------------------------------------------- script is pure


def test_script_imports_nothing_from_the_project_and_touches_no_network_or_database():
    import ast
    from pathlib import Path

    src = Path(rpt.__file__).read_text()
    imported = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    # fix-6 adds ast, math and os: pure standard library, still no project import
    assert imported <= {"__future__", "argparse", "ast", "math", "os", "re", "sys", "collections",
                        "datetime"}, imported


# =========================================================================== fix-6, part B


def one_day(stop_msgs, **kw):
    """A clean one-day input (both stop sources, one entry batch): needs --min-days 1."""
    return clean_week(stop_msgs, days=1, **kw)


def run1(lines, *args):
    return run(lines, "--min-days", "1", *args)


def failing(text):
    return checks_failing(text)


def status_of(text, name):
    m = re.search(rf"^\[(PASS|WARN|FAIL)\] {re.escape(name)}:", text, re.M)
    assert m, (name, text[-3000:])
    return m.group(1)


def filter_error_msg(monkeypatch, caplog, exc=ValueError("boom")):
    """The real `sw_live DRYRUN error=<Type> stage=filter` line, from the real router."""
    monkeypatch.setenv("POLYCLAWD_MODE", "PAPER")
    caplog.set_level(logging.INFO, logger=SW_LOGGER)
    caplog.clear()

    def boom(fired, gamma):
        raise exc

    monkeypatch.setattr(sw, "_live_stage1", boom)
    sw._route_live_smart_wallet([{"wallet": "0xa", "market": "0xm1"}], {})
    msgs = [r.getMessage() for r in caplog.records if r.name == SW_LOGGER]
    assert len(msgs) == 1 and "stage=filter" in msgs[0], msgs
    return msgs[0]


# --------------------------------------------------------------------------- B1 malformed


def test_clean_one_day_input_is_the_nearest_passing_neighbour_of_every_failing_case(stop_msgs):
    rc, out = run1(one_day(stop_msgs))
    assert rc == 0, out


def test_would_route_record_without_a_price_field_fails_malformed_lines(stop_msgs):
    bad = pre(T0 + timedelta(hours=5)) + record_msg("would_route").replace(" price=0.4000", "")
    assert " price=" not in bad
    rc, out = run1(one_day(stop_msgs, tail=[bad]))
    assert rc == 1 and failing(out) == ["malformed_lines"], out
    assert "malformed lines (a field a check needs is missing): 1" in out
    assert bad.split("] INFO: ")[1][:40] in out  # the line itself is printed


@pytest.mark.parametrize("mutate", [
    lambda m: m.replace(" price=0.4000", " price=abc"),
    lambda m: m.replace(" spread_pct=0.0250", " spread_pct=nan"),
    lambda m: m.replace("verdict=would_route", "verdict=maybe"),
])
def test_record_with_an_unparseable_number_or_verdict_is_malformed(stop_msgs, mutate):
    bad = pre(T0 + timedelta(hours=5)) + mutate(record_msg("would_route"))
    rc, out = run1(one_day(stop_msgs, tail=[bad]))
    assert "malformed_lines" in failing(out), out


def test_would_route_record_with_a_reason_is_malformed(stop_msgs):
    bad = pre(T0 + timedelta(hours=5)) + record_msg("would_route", "price_band")
    rc, out = run1(one_day(stop_msgs, tail=[bad]))
    assert "malformed_lines" in failing(out)


@pytest.mark.parametrize("line", [
    "sw_live DRYRUN mode=PAPER armed=false records=3 would_route=1",  # filtered= missing
    "sw_live DRYRUN mode=PAPER armed=false records=3 would_route=1 filtered=price_band",  # no count
    "sw_live DRYRUN mode=PAPER armed=false records=x would_route=1 filtered=-",
    "sw_live DRYRUN mode=PAPER armed=false records=3 would_route=1 filtered=- gamma_missing=?",
    "sw_live DRYRUN error= stage=filter",
])
def test_unusable_summaries_are_malformed(stop_msgs, line):
    rc, out = run1(one_day(stop_msgs, tail=[pre(T0 + timedelta(hours=5)) + line]))
    assert "malformed_lines" in failing(out), line


def test_stop_dryrun_without_a_source_is_malformed(stop_msgs):
    tick = stop_msgs(entries=ENTRIES)[0]
    bad = pre(T0 + timedelta(hours=5), "live_stops") + re.sub(r" source=\S+", "", tick)
    assert "source=" not in bad
    rc, out = run1(one_day(stop_msgs, tail=[bad]))
    assert "malformed_lines" in failing(out)


def test_failed_line_without_consecutive_is_malformed(stop_msgs):
    failed = stop_msgs(fail=RuntimeError("x"))[0]
    bad = pre(T0 + timedelta(hours=5), "live_stops") + re.sub(r" consecutive=\d+", "", failed)
    rc, out = run1(one_day(stop_msgs, tail=[bad]))
    assert "malformed_lines" in failing(out)


def test_malformed_armed_line_counts_as_armed_and_as_malformed():
    rc, out = run([pre(T0) + "sw_live ARMED records=x"], "--min-days", "0")
    assert "no_armed_lines" in failing(out) and "malformed_lines" in failing(out)


def test_only_ten_malformed_lines_are_printed(stop_msgs):
    bad = [pre(T0 + timedelta(hours=5, seconds=i)) + record_msg("would_route").replace(" price=0.4000", "")
           for i in range(12)]
    rc, out = run1(one_day(stop_msgs, tail=bad))
    assert "malformed lines (a field a check needs is missing): 12" in out
    assert out.count("malformed line ") == 10 and "... and 2 more" in out


def test_a_record_without_arch_is_not_malformed(stop_msgs):
    rc, out = run1(one_day(stop_msgs))
    assert "malformed_lines" not in failing(out) and status_of(out, "malformed_lines") == "PASS"


# --------------------------------------------------------------------------- B2 time and kinds


def test_one_untimed_evidence_line_fails_untimed_lines(stop_msgs):
    tick = stop_msgs(entries=ENTRIES, source="stop_evaluator")[0]
    rc, out = run1(one_day(stop_msgs, tail=[tick]))  # the same line without a time prefix
    assert rc == 1 and failing(out) == ["untimed_lines"], out
    assert "lines without a parseable time: 1" in out


def test_unknown_sw_live_kind_is_a_warn_and_does_not_change_the_exit_code(stop_msgs):
    rc, out = run1(one_day(stop_msgs, tail=[pre(T0 + timedelta(hours=5)) + "sw_live FOO records=1"]))
    assert rc == 0 and status_of(out, "unrecognised_lines") == "WARN"
    assert "sw_live FOO" in out and "unrecognised sw_live/live_stops line kinds: 1" in out


ARMED_PATH_SAMPLES = [
    ("sw_live: routing 2 fired alerts to live executor", "sw_live: routing %d fired alerts to live executor"),
    ("sw_live: bankroll=$16.97 → size=$2.00 (frac=10% floor=$2 cap=$15)", "sw_live: bankroll=$%.2f → size=$%.2f"),
    ("sw_live: dedup — ref sw-1 already in live_open_orders (status=open), skipping",
     "sw_live: dedup — ref %s already in live_open_orders"),
    ("sw_live: dedup check failed: boom", "sw_live: dedup check failed: %s"),
    ("sw_live: execute_intent failed for sw-1: boom", "sw_live: execute_intent failed for %s: %s"),
    ("sw_live: governor denied sw-1: kill_floor", "sw_live: governor denied %s: %s"),
    ("sw_live: clob setup failed: boom", "sw_live: clob setup failed: %s"),
    ("sw_live: sw-2026-11-12-0xabc-0 → maker_filled (entry=0.40, alert=0.41) reason=",
     "sw_live: %s → %s (entry=%.2f, alert=%.2f) reason=%s"),
    ("sw_live: GOVERNOR RAIL BLOCKING — kill_floor: x. y", "sw_live: GOVERNOR RAIL BLOCKING"),
    ("sw_live: SIZING DEADLOCK — size $5.00 > cap $1.00 (bankroll $9.00, frac)", "sw_live: SIZING DEADLOCK"),
    ("sw_live: deadlock tripwire failed: boom", "sw_live: deadlock tripwire failed: %s"),
    ("sw_live: governor-rail tripwire failed: boom", "sw_live: governor-rail tripwire failed: %s"),
    ("sw_live: min-size skip alert failed: boom", "sw_live: min-size skip alert failed: %s"),
    ("sw_live: telegram fill alert failed: boom", "sw_live: telegram fill alert failed: %s"),
]
WARNING_SAMPLES = [
    ("sw_live: archetype lookup failed (blocking)", "scripts/smart_wallet_fast_poll.py", "sw_live: archetype lookup failed"),
    ("sw_live: archetype gate errored type=OSError (blocking)", "scripts/smart_wallet_fast_poll.py",
     "sw_live: archetype gate errored type=%s"),
    ("sw_live: record error type=KeyError", "scripts/smart_wallet_fast_poll.py", "sw_live: record error type=%s"),
    ("sw_live: 2 record(s) with a reason outside LIVE_FILTER_REASONS, first=x", "scripts/smart_wallet_fast_poll.py",
     "sw_live: %d record(s) with a reason outside LIVE_FILTER_REASONS"),
    ("live_stops post-processing failed source=stop_evaluator: OSError: x", "services/live_stop_shadow.py",
     "live_stops post-processing failed source=%s"),
    ("live_stops state file /x is corrupt (e); treating as empty", "services/live_stop_shadow.py",
     "live_stops state file %s is corrupt"),
    ("live_stops could not write state file /x: OSError: y", "services/live_stop_shadow.py",
     "live_stops could not write state file %s"),
    ("live_stops page live-stop-x failed: OSError: y", "services/live_stop_shadow.py", "live_stops page %s failed"),
]


@pytest.mark.parametrize("sample,fragment", ARMED_PATH_SAMPLES)
def test_every_armed_path_line_fails_like_an_armed_line(stop_msgs, sample, fragment):
    rc, out = run1(one_day(stop_msgs, tail=[pre(T0 + timedelta(hours=5)) + sample]))
    assert rc == 1 and failing(out) == ["no_armed_lines"], (sample, out)
    assert "unrecognised sw_live/live_stops line kinds: 0" in out


@pytest.mark.parametrize("sample,path,fragment", WARNING_SAMPLES)
def test_known_non_evidence_warning_lines_are_counted_not_unrecognised(stop_msgs, sample, path, fragment):
    rc, out = run1(one_day(stop_msgs, tail=[pre(T0 + timedelta(hours=5)) + sample]))
    assert rc == 0 and status_of(out, "emitter_warnings") == "WARN", out
    assert "unrecognised sw_live/live_stops line kinds: 0" in out


def test_the_line_kind_table_still_matches_the_emitters():
    from pathlib import Path

    root = Path(rpt.__file__).resolve().parents[1]
    fast = (root / "scripts" / "smart_wallet_fast_poll.py").read_text()
    shadow_src = (root / "services" / "live_stop_shadow.py").read_text()
    for sample, fragment in ARMED_PATH_SAMPLES:
        assert fragment in fast, fragment
    for sample, path, fragment in WARNING_SAMPLES:
        assert fragment in (fast if path.startswith("scripts") else shadow_src), fragment


def test_filter_stage_error_line_is_its_own_kind_not_unrecognised(stop_msgs, monkeypatch, caplog):
    msg = filter_error_msg(monkeypatch, caplog)
    p = rpt.parse_lines([pre(T0) + msg])
    assert len(p["filter_errors"]) == 1 and not p["unrecognised"] and not p["malformed"]


# --------------------------------------------------------------------------- B3 per-source gap


def test_scheduler_still_calls_the_stop_tick_with_exactly_the_report_source_names():
    from pathlib import Path

    src = (Path(rpt.__file__).resolve().parents[1] / "services" / "scheduler.py").read_text()
    names = set(re.findall(r'_run_live_stop_shadow\("([A-Za-z_]+)"\)', src))
    assert names == set(rpt.STOP_SOURCES), names


def test_a_missing_source_fails_naming_it(stop_msgs):
    rc, out = run1(one_day(stop_msgs, sources=("stop_evaluator",)))
    assert rc == 1 and failing(out) == ["stop_source_present[stop_evaluator_urgent]"], out


def _two_sources_with_a_hole(stop_msgs, hole_source, hole_minutes):
    ticks = {src: stop_msgs(entries=ENTRIES, source=src)[0] for src in SOURCES}
    lines = []
    for src in SOURCES:
        minutes = range(0, 24 * 60 + 1, 5)
        if src == hole_source:
            minutes = [m for m in minutes if m not in (105, 110)] + hole_minutes
        for m in minutes:
            lines.append(pre(T0 + timedelta(minutes=m), "live_stops") + ticks[src])
    base = one_day(stop_msgs, sources=())
    return lines + base


def test_one_source_with_a_12_minute_gap_fails_naming_it_while_the_other_is_continuous(stop_msgs):
    lines = _two_sources_with_a_hole(stop_msgs, "stop_evaluator", [112])
    rc, out = run1(lines)
    assert rc == 1 and failing(out) == ["stop_gap[stop_evaluator]"], out
    assert "12.0 min" in out and status_of(out, "stop_gap[stop_evaluator_urgent]") == "PASS"


def test_the_same_hole_in_one_source_would_pass_if_the_sources_were_pooled_but_is_still_9_minutes_ok(stop_msgs):
    lines = _two_sources_with_a_hole(stop_msgs, "stop_evaluator", [109])  # 100 -> 109: 9 minutes
    rc, out = run1(lines)
    assert rc == 0, out


def test_a_source_that_stops_partway_fails_even_though_the_other_keeps_the_window_open(stop_msgs):
    ticks = {src: stop_msgs(entries=ENTRIES, source=src)[0] for src in SOURCES}
    lines = []
    for m in range(0, 24 * 60 + 1, 5):
        lines.append(pre(T0 + timedelta(minutes=m), "live_stops") + ticks["stop_evaluator_urgent"])
        if m < 600:
            lines.append(pre(T0 + timedelta(minutes=m), "live_stops") + ticks["stop_evaluator"])
    rc, out = run1(lines + one_day(stop_msgs, sources=()))
    assert "stop_gap[stop_evaluator]" in failing(out), out
    assert "stop_gap[stop_evaluator_urgent]" not in failing(out)


def test_the_overall_gap_stays_as_an_information_line(stop_msgs):
    rc, out = run1(one_day(stop_msgs))
    assert "longest gap between DRYRUN lines of either source together" in out


# --------------------------------------------------------------------------- B4 failed ticks


def _failed_lines(stop_msgs, upto):
    msgs = [stop_msgs(fail=RuntimeError("database is locked"))[0] for _ in range(upto)]
    return msgs


def test_failed_line_with_consecutive_3_fails_and_2_does_not(stop_msgs):
    msgs = _failed_lines(stop_msgs, 3)
    assert "consecutive=2" in msgs[1] and "consecutive=3" in msgs[2]
    base = one_day(stop_msgs)
    rc, out = run1(base + [pre(T0 + timedelta(hours=5), "live_stops") + msgs[2]])
    assert rc == 1 and failing(out) == ["stop_failed_streak"], out
    assert "pages at 3 in a row (read from services/live_stop_shadow.py)" in out
    rc, out = run1(base + [pre(T0 + timedelta(hours=5), "live_stops") + msgs[1]])
    assert "stop_failed_streak" not in failing(out), out


def test_page_threshold_is_read_by_ast_and_defaults_away_from_the_repo(tmp_path):
    assert rpt.page_threshold() == (shadow.TICK_FAILED_PAGE_AFTER, "read from services/live_stop_shadow.py")
    value, where = rpt.page_threshold(str(tmp_path))
    assert value == 3 and where.startswith("default 3") and "not readable here" in where
    (tmp_path / "services").mkdir()
    (tmp_path / "services" / "live_stop_shadow.py").write_text("X = 1\n")
    value, where = rpt.page_threshold(str(tmp_path))
    assert value == 3 and "not found" in where
    (tmp_path / "services" / "live_stop_shadow.py").write_text("TICK_FAILED_PAGE_AFTER = 7\n")
    assert rpt.page_threshold(str(tmp_path)) == (7, "read from services/live_stop_shadow.py")


@pytest.mark.parametrize("n_failed,status", [(5, "PASS"), (6, "WARN")])
def test_failed_share_warns_above_one_percent_and_never_fails(stop_msgs, n_failed, status):
    failed = stop_msgs(fail=RuntimeError("x"))[0]  # consecutive=1: under the paging threshold
    tail = [pre(T0 + timedelta(hours=5, seconds=i), "live_stops") + failed for i in range(n_failed)]
    rc, out = run1(one_day(stop_msgs, tail=tail))
    assert status_of(out, "stop_failed_share") == status and rc == 0, out
    assert f"{n_failed} FAILED lines of {578 + n_failed}" in out


def test_two_percent_failed_warns(stop_msgs):
    failed = stop_msgs(fail=RuntimeError("x"))[0]
    tail = [pre(T0 + timedelta(hours=5, seconds=i), "live_stops") + failed for i in range(12)]
    rc, out = run1(one_day(stop_msgs, tail=tail))
    assert status_of(out, "stop_failed_share") == "WARN" and rc == 0


# --------------------------------------------------------------------------- B5 nothing to check


def test_no_summary_line_fails_summaries_present(stop_msgs):
    lines = [ln for ln in one_day(stop_msgs) if "sw_live DRYRUN" not in ln]
    rc, out = run1(lines)
    assert rc == 1 and failing(out) == ["summaries_present"], out


def test_no_would_route_record_warns_and_does_not_change_the_exit_code(stop_msgs):
    rc, out = run1(one_day(stop_msgs, would_route=False))
    assert rc == 0 and status_of(out, "would_route_seen") == "WARN", out
    assert "the filters rejected everything" in out


def test_would_route_seen_passes_with_one(stop_msgs):
    assert status_of(run1(one_day(stop_msgs))[1], "would_route_seen") == "PASS"


# --------------------------------------------------------------------------- B6 filter errors


@pytest.mark.parametrize("n,status,rc_expected", [(0, "PASS", 0), (1, "WARN", 0), (5, "WARN", 0), (6, "FAIL", 1)])
def test_filter_stage_errors_warn_up_to_the_flag_and_fail_above(stop_msgs, monkeypatch, caplog, n, status, rc_expected):
    msg = filter_error_msg(monkeypatch, caplog)
    tail = [pre(T0 + timedelta(hours=5, seconds=i)) + msg for i in range(n)]
    rc, out = run1(one_day(stop_msgs, tail=tail))
    assert status_of(out, "filter_stage_errors") == status and rc == rc_expected, out
    if n:
        assert f"{n} `sw_live DRYRUN error=<Type> stage=filter` lines" in out and f"ValueError:{n}" in out


def test_max_filter_errors_option(stop_msgs, monkeypatch, caplog):
    msg = filter_error_msg(monkeypatch, caplog, exc=OSError("x"))
    tail = [pre(T0 + timedelta(hours=5, seconds=i)) + msg for i in range(3)]
    rc, out = run1(one_day(stop_msgs, tail=tail), "--max-filter-errors", "2")
    assert status_of(out, "filter_stage_errors") == "FAIL" and "OSError:3" in out


# --------------------------------------------------------------------------- B7 empty days


def test_days_with_no_entry_line_fail_naming_them(stop_msgs):
    lines = clean_week(stop_msgs, days=8, entry_days=[0, 1, 2, 7])
    rc, out = run(lines)
    assert rc == 1 and failing(out) == ["entry_shadow_daily_presence"], out
    for day in ("2026-11-08", "2026-11-09", "2026-11-10", "2026-11-11"):
        assert day in out.split("entry_shadow_daily_presence:")[1].split("\n")[0]
    assert "4 whole UTC days" in out


def test_allow_empty_days_accepts_them_knowingly(stop_msgs):
    lines = clean_week(stop_msgs, days=8, entry_days=[0, 1, 2, 7])
    rc, out = run(lines, "--allow-empty-days", "4")
    assert rc == 0 and status_of(out, "entry_shadow_daily_presence") == "PASS", out
    assert "4 empty days allowed by flag" in out
    rc, out = run(lines, "--allow-empty-days", "3")
    assert rc == 1 and "entry_shadow_daily_presence" in failing(out)


def test_every_day_present_passes_and_names_the_count(stop_msgs):
    rc, out = run(clean_week(stop_msgs))
    assert status_of(out, "entry_shadow_daily_presence") == "PASS" and "(6 days)" in out


def test_the_first_and_last_partial_days_are_not_required_to_have_entry_lines(stop_msgs):
    lines = clean_week(stop_msgs, entry_days=[1, 2, 3, 4, 5, 6])  # none on day 0, none on day 7
    rc, out = run(lines)
    assert rc == 0, out


# --------------------------------------------------------------------------- B8 time handling


def _journald(ts_text, tick):
    return f"{ts_text} vps polyclawd-scheduler[1]: [live_stops] INFO: {tick}"


def test_journald_lines_without_a_year_warn_naming_the_year_and_where_it_came_from(stop_msgs):
    tick = stop_msgs(entries=ENTRIES)[0]
    lines = [pre(T0 - timedelta(days=1)) + record_msg("filtered", "price_band"),
             _journald("Nov 05 00:00:00", tick), _journald("Nov 05 00:05:00", tick)]
    rc, out = run(lines, "--min-days", "0")
    assert status_of(out, "year_inferred") == "WARN"
    assert "the year 2026 was used (the first line with a Python log prefix)" in out
    assert "from a journald prefix without a year: 2" in out and "Python log prefix: 1 lines" in out


def test_python_prefixed_input_has_no_year_warning(stop_msgs):
    rc, out = run1(one_day(stop_msgs))
    assert status_of(out, "year_inferred") == "PASS" and "from a journald prefix without a year: 0" in out


def test_a_year_boundary_in_journald_only_input_warns_time_steps_backwards(stop_msgs):
    tick = stop_msgs(entries=ENTRIES)[0]
    lines = [_journald("Dec 31 23:55:00", tick), _journald("Jan 01 00:05:00", tick)]
    rc, out = run(lines, "--min-days", "0")
    assert status_of(out, "time_steps_backwards") == "WARN"
    assert "1 times the timestamps, in input order, step back" in out


@pytest.mark.parametrize("hours,status", [(23, "PASS"), (25, "WARN")])
def test_backward_step_threshold_is_24_hours(stop_msgs, hours, status):
    tick = stop_msgs(entries=ENTRIES)[0]
    lines = [pre(T0 + timedelta(days=2), "live_stops") + tick,
             pre(T0 + timedelta(days=2) - timedelta(hours=hours), "live_stops") + tick]
    rc, out = run(lines, "--min-days", "0")
    assert status_of(out, "time_steps_backwards") == status


# --------------------------------------------------------------------------- archetype diagnostics
# `arch=` is not in the emitter yet (a later commit adds it). These lines are real emitter lines
# with ` arch=...` appended by hand; switch to the emitter once it produces the field.


def arch_line(ts, arch, verdict="would_route", **kw):
    return record_line(ts, verdict, **kw) + f" arch={arch}"


def test_archetype_mixed_ok_and_abstain_passes_with_the_counts(stop_msgs):
    t = T0 + timedelta(hours=5)
    rc, out = run1(one_day(stop_msgs, tail=[arch_line(t, "ok"), arch_line(t, "abstain")]))
    assert "would_route records by archetype check: ok 1, abstain 1" in out
    assert status_of(out, "archetype_gate_had_data") == "PASS" and rc == 0, out


def test_archetype_all_abstain_warns_and_does_not_change_the_exit_code(stop_msgs):
    t = T0 + timedelta(hours=5)
    lines = one_day(stop_msgs, would_route=False, tail=[arch_line(t, "abstain"), arch_line(t, "abstain")])
    lines.append(summary_line(t, 2, 2, {}))
    rc, out = run1(lines)
    assert "would_route records by archetype check: ok 0, abstain 2" in out
    assert status_of(out, "archetype_gate_had_data") == "WARN" and rc == 0, out
    assert "its table may be empty" in out


def test_archetype_field_absent_is_not_logged_and_neither_pass_nor_warn(stop_msgs):
    rc, out = run1(one_day(stop_msgs))
    assert "archetype_gate_had_data: not logged" in out
    assert "[PASS] archetype_gate_had_data" not in out and "[WARN] archetype_gate_had_data" not in out
    assert rc == 0


def test_archetype_logged_on_filtered_records_only_with_no_would_route_does_not_warn(stop_msgs):
    t = T0 + timedelta(hours=5)
    rc, out = run1(one_day(stop_msgs, tail=[record_line(t, "filtered", "price_band") + " arch=-"]))
    assert "[WARN] archetype_gate_had_data" not in out

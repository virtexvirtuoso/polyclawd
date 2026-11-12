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

    def run(entries=(), armed=False, fail=None, hold_lock=False):
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
            shadow.run_live_stop_tick("stop_evaluator_urgent", now=1_800_000_000.0,
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


def clean_week(stop_msgs, days=7, step_min=5, omit_reason=None, tail=()):
    """Journal lines for `days` days: stop DRYRUN every step_min minutes, plus one daily batch
    of entry-gate records (every required reason, one would_route) and its summary."""
    tick = stop_msgs(entries=ENTRIES)
    lines = []
    ts = T0
    end = T0 + timedelta(days=days)
    while ts <= end:
        lines += stop_lines(ts, tick)
        ts += timedelta(minutes=step_min)
    for d in range(days):
        t = T0 + timedelta(days=d, hours=12)
        filtered = {}
        for r in ALL_REASONS:
            if r == omit_reason:
                continue
            lines.append(record_line(t, "filtered", r, price=0.65 if r == "price_band" else 0.40))
            filtered[r] = 1
        lines.append(record_line(t, "would_route"))
        lines.append(summary_line(t, len(filtered) + 1, 1, filtered))
    lines += list(tail)
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
    tick = stop_msgs(entries=ENTRIES)
    skipped = stop_msgs(hold_lock=True)
    lines = []
    for m in (0, 5, 10, 15, 20, 32, 37):  # no DRYRUN between 00:20 and 00:32
        lines += stop_lines(T0 + timedelta(minutes=m), tick)
    lines += stop_lines(T0 + timedelta(minutes=26), skipped)  # a SKIPPED line inside the hole
    rc, out = run(lines, "--min-days", "0")
    assert "stop_gap" in checks_failing(out)
    assert "12.0 min" in out and "00:20:00" in out and "00:32:00" in out
    assert "SKIPPED lines: 1" in out


def test_stop_gap_of_9_minutes_passes(stop_msgs):
    tick = stop_msgs(entries=ENTRIES)
    lines = []
    for m in (0, 5, 14, 19):
        lines += stop_lines(T0 + timedelta(minutes=m), tick)
    rc, out = run(lines, "--min-days", "0")
    assert "stop_gap" not in checks_failing(out)
    assert "9.0 min" in out


def test_max_gap_minutes_option(stop_msgs):
    tick = stop_msgs(entries=ENTRIES)
    lines = stop_lines(T0, tick) + stop_lines(T0 + timedelta(minutes=9), tick)
    rc, out = run(lines, "--min-days", "0", "--max-gap-minutes", "5")
    assert "stop_gap" in checks_failing(out)


def test_no_dryrun_line_at_all_fails_the_stop_gap():
    rc, out = run([record_line(T0, "filtered", "price_band")], "--min-days", "0")
    assert "stop_gap" in checks_failing(out)


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
    lines.insert(0, pre(T0 - timedelta(days=1)) + record_msg("filtered", "price_band"))
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
    lines = stop_lines(T0, stop_msgs(entries=ENTRIES)) + [
        pre(T0) + "sw_live BANANA records=1",
        pre(T0, "live_stops") + "live_stops could not write state file /x",
        pre(T0) + "sw_live: clob setup failed: stop here",  # not an emitted line kind: ignored
    ]
    rc, out = run(lines, "--min-days", "0")
    assert "unrecognised sw_live/live_stops line kinds: 2" in out
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
    assert imported <= {"__future__", "argparse", "re", "sys", "collections", "datetime"}, imported

"""scripts/export_shadow_lines.sh appends the evidence lines of the scheduler's journal to a file that
outlives the journal. These tests run the real script with a FAKE `journalctl` first on PATH (through
the script's own PATH-prefix seam): it records its arguments, keeps a cursor file the way journalctl
does (the number of lines already delivered) and prints fixture lines. The real journal is never read.
"""

import fcntl
import os
import re
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from scripts import shadow_evidence_report as rpt
# the evidence lines are built from the report tests' real-emitter helpers
from test_shadow_evidence_report import (  # noqa: F401  (fixtures are used by name)
    ENTRIES, T0, hook_text, jd_prefix, record_line, stop_lines, stop_msgs, summary_line)

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "export_shadow_lines.sh"

FAKE_JOURNALCTL = r"""#!/bin/bash
# fake journalctl: records its arguments; the cursor file holds how many fixture lines were delivered
echo "$*" >> "$FAKE_ARGS_LOG"
cursor=""
for a in "$@"; do
    case "$a" in --cursor-file=*) cursor="${a#--cursor-file=}" ;; esac
done
if [ -n "${FAKE_FAIL:-}" ]; then
    [ -n "$cursor" ] && echo 9999 > "$cursor"
    echo "journalctl: boom" >&2
    exit 1
fi
done_n=0
[ -n "$cursor" ] && [ -s "$cursor" ] && done_n="$(cat "$cursor")"
total="$(wc -l < "$FAKE_FIXTURE")"
if [ "$total" -le "$done_n" ]; then
    echo "-- No entries --"
else
    tail -n +"$((done_n + 1))" "$FAKE_FIXTURE"
    [ -n "$cursor" ] && echo "$total" > "$cursor"
fi
exit 0
"""


@pytest.fixture
def env(tmp_path):
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    fake = bindir / "journalctl"
    fake.write_text(FAKE_JOURNALCTL)
    fake.chmod(0o755)
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("")
    args_log = tmp_path / "args.log"
    out_dir = tmp_path / "out"
    e = dict(os.environ, SHADOW_EXPORT_PATH_PREFIX=str(bindir), FAKE_FIXTURE=str(fixture),
             FAKE_ARGS_LOG=str(args_log))
    e.pop("FAKE_FAIL", None)
    return {"env": e, "fixture": fixture, "args_log": args_log, "out": out_dir}


def set_fixture(env, lines):
    env["fixture"].write_text("".join(ln + "\n" for ln in lines))


def run_script(env, *args, script=SCRIPT, extra_env=None, cwd=None):
    e = dict(env["env"], **(extra_env or {}))
    return subprocess.run(["bash", str(script), "--out-dir", str(env["out"]), *args], env=e, cwd=cwd,
                          capture_output=True, text=True, timeout=60)


def calls(env):
    return env["args_log"].read_text().splitlines() if env["args_log"].exists() else []


def log_lines(env):
    path = env["out"] / "shadow-lines.log"
    return path.read_text().splitlines() if path.exists() else []


def prefixed(n, msg, name="polyclawd-scheduler"):
    return jd_prefix(T0 + timedelta(minutes=n), unit=name) + msg


NOISE = [prefixed(0, "[cross_sport_drift] UFC: no games found"),
         prefixed(1, "Traceback (most recent call last):")]
EVIDENCE = [prefixed(2, "2026-11-05 00:02:00,000 [live_stops] INFO: live_stops DRYRUN mode=PAPER armed=false "
                        "source=stop_evaluator checked=0 ok=0 would_exit=[] spread_held=[] unpriced=[] exempt=[] errors=[]"),
            prefixed(3, "2026-11-05 00:03:00,000 [x] INFO: sw_live DRYRUN mode=PAPER armed=false records=0 "
                        "would_route=0 filtered=-"),
            prefixed(4, "SMART_WALLET_ALERT scanner_hook FAILED:")]


# --------------------------------------------------------------------------- arguments and cursor


def test_first_run_passes_since_and_a_cursor_file_later_runs_do_not_pass_since(env):
    set_fixture(env, NOISE + EVIDENCE)
    assert run_script(env).returncode == 0
    first = calls(env)[0]
    assert "-u polyclawd-scheduler" in first and "--no-pager" in first and "-o short-iso" in first
    assert "--since -48h" in first
    m = re.search(r"--cursor-file=(\S+)", first)
    assert m and m.group(1) != str(env["out"] / ".cursor")           # journalctl works on a temporary copy
    assert Path(m.group(1)).parent == env["out"]
    set_fixture(env, NOISE + EVIDENCE + EVIDENCE)
    assert run_script(env).returncode == 0
    second = calls(env)[1]
    assert "--since" not in second and "--cursor-file=" in second


def test_unit_and_first_since_options(env):
    set_fixture(env, EVIDENCE)
    assert run_script(env, "--unit", "other-unit", "--first-since", "-2h").returncode == 0
    assert "-u other-unit" in calls(env)[0] and "--since -2h" in calls(env)[0]


def test_unknown_option_is_a_usage_error(env):
    r = run_script(env, "--bogus")
    assert r.returncode == 2 and "usage" in r.stderr


def test_the_real_cursor_advances_only_on_success_and_no_temporary_file_is_left(env):
    set_fixture(env, NOISE + EVIDENCE)
    assert run_script(env).returncode == 0
    assert (env["out"] / ".cursor").read_text().strip() == "5"
    assert sorted(p.name for p in env["out"].iterdir()) == [".cursor", ".lock", "shadow-lines.log"]


def test_a_failing_journalctl_leaves_cursor_and_log_untouched_and_exits_non_zero(env):
    set_fixture(env, NOISE + EVIDENCE)
    assert run_script(env).returncode == 0
    before_log = (env["out"] / "shadow-lines.log").read_text()
    set_fixture(env, NOISE + EVIDENCE + EVIDENCE)
    r = run_script(env, extra_env={"FAKE_FAIL": "1"})
    assert r.returncode != 0 and "journalctl failed" in r.stderr
    assert (env["out"] / ".cursor").read_text().strip() == "5"      # the temp copy was set to 9999, not this
    assert (env["out"] / "shadow-lines.log").read_text() == before_log
    assert sorted(p.name for p in env["out"].iterdir()) == [".cursor", ".lock", "shadow-lines.log"]


def test_the_cursor_stays_when_the_append_fails(env):
    """The cursor must not move past lines that were not written."""
    set_fixture(env, NOISE + EVIDENCE)
    assert run_script(env).returncode == 0
    log = env["out"] / "shadow-lines.log"
    log.unlink()
    log.mkdir()                                                      # appending to a directory fails
    set_fixture(env, NOISE + EVIDENCE + EVIDENCE)
    r = run_script(env)
    assert r.returncode != 0 and "cannot append" in r.stderr
    assert (env["out"] / ".cursor").read_text().strip() == "5"


def test_a_failed_first_run_leaves_no_cursor_file(env):
    set_fixture(env, EVIDENCE)
    assert run_script(env, extra_env={"FAKE_FAIL": "1"}).returncode != 0
    assert not (env["out"] / ".cursor").exists() and log_lines(env) == []


# --------------------------------------------------------------------------- what is kept


def test_only_evidence_lines_are_appended_with_ansi_stripped_and_order_kept(env):
    colour = "\x1b[32m"
    reset = "\x1b[0m"
    coloured = prefixed(5, f"{colour}2026-11-05 00:05:00,000 [live_stops] INFO: live_stops SKIPPED reason=lock "
                           f"source=stop_evaluator{reset}")
    set_fixture(env, [NOISE[0], EVIDENCE[0], NOISE[1], coloured, EVIDENCE[1], "-- No entries --", EVIDENCE[2]])
    r = run_script(env)
    assert r.returncode == 0
    plain = coloured.replace(colour, "").replace(reset, "")
    assert log_lines(env) == [EVIDENCE[0], plain, EVIDENCE[1], EVIDENCE[2]]
    assert r.stdout.strip() == (f"export_shadow_lines: appended 4 lines (6 read) to "
                                f"{env['out'] / 'shadow-lines.log'}")


def test_a_second_run_appends_only_the_new_lines(env):
    set_fixture(env, NOISE + EVIDENCE[:2])
    assert run_script(env).returncode == 0
    assert log_lines(env) == EVIDENCE[:2]
    set_fixture(env, NOISE + EVIDENCE[:2] + [NOISE[0], EVIDENCE[2]])
    r = run_script(env)
    assert r.returncode == 0 and "appended 1 lines (2 read)" in r.stdout
    assert log_lines(env) == EVIDENCE
    r = run_script(env)                                              # nothing new: nothing appended
    assert r.returncode == 0 and "appended 0 lines (0 read)" in r.stdout
    assert log_lines(env) == EVIDENCE


def test_two_overlapping_runs_the_second_exits_zero_and_appends_nothing(env):
    set_fixture(env, EVIDENCE)
    env["out"].mkdir()
    with open(env["out"] / ".lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = run_script(env)
    assert r.returncode == 0 and "another run" in r.stdout
    assert log_lines(env) == [] and calls(env) == []


def test_the_default_out_dir_is_under_the_repository_root_of_the_script(env, tmp_path):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / "export_shadow_lines.sh")
    set_fixture(env, EVIDENCE)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    r = subprocess.run(["bash", str(root / "scripts" / "export_shadow_lines.sh")], env=env["env"],
                       cwd=elsewhere, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert (root / "logs" / "shadow_evidence" / "shadow-lines.log").read_text().splitlines() == EVIDENCE
    assert list(elsewhere.iterdir()) == []


# --------------------------------------------------------------------------- the report reads the file


def test_the_appended_file_parses_in_the_report_with_nothing_malformed_or_untimed(env, stop_msgs, hook_text):
    ticks = stop_msgs(entries=ENTRIES, source="stop_evaluator")
    skipped = stop_msgs(hold_lock=True, source="stop_evaluator_urgent")
    lines = [prefixed(0, "unrelated line")]
    lines += [jd_prefix(T0) + ln for ln in stop_lines(T0, ticks)]
    lines += [jd_prefix(T0) + ln for ln in stop_lines(T0 + timedelta(minutes=1), skipped)]
    lines += [jd_prefix(T0) + record_line(T0 + timedelta(minutes=2), "would_route")]
    lines += [jd_prefix(T0) + record_line(T0 + timedelta(minutes=2), "filtered", "price_band", price=0.65)]
    lines += [jd_prefix(T0) + summary_line(T0 + timedelta(minutes=3), 2, 1, {"price_band": 1})]
    lines += [jd_prefix(T0 + timedelta(minutes=4)) + hook_text]
    set_fixture(env, lines)
    assert run_script(env).returncode == 0
    got = log_lines(env)
    assert len(got) == len(lines) - 1
    parsed = rpt.parse_lines((env["out"] / "shadow-lines.log").read_text().splitlines(True))
    assert parsed["malformed"] == [] and parsed["no_time"] == [] and parsed["hook_untimed"] == []
    assert len(parsed["hook_failures"]) == 1 and len(parsed["stop_skipped"]) == 1
    assert len(parsed["stop_dry"]) == 1 and len(parsed["records"]) == 2 and len(parsed["dryruns"]) == 1
    assert parsed["unrecognised"] == []


# --------------------------------------------------------------------------- static properties


def test_the_script_is_valid_bash_and_shellcheck_clean_when_available():
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode == 0
    if shutil.which("shellcheck"):
        r = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout


def test_the_script_is_safe_by_construction():
    text = SCRIPT.read_text()
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert "set -u" in code and "set -o pipefail" in code and "flock -n 9" in code
    assert "umask" not in code
    assert "PATH=\"${SHADOW_EXPORT_PATH_PREFIX:+$SHADOW_EXPORT_PATH_PREFIX:}/usr/local/bin:/usr/bin:/bin\"" in code
    for forbidden in ("sudo", "curl", "wget", "ssh ", "/etc/", ".env", "systemctl"):
        assert forbidden not in code, forbidden
    assert code.count("live_stops ") == 1                           # the evidence pattern is defined once
    assert os.access(SCRIPT, os.X_OK)


def test_the_hook_failure_text_in_the_pattern_is_the_report_s_and_the_source_s():
    pattern = re.search(r"^EVIDENCE_PATTERN='([^']+)'", SCRIPT.read_text(), re.M).group(1)
    assert rpt.HOOK_FAILED.pattern in pattern
    assert "SMART_WALLET_ALERT scanner_hook FAILED" in (REPO / "scripts" / "smart_wallet_alert.py").read_text()


def test_logs_is_git_ignored_and_no_machine_specific_strings_in_the_script():
    assert "logs/" in (REPO / ".gitignore").read_text().splitlines()
    text = SCRIPT.read_text()
    assert [s for s in ("linux" + "user", "/var/" + "www") if s in text] == []

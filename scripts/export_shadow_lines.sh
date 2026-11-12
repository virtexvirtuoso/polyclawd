#!/bin/bash
# Append the shadow-week evidence lines of the scheduler's journal to a file that outlives the
# journal (which keeps about two days; the evidence report needs seven).
#
#   scripts/export_shadow_lines.sh [--unit <unit>] [--out-dir <dir>] [--first-since <value>]
#
# Defaults: unit polyclawd-scheduler; out dir <repository root>/logs/shadow_evidence (the root is
# found from this file's own location; logs/ is git-ignored); --first-since -48h (used only on the
# first run, when there is no cursor file). Each run continues exactly after the last journal
# entry the previous run read: nothing is repeated and nothing is skipped.
#
#   python3 scripts/shadow_evidence_report.py <out-dir>/shadow-lines.log
#
# Read-only on the journal (the service user is in the systemd-journal group: no sudo), no network,
# nothing written outside the out dir. A hold on <out-dir>/.lock stops two runs overlapping (the
# second says so and exits 0). The real cursor moves only after the lines are appended: journalctl
# works on a temporary copy of it, and on any failure the real cursor is left as it was.
# SHADOW_EXPORT_PATH_PREFIX is a test seam: directories put in front of the fixed PATH.

PATH="${SHADOW_EXPORT_PATH_PREFIX:+$SHADOW_EXPORT_PATH_PREFIX:}/usr/local/bin:/usr/bin:/bin"
export PATH
set -u
set -o pipefail

# The evidence kinds, defined once: the stop shadow (`live_stops ...`), the entry-gate shadow
# (`sw_live ...`) and a sweep that failed before the router ran (scripts/smart_wallet_alert.py
# scanner_hook prints this text to stderr). Matched after the colour codes are stripped.
EVIDENCE_PATTERN='live_stops |sw_live|SMART_WALLET_ALERT scanner_hook FAILED'

ROOT="$(dirname "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")")"
UNIT="polyclawd-scheduler"
OUT_DIR="$ROOT/logs/shadow_evidence"
FIRST_SINCE="-48h"

die() {
    echo "export_shadow_lines: $*" >&2
    exit 1
}

usage() {
    echo "usage: export_shadow_lines.sh [--unit <unit>] [--out-dir <dir>] [--first-since <value>]" >&2
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --unit) [ $# -ge 2 ] || usage; UNIT="$2"; shift 2 ;;
        --out-dir) [ $# -ge 2 ] || usage; OUT_DIR="$2"; shift 2 ;;
        --first-since) [ $# -ge 2 ] || usage; FIRST_SINCE="$2"; shift 2 ;;
        *) usage ;;
    esac
done

mkdir -p "$OUT_DIR" || die "cannot create $OUT_DIR"

LOG="$OUT_DIR/shadow-lines.log"
CURSOR="$OUT_DIR/.cursor"

exec 9>"$OUT_DIR/.lock" || die "cannot open $OUT_DIR/.lock"
if ! flock -n 9; then
    echo "export_shadow_lines: another run holds $OUT_DIR/.lock; nothing done"
    exit 0
fi

JOURNAL="$(mktemp "$OUT_DIR/.journal.XXXXXX")" || die "cannot create a temporary file in $OUT_DIR"
FILTERED="$(mktemp "$OUT_DIR/.filtered.XXXXXX")" || die "cannot create a temporary file in $OUT_DIR"
NEW_CURSOR="$(mktemp "$OUT_DIR/.cursor.new.XXXXXX")" || die "cannot create a temporary file in $OUT_DIR"
cleanup() {
    rm -f "$JOURNAL" "$FILTERED" "$NEW_CURSOR"
}
trap cleanup EXIT

# journalctl works on a copy of the cursor; the real one moves only at the very end.
SINCE_ARGS=()
if [ -s "$CURSOR" ]; then
    cp "$CURSOR" "$NEW_CURSOR" || die "cannot copy the cursor file"
else
    SINCE_ARGS=(--since "$FIRST_SINCE")
fi

journalctl -u "$UNIT" --no-pager -o short-iso --cursor-file="$NEW_CURSOR" ${SINCE_ARGS[@]+"${SINCE_ARGS[@]}"} \
    > "$JOURNAL" || die "journalctl failed for unit $UNIT; the cursor and $LOG are untouched"

READ="$(grep -vc '^-- ' "$JOURNAL")"
[ "$READ" -gt 0 ] && [ ! -s "$NEW_CURSOR" ] &&
    die "journalctl read $READ lines but left no cursor; the cursor and $LOG are untouched"

ESC="$(printf '\033')"
sed "s/${ESC}\[[0-9;]*[A-Za-z]//g" "$JOURNAL" | grep -E -- "$EVIDENCE_PATTERN" > "$FILTERED"
[ $? -le 1 ] || die "filtering failed; the cursor and $LOG are untouched"
APPENDED="$(wc -l < "$FILTERED")"

BEFORE="$(stat -c %s "$LOG" 2> /dev/null || echo 0)"
cat "$FILTERED" >> "$LOG" || {
    truncate -s "$BEFORE" "$LOG" 2> /dev/null
    die "cannot append to $LOG; the cursor is untouched"
}
if [ -s "$NEW_CURSOR" ]; then
    mv -f "$NEW_CURSOR" "$CURSOR" || die "lines were appended but the cursor could not be moved: the next run repeats them"
fi

echo "export_shadow_lines: appended $((APPENDED + 0)) lines ($READ read) to $LOG"

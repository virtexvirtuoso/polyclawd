#!/usr/bin/env python3
"""capture_health.py — dead-man for the Polymarket order-book capture (polyclawd-capture).

Cron on the VPS every 5 min (linuxuser crontab, SENTINEL_TOPIC=data). It reads BOTH the
capture's own status JSON (127.0.0.1:8424/status) AND the artefact the capture exists to
produce (mtime of the newest storage/book_capture/changes/*/*.parquet*), so a process that
is up but wedged, or a status server that answers while the writer is dead, is caught
either way. It also watches the systemd unit for a restart loop.

Alerts on TRANSITIONS only (a new problem set, or recovered) and repeats every 6 h while a
problem persists, via ~/bin/infra-notify (WARN level, Telegram only). Counter events
(rows dropped, flush errors, corrupt files) page when they grow but never produce a
"recovered" message of their own.

Checks (default; env override):
  unit          polyclawd-capture ActiveState != active                     CAPTURE_UNIT
  restart-loop  >= 6 automatic restarts in the last hour
  status        GET /status fails or is not JSON                            CAPTURE_STATUS_URL
  data          last_data_age_s > 600 s, or no data frame 600 s after start  CAPTURE_DATA_STALE_S
                (the capture's own silence watchdog reconnects at 300 s, so 600 s means two
                of its cycles failed to restore the feed; a bare connected=false is NOT
                alarmed -- reconnects normally take seconds and data age covers the rest)
  universe      universe_size == 0 after 600 s (no watchset ever read)
  flush         flush_stale true (rows buffered, no flush for 3x flush interval)
  gap           gap_open true on two consecutive checks (>= 300 s)           CAPTURE_GAP_MAX_S
  artefact      newest changes/*/*.parquet* older than 600 s                 CAPTURE_DATA_ROOT
  event:<ctr>   rows_dropped_overflow / rows_dropped_flush_error / flush_errors /
                files_corrupt / quarantined grew since the last check (rebased on restart)

Silence: `touch ~/.cache/capture-health.mute` suppresses pages for 24 h from the touch
(auto-expires, so a planned stop cannot leave the dead-man muted forever).

Testing (both directions, per the fleet monitors-lie rule): DRY_RUN=1 prints instead of
sending; CAPTURE_STATUS_FILE=<json> replaces the HTTP fetch with a fixture; CAPTURE_STATE_FILE,
CAPTURE_NOTIFY, CAPTURE_DATA_ROOT, CAPTURE_UNIT isolate a test from the live state. In
DRY_RUN the state file is written only when CAPTURE_STATE_FILE is set explicitly.
One log line per run (liveness). Exit 0 = ran and delivered what it had to; 1 = undelivered.

Plan:  vault 02-Projects/Polyclawd/Plans/2026-10-01-book-capture-writer-plan.md
Tasks: vault 02-Projects/Polyclawd/Tasks.md (Order-Book Capture, dead-man item, 2026-10-02)
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
UNIT = os.environ.get("CAPTURE_UNIT", "polyclawd-capture")
STATUS_URL = os.environ.get("CAPTURE_STATUS_URL", "http://127.0.0.1:8424/status")
STATUS_FILE = os.environ.get("CAPTURE_STATUS_FILE")
DATA_ROOT = Path(os.environ.get("CAPTURE_DATA_ROOT", str(HERE / "storage" / "book_capture")))
STATE = Path(os.environ.get("CAPTURE_STATE_FILE", str(Path.home() / ".cache" / "capture-health.json")))
MUTE = Path(os.environ.get("CAPTURE_MUTE_FILE", str(Path.home() / ".cache" / "capture-health.mute")))
NOTIFY = os.environ.get("CAPTURE_NOTIFY", str(Path.home() / "bin" / "infra-notify"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
DATA_STALE_S = float(os.environ.get("CAPTURE_DATA_STALE_S", "600"))
GAP_MAX_S = float(os.environ.get("CAPTURE_GAP_MAX_S", "300"))
LOOP_RESTARTS_PER_H = 6
REPEAT_S = 6 * 3600
MUTE_S = 24 * 3600
COUNTERS = ("rows_dropped_overflow", "rows_dropped_flush_error", "flush_errors",
            "files_corrupt", "quarantined")
HOST = os.uname().nodename


def ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def fmt_age(s):
    s = float(s)
    if s >= 7200:
        return f"{s / 3600:.1f} h"
    if s >= 120:
        return f"{int(s // 60)} min"
    return f"{int(s)} s"


def unit_state():
    try:
        r = subprocess.run(["systemctl", "show", UNIT, "-p", "ActiveState", "-p", "NRestarts"],
                           capture_output=True, text=True, timeout=15)
        kv = dict(line.split("=", 1) for line in r.stdout.strip().splitlines() if "=" in line)
        return kv.get("ActiveState", "unknown"), int(kv.get("NRestarts", "0") or 0)
    except Exception as e:  # a missing/hung systemctl must not hide the other checks
        return f"unknown ({type(e).__name__})", 0


def fetch_status():
    """-> (status dict, None) or (None, error text)."""
    try:
        if STATUS_FILE:
            return json.loads(Path(STATUS_FILE).read_text()), None
        with urllib.request.urlopen(STATUS_URL, timeout=10) as r:
            return json.loads(r.read().decode()), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"[:120]


def newest_artefact_age(now):
    """Age of the newest changes file (finished or in-progress); None if there is none."""
    mtimes = []
    for f in (DATA_ROOT / "changes").glob("*/*.parquet*"):
        try:
            mtimes.append(f.stat().st_mtime)
        except FileNotFoundError:  # .inprogress renamed to .parquet between glob and stat
            pass
    return (now - max(mtimes)) if mtimes else None


def muted(now):
    try:
        return now - MUTE.stat().st_mtime < MUTE_S
    except FileNotFoundError:
        return False


def main():
    now = time.time()
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    problems = {}

    active, restarts = unit_state()
    if active != "active":
        problems["unit"] = f"{UNIT} is {active}"
    samples = [s for s in st.get("restarts", []) if now - s[0] <= 3600] + [[now, restarts]]
    loop = restarts - min(r for _, r in samples)  # automatic restarts within the last hour
    if loop >= LOOP_RESTARTS_PER_H:
        problems["restart-loop"] = f"{loop} automatic restarts in the last hour"

    status, err = fetch_status()
    counters = st.get("counters", {})
    gap_since = st.get("gap_open_since")
    data_age = None
    if status is None:
        problems["status"] = f"status endpoint not answering ({err})"
        gap_since = None
    else:
        w = status.get("writer") or {}
        uptime = float(status.get("uptime_s") or 0)
        data_age = status.get("last_data_age_s")
        conn = f"connected={status.get('connected')}, reconnects={status.get('reconnects')}"
        if data_age is None:
            if uptime > DATA_STALE_S:
                problems["data"] = f"no data frame {fmt_age(uptime)} after start ({conn})"
        elif float(data_age) > DATA_STALE_S:
            problems["data"] = f"no data frame for {fmt_age(data_age)} ({conn})"
        if not status.get("universe_size") and uptime > DATA_STALE_S:
            problems["universe"] = "universe empty: no watchset read (poly_ws / memcached?)"
        if status.get("flush_stale"):
            problems["flush"] = (f"writer flush stale (rows_buffered={w.get('rows_buffered')}, "
                                 f"last_flush_secs={w.get('last_flush_secs')})")
        if status.get("gap_open"):
            gap_since = gap_since or now
            if now - gap_since >= GAP_MAX_S:
                problems["gap"] = (f"subscription gap open for {fmt_age(now - gap_since)} "
                                   f"(gap_count={status.get('gap_count')}, {conn})")
        else:
            gap_since = None
        prev_up = counters.get("uptime_s")
        restarted = prev_up is not None and uptime < float(prev_up)
        for k in COUNTERS:
            cur = int(w.get(k) or 0)
            prev_v = counters.get(k)
            if prev_v is not None and not restarted and cur > int(prev_v):
                problems[f"event:{k}"] = f"{k} grew by {cur - int(prev_v)} since last check (now {cur})"
        counters = {k: int(w.get(k) or 0) for k in COUNTERS}
        counters["uptime_s"] = uptime

    art_age = newest_artefact_age(now)
    if art_age is None:
        problems["artefact"] = f"no changes files under {DATA_ROOT}"
    elif art_age > DATA_STALE_S:
        problems["artefact"] = f"newest changes file is {fmt_age(art_age)} old (writer not producing)"

    prev = st.get("problems", {})
    state_now = {k for k in problems if not k.startswith("event:")}
    state_prev = {k for k in prev if not k.startswith("event:")}
    new_events = {k for k in problems if k.startswith("event:")} - set(prev)
    last_alert = st.get("last_alert", 0)
    msg = None
    if problems and (state_now != state_prev or new_events or now - last_alert >= REPEAT_S):
        lines = "\n".join(f"• {k}: {v}" for k, v in sorted(problems.items()))
        cleared = sorted(state_prev - state_now)
        msg = (f"🚨 Book capture PROBLEM x{len(problems)} — {UNIT} on {HOST}\n{lines}"
               + (f"\ncleared since last page: {', '.join(cleared)}" if cleared else "")
               + "\n(capture-health, every 5 min; repeats 6h; "
                 "touch ~/.cache/capture-health.mute = quiet 24h)")
    elif not problems and state_prev:
        msg = (f"✅ Book capture recovered — {UNIT} on {HOST}: status answering, data fresh, "
               f"writer flushing, changes files advancing (was: {', '.join(sorted(state_prev))})")

    delivered = True
    sent = False
    if msg and muted(now):
        print(f"{ts()} MUTED ({MUTE}); would have sent: {msg.splitlines()[0]}")
    elif msg and DRY_RUN:
        print(f"{ts()} DRY-RUN would send:\n{msg}")
        sent = True  # so a dry-run sequence exercises the same dedup/repeat path as a live one
    elif msg:
        env = {**os.environ, "SENTINEL_TOPIC": os.environ.get("SENTINEL_TOPIC", "data")}
        delivered = subprocess.run([NOTIFY, msg], env=env).returncode == 0
        sent = delivered

    print(f"{ts()} problems={sorted(problems) or 'none'} unit={active} restarts={restarts} "
          f"data_age={data_age} art_age={None if art_age is None else int(art_age)}s "
          f"alert={'yes' if msg else 'no'} delivered={delivered}")

    if not DRY_RUN or "CAPTURE_STATE_FILE" in os.environ:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({
            "checked_at": now, "problems": problems, "restarts": samples,
            "counters": counters, "gap_open_since": gap_since,
            "last_alert": now if (sent and problems) else last_alert,
        }))
    return 0 if delivered else 1


if __name__ == "__main__":
    sys.exit(main())

"""Live stop tick: run `evaluate_live_stops` once per scheduler tick and log it.

Both stop tasks in services/scheduler.py call `run_live_stop_tick`. It runs the
evaluator as a dry run unless the bot is armed, logs one summary line (the "stop
shadow"), keeps the alarm for positions the stop cannot price, and pages for
positions it could not evaluate. Nothing here sells: when armed, the evaluator
calls the exit itself.

Standard library only at module level (the scheduler is at its memory limit);
everything else is imported inside the functions.
"""

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger("live_stops")

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_PATH = REPO_ROOT / "storage" / "live_stop_state.json"

# Choices, not measured optima (plan task 1.2).
UNPRICED_PAGE_AFTER_S = 30 * 60     # page a position with no two-sided price after 30 minutes
UNPRICED_COOLDOWN_S = 6 * 3600      # then at most once per 6 hours per position
IMMEDIATE_COOLDOWN_S = 3600         # bad_row / error / exit_failed / exit_unknown / tick failed: once an hour
ERROR_TEXT_MAX = 200                # characters of an error message put in a page
# Page a failed evaluation on the third failure in a row, not the first: the production
# databases already produce transient "database is locked" errors. A choice, not measured.
TICK_FAILED_PAGE_AFTER = 3

# Every action the evaluator can return, except an exit's own result. An entry whose
# action is not in this set came back from the real exit and counts as "exited".
_KNOWN_ACTIONS = {"exempt", "bad_row", "unpriced", "ok", "spread_held", "would_exit",
                  "exit_failed", "exit_unknown", "error"}
_ERROR_ACTIONS = {"bad_row", "error", "exit_failed", "exit_unknown"}

# One stop pass at a time: the 5-minute and 1-minute tasks run in different threads.
_TICK_LOCK = threading.Lock()
# In memory: a scheduler restart (about every 8 hours) may page again. Accepted.
_PAGE_STATE: dict = {}
# Pages are sent after the tick lock is released, so two threads can reach page_once at
# once. page_once reads then writes `_PAGE_STATE` and is not atomic, so sends take this
# lock: one page at a time, and a page is never sent twice for one key by a race.
_PAGE_LOCK = threading.Lock()
# Consecutive ticks whose evaluation raised; only touched while holding _TICK_LOCK.
_CONSECUTIVE_FAILURES = 0
# The "unpriced since" map is kept in memory (source of truth inside a process) and
# loaded from the state file once, on first use. None = not loaded yet.
_UNPRICED_SINCE = None
_STATE_DIRTY = False         # the map has changes the file does not have yet
_STATE_WRITE_FAILED = False  # the last write failed (and was logged); log again only after a success


def _ids(entries, *actions):
    out = set()
    for e in entries:
        if e.get("action") in actions:
            try:
                out.add(int(e.get("id")))
            except (TypeError, ValueError):
                continue
    return "[" + ",".join(str(i) for i in sorted(out)) + "]"


def reset_state_for_tests():
    """Forget the in-memory unpriced map, the failure count and the write-failure flag
    (a fresh process). Test hook."""
    global _UNPRICED_SINCE, _STATE_DIRTY, _STATE_WRITE_FAILED, _CONSECUTIVE_FAILURES
    _UNPRICED_SINCE = None
    _STATE_DIRTY = False
    _STATE_WRITE_FAILED = False
    _CONSECUTIVE_FAILURES = 0


def _send_pages(pending, now):
    """Send the collected (key, cooldown_s, text) pages through page_once, one at a time.
    Called with the tick lock released. A page that raises is logged and the rest are
    still tried. Never raises."""
    for key, cooldown_s, text in pending:
        try:
            from scripts.openclaw_alerts import page_once
            with _PAGE_LOCK:
                page_once(key, cooldown_s, text, state=_PAGE_STATE, now=now, parse_mode=None)
        except Exception as exc:
            logger.error("live_stops page %s failed: %s: %s", key, type(exc).__name__, exc)


# --- the unpriced alarm state file -------------------------------------------------

def _load_state(path):
    """Returns (map of position id -> first-unpriced epoch, was_corrupt)."""
    try:
        raw = Path(path).read_text()
    except FileNotFoundError:
        return {}, False
    except OSError as exc:
        logger.warning("live_stops state file %s unreadable (%s); treating as empty", path, exc)
        return {}, True
    try:
        data = json.loads(raw)
        since = data["unpriced_since"]
        if not isinstance(since, dict):
            raise ValueError("unpriced_since is not an object")
        return {str(k): float(v) for k, v in since.items()}, False
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("live_stops state file %s is corrupt (%s); treating as empty", path, exc)
        return {}, True


def _save_state(path, since):
    """Atomic: temp file in the same directory, then os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump({"unpriced_since": since}, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _update_unpriced(entries, state_path, now):
    """Maintain first-seen times and return the map. The in-memory map is the truth;
    the file is loaded once and written best-effort when the map changed (or an earlier
    write failed), so an unwritable file cannot restart the 30-minute clock."""
    global _UNPRICED_SINCE, _STATE_DIRTY, _STATE_WRITE_FAILED
    if _UNPRICED_SINCE is None:
        _UNPRICED_SINCE, _STATE_DIRTY = _load_state(state_path)  # a corrupt file is rewritten
    since = _UNPRICED_SINCE
    current = {str(e["id"]) for e in entries
               if e.get("action") == "unpriced" and e.get("id") is not None}
    # A position that is no longer "unpriced" is dropped, whatever it became (priced,
    # error, exempt, closed). So one that flickers unpriced <-> error never alarms
    # through the unpriced path (each unpriced spell restarts the clock), but it does
    # page through the error path.
    new = {k: v for k, v in since.items() if k in current}
    for k in current:
        new.setdefault(k, now)
    if new != since:
        _STATE_DIRTY = True
    _UNPRICED_SINCE = new
    if _STATE_DIRTY:
        try:
            _save_state(state_path, new)
            _STATE_DIRTY = False
            _STATE_WRITE_FAILED = False
        except Exception as exc:
            if not _STATE_WRITE_FAILED:
                logger.error("live_stops could not write state file %s: %s: %s",
                             state_path, type(exc).__name__, exc)
            _STATE_WRITE_FAILED = True
    return new


def _page_unpriced(since, now, shadow, pending):
    for pid, first in since.items():
        if now - first >= UNPRICED_PAGE_AFTER_S:
            minutes = int((now - first) // 60)
            text = (f"Polyclawd live stop: position {pid} has had no usable two-sided price "
                    f"for {minutes} minutes. The stop cannot evaluate it.")
            pending.append((f"live-stop-unpriced-{pid}", UNPRICED_COOLDOWN_S,
                            text + (" (shadow)" if shadow else "")))


def _page_errors(entries, shadow, pending):
    tag = " (shadow)" if shadow else ""
    for e in entries:
        action, pid = e.get("action"), e.get("id")
        if action not in _ERROR_ACTIONS:
            continue
        err = str(e.get("error", ""))[:ERROR_TEXT_MAX]
        if action == "exit_unknown":
            text = (f"Polyclawd live stop: a sale of position {pid} was attempted and its outcome "
                    f"is UNKNOWN ({err}). Check the exchange and the ledger now.")
        elif action == "exit_failed":
            text = (f"Polyclawd live stop: the exit of position {pid} did not confirm a sale. "
                    "Check the exchange and the ledger.")
        elif action == "error":
            text = (f"Polyclawd live stop: position {pid} could not be evaluated ({err}); "
                    f"no sale was attempted.{tag}")
        else:
            text = (f"Polyclawd live stop: position {pid} is {action} and the stop could not "
                    f"evaluate it.{tag}")
        pending.append((f"live-stop-{action}-{pid}", IMMEDIATE_COOLDOWN_S, text))


# --- the tick ----------------------------------------------------------------------

def run_live_stop_tick(source: str, *, now: float | None = None, state_path=None) -> dict:
    """One shadow-or-armed stop pass. Never raises. Returns {"skipped": True} when
    another pass holds the lock, {"failed": True} when the evaluation raised, else
    {"checked": n}. Pages are only collected under the lock and sent after it is
    released, so a slow or dead alert transport cannot hold up the other stop task."""
    global _CONSECUTIVE_FAILURES
    if not _TICK_LOCK.acquire(blocking=False):
        logger.info("live_stops SKIPPED reason=lock source=%s", source)
        return {"skipped": True}
    pending = []     # (key, cooldown_s, text) pages to send once the lock is free
    try:
        now = time.time() if now is None else now
        path = DEFAULT_STATE_PATH if state_path is None else state_path
        try:
            from execution import live_config
            from services.stop_evaluator import evaluate_live_stops
            armed = live_config.live_armed()
            mode = live_config.mode()
            entries = evaluate_live_stops(dry_run=not armed)
        except Exception as exc:
            _CONSECUTIVE_FAILURES += 1
            n = _CONSECUTIVE_FAILURES
            logger.error("live_stops FAILED source=%s consecutive=%d error=%s: %s",
                         source, n, type(exc).__name__, exc)
            if n >= TICK_FAILED_PAGE_AFTER:
                pending.append(("live-stop-tick-failed", IMMEDIATE_COOLDOWN_S,
                                f"Polyclawd live stop: the stop evaluation itself has failed "
                                f"{n} times in a row; last error "
                                f"{type(exc).__name__}: {str(exc)[:ERROR_TEXT_MAX]}. "
                                f"No position was checked."))
            result = {"failed": True}
        else:
            _CONSECUTIVE_FAILURES = 0
            result = {"checked": len(entries)}
            try:
                shadow = not armed
                checked = len(entries)
                ok = sum(1 for e in entries if e.get("action") == "ok")
                common = (f"spread_held={_ids(entries, 'spread_held')} unpriced={_ids(entries, 'unpriced')} "
                          f"exempt={_ids(entries, 'exempt')} errors={_ids(entries, *_ERROR_ACTIONS)}")
                if shadow:
                    logger.info("live_stops DRYRUN mode=%s armed=false source=%s checked=%d ok=%d "
                                "would_exit=%s %s", mode, source, checked, ok,
                                _ids(entries, "would_exit"), common)
                else:
                    exited = [e for e in entries if e.get("action") not in _KNOWN_ACTIONS]
                    logger.info("live_stops ARMED source=%s checked=%d ok=%d exited=%s %s",
                                source, checked, ok,
                                "[" + ",".join(str(i) for i in sorted(
                                    int(e["id"]) for e in exited if str(e.get("id")).isdigit())) + "]",
                                common)
                for e in entries:
                    if e.get("action") in ("would_exit", "spread_held"):
                        logger.info("live_stops DETAIL id=%s action=%s mark=%s loss_pct=%s "
                                    "spread_pct=%s threshold=%s", e.get("id"), e["action"], e.get("mark"),
                                    e.get("loss_pct"), e.get("spread_pct"), e.get("threshold"))
                since = _update_unpriced(entries, path, now)
                _page_unpriced(since, now, shadow, pending)
                _page_errors(entries, shadow, pending)
            except Exception as exc:
                logger.error("live_stops post-processing failed source=%s: %s: %s",
                             source, type(exc).__name__, exc)
    finally:
        _TICK_LOCK.release()
    _send_pages(pending, now)
    return result

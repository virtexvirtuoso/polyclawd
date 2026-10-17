"""One-cycle memory diagnostic for polyclawd-scheduler (self-removing).

Context (2026-10-17): scheduler anon memory ramps ~170MB -> 1.2G MemoryMax cap
in ~24-30h, then the cgroup OOM-killer kills it (Oct 11 event: 1.1G mem peak,
2.2G swap peak). Static leak analysis (memory/2026-10-17-leak-analysis.md,
Polyclawd session) found only slow unbounded growers; the fast leak needs
runtime attribution. This probe: tracemalloc(25 frames) + a 6h snapshot-diff
loop logging the top allocation sites BY TRACEBACK to the journal.

Self-removal (Sentinel condition (a)): armed for ONE 8h window via
/tmp/polyclawd_memdiag.deadline (override: POLYCLAWD_MEMDIAG_DEADLINE_FILE).
First start creates the deadline (now + 8h) and runs the probe. Any start
AFTER the deadline is INERT (no tracemalloc, no task) and logs that the
window closed. The eventual targeted-fix PR deletes this module entirely.
"""
from __future__ import annotations

import gc
import logging
import os
import resource
import time
import tracemalloc
from pathlib import Path

DEADLINE_FILE = Path(
    os.environ.get(
        "POLYCLAWD_MEMDIAG_DEADLINE_FILE",
        "/tmp/polyclawd_memdiag.deadline",
    )
)
WINDOW_S = 8 * 3600
DIFF_EVERY_S = 6 * 3600
WARMUP_S = 60
N_FRAMES = 25
TOP_N = 25
TAG = "MEMDIAG"


def _deadline() -> float:
    try:
        return float(DEADLINE_FILE.read_text().strip())
    except Exception:
        return 0.0


def arm_and_start(logger: logging.Logger) -> bool:
    """Arm the probe for its one window. Returns True if the diag loop should start."""
    now = time.time()
    dl = _deadline()
    if dl and now > dl:
        logger.info(
            "%s: diagnostic window closed (%.1fh after deadline) — probe INERT; "
            "remove this instrumentation in the targeted-fix PR",
            TAG, (now - dl) / 3600,
        )
        return False
    if not dl:
        try:
            DEADLINE_FILE.write_text(str(now + WINDOW_S))
            logger.info(
                "%s: armed for ONE %.0fh window (deadline %s); diffs every %.0fh",
                TAG, WINDOW_S / 3600,
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + WINDOW_S)),
                DIFF_EVERY_S / 3600,
            )
        except Exception as e:
            logger.warning("%s: could not write deadline file (%s) — probe disabled", TAG, e)
            return False
    else:
        logger.info(
            "%s: resuming armed window (deadline %s)",
            TAG, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(dl)),
        )
    tracemalloc.start(N_FRAMES)
    return True


def _anon_bytes() -> int | None:
    """Best-effort cgroup anon read — the leak signal (page cache is not the leak)."""
    for p in (
        "/sys/fs/cgroup/system.slice/polyclawd-scheduler.service/memory.stat",
        "/sys/fs/cgroup/memory.stat",
    ):
        try:
            for line in Path(p).read_text().splitlines():
                if line.startswith("anon "):
                    return int(line.split()[1])
        except Exception:
            continue
    return None


def _log_diff(logger: logging.Logger, prev, label: str) -> object | None:
    cur = tracemalloc.take_snapshot()
    stats = cur.compare_to(prev, "traceback") if prev is not None else cur.statistics("traceback")
    anon = _anon_bytes()
    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    lines = [
        f"{TAG} {label}: anon={anon / 1e6 if anon is not None else -1:.0f}MB "
        f"ru_maxrss={rss_mb:.0f}MB gc_objects={len(gc.get_objects())} "
        f"tracemalloc_overhead={tracemalloc.get_tracemalloc_memory() / 1e6:.0f}MB",
    ]
    for i, s in enumerate(stats[:TOP_N]):
        frames = " <- ".join(f.frame for f in s.traceback[-3:])
        lines.append(f"{TAG} #{i + 1:02d} {s.size / 1e6:+.1f}MB n={s.count} {frames}")
    logger.info("\n".join(lines))
    return cur


async def run_diag_loop(logger: logging.Logger) -> None:
    """Warm up, snapshot, then log one diff per DIFF_EVERY_S until the window closes."""
    import asyncio

    await asyncio.sleep(WARMUP_S)
    prev = _log_diff(logger, None, "baseline (warmup +60s)")
    while True:
        await asyncio.sleep(DIFF_EVERY_S)
        dl = _deadline()
        if dl and time.time() > dl:
            _log_diff(logger, prev, "final diff (window closing)")
            tracemalloc.stop()
            logger.info("%s: window complete — probe self-stopping (INERT after restart)", TAG)
            return
        prev = _log_diff(logger, prev, f"diff @+{DIFF_EVERY_S // 3600}h")
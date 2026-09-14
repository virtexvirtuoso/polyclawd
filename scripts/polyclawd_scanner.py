#!/usr/bin/env python3
"""Standalone mispriced-category scanner for Polyclawd.

Decoupled from polyclawd-api per
Design-Notes/Polyclawd-Scanner-Decoupling-2026-09-13.md (2026-09-13).

Runs as polyclawd-scanner.service (timer, every 6 min, flock-guarded).
Calls the SAME get_mispriced_category_signals() entry point the API routes
used inline (same persistence: shadow_trades + signal_snapshots), then
writes the result atomically to storage/mispriced-snapshot.json so the API
can serve reads without ever running the 5-12 min blocking scan inline.

Import order matters: sys.path puts signals/ BEFORE the project root so the
bare-name import resolves to the real scanner module, never the root-level
API shim (which, in snapshot mode, would read this scanner's own snapshot
instead of scanning).

Exit codes: 0 = scan+write ok (or flock skip by design); 1 = scan or write
failed (details on stderr/journal).
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "storage" / "mispriced-snapshot.json"

sys.path.insert(0, str(ROOT))                # for `from signals.basket_arb_scanner import ...`
sys.path.insert(0, str(ROOT / "signals"))    # bare imports (shadow_tracker); MUST precede ROOT

# The shared module gates inline scanning on MISPRICED_SOURCE (in-module
# gate, 2026-09-14). The scanner must ALWAYS scan, so strip the flag in case
# it ever leaks into /etc/default/polyclawd, which this unit sources.
os.environ.pop("MISPRICED_SOURCE", None)

import mispriced_category_signal as m  # noqa: E402

# Guard: if this ever resolves to the root-level API shim, the scanner would
# read its own snapshot in snapshot mode and silently stop scanning. Fail loud.
assert m.__file__.endswith("signals/mispriced_category_signal.py"), (
    f"scanner imported the wrong module: {m.__file__}"
)


def main() -> int:
    result = m.get_mispriced_category_signals()
    result["source"] = "scanner-service"
    result["snapshot_written_at"] = datetime.now(timezone.utc).isoformat()
    result["scanner_pid"] = os.getpid()

    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(SNAPSHOT.parent), prefix=".mispriced-snapshot.", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(result, f, default=str)
        os.replace(tmp, SNAPSHOT)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    print(
        f"scan ok: total={result.get('total')} "
        f"poly={result.get('polymarket_signals')} "
        f"kalshi={result.get('kalshi_signals')} "
        f"basket={result.get('basket_arb_signals')} -> {SNAPSHOT}"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"scanner FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
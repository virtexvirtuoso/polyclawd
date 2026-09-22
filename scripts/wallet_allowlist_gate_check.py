#!/usr/bin/env python3
"""Weekly wallet-allowlist gate check -> Telegram (polyclawd channel).

Runs the pre-registered forward-test report (scripts/wallet_allowlist.py
report) and delivers the verdict + scoreboard to Mr. V's chat. Weekly cron,
Sundays 09:00 ET / 13:00 UTC (installed 2026-09-21 on Mr. V's request).
Gate + design: vault 02-Projects/Polyclawd/Strategy/Smart-Money-Whale/
Wallet-Allowlist-Forward-Test-2026-09-21.md.

A PASS verdict changes NOTHING automatically — it is a recommendation that
requires Mr. V's explicit go to revive the live book.
"""

import contextlib
import io
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from scripts.wallet_allowlist import (  # noqa: E402
    GATE_MIN_DAYS,
    GATE_MIN_EDGE_PP,
    GATE_MIN_TRADES,
    current_allowlist,
    report,
)


def _short(w: str) -> str:
    return w[:10] + "…"


def build_message() -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        report()
    body = buf.getvalue().strip()
    edge = re.search(r"^edge: ([+-][\d.]+)pp$", body, re.M)
    days = re.search(r"forward window: ([\d.]+) days", body)
    n = re.search(r"allowlist-followed: n=(\d+)", body)
    verdict = "PASS" if "gate [PASS]" in body else "NOT YET"
    head = (
        f"🚦 Allowlist gate: {verdict} — edge {edge.group(1) if edge else '?'}pp "
        f"(need ≥{GATE_MIN_EDGE_PP:.0f}), day {days.group(1) if days else '?'} "
        f"of {GATE_MIN_DAYS}, trades {n.group(1) if n else '?'} of {GATE_MIN_TRADES}"
    )
    wallets = sorted(current_allowlist())
    tail = "on the list: " + (", ".join(_short(w) for w in wallets) if wallets else "(none)")
    return head + "\n\n" + body + "\n\n" + tail


def main() -> None:
    try:
        msg = build_message()
    except Exception as exc:  # noqa: BLE001 — a failed check must still page
        msg = (
            "🚨 ALLOWLIST GATE CHECK FAILED: "
            f"{type(exc).__name__}: {exc}\n(run scripts/wallet_allowlist.py report manually)"
        )
    from scripts.openclaw_alerts import alert_openclaw

    ok = alert_openclaw(msg, parse_mode=None)
    print(msg)
    print(f"delivered: {ok}")


if __name__ == "__main__":
    main()

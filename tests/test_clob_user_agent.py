"""Guard: never send a Mozilla User-Agent to clob.polymarket.com.

The CLOB API bot-blocks browser-shaped User-Agents. Verified on the VPS
2026-08-28, same URL, same process:

    UA=Mozilla/5.0     -> HTTP 403
    UA=Polyclawd/2.0   -> HTTP 200
    UA=curl/8.5.0      -> HTTP 200

gamma-api.polymarket.com does NOT block it, which is why this went unnoticed:
most Polymarket calls in this repo go to Gamma and work fine.

signals/shadow_tracker.py::_fetch_json hardcoded "Mozilla/5.0" and is the
only fetcher used by _check_polymarket_resolution. Every Polymarket
resolution check therefore returned None ("still open") for every trade,
including markets closed months earlier. Combined with resolve_trades()
taking `ORDER BY timestamp ASC LIMIT 15`, the same 15 oldest rows were
retried forever and the backlog never advanced: 81 unresolved rows, oldest
2026-02-21. The failure was invisible because _fetch_json logs at DEBUG and
returns None, which is indistinguishable from "market not resolved yet".

signals/paper_portfolio.py had the same UA on two local _fetch helpers that
price open positions against {CLOB_API}/markets/{id}.
"""

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
# scratch/ and research/ are throwaway one-off analyses, not runtime code, and
# are excluded so this guard stays about paths that actually ship. They carry
# the same broken UA; fix them if you ever revive one.
SKIP = (
    "/venv/",
    "/.git/",
    "__pycache__",
    "/node_modules/",
    "/tests/",
    "/scratch/",
    "/research/",
)

CLOB_HOST = "clob.polymarket.com"
UA_RE = re.compile(r'["\']User-Agent["\']\s*:\s*["\']([^"\']+)["\']')


def _files_touching_clob():
    for p in sorted(REPO.rglob("*.py")):
        s = str(p)
        if any(x in s for x in SKIP):
            continue
        try:
            src = p.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if CLOB_HOST in src:
            yield p, src


def test_some_files_touch_clob():
    """Positive control: if this finds nothing, the test below is vacuous."""
    files = list(_files_touching_clob())
    assert files, f"no source file references {CLOB_HOST}"


def test_no_mozilla_user_agent_in_clob_callers():
    """Any module talking to the CLOB must not declare a Mozilla UA.

    Scoped to files that reference the CLOB host: a Mozilla UA aimed at
    Gamma, Kalshi, Yahoo or ESPN is fine and stays untouched.
    """
    offenders = []
    for path, src in _files_touching_clob():
        for lineno, line in enumerate(src.splitlines(), 1):
            m = UA_RE.search(line)
            if m and "mozilla" in m.group(1).lower():
                offenders.append(f"{path.relative_to(REPO)}:{lineno} -> {m.group(1)!r}")
    assert not offenders, (
        f"Mozilla User-Agent in a module that calls {CLOB_HOST} "
        "(the CLOB returns 403 for browser-shaped agents):\n  " + "\n  ".join(offenders)
    )

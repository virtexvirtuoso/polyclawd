"""Gated + cached + balance-refreshing the-odds-api fetch for the live-monitor
scripts (scripts/cross_sport_drift.py, mlb_live_monitor.py, soccer_live_monitor.py).

WHY THIS EXISTS
---------------
Those three monitors historically called the-odds-api with raw urllib, which:
  * BYPASSED the rate_limiter credit floor — they spent straight through the
    5,000-credit reserve meant for `critical` crons (the June-2026 leak: the
    balance drained to 4,926, just under the floor, because only the gated edge
    path stopped while these monitors kept going);
  * re-fetched the ENTIRE sport slate once PER active game each tick (a full
    refetch per matchup instead of one shared fetch);
  * discarded the `x-requests-remaining` response header, so the authoritative
    balance cache went stale and dead-locked the gate (the gate reads a cached
    balance that only a successful fetch refreshes — but the gate blocks the
    fetch, so a stale-low reading never self-heals).

This helper routes all three through one path that gates, memoizes per identical
URL for a short TTL (collapsing per-game refetches into one fetch per sport per
tick), and writes the live credit headers back on every fetch.
"""
import json
import time
import urllib.parse
import urllib.request
from typing import Optional

# Up to 10 bookmakers bill as 1 the-odds-api credit (vs regions=us,uk,eu = 3).
# This list spans us/uk/eu sharp + liquid books so the monitors' frozen-book
# staleness filter still has multiple LIVE books to compare — preserving the
# anti-freeze behaviour they were written for, at 1/3 the credit cost.
LIVE_BOOKS = (
    "pinnacle,betfair_ex_eu,betfair_ex_uk,williamhill,betmgm,"
    "draftkings,fanduel,unibet_eu,betvictor,marathonbet"
)

# url -> (fetched_ts, parsed_json). Module-level so repeated per-game calls in a
# single tick collapse to one upstream fetch per identical URL.
_CACHE: dict = {}


def clear_cache() -> None:
    """Drop all memoized responses (used by tests and between scan cycles)."""
    _CACHE.clear()


def gated_fetch_json(base_url: str, params: Optional[dict] = None,
                     ttl: float = 90.0, priority: str = "low",
                     timeout: int = 12):
    """GET the-odds-api JSON through the credit gate with short-TTL memoization.

    - Returns a cached payload if fetched within `ttl` seconds (kills per-game
      full-slate refetch).
    - Gates on rate_limiter.can_make_call(priority): below the CREDIT_FLOOR it
      returns the last cached payload (even if stale) rather than spending the
      reserve — it never bypasses the floor the way raw urllib did.
    - On every real fetch, persists x-requests-remaining so the floor's balance
      stays fresh (fixes the stale-credit-cache deadlock).

    Returns parsed JSON, or None when gated with no cache / on error.
    """
    url = base_url + ("?" + urllib.parse.urlencode(params) if params else "")
    now = time.time()
    hit = _CACHE.get(url)
    if hit and (now - hit[0]) < ttl:
        return hit[1]

    rl = None
    ok = True
    try:
        from odds import rate_limiter as rl
        ok, _why = rl.can_make_call(priority)
    except Exception:  # never let gating break a fetch
        ok = True
    if not ok:
        return hit[1] if hit else None  # respect the floor; serve stale, don't spend

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            headers = {k.lower(): v for k, v in r.headers.items()}
            data = json.loads(r.read().decode())
    except Exception as e:
        print(f"[monitor_gate] GET {url[:70]} -> {e}", flush=True)
        return hit[1] if hit else None

    if rl is not None:
        try:
            rem = headers.get("x-requests-remaining")
            used = headers.get("x-requests-used")
            if rem is not None:
                rl.persist_real_remaining(int(rem), int(used) if used is not None else None)
            rl.update_from_headers(headers)
        except Exception:
            pass

    _CACHE[url] = (now, data)
    return data

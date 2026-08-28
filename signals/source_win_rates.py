"""Empirical per-source win rates from resolved paper/shadow trades.

Read-only side of the per-source learning loop: api.routes.signals blends
the seed priors in data/source_outcomes.json with realized outcomes from
storage/shadow_trades.db via lookup().

'stopped' outcomes are deliberately EXCLUDED: a stop-out reflects stop
policy (risk management), not model accuracy, so counting it would bias
the win-rate signal that confidence scoring consumes.

The two source tables use DIFFERENT outcome vocabularies and the union below
must normalize them, NOT assume one:

  paper_positions.status : 'won' | 'lost' | 'stopped' | 'open'
  shadow_trades.side     : 'YES' | 'NO' | 'PASS' | ''
  shadow_trades.outcome  : 'YES' | 'NO' | 'VOID' | ''  (NULL possible pre-schema)

shadow_trades has never stored the string 'won' -- filtering it for
'won'/'lost' matched 0 of 334 resolved shadow trades in production
(2026-08-26) and silently reduced this loop to the paper_positions arm.

A shadow WIN is the sign of pnl, which every resolver writes as the trade's
signed settlement and treats as its own win definition (shadow_tracker.py
run stats: wins = pnl > 0; baseball_resolver.py: pnl derived from
name-matched is_correct). side == outcome is NOT a valid predicate across
strategies: `outcome` is the market-frame resolution ('YES' = first-listed
token won) while sports resolvers score the trade by matching its chosen
team/total, so side == outcome contradicts the resolver's own verdict on
113 of 332 scoreable rows (all baseball_*), while pnl's sign agrees with
side == outcome on all 92 non-sports rows (verified 2026-08-26; the
scoreable set has no NULL and no exactly-zero pnl).

VOID, ''/NULL-outcome, side PASS/'' and NULL-pnl rows are excluded rather
than defaulted: they are unscoreable, and letting them fall through would
count each one as a loss. If the DB ever holds resolved shadow rows while
the shadow arm matches none of them, empirical_source_counts logs a
WARNING (fleet rule: a filter that can never match must alarm, not sit
quiet).
"""

import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "storage" / "shadow_trades.db"

_CACHE_TTL = 60.0  # seconds
# counts=None means "nothing cached yet". An empty dict {} is a VALID cached
# result (empty DB) and is served for the full TTL like any other -- caching
# only truthy results reopened SQLite on every request while the DB was empty.
_cache: dict = {"ts": 0.0, "counts": None}

_QUERY = """
SELECT strategy,
       SUM(CASE WHEN outcome='won' THEN 1 ELSE 0 END) AS wins,
       COUNT(*) AS total
FROM (
  SELECT strategy, status AS outcome FROM paper_positions WHERE status IN ('won','lost')
  UNION ALL
  SELECT strategy,
         CASE WHEN pnl > 0 THEN 'won' ELSE 'lost' END AS outcome
  FROM shadow_trades
  WHERE resolved = 1 AND outcome IN ('YES','NO') AND side IN ('YES','NO')
    AND pnl IS NOT NULL
)
GROUP BY strategy
"""

# Cheap sentinel for the shadow arm going structurally dead (the D1 failure
# class): resolved shadow rows exist but the scoreable-row filter matches none.
_SHADOW_ZERO_HIT_QUERY = """
SELECT COUNT(*) AS resolved_total,
       SUM(CASE WHEN outcome IN ('YES','NO') AND side IN ('YES','NO')
                     AND pnl IS NOT NULL
                THEN 1 ELSE 0 END) AS scoreable
FROM shadow_trades
WHERE resolved = 1
"""

# record_outcome() source keys -> shadow_trades.db strategy names.
# Populated from the actual distinct strategies present in the DB where a
# sensible mapping exists. Legacy sources with no strategy counterpart are
# left unmapped ON PURPOSE: they keep their seed prior.
#
# DELIBERATE EXCLUSIONS (not oversights):
#   kalshi_fade_longshot_no (242W/253 resolved) and kalshi_fade_favorite_yes
#   (10W/16 resolved) in paper_positions are strategy labels written by
#   signals/kalshi_weather_fade.py (STRATEGY_NO / STRATEGY_YES). They are a
#   standalone fade strategy, not one of the signal sources that
#   api.routes.signals scores -- no source key with either name exists
#   anywhere in the signal-generation code (verified by grep, 2026-08-26),
#   and data/source_outcomes.json seeds no such key. Their records remain
#   reachable through lookup()'s exact-strategy-name match should a consumer
#   ever be added; mapping them to an unrelated source key here would
#   contaminate that source's win rate.
SOURCE_TO_STRATEGY: dict[str, list[str]] = {
    # Both are the mispriced-category scanner; 'legacy_' is the pre-rename
    # strategy label still attached to older shadow rows. Mapping only the
    # newer name scored the source off n=12 (2 wins) and pinned it to the
    # 0.20 clamp floor; together they are 16/65 = 24.6% (2026-08-26).
    "mispriced_category": ["MispricedCategoryWhale", "legacy_mispriced_category"],
    "tweet_count_scanner": ["tweet_count_mc"],
    "volume_spike": ["legacy_volume_spike"],
    "options_implied": ["options_implied"],
}


def empirical_source_counts() -> dict[str, tuple[int, int]]:
    """Return {strategy: (wins, total)} over resolved paper + shadow trades.

    Opens the DB read-only with a 5s timeout; results are cached for 60s --
    including a legitimately-empty result. Any failure (missing DB, lock,
    schema drift) returns {} WITHOUT caching, so callers fall back to their
    priors and the next call retries.

    Alarm (once per cache refresh): if resolved shadow rows exist but zero of
    them pass the scoreable-row filter, the shadow arm has gone structurally
    dead again (outcome/side vocabulary drift) and a WARNING is logged.
    """
    now = time.time()
    if _cache["counts"] is not None and now - _cache["ts"] < _CACHE_TTL:
        return _cache["counts"]
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(_QUERY).fetchall()
            resolved_total, scoreable = conn.execute(_SHADOW_ZERO_HIT_QUERY).fetchone()
        finally:
            conn.close()
        if (resolved_total or 0) > 0 and (scoreable or 0) == 0:
            logger.warning(
                "source_win_rates: shadow arm matched 0 of %d resolved "
                "shadow_trades rows -- the outcome/side/pnl filter no longer "
                "matches the stored vocabulary, so shadow trades contribute "
                "nothing to empirical win rates",
                resolved_total,
            )
        counts = {r[0]: (int(r[1] or 0), int(r[2] or 0)) for r in rows if r[0]}
        _cache["ts"] = now
        _cache["counts"] = counts
        return counts
    except Exception:
        return {}


def lookup(source: str) -> tuple[int, int]:
    """Return (wins, total) of resolved trades for a source key.

    Exact strategy-name match first, then SOURCE_TO_STRATEGY. Returns
    (0, 0) when unmapped or when there are no resolved trades -- callers
    then keep their seed prior exactly. 'stopped' outcomes never count
    (see module docstring).
    """
    counts = empirical_source_counts()
    if source in counts:
        return counts[source]
    wins = total = 0
    for strat in SOURCE_TO_STRATEGY.get(source, []):
        w, t = counts.get(strat, (0, 0))
        wins += w
        total += t
    return wins, total

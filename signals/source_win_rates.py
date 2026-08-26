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
  shadow_trades.side     : 'YES' | 'NO' | 'PASS'
  shadow_trades.outcome  : 'YES' | 'NO' | 'VOID' | NULL

A shadow trade is a win when side == outcome. shadow_trades has never stored
the string 'won'. Filtering it for 'won'/'lost' matched 0 of 334 resolved
shadow trades in production (2026-08-26) and silently reduced this loop to
the paper_positions arm alone.

VOID, NULL and side='PASS' rows are excluded rather than defaulted: they are
unscoreable, and letting them fall through would count each one as a loss.
"""

import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "storage" / "shadow_trades.db"

_CACHE_TTL = 60.0  # seconds
_cache: dict = {"ts": 0.0, "counts": {}}

_QUERY = """
SELECT strategy,
       SUM(CASE WHEN outcome='won' THEN 1 ELSE 0 END) AS wins,
       COUNT(*) AS total
FROM (
  SELECT strategy, status AS outcome FROM paper_positions WHERE status IN ('won','lost')
  UNION ALL
  SELECT strategy,
         CASE WHEN side = outcome THEN 'won' ELSE 'lost' END AS outcome
  FROM shadow_trades
  WHERE resolved = 1 AND outcome IN ('YES','NO') AND side IN ('YES','NO')
)
GROUP BY strategy
"""

# record_outcome() source keys -> shadow_trades.db strategy names.
# Populated from the actual distinct strategies present in the DB where a
# sensible mapping exists. Legacy sources with no strategy counterpart are
# left unmapped ON PURPOSE: they keep their seed prior.
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

    Opens the DB read-only with a 5s timeout; results are cached for 60s.
    Any failure (missing DB, lock, schema drift) returns {} so callers
    fall back to their priors.
    """
    now = time.time()
    if _cache["counts"] and now - _cache["ts"] < _CACHE_TTL:
        return _cache["counts"]
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(_QUERY).fetchall()
        finally:
            conn.close()
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

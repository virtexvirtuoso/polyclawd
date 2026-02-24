# API Resilience Layer

**Deployed:** 2026-02-24  
**Issue:** All data sources used bare `try/except → return None/[]` — no retries, no circuit breakers, no health tracking. Sources silently failed and the pipeline continued with missing data.

---

## Layer 1: Source Health Registry

**File:** `api/services/source_health.py`

- `source_health` table in existing SQLite DB (`storage/shadow_trades.db`)
- Tracks per-source: last_success, last_error, consecutive_failures, avg_latency
- 7 tracked sources: polymarket_gamma, polymarket_clob, kalshi, manifold, action_network, vegas, espn
- Endpoint: `GET /api/source-health`

## Layer 2: Resilient Fetch Wrapper

**File:** `api/services/resilient_fetch.py`

- `@resilient(source_name, retries=2, backoff_base=2)` decorator
- Exponential backoff with random jitter
- Circuit breaker: 5 consecutive failures → 30min cooldown (skips source entirely)
- All attempts logged with timing
- Updates source health registry on every success/failure

## Layer 3: Staleness Tags

- Signals now carry `data_age_seconds` and `sources_used[]`
- `scan_cross_platform_arb()` returns per-arb source freshness
- `paper_portfolio.py` rejects signals where primary source data > 3600s old

---

## Integration

Wired into (with graceful `HAS_RESILIENT` guards):
- `odds/polymarket_clob.py`
- `odds/sports_odds.py`
- `odds/manifold.py`
- `odds/vegas_scraper.py`
- `signals/cross_platform_arb.py`

## Tests

- `tests/unit/test_source_health.py`
- `tests/unit/test_resilient_fetch.py`
- 16/16 passing

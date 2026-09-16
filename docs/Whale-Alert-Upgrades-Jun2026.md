# Whale Alert System Upgrades — June 12, 2026

## Changes Made

### 1. Score Display Fix (Drain Script)
**Problem:** Polymarket CRITICAL alerts showed misleading scores like "3/10" or "4/10" because the drain script displayed the `score` field (sweep-only score) instead of `raw_score` (full conviction score with all bonuses).

**Root Cause:** Two score fields in `whale_scanner.db`:
- `score` — original sweep/book score (order book signals only, 0-10)
- `raw_score` — computed score with all bonuses (flow magnitude, taker aggression, wallet concentration), unbounded

Severity thresholds use `raw_score` (≥10.0 = CRITICAL), but the drain script displayed `score`.

**Fix:** `whale_alert_drain.py` line 104 now uses `min(int(float(raw_score)), 10)` for display.

### 2. CRITICAL-Only Filter Restored
**Problem:** Drain query had been expanded to include Polymarket HIGH alerts.

**Fix:** Reverted to `severity = 'CRITICAL'` for all platforms.

### 3. Polymarket CLOB Book Inspection (Kalshi Parity)
**Problem:** Kalshi alerts scored higher because they got two signal layers:
1. `sweep_score()` — volume/OI deltas from trades feed
2. `score_change()` — order book diff (level jumps, depth surges, imbalance flips, spread collapse)

Polymarket only got `sweep_score()` despite having a CLOB with full order book data.

**Fix:** Added `inspect_pm_book()` to `whale_scanner.py`. For every Polymarket flow candidate that passes `ALERT_MIN_SCORE`:
1. Fetches CLOB orderbook via `https://clob.polymarket.com/book?token_id={yes_token}`
2. Converts to `(price, size_dollars)` tuples → `book_summary()`
3. Diffs against previous snapshot via `score_change()`
4. Merges book score + reasons into the alert before `_mk_alert()`

Token IDs come from Gamma API's `clobTokenIds` field (index 0 = YES token).

**Signal parity after patch:**

| Signal | PM (before) | PM (after) | Kalshi |
|---|---|---|---|
| Volume/OI spikes | ✅ | ✅ | ✅ |
| Taker flow direction | ✅ | ✅ | ✅ |
| Wallet tracking | ✅ | ✅ | ❌ |
| Level jumps | ❌ | ✅ | ✅ |
| Depth surges | ❌ | ✅ | ✅ |
| Imbalance flips | ❌ | ✅ | ✅ |
| Spread collapse | ❌ | ✅ | ✅ |

### 4. Cron Model Switch (Kimi)
**Problem:** Whale alert and resolution timing cron jobs used Claude, which hit rate limits during peak hours.

**Fix:**
- `whale-shark-alerts` → `nvidia/moonshotai/kimi-k2.6`
- `resolution-timing-alert` → `nvidia/moonshotai/kimi-k2.6` (with fallback chain)

## Scoring System Reference

### Raw Score Computation (`_compute_raw_score`)
Base = sweep_score + book_score, then:

| Bonus | Points | Trigger |
|---|---|---|
| Flow magnitude | +0 to ~4 | log₁₀(flow/$100) × 1.5 |
| Flow intensity | +1 or +2 | ≥50% or ≥80% of lifetime volume |
| Taker aggressiveness | +1/+2/+3 | ≥60% / ≥80% / ≥95% one-sided |
| Wallet concentration | +1/+2/+3 | top wallet ≥30% / ≥50% / ≥80% |

| Penalty | Effect | Trigger |
|---|---|---|
| Market maturity | -1 or -3 | closes >14d or >30d out |
| Bilateral flow | ×0.7 or ×0.5 | minority side 10-30% or 30%+ |
| Thin market | halves sweep | lifetime volume <$5K |
| Book-only | ×0.5 | zero executed flow |

### Severity Thresholds
- `raw_score ≥ 10.0` → CRITICAL
- `raw_score ≥ 6.0` → HIGH
- `raw_score ≥ 3.0` → LOW
- Below 3.0 → SUPPRESSED

## Files Modified
- `scripts/whale_alert_drain.py` — score display + CRITICAL filter
- `signals/whale_scanner.py` — `inspect_pm_book()` + wiring in `scan_polymarket_flow()`
- Cron jobs: `whale-shark-alerts`, `resolution-timing-alert` (model field)

## Notes
- First PM book scan establishes baselines (score 0). Diffs appear on second+ scan (~15 min).
- Book snapshots stored in `whale_snapshots` table with key `pm:{condition_id[:16]}`.
- Backup: `whale_scanner.py.bak` on VPS.

# Becker Dataset Upgrade Plan — Feb 18, 2026

Based on Roan (@RohOnChain) article: "How To Use Prediction Market Data Like Hedge Funds"
Dataset: Jon Becker's 400M+ trade dataset (github.com/Jon-Becker/prediction-market-analysis)

## Priority Order

### P0: CV Kelly Haircut (Position Sizing Risk Fix)
**Status**: Not implemented
**Why P0**: We may be overbetting. Current Kelly is deterministic — no uncertainty adjustment.
**Formula**: `f_empirical = f_kelly × (1 - CV_edge)`
- CV_edge = stdev(edge_estimates) / mean(edge_estimates) from historical analogs
- High uncertainty → aggressive haircut → smaller positions
- Low uncertainty → sizing closer to theoretical Kelly

**Implementation**:
1. In `config/scaling_phases.py` — add CV calculation from resolved shadow trades
2. Bootstrap resample resolved trades (1000 iterations) to estimate edge distribution
3. Compute CV from bootstrapped edge means
4. Apply haircut: `kelly_fraction *= (1 - cv_edge)`
5. Add Monte Carlo drawdown check: simulate 10,000 paths, ensure 95th percentile max drawdown < 20%

**Files**: `config/scaling_phases.py`, `signals/paper_portfolio.py`
**Estimated effort**: 2-3 hours

---

### P1: Download Updated Becker Dataset (Polymarket Trades)
**Status**: We have the repo but may be missing Feb 10 Polymarket update (400M trades)
**Why P1**: Unlocks calibration surface + taker analysis. 36GB compressed.

**Implementation**:
1. Check current dataset state on Mac Mini (`~/Desktop/prediction-market-analysis/data/`)
2. `cd ~/Desktop/prediction-market-analysis && make setup` (or targeted Polymarket download)
3. Verify Parquet files: `ls data/polymarket/trades/`
4. Run DuckDB count to confirm 400M+ trades

**Location**: Mac Mini local (190GB free, needs 40GB+)
**Estimated effort**: 30 min active + download time

---

### P2: Calibration Surface C(p,t)
**Status**: We have 1D price-based mispricing. Missing time dimension.
**Why P2**: Formalizes our price-zone insights + adds temporal edge detection.

**Implementation**:
1. New script: `src/analysis/custom/calibration_surface.py`
2. From Becker dataset: for each (price_bucket, days_to_resolution_bucket), compute:
   - `C(p,t)` = empirical win rate at that (price, time) combination
   - `M(p,t) = C(p,t) - p/100` (mispricing function)
3. Generate 2D heatmap: price (0-100) × time (0-365 days) → mispricing %
4. Export optimal entry rules: `M(p,t) > threshold` → short, `M(p,t) < -threshold` → long
5. Wire into signal pipeline as confidence modifier

**Dependencies**: P1 (needs full trade dataset)
**Files**: New analysis script + `signals/mispriced_category_signal.py` integration
**Estimated effort**: 4-5 hours

---

### P3: Taker Direction Analysis
**Status**: Not tracked
**Why P3**: Becker proves takers lose at 80/99 price levels. Understanding flow improves timing.

**Implementation**:
1. From Becker dataset: analyze taker_side field in trades
2. Compute taker profitability by (price, category, time_to_resolution)
3. Build "taker flow pressure" indicator: when takers are aggressively buying YES on longshots → fade harder
4. If Polymarket/Kalshi APIs expose taker direction on live trades, integrate as signal input
5. Otherwise, use as backtested insight for position sizing

**Dependencies**: P1 (needs trade dataset with taker_side field)
**Files**: New analysis script + potential signal integration
**Estimated effort**: 3-4 hours

---

## Execution Plan

| Phase | Task | Timeline | Blocking? |
|-------|------|----------|-----------|
| 1 | P0: CV Kelly haircut | Today | No |
| 2 | P1: Download dataset | Today (parallel) | Blocks P2, P3 |
| 3 | P2: Calibration surface | After P1 download | Blocks signal upgrade |
| 4 | P3: Taker analysis | After P1 download | No |
| 5 | Wire P2 into live pipeline | After P2 | — |
| 6 | Backtest all upgrades on historical data | After P2+P3 | — |

## Expected Impact
- **CV Kelly**: Reduce max drawdown risk 30-50%, smoother equity curve
- **Calibration surface**: Formalize price×time edge, potentially 5-10% WR improvement
- **Taker analysis**: Better signal timing, avoid being "the taker"
- **Combined**: Move from ~53% WR empirical to 60%+ with proper risk management

## References
- Article: https://x.com/rohonchain/status/2023781142663754049
- Dataset: https://github.com/Jon-Becker/prediction-market-analysis
- Becker's research: 72.1M Kalshi trades, longshot bias -57% at 1¢, takers lose at 80/99 levels

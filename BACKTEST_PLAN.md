# Polyclawd Backtest Plan

> Full strategy validation against 3.75M+ prediction market records  
> Coordinated by Maestro, executed by Polyclawd  
> Last updated: 2026-02-11

---

## 1. What We've Done (Market Structure Analysis)

9 custom analyses validated **where edges exist** across 3.75M markets:

| # | Analysis | Key Finding | Status |
|---|----------|-------------|--------|
| 1 | Cross-Platform Divergence | Kalshi vs Poly have different pricing structures — arb valid in 15-85% zone | ✅ |
| 2 | Volume Spike → Outcomes | 99.8% directional accuracy across 499K spike events | ✅ |
| 3 | Polymarket Mispricing | 99.7% resolve to extremes — edge is catching transitions early | ✅ |
| 4 | Whale/Volume Tier Profitability | Whales more accurate than assumed — raise win est. 35%→45% | ✅ |
| 5 | Price Impact by Size | r=-0.323 — markets <$10K volume have 200x more pricing error | ✅ |
| 6 | Resolution Timing (Theta) | <7 day markets = best theta/accuracy balance | ✅ |
| 7 | Category Edge Persistence | EUR/USD hourly = 45% error, PGA Tour = 1% — massive category spread | ✅ |
| 8 | Post-Event Price Efficiency | 13% still contested at close — that's the opportunity window | ✅ |
| 9 | Weekend vs Weekday | Weekends MORE efficient (-1.4pp) — Friday worst, Sunday best | ✅ |

**Dashboard:** https://virtuosocrypto.com/polyclawd/analysis.html

---

## 2. What We Haven't Done (Strategy Backtesting)

The 9 analyses prove edges exist. What we haven't done is simulate **Polyclawd's actual trading signals** against historical data to calculate real P&L.

### Strategies to Backtest

| Strategy | Description | Data Needed | Available? |
|----------|-------------|-------------|------------|
| `cross_platform_arb` | Buy cheap on one platform, sell expensive on other | Market prices from both platforms | ✅ Have it |
| `inverse_whale` | Fade whale positions at extremes | Volume tier data | ✅ Have it |
| `volume_spike` | Enter when volume confirms direction | Market volume + timestamps | ⚠️ Partial — need trade-level data |
| `theta_collection` | Buy near-expiry markets where price is locked in | Market duration + close prices | ✅ Have it |
| `category_edge` | Target mispriced categories (Spotify, weather, FX) | Category + error data | ✅ Have it |
| `vegas_devig` | Compare prediction market prices to devigged sportsbook odds | Historical Vegas/ESPN odds | ❌ Need to scrape |
| `correlation_violation` | Multi-signal convergence (high conviction) | Multiple signal streams with timestamps | ❌ Need trade data |
| `weekend_timing` | Weight signals by day-of-week efficiency | DOW resolution data | ✅ Have it |

---

## 3. Backtest Engine Architecture

### Components

```
┌─────────────────────────────────────────────────────┐
│                    MAESTRO                            │
│           (Orchestrator & Parameter Sweeps)           │
├─────────────────────────────────────────────────────┤
│                                                       │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────┐ │
│  │  Strategy    │  │  Backtest    │  │  Report     │ │
│  │  Configs     │  │  Engine      │  │  Generator  │ │
│  │  (YAML)      │  │  (Python)    │  │  (HTML/MD)  │ │
│  └──────┬──────┘  └──────┬───────┘  └──────┬──────┘ │
│         │                │                  │         │
│         ▼                ▼                  ▼         │
│  ┌─────────────────────────────────────────────────┐ │
│  │              DuckDB (3.75M markets)              │ │
│  │         Kalshi + Polymarket parquet files         │ │
│  └─────────────────────────────────────────────────┘ │
│                                                       │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────┐ │
│  │  POLYCLAWD  │  │  Signal      │  │  P&L        │ │
│  │  Signal API  │  │  Validator   │  │  Calculator │ │
│  └─────────────┘  └──────────────┘  └─────────────┘ │
└─────────────────────────────────────────────────────┘
```

### Backtest Engine (`src/backtest/engine.py`)

```python
class BacktestEngine:
    """Simulates Polyclawd signal pipeline against historical markets."""
    
    def run(self, strategy: Strategy, params: dict) -> BacktestResult:
        """
        1. Load historical markets matching strategy filters
        2. Generate entry signals using strategy logic
        3. Simulate position sizing and entry/exit
        4. Calculate P&L, win rate, drawdown, Sharpe
        5. Return BacktestResult with full trade log
        """
```

### Strategy Config Format (`strategies/*.yaml`)

```yaml
name: cross_platform_arb
version: 1.0
description: Cross-platform arbitrage between Kalshi and Polymarket

filters:
  min_volume: 10000          # $10K floor (from analysis #5)
  price_range: [0.15, 0.85]  # Overlap zone (from analysis #1)
  max_duration_days: 7       # Theta sweet spot (from analysis #6)
  categories_exclude:        # Well-priced categories (from analysis #7)
    - KXPGATOUR
    - KXMLB

entry:
  signal: price_divergence
  min_edge_pct: 3.0          # Minimum 3% price difference
  confirmation: volume_spike  # Wait for volume confirmation

exit:
  take_profit_pct: 80        # Exit at 80% of max edge
  stop_loss_pct: -5          # Cut at 5% loss
  max_hold_days: 7           # Force exit after 7 days

sizing:
  method: kelly_fraction
  kelly_multiplier: 0.25     # Quarter Kelly for safety
  max_position_pct: 5        # Max 5% of bankroll per trade
```

---

## 4. Parameter Sweep Matrix

Maestro runs these combinations to find optimal settings:

| Parameter | Values to Test | Impact |
|-----------|---------------|--------|
| `min_volume` | $1K, $5K, $10K, $25K, $50K | Signal quality vs opportunity count |
| `min_edge_pct` | 1%, 2%, 3%, 5%, 8% | Win rate vs trade frequency |
| `max_duration_days` | 1, 3, 7, 14, 30 | Theta vs exposure time |
| `min_confidence` | 55%, 60%, 65%, 70%, 80% | Precision vs recall |
| `kelly_multiplier` | 0.1, 0.25, 0.5 | Risk tolerance |
| `whale_win_rate` | 35%, 40%, 45%, 50% | inverse_whale calibration |

**Total combinations:** ~5,400 parameter sets  
**Estimated runtime:** ~2-4 hours with parallel execution

---

## 5. Metrics & Reporting

Each backtest run produces:

### Core Metrics
- **Total P&L** — net profit/loss in dollars
- **Win Rate** — % of trades that were profitable
- **Sharpe Ratio** — risk-adjusted return
- **Max Drawdown** — worst peak-to-trough decline
- **Profit Factor** — gross profit / gross loss
- **Average Edge** — mean edge captured per trade
- **Trade Count** — total signals generated

### Per-Strategy Breakdown
- P&L curve over time
- Win rate by category, volume tier, duration
- Edge decay analysis (does the edge shrink over time?)
- Correlation between strategies (diversification benefit)

### Output
- **HTML dashboard** — interactive charts on VPS (extends current analysis page)
- **CSV trade log** — every simulated trade with entry/exit/P&L
- **Strategy rankings** — sorted by Sharpe ratio
- **Optimal parameters** — best combo per strategy

---

## 6. Execution Plan

### Phase 1: Build Engine (Now)
- [ ] Create `src/backtest/engine.py` — core simulation loop
- [ ] Create `src/backtest/strategies/` — strategy implementations
- [ ] Create `src/backtest/metrics.py` — P&L and risk calculations
- [ ] Write strategy configs for all 8 strategies
- **Owner:** Polyclawd
- **ETA:** Today

### Phase 2: Run Backtests (After engine is built)
- [ ] Maestro spawns parallel backtest runs
- [ ] Run each strategy independently first
- [ ] Then run parameter sweep matrix
- [ ] Compile results into unified report
- **Owner:** Maestro (orchestration) + Polyclawd (execution)
- **ETA:** Today/Tomorrow

### Phase 3: Trade-Level Backtests (After Kalshi trades data)
- [ ] Kalshi trades indexer completes (currently downloading)
- [ ] Re-run volume_spike and correlation_violation with timestamps
- [ ] Add entry timing analysis — can we catch moves before they complete?
- [ ] Calculate slippage estimates
- **Owner:** Polyclawd
- **ETA:** When Kalshi trades download completes

### Phase 4: Vegas/ESPN Validation (Requires data collection)
- [ ] Scrape historical sportsbook odds (ESPN, Vegas Insider, etc.)
- [ ] Build devigging pipeline
- [ ] Backtest vegas_devig strategy
- [ ] Compare sportsbook-implied probs vs prediction market prices
- **Owner:** Polyclawd
- **ETA:** TBD — depends on data availability

---

## 7. Data Inventory

| Dataset | Records | Size | Location | Status |
|---------|---------|------|----------|--------|
| Kalshi markets | 3.46M | 2,581 parquet files | `data/kalshi/markets/` | ✅ Available |
| Polymarket markets | 236K | parquet files | `data/polymarket/` | ✅ Available |
| Kalshi trades | TBD | TBD | `data/kalshi/trades/` | 🔄 Indexing |
| Polymarket trades | ~37GB | parquet files | `data/polymarket/trades/` | ✅ Available (slow to scan) |
| Historical Vegas odds | None | — | — | ❌ Need to collect |
| ESPN moneylines | None | — | — | ❌ Need to collect |

---

## 8. Success Criteria

The backtest is successful if we can answer:

1. **Which strategies are profitable?** — Positive P&L after fees
2. **What are the optimal parameters?** — Volume floor, edge threshold, etc.
3. **How do strategies correlate?** — Can we run multiple simultaneously?
4. **What's the expected monthly return?** — With realistic position sizing
5. **Where does the edge come from?** — Category? Timing? Platform? Volume?

**Target:** Identify 2-3 strategies with Sharpe > 1.0 and positive P&L over the full dataset.

---

*Generated by Polyclawd · Virtuoso Crypto*

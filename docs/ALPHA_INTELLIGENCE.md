# Alpha Intelligence Pipeline

> Turning Virtuoso confluence scores + price snapshots into prediction market edge.

## Data Sources

| Source | Frequency | Storage | Symbols |
|--------|-----------|---------|---------|
| Confluence Alpha Scores | 30min | `alpha_snapshots` table | 10 monitored alts |
| BTC/ETH Price Snapshots | 30min | `price_snapshots` table | BTCUSDT, ETHUSDT |
| Prediction Market Prices | 30min | signal scan | Polymarket + Kalshi |
| BTC Wiz On-Chain Composite | TBD | TBD | BTC |

## Strategy 1: Score Velocity → Conviction Multiplier

**Thesis:** Confluence score trajectory predicts price direction before prediction markets reprice.

**Mechanism:**
1. Calculate score velocity: `Δscore / Δtime` over 2h and 6h windows
2. Classify: accelerating (+), decelerating (-), flat (±1pt)
3. Apply as multiplier to existing mispriced category signals:
   - Score accelerating toward signal direction → 1.2x confidence
   - Score decelerating against signal direction → 0.7x confidence
   - Flat → 1.0x (no modification)

**Example:**
- SOL score drops 8pts in 2hrs (velocity = -4/hr)
- Polymarket "SOL above $180" sits at 65¢
- Score velocity says bearish → boost confidence on NO side
- If category edge already says NO is mispriced → compound edge

**Data Required:** `alpha_snapshots` table (already collecting)

**Implementation:**
- `get_score_delta()` already exists in `alpha_score_tracker.py`
- Wire into `mispriced_category_signal.py` alongside Virtuoso confirmation modifier
- New modifier: `score_velocity_modifier` (0.7x to 1.3x)

**Edge Estimate:** +3-5% win rate improvement on crypto-related markets

---

## Strategy 2: Price-to-Strike Distance (Volatility-Adjusted)

**Thesis:** Most crypto strike markets (e.g., "BTC above $75K") are mispriced because market makers don't properly account for current momentum + realized volatility.

**Mechanism:**
1. From `price_snapshots`, calculate:
   - Current price
   - Realized volatility (std dev of returns over available window)
   - Current momentum (linear regression slope)
2. For each open strike market, compute:
   - Distance to strike: `|strike - current_price| / current_price`
   - Days remaining until expiry
   - Required daily move: `distance / days_remaining`
   - Historical probability of that move (from realized vol)
   - Momentum adjustment: if trending toward strike, increase prob
3. Compare calculated probability vs market price → edge

**Example:**
```
BTC = $69,246
Strike = $75,000
Days left = 15
Distance = 8.3%
Required daily move = 0.55%/day
BTC realized vol (30d) = 2.1%/day
Momentum = +3.2% over 6h (strong uptrend)

Base probability (vol-adjusted): ~38%
Momentum-adjusted: ~45%
Market price: 25¢ (implies 25%)
→ Edge: +20% (strong YES signal)
```

**Data Required:**
- `price_snapshots` (already collecting)
- Strike prices from open Kalshi/Polymarket markets
- Market expiry dates

**Implementation:**
- New module: `signals/strike_probability.py`
- Uses Black-Scholes-like model adapted for crypto (fat tails → use Student-t or historical distribution)
- Momentum overlay from price snapshot regression
- Feeds into Resolution Certainty Scanner

**Edge Estimate:** 15-25% edge on mispriced strike markets — highest alpha of all three strategies

---

## Strategy 3: Cross-Asset Regime Detection

**Thesis:** When multiple confluence scores move together, it signals a market regime shift. Prediction markets lag regime shifts by hours.

**Mechanism:**
1. Every 30min, compute:
   - Average score across all 10 symbols
   - Score dispersion (std dev)
   - Count of symbols with score > 65 (bullish) vs < 50 (bearish)
2. Classify regime:
   - **Risk-On:** avg > 65, majority bullish, low dispersion
   - **Risk-Off:** avg < 50, majority bearish, low dispersion
   - **Rotation:** high dispersion (some bull, some bear)
   - **Neutral:** avg 50-65, mixed signals
3. Detect regime transitions:
   - Compare current regime to regime 2h ago
   - Transition detected → scan all crypto prediction markets
   - Markets priced for old regime = immediate edge

**Example:**
```
T-2h: avg_score=67, regime=RISK_ON
T-0h: avg_score=52, regime=NEUTRAL (dropping fast)

Regime shift: RISK_ON → NEUTRAL
→ Scan for "BTC above $X" markets still priced high
→ Scan for "crypto crash" markets still priced low
→ Signal: fade the old regime
```

**Data Required:** `alpha_snapshots` table (already collecting)

**Implementation:**
- New module: `signals/regime_detector.py`
- Regime state stored in SQLite (new table `regime_snapshots`)
- Transition alerts trigger immediate market scan
- Override normal 30min scan cycle — regime shifts get 5min priority

**Edge Estimate:** 5-10% edge, but rare (regime shifts happen ~2-4x/week)

---

## Priority & Dependencies

```
                    ┌──────────────────────┐
                    │  Alpha Score Tracker  │ ← LIVE ✅
                    │  (30min snapshots)    │
                    └──────┬───────────────┘
                           │
              ┌────────────┼────────────────┐
              ▼            ▼                ▼
    ┌─────────────┐ ┌──────────────┐ ┌─────────────┐
    │  Strategy 1 │ │  Strategy 2  │ │  Strategy 3 │
    │  Score      │ │  Strike      │ │  Regime     │
    │  Velocity   │ │  Probability │ │  Detection  │
    │  Modifier   │ │  Calculator  │ │             │
    └──────┬──────┘ └──────┬───────┘ └──────┬──────┘
           │               │                │
           ▼               ▼                ▼
    ┌─────────────────────────────────────────────┐
    │          Signal Aggregator                   │
    │  (Bayesian confidence + all modifiers)       │
    └──────────────────┬──────────────────────────┘
                       ▼
    ┌─────────────────────────────────────────────┐
    │          Paper Portfolio Engine               │
    │  (Kelly sizing, position management)          │
    └──────────────────────────────────────────────┘
```

## Build Order

| # | Strategy | Effort | Expected Edge | Dependencies |
|---|----------|--------|---------------|-------------|
| 1 | Price-to-Strike | 1-2 days | 15-25% | price_snapshots + market data |
| 2 | Score Velocity | 0.5 day | 3-5% | alpha_snapshots (ready) |
| 3 | Regime Detection | 1 day | 5-10% | alpha_snapshots (ready) |

**Build #2 (Price-to-Strike) first** — highest edge, pure math, all data available.

## BTC Wiz Integration (Future)

The on-chain composite from BTC Wiz adds a layer we don't have:
- Exchange inflows/outflows (selling/buying pressure)
- Whale wallet movements
- Miner behavior
- UTXO age distribution

This feeds into **all three strategies**:
- Strategy 1: On-chain momentum confirms/contradicts score velocity
- Strategy 2: Exchange outflows = accumulation → bullish strike probability adjustment
- Strategy 3: On-chain regime often leads price regime by 12-24h

**Integration approach:** Snapshot BTC Wiz composite alongside alpha scores, store in same DB, wire as additional modifier.

---

## Metrics & Validation

Track per-strategy:
- **Signal count** per day
- **Win rate** (target: >70%)
- **Edge accuracy** (predicted edge vs realized edge)
- **Latency** (time from signal to market reprice)
- **P&L contribution** (which strategy makes money)

All paper-traded for minimum 2 weeks before any live capital.

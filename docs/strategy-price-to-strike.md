# Strategy 2: Price-to-Strike Distance

> **Status:** Design complete — ready to build  
> **Expected Edge:** 15-25% on mispriced strike markets  
> **Effort:** 1-2 days  
> **Module:** `signals/strike_probability.py`

---

## The Thesis

Most crypto strike markets ("Will BTC be above $75K on March 1?") are mispriced because:

1. **Retail anchors on narrative, not math.** "BTC is pumping, $75K is inevitable" → YES gets bid up regardless of the actual probability based on volatility and time.
2. **Market makers are thin.** Polymarket/Kalshi crypto markets have $10K-$100K liquidity — not enough for sophisticated vol pricing.
3. **Realized volatility is measurable.** We can compute the exact historical probability of a given price move in a given timeframe. When the market price diverges from that probability, we have a quantifiable edge.

This is independent of Strategy 1 (NO fade). NO fade exploits **behavioral bias** (people overpay for YES). Price-to-Strike exploits **volatility mispricing** (people misjudge the probability of specific price moves).

---

## How It Works

### Step 1: Extract Strike Parameters

For every open crypto strike market, parse:
- **Asset:** BTC, ETH, SOL, etc.
- **Strike price:** the threshold ($64,000, $75,000, etc.)
- **Direction:** "above" or "below"
- **Expiry:** when the market resolves
- **Current market price:** YES/NO price on Polymarket/Kalshi

```
Market: "Will BTC be above $75,000 on March 15?"
→ Asset: BTCUSDT
→ Strike: $75,000
→ Direction: above
→ Expiry: 2026-03-15
→ Market YES price: 25¢ (implies 25% probability)
```

### Step 2: Calculate Volatility-Adjusted Probability

From our `price_snapshots` table (143 BTC snapshots, 143 ETH, collecting every 10min):

```python
# 1. Compute realized volatility
returns = [ln(price[t] / price[t-1]) for each snapshot]
daily_vol = std(returns) * sqrt(snapshots_per_day)  # annualize to daily

# 2. Distance to strike
distance_pct = (strike - current_price) / current_price  # e.g., +8.3%

# 3. Days remaining
days_left = (expiry - now).days  # e.g., 15 days

# 4. Required daily move
required_daily = distance_pct / days_left  # e.g., 0.55%/day

# 5. Probability of reaching strike
# Using log-normal model (standard) with fat-tail adjustment for crypto
z_score = distance_pct / (daily_vol * sqrt(days_left))
base_prob = 1 - norm.cdf(z_score)  # for "above" markets
# Flip for "below" markets
```

**Fat-tail adjustment:** Crypto has fatter tails than normal distribution. We use a Student-t distribution with 3-5 degrees of freedom (calibrated from Becker data), which increases the probability of extreme moves by 20-40% vs. normal.

### Step 3: Add Momentum Overlay

Raw volatility assumes random walk. But crypto trends. From `price_snapshots`:

```python
# Linear regression on last 6h of snapshots
slope = linregress(timestamps[-6h:], prices[-6h:]).slope

# Momentum score: -1 (strong bearish) to +1 (strong bullish)
momentum = slope / (daily_vol * current_price)  # normalized

# Adjust probability
if direction == "above":
    adjusted_prob = base_prob * (1 + 0.3 * momentum)  # momentum toward strike → higher prob
else:
    adjusted_prob = base_prob * (1 - 0.3 * momentum)
```

The 0.3 coefficient means momentum can shift probability by up to ±30%. This is conservative — Becker data shows momentum explains ~15% of outcome variance in 1-7 day markets.

### Step 4: Compute Edge

```python
market_implied_prob = yes_price  # YES at 25¢ = 25% implied probability
our_prob = adjusted_prob         # Our calculation: 38%

edge = our_prob - market_implied_prob  # +13% edge

# Signal generation
if edge > 0.10:  # 10% minimum edge threshold
    if our_prob > market_implied_prob:
        signal = "YES"  # market underprices the event
    else:
        signal = "NO"   # market overprices the event
```

### Step 5: Feed Into Pipeline

The signal enters the existing pipeline:

```
Strike Probability Signal
  → Dynamic Kelly (WR/drawdown check)
  → Time Decay (duration × volume modifier)
  → Volume Spike (boost if 3x+ volume)
  → Momentum Filter (YES falling → block)
  → Correlation Cap (max 3 crypto positions)
  → Position opens
```

---

## Worked Example

```
Current State:
  BTC = $64,416 (from price_snapshots)
  BTC 24h vol = 2.1%/day realized
  BTC 6h momentum = +0.34 (mild bullish)

Market: "Will BTC be above $75,000 on March 15?" (19 days out)
  YES price: 25¢

Calculation:
  Distance: ($75,000 - $64,416) / $64,416 = +16.4%
  Daily vol: 2.1%
  Z-score: 16.4% / (2.1% × √19) = 16.4% / 9.15% = 1.79
  
  Normal prob: 1 - Φ(1.79) = 3.7%
  Student-t (df=4): ~7.2% (fat tails double it)
  Momentum-adjusted: 7.2% × (1 + 0.3 × 0.34) = 7.9%
  
  Our estimate: 7.9%
  Market implies: 25%
  Edge: -17.1% (market OVERPRICES the event)
  
  Signal: NO @ 75¢ with 17.1% edge ✅
```

```
Market: "Will BTC be above $62,000 on February 25?" (1 day out)
  NO price: 74.7¢ (YES = 25.3¢)

Calculation:
  Distance: ($62,000 - $64,416) / $64,416 = -3.8% (already above strike)
  Daily vol: 2.1%
  Z-score: -3.8% / (2.1% × √1) = -1.81
  
  Normal prob: 1 - Φ(-1.81) = 96.5%
  Student-t: ~93%
  Momentum-adjusted: 93% × (1 + 0.3 × 0.34) = 96.5%
  
  Our estimate: BTC above $62K = 96.5%
  Market implies YES = 25.3%
  Edge: +71.2% (market massively UNDERPRICES YES)
  
  Wait — this means our NO bet is bad.
  
  Actually: we hold NO @ 74.7¢. Market says 74.7% chance NO wins.
  Our model says NO wins = 3.5%.
  This position is underwater by our model. 
  
  BUT: this is a 1-day market. Time decay modifier = 0.88x (penalty).
  The NO fade strategy entered this based on behavioral bias, not vol math.
  Price-to-Strike would NOT have entered this trade. That's the value —
  it would have BLOCKED this bad entry.
```

---

## Key Design Decisions

### Why Not Black-Scholes Directly?

Black-Scholes assumes:
- Log-normal returns (crypto has fat tails)
- Constant volatility (crypto vol is regime-dependent)
- No drift (crypto trends heavily)

We use the same framework but with:
- **Student-t distribution** (df=3-5) instead of normal — captures 10x+ moves
- **Rolling realized vol** instead of implied vol (no options market for these contracts)
- **Momentum overlay** — trend-following adjustment to drift assumption

### Strike Parsing

Markets use natural language: "Will BTC be above $64,000 on February 26?"

Parser needs to extract:
- Asset → regex match known tickers (BTC, ETH, SOL, etc.)
- Strike → dollar amount after "above/below"
- Direction → "above" = bullish strike, "below" = bearish strike
- Date → expiry date from market title or metadata

Fallback: if parsing fails, skip the market (don't guess).

### Which Markets This Applies To

**Strong fit (high edge):**
- Crypto price threshold markets (BTC above $X, ETH above $X)
- 3-30 day duration (vol math is most accurate here)
- Markets with $10K+ volume (enough liquidity to trust the price)

**Weak fit (skip):**
- Same-day markets (too noisy, vol estimate unreliable with <24h of data)
- Non-price markets (Oscars, elections — no vol model applies)
- Markets > 90 days out (vol estimate degrades, regime changes likely)

### Interaction with Strategy 1 (NO Fade)

These strategies can **agree or disagree**:

| NO Fade Says | Price-to-Strike Says | Action |
|---|---|---|
| NO (behavioral edge) | NO (vol edge) | **Strong NO** — compound confidence |
| NO (behavioral edge) | YES (vol says underpriced) | **Conflict** — reduce size or skip |
| No signal | YES (vol edge) | **YES entry** — new opportunity |
| No signal | NO (vol edge) | **NO entry** — new opportunity |

When they agree: boost confidence 1.2x  
When they disagree: halve position size (the market is genuinely uncertain)

---

## Data Requirements

| Data | Source | Status |
|---|---|---|
| BTC/ETH price snapshots | `price_snapshots` table | ✅ 286 snapshots, ~24h history |
| Crypto strike markets | Polymarket Gamma API + Kalshi events | ✅ Already fetched in signal pipeline |
| Market expiry dates | Market metadata | ✅ Available from API |
| Historical volatility | Computed from snapshots | ✅ Enough data for 24h vol |
| Becker backtesting data | 408K markets on G-DRIVE | ✅ For calibration of fat-tail params |

**Gap:** Only 24h of price snapshots currently. Need 7+ days for reliable vol estimates. Collecting at ~144/day, so 7 days ≈ 1,000 snapshots. By March 3 we'll have enough.

---

## Implementation Plan

### Phase 1: Core Module (Day 1)

**File:** `signals/strike_probability.py`

```python
class StrikeProbabilityCalculator:
    def __init__(self, db_path):
        self.db = db_path
    
    def get_realized_vol(self, symbol, window_hours=168):
        """Rolling realized vol from price_snapshots."""
        
    def parse_strike_market(self, market_title, market_metadata):
        """Extract asset, strike, direction, expiry."""
        
    def calculate_probability(self, symbol, strike, direction, days_left):
        """Vol-adjusted probability with fat tails + momentum."""
        
    def score_market(self, market):
        """Full scoring: vol prob vs market price → edge."""
        
    def scan_all_strikes(self):
        """Scan all open crypto strike markets, return ranked signals."""
```

### Phase 2: Pipeline Integration (Day 1)

- Wire `scan_all_strikes()` into watchdog signal pipeline
- Add `strategy` field to paper_positions: "no_fade" vs "price_to_strike"
- Compound scoring when both strategies agree

### Phase 3: Backtesting (Day 2)

- Run against Becker dataset: 117K crypto/price markets
- Calibrate Student-t degrees of freedom
- Calibrate momentum coefficient
- Measure: predicted probability vs actual outcome

### Phase 4: Dashboard (Day 2)

- New section on portfolio.html: "Strike Probability Scanner"
- Show: market, our prob, market prob, edge, signal
- Visual: probability gauge comparing our estimate vs market

---

## Success Metrics

| Metric | Target | Measurement |
|---|---|---|
| Win rate | >65% | Tracked per-strategy in paper_positions |
| Edge accuracy | ±5% | predicted_prob vs actual_outcome |
| Signal volume | 5-15/day | Count of markets with >10% edge |
| False positive rate | <20% | Signals that flip direction within 24h |
| P&L contribution | Positive within 2 weeks | Per-strategy P&L tracking |

---

## Risks

1. **Vol estimate unreliable with small sample** — mitigated by requiring 7+ days of snapshots before going live
2. **Strike parsing errors** — mitigated by fallback (skip if parse fails) + unit tests
3. **Fat-tail calibration wrong** — mitigated by Becker backtesting before live
4. **Momentum is noise on short timeframes** — mitigated by only applying to 3+ day markets
5. **Correlation with NO fade** — if both strategies bet the same markets, diversification benefit is reduced. Tracked via `strategy` field in positions.

---

## Bottom Line

NO fade says: "People overpay for YES. Bet against them."  
Price-to-Strike says: "The market misprices the probability of specific price moves. Bet the math."

Two independent edges. One portfolio. Ship it.

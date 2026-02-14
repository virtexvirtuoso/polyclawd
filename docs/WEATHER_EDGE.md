# Weather Market Edge Strategy

> Exploiting thin-liquidity weather prediction markets using superior forecast data.

---

## Thesis

Weather prediction markets on Polymarket are:
1. **Thinly traded** — ~$10K avg volume, no professional market makers
2. **Objectively resolvable** — temperature is a number, no interpretation
3. **Forecastable with high accuracy** — Open-Meteo/ECMWF gives ±1-2°F within 24h
4. **Priced by retail intuition** — participants guess rather than model

This creates a structural edge: we have better data than the market, and no one is competing for it.

---

## Market Landscape (Feb 2026)

| Metric | Value |
|--------|-------|
| Platform | Polymarket (sole venue) |
| Active weather markets | 98 |
| Priced markets (0.01-0.99) | 69 |
| Total volume | ~$1M |
| Avg volume per market | ~$10K |
| Market types | Temperature high/low, ranges, exact |
| Cities covered | NYC, London, Buenos Aires, Dallas, Wellington, Toronto, Seattle + others |
| Resolution | Daily (most expire same-day or next-day) |

**Kalshi weather (KXTEMPD, KXWIND, KXRAIND) is discontinued** — zero open weather markets as of Feb 2026.

---

## Data Advantage

### Open-Meteo (Primary Source)

| Timeframe | Data Type | Accuracy | Cost |
|-----------|-----------|----------|------|
| Same-day | Hourly temp/precip/wind/humidity | ±1-2°F | Free |
| Next-day | Hourly forecast | ±2-3°F | Free |
| 3-7 days | Daily high/low/precip | ±3-5°F | Free |
| Historical | Actuals for resolution verification | Exact | Free |

Open-Meteo aggregates the world's best models:
- **ECMWF IFS** — #1 global forecast model, 9km resolution
- **GFS** — US standard model
- **ICON** — German high-resolution model
- **GraphCast** — Google DeepMind AI weather model

No API key required. Unlimited calls.

### Forecast Accuracy by Horizon

```
Hours until resolution → Forecast accuracy

  0-6h:   ±1°F  → 95%+ probability estimate accuracy
  6-24h:  ±2°F  → 85-95% accuracy
  24-48h: ±3°F  → 75-85% accuracy
  3-7d:   ±5°F  → 60-75% accuracy
  7d+:    ±8°F  → below trading threshold
```

**Key insight:** Within 24 hours, weather forecasts are more accurate than almost any other predictable event. A market asking "Will NYC high exceed 55°F tomorrow?" when the forecast says 48°F is essentially free money.

---

## Strategy: Market Maker on Thin Books

### Why Market Making (Not Market Taking)

Traditional prediction market strategy: find a mispriced order and take it. But on thin weather markets:

- Order books are sparse — few orders at any price
- Spreads are wide — 10-30¢ between bid and ask
- Retail participants post imprecise limit orders

**We flip the script:** instead of taking bad prices, we post good ones.

### Execution Model

```
1. Open-Meteo says NYC high tomorrow = 48°F (±2°F, high confidence)

2. Market: "Will NYC high exceed 55°F on Feb 15?"
   - Current book: no bids, one ask at 40¢
   - Our fair value: ~10¢ (forecast says 7°F below threshold)

3. We post: NO limit order at 12¢ (selling YES at 88¢)
   - If filled, we risk 12¢ to win 88¢
   - With 90%+ confidence the answer is NO

4. Wait for retail to take our order
   - Someone sees the market, thinks "maybe it'll warm up"
   - They buy YES at our 88¢ offer (we get filled on NO at 12¢)

5. Market resolves NO → we profit 88¢ per contract
```

### Edge Tiers

| Forecast Margin | Confidence | Fair Value (YES for "above X") | Min Edge to Trade |
|----------------|------------|-------------------------------|-------------------|
| Forecast 8°F+ above threshold | Very High | 90¢+ | 5¢ |
| Forecast 5-8°F above | High | 75-90¢ | 10¢ |
| Forecast 2-5°F above | Medium | 55-75¢ | 15¢ |
| Forecast 0-2°F above | Low | 40-55¢ | 20¢+ (or skip) |
| Forecast below threshold | Flip (bet NO) | Mirror values | Same |

### Position Sizing

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Max per market | $25 | Thin liquidity, don't dominate the book |
| Max concurrent | 10 | Diversify across cities/dates |
| Max daily exposure | $100 | Until track record established |
| Kelly fraction | 1/8 Kelly | Uncalibrated, conservative |
| Min confidence | 75% | Only trade high-certainty forecasts |
| Min edge | 10% | Cover spread + execution risk |

---

## Resolution Certainty Scoring

For weather, we can compute near-exact resolution certainty:

```python
def weather_certainty(forecast_temp, threshold, hours_until, comparison="above"):
    """
    Returns certainty score 0-100 based on forecast vs threshold.
    
    Same-day with 8°F margin = 95+ certainty
    Next-day with 5°F margin = 85+ certainty
    """
    margin = forecast_temp - threshold  # positive = above threshold
    
    if comparison == "below":
        margin = -margin
    
    # Time-based accuracy band
    if hours_until <= 6:
        accuracy = 1.5  # ±1.5°F
    elif hours_until <= 24:
        accuracy = 2.5
    elif hours_until <= 48:
        accuracy = 4.0
    else:
        accuracy = 6.0
    
    # How many accuracy bands away from threshold?
    z_score = abs(margin) / accuracy
    
    if z_score > 3:
        certainty = 97
    elif z_score > 2:
        certainty = 92
    elif z_score > 1.5:
        certainty = 85
    elif z_score > 1:
        certainty = 75
    elif z_score > 0.5:
        certainty = 60
    else:
        certainty = 50  # coin flip, don't trade
    
    # Direction: is the forecast on the right side?
    if margin > 0 and comparison == "above":
        side = "YES"
    elif margin < 0 and comparison == "above":
        side = "NO"
        certainty = certainty  # same certainty for NO
    
    return {"certainty": certainty, "side": side, "z_score": z_score}
```

---

## Market Parsing

Weather markets follow predictable title patterns:

| Pattern | Example | Extraction |
|---------|---------|------------|
| "above X°F" | "Will the highest temperature in NYC be above 55°F on Feb 15?" | city=NYC, date=Feb 15, comparison=above, threshold=55°F |
| "below X°F" | "Will the highest temperature in London be 46°F or below on Feb 1?" | city=London, date=Feb 1, comparison=below, threshold=46°F |
| "between X-Y°F" | "Will the highest temperature in London be between 66-67°F on May 10?" | city=London, date=May 10, comparison=between, range=66-67°F |
| "be X°C" | "Will the highest temperature in Buenos Aires be 30°C on Feb 12?" | city=Buenos Aires, date=Feb 12, comparison=exact, threshold=86°F |
| "X°C or higher" | "Will the highest temperature in Toronto be -1°C or higher on Feb?" | city=Toronto, comparison=above, threshold=30.2°F |

**28 cities mapped** with coordinates and timezones for Open-Meteo queries.

---

## Why Thin Liquidity Is Our Friend

### Conventional Wisdom
> "Don't trade illiquid markets — you can't get in or out."

### Our Reality
Weather markets are different because:

1. **We hold to resolution.** No need to exit early. These are 1-3 day markets that resolve to a verifiable number. Liquidity for exit is irrelevant.

2. **Small size is correct.** $5-25 per market matches the liquidity naturally. We're not trying to deploy $10K into a $10K market.

3. **No sophisticated competition.** Quant firms aren't building weather models for $10K Polymarket markets. The ROI on their infrastructure doesn't justify it. For us, it's a Python script calling a free API.

4. **Wide spreads = bigger edge.** On a liquid market with 1¢ spreads, our 10% forecast edge might net 5¢. On a thin market with 20¢ spreads, the same edge nets 15¢+.

5. **Market maker premium.** By posting limit orders, we capture the spread instead of paying it. We're the house, not the gambler.

### The Math

```
Liquid crypto market:
  Our edge: 10%
  Spread cost: 2%
  Net edge: 8%
  Competition: High (other bots, quant firms)
  
Thin weather market:
  Our edge: 15-30% (better data advantage)
  Spread cost: 0% (we're the market maker)
  Net edge: 15-30%
  Competition: Near zero
```

---

## Implementation Architecture

```
Open-Meteo API (hourly + daily)
    │
    ▼
Weather Scanner (signals/weather_scanner.py)
    │ Parses market titles → city, date, threshold
    │ Fetches forecast → computes fair value
    │ Calculates edge vs market price
    │
    ▼
Signal Aggregator
    │ Source: "weather_scanner"
    │ IC tracking from day 1
    │
    ▼
Portfolio Engine (limit order mode)
    │ Posts limit orders at our fair value + margin
    │ Tracks fills, holds to resolution
    │
    ▼
Resolution Scanner (5min)
    │ Fetches actual temperature on resolution day
    │ Auto-resolves trades
    │
    ▼
IC Tracker + Calibrator
    │ Measures forecast accuracy vs outcomes
    │ Adjusts confidence over time
```

### What's Built

| Component | Status |
|-----------|--------|
| Weather scanner (daily + hourly) | ✅ Deployed |
| Open-Meteo integration | ✅ Working |
| 28 cities mapped | ✅ |
| Title parser (above/below/between/exact, °F/°C) | ✅ |
| Fair value calculator | ✅ |
| API endpoint `/api/signals/weather` | ✅ |
| IC tracking wired | ✅ (through signal aggregator) |

### What's Needed

| Component | Effort | Priority |
|-----------|--------|----------|
| Limit order mode in portfolio engine | 1 day | High |
| Polymarket order placement (Simmer SDK) | 1 day | High |
| Actual temperature fetcher for resolution | 0.5 day | High |
| Watchdog integration (scan every 10min) | 0.5 day | Medium |
| Historical backtest (Open-Meteo historical forecast API) | 1-2 days | Medium |
| Multi-model ensemble (combine ECMWF + GFS + ICON) | 1 day | Low |

---

## Risk Management

### Weather-Specific Risks

| Risk | Mitigation |
|------|------------|
| Forecast bust (model wrong by 10°F+) | Rare (<2% for same-day). Max $25/trade limits damage. |
| Resolution dispute | Temperature is objective. Use same source (NWS/official) as market resolution. |
| All trades same city/day | Max 3 markets per city per day. Diversify across cities. |
| Sudden weather change (front moves faster) | Hourly re-scan every 10min detects shifts. Cancel unfilled orders if forecast changes. |
| Market never fills | No loss — limit orders expire unfilled. Cost = $0. |
| Polymarket settlement delay | Standard — not weather-specific. Track resolution. |

### Correlation Management

Weather markets across cities are partially correlated (same weather system affects multiple cities). Limit:
- Max 3 same-region markets (e.g., NYC + Boston + Philly)
- Max $50 on correlated markets
- Diversify: US East + US West + International

---

## Expected Performance

### Conservative Estimate

| Metric | Value | Basis |
|--------|-------|-------|
| Markets traded/day | 3-5 | 69 active, ~30% have edge, ~50% fill |
| Avg edge per trade | 15% | Weather data advantage + market maker spread |
| Win rate | 80-90% | Same-day forecasts are extremely accurate |
| Avg trade size | $10 | Conservative, thin markets |
| Daily profit | $3-8 | 4 trades × $10 × 15% edge × 85% WR |
| Monthly profit | $90-240 | Steady, low-variance income stream |
| Max drawdown | $50 | Single bad weather day, multiple correlated losses |
| Sharpe (annualized) | 2.5-4.0 | High win rate + consistent small gains |

### Why This Is Conservative

- Assumes 50% fill rate (market maker orders don't always fill)
- Assumes only same-day/next-day trades (highest confidence)
- Doesn't account for edge from 3-7 day forecasts
- Doesn't include potential Kalshi weather relaunch

### Comparison to Crypto Strategy

| Dimension | Crypto Markets | Weather Markets |
|-----------|---------------|-----------------|
| Edge source | Category mispricing + momentum | Superior forecast data |
| Competition | Medium (bots, some quants) | Very low (retail only) |
| Win rate | 70-80% | 80-90% |
| Resolution speed | Daily | Daily |
| Liquidity | $10K-$1M/market | $1K-$20K/market |
| Max trade size | $25-100 | $5-25 |
| Correlation risk | High (all crypto-linked) | Low (weather is independent) |
| Data cost | Free (CoinGecko, Virtuoso) | Free (Open-Meteo) |

**Weather is the diversification play.** Uncorrelated with crypto, high certainty, lower variance. Crypto is the volume play. Together they smooth the equity curve.

---

## Backtest Opportunity

Open-Meteo has a **Historical Forecast API** — we can pull what the forecast said on any past date and compare against actual temperatures. This enables:

1. Pull all resolved Polymarket weather markets (from their API)
2. For each: what did Open-Meteo forecast on the day before resolution?
3. Compare forecast-based fair value vs market price at that time
4. Calculate hypothetical P&L

This gives us a backtest before risking any capital. Estimated effort: 1-2 days.

---

## Timeline

| Week | Action |
|------|--------|
| Week 1 | Scanner live, paper trading limit orders, collect fills |
| Week 2 | First resolutions, IC data accumulating |
| Week 3 | 15+ resolved trades, preliminary calibration |
| Week 4 | Backtest using historical forecast API |
| Week 5-6 | If IC > 0.05 and win rate > 75%: deploy $100 live |
| Month 3 | Scale to full $250 allocation if profitable |

---

*Weather markets are the quiet edge. No one's fighting for them because the individual markets are small. But 69 active markets × 15% edge × daily resolution = a steady, uncorrelated alpha stream that compounds alongside our crypto strategy.*

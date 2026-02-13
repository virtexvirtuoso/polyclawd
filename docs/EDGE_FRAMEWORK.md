# Polyclawd Edge Framework

> The definitive guide to where prediction market alpha comes from and how we capture it.
> 
> Last updated: 2026-02-13

---

## Core Principle

Prediction market prices are probabilities. **Edge = knowing the true probability better than the market.** Every strategy must answer: *why do we know more than the current price reflects?*

---

## Table of Contents

1. [Edge Taxonomy](#edge-taxonomy)
2. [Strategy 1: Resolution Certainty Harvesting](#strategy-1-resolution-certainty-harvesting)
3. [Strategy 2: News-to-Trade Speed](#strategy-2-news-to-trade-speed)
4. [Strategy 3: Cross-Platform Velocity Arbitrage](#strategy-3-cross-platform-velocity-arbitrage)
5. [Strategy 4: Category Mispricing (Current)](#strategy-4-category-mispricing-current)
6. [Strategy 5: Time Decay Harvesting](#strategy-5-time-decay-harvesting)
7. [Strategy 6: Expiry Pressure Trading](#strategy-6-expiry-pressure-trading)
8. [Strategy 7: AI Model Market Specialist](#strategy-7-ai-model-market-specialist)
9. [Edge Decay Model](#edge-decay-model)
10. [Speed Requirements](#speed-requirements)
11. [Risk Framework](#risk-framework)
12. [Implementation Priority](#implementation-priority)
13. [Performance Metrics](#performance-metrics)

---

## Edge Taxonomy

Every prediction market edge falls into one of four categories:

| Type | Description | Decay Speed | Our Coverage |
|------|-------------|-------------|-------------|
| **Information** | You know something the market doesn't | Minutes | ❌ Not capturing |
| **Analytical** | You compute the true probability better | Hours–Days | ✅ Category mispricing |
| **Structural** | Market mechanics create inefficiency | Persistent | ⚠️ Partial (cross-platform) |
| **Temporal** | Time-based patterns in pricing | Hours | ❌ Not capturing |

**Key insight:** Our current pipeline is almost entirely *analytical* edge (historical category errors). The three fastest-decaying edge types — information, structural, temporal — are untouched. That's where the alpha is.

---

## Strategy 1: Resolution Certainty Harvesting

### The Edge
Markets often continue trading at 80-90¢ when the outcome is already ~100% knowable. The answer exists in public data, but traders haven't checked or haven't acted.

### Examples
| Market | Resolution Check | Edge Window |
|--------|-----------------|-------------|
| "ETH up or down Feb 13?" | Current ETH price vs 24h ago | Final 2-4 hours |
| "Google #1 on Arena Feb 28?" | Current Arena leaderboard | Final 3-5 days |
| "Will it rain in NYC tomorrow?" | Current weather radar/forecast | Final 6-12 hours |
| "BTC above $75K in Feb?" | Current BTC price + days remaining | Ongoing |
| "Spotify #1 artist this week?" | Current Spotify charts | Final 1-2 days |

### Why It Works
- Retail traders set positions and forget — they don't monitor resolution data
- Market makers don't exist on prediction platforms (no one's arbing toward fair value in real-time)
- Resolution data is free and public but requires domain-specific knowledge to interpret
- The gap between "outcome is knowable" and "market reflects it" is consistently minutes to hours

### Implementation
```
Resolution Certainty Scanner:
1. For each open market, identify the resolution data source
2. Fetch current state of that data source
3. Compute P(outcome | current data)
4. If P > 95% and market price < 90¢ → BUY signal
5. If P < 5% and market price > 10¢ → SELL signal
6. Scan every 5 minutes (this is time-sensitive)
```

### Data Sources to Wire
| Category | Source | API/Method |
|----------|--------|-----------|
| Crypto prices | CoinGecko, Binance | REST API |
| AI model rankings | Arena leaderboard | Web scrape (built ✅) |
| Weather | OpenWeatherMap, NWS | REST API |
| Sports scores | ESPN | REST API |
| Spotify charts | Spotify API | REST API |
| Election/politics | Polling aggregators | Web scrape |
| Economic data | FRED, BLS | REST API |

### Expected Edge: 10-25% per trade
### Win Rate: 90%+ (we only trade near-certainties)
### Risk: Low (outcome is already ~known)
### Priority: 🔴 **#1 — Build first**

---

## Strategy 2: News-to-Trade Speed

### The Edge
When news breaks that directly affects a prediction market, there's a window (minutes to hours) before the market price fully adjusts. The trader who reacts first captures the entire move.

### Examples
| News Event | Affected Market | Expected Move | Window |
|-----------|----------------|---------------|--------|
| "Google announces Gemini 4.0" | "Google #1 on Arena" | +20-40% | 30-60 min |
| "Fed announces rate decision" | GDP/inflation markets | ±15% | 5-15 min |
| "Hurricane upgraded to Cat 5" | Weather/damage markets | +30% | 1-2 hours |
| "Celebrity endorsement tweet" | Entertainment markets | ±10% | 15-30 min |
| "Earnings beat/miss" | Company-specific markets | ±20% | 10-30 min |

### Why It Works
- Prediction market traders are part-time — most aren't watching news feeds
- No algorithmic market makers to absorb information instantly (unlike equities)
- Polymarket's orderbook is thin — even small informed flow moves prices significantly
- News APIs deliver structured data faster than humans can read and react

### Implementation
```
News Event Matcher:
1. Monitor RSS feeds, Google News API, Twitter/X for keywords
2. Match headlines to open markets via NLP/keyword mapping
3. Assess directional impact (positive/negative for which outcome)
4. If high-confidence match → flag for immediate evaluation
5. Cross-reference with current market price
6. If price hasn't moved yet → signal with urgency flag
```

### Keyword-to-Market Mapping
```python
MARKET_KEYWORDS = {
    "arena|leaderboard|chatbot arena": "AI model markets",
    "bitcoin|btc|crypto rally|crypto crash": "BTC price markets",
    "ethereum|eth|merge|upgrade": "ETH price markets",
    "fed|fomc|rate cut|rate hike": "Economic/macro markets",
    "hurricane|tornado|flood": "Weather damage markets",
    "spotify|billboard|grammy": "Entertainment markets",
    "elon|trump|biden": "Political/personality markets",
}
```

### News Sources
| Source | Latency | Coverage |
|--------|---------|----------|
| Google News RSS | 1-5 min | Broad |
| Reddit (relevant subs) | 1-10 min | Crypto, politics, tech |
| Twitter/X firehose | Seconds | Breaking news |
| Company blogs (Anthropic, Google, OpenAI) | Minutes | AI model releases |
| FRED/BLS data releases | Scheduled | Economic data |
| NWS alerts | Real-time | Weather events |

### Expected Edge: 15-40% on matched events
### Win Rate: 65-75% (news interpretation isn't always clear)
### Risk: Medium (news can be misleading or already priced in)
### Priority: 🟡 **#2 — Build after resolution scanner**

---

## Strategy 3: Cross-Platform Velocity Arbitrage

### The Edge
When the same event is traded on Kalshi and Polymarket, price moves on one platform lag the other by minutes to hours. The platform that moves first has new information; the stale platform is temporarily mispriced.

### Why Current Approach Is Insufficient
We currently detect static divergence: "Kalshi says 40%, Polymarket says 55%."
We do NOT detect: "Polymarket just moved from 45% to 55% in the last hour, Kalshi is still at 40%."

The velocity matters more than the spread.

### Implementation
```
Cross-Platform Velocity Monitor:
1. Store price snapshots every scan (30 min)
2. For matched markets, compute Δprice/Δtime on each platform
3. If Platform A moved >5% but Platform B is flat → signal on B
4. Direction: follow the mover (Platform A's direction)
5. Confidence scales with: move size, volume behind move, historical correlation
```

### Price Snapshot Schema
```sql
CREATE TABLE price_snapshots (
    timestamp TEXT,
    market_id TEXT,
    platform TEXT,         -- 'kalshi' or 'polymarket'
    price REAL,
    volume INTEGER,
    matched_market_id TEXT -- ID on the other platform
);
```

### Velocity Signal Logic
```
velocity_A = (price_now_A - price_30min_ago_A) / price_30min_ago_A
velocity_B = (price_now_B - price_30min_ago_B) / price_30min_ago_B

if abs(velocity_A) > 0.05 and abs(velocity_B) < 0.01:
    signal = direction_of_A on platform B
    edge = abs(velocity_A) * historical_correlation
```

### Expected Edge: 5-15% per trade
### Win Rate: 60-70%
### Risk: Medium (divergence can persist or widen)
### Priority: 🟡 **#3 — Builds on existing cross-platform matching**

---

## Strategy 4: Category Mispricing (Current)

### The Edge
Historical analysis of 3.75M markets shows certain categories are consistently mispriced. FX markets average 45% error. Entertainment (Spotify) averages 55-60% error. We bet against these systematic biases.

### Current Implementation ✅
- `signals/mispriced_category_signal.py` — scans Kalshi + Polymarket
- 906 categories with >10% average error identified
- Parameter-optimized: vol ≥ 5000, edge ≥ 5%, 30-day max, 1/8 Kelly
- Backtest: 79.6% WR, 1.80 Sharpe across 18K+ trades

### Limitations
- **Static edge** — based on historical patterns, not real-time information
- **Slow decay** — category biases persist for weeks/months
- **No urgency** — 30-min scan is more than sufficient
- **Diminishing returns** — as prediction markets mature, category mispricing will shrink

### Enhancement Path
- Virtuoso MCP confirmation layer (partially built, MCP integration blocked)
- Dynamic category weight updates based on recent resolution accuracy
- Seasonal adjustments (weather markets more mispriced in winter)

### Expected Edge: 5-15% per trade
### Win Rate: 75-80%
### Risk: Low-Medium
### Priority: 🟢 **Active — maintain and refine**

---

## Strategy 5: Time Decay Harvesting

### The Edge
Markets priced at 15-30% for unlikely outcomes bleed value as expiry approaches, similar to options theta decay. The probability of a surprise outcome decreases with each passing day of no news.

### The Math
```
If market = 25% with 30 days to expiry:
  - No news after 15 days → fair value drops to ~15%
  - No news after 25 days → fair value drops to ~8%
  - Each quiet day is evidence AGAINST the unlikely outcome

Theta = daily probability decay from "nothing happened"
```

### Why It Works
- Prediction markets don't have market makers who reprice theta daily
- Retail traders anchor to their entry price and don't adjust for time passing
- "Nothing happened" is itself information, but it's not dramatic enough to trigger repricing
- This is the prediction market equivalent of selling options premium

### Implementation
```
Time Decay Scanner:
1. Find markets with:
   - Price 10-35% (unlikely outcome)
   - > 7 days remaining
   - No significant news in last 48h
   - Historical category suggests unlikely outcome
2. Track daily: has anything changed?
3. If no material change for 3+ consecutive days → sell (NO) signal
4. Exit when price hits < 5% or on any material news
```

### Decay Rate Model
```
daily_theta = base_probability * (1 / days_remaining) * no_news_factor

Example: Google AI market
  - Current price: 25% (Google YES)
  - Days remaining: 15
  - No Google model announcement in 7 days
  - daily_theta ≈ 0.25 * (1/15) * 1.2 = 2% per day
  - In 5 quiet days: 25% → ~15%
```

### Expected Edge: 2-5% per day held (compounds)
### Win Rate: 80%+ (most unlikely things stay unlikely)
### Risk: Tail risk — the one time a surprise happens, you lose big
### Priority: 🟡 **#4 — Low-hanging fruit, complements existing signals**

---

## Strategy 6: Expiry Pressure Trading

### The Edge
In the final hours before market resolution, losing traders capitulate and winning traders take profit. This creates predictable price patterns:
- Losing positions get dumped → price overshoots toward resolution
- Thin liquidity near expiry amplifies moves
- Late retail entries chase momentum

### Why It Works
- Identical to options expiry pinning/gamma squeeze effects
- Prediction markets have no circuit breakers or market makers to smooth flow
- Polymarket orderbooks are especially thin near expiry (<$10K depth)
- Most traders learned from crypto — they panic sell, they FOMO buy

### Implementation
```
Expiry Pressure Scanner:
1. Find markets expiring in < 6 hours
2. Check orderbook depth (if available via Polymarket API)
3. Identify current price trend (accelerating toward one side?)
4. If momentum + thin book + clear resolution direction → ride the wave
5. Position size: smaller (higher risk, shorter duration)
```

### Expected Edge: 5-10% in final hours
### Win Rate: 60-65% (chaotic, harder to predict)
### Risk: High (fast-moving, thin liquidity)
### Priority: 🔵 **#5 — Advanced, build after core strategies proven**

---

## Strategy 7: AI Model Market Specialist

### The Edge
We have domain expertise + automated monitoring that most prediction market traders lack:
- Real-time Arena leaderboard tracking (score gaps, vote counts, new models)
- Understanding of model release cycles and corporate patterns
- Historical Arena data for trend analysis
- Cross-reference with company financials and hiring signals

### Current Implementation ✅
- `signals/ai_model_tracker.py` — Arena scraper + SQLite snapshots
- Company classification for 11 AI labs
- 6-hourly automated snapshots
- API endpoint: `/api/signals/ai-models`

### Enhancement Path
- Model release detection (GitHub, blog RSS, HuggingFace)
- Vote velocity tracking (flag unstable rankings)
- Head-to-head model comparison markets
- "Best coding/reasoning model" category-specific markets
- Corporate signal tracking (earnings calls, hiring patterns)

### Expected Edge: 10-20% on AI markets specifically
### Win Rate: 70-80%
### Risk: Low-Medium (we have strong domain knowledge)
### Priority: 🟢 **Active — expand data sources**

---

## Edge Decay Model

All edges decay over time as markets mature. Understanding the decay rate determines urgency:

```
Edge Lifespan by Type:

Information edge (news):     ████░░░░░░  Minutes to hours
Resolution certainty:        ██████░░░░  Hours to days  
Cross-platform velocity:     ████░░░░░░  Minutes to hours
Category mispricing:         ████████░░  Weeks to months
Time decay:                  ██████████  Persistent (structural)
Expiry pressure:             ██░░░░░░░░  Final hours only
AI model specialist:         ████████░░  Days to weeks
```

**Implication:** Our current 30-min scan is fine for category mispricing and time decay. But information, resolution certainty, and cross-platform velocity need 5-min or faster scanning.

---

## Speed Requirements

| Strategy | Required Scan Frequency | Current | Gap |
|----------|------------------------|---------|-----|
| Resolution Certainty | Every 5 min | ❌ Not built | **Build with 5-min cron** |
| News-to-Trade | Real-time (push) | ❌ Not built | **RSS polling every 5 min** |
| Cross-Platform Velocity | Every 5-15 min | 30 min (partial) | **Add price snapshots** |
| Category Mispricing | Every 30 min | ✅ 30 min | None |
| Time Decay | Every 6-12 hours | ❌ Not built | **Daily scan sufficient** |
| Expiry Pressure | Every 5 min (near expiry) | ❌ Not built | **Conditional 5-min scan** |
| AI Model Specialist | Every 6 hours | ✅ 6 hours | None |

### Tiered Scan Architecture
```
┌─────────────────────────────────────────────┐
│              SCAN TIERS                      │
├─────────────────────────────────────────────┤
│                                              │
│  TIER 1 — Every 5 min (time-critical)       │
│  ├── Resolution certainty checker            │
│  ├── Expiry pressure (markets < 6h out)      │
│  └── Shadow trade resolution                 │
│                                              │
│  TIER 2 — Every 30 min (analytical)          │
│  ├── Category mispricing scan                │
│  ├── Cross-platform velocity check           │
│  ├── News event matcher                      │
│  └── Paper portfolio processing              │
│                                              │
│  TIER 3 — Every 6 hours (monitoring)         │
│  ├── Arena leaderboard snapshot              │
│  ├── Time decay assessment                   │
│  └── Portfolio rebalancing check             │
│                                              │
│  TIER 4 — Daily (reporting)                  │
│  ├── Performance summary                     │
│  ├── Edge quality metrics                    │
│  └── Strategy weight adjustment              │
│                                              │
└─────────────────────────────────────────────┘
```

---

## Risk Framework

### Position Sizing
```
Base: 1/8 Kelly
Adjustment by strategy:

Resolution Certainty:  1/4 Kelly (high confidence, known outcome)
Category Mispricing:   1/8 Kelly (standard)
Cross-Platform Arb:    1/8 Kelly (standard)
News-to-Trade:         1/16 Kelly (uncertain interpretation)
Time Decay:            1/8 Kelly (standard, but tail risk)
Expiry Pressure:       1/16 Kelly (high variance)
AI Model Specialist:   1/8 Kelly (domain expertise)
```

### Correlation Management
- Max 3 positions in same category
- Max 2 positions on same underlying event
- Max $75 total exposure (3 × $25)
- Crypto-correlated markets count as one basket

### Stop-Loss Rules
- No hard stop-losses (prediction markets are binary — either you're right or wrong)
- Exit if thesis invalidated (e.g., news breaks against your position)
- Exit if edge drops below 3% (price moved toward your position, take profit)
- Mandatory exit 1 hour before resolution (avoid illiquid final minutes)

### Drawdown Limits
- Max drawdown: 30% of bankroll ($150 from $500)
- If hit: reduce to 1/16 Kelly for 1 week
- If 40% drawdown: pause all new positions for 48h
- If 50% drawdown: full stop, review all strategies

---

## Implementation Priority

```
Phase 1 — NOW (Week 1-2)                    Status
├── Category mispricing                       ✅ Live
├── AI model specialist                       ✅ Live
├── Paper portfolio                           ✅ Live
├── Shadow trade tracking                     ✅ Live
└── Resolution certainty scanner              🔴 BUILD NEXT

Phase 2 — SOON (Week 3-4)
├── News event matcher (RSS/Google News)      🟡 Queued
├── Cross-platform velocity tracking          🟡 Queued
├── Time decay scanner                        🟡 Queued
└── Price snapshot storage                    🟡 Queued

Phase 3 — LATER (Month 2)
├── Expiry pressure trading                   🔵 Planned
├── AI model release detection                🔵 Planned
├── Full Virtuoso MCP integration             🔵 Blocked
└── Live trading (post paper validation)      🔵 Pending

Phase 4 — FUTURE (Month 3+)
├── Multi-strategy portfolio optimization     ⬜ Designed
├── Dynamic Kelly adjustment                  ⬜ Designed
├── Real-time Twitter/X sentiment             ⬜ Concept
└── Custom market making                      ⬜ Concept
```

---

## Performance Metrics

### Strategy-Level Tracking
For each strategy, track independently:
- Win rate
- Average edge at entry
- Average P&L per trade
- Sharpe ratio
- Max drawdown
- Average time in position
- Edge decay rate (does the edge shrink while we're in the trade?)

### Portfolio-Level Metrics
- Total bankroll
- Cumulative P&L
- Portfolio Sharpe (across all strategies)
- Strategy contribution (which strategy drives most P&L?)
- Correlation between strategy returns

### Edge Quality Score
```
Edge Quality = (Win Rate × Avg Win) - ((1 - Win Rate) × Avg Loss)
                ÷ Standard Deviation of Returns

Minimum viable: Edge Quality > 0.5
Good: > 1.0
Excellent: > 1.5
```

### Monthly Review Checklist
- [ ] Is each strategy's edge holding or decaying?
- [ ] Are there new market categories worth adding?
- [ ] Have any data sources degraded or gone offline?
- [ ] Is position sizing appropriate given recent variance?
- [ ] Are there new platforms to add (Manifold, PredictIt if unblocked)?
- [ ] Should any strategy be retired or reduced?

---

## Key Insight

> **The biggest untapped edge is resolution certainty harvesting.** We're literally sitting on the answer for several markets (Arena leaderboard, current crypto prices, weather data) and checking every 30 minutes instead of matching the answer to the market price in real-time. This is like having tomorrow's newspaper and reading it once a day.

Build the resolution certainty scanner. Everything else follows.

---

*Polyclawd Edge Framework v1.0 — Virtuoso Crypto*

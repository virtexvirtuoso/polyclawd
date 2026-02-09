# Polyclawd Data Sources

Documentation of all 13 signal sources and external integrations.

---

## Overview

| # | Source | Type | API/Method | Update Freq | Weight |
|---|--------|------|------------|-------------|--------|
| 1 | News (Google) | Sentiment | Google News RSS | Real-time | MEDIUM |
| 2 | News (Reddit) | Sentiment | Reddit JSON API | Real-time | LOW |
| 3 | Volume Spikes | Technical | Polymarket API | 5 min | LOW |
| 4 | Smart Money | On-chain | Polymarket Data API | 10 min | MEDIUM |
| 5 | Inverse Whale | On-chain | Polymarket Data API | 10 min | HIGH |
| 6 | Kalshi | Cross-platform | Kalshi REST API | 15 min | MEDIUM |
| 7 | Manifold | Leading indicator | Manifold REST API | 5 min | MEDIUM |
| 8 | Metaculus | Forecasting | Metaculus REST API | 1 hour | LOW |
| 9 | PredictIt | Cross-platform | PredictIt REST API | 15 min | MEDIUM |
| 10 | Betfair | Sharp odds | The Odds API | 30 min | HIGH |
| 11 | PolyRouter | Aggregator | PolyRouter GraphQL | 10 min | HIGH |
| 12 | Vegas | Sharp lines | The Odds API + Scraping | 30 min | HIGH |
| 13 | ESPN | Game odds | ESPN BET API | 5 min | MEDIUM |
| 14 | Simmer | Divergence | Simmer REST API | 5 min | MEDIUM |

---

## Signal Sources

### 1. News - Google News

**Purpose:** Breaking news sentiment detection for active markets.

**Implementation:**
- **Source file:** `signals/news_signal.py`
- **API:** Google News RSS (`news.google.com/rss/search`)
- **Method:** RSS feed parsing
- **Rate limit:** 60s between fetches per query
- **Cache:** `~/.openclaw/polyclawd/news_cache.json`

**How it works:**
1. Extract keywords from market titles (pattern matching + dynamic extraction)
2. Fetch Google News RSS for each keyword
3. Filter to articles < 30 minutes old
4. Analyze sentiment using keyword detection
5. Generate signal if sentiment is bullish/bearish

**Sentiment keywords:**
- Bullish: surge, soar, rally, approved, wins, breakthrough
- Bearish: crash, plunge, rejected, lawsuit, hack, ban

---

### 2. News - Reddit

**Purpose:** Social sentiment from high-engagement posts.

**Implementation:**
- **Source file:** `signals/news_signal.py`
- **API:** Reddit JSON API (no auth required)
- **Subreddits:** cryptocurrency, bitcoin, politics, nfl, nba
- **Rate limit:** 60s between fetches

**Filters:**
- Score > 100 upvotes
- Age < 60 minutes
- Upvote ratio > 0.7

---

### 3. Volume Spikes

**Purpose:** Detect unusual trading activity using statistical analysis.

**Implementation:**
- **Source file:** `api/routes/signals.py`
- **API:** Gamma API (`gamma-api.polymarket.com`)
- **Method:** Z-score calculation

**Algorithm:**
```python
z_score = (current_volume - mean_volume) / std_volume
if z_score >= 2.0:
    generate_signal()
```

**Signal confidence:** `z_score * 10` (capped at 100)

---

### 4. Smart Money

**Purpose:** Track net whale buying/selling weighted by accuracy.

**Implementation:**
- **Source file:** `api/routes/signals.py`
- **API:** Polymarket Data API (`data-api.polymarket.com`)
- **Config:** `config/whale_config.json`

**Algorithm:**
1. Load tracked whale addresses from config
2. Fetch positions for each whale
3. Weight by whale's historical accuracy
4. Calculate net weighted flow per market
5. Generate signal when flow exceeds threshold

**Signal strength:**
- STRONG: Net weighted flow > $2,000
- MODERATE: Net weighted flow > $500
- WEAK: Net weighted flow < $500

---

### 5. Inverse Whale

**Purpose:** Fade positions of consistently losing traders.

**Implementation:**
- **Source file:** `api/routes/signals.py`
- **API:** Polymarket Data API
- **Stats file:** `data/predictor_stats.json`

**Selection criteria:**
- Minimum 10 tracked predictions
- Accuracy < 50%

**Confidence formula:**
```python
confidence = min(100, (whale_value / 1000) * (50 - avg_accuracy))
```

---

### 6. Kalshi

**Purpose:** Cross-platform arbitrage with US-regulated exchange.

**Implementation:**
- **Source file:** `odds/kalshi_edge.py`
- **API:** Kalshi REST API
- **Auth:** Email/password credentials

**Features:**
- Market matching with Polymarket via title similarity
- Edge calculation when price gap > 5%
- US regulatory event focus

---

### 7. Manifold

**Purpose:** Leading indicator (play money moves before real money).

**Implementation:**
- **Source file:** `odds/manifold.py`
- **API:** Manifold REST API (public, no auth)
- **Edge threshold:** 10% price gap

**Strategy:**
Play money markets have no friction, so they move faster than real money markets. When Manifold jumps 10%+, trade Polymarket before it catches up.

---

### 8. Metaculus

**Purpose:** Expert forecaster predictions.

**Implementation:**
- **Source file:** `odds/metaculus.py`
- **API:** Metaculus REST API (public)
- **Focus:** Long-term forecasts, AI/tech questions

**Edge calculation:**
Compare community median prediction vs Polymarket price.

---

### 9. PredictIt

**Purpose:** US politics price gaps.

**Implementation:**
- **Source file:** `odds/predictit.py`
- **API:** PredictIt REST API
- **Proxy:** `scripts/predictit_proxy.py` (for rate limiting)

**Note:** PredictIt has $850 per-contract limits and 10% fees, affecting true edge calculation.

---

### 10. Betfair

**Purpose:** Sharp exchange odds (professional bettors).

**Implementation:**
- **Source file:** `odds/betfair_edge.py`
- **API:** The Odds API (aggregated)
- **Markets:** Sports, politics

**Why Betfair is sharp:**
- No vig on exchange
- Professional bettors set prices
- High liquidity

---

### 11. PolyRouter

**Purpose:** Unified access to 7 prediction market platforms.

**Implementation:**
- **Source file:** `odds/polyrouter.py`
- **API:** PolyRouter GraphQL
- **Local MCP:** `mcp/polyrouter/`

**Platforms aggregated:**
1. Polymarket
2. PredictIt
3. Kalshi
4. Manifold
5. Metaculus
6. Betfair
7. Smarkets

**Edge detection:**
Find same markets across platforms, identify price gaps.

---

### 12. Vegas / Sports Odds

**Purpose:** Sharp sportsbook lines (true probability reference).

**Implementation:**
- **Source file:** `odds/vegas_scraper.py`
- **Source file:** `odds/soccer_edge.py`
- **API:** The Odds API + VegasInsider scraping

**Sports covered:**
| Sport | Source | Markets |
|-------|--------|---------|
| NFL | The Odds API | Moneyline, Spread, Super Bowl futures |
| NBA | The Odds API | Moneyline, Spread, Championship |
| NHL | The Odds API | Moneyline, Stanley Cup |
| MLB | The Odds API | Moneyline, World Series |
| Soccer | VegasInsider scrape | EPL, UCL, La Liga, Bundesliga, World Cup |

**Devigging:**
Vegas odds include ~4% vig. We remove it for true probability:
```python
prob_true = prob_raw / (prob_a + prob_b)
```

---

### 13. ESPN

**Purpose:** Real-time game odds from ESPN BET integration.

**Implementation:**
- **Source file:** `odds/espn_odds.py`
- **API:** ESPN scoreboard + BET odds API

**Sports:**
- NFL, NBA, NHL, MLB
- NCAAF, NCAAB

**Features:**
- True probability calculation from moneylines
- Live game status
- Edge vs Polymarket

---

### 14. Simmer

**Purpose:** Price divergence detection vs Polymarket.

**Implementation:**
- **Source file:** `api/routes/trading.py`
- **API:** Simmer REST API

**Integration:**
- Portfolio sync
- Position tracking
- Divergence signals when price gap > 5%

---

## Data Flow

```
┌─────────────────────────────────────────────────────────────┐
│                     External APIs                            │
├─────────────┬─────────────┬─────────────┬──────────────────┤
│ Google News │ Reddit      │ Polymarket  │ The Odds API     │
│ Manifold    │ Metaculus   │ Kalshi      │ ESPN             │
│ PredictIt   │ Betfair     │ PolyRouter  │ Simmer           │
└──────┬──────┴──────┬──────┴──────┬──────┴────────┬─────────┘
       │             │             │               │
       ▼             ▼             ▼               ▼
┌─────────────────────────────────────────────────────────────┐
│                    Signal Aggregator                         │
│                 (api/routes/signals.py)                      │
├─────────────────────────────────────────────────────────────┤
│  • Collect signals from all sources                          │
│  • Apply Bayesian confidence scoring                         │
│  • Check for multi-source agreement                          │
│  • Detect and log conflicts                                  │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                    Trading Engine                            │
│                 (api/routes/engine.py)                       │
├─────────────────────────────────────────────────────────────┤
│  • Position sizing via Kelly criterion                       │
│  • Phase-based limits                                        │
│  • Daily loss limits                                         │
│  • Execute paper/live trades                                 │
└─────────────────────────────────────────────────────────────┘
```

---

## API Keys Required

| Service | Variable | Required | Free Tier |
|---------|----------|----------|-----------|
| The Odds API | `ODDS_API_KEY` | For Vegas/Betfair | 500 req/mo |
| Kalshi | `KALSHI_EMAIL/PASSWORD` | For Kalshi | Free |
| Polymarket | `POLY_*` | For live trading | Free |
| Betfair | `BETFAIR_API_KEY` | For Betfair direct | Free |

---

## Caching

All external sources use internal caching to reduce API calls:

| Source | Cache TTL | Location |
|--------|-----------|----------|
| News | 60s | `~/.openclaw/polyclawd/news_cache.json` |
| Vegas | 60s | `odds/vegas_cache.json` |
| Markets | 60s | In-memory (EdgeCache) |
| Positions | 30s | In-memory |

---

## Adding New Sources

1. Create module in `odds/` or `signals/`
2. Implement fetch function with rate limiting
3. Add to signal aggregator in `api/routes/signals.py`
4. Add to MCP tools in `mcp/server.py`
5. Document in this file

---

## API Audit (2026-02-08)

### New Endpoints Added

| Source | New Endpoint | Trading Value |
|--------|--------------|---------------|
| Polymarket | CLOB Orderbook depth | See real liquidity, detect spoofing |
| Polymarket | Price history (OHLC) | Chart analysis, momentum |
| Manifold | Bets endpoint | Track betting flow |
| Manifold | User portfolio | Whale tracking |
| Metaculus | Metaculus prediction | Expert vs crowd divergence |
| Metaculus | Prediction history | Momentum signals |
| The Odds API | Player props | Less efficient market |
| The Odds API | Spreads/Totals | More edge opportunities |
| PolyRouter | Player props | Cross-platform prop edges |
| PolyRouter | Arbitrage detection | Risk-free profits |
| ESPN | Injuries | Fast line movement |
| ESPN | Standings | Team form analysis |
| VegasInsider | NBA futures | Basketball edges |
| VegasInsider | MLB futures | Baseball edges |
| VegasInsider | NHL futures | Hockey edges |

### Files Created/Modified

- `odds/polymarket_clob.py` - **NEW** - Orderbook + price history
- `odds/manifold.py` - Added bets, portfolio, top traders
- `odds/metaculus.py` - Added Metaculus prediction, history
- `odds/client.py` - Added spreads, totals, outrights, props
- `odds/polyrouter.py` - Added props, enhanced arb detection
- `odds/espn_odds.py` - Added injuries, standings
- `odds/vegas_scraper.py` - Added NBA, MLB, NHL futures

### Priority Recommendations

1. **HIGH VALUE**: Polymarket orderbook analysis for liquidity signals
2. **HIGH VALUE**: Player props for less efficient markets
3. **HIGH VALUE**: Injuries data for line movement prediction
4. **MEDIUM VALUE**: Cross-platform arbitrage scanning
5. **MEDIUM VALUE**: Metaculus vs community prediction divergence

See `docs/api/API_AUDIT.md` for full audit report.

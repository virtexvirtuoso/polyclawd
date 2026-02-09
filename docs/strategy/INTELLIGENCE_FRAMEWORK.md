# Intelligence Framework

What insights can we derive by combining Polyclawd's data sources?

---

## Data Sources Inventory

### Prediction Markets
| Source | Data | Unique Value |
|--------|------|--------------|
| **Polymarket** | Orderbook, microstructure, events | Deepest crypto liquidity, real-time order flow |
| **Kalshi** | Regulated US markets, entertainment | Legal US trading, unique props (halftime, awards) |
| **Manifold** | Play money, top traders, bets | Wisdom of crowds, no vig distortion |
| **Metaculus** | Expert forecasters, questions | Calibrated superforecasters, long-term |
| **PredictIt** | Political markets | US politics specialty |

### Sports/Vegas
| Source | Data | Unique Value |
|--------|------|--------------|
| **The Odds API** | 15+ sportsbooks, all major sports | Sharp book access (Pinnacle) |
| **ESPN** | Moneylines, injuries, standings | Real-time injury news |
| **Betfair** | Exchange prices | True market prices, no bookie margin |
| **VegasInsider** | Futures, soccer | Long-term futures |

### Aggregators
| Source | Data | Unique Value |
|--------|------|--------------|
| **PolyRouter** | 7 platforms unified | Cross-platform arbitrage |
| **Simmer** | AI portfolio signals | LLM-generated context |

---

## Intelligence Categories

### 1. Cross-Platform Arbitrage
**Sources:** Polymarket + Kalshi + Vegas + Manifold + PolyRouter

```
Signal: Same event priced differently across platforms

Example:
- Polymarket: Chiefs YES @ 52¢
- Kalshi: Chiefs YES @ 48¢
- Pinnacle implied: 50.5%

Intelligence: 4¢ spread = risk-free arb OR information asymmetry
Action: Buy Kalshi 48¢, sell Polymarket 52¢
```

**API:** `/api/polyrouter/arbitrage`, `/api/arb-scan`

---

### 2. Sharp vs Recreational Divergence
**Sources:** Pinnacle (sharp) vs DraftKings/FanDuel (soft)

```
Signal: Sharp books move before soft books

Example:
- Pinnacle: Patriots -3.5 → -4.5 (moved 1 point)
- DraftKings: Patriots -3.5 (hasn't moved)

Intelligence: Sharp money on Patriots, soft books will follow
Action: Bet Patriots -3.5 on DraftKings before line moves
```

**API:** `/api/vegas/edge`, `/api/espn/moneyline/{sport}`

---

### 3. Expert vs Market Divergence
**Sources:** Metaculus (experts) vs Polymarket (crowd)

```
Signal: Calibrated forecasters disagree with market

Example:
- Metaculus median: 35% (from superforecasters)
- Polymarket: 48¢

Intelligence: Experts with 2%+ Brier scores see 13% edge
Action: Bet NO on Polymarket
```

**API:** `/api/metaculus/divergence`

---

### 4. Whale Behavior Analysis
**Sources:** Polymarket orderbook + volume spikes + smart money

```
Signal: Large orders or unusual volume

Example:
- $500K buy wall appears at 45¢
- Volume 10x average
- Known whale address active

Intelligence: Informed money accumulating
Action: Follow whale direction with confirmation
```

**API:** `/api/polymarket/orderbook/{slug}`, `/api/volume/spikes`, `/api/smart-money`, `/api/inverse-whale`

---

### 5. Orderbook Microstructure
**Sources:** Polymarket CLOB depth analysis

```
Signal: Order book imbalance reveals true sentiment

Example:
- Bid depth: $200K within 2%
- Ask depth: $50K within 2%
- Imbalance ratio: 4:1 bullish

Intelligence: More buyers than sellers = price likely to rise
Action: Front-run the imbalance
```

**API:** `/api/polymarket/microstructure/{slug}`

---

### 6. Injury Impact Quantification
**Sources:** ESPN injuries + Vegas line movement

```
Signal: Key player injury not yet priced in

Example:
- ESPN: Patrick Mahomes "Questionable" (just reported)
- Pinnacle: Chiefs still -7.5
- Historical impact: Mahomes out = +4 points to spread

Intelligence: Line should move to -3.5, current line is stale
Action: Bet against Chiefs before line moves
```

**API:** `/api/espn/injuries/{sport}`, `/api/vegas/nfl`

---

### 7. Resolution Timing Alpha
**Sources:** Market resolution schedules + price behavior

```
Signal: Markets approaching resolution behave predictably

Example:
- Election market resolves in 2 hours
- Price at 94¢, needs to go to 100¢ or 0¢
- No news catalyst expected

Intelligence: 94¢ likely to drift toward resolution
Action: Buy at 94¢, collect 6¢ with high probability
```

**API:** `/api/resolution/approaching`, `/api/resolution/imminent`

---

### 8. Cross-Market Correlation
**Sources:** Multiple markets on same underlying

```
Signal: Related markets should move together

Example:
- "Chiefs win Super Bowl" @ 35¢
- "Chiefs win AFC" @ 55¢
- "Mahomes MVP" @ 40¢

Intelligence: If Chiefs win SB, they must win AFC
Constraint: P(SB) ≤ P(AFC) always
Action: Arb if constraint violated
```

**API:** `/api/polymarket/events`, `/api/kalshi/markets`

---

### 9. Manifold Wisdom Extraction
**Sources:** Manifold top traders + bet history

```
Signal: Best predictors have track records

Example:
- Top 10 Manifold traders all betting YES
- Their historical accuracy: 68%
- Market price implies 45%

Intelligence: Smart play-money crowd sees value
Action: Follow top traders on real-money markets
```

**API:** `/api/manifold/top-traders`, `/api/manifold/bets`

---

### 10. Vegas-to-Prediction-Market Edge
**Sources:** Devigged Vegas odds vs Polymarket

```
Signal: Vegas has better info on sports

Example:
- Pinnacle devigged: Eagles 62%
- Polymarket: Eagles 58¢

Intelligence: 4% edge, Vegas sharper on sports
Action: Buy Polymarket YES
```

**API:** `/api/vegas/edge`, `/api/espn/edge`

---

### 11. Entertainment Props Intelligence
**Sources:** Kalshi entertainment + news + social

```
Signal: Unique props not available elsewhere

Example:
- Kalshi: "Bad Bunny opens halftime" @ 55¢
- Insider rumor: Bad Bunny rehearsing "Tití Me Preguntó"
- Social volume spiking

Intelligence: Information edge on entertainment
Action: Buy specific song props
```

**API:** `/api/kalshi/entertainment`

---

### 12. Confidence Calibration Feedback
**Sources:** Historical outcomes + signal performance

```
Signal: Track which signals actually work

Example:
- Inverse whale: 62% win rate (120 samples)
- Smart money: 58% win rate (85 samples)
- Volume spike: 51% win rate (200 samples)

Intelligence: Weight signals by proven accuracy
Action: Trust inverse whale > volume spike
```

**API:** `/api/confidence/sources`, `/api/confidence/calibration`

---

## Combined Intelligence Queries

### "Best Bets Right Now"
```bash
# 1. Get all edges
/api/signals

# 2. Filter by adjusted edge > 3%
# 3. Cross-reference with:
#    - Sharp book agreement (/api/vegas/edge)
#    - No conflicting injuries (/api/espn/injuries)
#    - Healthy orderbook (/api/polymarket/microstructure)
#    - Multiple source agreement (/api/confidence/sources)
```

### "Super Bowl Alpha"
```bash
# Combine:
/api/vegas/nfl/superbowl     # Vegas lines
/api/kalshi/entertainment     # Halftime, anthem props
/api/espn/injuries/nfl        # Player status
/api/polymarket/orderbook     # Liquidity depth
/api/polyrouter/props/nfl     # Cross-platform props
```

### "Arbitrage Scanner"
```bash
# Find price discrepancies:
/api/arb-scan                 # All platforms
/api/polyrouter/arbitrage     # PolyRouter's view
/api/metaculus/divergence     # Expert disagreement
```

### "Follow Smart Money"
```bash
# Track informed flow:
/api/smart-money              # Whale addresses
/api/inverse-whale            # Contrarian whales
/api/volume/spikes            # Unusual activity
/api/manifold/top-traders     # Best predictors
```

---

## Intelligence Gaps (Future)

| Gap | Potential Source | Value |
|-----|------------------|-------|
| Social sentiment | Twitter API, Reddit | Early signal detection |
| News velocity | NewsAPI, RSS | Breaking news edge |
| On-chain flow | Dune, Nansen | Whale wallet tracking |
| Options flow | CBOE, unusual activity | Implied volatility |
| Insider filings | SEC EDGAR | Corporate event edge |

---

## Automated Monitoring

All 12 intelligence types are monitored via cron jobs:

| Intelligence | Cron Job | Schedule |
|-------------|----------|----------|
| #1 Cross-platform arb | edge-scanner-6h | Every 6h |
| #2 Sharp vs soft | vegas-edge-scanner | Every 2h |
| #5 Orderbook walls | orderbook-whale-walls | Every 4h |
| #6 Injury impact | injury-impact-scanner | Every 3h |
| #7 Resolution timing | resolution-timing-alert | Every 2h |
| #8 Correlation | correlation-violation-scanner | Every 4h |
| #10 Vegas edge | vegas-edge-scanner | Every 2h |
| #11 Entertainment | kalshi-edge-scanner | 9am/3pm/9pm |
| #12 Calibration | weekly-signal-calibration | Sun 9am |

See `docs/operations/CRON_JOBS.md` for full details.

---

## Signal Hierarchy

When signals conflict, trust in this order:

1. **Sharp book consensus** (Pinnacle, Circa) - professionals with skin in game
2. **Cross-platform arbitrage** - math doesn't lie
3. **Metaculus experts** - calibrated superforecasters
4. **Orderbook imbalance** - revealed preferences
5. **Whale activity** - informed money (but can be wrong)
6. **Volume spikes** - attention signal, not direction
7. **Manifold traders** - play money, less reliable

---

## Quick Intelligence Commands

```bash
# What's the sharpest edge right now?
curl /api/signals | jq '.[] | select(.adjusted_edge > 5)'

# Any arbitrage opportunities?
curl /api/arb-scan

# Where are whales moving?
curl /api/smart-money

# Expert disagreement?
curl /api/metaculus/divergence

# Injuries affecting lines?
curl /api/espn/injuries/nfl
```

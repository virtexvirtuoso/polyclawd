# Polyclawd - AI-Powered Prediction Market Trading Bot

**Virtuoso Crypto's intelligent cross-platform trading system for prediction markets.**

Production: `https://virtuosocrypto.com/polyclawd`

---

## Data Sources

### Prediction Markets (Real Money)
| Platform | API | Status | Use Case |
|----------|-----|--------|----------|
| **Polymarket** | REST + WebSocket | ✅ Live | Main execution venue, crypto/politics |
| ~~PredictIt~~ | REST | ⛔ Deprecated | Persistent 403 errors since Feb 2026 |
| **Kalshi** | REST | ✅ Live | Market overlap detection, entertainment |
| **Betfair** | via Odds API | ⚠️ Quota | Sharp odds reference (shares Odds API credits) |
| **Smarkets** | REST | ✅ Live | UK/EU politics |

### Prediction Markets (Play Money / Signals)
| Platform | API | Status | Use Case |
|----------|-----|--------|----------|
| **Manifold** | REST | ✅ Live | Leading indicator (moves first) |
| **Metaculus** | REST | ✅ Live | Forecasting divergence, question-level data |
| **Simmer** | REST | ✅ Live | Price divergence detection |

### Sports Odds (Sharp Lines)
| Source | API | Status | Use Case |
|--------|-----|--------|----------|
| **ActionNetwork** | REST (free) | ✅ Live | NBA, NFL, NHL, MLB, Soccer, EPL — 18+ books |
| ~~Vegas/Pinnacle~~ | ~~The Odds API~~ | ⛔ Deprecated | No API key configured |
| ~~ESPN~~ | ~~Scraper~~ | ⛔ Deprecated | ESPN removed odds from free API |
| **Soccer Futures** | VegasInsider scrape | ✅ Live | EPL, UCL, La Liga, Bundesliga, World Cup |
| **Azuro** | GraphQL | ✅ Live | DeFi sports betting |

### Meta / Aggregation
| Source | API | Status | Use Case |
|--------|-----|--------|----------|
| **PolyRouter** | Internal | ✅ Live | Cross-platform market matching, arbitrage, sports/futures |

### Dead/Deprecated Sources
| Platform | Status | Reason |
|----------|--------|--------|
| Zeitgeist | ❌ Dead | API endpoints removed/migrated |
| Polkamarkets | ❌ Dead | Pivoted to B2B, no public markets |
| Omen | ❌ Dead | The Graph hosted service shut down |

---

## Signal Sources (15 Active)

| # | Source | Type | Weight | Description |
|---|--------|------|--------|-------------|
| 1 | **Inverse Whale** | On-chain | HIGH | Fade losing traders (<50% accuracy) |
| 2 | **Smart Money Flow** | On-chain | MEDIUM | Follow net flow from accurate traders |
| 3 | **Simmer Divergence** | Cross-platform | MEDIUM | Price gaps vs Polymarket |
| 4 | **Volume Spikes** | Technical | LOW | Z-score anomaly (2σ+ activity) |
| 5 | **New Markets** | Calendar | LOW | Early mover on new markets |
| 6 | **Resolution Timing** | Calendar | LOW | High uncertainty near expiry |
| 7 | ~~Vegas Edge~~ | ~~Sharp odds~~ | ⛔ | DEPRECATED — replaced by ActionNetwork |
| 8 | **Soccer Edge** | Sharp odds | HIGH | Futures vs Polymarket (devigged) |
| 9 | ~~Betfair Edge~~ | ~~Sharp odds~~ | ⛔ | DEPRECATED — used The Odds API |
| 10 | **Manifold Edge** | Leading indicator | MEDIUM | Play money signals |
| 11 | **PredictIt Edge** | Cross-platform | MEDIUM | Politics price gaps |
| 12 | **Kalshi Overlap** | Cross-platform | MEDIUM | Market matching |
| 13 | ~~ESPN Edge~~ | ~~Sharp odds~~ | ⛔ | DEPRECATED — ESPN removed odds from API |
| 14 | **Metaculus Divergence** | Forecasting | MEDIUM | Expert forecasts vs market prices |
| 15 | **Correlation Violations** | Math constraint | HIGH | Parent/child market price inconsistencies |
| 16 | **ActionNetwork** | Sharp odds | HIGH | 18+ books, devigged probs vs Polymarket |
| 17 | **Basket Arb** | Arbitrage | HIGH | Sum-to-one multi-outcome guaranteed profit |
| 18 | **Copy-Trade** | Whale tracking | MEDIUM | Top wallet positions, signal confirmation |
| 19 | **Empirical Confidence** | Self-improving | HIGH | Bayesian WR by archetype × side × price zone |

---

## Profit Strategies

### 1. Sharp vs Soft Line Arbitrage
```
Sharp (Betfair/Vegas) = True probability (professional bettors)
Soft (Polymarket) = Retail sentiment (crypto degens)
EDGE: Trade Poly toward sharp price when gap > 5%

DEVIGGING: Vegas odds include ~4% vig (house edge).
We remove vig before comparing to get TRUE probabilities:
  - Two-way: prob_true = prob_raw / (prob_a + prob_b)
  - Multi-way: prob_true = prob_raw / sum(all_probs)
This makes edge detection ~2-4% more accurate.
```

### 2. Manifold → Polymarket Flow
```
Manifold = Play money, moves FAST (no friction)
Polymarket = Real money, moves SLOW
EDGE: When Manifold jumps 10%+, trade Poly before it catches up
Latency: 1-4 hours typical
```

### 3. Cross-Platform Arbitrage
```
Same market, different prices across platforms
PredictIt vs Polymarket (politics)
EDGE: Need >12% gap after fees to profit
```

### 4. Whale Fade
```
Track Polymarket whale wallets on-chain
Identify losers (<50% win rate)
EDGE: Bet opposite = 55-60% historical win rate
```

### 5. News Speed Edge
```
News breaks → markets adjust at different speeds
EDGE: Trade slow platform before price updates
Requires: Fast news monitoring (Google News, X)
```

### 6. Correlation Violation Arbitrage
```
Parent market: "Will Team X win the championship?" = 30%
Child market: "Will Team X win Game 7?" = 20%
VIOLATION: Child can't be lower than parent implies
EDGE: Math constraint broken → high conviction signal
```

### 7. Injury Impact Edge
```
Key player injury announced → lines should move ~3-4 points
If Polymarket hasn't repriced yet → trade the stale line
EDGE: Time-sensitive, lines move within hours
```

---

## Trading Engine

### Features
- **Real-time scanning**: Every 30 seconds
- **Adaptive confidence**: Gets stricter as trades accumulate (+3/trade, -1/30min decay)
- **Drawdown breaker**: Halts at 5% daily loss
- **Opportunity cost engine**: Rotates weak positions for better signals
- **Kelly sizing**: Quarter-Kelly (25%) for conservative sizing
- **Phase management**: Configurable trading phases with position limits

### Bayesian Confidence Formula
```
Final = Base × (source_win_rate / 0.5) × (1 + 0.2 × agreeing_sources)

Example:
  Base: 40
  Source win rate: 60% → multiplier = 1.2
  2 agreeing sources → multiplier = 1.4
  Final: 40 × 1.2 × 1.4 = 67.2
```

---

## API Endpoints

### Edge Detection
| Endpoint | Description |
|----------|-------------|
| `GET /api/vegas/edge` | Vegas vs Polymarket (NFL/NBA/NHL/MLB) |
| `GET /api/vegas/soccer` | Soccer futures vs Polymarket |
| `GET /api/vegas/epl` | EPL futures |
| `GET /api/vegas/ucl` | Champions League futures |
| `GET /api/vegas/laliga` | La Liga futures |
| `GET /api/vegas/bundesliga` | Bundesliga futures |
| `GET /api/vegas/worldcup` | World Cup futures |
| `GET /api/vegas/nfl` | NFL lines |
| `GET /api/vegas/nfl/superbowl` | Super Bowl odds |
| `GET /api/vegas/nba` | NBA lines |
| `GET /api/vegas/nhl` | NHL lines |
| `GET /api/vegas/mlb` | MLB lines |
| `GET /api/betfair/edge` | Betfair Exchange vs Polymarket |
| `GET /api/manifold/edge` | Manifold vs Polymarket |
| `GET /api/predictit/edge` | PredictIt vs Polymarket |
| `GET /api/metaculus/edge` | Metaculus vs Polymarket |
| `GET /api/metaculus/divergence` | Metaculus forecast divergences |
| `GET /api/espn/edge` | ESPN moneylines vs Polymarket |

### ESPN Sports Data
| Endpoint | Description |
|----------|-------------|
| `GET /api/espn/odds` | Current ESPN odds across sports |
| `GET /api/espn/nfl` | NFL odds |
| `GET /api/espn/nba` | NBA odds |
| `GET /api/espn/nhl` | NHL odds |
| `GET /api/espn/mlb` | MLB odds |
| `GET /api/espn/ncaaf` | College football odds |
| `GET /api/espn/ncaab` | College basketball odds |
| `GET /api/espn/moneyline/{sport}` | Moneyline for specific sport |
| `GET /api/espn/moneylines` | All moneylines |
| `GET /api/espn/injuries/{sport}` | Injury reports by sport |
| `GET /api/espn/standings/{sport}` | Standings by sport |

### Cross-Platform Edge Scanner
| Endpoint | Description |
|----------|-------------|
| `GET /api/edge/scan` | Cross-platform edge scan (all sources) |
| `GET /api/edge/topics` | Tracked topic keywords |
| `POST /api/edge/calculate` | Sophisticated edge calc (Shin method) |
| `GET /api/edge/calculate/example` | Example edge calculation |
| `GET /api/edge/sharp-books` | Sharp bookmaker reference |

### PolyRouter (Multi-Platform)
| Endpoint | Description |
|----------|-------------|
| `GET /api/polyrouter/markets` | Matched markets across platforms |
| `GET /api/polyrouter/search` | Cross-platform market search |
| `GET /api/polyrouter/edge` | Cross-platform edge detection |
| `GET /api/polyrouter/sports/{league}` | Sports markets by league |
| `GET /api/polyrouter/futures/{league}` | Futures markets by league |
| `GET /api/polyrouter/props/{league}` | Prop bets by league |
| `GET /api/polyrouter/arbitrage` | Arbitrage opportunities |
| `GET /api/polyrouter/platforms` | Platform status overview |

### Market Data
| Endpoint | Description |
|----------|-------------|
| `GET /api/markets/trending` | Trending markets by volume |
| `GET /api/markets/search` | Market search |
| `GET /api/markets/new` | Recently created markets |
| `GET /api/markets/opportunities` | High-opportunity markets |
| `GET /api/markets/{market_id}` | Single market detail |
| `GET /api/arb-scan` | Arbitrage scan |
| `GET /api/rewards` | Liquidity rewards |
| `GET /api/manifold/markets` | Manifold market summary |
| `GET /api/manifold/bets` | Manifold bet history |
| `GET /api/manifold/top-traders` | Top Manifold traders |
| `GET /api/predictit/markets` | PredictIt market summary |
| `GET /api/kalshi/markets` | Kalshi overlap detection |
| `GET /api/kalshi/entertainment` | Kalshi entertainment markets |
| `GET /api/kalshi/all` | All Kalshi markets |
| `GET /api/metaculus/questions` | Metaculus question data |
| `GET /api/polymarket/events` | Polymarket event listing |
| `GET /api/polymarket/orderbook/{slug}` | Orderbook depth for market |
| `GET /api/polymarket/microstructure/{slug}` | Market microstructure analysis |

### Signals
| Endpoint | Description |
|----------|-------------|
| `GET /api/signals` | Aggregated signals (all sources) |
| `GET /api/signals/news` | Google News + Reddit signals |
| `POST /api/signals/auto-trade` | Auto-trade on signal |
| `GET /api/inverse-whale` | Whale fade signals |
| `GET /api/smart-money` | Smart money flow |
| `GET /api/volume/spikes` | Volume anomalies |
| `GET /api/resolution/approaching` | Markets nearing resolution |
| `GET /api/resolution/imminent` | Markets resolving within hours |
| `GET /api/conflicts/stats` | Signal conflict statistics |
| `GET /api/conflicts/active` | Active signal conflicts |
| `GET /api/rotations` | Position rotation history |
| `GET /api/rotation/candidates` | Candidate positions for rotation |

### Confidence Tracking
| Endpoint | Description |
|----------|-------------|
| `GET /api/confidence/sources` | Per-source win rate stats |
| `POST /api/confidence/record` | Record trade outcome |
| `GET /api/confidence/market/{market_id}` | Confidence for specific market |
| `GET /api/confidence/history` | Historical confidence data |
| `GET /api/confidence/calibration` | Signal calibration report |
| `GET /api/predictors` | Accuracy leaderboard |
| `POST /api/predictors/update` | Update predictor stats |

### Engine Control
| Endpoint | Description |
|----------|-------------|
| `GET /api/engine/status` | Engine status + adaptive state |
| `POST /api/engine/start` | Start trading engine |
| `POST /api/engine/stop` | Stop trading engine |
| `GET /api/engine/config` | Get engine configuration |
| `POST /api/engine/config` | Update engine configuration |
| `POST /api/engine/trigger` | Force evaluation cycle |
| `POST /api/engine/reset-daily` | Reset adaptive boost + drawdown |

### Phase Management
| Endpoint | Description |
|----------|-------------|
| `GET /api/phase/current` | Current trading phase |
| `GET /api/phase/history` | Phase transition history |
| `GET /api/phase/config` | Phase configuration |
| `GET /api/phase/limits` | Phase position limits |
| `POST /api/phase/simulate` | Simulate phase transition |

### Kelly Sizing
| Endpoint | Description |
|----------|-------------|
| `GET /api/kelly/current` | Current Kelly parameters |
| `GET /api/kelly/simulate` | Simulate Kelly sizing |

### Alerts
| Endpoint | Description |
|----------|-------------|
| `GET /api/alerts` | List active alerts |
| `POST /api/alerts` | Create new alert |
| `DELETE /api/alerts/{alert_id}` | Delete alert |
| `GET /api/alerts/check` | Check alert conditions |

### LLM Integration
| Endpoint | Description |
|----------|-------------|
| `GET /api/llm/status` | LLM service status |
| `POST /api/llm/test` | Test LLM inference |

### Paper Trading
| Endpoint | Description |
|----------|-------------|
| `GET /api/paper/status` | Paper account status |
| `GET /api/paper/positions` | Open paper positions |
| `POST /api/paper/trade` | Execute paper trade |
| `GET /api/balance` | Account balance + P&L |
| `GET /api/positions` | All positions |
| `GET /api/positions/check` | Position health check |
| `POST /api/positions/{id}/resolve` | Resolve position |
| `GET /api/trades` | Trade history |
| `POST /api/trade` | Execute trade |
| `POST /api/reset` | Reset paper account |

### Simmer SDK (Live Trading)
| Endpoint | Description |
|----------|-------------|
| `GET /api/simmer/status` | Simmer agent status |
| `GET /api/simmer/portfolio` | Live portfolio |
| `GET /api/simmer/positions` | Live positions |
| `GET /api/simmer/trades` | Live trade history |
| `POST /api/simmer/trade` | Execute live trade |
| `GET /api/simmer/context/{market_id}` | Pre-trade context |

### System
| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check (JSON) |
| `GET /ready` | Readiness probe |
| `GET /metrics` | System metrics |

---

## Cron Jobs (OpenClaw)

Automated monitoring and alerting via [OpenClaw](https://github.com/anthropics/openclaw) agent framework, delivering alerts to Telegram.

| Job | Schedule | Description |
|-----|----------|-------------|
| `polyclawd-monitor` | Every 2h | Full system check: engine status, whale signals, rotations, smart money |
| `polyclawd-rotation-alert` | Every 30m | Position rotation notifications |
| `vegas-edge-scanner` | 3x/day (9am, 3pm, 9pm) | Sports edges >= 8% with Kelly sizing |
| `kalshi-edge-scanner` | 3x/day (9am, 3pm, 9pm) | Kalshi vs Polymarket edge detection |
| `edge-scanner-6h` | Every 6h | Cross-platform edge scan (all sources) |
| `correlation-violation-scanner` | Every 4h | Math constraint violations between related markets |
| `injury-impact-scanner` | Every 3h | Key player injuries vs stale lines |
| `resolution-timing-alert` | Every 2h | Markets resolving soon (theta collection) |
| `orderbook-whale-walls` | Every 4h | Bid/ask wall detection in orderbooks |
| `weekly-signal-calibration` | Sunday 9am | Signal source win rates and recommendations |

All schedules are in `America/New_York` timezone.

---

## Quick Start

```bash
# Local development
cd ~/Desktop/polyclawd
source venv/bin/activate
uvicorn api.main:app --host 127.0.0.1 --port 8420

# Production (VPS)
ssh vps
sudo systemctl status polyclawd-api
```

---

## Architecture

### API Structure (v2.1.0)

The API uses a modular router architecture with domain-specific modules:

```
api/
├── main.py              # App factory, lifespan, router registration
├── routes/
│   ├── system.py        # /health, /ready, /metrics
│   ├── trading.py       # /api/balance, /api/positions, /api/trade, /api/simmer/*, /api/paper/*
│   ├── markets.py       # /api/markets/*, /api/vegas/*, /api/espn/*, /api/betfair/*, /api/polyrouter/*, etc.
│   ├── signals.py       # /api/signals, /api/inverse-whale, /api/confidence/*, /api/rotations
│   ├── engine.py        # /api/engine/*, /api/alerts/*, /api/kelly/*, /api/phase/*, /api/llm/*
│   └── edge_scanner.py  # /api/edge/scan, /api/edge/calculate, /api/edge/topics
├── deps.py              # Dependency injection (settings, services)
├── middleware.py         # Security headers, rate limiting, auth
└── services/            # Business logic layer
```

### Router Organization

| Router | Prefix | Endpoints | Purpose |
|--------|--------|-----------|---------|
| `system_router` | (none) | 3 | Health, readiness, metrics |
| `trading_router` | `/api` | 16 | Paper trading, Simmer SDK |
| `markets_router` | `/api` | 58 | Market data, edge detection, ESPN, PolyRouter |
| `signals_router` | `/api` | 19 | Signal aggregation, whales, confidence |
| `engine_router` | `/api` | 20 | Engine control, alerts, Kelly, phases, LLM |
| `edge_scanner_router` | `/api/edge` | 5 | Cross-platform edge scanning |

**Total: 121 endpoints**

### Security

- **API Key Authentication**: Protected endpoints require `X-API-Key` header
- **Rate Limiting**: SlowAPI with per-endpoint limits
- **Security Headers**: X-Content-Type-Options, X-Frame-Options, CSP
- **CORS**: Restricted origins with explicit allow list

### Data Flow

```
┌──────────────────────────────────────────────────────────────────────┐
│                           DATA SOURCES                               │
├──────────┬──────────┬──────────┬──────────┬──────────┬──────────────-┤
│Polymarket│PredictIt │ Manifold │  Vegas   │  ESPN    │  Metaculus   │
│  Kalshi  │ Smarkets │  Simmer  │ Betfair  │ Soccer   │  PolyRouter  │
└────┬─────┴────┬─────┴────┬─────┴────┬─────┴────┬─────┴──────┬───────┘
     │          │          │          │          │            │
     └──────────┴──────────┴────┬─────┴──────────┴────────────┘
                                ▼
                  ┌──────────────────────────┐
                  │     EDGE SCANNER         │
                  │  Cross-platform + Shin   │
                  └────────────┬─────────────┘
                               ▼
                  ┌──────────────────────────┐
                  │   SIGNAL AGGREGATOR      │
                  │   (15 sources)           │
                  └────────────┬─────────────┘
                               ▼
                  ┌──────────────────────────┐
                  │   BAYESIAN SCORING       │
                  │   + Confidence Tracking  │
                  └────────────┬─────────────┘
                               ▼
                  ┌──────────────────────────┐
                  │   TRADING ENGINE         │
                  │   Adaptive + Kelly       │
                  └────────────┬─────────────┘
                               ▼
                  ┌──────────────────────────┐
                  │   PAPER TRADING          │
                  │   $10K Starting Balance  │
                  └────────────┬─────────────┘
                               ▼
                  ┌──────────────────────────┐
                  │   OPENCLAW CRON ALERTS   │
                  │   → Telegram             │
                  └──────────────────────────┘
```

### Performance

Load tested with Locust (50 concurrent users):
- **Local endpoints**: p95 < 20ms, ~65 req/s
- **External API endpoints**: Depends on upstream latency
- **Memory**: ~15MB RSS under load

---

## Operations

### VPS Infrastructure
- **Host**: Hetzner VPS (`ssh vps` / 5.223.63.4)
- **Service**: `polyclawd-api.service` (systemd, port 8420, behind nginx)
- **Reverse proxy**: nginx at `virtuosocrypto.com/polyclawd`

### Watchdog
Automated health monitoring runs every 5 minutes via cron:

```
*/5 * * * * root /usr/local/bin/polyclawd-watchdog.sh
```

Behavior:
- Checks `GET /health` endpoint with 3 retries (5s gaps, 8s timeout each)
- Validates JSON response contains `"healthy"`
- On failure: `systemctl restart polyclawd-api`
- Backoff: stops after 5 consecutive restarts (manual intervention required)
- State: `/tmp/polyclawd-watchdog.state` (consecutive restart counter)
- Logs: `journalctl -t polyclawd-watchdog`

### Common Operations
```bash
# Check service status
ssh vps "systemctl status polyclawd-api"

# View recent logs
ssh vps "journalctl -u polyclawd-api --since '1 hour ago' --no-pager"

# Restart service
ssh vps "sudo systemctl restart polyclawd-api"

# Check watchdog state
ssh vps "cat /tmp/polyclawd-watchdog.state"

# View watchdog logs
ssh vps "sudo journalctl -t polyclawd-watchdog --since '1 hour ago'"
```

---

## License

Proprietary - Virtuoso Crypto

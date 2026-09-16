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

## Signal Pipeline (11 Stages)

Every signal passes through this pipeline before a position is opened:

```
Signal → Confidence → Edge → Archetype Blocklist → NO Prob Floor
→ Kelly Sizing → CV Kelly → Time Decay → Volume Spike
→ Score Velocity → Archetype Boost → Correlation Cap → TRADE
```

### Key Components

| Component | Description |
|-----------|-------------|
| **Bootstrap Kelly** | Seeded 57% WR + 1/8 Kelly until 20 resolved trades |
| **CV Kelly Haircut** | Monte Carlo uncertainty adjustment (post-bootstrap) |
| **Archetype Blocklist** | `price_above` (0/4) and `sports_winner` (0/3) blocked entirely |
| **NO Prob Floor** | Markets where NO <35% implied are too efficient to fade |
| **Time Decay** | Becker-calibrated 28-cell lookup (7 durations × 4 volume buckets) |
| **Volume Spike Detector** | 3x+ = spike (+10%), 10x+ = mega (+20%) from `signal_snapshots` |
| **Price Momentum** | YES rising 5%+ → 1.15x boost, YES falling 5%+ → BLOCK |
| **Score Velocity** | Alpha score delta for crypto archetypes, multiplier [0.7, 1.3] |
| **Correlation Cap** | 6 groups (politics, geopolitical, culture, sports, crypto, weather), max 3 per group |

### Sizing
- **$100 minimum bet** — small bets don't move P&L
- **$10K starting bankroll**
- **Dynamic Kelly** — rolling WR over 20 trades, WR<55% → 1/12 Kelly, drawdown≥15% → pause

## Weather Ensemble (4-Source Probabilistic)

Multi-source forecast aggregator producing calibrated probability distributions:

| Source | Models | Auth | Update |
|--------|--------|------|--------|
| **Open-Meteo Ensemble** | 92 members (ICON, GEFS, GEM) | No key | 6-12h |
| **Pirate Weather** | GEFS + GFS + HRRR + ECMWF | Free API key | 6h |
| **Tomorrow.io** | Proprietary AI (HyperCast) | Free API key | Continuous |
| **WeatherAPI.com** | Station blend + ML | Free API key | 6h |

- Normal/Student-t CDF for calibrated probabilities (not hardcoded buckets)
- Source disagreement >3°F auto-widens distribution (fat tail penalty)
- Multi-day response caching — 1 API call per city returns all dates
- **Same-day re-evaluation** every 5min — auto-closes if edge flips >5%
- 15 cities: NYC, London, Buenos Aires, Wellington, Miami, Dallas, Atlanta, São Paulo, Toronto, Seoul, Seattle, Chicago, Paris, Sydney, Tokyo

## Election Prediction

- `signals/election_polls.py` — Wikipedia polling scraper with recency weighting (30d=1.0x, 90d=0.7x, >90d=0.4x)
- `signals/cross_platform_elections.py` — Manifold vs Polymarket divergence detection (>10%=1.3x, 5-10%=1.15x)
- Incumbency advantage as systematic NO thesis (~70% win rate globally)
- `geopolitical` correlation group (separate from `politics`, each gets 3 slots)

## API Resilience

- **Source health table** — tracks failures per data source
- **`@resilient()` decorator** — circuit breaker (5 fails → 30min cooldown)
- **Staleness tags** — flags stale data from degraded sources
- **ESPN fallback** — Vegas endpoints fall back to ESPN when circuit-broken

## MCP Server (Auto-Discovery)

- `https://virtuosocrypto.com/polyclawd/mcp` — public FastMCP endpoint
- **140 tools auto-discovered** from OpenAPI spec — no manual tool list
- Add API endpoint → restart MCP → tool appears
- Both stdio (`mcp/server.py`) and HTTP (`mcp/http_server.py`) transports

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

## Automated Operations

### VPS Watchdog (`/etc/cron.d/polyclawd-watchdog`)
Runs every 5 minutes — handles health, resolution, signal scanning, and weather re-eval. See Watchdog section below.

### VPS Cache Warmer (`*/15 * * * *`)
Pre-warms expensive API responses every 15 minutes.

### OpenClaw Agent
Polyclawd runs as an [OpenClaw](https://github.com/openclaw/openclaw) agent, delivering alerts and analysis to Telegram.

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

### Project Structure

```
api/
├── main.py              # FastAPI app, lifespan, router registration
├── routes/              # 6 routers, 140+ endpoints
│   ├── system.py        # /health, /ready, /metrics
│   ├── trading.py       # Paper trading, portfolio, Simmer SDK
│   ├── markets.py       # Market data, Vegas, ESPN, PolyRouter
│   ├── signals.py       # Signal aggregation, weather, elections, IC
│   ├── engine.py        # Engine control, Kelly, phases, alerts
│   └── edge_scanner.py  # Cross-platform edge scanning
├── middleware.py         # Security headers, rate limiting, auth
└── services/            # Business logic layer

signals/
├── paper_portfolio.py       # Core trading engine + 11-stage pipeline
├── weather_scanner.py       # Polymarket weather market discovery
├── weather_ensemble.py      # 4-source probabilistic forecasting
├── election_polls.py        # Wikipedia polling scraper
├── cross_platform_elections.py  # Manifold vs Polymarket divergence
├── cv_kelly.py              # CV Kelly uncertainty adjustment
├── strike_probability.py    # Price-to-Strike (Strategy 2)
├── alpha_score_tracker.py   # Score velocity tracking
├── mispriced_category_signal.py  # Core signal generator
├── ic_tracker.py            # Information Coefficient tracking
├── calibrator.py            # Signal calibration + source weights
├── resilience.py            # Circuit breaker + source health
└── shadow_tracker.py        # Shadow trade resolution

mcp/
├── server.py           # stdio MCP — auto-discovers from OpenAPI spec
└── http_server.py      # FastMCP HTTP — 140 tools, port 8421

static/
├── portfolio.html      # Paper trading dashboard (auth-gated)
├── analysis.html       # Signal analysis dashboard
├── how-it-works.html   # Pipeline visualization
├── login.html          # Access code gate
└── auth.js             # Client-side SHA-256 auth
```

**140+ API endpoints**, **140 MCP tools** (auto-discovered from OpenAPI spec)

### Security

- **API Key Authentication**: Protected endpoints require `X-API-Key` header
- **Rate Limiting**: SlowAPI with per-endpoint limits
- **Security Headers**: X-Content-Type-Options, X-Frame-Options, CSP
- **CORS**: Restricted origins with explicit allow list

### Data Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                         DATA SOURCES                             │
├───────────┬──────────┬──────────┬──────────┬───────────┬────────┤
│Polymarket │ Manifold │  ESPN    │ Metaculus │ Weather×4 │ Polls  │
│  Kalshi   │ Simmer   │ Action   │ PolyRouter│ (ensemble)│ (Wiki) │
└─────┬─────┴────┬─────┴────┬─────┴────┬─────┴─────┬─────┴───┬────┘
      └──────────┴──────────┴────┬─────┴───────────┴─────────┘
                                 ▼
              ┌──────────────────────────────────┐
              │   API RESILIENCE LAYER           │
              │   Circuit breaker + staleness    │
              └──────────────┬───────────────────┘
                             ▼
              ┌──────────────────────────────────┐
              │   SIGNAL PIPELINE (11 stages)    │
              │   Confidence → Edge → Blocklist  │
              │   → Kelly → Time Decay → Vol     │
              │   → Score Velocity → Corr Cap    │
              └──────────────┬───────────────────┘
                             ▼
              ┌──────────────────────────────────┐
              │   PAPER PORTFOLIO ENGINE         │
              │   Bootstrap Kelly · $10K bank    │
              │   6 correlation groups · 10 max  │
              └──────────────┬───────────────────┘
                             ▼
              ┌──────────────────────────────────┐
              │   WATCHDOG (every 5-10min)       │
              │   Resolution · Re-eval · IC      │
              └──────────────┬───────────────────┘
                             ▼
              ┌──────────────────────────────────┐
              │   MCP SERVER (140 tools)         │
              │   + Dashboard + Discord alerts   │
              └──────────────────────────────────┘
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
- **Service**: `polyclawd-api.service` (systemd, port 8420, 2 uvicorn workers)
- **MCP**: port 8421, proxied via nginx at `/polyclawd/mcp`
- **Reverse proxy**: nginx at `virtuosocrypto.com/polyclawd`
- **Database**: SQLite `storage/shadow_trades.db` (WAL mode)
  - Tables: `paper_positions`, `paper_portfolio_state`, `shadow_trades`, `signal_snapshots`, `source_health`, `visitor_log`, `price_snapshots`, `daily_summaries`, `alpha_snapshots`, `signal_predictions`, `ic_measurements`, `calibration_curves`, `source_weights`
- **Test suite**: 300+ tests (`venv/bin/pytest`)

### Watchdog (v8)
Automated health + trading loop runs every 5 minutes via `/etc/cron.d/polyclawd-watchdog`:

| Cycle | Frequency | What it does |
|-------|-----------|-------------|
| Health check | Every 5min | 3 retries → restart if unhealthy, backoff after 5 consecutive |
| Resolution | Every 5min | CLOB → Gamma fallback → force-resolve 24h+ past expiry |
| Weather re-eval | Every 5min | Fresh ensemble data for same-day positions, auto-close on flip |
| Signal scan | Every 10min | Mispriced category + weather scanner → `process_signals()` |
| Alpha snapshot | Every 10min | Score velocity tracking per crypto symbol |
| IC + Calibration | Every 30min | Spearman IC, calibration curves, source weight updates |
| Arena snapshot | Every 6h | Leaderboard tracking |

- State: `/tmp/polyclawd-watchdog.state`
- Logs: `/var/log/polyclawd-watchdog.log` (auto-rotated at 2000 lines)

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

# Polyclawd - AI-Powered Prediction Market Trading Bot

**Virtuoso Crypto's intelligent cross-platform trading system for prediction markets.**

Production: `https://virtuosocrypto.com/polyclawd`

---

## Data Sources

### Prediction Markets (Real Money)
| Platform | API | Status | Use Case |
|----------|-----|--------|----------|
| **Polymarket** | REST | ✅ Live | Main execution venue, crypto/politics |
| **PredictIt** | REST | ✅ Live | US politics, cross-platform arb |
| **Kalshi** | REST | ✅ Live | Market overlap detection |
| **Betfair** | via Odds API | ✅ Live | Sharp odds reference |
| **Smarkets** | REST | ✅ Live | UK/EU politics |

### Prediction Markets (Play Money / Signals)
| Platform | API | Status | Use Case |
|----------|-----|--------|----------|
| **Manifold** | REST | ✅ Live | Leading indicator (moves first) |
| **Simmer** | REST | ✅ Live | Price divergence detection |

### Sports Odds (Sharp Lines)
| Source | API | Status | Use Case |
|--------|-----|--------|----------|
| **Vegas/Pinnacle** | The Odds API | ✅ Live | NFL, NBA, NHL true odds |
| **Soccer Futures** | VegasInsider scrape | ✅ Live | EPL, UCL, La Liga, Bundesliga |
| **Azuro** | GraphQL | ✅ Live | DeFi sports betting |

### Dead/Deprecated Sources
| Platform | Status | Reason |
|----------|--------|--------|
| Zeitgeist | ❌ Dead | API endpoints removed/migrated |
| Polkamarkets | ❌ Dead | Pivoted to B2B, no public markets |
| Omen | ❌ Dead | The Graph hosted service shut down |

---

## Signal Sources (12 Active)

| # | Source | Type | Weight | Description |
|---|--------|------|--------|-------------|
| 1 | **Inverse Whale** | On-chain | HIGH | Fade losing traders (<50% accuracy) |
| 2 | **Smart Money Flow** | On-chain | MEDIUM | Follow net flow from accurate traders |
| 3 | **Simmer Divergence** | Cross-platform | MEDIUM | Price gaps vs Polymarket |
| 4 | **Volume Spikes** | Technical | LOW | Z-score anomaly (2σ+ activity) |
| 5 | **New Markets** | Calendar | LOW | Early mover on new markets |
| 6 | **Resolution Timing** | Calendar | LOW | High uncertainty near expiry |
| 7 | **Vegas Edge** | Sharp odds | HIGH | Sports lines vs Polymarket |
| 8 | **Soccer Edge** | Sharp odds | HIGH | Futures vs Polymarket |
| 9 | **Betfair Edge** | Sharp odds | HIGH | Exchange odds vs Polymarket |
| 10 | **Manifold Edge** | Leading indicator | MEDIUM | Play money signals |
| 11 | **PredictIt Edge** | Cross-platform | MEDIUM | Politics price gaps |
| 12 | **Kalshi Overlap** | Cross-platform | MEDIUM | Market matching |

---

## Profit Strategies

### 1. Sharp vs Soft Line Arbitrage
```
Sharp (Betfair/Vegas) = True probability (professional bettors)
Soft (Polymarket) = Retail sentiment (crypto degens)
EDGE: Trade Poly toward sharp price when gap > 5%
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

---

## Trading Engine

### Features
- **Real-time scanning**: Every 30 seconds
- **Adaptive confidence**: Gets stricter as trades accumulate (+3/trade, -1/30min decay)
- **Drawdown breaker**: Halts at 5% daily loss
- **Opportunity cost engine**: Rotates weak positions for better signals
- **Kelly sizing**: Quarter-Kelly (25%) for conservative sizing

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
| `GET /api/vegas/edge` | Vegas vs Polymarket (NFL/NBA/NHL) |
| `GET /api/vegas/soccer` | Soccer futures vs Polymarket |
| `GET /api/betfair/edge` | Betfair Exchange vs Polymarket |
| `GET /api/manifold/edge` | Manifold vs Polymarket |
| `GET /api/manifold/markets` | Manifold market summary |
| `GET /api/predictit/edge` | PredictIt vs Polymarket |
| `GET /api/predictit/markets` | PredictIt market summary |
| `GET /api/kalshi/markets` | Kalshi overlap detection |

### Edge Cache
| Endpoint | Description |
|----------|-------------|
| `GET /api/edge/cache` | Cached edge signals (fast) |
| `POST /api/edge/refresh` | Force refresh edge cache |

### Signals
| Endpoint | Description |
|----------|-------------|
| `GET /api/signals` | Aggregated signals (all 12 sources) |
| `GET /api/signals/news` | Google News + Reddit signals |
| `GET /api/inverse-whale` | Whale fade signals |
| `GET /api/smart-money` | Smart money flow |
| `GET /api/volume/spikes` | Volume anomalies |

### Engine Control
| Endpoint | Description |
|----------|-------------|
| `GET /api/engine/status` | Engine status + adaptive state |
| `POST /api/engine/start` | Start trading engine |
| `POST /api/engine/stop` | Stop trading engine |
| `POST /api/engine/trigger` | Force evaluation cycle |
| `POST /api/engine/reset-daily` | Reset adaptive boost + drawdown |

### Paper Trading
| Endpoint | Description |
|----------|-------------|
| `GET /api/paper/status` | Paper account ($10K) |
| `GET /api/paper/positions` | Open positions |
| `GET /api/paper/positions/ev` | Positions ranked by EV |
| `GET /api/rotations` | Position rotation history |

### Whale Tracking
| Endpoint | Description |
|----------|-------------|
| `GET /api/whales` | Tracked whale wallets |
| `GET /api/whales/signals` | Copy trade signals |
| `GET /api/predictors` | Accuracy leaderboard |

---

## Cron Jobs

| Job | Schedule | Description |
|-----|----------|-------------|
| `vegas-edge-scanner` | Every 2h | Scan sports edges, alert on 8%+ |
| `polyclawd-monitor` | Every 2h | Full system check + alerts |
| `polyclawd-rotation-alert` | Every 30m | Position rotation notifications |

---

## Current Edges (Live)

As of 2026-02-07:
- 🔥 **Real Madrid La Liga**: Vegas 84% vs Poly 65% = **+19% edge**
- **Vance 2028 GOP**: PredictIt 55¢ (cross-ref with Poly)
- **Newsom 2028 DEM**: PredictIt 26¢, Betfair shows -9.8% vs Poly

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

```
┌─────────────────────────────────────────────────────────────┐
│                     DATA SOURCES                            │
├─────────────┬─────────────┬─────────────┬─────────────────-─┤
│ Polymarket  │ PredictIt   │ Manifold    │ Vegas/Betfair    │
│ Kalshi      │ Smarkets    │ Simmer      │ Soccer Futures   │
└──────┬──────┴──────┬──────┴──────┬──────┴────────┬─────────-┘
       │             │             │               │
       └─────────────┴──────┬──────┴───────────────┘
                            ▼
                 ┌─────────────────────┐
                 │    EDGE CACHE       │
                 │  (5 min refresh)    │
                 └──────────┬──────────┘
                            ▼
                 ┌─────────────────────┐
                 │  SIGNAL AGGREGATOR  │
                 │  (12 sources)       │
                 └──────────┬──────────┘
                            ▼
                 ┌─────────────────────┐
                 │  BAYESIAN SCORING   │
                 │  + Composite Boost  │
                 └──────────┬──────────┘
                            ▼
                 ┌─────────────────────┐
                 │  TRADING ENGINE     │
                 │  Adaptive + Kelly   │
                 └──────────┬──────────┘
                            ▼
                 ┌─────────────────────┐
                 │  PAPER TRADING      │
                 │  $10K Account       │
                 └──────────┬──────────┘
                            ▼
                 ┌─────────────────────┐
                 │  CRON ALERTS        │
                 │  → Telegram         │
                 └─────────────────────┘
```

---

## License

Proprietary - Virtuoso Crypto

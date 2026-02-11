# Polyclawd API Endpoints

Complete reference for all REST API endpoints.

**Base URL:** `https://virtuosocrypto.com/polyclawd/api`

---

## Table of Contents

- [System](#system)
- [Signals](#signals)
- [Confidence & Learning](#confidence--learning)
- [Markets](#markets)
- [Vegas Odds](#vegas-odds)
- [ESPN Odds](#espn-odds)
- [Edge Scanners](#edge-scanners)
- [Trading](#trading)
- [Paper Trading](#paper-trading)
- [Simmer Integration](#simmer-integration)
- [Engine](#engine)
- [Phases & Kelly](#phases--kelly)
- [Alerts](#alerts)

---

## System

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/ready` | Readiness check (all dependencies) |
| GET | `/metrics` | System metrics (uptime, requests, etc.) |

### GET /health
Returns API health status.

**Response:**
```json
{
  "status": "healthy",
  "version": "2.0.0",
  "timestamp": "2026-02-08T13:00:00Z"
}
```

---

## Signals

Core signal aggregation endpoints.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/signals` | All aggregated signals from all sources |
| GET | `/signals/news` | News signals (Google News + Reddit) |
| POST | `/signals/auto-trade` | Execute automated paper trading |
| GET | `/volume/spikes` | Volume spike detection (Z-score) |
| GET | `/resolution/approaching` | Markets approaching resolution |
| GET | `/resolution/imminent` | Markets resolving in <24h |
| GET | `/inverse-whale` | Inverse whale signals (fade losers) |
| GET | `/smart-money` | Smart money flow analysis |
| GET | `/rotations` | Position rotation history |
| GET | `/rotation/candidates` | Weak positions to exit |

### GET /signals

Aggregates signals from all sources with Bayesian confidence scoring.

**Response:**
```json
{
  "signals": [
    {
      "source": "inverse_whale",
      "platform": "polymarket",
      "market": "Will Trump win 2028?",
      "side": "NO",
      "confidence": 72.5,
      "bayesian_confidence": 78.3,
      "reasoning": "Fade 3 losing whale(s) with 42% accuracy",
      "price": 0.45
    }
  ],
  "count": 15,
  "scan_time": "2026-02-08T13:00:00Z"
}
```

### GET /volume/spikes

Detect unusual volume activity using statistical analysis.

**Query Parameters:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| threshold | float | 2.0 | Z-score threshold |
| use_zscore | bool | true | Use Z-score vs ratio method |

**Response:**
```json
{
  "spikes": [
    {
      "market_id": "abc123",
      "title": "Will X happen?",
      "current_volume": 150000,
      "z_score": 3.2,
      "spike_ratio": 4.5,
      "yes_price": 0.65,
      "url": "https://polymarket.com/event/..."
    }
  ],
  "mean_volume": 25000,
  "std_volume": 40000,
  "method": "zscore"
}
```

---

## Confidence & Learning

Bayesian confidence scoring and outcome tracking.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/confidence/sources` | Win rates by signal source |
| POST | `/confidence/record` | Record trade outcome for learning |
| GET | `/confidence/market/{market_id}` | Confidence data for specific market |
| GET | `/confidence/history` | Historical confidence records |
| GET | `/confidence/calibration` | Calibration statistics |
| GET | `/conflicts/stats` | Signal conflict statistics |
| GET | `/conflicts/active` | Currently conflicting signals |

### GET /confidence/sources

Get win rates for each signal source (used in Bayesian scoring).

**Response:**
```json
{
  "sources": {
    "inverse_whale": {"wins": 45, "losses": 35, "total": 80, "win_rate": 0.56},
    "smart_money": {"wins": 52, "losses": 48, "total": 100, "win_rate": 0.52},
    "volume_spike": {"wins": 30, "losses": 70, "total": 100, "win_rate": 0.30}
  }
}
```

### POST /confidence/record

Record outcome for Bayesian learning.

**Query Parameters:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| source | string | Yes | Signal source name |
| won | bool | Yes | Whether trade won |
| market_title | string | No | Market title for logging |

---

## Markets

Market discovery and search.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/arb-scan` | Cross-platform arbitrage opportunities |
| GET | `/rewards` | Active rewards programs |
| GET | `/markets/trending` | Trending Polymarket markets |
| GET | `/markets/search` | Search markets by query |
| GET | `/markets/new` | Newly created markets |
| GET | `/markets/opportunities` | Mispriced/high-volume markets |
| GET | `/markets/{market_id}` | Get specific market details |
| GET | `/polymarket/events` | Polymarket events list |

### GET /markets/search

Search Polymarket markets.

**Query Parameters:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| q | string | - | Search query |
| limit | int | 20 | Max results |

---

## Vegas Odds

Sports futures from Vegas/Pinnacle (sharp lines).

| Method | Path | Description |
|--------|------|-------------|
| GET | `/vegas/sports` | All available sports |
| GET | `/vegas/odds` | All Vegas odds |
| GET | `/vegas/edge` | Vegas vs Polymarket edge |
| GET | `/vegas/soccer` | All soccer futures |
| GET | `/vegas/epl` | English Premier League futures |
| GET | `/vegas/ucl` | UEFA Champions League futures |
| GET | `/vegas/bundesliga` | Bundesliga futures |
| GET | `/vegas/laliga` | La Liga futures |
| GET | `/vegas/worldcup` | World Cup futures |
| GET | `/vegas/nfl` | NFL odds (spreads, moneylines) |
| GET | `/vegas/nfl/superbowl` | Super Bowl winner futures |

### GET /vegas/edge

Find edge between Vegas sharp lines and Polymarket.

**Query Parameters:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| min_edge | float | 3.0 | Minimum edge % to return |

**Response:**
```json
{
  "opportunities": [
    {
      "team": "Kansas City Chiefs",
      "vegas_prob": 0.22,
      "poly_prob": 0.18,
      "edge_pct": 4.2,
      "action": "BUY Chiefs YES on Polymarket"
    }
  ]
}
```

---

## ESPN Odds

Real-time game odds from ESPN.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/espn/odds` | All ESPN odds |
| GET | `/espn/edge` | ESPN vs Polymarket edge |
| GET | `/espn/nfl` | NFL game odds |
| GET | `/espn/nba` | NBA game odds |
| GET | `/espn/nhl` | NHL game odds |
| GET | `/espn/mlb` | MLB game odds |
| GET | `/espn/ncaaf` | College football odds |
| GET | `/espn/ncaab` | College basketball odds |
| GET | `/espn/moneyline/{sport}` | Moneyline odds for sport |
| GET | `/espn/moneylines` | All moneylines across sports |

---

## Edge Scanners

Cross-platform edge detection.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/scan` | Run full edge scan (all platforms) |
| GET | `/topics` | Hot topics by platform |
| GET | `/betfair/edge` | Betfair exchange edge |
| GET | `/kalshi/markets` | Kalshi markets |
| GET | `/manifold/edge` | Manifold vs Polymarket edge |
| GET | `/manifold/markets` | Manifold markets |
| GET | `/predictit/edge` | PredictIt vs Polymarket edge |
| GET | `/predictit/markets` | PredictIt markets |
| GET | `/polyrouter/markets` | PolyRouter markets (7 platforms) |
| GET | `/polyrouter/search` | Search PolyRouter |
| GET | `/polyrouter/edge` | PolyRouter edge opportunities |
| GET | `/polyrouter/sports/{league}` | Sports by league |
| GET | `/polyrouter/futures/{league}` | Futures by league |
| GET | `/polyrouter/platforms` | Available platforms |
| GET | `/metaculus/questions` | Metaculus questions |
| GET | `/metaculus/edge` | Metaculus edge |

---

## Trading

Live trading operations (requires API key).

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| GET | `/balance` | Current balance | No |
| GET | `/positions` | Current positions | No |
| GET | `/trades` | Recent trades | No |
| POST | `/trade` | Execute trade | **Yes** |
| POST | `/reset` | Reset paper trading | **Yes** |
| GET | `/positions/check` | Check position status | No |
| POST | `/positions/{id}/resolve` | Manually resolve position | **Yes** |

### POST /trade

Execute a trade (paper or live).

**Headers:**
```
X-API-Key: your-api-key
```

**Query Parameters:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| market_id | string | Yes | Market identifier |
| side | string | Yes | YES or NO |
| amount | float | Yes | Amount in USD |
| platform | string | No | polymarket, kalshi, etc. |

---

## Paper Trading

Paper trading simulation.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/paper/status` | Paper trading account status |
| GET | `/paper/positions` | Paper positions |
| POST | `/paper/trade` | Execute paper trade |

---

## Simmer Integration

Simmer prediction market integration.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/simmer/status` | Simmer connection status |
| GET | `/simmer/portfolio` | Simmer portfolio |
| GET | `/simmer/positions` | Simmer positions |
| GET | `/simmer/trades` | Simmer trade history |
| POST | `/simmer/trade` | Execute Simmer trade |
| GET | `/simmer/context/{market_id}` | Market context from Simmer |

---

## Engine

Automated trading engine control.

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| GET | `/engine/status` | Engine status | No |
| POST | `/engine/start` | Start engine | No |
| POST | `/engine/stop` | Stop engine | No |
| GET | `/engine/config` | Get engine config | No |
| POST | `/engine/config` | Update engine config | No |
| POST | `/engine/trigger` | Manually trigger scan | No |
| POST | `/engine/reset-daily` | Reset daily counters | No |

### GET /engine/status

**Response:**
```json
{
  "running": true,
  "mode": "paper",
  "scan_interval": 300,
  "last_scan": "2026-02-08T12:55:00Z",
  "trades_today": 5,
  "daily_pnl": 45.50,
  "phase": "seed"
}
```

---

## Phases & Kelly

Position sizing and phase management.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/phase/current` | Current scaling phase |
| GET | `/phase/history` | Phase transition history |
| GET | `/phase/config` | Phase configuration |
| GET | `/phase/limits` | Current limits (trades, exposure) |
| POST | `/phase/simulate` | Simulate position sizing |
| GET | `/kelly/current` | Current Kelly fraction |
| GET | `/kelly/simulate` | Simulate Kelly calculation |

### POST /phase/simulate

Simulate position sizing for given parameters.

**Query Parameters:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| balance | float | 1000 | Account balance |
| confidence | float | 50 | Signal confidence 0-100 |
| win_rate | float | 0.55 | Recent win rate |
| win_streak | int | 0 | Current streak |
| source_agreement | int | 1 | Number of agreeing sources |

**Response:**
```json
{
  "position_usd": 55.00,
  "position_pct": 0.055,
  "kelly_raw": 0.20,
  "kelly_adjusted": 0.25,
  "phase": "seed",
  "multipliers": {
    "performance": 1.0,
    "streak": 1.0,
    "agreement": 1.1
  }
}
```

---

## Alerts

Custom alerts management.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/alerts` | List all alerts |
| POST | `/alerts` | Create new alert |
| DELETE | `/alerts/{alert_id}` | Delete alert |

---

## Whale Tracking

| Method | Path | Description |
|--------|------|-------------|
| GET | `/predictors` | Whale accuracy tracking |
| POST | `/predictors/update` | Update predictor stats |

---

## Authentication

Protected endpoints require the `X-API-Key` header:

```bash
curl -X POST "https://virtuosocrypto.com/polyclawd/api/trade" \
  -H "X-API-Key: your-api-key" \
  -d "market_id=abc123&side=YES&amount=50"
```

API keys are configured in `.env`:
```
POLYCLAWD_API_KEYS=key1,key2,admin-key
```

---

## Rate Limits

- **Default:** 60 requests/minute per IP
- **Authenticated:** 120 requests/minute per key
- Edge scanners have internal caching (60s TTL)

---

## Error Responses

All errors return standard format:

```json
{
  "detail": "Error message here",
  "status_code": 400
}
```

| Code | Meaning |
|------|---------|
| 400 | Bad request (invalid params) |
| 401 | Unauthorized (missing/invalid API key) |
| 404 | Not found |
| 429 | Rate limit exceeded |
| 500 | Internal server error |

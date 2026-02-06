# Polyclawd - AI-Powered Prediction Market Trading Bot

**Virtuoso Crypto's intelligent trading system for Polymarket and Simmer.**

## Features

### Signal Sources (9 Active)
| Source | Description | Platform |
|--------|-------------|----------|
| **Inverse Whale** | Fade positions of losing traders (<50% accuracy) | Polymarket |
| **Smart Money Flow** | Follow net weighted flow from accurate traders | Polymarket |
| **Simmer Divergence** | Exploit price differences vs Polymarket | Simmer |
| **Volume Spikes** | Z-score anomaly detection (2σ+ unusual activity) | Polymarket |
| **New Markets** | Early mover on newly created markets | Polymarket |
| **Resolution Timing** | High uncertainty markets near expiry | Polymarket |
| **Price Alerts** | User-defined price triggers | Any |
| **Cross-Arb** | Cross-platform arbitrage (strict/related) | Multi |
| **Whale Activity** | Copy new positions from tracked whales | Polymarket |

### Trading Engine
- **Real-time**: Scans every 30 seconds
- **Bayesian Confidence**: Learns from outcomes, adjusts source weights
- **Composite Scoring**: Multiple agreeing sources = boosted confidence
- **Auto Paper Trading**: Execute on Simmer, log Polymarket for manual
- **Position Tracking**: Auto-resolve and update P&L

### Risk Management
- Kelly Criterion position sizing
- Configurable min confidence threshold
- Daily trade limits
- Cooldown between trades
- Max position % of bankroll

## Quick Start

```bash
# Start the API
cd ~/Desktop/polyclawd
source venv/bin/activate
uvicorn api.main:app --host 127.0.0.1 --port 8000
```

## API Endpoints

### Engine Control
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/engine/status` | GET | Engine status and config |
| `/api/engine/start` | POST | Start trading engine |
| `/api/engine/stop` | POST | Stop trading engine |
| `/api/engine/trigger` | POST | Force one evaluation |
| `/api/engine/config` | POST | Update thresholds |

### Signals
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/signals` | GET | Aggregated signals from all sources |
| `/api/signals/auto-trade` | POST | Execute paper trades on signals |
| `/api/inverse-whale` | GET | Inverse whale signals |
| `/api/smart-money` | GET | Smart money flow |
| `/api/volume/spikes` | GET | Volume anomalies |
| `/api/resolution/approaching` | GET | Markets near expiry |
| `/api/markets/new` | GET | New market detection |

### Confidence & Learning
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/confidence/sources` | GET | Win rates per source |
| `/api/confidence/record` | POST | Record trade outcome |
| `/api/positions/check` | GET | Check & resolve positions |
| `/api/positions/{id}/resolve` | POST | Manual resolution |

### Paper Trading
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/paper/status` | GET | Paper account status |
| `/api/paper/reset` | POST | Reset to $10,000 |

### Simmer Integration
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/simmer/status` | GET | Simmer connection status |
| `/api/simmer/opportunities` | GET | Price divergence opps |
| `/api/simmer/auto-trade` | POST | Execute Simmer trades |

### Whale Tracking
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/whales` | GET | List tracked whales |
| `/api/whales/signals` | GET | Whale copy signals |
| `/api/whales/activity` | GET | Position changes |
| `/api/predictors` | GET | Predictor accuracy leaderboard |

### Cross-Platform Arb
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/cross-arb/strict` | GET | True arbitrage (identical markets) |
| `/api/cross-arb/related` | GET | Correlated markets |

### Utilities
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/kelly` | GET | Kelly position sizing calc |
| `/api/alerts` | GET/POST | Price alerts |
| `/api/webhooks` | GET/POST | Webhook subscriptions |
| `/api/health` | GET | API health check |

## Bayesian Confidence Formula

```
Final Confidence = Base × Bayesian_Mult × Composite_Mult

Base:       Raw score normalized to 0-100
Bayesian:   source_win_rate / 0.5 (60% = 1.2x, 40% = 0.8x)
Composite:  1 + 0.2 per agreeing source (max 2x)
```

## Configuration

Engine config via `/api/engine/config`:
- `min_confidence`: Minimum score to trade (default: 20)
- `max_per_trade`: Maximum $ per trade (default: 100)
- `max_daily_trades`: Daily trade limit (default: 20)
- `cooldown_minutes`: Minutes between trades (default: 5)
- `max_position_pct`: Max % of bankroll per trade (default: 5%)

## Deployment

**Local:** `http://127.0.0.1:8000`
**Production:** `https://virtuosocrypto.com/polyclawd`

## License

Proprietary - Virtuoso Crypto

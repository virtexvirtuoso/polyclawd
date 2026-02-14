# Polyclawd System Tracker

> Living document — current state of all system components.

Last updated: 2026-02-13 22:12 EST

---

## Pipeline Architecture

```
Market Data (Kalshi + Polymarket)
    │
    ▼
Signal Sources (7)
    │
    ▼
Auto-Calibrator ←── Calibration Curves (per-source)
    │
    ▼
Score Velocity Modifier ←── Alpha Score Snapshots
    │
    ▼
Bayesian Aggregator ←── IC-Squared Source Weights
    │
    ▼
Daily-Expiry Boost (sort priority)
    │
    ▼
IC Tracker (record prediction)
    │
    ▼
Paper Portfolio (Kelly sizing w/ variable odds)
    │
    ▼
Shadow Tracker (dedup, single side per market)
    │
    ▼
Resolution Scanner (5min) ──► IC Resolver ──► Calibrator Update
```

---

## Signal Sources

| # | Source | Status | IC | Notes |
|---|--------|--------|-----|-------|
| 1 | `mispriced_category` (Kalshi) | 🟢 Active | Pending | Fades the crowd in mispriced categories |
| 2 | `mispriced_category` (Polymarket) | 🟢 Active | Pending | Same strategy, cross-platform |
| 3 | `resolution_certainty` | 🟢 Active | Pending | Near-certain outcomes from real-time data |
| 4 | `volume_spike` | 🟢 Active | Pending | Z-score volume anomalies |
| 5 | `inverse_whale` | 🟢 Active | Pending | Fade large positions |
| 6 | `smart_money_flow` | 🟢 Active | Pending | Track informed capital |
| 7 | `ai_model_tracker` | 🟢 Active | Pending | Arena leaderboard → AI market signals |

IC status updates automatically after 10+ resolved trades per source.

---

## Feedback Loops

| Loop | Frequency | Data Source | Action | Kicks In |
|------|-----------|-------------|--------|----------|
| IC Tracking | Every signal scan (10min) | `signal_predictions` table | Records confidence + side for every signal | ✅ Live now |
| Auto-Resolution | Every 5min | `shadow_trades` + market data | Resolves trades, feeds outcomes to IC | ✅ Live now |
| Calibration Curves | On-demand + per-scan | `signal_predictions` resolved | Adjusts confidence (predicted vs actual win rate) | After 20 resolved |
| Source Weights | On-demand | IC measurements | IC² weighting (high IC = more influence) | After 10 resolved/source |
| Signal Decay | On-demand | Resolution timestamps | Measures IC at different time horizons | After 20 resolved |
| Side-Flip Rejection | Every signal scan | `shadow_trades` | Blocks conflicting YES/NO on same market | ✅ Live now |

---

## Data Collection

| Table | Records | Frequency | Purpose |
|-------|---------|-----------|---------|
| `alpha_snapshots` | 930+ | Every 10min | Confluence scores for 10 symbols |
| `price_snapshots` | 186+ | Every 10min | BTC + ETH price tracking |
| `shadow_trades` | 16 open | Every 10min | Trade tracking with dedup |
| `signal_predictions` | 16 unresolved | Every 10min | IC measurement raw data |
| `calibration_curves` | 0 | After 20 resolved | Per-source accuracy data |
| `source_weights` | 0 | After 10 resolved/source | Optimal aggregation weights |
| `ic_measurements` | 0 | After 10 resolved/source | Historical IC values |

---

## API Endpoints

### Core Signals
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/signals` | GET | All aggregated signals (slow, ~30s) |
| `/api/signals/mispriced-category` | GET | Category mispricing signals |
| `/api/signals/resolution-certainty` | GET | Near-certain outcome signals |
| `/api/signals/ai-models` | GET | AI model market signals |
| `/api/signals/ai-models/trends` | GET | Arena leaderboard trends |

### Intelligence
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/signals/alpha-snapshot` | GET | Fresh confluence scores + BTC/ETH |
| `/api/signals/alpha-history/{symbol}` | GET | Score history + 2h/6h/24h deltas |
| `/api/signals/btc-tracker` | GET | BTC/ETH price history + deltas |

### Feedback System
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/signals/ic-report` | GET | IC report across all sources |
| `/api/signals/ic/{source}` | GET | Per-source IC detail |
| `/api/signals/calibration` | GET | Full calibration report |
| `/api/signals/calibration/{source}` | GET | Per-source curves + decay |
| `/api/signals/source-weights` | GET | IC-squared optimal weights |

### Portfolio
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/portfolio/status` | GET | Paper portfolio status |
| `/api/portfolio/positions` | GET | Open/closed positions |
| `/api/portfolio/history` | GET | Trade history |
| `/api/signals/shadow-performance` | GET | Shadow trade stats |

---

## Fixes Applied (Feb 13)

| Fix | Status | Commit |
|-----|--------|--------|
| Kelly variable odds | ✅ | `52da7fb` |
| Sharpe Bessel's correction | ✅ | `52da7fb` |
| PRAGMA busy_timeout | ✅ | `52da7fb` |
| Side logic (fade market) | ✅ | `7dd1405` |
| Shadow trade dedup (per market) | ✅ | `7dd1405` |
| Score velocity modifier | ✅ | `52da7fb` |
| IC tracker | ✅ | `52da7fb` |
| Auto-calibrator | ✅ | `62c03da` |
| 10min scan frequency | ✅ | `c8ad48c` |
| Daily-expiry sort boost | ✅ | `c8ad48c` |

---

## Milestones

| Milestone | Target | Status |
|-----------|--------|--------|
| First resolved trade | Feb 13-14 | ⏳ Waiting (BTC/ETH Feb 13 markets) |
| 20 resolved trades | ~Feb 17 | 🔲 Calibration kicks in |
| 50 resolved trades | ~Feb 21 | 🔲 Early calibration curves |
| 100 resolved trades | ~Feb 28 | 🔲 Meaningful IC readings |
| 200 resolved trades | ~Mar 7 | 🔲 Walk-forward ready, go/no-go on live capital |
| Kill first bad source | After 50 resolved | 🔲 IC < 0.03 |
| First calibration adjustment | After 20 resolved | 🔲 Auto-applied |

---

## Watchdog Schedule (v5)

| Interval | Tasks |
|----------|-------|
| Every 5min | Health check, shadow trade resolution, resolution certainty scan |
| Every 10min | Signal scan, paper portfolio, alpha score snapshot, IC recording |
| Every 6hr | Arena leaderboard snapshot |

---

## Key Files

| File | Purpose |
|------|---------|
| `signals/mispriced_category_signal.py` | Main signal generator (Kalshi + Polymarket) |
| `signals/alpha_score_tracker.py` | Confluence score + price snapshots |
| `signals/ic_tracker.py` | Information Coefficient measurement |
| `signals/calibrator.py` | Auto-calibration + source weights |
| `signals/shadow_tracker.py` | Shadow trade logging + resolution |
| `signals/paper_portfolio.py` | Paper portfolio engine |
| `signals/resolution_scanner.py` | Resolution certainty scanner |
| `signals/ai_model_tracker.py` | Arena leaderboard tracking |
| `config/scaling_phases.py` | Kelly sizing (variable odds) |
| `api/routes/signals.py` | All signal + calibration endpoints |
| `storage/shadow_trades.db` | SQLite — all tables |
| `scripts/polyclawd-watchdog.sh` | Watchdog v5 cron script |

---

## Remaining Work

### P1 — Before Paper Validation
- [ ] Staleness detection for data sources
- [ ] Per-strategy allocation limits
- [ ] Directional exposure limits (max 50% net-long BTC)
- [ ] Vol scaling sqrt(T) for Strategy 2 (Price-to-Strike)

### P2 — Before Live Capital
- [ ] Walk-forward validation (3+ months data)
- [ ] Fill probability model
- [ ] Benjamini-Hochberg FDR correction

### P3 — Production Quality
- [ ] Database split (market_data.db vs shadow_trades.db)
- [ ] Data retention policy (90-day rolling)
- [ ] `/api/alpha/health` endpoint
- [ ] CoinGecko fallback for prices

### Strategies to Build
- [ ] Strategy 2: Price-to-Strike (vol-adjusted probability)
- [ ] Strategy 3: Cross-Asset Regime Detection
- [ ] BTC Wiz on-chain composite integration
- [ ] News Event Matcher (RSS/Google News)
- [ ] Cross-Platform Velocity tracking

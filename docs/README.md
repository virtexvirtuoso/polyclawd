# Polyclawd Documentation

## 📁 Structure

```
docs/
├── api/                    # API & Integration docs
│   ├── MCP_TOOLS.md        # 69 MCP tools reference
│   ├── ENDPOINTS.md        # All API endpoints
│   └── THE_ODDS_API_PRICING.md
│
├── architecture/           # System design & refactoring
│   ├── REFACTORING_PLAN.md
│   ├── refactoring-prd.json
│   └── 2026-02-08_MODULAR_REFACTORING_QA_VALIDATION.md
│
├── strategy/               # Trading strategy docs
│   ├── INTELLIGENCE_FRAMEWORK.md  # 12 intelligence types
│   ├── CONFIDENCE_SCORING.md      # Bayesian + Shin method
│   ├── EDGE_QUICK_REFERENCE.md    # Formulas cheat sheet
│   ├── SCALING_STRATEGY.md        # Phase-based scaling
│   ├── EFFICIENCY_ROADMAP.md
│   └── KEYWORD_LEARNING.md        # Bayesian keyword learner
│
├── operations/             # Monitoring & alerting
│   ├── CRON_JOBS.md        # All automated monitoring jobs
│   ├── SETUP.md            # Installation & deployment
│   └── TROUBLESHOOTING.md  # Common issues & fixes
│
└── integrations/           # External platform integrations
    ├── VEGAS-POLYMARKET-EDGE-FINDER.md
    └── DATA_SOURCES.md     # All data source APIs
```

## 🚀 Quick Links

### For Claude/MCP Users
- **[MCP Tools Reference](api/MCP_TOOLS.md)** - All 69 tools with usage examples
- **[Intelligence Framework](strategy/INTELLIGENCE_FRAMEWORK.md)** - 12 types of edge detection

### For Developers
- **[API Endpoints](api/ENDPOINTS.md)** - Full REST API documentation
- **[Refactoring Plan](architecture/REFACTORING_PLAN.md)** - Modular architecture design

### For Traders
- **[Confidence Scoring](strategy/CONFIDENCE_SCORING.md)** - Shin method, Laplace smoothing, Kelly sizing
- **[Edge Quick Reference](strategy/EDGE_QUICK_REFERENCE.md)** - One-page formula cheat sheet
- **[Scaling Strategy](strategy/SCALING_STRATEGY.md)** - Phase-based position sizing

### For Operations
- **[Cron Jobs](operations/CRON_JOBS.md)** - All 10 automated monitoring jobs
- **[Setup Guide](operations/SETUP.md)** - Installation & configuration

---

## 📊 System Overview

Polyclawd is an AI-powered prediction market trading system with:

- **12 intelligence types** (cross-platform arb, sharp books, whale walls, injuries, etc.)
- **69 MCP tools** for Claude integration
- **10 automated cron jobs** monitoring all intelligence types
- **9 prediction platforms** (Polymarket, Kalshi, Manifold, PredictIt, Metaculus, PolyRouter, Betfair, Vegas)
- **Sophisticated edge math** (Shin method, Laplace smoothing, Kelly criterion)
- **Bayesian confidence scoring** that learns from outcomes
- **Paper trading engine** with phase-based scaling

---

## 🧠 Intelligence Types

| # | Type | Source | Alert |
|---|------|--------|-------|
| 1 | Cross-platform arb | All platforms | Every 6h |
| 2 | Sharp vs soft divergence | Vegas books | Every 2h |
| 3 | Expert vs crowd | Metaculus | Via signals |
| 4 | Whale behavior | Polymarket | Every 2h |
| 5 | Orderbook microstructure | CLOB | Every 4h |
| 6 | Injury impact | ESPN | Every 3h |
| 7 | Resolution timing | Polymarket | Every 2h |
| 8 | Correlation violations | Cross-market | Every 4h |
| 9 | Manifold wisdom | Top traders | Via signals |
| 10 | Vegas edge | Sharp books | Every 2h |
| 11 | Entertainment props | Kalshi | 3x daily |
| 12 | Calibration feedback | Historical | Weekly |

---

## 🔧 Key Formulas

```python
# Shin method (heavy favorites)
true_prob = (implied_prob - s) / (1 - 2*s)

# Laplace smoothing
smoothed_wr = (wins + 4) / (total + 8)

# Combined decision rule
adjusted_edge = |edge%| × (confidence / 100)
should_bet = adjusted_edge > 3.0

# Kelly sizing
kelly = edge / (1 - price)  # for YES
```

---

*Last updated: 2026-02-08*

# Polyclawd Changelog

All notable changes to Polyclawd.

---

## [2.0.0] - 2026-02-08

### 🎉 Major Release - AI-Powered Prediction Market Trading

#### Added

**MCP Integration (52 Tools)**
- Full Model Context Protocol server for Claude integration
- 52 MCP tools across all categories
- Stdio-based server in `mcp/server.py`
- Production endpoint: `https://virtuosocrypto.com/polyclawd`

**Signal Sources (13 Active)**
- News signals: Google News RSS + Reddit JSON API
- Volume spike detection with Z-score analysis
- Smart money flow (accuracy-weighted whale tracking)
- Inverse whale signals (fade losing traders)
- Resolution timing opportunities

**Cross-Platform Edge Detection**
- Kalshi integration with market matching
- Manifold leading indicator detection
- Metaculus expert forecaster signals
- PredictIt US politics coverage
- Betfair sharp odds via The Odds API
- PolyRouter unified platform access (7 platforms)

**Vegas Sports Odds**
- NFL: game lines, spreads, Super Bowl futures
- NBA, NHL, MLB coverage
- Soccer futures: EPL, UCL, La Liga, Bundesliga, World Cup
- Automatic devigging for true probabilities
- Edge detection vs Polymarket

**ESPN Integration**
- Real-time moneylines across 6 sports
- True probability calculation
- Edge scanner vs prediction markets
- Sports: NFL, NBA, NHL, MLB, NCAAF, NCAAB

**Bayesian Confidence Scoring**
- Source-level win rate tracking
- Automatic Bayesian multiplier adjustment
- Multi-source agreement detection
- Composite confidence boosting
- Conflict logging and analysis

**Kelly Criterion Position Sizing**
- Phase-based position limits (Seed → Preservation)
- Performance multipliers (hot hand, cold streak)
- Win streak adjustments
- Source agreement bonuses
- Daily loss limits per phase

**Paper Trading Engine**
- Automated signal-based trading
- Position tracking and P&L
- Daily trade limits
- Cooldown after losses
- Simmer integration

**Operations**
- OpenClaw cron integration
- Health monitoring jobs
- Edge scanner automation
- Rotation alerts

#### Changed
- Complete rewrite from v1.x
- FastAPI backend (from Flask)
- Modular route structure
- Centralized configuration

#### Removed
- Deprecated platforms: Zeitgeist, Polkamarkets, Omen
- Legacy API endpoints

---

## [1.5.0] - 2026-01-15

### Added
- Initial Simmer integration
- Basic whale tracking
- Volume alerts

---

## [1.0.0] - 2025-12-01

### Added
- Initial release
- Polymarket API integration
- Basic signal aggregation
- Paper trading

---

## Roadmap

### Planned for 2.1.0
- [ ] Live Polymarket trading
- [ ] Advanced backtesting
- [ ] ML-based signal weighting
- [ ] More sports coverage

### Planned for 2.2.0
- [ ] Mobile notifications
- [ ] Portfolio dashboard
- [ ] Historical performance charts
- [ ] Automated strategy optimization

---

## Version Numbering

Polyclawd follows [Semantic Versioning](https://semver.org/):

- **MAJOR**: Breaking API changes
- **MINOR**: New features, backward compatible
- **PATCH**: Bug fixes, backward compatible

# Polyclawd MCP Tools

69 tools for prediction market trading via Claude/MCP integration.

## Quick Start

```bash
# List all tools
mcporter list polyclawd

# Call a tool
mcporter call polyclawd.polyclawd_signals

# Call with arguments
mcporter call polyclawd.polyclawd_espn_moneyline '{"sport":"nba"}'
```

---

## Signals (5 tools)

| Tool | Description |
|------|-------------|
| `polyclawd_signals` | All aggregated trading signals from all sources |
| `polyclawd_news` | News-based signals from Google News and Reddit |
| `polyclawd_volume_spikes` | Unusual trading activity signals |
| `polyclawd_smart_money` | Whale wallet signals |
| `polyclawd_inverse_whale` | Fade-the-whales signals |

---

## Arbitrage & Edge (7 tools)

Cross-platform edge detection.

| Tool | Description |
|------|-------------|
| `polyclawd_arb_scan` | Scan for cross-platform arbitrage |
| `polyclawd_kalshi_edge` | Kalshi vs Polymarket edge |
| `polyclawd_manifold_edge` | Manifold vs Polymarket edge |
| `polyclawd_metaculus_edge` | Metaculus vs Polymarket edge |
| `polyclawd_predictit_edge` | PredictIt vs Polymarket edge |
| `polyclawd_betfair_edge` | Betfair exchange edge |
| `polyclawd_polyrouter_edge` | PolyRouter cross-platform edge (7 platforms) |

---

## Vegas Odds (6 tools)

Sportsbook odds from VegasInsider.

| Tool | Description |
|------|-------------|
| `polyclawd_vegas_nfl` | NFL futures (Super Bowl, AFC, NFC) |
| `polyclawd_vegas_superbowl` | Super Bowl winner odds only |
| `polyclawd_vegas_soccer` | All soccer futures |
| `polyclawd_vegas_epl` | English Premier League futures |
| `polyclawd_vegas_ucl` | UEFA Champions League futures |
| `polyclawd_vegas_edge` | Vegas vs Polymarket edge for sports |

---

## ESPN Odds (3 tools)

Live game odds from ESPN (DraftKings source).

| Tool | Arguments | Description |
|------|-----------|-------------|
| `polyclawd_espn_moneyline` | `sport`: nfl, nba, nhl, mlb | Moneyline with true probabilities (vig removed) |
| `polyclawd_espn_moneylines` | - | All sports moneylines |
| `polyclawd_espn_edge` | - | ESPN vs Polymarket edge |

---

## Markets (8 tools)

Market discovery and search.

| Tool | Arguments | Description |
|------|-----------|-------------|
| `polyclawd_markets_trending` | - | Trending Polymarket markets |
| `polyclawd_markets_opportunities` | - | Mispriced/high-volume opportunities |
| `polyclawd_markets_search` | `query`: string | Search markets by keyword |
| `polyclawd_markets_new` | - | Newly created markets |
| `polyclawd_kalshi_markets` | - | Kalshi markets |
| `polyclawd_manifold_markets` | - | Manifold markets |
| `polyclawd_predictit_markets` | - | PredictIt markets |
| `polyclawd_metaculus_questions` | - | Metaculus questions |

---

## PolyRouter (3 tools)

Unified API for 7 platforms: Polymarket, Kalshi, Manifold, Limitless, ProphetX, Novig, SX.bet.

| Tool | Arguments | Description |
|------|-----------|-------------|
| `polyclawd_polyrouter_markets` | - | All markets from 7 platforms |
| `polyclawd_polyrouter_search` | `query`: string | Search across platforms |
| `polyclawd_polyrouter_sports` | `league`: nfl, nba, mlb, nhl, soccer | Sports markets |

---

## Engine & Trading (6 tools)

Trading engine control.

| Tool | Description |
|------|-------------|
| `polyclawd_engine` | Engine status (running, trades today) |
| `polyclawd_engine_start` | Start automated trading |
| `polyclawd_engine_stop` | Stop automated trading |
| `polyclawd_engine_trigger` | Manually trigger a scan |
| `polyclawd_trades` | Recent trades |
| `polyclawd_positions` | Current positions |

---

## Paper Trading (5 tools)

Scaling phases and simulation.

| Tool | Arguments | Description |
|------|-----------|-------------|
| `polyclawd_phase` | - | Current phase, balance, limits |
| `polyclawd_balance` | - | Paper trading balance |
| `polyclawd_simulate` | `balance`, `confidence` | Simulate position sizing |
| `polyclawd_simmer_portfolio` | - | Simmer portfolio |
| `polyclawd_simmer_status` | - | Simmer account status |

---

## Confidence & Learning (4 tools)

Bayesian confidence system.

| Tool | Arguments | Description |
|------|-----------|-------------|
| `polyclawd_keywords` | - | Learned keyword stats |
| `polyclawd_learn` | `title`, `outcome` (win/loss) | Teach from market title |
| `polyclawd_confidence_sources` | - | Confidence by signal source |
| `polyclawd_confidence_calibration` | - | Calibration stats |

---

## Resolution & Rotation (3 tools)

Position management.

| Tool | Description |
|------|-------------|
| `polyclawd_resolution_approaching` | Markets approaching resolution |
| `polyclawd_resolution_imminent` | Markets resolving <24h |
| `polyclawd_rotation_candidates` | Weak positions to exit |

---

## System (2 tools)

Health and metrics.

| Tool | Description |
|------|-------------|
| `polyclawd_health` | API health status |
| `polyclawd_metrics` | System metrics |

---

## Configuration

MCP server location: `~/Desktop/polyclawd/mcp/server.py`

Add to Claude settings (`~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "polyclawd": {
      "command": "python3",
      "args": ["/path/to/polyclawd/mcp/server.py"]
    }
  }
}
```

---

## API Base URL

All tools call: `https://virtuosocrypto.com/polyclawd/api/...`

---

*Last updated: 2026-02-08*

---

## NEW: Polymarket CLOB (2 tools)

Orderbook depth and microstructure analysis.

| Tool | Arguments | Description |
|------|-----------|-------------|
| `polyclawd_polymarket_orderbook` | `slug`, `outcome` | Get orderbook (bids/asks/spread) |
| `polyclawd_polymarket_microstructure` | `slug` | Liquidity analysis, whale detection |

---

## NEW: Manifold Smart Money (2 tools)

| Tool | Description |
|------|-------------|
| `polyclawd_manifold_bets` | Recent bets (track betting flow) |
| `polyclawd_manifold_top_traders` | Top traders (smart money signals) |

---

## NEW: Metaculus (1 tool)

| Tool | Description |
|------|-------------|
| `polyclawd_metaculus_divergence` | Expert vs crowd disagreement signals |

---

## NEW: Cross-Market Correlation (2 tools)

| Tool | Arguments | Description |
|------|-----------|-------------|
| `polyclawd_correlation_violations` | `min_violation` | Find probability constraint violations (arb opportunities) |
| `polyclawd_correlation_entities` | - | List entities with multiple related markets |

**Use Case:** Detect when P(Team wins Championship) > P(Team wins Conference) - a mathematical impossibility that indicates arbitrage.

---

## NEW: ESPN (2 tools)

| Tool | Arguments | Description |
|------|-----------|-------------|
| `polyclawd_espn_injuries` | `sport` | Injury reports (predict line movements) |
| `polyclawd_espn_standings` | `sport` | Team standings |

---

## NEW: Vegas Futures (3 tools)

| Tool | Description |
|------|-------------|
| `polyclawd_vegas_nba` | NBA championship futures |
| `polyclawd_vegas_mlb` | MLB World Series futures |
| `polyclawd_vegas_nhl` | NHL Stanley Cup futures |

---

## NEW: PolyRouter (2 tools)

| Tool | Arguments | Description |
|------|-----------|-------------|
| `polyclawd_polyrouter_arbitrage` | - | Cross-platform arb finder |
| `polyclawd_polyrouter_props` | `league` | Player props from 7 platforms |

---

## NEW: Kalshi Entertainment (1 tool)

| Tool | Description |
|------|-------------|
| `polyclawd_kalshi_entertainment` | Super Bowl, Grammys, Oscars, NFL props |

---

*Total: 67 MCP tools*
*Updated: 2026-02-08*

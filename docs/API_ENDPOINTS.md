# Polyclawd — API Endpoints Reference

> **Source of truth:** live `GET /api/openapi.json` from the running service.  
> **Captured:** 2026-06-23 · **API version:** `2.1.0` · **283 operations / 277 paths.**  
> Regenerate: `ssh vps 'curl -s http://127.0.0.1:8420/api/openapi.json'` → re-run parser. Candidates: API-Endpoint-Backlog. MCP tools: MCP_TOOLS.

Base URL `https://virtuosocrypto.com/polyclawd` (nginx) · local `:8420`. Unit `polyclawd-api.service`. Auth: mutating endpoints expect `X-API-Key`. `*` = required query param.

## Groups

| Group | Count | Mount | Scope |
|-------|-------|-------|-------|
| System | 13 | `/` | |
| Trading | 18 | `/api` | |
| Markets | 94 | `/api` | |
| Signals | 96 | `/api` | |
| Engine | 26 | `/api` | |
| Edge Scanner | 5 | — | |
| Live | 5 | `/api` | |
| Whale | 8 | `/api` | |
| Insider | 3 | `/api/insider` | |
| Social | 3 | `/api` | |
| Analytics | 6 | `/api` (mixed) | |
| Untagged | 6 | `/` | |
| **TOTAL** | **283** | | |

## System  (13)

| Method | Path | Query params | Description |
|--------|------|--------------|-------------|
| `GET` | `/api/activity` | limit, type, since | Activity |
| `GET` | `/api/clv` | — | Clv Analysis |
| `GET` | `/api/crypto-signals` | — | Crypto Signals |
| `GET` | `/api/daily-pnl` | — | Daily Pnl |
| `GET` | `/api/logs` | lines, level, module, search | Logs |
| `GET` | `/api/meta-model` | — | Meta Model Stats |
| `GET` | `/api/opportunities` | — | Opportunities |
| `GET` | `/api/shadow-performance` | — | Shadow Performance |
| `GET` | `/api/source-health` | — | Source Health |
| `GET` | `/api/strategy-breakdown` | — | Strategy Breakdown |
| `GET` | `/health` | — | Health |
| `GET` | `/metrics` | — | Metrics |
| `GET` | `/ready` | — | Ready |

## Trading  (18)

| Method | Path | Query params | Description |
|--------|------|--------------|-------------|
| `GET` | `/api/balance` | — | Get Balance |
| `GET` | `/api/paper/polymarket/status` | — | Get Polymarket Paper Status |
| `GET` | `/api/paper/positions` | — | Get Paper Positions |
| `GET` | `/api/paper/status` | — | Get Paper Status |
| `POST` | `/api/paper/trade` | market_id*, market_title*, side*, amount*, price*, reasoning | Execute Paper Trade Manual |
| `GET` | `/api/portfolio/resolution-cluster` | — | Portfolio Resolution Cluster |
| `GET` | `/api/positions` | — | Get Positions |
| `GET` | `/api/positions/check` | — | Check Positions |
| `POST` | `/api/positions/{position_id}/resolve` | won* | Resolve Position |
| `POST` | `/api/reset` | — | Reset Paper Trading |
| `GET` | `/api/simmer/context/{market_id}` | — | Get Simmer Context |
| `GET` | `/api/simmer/portfolio` | — | Get Simmer Portfolio |
| `GET` | `/api/simmer/positions` | — | Get Simmer Positions |
| `GET` | `/api/simmer/status` | — | Get Simmer Status |
| `POST` | `/api/simmer/trade` | market_id*, side*, amount*, reasoning | Execute Simmer Trade |
| `GET` | `/api/simmer/trades` | limit | Get Simmer Trades |
| `POST` | `/api/trade` | — | Execute Trade |
| `GET` | `/api/trades` | limit | Get Trades |

## Markets  (94)

| Method | Path | Query params | Description |
|--------|------|--------------|-------------|
| `GET` | `/api/arb-scan` | limit | Arb Scan |
| `GET` | `/api/baseball/dashboard` | — | Get Baseball Dashboard |
| `GET` | `/api/baseball/edge` | min_edge | Get Baseball Edge |
| `GET` | `/api/baseball/kalshi/scan` | min_edge, last_n | Get Baseball Kalshi Scan |
| `GET` | `/api/baseball/props` | — | Get Baseball Props |
| `GET` | `/api/baseball/props/alerts` | limit | Get Baseball Prop Alerts |
| `GET` | `/api/baseball/props/scan-analytics` | — | Get Baseball Scan Analytics |
| `GET` | `/api/baseball/props/scout` | last_n, min_edge, min_games | Get Baseball Props Scout |
| `GET` | `/api/betfair/edge` | — | Get Betfair Edge |
| `GET` | `/api/espn/edge` | min_edge | Get Espn Edge |
| `GET` | `/api/espn/injuries/{sport}` | — | Get Espn Injuries |
| `GET` | `/api/espn/mlb` | — | Get Espn Mlb |
| `GET` | `/api/espn/moneyline/{sport}` | — | Get Espn Moneyline |
| `GET` | `/api/espn/moneylines` | — | Get All Espn Moneylines |
| `GET` | `/api/espn/nba` | — | Get Espn Nba |
| `GET` | `/api/espn/ncaab` | — | Get Espn Ncaab |
| `GET` | `/api/espn/ncaaf` | — | Get Espn Ncaaf |
| `GET` | `/api/espn/nfl` | — | Get Espn Nfl |
| `GET` | `/api/espn/nhl` | — | Get Espn Nhl |
| `GET` | `/api/espn/odds` | — | Get Espn Odds |
| `GET` | `/api/espn/standings/{sport}` | — | Get Espn Standings |
| `GET` | `/api/hf/backtest` | balance, simulations, trades | Hf Backtest |
| `GET` | `/api/hf/backtest/{strategy}` | balance, simulations, trades, kelly | Hf Backtest Strategy |
| `GET` | `/api/hf/collect` | — | Hf Run Collection |
| `GET` | `/api/hf/collection-stats` | — | Hf Collection Stats |
| `POST` | `/api/hf/daily-summary` | — | Hf Daily Summary |
| `GET` | `/api/hf/latency` | — | Hf Latency State |
| `GET` | `/api/hf/latency/events` | — | Hf Latency Events |
| `GET` | `/api/hf/latency/signals` | — | Hf Latency Signals |
| `GET` | `/api/hf/markets` | — | Hf Discover Markets |
| `GET` | `/api/hf/negvig` | threshold | Hf Neg Vig Scan |
| `GET` | `/api/hf/opportunities` | — | Hf Opportunities |
| `GET` | `/api/hf/paper-performance` | — | Hf Paper Performance |
| `POST` | `/api/hf/resolve` | — | Hf Resolve Positions |
| `GET` | `/api/hf/risk` | max_drawdown, window_min | Hf Risk Gate |
| `GET` | `/api/hf/scan` | threshold | Hf Full Scan |
| `GET` | `/api/hf/signal/{asset}` | — | Hf Directional Signal |
| `GET` | `/api/hf/signals` | — | Hf All Signals |
| `POST` | `/api/hf/trade` | — | Hf Process Signals |
| `GET` | `/api/kalshi/all` | — | Get All Kalshi Markets |
| `GET` | `/api/kalshi/entertainment` | — | Get Kalshi Entertainment |
| `GET` | `/api/kalshi/markets` | — | Get Kalshi Markets |
| `GET` | `/api/manifold/bets` | limit | Get Manifold Bets |
| `GET` | `/api/manifold/edge` | min_edge | Get Manifold Edge |
| `GET` | `/api/manifold/markets` | — | Get Manifold Markets |
| `GET` | `/api/manifold/top-traders` | — | Get Manifold Top Traders |
| `GET` | `/api/markets/new` | — | Get New Markets |
| `GET` | `/api/markets/opportunities` | min_liquidity | Get Market Opportunities |
| `GET` | `/api/markets/search` | q*, limit | Search Markets |
| `GET` | `/api/markets/trending` | limit | Get Trending Markets |
| `GET` | `/api/markets/{market_id}` | — | Get Market Details |
| `GET` | `/api/metaculus/divergence` | — | Get Metaculus Divergence |
| `GET` | `/api/metaculus/edge` | min_edge | Get Metaculus Edge |
| `GET` | `/api/metaculus/questions` | limit, min_forecasters | Get Metaculus Questions |
| `GET` | `/api/odds-api/credits` | — | Get Odds Api Credits |
| `GET` | `/api/polymarket/events` | limit | Get Polymarket Events |
| `GET` | `/api/polymarket/microstructure/{slug}` | — | Get Polymarket Microstructure |
| `GET` | `/api/polymarket/orderbook/{slug}` | outcome | Get Polymarket Orderbook |
| `GET` | `/api/polymarket/whale-wall-scan` | top_n | Whale Wall Scan |
| `GET` | `/api/polyrouter/arbitrage` | — | Get Polyrouter Arbitrage |
| `GET` | `/api/polyrouter/edge` | min_edge | Get Polyrouter Edge |
| `GET` | `/api/polyrouter/futures/{league}` | — | Get Polyrouter Futures |
| `GET` | `/api/polyrouter/markets` | platform, query, limit | Get Polyrouter Markets |
| `GET` | `/api/polyrouter/platforms` | — | Get Polyrouter Platforms |
| `GET` | `/api/polyrouter/props/{league}` | — | Get Polyrouter Props |
| `GET` | `/api/polyrouter/search` | query*, limit | Search Polyrouter |
| `GET` | `/api/polyrouter/sports/{league}` | limit | Get Polyrouter Sports |
| `GET` | `/api/predictit/edge` | min_edge | Get Predictit Edge |
| `GET` | `/api/predictit/markets` | — | Get Predictit Markets |
| `GET` | `/api/rewards` | — | Get Rewards |
| `GET` | `/api/soccer/calibration` | — | Get Soccer Calibration Route |
| `GET` | `/api/soccer/dashboard` | — | Get Soccer Dashboard |
| `GET` | `/api/soccer/futures-edge` | — | Get Soccer Futures Edge |
| `GET` | `/api/soccer/kalshi-wc` | — | Get Soccer Kalshi Wc |
| `GET` | `/api/soccer/match-edge` | — | Get Soccer Match Edge |
| `GET` | `/api/soccer/wc-board` | — | Get Soccer Wc Board |
| `GET` | `/api/ufc/dashboard` | — | Get Ufc Dashboard |
| `GET` | `/api/ufc/edge` | — | Get Ufc Edge |
| `GET` | `/api/vegas/bundesliga` | min_edge | Get Bundesliga Edges |
| `GET` | `/api/vegas/edge` | min_edge, sports | Find Vegas Edge |
| `GET` | `/api/vegas/epl` | min_edge | Get Epl Edges |
| `GET` | `/api/vegas/laliga` | min_edge | Get Laliga Edges |
| `GET` | `/api/vegas/mlb` | — | Get Vegas Mlb |
| `GET` | `/api/vegas/nba` | — | Get Vegas Nba |
| `GET` | `/api/vegas/nfl` | — | Get Nfl Futures |
| `GET` | `/api/vegas/nfl/superbowl` | — | Get Superbowl Odds |
| `GET` | `/api/vegas/nhl` | — | Get Vegas Nhl |
| `GET` | `/api/vegas/odds` | sport, priority | Get Vegas Odds |
| `GET` | `/api/vegas/quota` | — | Get Odds Api Quota |
| `POST` | `/api/vegas/quota/reset` | — | Reset Odds Api Quota |
| `GET` | `/api/vegas/soccer` | min_edge | Get Soccer Edges |
| `GET` | `/api/vegas/sports` | — | Get Active Sports |
| `GET` | `/api/vegas/ucl` | min_edge | Get Ucl Edges |
| `GET` | `/api/vegas/worldcup` | min_edge | Get Worldcup Edges |

## Signals  (96)

| Method | Path | Query params | Description |
|--------|------|--------------|-------------|
| `GET` | `/api/alerts/stats` | days | Alert Stats |
| `GET` | `/api/archetype/calibration` | — | Get Calibration Audit |
| `GET` | `/api/archetype/classify` | title* | Classify Market Archetype |
| `GET` | `/api/archetype/evaluate` | title*, side*, price* | Evaluate With Empirical |
| `GET` | `/api/archetype/kill-check` | title*, price_cents* | Check Kill Rules |
| `GET` | `/api/archetype/kill-stats` | — | Get Kill Stats |
| `GET` | `/api/archetype/wr-buckets` | — | Get Wr Buckets |
| `GET` | `/api/basket-arb` | — | Basket Arb Signals |
| `GET` | `/api/basket-arb/compression` | — | Basket Arb Compression |
| `GET` | `/api/calibration/book-weights` | — | Get Book Weights |
| `GET` | `/api/calibration/cross-sport` | — | Get Cross Sport Calibration |
| `GET` | `/api/calibration/price-movement` | sport, hours | Get Price Movement |
| `GET` | `/api/confidence/calibration` | — | Get Calibration Data |
| `GET` | `/api/confidence/history` | limit | Get Confidence History |
| `GET` | `/api/confidence/market/{market_id}` | — | Get Market Confidence |
| `POST` | `/api/confidence/record` | source*, won* | Record Trade Outcome |
| `GET` | `/api/confidence/sources` | — | Get Source Statistics |
| `GET` | `/api/conflicts/active` | — | Get Active Conflicts |
| `GET` | `/api/conflicts/stats` | — | Get Conflict Stats |
| `GET` | `/api/copy-trade` | — | Copy Trade Signals |
| `GET` | `/api/copy-trade/positions` | — | Copy Trade Positions |
| `GET` | `/api/copy-trade/whales` | — | Copy Trade Whales |
| `GET` | `/api/correlation/entities` | — | Get Market Entities |
| `GET` | `/api/correlation/violations` | min_violation | Get Correlation Violations |
| `GET` | `/api/inverse-whale` | — | Inverse Whale Signals |
| `GET` | `/api/options/dashboard` | — | Options Dashboard |
| `GET` | `/api/options/iv-rv` | — | Get Options Iv Rv |
| `GET` | `/api/options/status` | — | Options Status |
| `GET` | `/api/portfolio/archetype-breakdown` | — | Get Portfolio Archetype Breakdown |
| `GET` | `/api/portfolio/archetype-pnl-series` | — | Get Archetype Pnl Series |
| `POST` | `/api/portfolio/close/{position_id}` | outcome* | Manually Close Position |
| `GET` | `/api/portfolio/equity-curve` | — | Get Portfolio Equity Curve |
| `GET` | `/api/portfolio/equity-series` | hours | Get Portfolio Equity Series |
| `POST` | `/api/portfolio/equity-snapshot` | — | Post Portfolio Equity Snapshot |
| `GET` | `/api/portfolio/history` | limit | Get Portfolio History |
| `GET` | `/api/portfolio/positions` | status | Get Portfolio Positions |
| `GET` | `/api/portfolio/positions-live` | — | Get Portfolio Positions Live |
| `POST` | `/api/portfolio/process-signals` | — | Process Portfolio Signals |
| `POST` | `/api/portfolio/resolve` | — | Resolve Portfolio Positions |
| `GET` | `/api/portfolio/resolve-log` | limit | Get Resolve Log |
| `GET` | `/api/portfolio/risk-guards` | — | Get Risk Guards |
| `GET` | `/api/portfolio/status` | — | Get Portfolio Status |
| `GET` | `/api/predictors` | — | Get Predictor Stats |
| `POST` | `/api/predictors/update` | — | Refresh Predictor Stats |
| `GET` | `/api/resolution/approaching` | hours | Get Approaching Resolution |
| `GET` | `/api/resolution/imminent` | — | Get Imminent Resolution |
| `GET` | `/api/rotation/candidates` | — | Get Rotation Candidates |
| `GET` | `/api/rotations` | hours | Get Recent Rotations |
| `GET` | `/api/signals` | — | Get All Signals |
| `GET` | `/api/signals/ai-models` | — | Get Ai Model Tracker |
| `GET` | `/api/signals/ai-models/trends` | days | Get Ai Model Trends |
| `GET` | `/api/signals/alpha-history/{symbol}` | hours | Get Alpha History |
| `GET` | `/api/signals/alpha-snapshot` | — | Run Alpha Snapshot |
| `POST` | `/api/signals/auto-trade` | max_trades, max_per_trade, min_confidence | Auto Trade On Signals |
| `GET` | `/api/signals/btc-tracker` | hours | Get Btc Tracker |
| `GET` | `/api/signals/calibration` | — | Get Calibration Report |
| `GET` | `/api/signals/calibration/{source}` | — | Get Source Calibration |
| `GET` | `/api/signals/category-momentum` | — | Get Category Momentum |
| `GET` | `/api/signals/clarity` | — | Get Clarity Widget Data |
| `GET` | `/api/signals/consensus-disagreement` | sports, min_signal | Get Consensus Disagreement |
| `GET` | `/api/signals/consensus-disagreement/{sport_key}` | — | Get Consensus Disagreement Sport |
| `GET` | `/api/signals/copy-trade` | — | Get Copy Trade Data |
| `GET` | `/api/signals/cross-platform-arb` | — | Get Cross Platform Arb |
| `GET` | `/api/signals/elections` | — | Get Election Markets |
| `GET` | `/api/signals/elections/core` | — | Get Election Markets Core |
| `GET` | `/api/signals/ic-report` | window_days | Get Ic Report |
| `GET` | `/api/signals/ic/{source}` | window_days | Get Ic For Source |
| `GET` | `/api/signals/implied-correlation` | min_markets | Get Implied Correlation |
| `GET` | `/api/signals/implied-correlation/clusters/list` | min_markets | List Correlation Clusters |
| `GET` | `/api/signals/implied-correlation/snapshots` | cluster, limit | Get Correlation Snapshots |
| `GET` | `/api/signals/implied-correlation/{cluster}` | — | Get Implied Correlation Cluster |
| `GET` | `/api/signals/mispriced-category` | — | Get Mispriced Category Strategy Signals |
| `GET` | `/api/signals/news` | — | Get News Signals |
| `GET` | `/api/signals/poly-delta-stats` | — | Get Poly Delta Stats |
| `GET` | `/api/signals/prop-composite` | — | Prop Composite Scan |
| `GET` | `/api/signals/prop-composite/{event_id}` | — | Prop Composite Event |
| `GET` | `/api/signals/resolution-certainty` | — | Get Resolution Certainty |
| `GET` | `/api/signals/scorecard/{strategy}` | — | Get Strategy Scorecard |
| `GET` | `/api/signals/shadow-performance` | — | Get Shadow Performance |
| `POST` | `/api/signals/shadow-resolve` | — | Trigger Shadow Resolution |
| `GET` | `/api/signals/source-weights` | — | Get Source Weights |
| `GET` | `/api/signals/strike-scanner` | — | Strike Scanner |
| `GET` | `/api/signals/theta-decay` | — | Get Theta Decay All |
| `GET` | `/api/signals/theta-decay/{archetype}` | — | Get Theta Decay Single |
| `GET` | `/api/signals/tweets` | — | Scan Tweet Counts |
| `GET` | `/api/signals/vpin-accuracy` | — | Get Vpin Accuracy |
| `GET` | `/api/signals/vpin-scan` | top_n | Get Vpin Scan |
| `GET` | `/api/signals/vpin/{slug}` | — | Get Vpin For Slug |
| `GET` | `/api/signals/weather` | — | Scan Weather |
| `GET` | `/api/signals/weather-resolution-edge` | city, platform | Weather Resolution Edge |
| `GET` | `/api/signals/weather/ensemble-status` | — | Weather Ensemble Status |
| `GET` | `/api/smart-money` | — | Smart Money Flow |
| `GET` | `/api/volume/spikes` | threshold, method | Get Volume Spikes |
| `GET` | `/api/weather/dashboard` | — | Weather Dashboard |
| `GET` | `/api/weather/forecast-log` | — | Weather Forecast Log |
| `GET` | `/api/weather/kalshi-fade/dashboard` | — | Kalshi Fade Dashboard |

## Engine  (26)

| Method | Path | Query params | Description |
|--------|------|--------------|-------------|
| `GET` | `/api/alerts` | — | List Alerts |
| `POST` | `/api/alerts` | market_id*, target_price*, direction, note | Create Alert |
| `GET` | `/api/alerts/check` | — | Check Alerts Endpoint |
| `DELETE` | `/api/alerts/{alert_id}` | — | Delete Alert |
| `GET` | `/api/engine/config` | — | Get Engine Config |
| `POST` | `/api/engine/config` | min_confidence, max_per_trade, max_daily_trades, cooldown_minutes, max_position_pct | Update Engine Config |
| `GET` | `/api/engine/liquidity-sizing` | — | Get Liquidity Sizing |
| `POST` | `/api/engine/liquidity-sizing` | enabled, max_slip_bps, min_book_usd | Update Liquidity Sizing |
| `POST` | `/api/engine/reset-daily` | — | Reset Daily Counter |
| `POST` | `/api/engine/start` | — | Start Engine Endpoint |
| `GET` | `/api/engine/status` | — | Get Engine Status Endpoint |
| `POST` | `/api/engine/stop` | — | Stop Engine Endpoint |
| `GET` | `/api/engine/stop-curve` | — | Get Stop Curve |
| `POST` | `/api/engine/stop-curve` | enabled, weather_hours, weather_max_loss | Update Stop Curve |
| `POST` | `/api/engine/trigger` | — | Trigger Engine Endpoint |
| `GET` | `/api/engine/weather-strategies` | — | Get Weather Strategies |
| `POST` | `/api/engine/weather-strategies` | weather_trading_enabled, kalshi_fade_enabled, kalshi_fade_ranked_fill | Update Weather Strategies |
| `GET` | `/api/kelly/current` | — | Get Kelly Status |
| `GET` | `/api/kelly/simulate` | confidence, source | Simulate Kelly |
| `GET` | `/api/llm/status` | — | Get Llm Status |
| `POST` | `/api/llm/test` | market*, side, confidence | Test Llm Validation |
| `GET` | `/api/phase/config` | — | Get All Phase Configs |
| `GET` | `/api/phase/current` | — | Get Current Phase |
| `GET` | `/api/phase/history` | — | Get Phase History |
| `GET` | `/api/phase/limits` | — | Check Phase Limits |
| `POST` | `/api/phase/simulate` | balance*, confidence, win_rate, win_streak, source_agreement, market_price | Simulate Position Size |

## Edge Scanner  (5)

| Method | Path | Query params | Description |
|--------|------|--------------|-------------|
| `POST` | `/api/edge/calculate` | — | Calculate Edge |
| `GET` | `/api/edge/calculate/example` | — | Get Edge Example |
| `GET` | `/api/edge/scan` | refresh | Get Cross Platform Edges |
| `GET` | `/api/edge/sharp-books` | — | Get Sharp Books |
| `GET` | `/api/edge/topics` | — | Get Tracked Topics |

## Live  (5)

| Method | Path | Query params | Description |
|--------|------|--------------|-------------|
| `GET` | `/api/live/edge-capture` | limit | Get Live Edge Capture |
| `GET` | `/api/live/fills` | limit | Get Live Fills |
| `GET` | `/api/live/governor` | — | Get Live Governor |
| `GET` | `/api/live/portfolio` | — | Get Live Portfolio Endpoint |
| `GET` | `/api/live/positions` | — | Get Live Positions |

## Whale  (8)

| Method | Path | Query params | Description |
|--------|------|--------------|-------------|
| `GET` | `/api/whale/alerts` | severity, platform, hours, limit | Whale Alerts |
| `GET` | `/api/whale/book` | platform*, market* | Whale Book |
| `GET` | `/api/whale/follows` | limit | Whale Follows |
| `GET` | `/api/whale/precision` | — | Whale Precision |
| `GET` | `/api/whale/stats` | — | Whale Stats |
| `GET` | `/api/whale/top` | limit, min_score, severity, platform | Whale Top |
| `GET` | `/api/whale/wallet/{wallet_addr}` | — | Whale Wallet Detail |
| `GET` | `/api/whale/wallets` | limit, sort | Whale Wallets |

## Insider  (3)

| Method | Path | Query params | Description |
|--------|------|--------------|-------------|
| `GET` | `/api/insider/leaderboard` | min_bets | Get Insider Leaderboard Endpoint |
| `GET` | `/api/insider/recent` | limit, min_score | Get Recent Insiders Endpoint |
| `POST` | `/api/insider/scan` | — | Trigger Insider Scan |

## Social  (3)

| Method | Path | Query params | Description |
|--------|------|--------------|-------------|
| `GET` | `/api/social/counts` | — | Get Social Counts |
| `GET` | `/api/social/history/{person}` | days | Get Person History |
| `GET` | `/api/social/snapshots` | limit | Get All Snapshots |

## Analytics  (6)

| Method | Path | Query params | Description |
|--------|------|--------------|-------------|
| `GET` | `/api/signals/ai-models/history` | company, days | Ai Models History |
| `GET` | `/api/signals/elections/control-history` | days | Election Control History |
| `GET` | `/api/signals/elections/race-prices` | state, race, days | Election Race Prices |
| `GET` | `/api/signals/strategy-ic` | window_days, min_n | Strategy Ic |
| `GET` | `/api/weather/ensemble-accuracy` | city, days | Weather Ensemble Accuracy |
| `GET` | `/api/whale/outcomes` | severity, days | Whale Outcomes |

## Untagged  (6)

| Method | Path | Query params | Description |
|--------|------|--------------|-------------|
| `GET` | `/` | — | Serve Index |
| `GET` | `/api/visitor-log` | limit | Get Visitor Log |
| `POST` | `/api/visitor-log` | — | Visitor Log |
| `GET` | `/manifest.json` | — | Serve Manifest |
| `GET` | `/sw.js` | — | Serve Sw |
| `GET` | `/{page}.html` | — | Serve Page |

## See Also
- API-Endpoint-Backlog — candidates to add
- MCP_TOOLS — curated MCP tool subset
- Canonical-Source-and-Deploy


# Polyclawd local↔VPS drift report

> Generated 2026-06-15. Read-only audit of `*.py` under odds/ signals/ services/ api/ src/.
> Local = `~/Desktop/polyclawd` (canonical). VPS = `/var/www/virtuosocrypto.com/polyclawd` (running).
> Deploy model is explicit-file copy, so drift is expected; this triages it.

**Summary:** 44 differ (12 VPS-ahead / 32 local-ahead) · 43 VPS-only · 7 local-only

## A. Differing files (content mismatch, both sides)

| Direction | Δ | local mtime | vps mtime | path |
|---|---|---|---|---|
| VPS-ahead | 103d | 2026-02-24 16:23 | 2026-06-07 17:39 | `api/routes/system.py` |
| VPS-ahead | 55d | 2026-02-11 15:41 | 2026-04-07 05:04 | `src/indexers/polymarket/markets.py` |
| VPS-ahead | 54d | 2026-02-11 15:41 | 2026-04-07 01:36 | `src/indexers/polymarket/client.py` |
| VPS-ahead | 27d | 2026-02-08 04:42 | 2026-03-06 19:14 | `api/middleware.py` |
| VPS-ahead | 21d | 2026-02-13 19:49 | 2026-03-06 17:17 | `signals/alpha_score_tracker.py` |
| VPS-ahead | 17d | 2026-02-24 06:10 | 2026-03-13 13:17 | `signals/volume_spike_detector.py` |
| VPS-ahead | 17d | 2026-02-24 06:26 | 2026-03-13 13:17 | `signals/price_momentum_filter.py` |
| VPS-ahead | 15d | 2026-02-19 22:56 | 2026-03-06 17:17 | `signals/basket_arb_scanner.py` |
| VPS-ahead | 10d | 2026-03-03 15:02 | 2026-03-13 06:19 | `signals/tweet_count_scanner.py` |
| VPS-ahead | 8d | 2026-02-27 19:24 | 2026-03-07 08:07 | `services/hf_triggers.py` |
| VPS-ahead | 7d | 2026-02-28 04:24 | 2026-03-07 08:07 | `services/hf_enrichment.py` |
| VPS-ahead | 7d | 2026-02-27 19:22 | 2026-03-06 17:17 | `services/hf_velocity.py` |
| LOCAL-ahead | 113d | 2026-06-01 15:31 | 2026-02-08 04:42 | `api/services/storage.py` |
| LOCAL-ahead | 110d | 2026-06-01 15:31 | 2026-02-11 15:41 | `src/common/chart_theme.py` |
| LOCAL-ahead | 110d | 2026-06-01 15:31 | 2026-02-11 20:29 | `src/strategies/mispriced_category_whale.py` |
| LOCAL-ahead | 102d | 2026-06-13 13:18 | 2026-03-03 17:08 | `services/hf_paper_trader.py` |
| LOCAL-ahead | 99d | 2026-06-13 13:18 | 2026-03-06 17:17 | `signals/browser_bridge.py` |
| LOCAL-ahead | 90d | 2026-06-13 13:18 | 2026-03-15 21:15 | `api/services/cross_platform_edge.py` |
| LOCAL-ahead | 87d | 2026-06-01 15:31 | 2026-03-06 17:17 | `signals/ai_model_tracker.py` |
| LOCAL-ahead | 87d | 2026-06-01 15:31 | 2026-03-06 17:17 | `signals/copy_trade_watcher.py` |
| LOCAL-ahead | 87d | 2026-06-01 15:31 | 2026-03-06 17:17 | `signals/cv_kelly.py` |
| LOCAL-ahead | 87d | 2026-06-01 15:31 | 2026-03-06 17:17 | `signals/keyword_learner.py` |
| LOCAL-ahead | 87d | 2026-06-01 15:31 | 2026-03-06 17:17 | `signals/news_signal.py` |
| LOCAL-ahead | 87d | 2026-06-01 15:31 | 2026-03-06 17:17 | `signals/resolution_scanner.py` |
| LOCAL-ahead | 87d | 2026-06-01 15:31 | 2026-03-06 17:17 | `services/hf_risk_gate.py` |
| LOCAL-ahead | 87d | 2026-06-01 15:31 | 2026-03-06 17:17 | `services/virtuoso_bridge.py` |
| LOCAL-ahead | 86d | 2026-06-01 15:31 | 2026-03-07 08:07 | `services/hf_backtest.py` |
| LOCAL-ahead | 86d | 2026-06-01 15:31 | 2026-03-07 08:07 | `services/hf_collector.py` |
| LOCAL-ahead | 80d | 2026-06-01 15:31 | 2026-03-13 13:17 | `signals/strike_probability.py` |
| LOCAL-ahead | 80d | 2026-06-01 15:31 | 2026-03-13 13:17 | `signals/time_decay_optimizer.py` |
| LOCAL-ahead | 80d | 2026-06-01 15:31 | 2026-03-13 13:17 | `signals/whale_wall_scanner.py` |
| LOCAL-ahead | 78d | 2026-06-01 15:31 | 2026-03-15 17:57 | `signals/calibrator.py` |
| LOCAL-ahead | 77d | 2026-06-01 15:31 | 2026-03-17 02:47 | `signals/empirical_confidence.py` |
| LOCAL-ahead | 74d | 2026-06-01 15:31 | 2026-03-19 16:29 | `services/hf_engine.py` |
| LOCAL-ahead | 58d | 2026-06-01 15:31 | 2026-04-04 21:03 | `signals/ic_tracker.py` |
| LOCAL-ahead | 4d | 2026-06-10 17:13 | 2026-06-07 01:55 | `odds/player_profile.py` |
| LOCAL-ahead | 3d | 2026-06-01 15:31 | 2026-05-29 18:59 | `signals/weather_ensemble.py` |
| LOCAL-ahead | 3d | 2026-06-13 13:18 | 2026-06-11 01:16 | `signals/discord_alerts.py` |
| LOCAL-ahead | 42h | 2026-06-10 16:32 | 2026-06-08 22:33 | `odds/nba_props_scanner.py` |
| LOCAL-ahead | 42h | 2026-06-10 16:31 | 2026-06-08 22:54 | `odds/best_nba.py` |
| LOCAL-ahead | 41h | 2026-06-10 16:32 | 2026-06-08 23:07 | `odds/nba_purified_scan.py` |
| LOCAL-ahead | 26h | 2026-06-02 15:55 | 2026-06-01 13:37 | `api/edge_cache.py` |
| LOCAL-ahead | 23h | 2026-06-13 13:18 | 2026-06-12 14:37 | `api/routes/signals.py` |
| LOCAL-ahead | 23h | 2026-06-13 13:18 | 2026-06-12 14:40 | `signals/paper_portfolio.py` |

**VPS-ahead = local canonical is STALE behind prod; deploying local would regress. Back-port these VPS→local.**

> Note: the cluster all stamped `2026-06-01 15:31` is a git checkout touching mtimes, not 20 real edits — 'local-ahead' there means 'this branch's version', not necessarily newer.

## B. VPS-only files — 43 (do NOT delete: live features)

- `api/activity_feed.py` (2026-03-06 17:09)
- `api/logging_config.py` (2026-03-06 23:20)
- `api/ranked_feed.py` (2026-06-13 21:48)
- `api/routes/consensus_disagreement.py` (2026-06-12 14:32)
- `api/routes/insider.py` (2026-03-17 03:48)
- `api/routes/prop_composite.py` (2026-06-12 14:32)
- `api/routes/social.py` (2026-03-20 21:56)
- `api/routes/vpin.py` (2026-06-12 14:32)
- `api/routes/weather_dashboard.py` (2026-03-11 17:34)
- `api/services/llm_market_matcher.py` (2026-03-09 22:21)
- `services/__init__.py` (2026-02-27 16:37)
- `services/book_logger.py` (2026-04-28 03:21)
- `services/fix_hf_trader.py` (2026-03-24 16:22)
- `services/scheduler_heartbeat.py` (2026-04-08 01:11)
- `services/weather_canary.py` (2026-05-07 04:13)
- `signals/ballotpedia_client.py` (2026-04-08 22:25)
- `signals/congress_bill_tracker.py` (2026-04-10 13:47)
- `signals/cross_platform_elections.py` (2026-03-13 13:17)
- `signals/crypto_price_signal.py` (2026-03-15 01:26)
- `signals/crypto_vote_scorer.py` (2026-04-10 14:10)
- `signals/edge_to_signals.py` (2026-03-13 06:20)
- `signals/election_db.py` (2026-04-08 04:18)
- `signals/election_pdf.py` (2026-04-07 20:37)
- `signals/election_polls.py` (2026-03-13 13:17)
- `signals/election_signal.py` (2026-06-07 23:18)
- `signals/election_tracker.py` (2026-06-07 23:19)
- `signals/fec_client.py` (2026-04-08 20:18)
- `signals/fec_crypto_pacs.py` (2026-04-10 14:11)
- `signals/fec_efiling.py` (2026-04-08 15:50)
- `signals/fec_spending.py` (2026-04-07 20:37)
- `signals/fred_client.py` (2026-04-08 22:24)
- `signals/google_trends.py` (2026-04-08 16:35)
- `signals/insider_detector.py` (2026-03-20 07:47)
- `signals/lda_crypto_lobbying.py` (2026-04-10 01:30)
- `signals/manifold_client.py` (2026-04-08 04:17)
- `signals/polymarket_price_history.py` (2026-04-10 14:31)
- `signals/predictit_client.py` (2026-04-08 19:25)
- `signals/rcp_client.py` (2026-04-08 17:52)
- `signals/smart_money.py` (2026-04-08 22:23)
- `signals/ufc_event_discovery.py` (2026-06-15 03:18)
- `signals/vegas_scraper.py` (2026-03-06 17:17)
- `signals/virtuoso_bridge.py` (2026-03-06 17:17)
- `signals/wiki_pageviews.py` (2026-04-08 15:51)

## C. Local-only files — 7

- `odds/mlb_enrichment.py` (2026-06-06 21:59)
- `odds/pitcher_profile.py` (2026-06-10 17:13)
- `odds/scorer_edge.py` (2026-06-15 15:39)
- `odds/scorer_sizing.py` (2026-06-15 15:50)
- `signals/scorer_paper_portfolio.py` (2026-06-15 15:53)
- `signals/scorer_resolution.py` (2026-06-15 15:37)
- `signals/scorer_resolution_fetch.py` (2026-06-15 15:52)


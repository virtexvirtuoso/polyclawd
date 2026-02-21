# Backtest Results: 159K Market Empirical Validation

> **Date:** 2026-02-21
> **Script:** `scripts/prediction_market_backtest.py`
> **Dataset:** 159,220 resolved markets (110,427 Kalshi + 48,789 Polymarket)
> **Trade simulation:** 16,575 NO-side trades with real entry prices from 72M Kalshi trade records
> **Classifier version:** v2 (17 archetypes including parlay, financial_price, game_total)

---

## 1. What We Did

Built a backtest harness that replays the **actual production signal pipeline** (`classify_archetype()`, `_check_kill_rules()`, `calculate_signal_confidence()`) on 50 GB of historical prediction market data. Two analysis modes:

- **Population-level:** Classify all 159K resolved markets by archetype and count NO wins (validates base rates)
- **Trade-level:** Simulate Kelly-sized NO trades on 10K+ Kalshi markets using real entry prices from trade records (validates P&L)

---

## 2. Critical Discovery: `last_price` Bias

The initial backtest showed a 26% NO win rate — far too low. Root cause: Kalshi's parquet `last_price` field is the **resolution price**, not an entry price.

| Resolution | Median `last_price` |
|---|---|
| YES wins | 99c |
| NO wins | 1c |

Filtering `last_price >= 55` selected almost exclusively YES-resolved markets. Fix: JOIN with 72M trade records via DuckDB to get the first trade at an eligible price (55-92c YES) per market.

**Lesson:** Always verify what "price" means in historical data. Resolution price != entry price.

---

## 3. Archetype NO Win Rates (Population-Level)

Combined weighted averages from both platforms. These are the fraction of markets where NO wins, regardless of entry price. Updated with classifier v2 (3 new archetypes added 2026-02-21).

| Archetype | n | NO WR | Prior | Delta | Status |
|---|---|---|---|---|---|
| parlay | 4,251 | **93.7%** | — | — | **NEW** — multi-leg bets |
| social_count | 1,773 | **94.1%** | 60.0% | +34.1 | |
| weather | 68 | **85.3%** | 60.0% | +25.3 | Small n |
| ai_model | 54 | **74.1%** | 77.4% | -3.3 | |
| entertainment | 114 | **71.1%** | 60.0% | +11.1 | |
| directional | 390 | **69.7%** | 60.0% | +9.7 | K5 killed |
| deadline_binary | 9,062 | **69.4%** | 60.0% | +9.4 | |
| geopolitical | 315 | **68.6%** | 69.6% | -1.0 | |
| other | 20,828 | **66.8%** | 59.3% | +7.5 | Shrunk 60K→21K |
| financial_price | 18,356 | **64.6%** | — | — | **NEW** — S&P, forex, VIX |
| election | 794 | **63.9%** | 69.6% | -5.7 | |
| price_above | 3,763 | **59.3%** | 52.8% | +6.5 | |
| sports_winner | 27,642 | **56.7%** | 78.1% | -21.4 | |
| sports_single_game | 6,309 | **56.0%** | 78.1% | -22.1 | |
| price_range | 31,982 | **56.6%** | 88.6% | -32.0 | |
| game_total | 10,999 | **52.1%** | — | — | **NEW** — K7 killed |
| intraday_updown | 15,570 | **50.4%** | 51.7% | -1.3 | K1 killed |
| daily_updown | 887 | **46.3%** | 51.7% | -5.4 | Poly only |

### Key Takeaways

- **Parlay is a goldmine.** 93.7% NO WR (n=4,251). Multi-leg combined bets almost always fail — any single leg losing = NO wins.
- **Financial price has solid edge.** 64.6% NO WR (n=18,356). S&P 500, Nasdaq, forex, gold, VIX threshold markets.
- **Game total is a coin flip.** 52.1% population WR, 40.8% traded WR (n=10,999). K7 kills it.
- **Classifier expansion cut `other` from 41% → 19%.** 33K+ markets reclassified into specific archetypes.
- **Becker's sports priors were wrong.** `sports_winner` (78% → 57%) and `sports_single_game` (78% → 56%) grossly overestimated.
- **`social_count` is near-guaranteed NO.** 94% NO WR (n=1,773).
- **`intraday_updown` confirmed as coin flip.** 50.4% NO WR at n=15,570. No edge after fees.

---

## 4. Price Zone Win Rates (Trade-Level, NO Side)

From 10,534 simulated NO trades with real Kalshi entry prices.

| Zone | YES Price | NO WR | Profit Factor | n |
|---|---|---|---|---|
| mid | 55-65c | **45.5%** | 1.20 | 4,401 |
| sweet | 65-75c | **37.7%** | 1.47 | 2,812 |
| premium | 75-85c | **30.1%** | 1.72 | 1,958 |
| expensive | 85-92c | **23.3%** | 2.43 | 1,363 |

### What This Means

Higher YES price = cheaper NO entry = **lower win rate but higher payoff per win**. The gradient is consistent across all archetypes tested.

The old price zone modifiers **boosted** confidence at sweet/premium (1.15, 1.10), making the system think these zones were both high-probability AND high-payoff. This double-counted the edge. Kelly sizing already captures payoff asymmetry through cost basis — the confidence system only needs to reflect probability.

### Updated Modifiers

| Zone | Old | New | Basis |
|---|---|---|---|
| garbage (<30c) | 0.25 | 0.55 | K3 kills these anyway |
| cheap (30-45c) | 0.55 | 0.75 | Extrapolated |
| mid_low (45-55c) | 0.85 | 0.90 | Extrapolated |
| mid (55-65c) | 1.00 | 1.00 | Reference (45.5% WR) |
| sweet (65-75c) | 1.15 | **0.83** | 37.7% / 45.5% = 0.83 |
| premium (75-85c) | 1.10 | **0.66** | 30.1% / 45.5% = 0.66 |
| expensive (85-92c) | 0.75 | **0.51** | 23.3% / 45.5% = 0.51 |

---

## 5. Kill Rule Validation

| Rule | What It Kills | Killed WR | n | Verdict |
|---|---|---|---|---|
| **K1** | intraday_updown (all) | 50.4% | 15,570 | **Validated** — coin flip minus fees |
| **K2** | price_above cheap YES (<45c) | ~20% | — | Validated (low data) |
| **K3** | Any trade <30c YES | ~20% | — | Validated (entry filter) |
| **K4** | price_range YES only | 43% YES | 31,982 | **Relaxed** — NO side passes (57% WR) |
| **K5** | directional (all) | 70% NO | 390 | **Possibly too aggressive** — NO has edge |
| **K6** | unknown archetype | 67% NO | 20,828 | Improved — 41%→19% after classifier v2 |
| **K7** | game_total (over/under) | 52% pop / 41% traded | 10,999 | **New** — coin flip, no edge after fees |

### K6 Coverage Dramatically Improved

Classifier v2 added three archetypes, reducing K6 kills from 41% (44,839) to 19% (20,828) of Kalshi markets:

| New Archetype | Markets Reclassified | NO WR | Action |
|---|---|---|---|
| `parlay` | 4,251 | 93.7% | Passes through — massive NO edge |
| `financial_price` | 18,356 | 64.6% | Passes through — solid NO edge |
| `game_total` | 10,999 | 52.1% | K7 kills — coin flip |

### K7 Added for Game Totals

Over/under total points markets (10,999 Kalshi markets) show 52.1% population NO WR — effectively a coin flip. The traded subset drops to 40.8% WR after entry price conditioning. No edge after fees. K7 kills these before confidence calculation.

### Remaining `other` (19%)

The remaining 20,828 `other` markets (66.8% NO WR) include Fed decisions, vote margins, regulatory approvals. Further classifier expansion could reduce this below 10%.

---

## 6. Dead Code Fix: Becker Priors Were Never Used

`BECKER_NO_WIN_RATES` was defined in `empirical_confidence.py` with per-archetype base rates, but `calculate_empirical_confidence()` had a hardcoded `else 0.50` fallback. With 0 resolved trades in the local DB (early deployment), every archetype got identical 50% base confidence.

```python
# BEFORE (dead code):
arch_wr = ... if arch_trades else 0.50

# AFTER (priors wired in):
becker_prior = BECKER_NO_WIN_RATES.get(archetype, 0.593)
arch_wr = ... if arch_trades else becker_prior
```

A `social_count` market (94% NO WR) was treated identically to `intraday_updown` (50% NO WR). The entire archetype differentiation was decorative.

---

## 7. Impact on Live System

Tested the combined changes (new priors + new modifiers + dead code fix) against 8 active shadow trades:

| Change | Effect |
|---|---|
| Premium/sweet zone trades | 20-33pp confidence reduction |
| Example: Iran nuclear NO @ 82c | 83% -> 50% confidence |
| Expensive zone trades | Up to 40pp reduction |
| Mid zone trades | Minimal change |

The corrections reduce overconfidence on expensive entries where NO rarely wins, which is the correct behavior.

---

## 8. Duration Modifier Validation

Duration modifiers were previously from Becker's study and never validated against our data. After classifier v2 reclassified 33K+ markets out of `other`, we recomputed duration bucket NO WRs on 97K tradeable markets (Kalshi + Polymarket, after kill rules).

### Blended WR (both platforms, tradeable only)

| Bucket | Days | NO WR | n | Old Mod | New Mod |
|---|---|---|---|---|---|
| daily | 0-1 | 57.8% | 51,425 | 0.85 | **0.94** |
| short | 2-3 | 61.7% | 18,030 | 0.95 | **1.00** |
| weekly | 4-7 | 70.8% | 13,502 | 1.10 | **1.15** |
| biweekly | 8-14 | 65.2% | 10,471 | 1.05 | **1.06** |
| monthly | 15-30 | 66.1% | 3,614 | 1.10 | **1.08** |

Baseline: 61.4% NO WR across all tradeable durations. Modifier = bucket WR / baseline.

### Key Findings

1. **Daily was over-penalized.** Old modifier (0.85) assumed daily markets were weak. After reclassifying parlays and game totals out, daily markets show 57.8% NO WR — much closer to baseline than Becker suggested.
2. **Short is at baseline.** 0.95→1.00. No duration bonus or penalty for 2-3 day markets.
3. **Weekly confirmed as sweet spot.** 70.8% NO WR (n=13,502). Strongest edge of any duration bucket.
4. **Platform divergence is small.** <9pp gap on all buckets. No need for platform-specific modifiers.
5. **quarterly/long kept at Becker values** since our max_days_to_close filter caps at 30 days.

### Kalshi Duration x Archetype (tradeable, top 6)

The duration effect varies by archetype:

- **financial_price**: Weekly is extraordinary — 83.3% NO WR (n=2,079) vs 60.9% daily
- **parlay**: High WR across all durations (>89%) — duration barely matters
- **sports_winner**: Daily near coin flip (50.2%), weekly much stronger (70.3%)
- **price_range**: Concentrated in daily (n=26,179), limited data at longer durations

---

## 9. Files Changed

| File | Change |
|---|---|
| `signals/empirical_confidence.py` | Updated `BECKER_NO_WIN_RATES`, `PRICE_ZONE_MODIFIERS`, `DURATION_MODIFIERS`, fixed dead code fallback |
| `signals/mispriced_category_signal.py` | Added `parlay`, `financial_price`, `game_total` archetypes; K7 kill rule; updated K1-K6 comments |
| `tests/unit/test_archetype_classifier.py` | Added 12 classifier + 3 kill rule test cases (63 total) |
| `scripts/prediction_market_backtest.py` | New backtest harness (DuckDB trades join, Kelly simulation, population analysis) |

---

## 9. Remaining Work

1. ~~**Expand classifier**~~ **DONE.** Added `parlay`, `financial_price`, `game_total`. `other` reduced 41%→19%.
2. ~~**Validate duration modifiers**~~ **DONE.** Validated on 97K tradeable markets. daily 0.85→0.94, short 0.95→1.00, weekly 1.10→1.15.
3. **Reconsider K5** — Directional markets have 70% NO WR; consider relaxing to kill only YES side
4. **YES-side analysis** — Current modifiers are NO-side only; run `--both-sides` backtest
5. **Calibration loop** — `calibration_audit()` exists but needs resolved trades in local DB to function
6. **Minor archetype sample sizes** — `ai_model` (n=54), `weather` (n=68), `entertainment` (n=114) are noisy
7. **Further `other` reduction** — 19% remaining includes Fed decisions, vote margins; more archetypes could push below 10%

---

## 10. Key Lessons

1. **Verify what "price" means.** `last_price` = resolution price caused a 14pp WR swing.
2. **n=51 lies.** Sweet zone showed 100% WR (n=4). Reality: 37.7% (n=2,812). Small samples tell the wrong story.
3. **Population WR != trade-level WR.** Population counts resolutions. Trade-level conditions on entry price. Both useful, different answers.
4. **Wiring > design.** Becker priors were correct and well-documented but hardcoded `0.50` meant they were never used. Dead code that looks alive is worse than missing code.
5. **Price zone gradients are universal.** Mid > sweet > premium > expensive WR pattern holds across every archetype. The market is usually right about probability.
6. **Confidence must estimate probability, not signal quality.** A high-quality signal (volume spike, whale activity) on a coin-flip market (intraday up/down) still loses money.

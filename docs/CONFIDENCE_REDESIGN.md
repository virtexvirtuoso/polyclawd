# Confidence Redesign: From Signal Quality to Win Probability

> **Status:** Phase 0 complete — empirical calibration validated at scale, classifier expanded, duration validated
> **Date:** 2026-02-19 (original), 2026-02-21 (backtest validation + classifier expansion + duration validation)
> **Data basis:** 159,220 resolved markets (110,427 Kalshi + 48,789 Polymarket) + 16,575 simulated trades

---

## 1. The Problem

Our confidence score is **useless as a win predictor**.

| Metric | Value |
|---|---|
| Avg confidence on WINS | 85.3% |
| Avg confidence on LOSSES | 81.8% |
| **Separation** | **3.5%** |

A 3.5% separation means confidence tells us almost nothing about whether we'll actually win. A random number generator would do nearly as well.

### Why It's Broken

The current confidence measures **signal quality indicators** — not win probability:

```
confidence = (
    category_edge_score × 0.35    ← "how mispriced is this category historically?"
  + volume_spike_score  × 0.25    ← "is there unusual volume?"
  + whale_activity      × 0.20    ← "are whales trading this?"
  + theta_score         × 0.20    ← "how soon does it expire?"
) × confirmation_bonus × velocity_modifier
```

Every short-dated, high-volume market in a historically mispriced category gets 80-90% confidence **regardless of whether our fade will win**. Then edge is calculated as:

```
edge = confidence - market_price
```

Producing fantasy edges like 49% on markets where our actual edge is 2-5%.

### The Consequence

The system enters trades with inflated confidence, sizes positions with fake edge, and the Kelly criterion — which depends on honest probability estimates — produces meaningless bet sizes.

---

## 2. What Actually Predicts Wins

From 51 resolved trades, three factors have real predictive power:

### 2.1 Market Archetype

| Archetype | Record | WR | Description |
|---|---|---|---|
| **daily_updown** | 13W/5L | **72%** | "Bitcoin Up or Down on February 14?" — daily resolution |
| **intraday_updown** | 9W/8L | **53%** | "Bitcoin Up or Down - February 14, 2PM ET" — intraday |
| **price_above** | 5W/8L | **38%** | "Will BTC be above $68,000 on February 17?" — strike price |
| **price_range** | 0W/1L | **0%** | "Bitcoin price range on Feb 18?" — binary option on exact strike |
| **btc_dip** | 0W/1L | **0%** | "Will Bitcoin dip to $60,000?" — directional longshot |

**Market type alone explains more variance than confidence, volume, category edge, and theta combined.**

### 2.2 Side Selection (by Archetype)

| Archetype | YES WR | NO WR | Best Side |
|---|---|---|---|
| daily_updown | **83%** (n=12) | 50% (n=6) | YES |
| intraday_updown | 22% (n=9) | **88%** (n=8) | NO |
| price_above | 20% (n=5) | 50% (n=8) | NO |

This is the most important finding: **the correct side to take depends entirely on market type.**

- Daily up/down: Take YES (market under-prices "up" on daily resolution)
- Intraday up/down: Take NO (market over-prices short-term moves)
- Price above: Take NO (market over-prices threshold breach)

### 2.3 Entry Price Zone

| Entry Price | WR | n | Verdict |
|---|---|---|---|
| <30¢ | 20% | 10 | ☠️ Bleeding — cheap = wrong |
| 30-45¢ | 43% | 14 | ❌ Losing — still too cheap |
| 45-55¢ | 100% | 1 | ⚠️ Insufficient data |
| 55-65¢ | 64% | 11 | ✅ Edge zone |
| 65-75¢ | 100% | 4 | 🎯 Sweet spot |
| 75-85¢ | 80% | 5 | ✅ Strong |
| 85-100¢ | 50% | 6 | ⚠️ Overpaying |

**Cheap contracts (<45¢) lose money.** The market is correctly pricing them as unlikely. Our "fade" strategy works best when the market is already leaning our way (55-85¢).

### 2.4 Multi-Factor Sweet Spots

| Archetype | Side | Price Zone | WR | n |
|---|---|---|---|---|
| daily_updown | YES | premium (65-85¢) | **100%** | 2 |
| daily_updown | YES | expensive (85¢+) | **100%** | 2 |
| daily_updown | YES | cheap (<45¢) | **71%** | 7 |
| intraday_updown | NO | mid (45-65¢) | **80%** | 5 |
| intraday_updown | NO | premium (65-85¢) | **100%** | 2 |
| price_above | NO | premium (65-85¢) | **100%** | 3 |

### 2.5 What Has Zero Predictive Power

| Factor | Separation | Verdict |
|---|---|---|
| Confidence score | 3.5% | ❌ Useless |
| Confirmations (2 vs 3 vs 4) | ~2% | ❌ Useless |
| Volume (all >50K) | N/A | ❌ No variance in sample |
| Category tier | ~14% | ⚠️ Weak, confounded by sample |

---

## 3. The Redesign

### 3.1 Core Principle

> **Confidence must equal our empirical probability of winning.**

If confidence says 75%, we should win ~75% of trades at that confidence level. This is called **calibration** — and it's the foundation of profitable betting.

### 3.2 New Confidence Formula

```
confidence = bayesian_smooth(
    prior   = archetype_side_WR,
    data    = bucket_WR,
    n       = bucket_sample_size,
    weight  = 5
) × price_zone_modifier
```

#### Step 1: Classify Market Archetype

```python
def classify_archetype(title: str) -> str:
    t = title.lower()
    if 'up or down' in t:
        if has_intraday_pattern(t):  # AM/PM, time ranges, 5m/15m/1h/4h
            return 'intraday_updown'
        return 'daily_updown'
    if 'above' in t or 'price of' in t:
        return 'price_above'
    if 'range' in t or 'between' in t:
        return 'price_range'
    if 'dip' in t or 'crash' in t or 'fall' in t:
        return 'directional'
    return 'other'
```

#### Step 2: Look Up Base Win Rate

From resolved trades in the database, grouped by `(archetype, side)`:

```python
BASE_WR = {
    ('daily_updown', 'YES'):     0.83,   # n=12
    ('daily_updown', 'NO'):      0.50,   # n=6
    ('intraday_updown', 'NO'):   0.88,   # n=8
    ('intraday_updown', 'YES'):  0.22,   # n=9 — KILL ZONE
    ('price_above', 'NO'):       0.50,   # n=8
    ('price_above', 'YES'):      0.20,   # n=5 — KILL ZONE
}
```

These update automatically as more trades resolve.

#### Step 3: Apply Bayesian Smoothing

Small samples are noisy. We smooth toward the archetype-level prior:

```python
def bayesian_smooth(prior_wr, bucket_wr, n, prior_weight=5):
    """
    Bayesian smoothing with conjugate beta prior.
    prior_weight=5 means "trust the prior as much as 5 observations."
    As n grows, the bucket data dominates.
    """
    return (prior_wr * prior_weight + bucket_wr * n) / (prior_weight + n)
```

Example: `daily_updown YES premium` — prior = 83% (daily YES overall), bucket = 100% (n=2):
```
smoothed = (0.83 × 5 + 1.00 × 2) / (5 + 2) = 0.886 → 88.6%
```

#### Step 4: Price Zone Modifier

Entry price quality multiplier based on empirical data:

| Zone | Price Range | Modifier | Rationale |
|---|---|---|---|
| garbage | <30¢ | 0.25 | 20% WR — market is right, we're wrong |
| cheap | 30-45¢ | 0.55 | 43% WR — slightly below breakeven |
| mid | 45-55¢ | 0.85 | Limited data, neutral zone |
| edge | 55-65¢ | 1.00 | 64% WR — reference zone |
| sweet | 65-75¢ | 1.15 | 100% WR (small n), best risk/reward |
| premium | 75-85¢ | 1.10 | 80% WR — strong but less upside |
| expensive | 85-100¢ | 0.75 | 50% WR — overpaying for certainty |

#### Step 5: Final Confidence (capped)

```python
confidence = min(0.92, bayesian_smooth(...) × price_zone_modifier)
```

Cap at 92% — no trade is ever a certainty. Leaves room for model error.

### 3.3 New Edge Formula

```
For YES: edge = confidence - entry_price
For NO:  edge = confidence - (1 - entry_price)
```

Now this means something real. If confidence = 75% and we're buying YES @60¢:
```
edge = 0.75 - 0.60 = 0.15 → 15% real edge
```

This is an honest 15% because confidence IS our win probability, not a signal quality score.

### 3.4 Kelly Sizing (Unchanged but Now Honest)

```python
# Variable odds Kelly
b = (1 - entry_price) / entry_price  # for YES
f_star = (b * confidence - (1 - confidence)) / b
bet = bankroll * f_star * KELLY_FRACTION  # 1/2 Kelly
```

With honest confidence, Kelly naturally sizes:
- High-confidence cheap trades → larger bets (as it should)
- Low-edge premium trades → tiny bets (protecting against overpaying)
- Kill zone trades → negative Kelly → no bet

---

## 4. Kill Rules (Hard Rejects)

Some market/side/price combinations should **never** be traded. Confidence = 0, no exceptions.

| Rule | Condition | Empirical WR | Reason |
|---|---|---|---|
| K1 | `intraday_updown` + `YES` | 22% (n=9) | Market over-prices intraday YES. Coin flip minus fees. |
| K2 | `price_above` + `YES` + entry < 45¢ | 20% (n=5) | Cheap longshot lottery tickets. Market is right. |
| K3 | Any trade + entry < 30¢ | 20% (n=10) | Cheap = wrong. Below 30¢ the market has priced it correctly. |
| K4 | `price_range` + YES side | 43% (n=31,982) | YES side loses. NO side passes through (57% WR). |
| K5 | `directional` (dip/crash) | 70% NO (n=390) | Low sample, kill to be safe. |
| K6 | Unknown archetype (`other`) | 67% NO (n=20,828) | Don't trade what we can't classify. |
| K7 | `game_total` (over/under) | 52% NO (n=10,999) | Coin flip after fees. 41% traded WR. |

---

## 5. Self-Improving Feedback Loop

The new system automatically gets better with more data:

```
┌─────────────────────────────────────────────────────────┐
│                   FEEDBACK LOOP                         │
│                                                         │
│  Trade resolves                                         │
│       ↓                                                 │
│  Update WR bucket: (archetype, side, price_zone)        │
│       ↓                                                 │
│  Bayesian smooth recalculates base rates                │
│       ↓                                                 │
│  Next signal uses updated confidence                    │
│       ↓                                                 │
│  Kelly sizes according to honest probability            │
│       ↓                                                 │
│  Calibration check: binned confidence vs actual WR      │
│       ↓                                                 │
│  If miscalibrated → adjust price_zone_modifiers         │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### Calibration Audit (runs weekly or every 20 resolved trades)

```python
def calibration_check(trades):
    """
    Bin trades by confidence decile, compare predicted vs actual WR.
    Perfect calibration: 70% confidence trades win 70% of the time.
    """
    for lo in range(50, 95, 5):
        hi = lo + 5
        bucket = [t for t in trades if lo <= t.confidence * 100 < hi]
        if len(bucket) >= 5:
            actual_wr = sum(t.won for t in bucket) / len(bucket)
            predicted_wr = (lo + hi) / 2 / 100
            miscalibration = actual_wr - predicted_wr
            # If |miscalibration| > 10%, flag for review
```

### Sample Size Governance

| Total Resolved | Bayesian Prior Weight | Behavior |
|---|---|---|
| <30 | 10 | Heavy prior — conservative, trusts base rates |
| 30-100 | 5 | Balanced — data starts to dominate |
| 100-300 | 3 | Data-driven — priors are training wheels |
| 300+ | 1 | Empirical — bucket data is the truth |

As we approach 200 resolved trades (~Mar 7), the system will have enough data per bucket to produce reliable confidence estimates without relying on priors.

---

## 6. Projected Impact

### Before (Current System)

| Metric | Value |
|---|---|
| Overall WR | 52.9% |
| Confidence separation | 3.5% (useless) |
| Fake edges | 30-50% reported |
| Worst trades entered | YES @22¢ (20% WR), intraday YES (22% WR) |
| Kelly sizing | Based on fantasy → meaningless |

### After (Projected)

| Metric | Projected |
|---|---|
| Overall WR | **65-75%** (kill rules eliminate bottom 30% of trades) |
| Confidence separation | **~30%** (by design — confidence IS win probability) |
| Real edges | 5-20% (honest, tradeable) |
| Trades rejected | ~50% of current signals (the losing ones) |
| Kelly sizing | Honest → proper risk management |

### Trade Volume Impact

Current: ~10-15 signals per day → ~8-12 enter portfolio

After kill rules: ~10-15 signals → **~4-6 enter portfolio**

Fewer trades, but the ones we take should actually make money.

---

## 7. Implementation Plan

### Files to Create/Modify

| File | Action | Description |
|---|---|---|
| `signals/archetype_classifier.py` | **NEW** | Market archetype classification + side recommendation |
| `signals/empirical_confidence.py` | **NEW** | Bayesian smoothed WR lookup, price zone modifiers, calibration |
| `signals/mispriced_category_signal.py` | **MODIFY** | Replace `calculate_signal_confidence()` with new system |
| `signals/paper_portfolio.py` | **MODIFY** | Edge = confidence - cost_basis (honest calc) |
| `signals/calibrator.py` | **MODIFY** | Add calibration audit for new confidence |
| `mcp/server.py` | **MODIFY** | Expose new confidence breakdown in API |

### Migration

1. Deploy new confidence alongside old (shadow mode) for 48 hours
2. Compare: does new confidence predict wins better?
3. If yes → swap to new confidence for portfolio decisions
4. Old confidence retained as `signal_quality` score (useful for filtering noise, not for sizing)

### Rollback

If new confidence performs worse:
- Revert `evaluate_signal()` to use old confidence
- Keep archetype classifier and kill rules (these are pure wins regardless)

---

## 8. Open Questions

1. **Sample size:** n=51 is small. Some buckets have n=2. How aggressive should we be with the Bayesian prior weight?
   - **Recommendation:** Start conservative (prior_weight=10), decrease as data accumulates.

2. **Market regime:** Are these WR patterns stable, or are they specific to the Feb 13-19 BTC price regime?
   - **Mitigation:** Decaying window — recent trades weighted more than old ones (half-life = 14 days).

3. **Should we keep signal quality as a secondary filter?**
   - **Yes.** Volume, theta, and category edge are useful for filtering noise even if they don't predict wins. Use them as minimum thresholds, not as confidence inputs.

4. **What about new market types we haven't seen?**
   - Default to conservative: `confidence = 0.50 × price_zone_modifier`, small position until data accumulates.

---

## 9. Success Criteria

The redesign is successful if, after 50 trades under the new system:

- [ ] Confidence-WR separation > 15% (currently 3.5%)
- [ ] Calibration error < 10% per decile bin
- [ ] Overall WR > 60% (currently 52.9%)
- [ ] No trade entered with honest edge < 5%
- [ ] Kelly sizing produces bankroll growth (not decay)

---

## 10. Phase 0 Backtest Validation (2026-02-21)

The backtest harness (`scripts/prediction_market_backtest.py`) replayed the **actual** signal pipeline on 50 GB of historical data (769 Kalshi market parquet files + 286 Polymarket files). This section documents what we found and what changed as a result.

### 10.1 Critical Bug: `last_price` Is Resolution Price

**Discovery:** The initial backtest showed a 26% NO win rate — suspiciously low. Investigation revealed that Kalshi's parquet `last_price` field is the **resolution price**, not an entry price. Markets resolving YES have median `last_price` = 99c; markets resolving NO have median `last_price` = 1c.

**Impact:** Filtering `last_price >= 55` (our MIN_ENTRY_PRICE) selected almost exclusively YES-resolved markets, creating massive survivorship bias.

**Fix:** Rewrote `load_kalshi_markets()` to JOIN with 72M trade records via DuckDB, extracting the first trade at an eligible price (55-92c YES range) per market:

```sql
WITH first_entry AS (
    SELECT t.ticker, t.yes_price AS entry_cents,
           ROW_NUMBER() OVER (PARTITION BY t.ticker ORDER BY t.created_time) AS rn
    FROM trades t SEMI JOIN resolved r ON t.ticker = r.market_id
    WHERE t.yes_price BETWEEN 55 AND 92
)
SELECT r.*, fe.entry_cents
FROM resolved r LEFT JOIN first_entry fe ON r.market_id = fe.ticker AND fe.rn = 1
```

**Lesson:** Always verify what "price" means in historical data. Resolution price != entry price.

### 10.2 Updated Archetype Win Rates (159K Markets)

The original Section 2.1 was based on 51 trades. The backtest validated across **159,220 resolved markets** from both platforms. These are **population-level NO win rates** — the fraction of markets where NO wins, regardless of entry price.

After classifier expansion (v2, 2026-02-21), three new archetypes (`parlay`, `financial_price`, `game_total`) were added, reducing the `other` bucket from 41% to 19% of Kalshi markets.

| Archetype | n | NO WR | Prior | Delta | Notes |
|---|---|---|---|---|---|
| **parlay** | 4,251 | **93.7%** | — | — | NEW — multi-leg bets almost always fail |
| **social_count** | 1,773 | **94.1%** | 60.0% | +34.1 | NO almost always wins (Poly only) |
| **weather** | 68 | **85.3%** | 60.0% | +25.3 | Small n, outsized NO edge |
| **ai_model** | 54 | **74.1%** | 77.4% | -3.3 | Becker was close |
| **entertainment** | 114 | **71.1%** | 60.0% | +11.1 | |
| **directional** | 390 | **69.7%** | 60.0% | +9.7 | Poly only |
| **deadline_binary** | 9,062 | **69.4%** | 60.0% | +9.4 | Large n, reliable |
| **geopolitical** | 315 | **68.6%** | 69.6% | -1.0 | Becker was accurate |
| **financial_price** | 18,356 | **64.6%** | — | — | NEW — S&P, Nasdaq, forex, gold, VIX |
| **election** | 794 | **63.9%** | 69.6% | -5.7 | |
| **other** | 20,828 | **66.8%** | 59.3% | +7.5 | Shrunk from 60K → 21K after classifier v2 |
| **price_above** | 3,763 | **59.3%** | 52.8% | +6.5 | |
| **price_range** | 31,982 | **56.6%** | 88.6% | **-32.0** | Becker was wildly off |
| **sports_winner** | 27,642 | **56.7%** | 78.1% | **-21.4** | Becker overestimated |
| **sports_single_game** | 6,309 | **56.0%** | 78.1% | **-22.1** | Becker overestimated |
| **game_total** | 10,999 | **52.1%** | — | — | NEW — coin flip, K7 kills it |
| **intraday_updown** | 15,570 | **50.4%** | 51.7% | -1.3 | Coin flip confirmed |
| **daily_updown** | 887 | **46.3%** | 51.7% | -5.4 | Poly only |

**Key findings:**

1. **Parlay is a goldmine.** 93.7% NO WR (n=4,251). Multi-leg combined bets almost always fail because any single leg losing = NO wins. Highest-edge archetype discovered.
2. **Financial price has solid edge.** 64.6% NO WR (n=18,356). S&P 500, forex, gold, VIX threshold markets — the market overprices breach probability.
3. **Game total is a coin flip.** 52.1% population WR, 40.8% traded WR. No edge after fees. K7 kills it.
4. **Classifier expansion cut `other` from 41% → 19%.** 33,528 markets reclassified from `other` into specific archetypes.
5. **Becker's sports priors were wrong.** Both `sports_winner` (78.1% → 56.7%) and `sports_single_game` (78.1% → 56.0%) were grossly overestimated.
6. **`social_count` is free money for NO.** 94.1% NO WR (n=1,773) — follower count markets almost never hit YES.
7. **`intraday_updown` is a coin flip** (50.4%, n=15,570). K1 kill rule validated at scale.

### 10.3 Price Zone Win Rates (NO Side, 10K+ Kalshi Trades)

The original Section 2.3 was based on 51 trades. The backtest used **10,534 simulated NO trades** with real entry prices from 72M trade records.

| Zone | YES Price | NO WR | Old Modifier | New Modifier | n |
|---|---|---|---|---|---|
| mid | 55-65c | **45.5%** | 1.00 | 1.00 (reference) | 4,401 |
| sweet | 65-75c | **37.7%** | 1.15 | **0.83** | 2,812 |
| premium | 75-85c | **30.1%** | 1.10 | **0.66** | 1,958 |
| expensive | 85-92c | **23.3%** | 0.75 | **0.51** | 1,363 |

**The old modifiers were backwards.** The original 51-trade sample showed higher WR at sweet/premium zones (100%, n=4; 80%, n=5), but the 10K-trade backtest proved the opposite: higher YES price = lower NO WR. This makes intuitive sense — when the market prices YES at 80c, it's usually right.

**Why Kelly doesn't compensate:** Kelly sizing captures the **payoff** asymmetry (NO at 80c YES costs 20c, pays $1 → 4:1 payout), but the confidence system needs to reflect the **probability** of winning. The old modifiers boosted confidence at sweet/premium, making Kelly think the trade was both high-probability AND high-payoff — double-counting the edge.

**The gradient is archetype-independent.** Verified across `sports_winner`, `deadline_binary`, `election`, and `sports_single_game` — all show the same mid > sweet > premium > expensive WR pattern.

### 10.4 Dead Code: Becker Priors Never Used

**Discovery:** `BECKER_NO_WIN_RATES` was defined in `empirical_confidence.py` but **never referenced** by `calculate_empirical_confidence()`. Line 313 had:

```python
# BEFORE (dead code — every archetype got 50%):
arch_wr = ... if arch_trades else 0.50
overall_wr = ... if trades else 0.50

# AFTER (priors wired in):
becker_prior = BECKER_NO_WIN_RATES.get(archetype, 0.593)
arch_wr = ... if arch_trades else becker_prior
overall_wr = ... if trades else 0.593
```

**Impact:** With 0 resolved trades in the local DB (early deployment), every archetype got identical 50% base confidence. A `social_count` market (94% NO WR) was treated the same as an `intraday_updown` market (50% NO WR). The entire archetype differentiation was theater — the data existed but was never consulted.

**Impact on active trades:** Tested against 8 live shadow trades. Premium/sweet zone trades saw 20-33pp confidence reductions (e.g., Iran nuclear deal NO @ 82c: 83% → 50%), correctly reducing overconfidence on expensive entries where NO rarely wins.

### 10.5 Kill Rule Validation at Scale

| Rule | Markets Killed | Killed WR | Avoided Loss? | Verdict |
|---|---|---|---|---|
| **K1** (intraday) | ~15,570 | 50.4% | Yes — coin flip minus fees | Validated |
| **K2** (price_above cheap YES) | — | — | — | Not testable (NO-only default) |
| **K3** (sub-30c) | — | — | — | Not testable (entry price filter) |
| **K4** (price_range) | Relaxed | 56.6% | Marginal | Pass-through for NO side |
| **K5** (directional) | 390 | 69.7% NO | Possibly too aggressive | Under review |
| **K6** (unknown) | ~20,828 | 66.8% | Yes | Reduced from 60K→21K by classifier v2 |
| **K7** (game_total) | ~10,999 | 52.1% pop / 40.8% traded | Yes — no edge | **New** — added 2026-02-21 |

**K1 confirmed:** Intraday up/down is noise. 50.4% NO WR across 15,570 markets = zero edge after fees.

**K4 relaxed:** Becker said 89% NO WR; empirical shows 57%. NO side passes through.

**K5 may be too aggressive:** Directional markets show 70% NO WR (n=390). Low sample; revisit after more data.

**K6 dramatically improved:** Classifier expansion reduced `other` from 41% (44,839) to 19% (20,828) of Kalshi markets. Three new archetypes (`parlay`, `financial_price`, `game_total`) reclassified 33K+ markets. Remaining `other` has 66.8% NO WR — still killed as unclassified.

**K7 added for game_total:** Over/under total points markets show 52.1% population NO WR (n=10,999) — effectively a coin flip. Traded subset WR drops to 40.8% after entry price conditioning. No edge after fees; these markets are efficiently priced.

### 10.6 System Changes Applied

| Component | Change | File |
|---|---|---|
| Becker priors | Updated from 408K Becker study to 159K empirical | `empirical_confidence.py` |
| Price zone modifiers | Inverted: sweet 1.15→0.83, premium 1.10→0.66 | `empirical_confidence.py` |
| Dead code fix | Wired `BECKER_NO_WIN_RATES` into Bayesian fallback | `empirical_confidence.py` |
| Kill rule comments | Updated K1, K4, K5 with empirical data | `empirical_confidence.py`, `mispriced_category_signal.py` |
| Tests | Fixed 3 expectations (Oscars→entertainment, K4 pass-through, K6 title) | `test_archetype_classifier.py` |
| Backtest harness | Full script with DuckDB trades join, Kelly sizing, population analysis | `scripts/prediction_market_backtest.py` |
| **Classifier v2** | Added `parlay`, `financial_price`, `game_total` archetypes | `mispriced_category_signal.py` |
| **K7 kill rule** | Kill `game_total` (52% NO WR, no edge after fees) | `mispriced_category_signal.py` |
| **Duration modifiers** | Validated from Becker priors to 97K tradeable markets empirical | `empirical_confidence.py` |
| **New archetype priors** | `parlay: 0.937`, `financial_price: 0.646`, `game_total: 0.521` | `empirical_confidence.py` |
| **New archetype tests** | 12 classifier + 3 kill rule test cases | `test_archetype_classifier.py` |

### 10.7 Remaining Gaps

1. ~~**Classifier coverage (41% → `other`):**~~ **DONE.** Expanded classifier reduced `other` from 41% to 19% with `parlay`, `financial_price`, `game_total` archetypes. 33K+ markets reclassified.

2. **YES-side modifier asymmetry:** Current modifiers are NO-side only. YES-side trades at different price zones likely have different WR patterns. Need `--both-sides` backtest analysis.

3. ~~**Duration modifier validation:**~~ **DONE.** Validated on 97K tradeable markets (blended Kalshi + Polymarket). Key changes: daily 0.85→0.94, short 0.95→1.00, weekly 1.10→1.15.

4. **K5 directional reconsideration:** 70% NO WR with n=390 suggests genuine edge for NO side. K5 could be relaxed to only kill YES side (mirroring K4's relaxation).

5. **Calibration loop not yet running:** The `calibration_audit()` function exists but requires resolved trades in the local DB (currently empty on fresh deploy). As trades accumulate, this becomes the primary feedback mechanism.

6. **Sample sizes in minor archetypes:** `ai_model` (n=54), `weather` (n=68), `entertainment` (n=114) have small samples. Priors here are noisy and will shift as data accumulates.

7. **Further `other` bucket reduction (19% remaining):** The remaining 20,828 `other` markets include Fed decisions, vote margins, regulatory approvals. Additional archetypes could bring coverage below 10%.

### 10.8 Key Lessons

1. **Always verify what "price" means in historical data.** The `last_price` bias took the entire backtest from 26% WR to ~40% WR — a 14pp swing from a single column misinterpretation.

2. **Population WR != trade-level WR.** Population WR counts all market resolutions. Trade-level WR is conditioned on entry price. For NO bets at high YES prices, trade-level WR is always lower than population WR. Both are useful; they answer different questions.

3. **n=51 is dangerously small.** The original analysis showed sweet zone WR = 100% (n=4) and premium WR = 80% (n=5). The 10K-trade backtest showed sweet = 37.7% and premium = 30.1%. Small samples told the opposite story.

4. **Wiring matters as much as design.** `BECKER_NO_WIN_RATES` was beautifully designed and commented — but a hardcoded `0.50` fallback meant it was never used. Dead code that looks alive is worse than missing code.

5. **Price zone gradients are universal.** The mid > sweet > premium > expensive WR pattern holds across every archetype tested. This is the market being right about probability — the higher the YES price, the more likely YES wins.

6. **The confidence system's job is probability estimation, not signal quality.** The old system conflated "this is a good signal" with "we'll probably win." These are different things. Good signals with bad probability (intraday up/down with volume spike) still lose money.

---

*"The goal is not to be confident. The goal is to be right."*

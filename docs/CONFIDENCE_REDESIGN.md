# Confidence Redesign: From Signal Quality to Win Probability

> **Status:** Proposal — pending implementation  
> **Date:** 2026-02-19  
> **Data basis:** 51 resolved trades (45 shadow + 6 paper)

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
| K4 | `price_range` + any | 0% (n=1) | Binary option on exact strike. Efficiently priced derivative. |
| K5 | `directional` (dip/crash) + `YES` | 0% (n=1) | Tail risk bet with no edge. |
| K6 | Unknown archetype | — | Don't trade what we can't classify. |

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

*"The goal is not to be confident. The goal is to be right."*

# Whale Alert Trading System — Execution Plan

> **Goal:** Turn a ranked alert feed into profitable trades. Auto-learns from every resolution.

---

## Current State (VPS, June 13)

| What | Count | Status |
|------|-------|--------|
| Total alerts logged | 59,276 | ✅ Working |
| Whale outcomes tracked | 57,836 | ✅ Working |
| Whale paper follows | 245 | ✅ Working |
| **Result tracking (wins/losses)** | **237/245 empty** | ❌ **Broken** |
| CRITICAL alerts/day | ~900 | ❌ **Need prioritization** |
| Kalshi integrated | 44,920 (76%) | ✅ |
| Polymarket integrated | 14,547 (24%) | ✅ |
| Smart wallets tracked | 59 | ✅ |

**The system detects whales fine. It cannot rank them, cannot measure its own performance, and cannot adapt when markets change.**

### ⚠️ Critical Context: The System Is 2 Days Old

Whale scanner went live on June 11. All data below is **2-3 days of observations** — not weeks or months.

| Stat | Value | Implication |
|------|-------|-------------|
| CRITICAL resolved | 32 | N=32 is **not statistically significant.** The 65.6% precision could be ±15%. |
| Resolution window | All 411 resolved in ~35min on June 11 | These aren't independent events — they're the same daily markets tracked under multiple alert IDs. A Bayesian system trained on this converges to a model of one afternoon. |
| Counterfactual data | Only 424/59K have price_1h (0.7%) | Cannot compute follow/fade P&L for most alerts yet. Need weeks of backfill before counterfactual analysis is meaningful. |
| System runtime | ~48 hours | No drift data exists. No seasonal patterns visible. No regime baseline established. |

**Realistic timeline for auto-learning:** 3-5 weeks of data collection before Bayesian weights converge enough to be useful for trading decisions. The first 2-3 weeks should focus on getting clean, reliable data — not on building the ML model.

---

## What We're Building: Three-Layer Auto-Learning System

```
Layer 1 — Bayesian Score (updates every alert)
├── Feature weights auto-tune from every resolved alert
├── Wallet win rates use exponential decay (recent > old)
└── Score = weighted combination with uncertainty bounds

Layer 2 — Contextual Bandit (updates weekly)
├── Learns which action (follow/fade/skip) is best for each alert type
├── Uses Thompson sampling — explores new strategies while exploiting known ones
└── Counterfactual evaluation: what if we had done the opposite?

Layer 3 — Drift Detection (continuous, flags automatically)
├── Moving window precision (last 50 alerts vs 50 before)
├── Feature importance tracking (is flow_dollars still predictive?)
├── Auto-adjusts thresholds when precision drops 15%+
└── Alerts you: "Strategy drifting — suggest review"
```

### Sources & Research Basis

| Layer | Technique | Source | Why It Fits Us |
|-------|-----------|--------|----------------|
| **Bayesian Score** | Conjugate Beta-Binomial priors with exponential weight decay | Duran-Martin, "Adaptive, Robust and Scalable Bayesian Filtering for Online Learning" (arXiv 2505.07267, May 2025) | Sequential Bayesian updating is ideal for non-stationary environments where data arrives one alert at a time — exactly our resolution stream |
| **Contextual Bandit** | Thompson sampling with Beta priors; off-policy counterfactual evaluation | Agarwal et al., "Off-policy Evaluation and Learning" (Harvard/ICLR); MetaEXP-OPE, ACM 2025 (10.1145/3726302.3730176) | One-shot decisions (follow/fade/skip) map to the bandit framework; off-policy evaluation lets us learn from ALL alerts, not just acted-on ones |
| **Drift Detection** | Moving window precision monitoring; proactive model adaptation | "Proactive Model Adaptation Against Concept Drift for Online Time Series Forecasting" (KDD 2025, arXiv 2412.08435); IEEE Access 2025 (10.1109/ACCESS.2025.3572901) | CRITICAL precision dropping from 65% → 48% is exactly the concept drift pattern these methods detect; we use simple rolling windows instead of Wasserstein distance because our feature space is small and interpretability matters |
| **Counterfactual** | Importance sampling for off-policy evaluation | Swaminathan & Joachims, "Off-Policy Evaluation for Bandit Algorithms" (ICML); Open Bandit Dataset pipeline (Yahoo/RecSys) | Computing follow_pnl and fade_pnl for EVERY alert creates a complete counterfactual dataset — no need for importance sampling corrections since we can deterministically compute trajectory P&L |
| **Weight Decay** | Adaptive weight decay via MAP Bayesian priors | Apple ML Research, "Adaptive Weight Decay" (machinelearning.apple.com); Auroria.io, "From Bayesian Priors to Weight Decay" | Exponential moving average on wallet win rates is the simplest form of adaptive weight decay — recent trades > old trades, controlled by a single decay parameter lambda

---

## How Bayesian Auto-Learning Works (Simple, Practical)

### Weighted Win Rates with Exponential Decay

Instead of counting all a wallet's trades equally, recent trades count more:

```
wallet_score = recent_win_rate × 0.7 + lifetime_win_rate × 0.3
```

A wallet that was 8/10 in January but 2/10 since April:
- **Simple average:** 50% — looks mediocre
- **Decayed:** 25% × 0.7 + 50% × 0.3 = **32.5%** — correctly terrible lately

A wallet that was 2/10 in January but 8/10 since April:
- **Simple average:** 50% — looks mediocre  
- **Decayed:** 80% × 0.7 + 50% × 0.3 = **71%** — correctly hot lately

**Implementation:** One SQL update on `pm_wallets` that recomputes `win_rate` with a time decay factor. 10 lines of Python.

### Bayesian Feature Weights

Every feature (flow size, wallet reputation, time to resolve, spread) gets a weight and a confidence interval. As more alerts resolve, weights converge to their true values.

Initially: all weights = 1.0 (flat, no confidence)

After each resolved alert:
```
weight_update = learning_rate × (actual_outcome - predicted_outcome) × feature_value
confidence_update = confidence + feature_value²
```

A feature that consistently predicts outcomes gets high weight + high confidence. A feature that's random gets low weight + wide confidence interval → system ignores it automatically.

**Implementation:** Store weights + confidences in `kv` table. Update after every 10 resolved alerts. ~30 lines of Python, no ML library needed.

---

## How Contextual Bandit Selection Works

Each alert is a "context" with features: `{flow_dollars, wallet_win_rate, spread_bps, hours_to_resolve, market_archetype}`. The system chooses one of three actions: **follow, fade, or skip**.

**Thompson sampling:** For each action, maintain a Beta distribution of wins/losses for similar contexts. When a new alert arrives:
1. Sample from each action's Beta distribution
2. Pick the action with the highest sample
3. Execute it (or recommend it to you)
4. When the market resolves, update the Beta distribution

This naturally balances **exploration** (trying actions you're unsure about) with **exploitation** (doing what's worked before).

**Why this beats a static score:** A static score never learns that "big flow + unknown wallet" might need different treatment than "big flow + proven wallet." The bandit learns these interactions automatically.

**Implementation constraint:** Needs ~50-100 resolved alerts per action to converge. We have 411 resolved alerts total — enough to start.

---

## How Drift Detection Works

Every week, compare the last 50 alerts to the 50 before that:

| Metric | Stable System | Drifting System |
|--------|---------------|-----------------|
| CRITICAL precision | 60-70% | <50% or >80% |
| Feature importance rank | Same top 3 features | Different top features |
| Wallet win rate distribution | Normal | Skewed |
| Avg flow size | Consistent | Shifting |

**If precision drops 15%+ in any slice, the system:**  
1. Logs the drift with timestamp + affected slice  
2. Halves the weight of drifted features  
3. Suggests reverting to a simpler default strategy  
4. Alerts you: "⚠️ Whale signal degraded — last 50 alerts at 48% vs 65% baseline"  

**Implementation:** A cron job that runs weekly calibration queries and compares to stored baselines. ~40 lines of Python.

---

## Counterfactual Learning — The Most Important Part

The system currently only learns from **followed** alerts. It never learns from alerts it ignored. This is survivorship bias applied to strategy development.

**Fix:** Log EVERY alert to `whale_outcomes`, not just followed ones. For each:
- What did the ranking score say at alert time?  
- Was it followed, faded, or skipped?  
- What would have happened if we followed? (computed from price trajectory)  
- What would have happened if we faded? (computed from price trajectory)  
- Did the actual outcome match the ranking score's prediction?  

This creates a **perfect counterfactual dataset** — you can backtest any strategy change against 59K historical alerts.

**What this enables:**
- "What if we had only traded alerts with >1.5 score?" → run the query
- "What if we had always faded CRITICAL alerts?" → run the query
- "What if wallet reputation counted 2x?" → run the query

You can validate strategy changes in 5 minutes before ever risking real capital.

**Implementation:** In the backfill pipeline, after computing price trajectory, compute follow_pnl and fade_pnl for EVERY alert, not just followed ones. Store in existing schema.

---

## What This Changes in the Execution Plan

| Before | After |
|--------|-------|
| Step 1: Fix result tracking | Same — still the first thing |
| Step 2: Static prioritization | **Bayesian ranking with auto-tuning weights** |
| Step 3: Cost-adjusted P&L | Same — still critical |
| Step 4: Position sizing | Same — still critical |
| Step 5: Everything else | **Contextual bandit + drift detection + counterfactual logging** |
| N/A | **Counterfactual dataset on all 59K alerts** |

---

## Revised Execution Order (With Data Collection Gates)

```
Week 1 (this week): Fix Plumbing + Collect Baseline Data (~4h)
├── Step 1: Fix Result Tracking (30 min)
│   ├── Backfill 237 empty results
│   ├── Fix code so new follows get results automatically
│   └── Know win rate TODAY (but with grain of salt — only 2 days of data)
│
├── Step 0: Schema Expansion + Backfill (2h) [prerequisite for everything]
│   ├── Extract flow_dollars, top_wallet, best_bid/ask from JSON payloads
│   ├── Add 30 columns to whale_outcomes (focus on queryable ones, skip exotic ones)
│   ├── Build market_archetype classifier
│   └── Backfill price trajectory for existing alerts (price_1h from snapshot history)
│
├── Step 0b: Alert Prioritization — Simple MVP (1h)
│   ├── Flat weighted score from extracted fields
│   ├── No ML, just flow_size × wallet_reputation × urgency
│   └── Ranked feed endpoint showing top 10 by score
│   └── NOTE: weights are educated guesses until we have 500+ resolved alerts
│
└── Gate: Need 7+ days of data before proceeding

Week 2-3: Let Data Accumulate (monitoring only, ~1h)
├── Monitor ranked feed output
├── Collect more resolved alerts
├── Manually verify a few high-score predictions
├── Track which alert types resolve correctly
└── Gate: Need 200+ resolved CRITICAL alerts (not just 32)

Week 4: Cost-Adjusted P&L + Position Sizing (2h)
├── Step 3: Add spread costs to all P&L calculations
├── Step 4: Kelly sizing, max daily loss, correlation limits
├── Re-run follower P&L with real costs
└── Gate: Need 200+ CRITICAL resolves with consistent precision

Week 5+: Build Auto-Learning (ongoing)
├── Step 2b: Bayesian feature weights (once 500+ resolved)
├── Step 5a: Contextual bandit (once 1000+ resolved)
├── Step 5b: Drift detection (once 4+ weeks of baseline)
└── Step 5c: Counterfactual dataset (once price_history covers most alerts)
```

### Time Estimate Honesty

| Step | Claimed | Realistic | Why |
|------|---------|-----------|-----|
| Fix result tracking | 5 min | **30 min** | Need to verify backfill doesn't break active follows; test on staging first |
| Schema expansion + backfill | Not estimated | **2h** | 50+ ALTER TABLEs, JSON parsing 59K rows, verifying data integrity |
| Alert prioritization MVP | 1h | **1h** (accurate) | Flat weighted score from existing columns, no training needed |
| Cost-adjusted P&L | 30 min | **1h** | Need to re-derive historical spread data, not just current |
| Position sizing | 30 min | **30 min** (accurate) | Pure config, no data dependencies |
| Bayesian feature weights | Part of 1h step | **2h + 1 week data gate** | Weights need 500+ resolves to converge meaningfully |
| Contextual bandit | 1h | **4h + 2 week data gate** | Need 1000+ resolves across multiple action types |
| Drift detection | Part of 1h step | **1h + 4 week baseline** | Need 4+ weeks of data to establish normal variance |
| Counterfactual backfill | Part of 1h step | **3h + 3 week data gate** | Only 0.7% of alerts have price_1h — need weeks of backfill |

**Total to MVP (ranked feed with flat weights): ~3.5h this week**
**Total to auto-learning working: 28-40h across 5+ weeks**

---

## Practical Auto-Learning: What You'd See (Realistic Timeline)

*All timelines assume the system starts collecting data this week.*

**Week 1:** Fix result tracking. Extract payload fields into queryable columns. Basic ranked feed goes live with flat, uncalibrated weights. 
- You can see: "Alert #1 has bigger flow than Alert #2"
- You CANNOT see: "Alert #1 has 65% probability of being right" (N=32 is not enough)

**Week 3:** After 200+ resolved CRITICAL alerts, the Bayesian weights start converging. Wallet reputation emerges as predictive. System can say: "wallets >60% WR have 68% precision; wallets <40% WR have 42%."

**Week 5:** After 500+ resolved alerts, the bandit has enough data. System notices that follow/fade/skip performs differently per alert type. Recommendations become actionable.

**Week 8+:** Drift detection has enough baseline (4+ weeks) to distinguish normal variance from true drift. First drift alarm may fire. If it's a false alarm, the bound widens. If it's real, weights auto-adjust.

**Key constraint at every stage:** The system is only as good as its data. No amount of Bayesian math compensates for N=32. The plan is designed to produce *conservative* recommendations early and *confident* ones later — never the reverse.

---

## What We're NOT Building (YAGNI)

| Technique | Why Not | Source |
|-----------|---------|--------|
| Deep neural networks | Need 10K+ samples, not interpretable, overkill for 200 alerts/week | Duran-Martin (2025) confirms Bayesian filters match DNN performance at fraction of complexity for sequential data |
| Full reinforcement learning (multi-step) | Contextual bandit is sufficient — each alert is one decision | Agarwal off-policy survey: bandit is the correct framework for one-shot decision problems; multi-step RL adds complexity without benefit when actions don't compound |
| Wasserstein distance drift detection | Moving window precision is simpler and works for our scale | KDD 2025 proactive drift adaptation paper uses Wasserstein for high-dim feature spaces; our 5-feature space doesn't need it |
| Bayesian neural networks | Beta-Binomial conjugate priors are sufficient and trivially implementable in SQL | arXiv 2505.07267 demonstrates BNNs for high-dim spaces; our 5-parameter model fits closed-form conjugate updates |
| Online gradient descent | Feature weights can update via simple exponential moving average | Apple Adaptive Weight Decay shows EMA matches gradient methods for weight adaptation at a fraction of compute

**The simplest approach that could possibly work:** Beta-Binomial conjugate priors for win rates, exponential moving averages for feature weights, and moving window precision for drift detection. All implementable in SQL + 100 lines of Python. No ML libraries needed.

---

## Success Criteria (Gate-Checked)

After Week 1 (Step 1 + Step 0 + Step 0b):
- [ ] Accurate win/loss results for all whale_follows
- [ ] Win rate known by archetype, exit reason, platform (but treat as provisional — <1 week of data)
- [ ] flow_dollars, top_wallet, spread_bps extracted into queryable columns
- [ ] market_archetype classifier built and applied to all 59K alerts
- [ ] Basic ranked feed showing top 10 alerts by flat weighted score

**Gate for Week 2-3:** 7+ days of continuous data, 200+ resolved CRITICAL alerts

After Week 4 (Cost P&L + Position Sizing):
- [ ] All P&L numbers adjusted for spread costs
- [ ] Historical follower P&L re-computed with real costs
- [ ] Kelly sizing, max daily loss, correlation limits enforced
- [ ] Signal generator never recommends >5 positions at once

**Gate for Week 5+:** 500+ resolved CRITICAL alerts, consistent precision band established

After Week 5+ (Auto-Learning):
- [ ] Bayesian feature weights updating from resolved alerts
- [ ] Thompson sampling choosing follow/fade/skip per alert
- [ ] Drift detection with 4+ week baseline established
- [ ] Counterfactual dataset covering >50% of alerts
- [ ] **Guardrail:** Max/min bounds on every auto-adjusted weight (±3σ from initial) — prevents convergence to a bad local optimum

## Guardrails: Preventing Bad Auto-Learning

The system rewrites its own weights. That's powerful and dangerous. These guardrails prevent it from converging to bad strategies:

| Guardrail | What It Prevents | How |
|-----------|------------------|-----|
| **Weight bounds** | Any single feature dominating the score | Each feature weight clamped to [0.1, 3.0] — can't go to zero or infinity |
| **Min sample size** | Overfitting on tiny samples | Bayesian weights don't update until 50+ resolved alerts exist for that feature slice |
| **Rollback checkpoint** | A bad week ruining months of tuning | Feature weights snapshotted daily; auto-rollback if weekly precision drops >15% below rolling average |
| **Drift confirmation** | Acting on one bad day | Drift must persist 3+ consecutive checks (3 weeks) before auto-adjusting; single-week dips are monitored but don't trigger changes |
| **Human override** | System making bad autonomous decisions | Email alert on every auto-adjustment; you approve or reject within 24h or it rolls back |

## Failure Mode Analysis

| Failure Mode | Likelihood | Impact | Mitigation |
|-------------|-----------|--------|------------|
| N=32 CRITICAL precision was a fluke | **Medium** — 32 is small for 65.6% | Precision actually 50% — strategy is coinflip | Don't act on precision until N=200+; use Bayesian credible intervals |
| System trained on one afternoon of markets | **High** — all 411 resolves are June 11 35-min window | Weights converge to that afternoon's pattern, not general truth | Explicit data gate: 7+ days before Bayesian updates begin |
| Counterfactual P&L uncomputable for most alerts | **Certain** — only 0.7% have price_1h | Can't backtest strategy changes for 3+ weeks | Accept limited counterfactual dataset; prioritize price backfill in Step 0 |
| Auto-adjusted weights converge to bad strategy | **Low** — bounds prevent extreme values | Suboptimal but not catastrophic | Guardrails + human override ensure quick recovery |
| Whale behavior changes (new exchange, new regulation) | **Medium** — prediction markets are evolving | Old model becomes inaccurate | Drift detection auto-flags; 3-consecutive-check confirmation prevents false alarms |
| Kalshi/Polymarket API changes | **Low** — stable APIs | Scanner stops producing alerts | Source health monitoring already in place; 24h alert on failures

---

## The Key Insight

**You don't need a PhD in ML to make this work.** The right approach for our scale (200 alerts/week, 411 resolved) is:

1. **Beta-Binomial priors** for win rates (updates in SQL, interpretable)
2. **Exponential moving averages** for feature weights (resistant to noise)
3. **Moving window precision** for drift detection (simple, catches regime shifts)
4. **Counterfactual logging** for strategy backtesting (build it once, use it forever)

This is production-grade auto-learning without the complexity. Start with Step 1.
---

## Session Log — June 13, 2026

### Shipped This Session

| Step | Task | Status |
|------|------|--------|
| Step 1 | Fix result tracking (backfill 238 empty results) | ✅ Done |
| Step 0 | Schema expansion: +19 columns to whale_outcomes | ✅ Done |
| Step 0 | Backfill 60,512 outcomes from payload JSON | ✅ Done |
| Step 0b | `/api/whale/top` ranked feed (composite score) | ✅ Live |
| Step 0b | `/api/whale/precision` endpoint | ✅ Live |
| Step 3 | `ev_net` (cost-adjusted EV) in ranked feed | ✅ Done |
| Infra | Archetype weights calibrated from 411 real resolved alerts | ✅ Done |
| Infra | PM flow_yes/flow_no fix (was hardcoded total flow) | ✅ Done |
| Infra | BACKFILL_CAP 300→500, PM slug cap 80→150 | ✅ Done |
| Alerts | Daily ranked digest Telegram (8am ET + 4pm ET) | ✅ Live |
| Alerts | `whale_alert_tg.py` with wallet WR filter + dedup | ✅ Live |

### Current Calibration (from 411 resolved Kalshi alerts)

| Archetype | N Resolved | Precision | Weight |
|-----------|-----------|-----------|--------|
| Weather | 99 | **72.7%** | 1.0 |
| Other | 130 | 53.8% | 0.5 |
| Index | 42 | 50.0% | 0.3 |
| Sports | 140 | **35.7%** | 0.15 (penalty — below random) |
| CRITICAL severity | 32 | 65.6% | (guard: N too small) |

Sports archetype was initially weighted 0.7 (bonus). Real data showed 35.7% = below random. Corrected to 0.15.

### Known Limitations

- **PM direction**: Only set for ~5% of PM alerts (those with `flow_desc` where one side has ≥67% of flow). `flow_yes`/`flow_no` was hardcoded as total flow — fixed in scanner (f56ee40).
- **PM resolution**: Resolves naturally as games/events settle. CLV and direction backfill runs hourly.
- **EV calculation**: Direction-aware since 2026-06-14 (Fix 2, commit 644b091). NO bets use `1-price` as entry. PM alerts with `direction=None` still fall back to YES price assumption.
- **Auto-tuning**: Bayesian weight updates blocked until 200+ resolved CRITICAL alerts. Growing steadily.
- **wallet_n coverage**: Only ~4% of alerts have pm_wallets entries. wallet_n=None alerts bypass WR gate (treated unknown). Coverage grows as pm_wallets is refreshed.

### QA Audit (2026-06-14)
Full /qa pass run against June 13 implementation. 4 bugs found and fixed, all verified. See `05-Decisions/2026-06-14-QA-Whale-Autolearning.md`.

Commits: 644b091 (fixes 1-4), 54f4f3b (Fix 3 proper — wallet_n schema).

### Next Steps

- [ ] Add position sizing: Kelly fraction, max daily loss $250, max 5 concurrent positions
- [ ] Bayesian weight updates once 200 CRITICAL resolved
- [ ] CLOB historical price backfill (conditionId batch API, limited by rate limits)
- [ ] `fade_score` for known bad wallets (wr=0.0, n≥20 wallets are strong fade signals)

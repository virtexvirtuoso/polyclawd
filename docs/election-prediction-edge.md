# Election Prediction Edge

> **Status:** Live — modules deployed Feb 26, 2026
> **Correlation Group:** `geopolitical` (separate from politics, max 3 slots)

## Core Thesis

Prediction markets systematically overprice challengers in elections because narratives are exciting. Incumbents win **~70% globally**. We fade the crowd with NO bets on challenger-overpriced markets.

## 3-Layer Edge Stack

### Layer 1: Incumbency Bias (Structural)
- Incumbents have institutional advantages (media, funding, state apparatus)
- Markets price "change" narratives at a premium
- Our NO thesis exploits this persistent mispricing
- Special case: authoritarian regimes (Venezuela) — markets price in "hope" for democratic transition that almost never happens

### Layer 2: Wikipedia Polling Divergence
**Module:** `signals/election_polls.py`

Scrapes Wikipedia opinion polling tables and compares to Polymarket prices:

| Condition | Multiplier |
|---|---|
| Polls agree with NO thesis | 1.2x boost |
| Polls disagree with NO thesis | 0.7x dampen |
| No polls available | 1.0x neutral |

**Recency weighting:**
- Last 30 days: 1.0x (full weight)
- 30-90 days: 0.7x
- >90 days: 0.4x (stale)

**Key insight:** Independent polls vs government-aligned polls diverge in authoritarian-leaning countries. Weight independent pollsters higher.

### Layer 3: Cross-Platform Arbitrage
**Module:** `signals/cross_platform_elections.py`

Compares Polymarket vs Manifold Markets prices:

| Divergence | Multiplier | Interpretation |
|---|---|---|
| >10% | 1.3x boost | One platform is wrong — high edge |
| 5-10% | 1.15x boost | Moderate disagreement |
| <5% | 1.0x neutral | Consensus — no extra edge |

## Live Markets (Feb 2026)

### 🇭🇺 Hungary — April 12, 2026
- **Bet:** NO on "Orbán loses" (i.e., Orbán stays PM)
- **Edge:** 36% (~$338 position)
- **Polymarket:** Orbán ~60% to win
- **Manifold:** Orbán 38% to win → **22% cross-platform divergence** (massive)
- **Polls:** Split — TISZA (Magyar) leads in independent polls, Orbán leads in govt-aligned polls
- **Incumbency:** Orbán has been PM since 2010, controls state media
- **Risk:** Genuine opposition momentum (Magyar is a real challenger, not controlled opposition)

### 🇻🇪 Venezuela
- **Bet:** NO on opposition winning
- **Edge:** 26% (~$219 position)
- **Incumbency:** Maduro controls CNE (electoral council), military, judiciary
- **Key:** Markets price in democratic hope. History: Maduro survived 2019 Guaidó challenge, 2024 election fraud
- **Risk:** International pressure, but Maduro has weathered worse

### 🇧🇷 Brazil — October 4, 2026
- **Status:** Too far out, monitoring only
- **Polls:** Lula at 47.1% (AtlasIntel Feb 2026)
- **Incumbency:** Lula is incumbent
- **Action:** Re-evaluate at 6 months out (April 2026)

## Pipeline Integration

Election signals flow through the standard pipeline with election-specific multipliers applied:

```
Signal → Confidence → Edge → Archetype Check → NO Prob Floor
    → Kelly Sizing → Archetype Boost
    → [Election Polling Multiplier]      ← NEW
    → [Cross-Platform Multiplier]        ← NEW
    → Time Decay → Volume Spike → Momentum → Correlation Cap
```

Elections are in the `geopolitical` correlation group (separate from `politics`), allowing up to 3 independent election positions without competing with domestic political markets.

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /api/signals/election-polls` | Wikipedia polling data for tracked elections |
| `GET /api/signals/cross-platform` | Manifold vs Polymarket divergence |

## When to NOT Bet Elections

1. **>6 months out** — Too much uncertainty, polls shift
2. **Genuine competitive democracy** (e.g., US, UK) — Incumbency edge is weaker (~55%, not 70%)
3. **Polls unanimously against incumbent** — Don't fight consensus
4. **Cross-platform agreement on challenger** — Both Poly + Manifold say challenger wins? Step aside.
5. **NO implied prob <35%** — Market too efficient, floor blocks it

## Calibration Targets

After 10 resolved election bets:
- Target WR: >65% (incumbency baseline 70%, minus friction)
- Target ROI: >15% per position
- If WR <55% after 10: re-evaluate incumbency thesis for modern elections

## Files

| File | Purpose |
|---|---|
| `signals/election_polls.py` | Wikipedia polling scraper + confidence multipliers |
| `signals/cross_platform_elections.py` | Manifold comparison + divergence signals |
| `docs/election-prediction-edge.md` | This document |
| `docs/strategy-price-to-strike.md` | Strategy 2 (separate) |

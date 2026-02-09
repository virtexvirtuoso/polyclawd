# Confidence Scoring System

Polyclawd uses a sophisticated Bayesian confidence scoring system that learns from outcomes and adjusts signal weights based on historical performance.

**v2.0 Update (2026-02-08):** Added Shin method for edge calculation, Laplace smoothing, disagreement penalties, and combined decision scoring.

---

## Overview

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ Base Signal  │ →  │   Bayesian   │ →  │  Agreement   │ →  │    Final     │
│  Confidence  │    │  Multiplier  │    │   Penalty    │    │  Confidence  │
└──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
     (0-100)         (smoothed WR)      (0.85 - 1.30)       (0-100)
```

---

## Edge Calculation: Shin Method

### Why Shin?

Traditional vig removal assumes bookmakers distribute their margin evenly across outcomes. This works for balanced lines but fails for heavy favorites.

The **Shin method** (Shin 1991, 1992, 1993) models bookmaker behavior more accurately by accounting for informed bettors (insiders). It produces better true probability estimates for unbalanced lines.

### When to Use Shin

| Implied Probability | Method | Reason |
|---------------------|--------|--------|
| < 75% | Basic no-vig | Lines are balanced enough |
| ≥ 75% | Shin method | Heavy favorite creates asymmetric vig |

### Formula

```python
def shin_no_vig(p_fav: float, p_dog: float) -> Tuple[float, float]:
    """
    Z = total implied probability (overround)
    s = Shin factor (fraction of informed bettors)
    
    Solve for s, then:
    true_prob = (implied_prob - s) / (1 - 2*s)
    """
    Z = p_fav + p_dog
    
    # Shin factor calculation
    discriminant = p_fav**2 + p_dog**2 - (Z**2 - 2*Z + 2)
    s = (Z - sqrt(discriminant)) / (2*Z - 2)
    s = clamp(s, 0, 0.5)
    
    # Calculate true probabilities
    true_fav = (p_fav - s) / (1 - 2*s)
    true_dog = (p_dog - s) / (1 - 2*s)
    
    return normalize(true_fav, true_dog)
```

### Example: Heavy Favorite

```
Line: -350 / +280
Implied: 77.8% / 26.3% (Total: 104.1%, Vig: 4.1%)

Basic no-vig:  74.7% / 25.3%
Shin method:   75.2% / 24.8%

Difference: 0.5% - significant for edge calculation!
```

---

## Sharp Book Prioritization

### Book Tiers

**Sharp Books (Priority 1):**
- Pinnacle / Pinnacle Sports
- Circa
- BetCRIS
- Bookmaker

*Low vig (~2-3%), efficient markets, attract professional bettors*

**Soft Books (Priority 2):**
- DraftKings
- FanDuel
- BetMGM
- Caesars
- PointsBet

*Higher vig (~5-8%), recreational markets*

### Consensus Calculation

```python
def get_consensus_true_prob(bookmaker_odds, outcome):
    sharp_probs = []
    soft_probs = []
    
    for book in bookmaker_odds:
        true_prob = calculate_true_prob(book, method="shin_if_heavy")
        
        if book in SHARP_BOOKS:
            sharp_probs.append(true_prob)
        else:
            soft_probs.append(true_prob)
    
    # Prioritize sharp book consensus
    if sharp_probs:
        return average(sharp_probs)
    return average(soft_probs)
```

---

## Laplace Smoothing

### The Problem

With small sample sizes, win rates are unreliable:
- 1 win / 1 total = 100% win rate (overconfident!)
- 0 wins / 3 total = 0% win rate (too harsh!)

### The Solution: Laplace Smoothing

Add "pseudo-observations" that pull toward 50%:

```python
def laplace_smoothed_win_rate(wins: int, total: int, alpha: float = 4.0) -> float:
    """
    alpha=4 adds 4 wins + 4 losses worth of prior belief toward 50%
    """
    return (wins + alpha) / (total + 2 * alpha)
```

### Examples

| Actual Wins | Actual Total | Raw Win Rate | Smoothed (α=4) |
|-------------|--------------|--------------|----------------|
| 0 | 0 | undefined | 50.0% |
| 1 | 1 | 100.0% | 62.5% |
| 0 | 3 | 0.0% | 36.4% |
| 5 | 5 | 100.0% | 69.2% |
| 10 | 10 | 100.0% | 77.8% |
| 50 | 100 | 50.0% | 50.0% |
| 70 | 100 | 70.0% | 68.5% |

As samples grow, smoothing has less effect and raw rate dominates.

---

## Bayesian Confidence v2

### Improvements Over v1

1. **Laplace smoothing** - prevents overfitting on small samples
2. **Weighted average** - sources weighted by their win rate
3. **Disagreement penalty** - reduces confidence when sources conflict
4. **Capped multipliers** - prevents runaway confidence (max 1.8x)

### Formula

```python
def calculate_bayesian_confidence_v2(
    raw_scores: dict,      # {source: base_confidence}
    source_stats: dict,    # {source: {wins, total, direction}}
    alpha: float = 4.0,
    max_multiplier: float = 1.8
) -> dict:
    
    bayesian_confs = {}
    smoothed_wrs = {}
    directions = {}
    
    for source, base in raw_scores.items():
        wins, total = source_stats[source]["wins"], source_stats[source]["total"]
        
        # 1. Laplace smoothed win rate
        smoothed_wr = (wins + alpha) / (total + 2 * alpha)
        
        # 2. Capped multiplier
        multiplier = min(smoothed_wr / 0.5, max_multiplier)
        
        # 3. Calculate Bayesian confidence
        bayesian_confs[source] = base * multiplier
        directions[source] = source_stats[source]["direction"]
    
    # 4. Weighted average (weight = smoothed win rate)
    weighted_conf = sum(bayesian_confs[s] * smoothed_wrs[s] 
                        for s in bayesian_confs) / sum(smoothed_wrs.values())
    
    # 5. Agreement/disagreement multiplier
    has_disagreement = len(set(directions.values())) > 1
    
    if has_disagreement:
        agreement_mult = 0.85   # 15% penalty
    elif len(raw_scores) >= 3:
        agreement_mult = 1.30   # 30% boost
    elif len(raw_scores) == 2:
        agreement_mult = 1.15   # 15% boost
    else:
        agreement_mult = 1.0
    
    final_conf = min(100, weighted_conf * agreement_mult)
    
    return {"final_confidence": final_conf, ...}
```

### Agreement Multipliers

| Condition | Multiplier | Effect |
|-----------|------------|--------|
| Sources disagree (YES vs NO) | 0.85 | -15% penalty |
| Single source | 1.00 | No change |
| 2 sources agree | 1.15 | +15% boost |
| 3+ sources agree | 1.30 | +30% boost |

---

## Combined Decision Rule

### The Problem

A 10% edge with 20% confidence shouldn't trigger a bet.
A 2% edge with 90% confidence also shouldn't trigger a bet.

### The Solution: Adjusted Edge

```python
adjusted_edge = |edge_pct| × (confidence / 100)
```

This ensures we need **BOTH** a meaningful edge AND high confidence.

### Decision Thresholds

| Adjusted Edge | Strength | Action | Size Multiplier |
|---------------|----------|--------|-----------------|
| > 5.0% | STRONG | BET | 1.0x (full) |
| > 3.0% | MODERATE | BET | 0.5x (half) |
| ≤ 3.0% | WEAK | SKIP | 0.25x (quarter) |

### Examples

```python
# Example 1: High edge, low confidence
edge = 8.0%, confidence = 30
adjusted = 8.0 × 0.30 = 2.4% → WEAK, SKIP

# Example 2: Low edge, high confidence
edge = 2.5%, confidence = 85
adjusted = 2.5 × 0.85 = 2.1% → WEAK, SKIP

# Example 3: Good edge, good confidence
edge = 5.0%, confidence = 70
adjusted = 5.0 × 0.70 = 3.5% → MODERATE, BET at 0.5x

# Example 4: Strong signal
edge = 7.0%, confidence = 80
adjusted = 7.0 × 0.80 = 5.6% → STRONG, BET at 1.0x
```

---

## Edge Filters

### Time Decay

Markets far from resolution have more uncertainty, requiring higher edges:

| Time to Resolution | Edge Multiplier | Min Edge (base 2%) |
|--------------------|-----------------|-------------------|
| < 24 hours | 1.0x | 2.0% |
| 1-3 days | 1.2x | 2.4% |
| 3-7 days | 1.3x | 2.6% |
| > 1 week | 1.5x | 3.0% |

### Volume Filter

Minimum $100,000 market volume prevents:
- Illiquid markets with unreliable prices
- Markets where slippage destroys edge
- Potential manipulation on thin books

### Combined Filters

```python
@dataclass
class EdgeFilter:
    min_edge_pct: float = 2.0       # Base minimum edge
    min_volume: float = 100000      # $100k minimum
    min_confidence: float = 40      # Minimum confidence
    min_adjusted_edge: float = 3.0  # Adjusted edge threshold
    edge_time_decay: bool = True    # Apply time decay

def apply_edge_filters(edge_pct, confidence, volume, hours_to_resolution):
    # All conditions must pass
    passes = (
        abs(edge_pct) >= min_edge_required and
        confidence >= min_confidence and
        volume >= min_volume and
        adjusted_edge >= min_adjusted_edge
    )
    return passes
```

---

## Kelly Criterion

### Basic Formula

```python
kelly = edge / (1 - market_price)  # For YES bets
kelly = edge / market_price        # For NO bets
```

### Fractional Kelly

Full Kelly is too aggressive. We use fractional Kelly:

| Kelly Fraction | Risk Level | When to Use |
|----------------|------------|-------------|
| Full (1.0x) | Aggressive | Never (in practice) |
| Half (0.5x) | Moderate | Strong signals |
| Quarter (0.25x) | Conservative | Moderate signals |

### With Combined Decision

```python
def position_size(balance, edge_pct, confidence, market_price):
    # 1. Calculate raw Kelly
    kelly_raw = abs(edge_pct / 100) / (1 - market_price)
    
    # 2. Get decision score
    decision = combined_decision_score(edge_pct, confidence)
    
    # 3. Scale Kelly by signal strength
    kelly_adjusted = kelly_raw * decision["size_multiplier"]
    
    # 4. Apply phase limits
    kelly_clamped = clamp(kelly_adjusted, phase.kelly_min, phase.kelly_max)
    
    return balance * kelly_clamped
```

---

## API Endpoints

### Calculate Edge

**POST** `/api/edge/calculate`

```json
{
    "bookmaker_odds": [
        {"book": "pinnacle", "yes_odds": -250, "no_odds": 200},
        {"book": "draftkings", "yes_odds": -280, "no_odds": 220}
    ],
    "market_price": 0.72,
    "outcome": "yes",
    "confidence": 65,
    "volume": 250000,
    "hours_to_resolution": 48
}
```

Response:
```json
{
    "true_probability": {
        "value": 71.4,
        "source": "sharp_consensus",
        "sharp_books_used": ["pinnacle"]
    },
    "edge": {
        "true_prob": 71.4,
        "market_price": 72.0,
        "edge_pct": -0.6,
        "edge_direction": "NO",
        "kelly_full": 2.14,
        "kelly_half": 1.07
    },
    "filters": {
        "passes_filter": false,
        "min_edge_required": 2.4,
        "adjusted_edge": 0.39,
        "reasons": ["Edge 0.6% < 2.4% minimum"]
    },
    "decision": {
        "adjusted_edge": 0.39,
        "should_bet": false,
        "bet_direction": "NO",
        "strength": "weak"
    }
}
```

### Get Example

**GET** `/api/edge/calculate/example`

Returns a worked example comparing basic vs Shin vig removal.

### Source Statistics

**GET** `/api/confidence/sources`

Returns win rates and Bayesian multipliers for all signal sources.

---

## Recording Outcomes

### Automatic Recording

The trading engine records outcomes when positions resolve:

```python
record_outcome(source="inverse_whale", won=True, market_title="Will X happen?")
```

### Manual Recording

**POST** `/api/confidence/record`

```bash
curl -X POST "/api/confidence/record?source=smart_money&won=true"
```

---

## Best Practices

1. **Use Shin for heavy favorites**: Lines with >75% implied need Shin method
2. **Trust sharp books**: Pinnacle > DraftKings for true probability
3. **Wait for samples**: Need 20+ outcomes before trusting source win rates
4. **Apply both thresholds**: Edge AND confidence must be sufficient
5. **Time decay matters**: Require higher edges for distant resolution
6. **Fractional Kelly**: Never use full Kelly in practice
7. **Track disagreements**: Source conflicts reveal which to trust

---

## Changelog

### v2.0 (2026-02-08)
- Added Shin method for unbalanced lines
- Sharp book prioritization (Pinnacle, Circa, etc.)
- Laplace smoothing with α=4 default
- Disagreement penalty (0.85x when sources conflict)
- Combined decision scoring (adjusted_edge = |edge| × conf/100)
- Time decay for edge thresholds
- New `/api/edge/calculate` endpoint

### v1.0 (Initial)
- Basic Bayesian multiplier (win_rate / 0.5)
- Simple agreement boost (+10% per source)
- Basic vig removal only

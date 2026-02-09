# Edge & Confidence Quick Reference

## Decision Rule

```
adjusted_edge = |edge%| × (confidence / 100)

BET if adjusted_edge > 3.0%
```

## Formulas

| Metric | Formula |
|--------|---------|
| Implied Prob | `-odds / (-odds + 100)` or `100 / (odds + 100)` |
| Basic No-Vig | `p / (p_yes + p_no)` |
| Shin No-Vig | `(p - s) / (1 - 2s)` where s = Shin factor |
| Laplace WR | `(wins + 4) / (total + 8)` |
| Bayesian Mult | `min(smoothed_wr / 0.5, 1.8)` |
| Kelly | `edge / (1 - price)` for YES, `edge / price` for NO |

## Thresholds

| Check | Threshold |
|-------|-----------|
| Min raw edge | 2.0% (3.0% if >1 week out) |
| Min confidence | 40 |
| Min volume | $100,000 |
| Min adjusted edge | 3.0% |

## Agreement Multipliers

| Condition | Multiplier |
|-----------|------------|
| Sources disagree | 0.85x |
| 1 source | 1.0x |
| 2 sources agree | 1.15x |
| 3+ sources agree | 1.30x |

## Sharp Books (Use First)

Pinnacle, Circa, BetCRIS, Bookmaker

## When to Use Shin

Implied probability > 75% (heavy favorite like -300)

## Position Sizing

| Strength | Adjusted Edge | Kelly Fraction |
|----------|---------------|----------------|
| Strong | > 5% | 0.5x (half) |
| Moderate | > 3% | 0.25x (quarter) |
| Weak | ≤ 3% | Skip |

## API

```bash
# Calculate edge
curl -X POST /api/edge/calculate \
  -d '{"bookmaker_odds":[{"book":"pinnacle","yes_odds":-250,"no_odds":200}],"market_price":0.72}'

# Get example
curl /api/edge/calculate/example

# List sharp books
curl /api/edge/sharp-books
```

# Kalshi Category Calibration Screen

- Generated: 2026-07-10T02:06:14Z
- Settled markets pulled: 18714
- Calibratable (valid pre-settlement reference): 693
- Settlement date range: ['2026-04-30', '2026-07-10']
- Reference: candlestick ~18h before close, excluding last 6h (traded price preferred; tight-spread midpoint fallback)

## Caveats

- SCREEN ONLY — no holdout, no PnL claim. Candidates need a dedicated pre-registered audit before capital.
- Reference-price choice: a pre-settlement candlestick snapshot. Final `last_price_dollars` was rejected (convergence-degenerate: collapses to 0/1).
- Survivorship: only *settled* markets in-window; voided markets and never-traded illiquid brackets are dropped (drops calibratable N).
- Fees: Kalshi charges ~0.07*p*(1-p) per contract; a gap must clear this (~1.75c at p=0.5, ~0.9c at p=0.85) to be tradeable.
- Per-market independence is FALSE for same-event brackets; flags require the gap's 95% CI to exclude 0 under BOTH event- and series-clustered bootstraps.

## Economics — calibratable n=226

| bin | n | events | series | implied | realized | gap | survives |
|---|---|---|---|---|---|---|---|
| [0.00,0.05) | 58 | 22 | 15 | 0.019 | 0.017 | -0.002 |  |
| [0.05,0.15) | 31 | 18 | 13 | 0.089 | 0.097 | +0.008 |  |
| [0.15,0.30) | 20 | 18 | 10 | 0.200 | 0.050 | -0.150 | YES |
| [0.30,0.50) | 13 | 12 | 7 | 0.407 | 0.538 | +0.131 |  |
| [0.50,0.70) | 10 | 7 | 6 | 0.591 | 0.800 | +0.209 |  |
| [0.70,0.85) | 11 | 9 | 6 | 0.774 | 0.909 | +0.136 |  |
| [0.85,0.95) | 22 | 15 | 10 | 0.913 | 1.000 | +0.087 | YES |
| [0.95,1.00) | 61 | 20 | 14 | 0.983 | 1.000 | +0.017 |  |

## Financials — calibratable n=207

| bin | n | events | series | implied | realized | gap | survives |
|---|---|---|---|---|---|---|---|
| [0.00,0.05) | 58 | 24 | 16 | 0.014 | 0.000 | -0.014 |  |
| [0.05,0.15) | 8 | 8 | 8 | 0.089 | 0.000 | -0.089 |  |
| [0.15,0.30) | 2 | 2 | 1 | 0.200 | 0.000 | -0.200 |  |
| [0.30,0.50) | 6 | 5 | 4 | 0.418 | 0.333 | -0.085 |  |
| [0.50,0.70) | 8 | 7 | 6 | 0.559 | 0.500 | -0.059 |  |
| [0.70,0.85) | 7 | 6 | 6 | 0.780 | 1.000 | +0.220 |  |
| [0.85,0.95) | 16 | 8 | 8 | 0.910 | 1.000 | +0.090 |  |
| [0.95,1.00) | 102 | 17 | 14 | 0.982 | 1.000 | +0.018 |  |

## Crypto — n=3 (below reporting threshold)

## Politics — n=12 (below reporting threshold)

## Sports — calibratable n=245

| bin | n | events | series | implied | realized | gap | survives |
|---|---|---|---|---|---|---|---|
| [0.00,0.05) | 43 | 7 | 6 | 0.011 | 0.023 | +0.012 |  |
| [0.05,0.15) | 52 | 33 | 7 | 0.089 | 0.173 | +0.084 | YES |
| [0.15,0.30) | 30 | 26 | 4 | 0.223 | 0.367 | +0.144 |  |
| [0.30,0.50) | 41 | 36 | 6 | 0.416 | 0.390 | -0.025 |  |
| [0.50,0.70) | 54 | 52 | 4 | 0.586 | 0.518 | -0.067 |  |
| [0.70,0.85) | 20 | 19 | 4 | 0.776 | 0.650 | -0.126 |  |
| [0.85,0.95) | 4 | 4 | 3 | 0.870 | 1.000 | +0.130 |  |
| [0.95,1.00) | 1 | 1 | 1 | 0.950 | 0.000 | -0.950 |  |

## Flagged candidates

| category | bin | n | implied | realized | gap | CI(event) | CI(series) |
|---|---|---|---|---|---|---|---|
| Economics | [0.15,0.30) | 20 | 0.200 | 0.050 | -0.150 | [-0.2076, -0.0475] | [-0.2086, -0.035] |
| Economics | [0.85,0.95) | 22 | 0.913 | 1.000 | +0.087 | [0.0807, 0.0952] | [0.0807, 0.0941] |
| Sports | [0.05,0.15) | 52 | 0.089 | 0.173 | +0.084 | [0.0018, 0.1658] | [0.0062, 0.1383] |
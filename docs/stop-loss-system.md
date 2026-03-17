# Polymarket Stop-Loss / Early Exit System

**Status:** Phase 0 + Phase 1 LIVE ✅  
**Priority:** High  
**Created:** 2026-03-17  
**Deployed:** 2026-03-17  

## Problem

All prediction market trades are binary: win pays out, loss = 100% of stake. Zero mid-trade risk management. A 55% win rate can still bleed capital if every loss is a full wipeout.

## Solution

Monitor live Polymarket prices on open positions and trigger exits before full loss. Conservative fixed stops now, adaptive learned stops later.

---

## What's Deployed

### Phase 0 — Price Logging ✅

**File:** `services/price_logger.py`  
**Table:** `position_price_log`  
**Schedule:** Every 5min via `tick_5min()` in `services/scheduler.py`

Logs current YES token price for every open position. Pure data collection for learning optimal exit curves later.

```sql
CREATE TABLE position_price_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    market_price REAL,
    edge_current REAL,
    FOREIGN KEY (position_id) REFERENCES paper_positions(id)
);
CREATE INDEX idx_ppl_position_ts ON position_price_log(position_id, timestamp);
```

- Fetches prices from Polymarket CLOB API / Kalshi API in parallel (8 workers)
- `edge_current` is nullable — reserved for Phase 2 when we re-estimate edge at each snapshot

### Phase 1 — Conservative Stop-Loss ✅

**File:** `services/stop_evaluator.py`  
**Schedule:** Every 5min via `tick_5min()` in `services/scheduler.py` (runs before price_logger)

#### Stop Criteria

| Check | Threshold | Logic |
|-------|-----------|-------|
| **Max Loss %** | 50% of bet size | If unrealized loss exceeds 50% of the original bet, exit immediately |
| **Edge Erosion** | -2pp floor | Placeholder for Phase 2 — will re-run signal and exit if edge flips negative |

**Why 50%?** Intentionally wide/conservative. We'd rather miss some saves than kill winners with tight stops. Phase 2 will learn tighter per-strategy thresholds from actual price trajectory data.

#### How It Works

1. Fetch current YES token price for all open positions (parallel, 8 workers)
2. Compute unrealized P&L:
   - YES positions: `bet_size * (current_price / entry_price - 1)`
   - NO positions: `bet_size * ((1 - current_price) / (1 - entry_price) - 1)`
3. If `abs(unrealized_loss) / bet_size >= 0.50` → close position
4. Position status set to `stopped` (distinct from won/lost/void)
5. Discord alert sent via existing webhook

#### New Position Status: `stopped`

- Stored in `paper_positions.status`
- `close_reason` = `"stop-loss: loss X% >= 50% threshold"`
- Included in P&L calculations (`status IN ('won','lost','stopped')`)
- NOT included in win-rate calculations (only won/lost count)
- Does NOT block new position opening on same market (unique index = `WHERE status='open'`)

#### Discord Alerts

Uses existing `discord_alerts.py` webhook infrastructure. Alert includes:
- Side, entry → exit price
- Strategy
- Loss (stopped) vs loss (if held to resolution)
- Amount saved
- Portfolio context (bankroll, record)

Alert cooldown: 60 minutes per position to avoid spam.

### Re-Entry Gate ✅

**File:** `signals/paper_portfolio.py` (in `open_position()`)

If a market was previously stopped out, re-entry is **only allowed if the new edge is strictly greater than the original entry edge.**

```
prev_stopped = query last stopped position for this market_id
if prev_stopped:
    if new_edge <= prev_stopped.edge → BLOCKED
    if new_edge > prev_stopped.edge  → ALLOWED (data is stronger now)
```

**Rationale:** The market told us something by moving against us. Don't go back in on equal or weaker signal. Only re-enter if the data case is objectively stronger than before.

### Schema Changes ✅

```sql
-- Unique index updated: only blocks duplicate OPEN positions (was: != 'void')
DROP INDEX idx_one_position_per_market;
CREATE UNIQUE INDEX idx_one_position_per_market 
ON paper_positions(market_id) WHERE status = 'open';
```

```sql
-- P&L queries updated throughout paper_portfolio.py and discord_alerts.py
-- Old: status IN ('won','lost')
-- New: status IN ('won','lost','stopped')
```

### First Run Results (2026-03-17)

| Market | Side | Entry | Exit | Loss | Full Loss | Saved |
|--------|------|-------|------|------|-----------|-------|
| Paris 16°C Mar 17 | NO | 38% | 80% | -$67.74 | -$100.00 | **$32.26** |
| Musk 280-299 tweets | NO | 24% | 88% | -$100.94 | -$120.01 | **$19.07** |
| London 15°C Mar 17 | NO | 42% | 80% | -$66.09 | -$100.00 | **$33.91** |

**Total saved on first run: ~$85.24**

---

## What's Next

### Phase 2 — Adaptive Stops (target: ~2-3 weeks)

**Prerequisite:** 30+ resolved trades with full price trajectories in `position_price_log`.

**Approach:** No hardcoded thresholds. Learn optimal exit curves from our own data, per strategy. Same philosophy as inverse-RMSE calibration.

#### Recovery Probability Curves

After each trade resolves, backtest the price trajectory:
- "This trade dropped X% at hour Y and eventually lost — cutting would have saved $Z"
- "This trade dropped X% at hour Y but recovered and won — cutting would have killed a winner"

Build per-strategy recovery probability function:

```
recovery_prob(strategy, price_drop_pct, time_elapsed, edge_remaining) → probability
```

#### Adaptive Exit Rule

```
if recovery_probability < learned_threshold → EXIT
```

Where `learned_threshold` is optimized to maximize portfolio Sharpe ratio, not just win rate.

#### Edge Re-Estimation

- Re-run the signal pipeline at each price snapshot
- Populate `edge_current` in `position_price_log`
- Use edge trajectory (not just price) for exit decisions
- Signal reversal (edge flips negative) becomes a learned feature, not a hardcoded rule

### Phase 3 — Auto-Execution (future)

- Polymarket CLOB sell order integration
- Slippage estimation from orderbook depth before executing
- Execution confirmation + P&L logging

---

## File Inventory

| File | Location (VPS) | Purpose |
|------|---------------|---------|
| `services/price_logger.py` | `/var/www/virtuosocrypto.com/polyclawd/services/` | Price snapshot logging |
| `services/stop_evaluator.py` | `/var/www/virtuosocrypto.com/polyclawd/services/` | Stop-loss evaluation + execution |
| `services/scheduler.py` | `/var/www/virtuosocrypto.com/polyclawd/services/` | Orchestrator (tick_5min calls both) |
| `signals/paper_portfolio.py` | `/var/www/virtuosocrypto.com/polyclawd/signals/` | Re-entry gate in open_position() |
| `signals/discord_alerts.py` | `/var/www/virtuosocrypto.com/polyclawd/signals/` | Alert delivery (stopped included in lost count) |

## Risk Notes

- **Whipsaw:** Temporary dips can trigger stops on trades that would have won. 50% threshold is wide enough to avoid most false triggers. Phase 2 will learn the right threshold per strategy.
- **Slippage:** Paper trading assumes exit at current mid-price. Real execution would face spread + depth constraints.
- **Re-entry loops:** Gate prevents weak re-entries, but a market could theoretically get entered → stopped → re-entered at higher edge → stopped again. The 50% loss + stronger-edge requirement makes this unlikely and self-limiting.

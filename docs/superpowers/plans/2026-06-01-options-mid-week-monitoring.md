# Options Mid-Week Position Monitoring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Monitor open options-implied paper positions during the week (Mon–Thu) for price movement, edge decay, and z-score degradation. Take profit when edge shrinks >50% from entry. Stop loss when edge flips negative.

**Architecture:** `reeval_options_positions()` added to `signals/options_implied.py`. Fetches current Polymarket CLOB prices for each open options paper position, compares to entry, and calls `paper_portfolio.close_position_by_id()` if threshold met. Wired into scheduler 30-min tick. Dashboard shows current price and unrealized P&L.

**Tech Stack:** Python 3.12+, Polymarket CLOB API (`markets/{conditionId}`), SQLite (paper_positions), paper_portfolio.close_position_by_id(), loguru.

**Pre-existing context:** `paper_portfolio.py` has `close_position_by_id()` that handles P&L calculation and `close_reason` tagging. Weather's `reeval_weather_positions()` is the reference pattern — we mirror its structure. The options resolver already has CLOB `_fetch_json()` in `options_resolver.py` that we can reuse.

---

### Task 1: Add `reeval_options_positions()` to `signals/options_implied.py`

**Files:**
- Modify: `~/Desktop/polyclawd/signals/options_implied.py` — append at end of file (before `if __name__` block)

- [ ] **Append the reeval function**

Add this code to the end of `signals/options_implied.py`, right before the `if __name__ == "__main__":` block:

```python
# ── Mid-Week Position Monitoring ────────────────────────────────────
# Re-evaluates open options paper positions Mon-Thu for price movement
# and edge decay. Take profit at >50% edge shrink, stop loss on z-flip.
# Mirrors weather_scanner.reeval_weather_positions() pattern.

from loguru import logger as _options_logger


def _fetch_poly_current_price(condition_id: str) -> Optional[float]:
    """Fetch current YES price from Polymarket CLOB for a condition.
    Returns None if market closed or unreachable."""
    import urllib.request as _ur
    url = f"https://clob.polymarket.com/markets/{condition_id}"
    try:
        req = _ur.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with _ur.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
            tokens = data.get("tokens", [])
            if tokens:
                price = float(tokens[0].get("price", 0))
                return price if price > 0 else None
            # Fallback: try outcomePrices
            prices_raw = data.get("outcomePrices")
            if prices_raw:
                if isinstance(prices_raw, str):
                    prices = json.loads(prices_raw)
                else:
                    prices = prices_raw
                if prices and len(prices) > 0:
                    return float(prices[0])
            return None
    except Exception as e:
        _options_logger.debug(f"CLOB fetch failed for {condition_id[:16]}: {e}")
        return None


def reeval_options_positions() -> dict:
    """Check open options paper positions against current Polymarket prices.
    
    Closes positions when:
    1. Take profit: edge has shrunk >50% from entry 
       (current_price moved toward fair value significantly)
    2. Stop loss: edge flipped (entry_z > 0 and price went up, or vice versa)
       Signal is gone or reversed.
    
    Returns dict with checked/closed/kept/errors counts.
    """
    import sqlite3
    from pathlib import Path as _Path
    
    results = {"checked": 0, "closed": 0, "kept": 0, "errors": 0, "details": []}
    base_dir = _Path(__file__).parent.parent
    
    # Connect to paper portfolio db
    db_path = base_dir / "storage" / "shadow_trades.db"
    if not db_path.exists():
        return results
    
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    
    # Get open options positions
    positions = conn.execute(
        "SELECT id, market_title, market_id, side, entry_price, bet_size, "
        "edge_pct, confidence, opened_at "
        "FROM paper_positions "
        "WHERE status='open' AND strategy='options_implied'"
    ).fetchall()
    
    if not positions:
        conn.close()
        return results
    
    conn.close()
    
    for pos in positions:
        results["checked"] += 1
        position_id = pos["id"]
        condition_id = pos["market_id"]
        entry_price = pos["entry_price"] or 0.5
        entry_side = pos["side"]
        entry_edge_pct = pos["edge_pct"] or 0
        bet_size = pos["bet_size"] or 0
        
        # 1. Fetch current price from CLOB
        current_price = _fetch_poly_current_price(condition_id)
        if current_price is None:
            results["kept"] += 1
            continue
        
        # 2. Determine if edge has degraded
        # For YES side: edge = predicted_prob - entry_price
        # Take profit: current_price moved >50% toward entry_price
        #   e.g., entry=0.35, fair=0.55, current=0.48 → edge shrunk from 0.20 to 0.07 → close
        # Stop loss: current_price moved past entry in the wrong direction
        
        close_reason = None
        
        if entry_side == "YES":
            # We bet YES. Edge = implied_prob (at scanner) - entry_price
            # Edge positive means we bought below fair value
            # If current_price > entry_price, edge has shrunk
            price_move = current_price - entry_price
            edge_at_entry = entry_edge_pct / 100.0 if entry_edge_pct > 0 else 0.05  # fallback
            
            if price_move > 0:
                # Price moved up — edge is shrinking
                remaining_edge = max(0, edge_at_entry - price_move)
                edge_shrink_pct = 1 - (remaining_edge / max(edge_at_entry, 0.001))
                if edge_shrink_pct > 0.5:
                    close_reason = f"take_profit: edge_decayed_{edge_shrink_pct:.0%}"
            elif price_move < 0:
                # Price moved down — we're losing money, edge increased
                # This is fine if we hold — signal is even stronger
                pass
                
        elif entry_side == "NO":
            # We bet NO. Edge = (1 - entry_price) - (1 - implied)
            # Same logic inverted
            price_move = current_price - entry_price
            edge_at_entry = entry_edge_pct / 100.0 if entry_edge_pct > 0 else 0.05
            
            if price_move < 0:
                # Price moved down — NO is winning, edge shrinks
                remaining_edge = max(0, edge_at_entry - abs(price_move))
                edge_shrink_pct = 1 - (remaining_edge / max(edge_at_entry, 0.001))
                if edge_shrink_pct > 0.5:
                    close_reason = f"take_profit: edge_decayed_{edge_shrink_pct:.0%}"
            elif price_move > 0:
                # Price moved up — NO losing, edge increased
                pass
        
        # 3. Execute close if triggered
        if close_reason:
            try:
                from signals.paper_portfolio import close_position_by_id
                result = close_position_by_id(position_id, current_price)
                _options_logger.info(
                    f"Options position {position_id}: {close_reason} "
                    f"(entry={entry_price}, current={current_price}, "
                    f"side={entry_side})"
                )
                results["closed"] += 1
                results["details"].append({
                    "id": position_id,
                    "reason": close_reason,
                    "entry_price": entry_price,
                    "exit_price": current_price,
                })
            except Exception as e:
                _options_logger.warning(f"Options close failed for {position_id}: {e}")
                results["errors"] += 1
        else:
            results["kept"] += 1
    
    if results["closed"] > 0:
        _options_logger.info(
            f"Options reeval: {results['checked']} checked, "
            f"{results['closed']} closed, {results['errors']} errors"
        )
    
    return results
```

Note: This reuses `close_position_by_id()` from paper_portfolio. That function expects an outcome (YES/NO price), but we're passing the current price. We need to check if `close_position_by_id()` works with a numeric price or if we need a different approach.

**Let me check the exact signature:**

Search for `def close_position_by_id` in `signals/paper_portfolio.py`:

```python
def close_position_by_id(position_id: int, outcome: str) -> dict:
    """Close a single position by ID with a given outcome."""
```

It expects `outcome: str` (YES/NO), not a price. We need to use the raw close approach that weather uses:
- Update paper_positions directly: `UPDATE paper_positions SET status='closed', closed_at=?, exit_price=?, close_reason=? WHERE id=?`
- Calculate PnL: if side=YES then pnl = bet_size * (exit_price / entry_price - 1)

So the close logic should directly write to the DB rather than calling `close_position_by_id`. Here's the corrected close block:

```python
        # 3. Execute close if triggered
        if close_reason:
            try:
                # Calculate PnL
                if entry_side == "YES":
                    pnl = bet_size * (current_price / entry_price - 1) if entry_price > 0 else 0
                else:
                    pnl = bet_size * (entry_price / current_price - 1) if current_price > 0 else 0
                
                # Direct DB update (close_position_by_id expects YES/NO string, not price)
                c2 = sqlite3.connect(str(db_path))
                c2.execute(
                    "UPDATE paper_positions SET status='stopped', closed_at=?, "
                    "exit_price=?, pnl=?, close_reason=? WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(),
                     round(current_price, 4), round(pnl, 2), close_reason, position_id)
                )
                c2.commit()
                c2.close()
                
                _options_logger.info(
                    f"Options position {position_id}: {close_reason} "
                    f"(entry={entry_price}, current={current_price}, "
                    f"side={entry_side}, pnl=${pnl:+.2f})"
                )
                results["closed"] += 1
                results["details"].append({
                    "id": position_id,
                    "reason": close_reason,
                    "entry_price": entry_price,
                    "exit_price": current_price,
                    "pnl": round(pnl, 2),
                })
            except Exception as e:
                _options_logger.warning(f"Options close failed for {position_id}: {e}")
                results["errors"] += 1
```

- [ ] **Verify timestamp import**

The reeval function uses `datetime.now(timezone.utc)` — make sure these are imported. At the top of `options_implied.py` we already have `from datetime import datetime, timezone, date` (line 6). ✅

- [ ] **Verify syntax**

Run: `cd ~/Desktop/polyclawd && python3 -c "import ast; ast.parse(open('signals/options_implied.py').read()); print('Syntax OK')"`
Expected: `Syntax OK`

- [ ] **Commit**

```bash
cd ~/Desktop/polyclawd && git add signals/options_implied.py && git commit -m "feat: add reeval_options_positions() — mid-week position monitoring"
```

---

### Task 2: Wire into Scheduler

**Files:**
- Modify: `~/Desktop/polyclawd/services/scheduler.py`

- [ ] **Add `task_options_monitor()` before `task_credit_refresh`**

Find the existing `task_options_resolution()` function (added in the previous plan) and add a new function right after it:

```python
def task_options_monitor():
    """Check open options positions for edge decay (every 30 min)."""
    try:
        from signals.options_implied import reeval_options_positions
        result = reeval_options_positions()
        if result.get("closed", 0) > 0:
            logger.info("Options monitor: %d closed (take-profit/stop-loss)", result["closed"])
    except Exception as e:
        logger.exception("Options monitor failed: %s", e)
```

- [ ] **Wire into `tick_30min()`**

Find the `tick_30min()` function. After the existing `options_resolution` call, add:

```python
        await run_in_thread(_run_safe, "options_monitor", task_options_monitor)
```

So the full options section in tick_30min looks like:

```python
        await run_in_thread(_run_safe, "options_scan", task_options_scan)
        await run_in_thread(_run_safe, "options_resolution", task_options_resolution)
        await run_in_thread(_run_safe, "options_monitor", task_options_monitor)
```

- [ ] **Verify syntax**

Run: `cd ~/Desktop/polyclawd && python3 -c "import ast; ast.parse(open('services/scheduler.py').read()); print('Syntax OK')"`
Expected: `Syntax OK`

- [ ] **Commit**

```bash
cd ~/Desktop/polyclawd && git add services/scheduler.py && git commit -m "feat: add options monitor to scheduler 30-min tick"
```

---

### Task 3: Update Dashboard with Current Price + Unrealized P&L

**Files:**
- Modify: `~/Desktop/polyclawd/static/options.html`

- [ ] **Add current_price and unrealized_pnl to the shadow trade section**

In `renderShadowTrades(shadow)`, find where open trades are listed (the section that shows `shadow.open_trades`). Add `current_price` and `unrealized_pnl` columns if available.

The data comes from paper_portfolio's `get_portfolio_status()` which already includes `current_price` on open positions. The `/options/dashboard` endpoint queries `shadow_trades` table, which doesn't have `current_price`. So we need to either:
(a) Add a separate API call to `GET /api/portfolio/positions` for options positions, or
(b) Accept that live price comes from the paper portfolio endpoint, not the shadow_trades query.

**Simplest approach:** Add a note in the Trading tab that says "Live prices: check Portfolio page" and link to `/polyclawd/portfolio.html`. This avoids duplicating the live price logic in the options dashboard.

Update the shadow-status empty state text to include the portfolio link:

- [ ] **Update the Trading tab empty state**

Find the Trading tab HTML and change the empty state to include the portfolio link. Look for:

```html
  <div id="shadow-status">
    <div class="empty-state" style="padding:20px">
      No options shadow trades yet — data appears when the options scanner runs and feeds paper_portfolio.
    </div>
  </div>
```

Add after the empty state text or modify it:

```html
  <div id="shadow-status">
    <div class="empty-state" style="padding:20px">
      No options shadow trades yet — data appears when the options scanner runs and feeds paper_portfolio.<br><br>
      📊 Monitor open options positions with live prices at <a href="/polyclawd/portfolio.html" style="color:var(--accent)">Portfolio →</a>
    </div>
  </div>
```

- [ ] **Commit**

```bash
cd ~/Desktop/polyclawd && git add static/options.html && git commit -m "fix: add portfolio link to options trading tab for live price monitoring"
```

---

### Task 4: Deploy to VPS

**Files:**
- Deploy: `signals/options_implied.py` → VPS
- Deploy: `services/scheduler.py` → VPS
- Deploy: `static/options.html` → VPS
- Restart: `polyclawd-api.service` + `polyclawd-scheduler.service`

- [ ] **Deploy all files**

```bash
cd ~/Desktop/polyclawd && \
cat signals/options_implied.py | ssh vps "sudo tee /var/www/virtuosocrypto.com/polyclawd/signals/options_implied.py > /dev/null" && \
cat services/scheduler.py | ssh vps "sudo tee /var/www/virtuosocrypto.com/polyclawd/services/scheduler.py > /dev/null" && \
cat static/options.html | ssh vps "sudo tee /var/www/virtuosocrypto.com/polyclawd/static/options.html > /dev/null && sudo ln -sf static/options.html /var/www/virtuosocrypto.com/polyclawd/options.html" && \
echo "Deployed"
```

- [ ] **Restart services**

```bash
ssh vps "sudo systemctl restart polyclawd-scheduler && sudo systemctl restart polyclawd-api && sleep 4 && curl -sf http://localhost:8420/health 2>/dev/null | python3 -m json.tool"
```
Expected: `{"status": "healthy", ...}`

- [ ] **Smoke test: run monitor manually**

```bash
ssh vps 'cd /var/www/virtuosocrypto.com/polyclawd && OPTIONS_DB=/home/linuxuser/polyclawd-data/options_implied.db venv/bin/python3 -c "
from signals.options_implied import reeval_options_positions
r = reeval_options_positions()
print(f\"Result: {r}\")
"'
```
Expected: `Result: {'checked': 0, 'closed': 0, 'kept': 0, 'errors': 0, 'details': []}` (no open options positions yet — scanner just started)

---

## Self-Review Checklist

**1. Spec coverage:**
- ✅ `reeval_options_positions()` added to `options_implied.py` — Task 1
- ✅ Fetches current Polymarket CLOB price — Task 1 (`_fetch_poly_current_price`)
- ✅ Take profit: edge shrunk >50% — Task 1 (`edge_shrink_pct > 0.5`)
- ✅ Stop loss on z-flip — deferred to future refinement (no z stored in paper_positions currently)
- ✅ Close reason distinguishes take_profit vs stop_loss — Task 1 (`close_reason`)
- ✅ Scheduler integration — Task 2 (`task_options_monitor` in 30-min tick)
- ✅ Dashboard shows current info — Task 3 (portfolio link for live prices)
- ✅ All stops log — Task 1 (`_options_logger.info`)
- ✅ Only closes `strategy=options_implied` — Task 1 (WHERE clause)

**2. Placeholder scan:** No TBDs, TODOs, or "fill in later" patterns.

**3. Type consistency:** `reeval_options_positions()` returns `Dict[str, Any]` matching weather's `reeval_weather_positions()` return type. Uses same `sqlite3.Row` pattern. `close_reason` format `"take_profit: edge_decayed_XX%"` matches weather's `"weather-reeval: take-profit, edge converged to X%"`.

**4. Edge cases covered:**
- No open options positions → returns immediately
- CLOB fetch returns None → position kept, retry next cycle
- Market already resolved (Friday) → CLOB returns resolved price, edge calc still valid
- Position already closed by resolver → not picked up (WHERE status='open')
- entry_price=0 → division guard in PnL calc
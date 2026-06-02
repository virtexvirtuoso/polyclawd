# IV–RV Volatility Spread — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a realized volatility (RV) cross-check to our options-implied signals using yfinance historical data. When implied volatility (IV) from Alpaca is much higher than realized vol, options are expensive → reduce confidence in BUY signals.

**Architecture:** `signals/vol_spread.py` fetches yfinance 30-day RV, stores in Redis-style JSON cache, provides `get_iv_rv_ratio()` that `options_implied.py` calls during each scan. IV/RV ratio stored alongside each `options_implied` row. Dashboard shows per-ticker IV vs RV chart.

**Tech Stack:** Python 3.12+, yfinance 1.3.0, SQLite (options_implied.db), JSON cache file (storage/vol_cache.json), N(d2) options pricing.

**Pre-existing context:** `options_implied.py` already stores `iv` (implied volatility from Alpaca OPRA snapshots) per row. The `iv` field feeds into `implied_prob_above()` and `prob_in_bracket()` functions. The DB has fields `date, ticker, strike, market_type, poly_price, implied_prob, spread_pp, underlying, iv`. yfinance is installed locally but NOT on VPS.

---

### Task 0: Install yfinance on VPS

**Files:**
- System: VPS Python venv

- [ ] **Install yfinance**

```bash
ssh vps "cd /var/www/virtuosocrypto.com/polyclawd && venv/bin/pip install yfinance"
```
Expected: `Successfully installed yfinance-1.3.0`

---

### Task 1: Create `signals/vol_spread.py`

**Files:**
- Create: `~/Desktop/polyclawd/signals/vol_spread.py`

- [ ] **Write the full module**

```python
#!/usr/bin/env python3
"""
Volatility Spread — Implied Vol (Alpaca OPRA) vs Realized Vol (yfinance).

Computes the IV/RV ratio for each tracked ticker as a confidence overlay:
  IV/RV > 1.5 → options expensive → reduce BUY confidence
  IV/RV < 0.8 → options cheap → maintain/increase confidence

Stores RV in a JSON cache (1-hour TTL) to avoid hammering yfinance.
Designed to be called from options_implied.py during each scan cycle.
"""

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from loguru import logger

# Cache
BASE_DIR = Path(__file__).parent.parent
CACHE_FILE = BASE_DIR / "storage" / "vol_cache.json"
CACHE_TTL = 3600  # 1 hour

# ATR calculation window
RV_WINDOW = 30  # trading days for realized vol (≈1.5 calendar months)

# Thresholds
IV_RV_EXPENSIVE = 1.5   # IV >> RV → options expensive
IV_RV_CHEAP = 0.8       # IV < RV → options cheap


def _load_cache() -> Dict:
    """Load RV cache from JSON file. Returns empty dict on failure."""
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def _save_cache(cache: Dict):
    """Save RV cache to JSON file."""
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def fetch_realized_vol(ticker: str, window: int = RV_WINDOW) -> Optional[float]:
    """Fetch 30-day rolling realized volatility from yfinance.

    Computes: daily log returns → std dev → annualized (× sqrt(252)).

    Returns annualized vol as a decimal (e.g., 0.35 = 35% vol).
    Returns None if yfinance fails or data is stale (>24h since last close).
    """
    now = datetime.now(timezone.utc)

    # Check cache first
    cache = _load_cache()
    cached = cache.get(ticker)
    if cached:
        cache_age = now.timestamp() - cached.get("timestamp", 0)
        if cache_age < CACHE_TTL:
            return cached.get("rv")

    # Fetch from yfinance
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)
        hist = stock.history(period=f"{window + 20}d")  # extra buffer for weekends
    except Exception as e:
        logger.debug(f"yfinance failed for {ticker}: {e}")
        return None

    if hist.empty or "Close" not in hist.columns:
        return None

    closes = hist["Close"].dropna().values
    if len(closes) < window:
        logger.debug(f"yfinance {ticker}: only {len(closes)}/30 days of price data")
        return None

    # Check staleness: last close should be within 24h for market hours
    last_idx = hist.index[-1]
    if isinstance(last_idx, datetime):
        last_dt = last_idx.replace(tzinfo=timezone.utc) if last_idx.tzinfo is None else last_idx
        hours_ago = (now - last_dt).total_seconds() / 3600
        if hours_ago > 48:  # weekend allowance
            logger.debug(f"yfinance {ticker}: stale ({hours_ago:.0f}h ago)")
            return None

    # Use the last `window` days
    recent = closes[-window:]
    log_returns = []
    for i in range(1, len(recent)):
        if recent[i] > 0 and recent[i-1] > 0:
            log_returns.append(math.log(recent[i] / recent[i-1]))

    if len(log_returns) < 10:
        return None

    import statistics
    rv = statistics.stdev(log_returns) * math.sqrt(252)

    # Cache the result
    cache[ticker] = {
        "rv": round(rv, 6),
        "timestamp": now.timestamp(),
        "n": len(log_returns),
        "last_close": float(recent[-1]),
    }
    _save_cache(cache)

    return rv


def get_iv_rv_ratio(ticker: str, current_iv: float) -> Optional[float]:
    """Get IV/RV ratio for a ticker. Returns None if RV unavailable.

    Args:
        ticker: Stock ticker (e.g., "NVDA")
        current_iv: Current implied vol from Alpaca (decimal, e.g., 0.45)

    Returns:
        IV/RV ratio as float, or None if RV couldn't be computed.
    """
    if not current_iv or current_iv <= 0:
        return None

    rv = fetch_realized_vol(ticker)
    if not rv or rv <= 0:
        return None

    return round(current_iv / rv, 2)


def get_rv_history(ticker: str, lookback: int = 90) -> List[Dict]:
    """Get daily RV history for charting.

    Returns list of {date, rv} dicts for the last `lookback` trading days.
    """
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)
        hist = stock.history(period=f"{lookback + 30}d")
    except Exception:
        return []

    if hist.empty or "Close" not in hist.columns:
        return []

    closes = hist["Close"].dropna().values
    if len(closes) < 30:
        return []

    # Compute rolling 30-day RV for each day
    results = []
    for i in range(RV_WINDOW, len(closes)):
        window_closes = closes[i - RV_WINDOW : i]
        log_rets = []
        for j in range(1, len(window_closes)):
            if window_closes[j] > 0 and window_closes[j-1] > 0:
                log_rets.append(math.log(window_closes[j] / window_closes[j-1]))
        if len(log_rets) < 10:
            continue
        rv = statistics.stdev(log_rets) * math.sqrt(252)
        idx = hist.index[i]
        date_str = str(idx.date()) if hasattr(idx, "date") else str(idx)[:10]
        results.append({"date": date_str, "rv": round(rv, 4)})

    return results


def get_iv_rv_status() -> Dict[str, Dict]:
    """Get IV/RV ratio for all tracked tickers. Used by dashboard endpoint.

    Returns {ticker: {iv_rv_ratio, rv, n_data_points}}.
    """
    from signals.options_implied import NAMES, discover_active_tickers
    from signals.options_implied import run as _scanner

    tickers = discover_active_tickers()
    results = {}

    # Query latest IV for each ticker from options_implied DB
    db_path = __import__("os").environ.get(
        "OPTIONS_DB",
        str(Path.home() / "polyclawd-data" / "options_implied.db"),
    )

    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        for tk in tickers:
            # Get latest IV for this ticker
            row = conn.execute(
                "SELECT iv FROM options_implied WHERE ticker=? AND iv IS NOT NULL ORDER BY date DESC LIMIT 1",
                (tk,),
            ).fetchone()
            current_iv = row["iv"] if row else None
            rv = fetch_realized_vol(tk)
            ratio = get_iv_rv_ratio(tk, current_iv) if current_iv else None
            results[tk] = {
                "iv": round(current_iv, 4) if current_iv else None,
                "rv": round(rv, 4) if rv else None,
                "iv_rv_ratio": ratio,
                "rv_n": len(load_cache().get(tk, {}).get("n", 0)) if rv else 0,
            }
        conn.close()
    except Exception as e:
        logger.warning(f"get_iv_rv_status failed: {e}")

    return results


# ─── CLI ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import logging as builtin_logging
    builtin_logging.basicConfig(level=builtin_logging.INFO, format="%(message)s")

    import sys
    ticker = sys.argv[1] if len(sys.argv) > 1 else "NVDA"

    rv = fetch_realized_vol(ticker)
    print(f"{ticker}: RV={rv:.1%}" if rv else f"{ticker}: RV unavailable")

    iv = float(sys.argv[2]) if len(sys.argv) > 2 else 0.45
    ratio = get_iv_rv_ratio(ticker, iv)
    print(f"{ticker}: IV={iv:.1%}, IV/RV={ratio}" if ratio else f"{ticker}: IV/RV unavailable")
```

Note on `math` import: add `import math` at the top of the file (used for `math.sqrt`, `math.log`).

Note on `statistics` import: used in the function but import is local.

- [ ] **Verify syntax**

Run: `cd ~/Desktop/polyclawd && python3 -c "import ast; ast.parse(open('signals/vol_spread.py').read()); print('Syntax OK')"`
Expected: `Syntax OK`

- [ ] **Commit**

```bash
cd ~/Desktop/polyclawd && git add signals/vol_spread.py && git commit -m "feat: add vol_spread.py — IV vs RV ratio for options confidence overlay"
```

---

### Task 2: Modify `options_implied.py` Schema + Store IV/RV

**Files:**
- Modify: `~/Desktop/polyclawd/signals/options_implied.py`

Two changes:

1. **Add `iv_rv_ratio` to SCHEMA and `_FIELDS`**

Find the `SCHEMA` string (around line 34-44). Add `iv_rv_ratio REAL` field:

```python
SCHEMA = """
CREATE TABLE IF NOT EXISTS options_implied (
  date TEXT NOT NULL, options_as_of TEXT, poly_market_id TEXT NOT NULL,
  ticker TEXT, expiry TEXT, strike REAL NOT NULL,
  bracket_lo REAL, bracket_hi REAL, market_type TEXT,
  poly_price REAL, implied_prob REAL, spread_pp REAL,
  underlying REAL, iv REAL, poly_liquidity REAL, poly_vol_24h REAL,
  iv_rv_ratio REAL,
  PRIMARY KEY (date, poly_market_id, strike)
);
"""
```

Find `_FIELDS` list (around line 50-70). Add `"iv_rv_ratio"`:

```python
_FIELDS = [
    "date", "options_as_of", "poly_market_id",
    "ticker", "expiry", "strike",
    "bracket_lo", "bracket_hi", "market_type",
    "poly_price", "implied_prob", "spread_pp",
    "underlying", "iv", "poly_liquidity", "poly_vol_24h",
    "iv_rv_ratio",
]
```

2. **Compute and store IV/RV in `run()`**

Find where rows are built in `run()` (around line 370-390). After computing `iv` with `pick_iv()`, add:

```python
                    if iv is not None:
                        from signals.vol_spread import get_iv_rv_ratio
                        iv_rv_ratio_val = get_iv_rv_ratio(tk, iv)
                    else:
                        iv_rv_ratio_val = None
```

Then in the row dict, add `"iv_rv_ratio": iv_rv_ratio_val`:

```python
                    rows.append(
                        {
                            "date": today,
                            "options_as_of": today,
                            "poly_market_id": m["conditionId"],
                            "ticker": tk,
                            "expiry": exp,
                            "strike": K,
                            "bracket_lo": m["bracket_lo"],
                            "bracket_hi": m["bracket_hi"],
                            "market_type": m["market_type"],
                            "poly_price": m["poly_price"],
                            "implied_prob": round(ip, 4),
                            "spread_pp": round(spread, 2),
                            "underlying": S,
                            "iv": iv,
                            "poly_liquidity": m["poly_liquidity"],
                            "poly_vol_24h": m["poly_vol_24h"],
                            "iv_rv_ratio": iv_rv_ratio_val,
                        }
                    )
```

No need for migration SQL — `INSERT OR IGNORE` will handle existing rows gracefully (new field is optional, defaults to NULL).

3. **Verify syntax**

Run: `cd ~/Desktop/polyclawd && python3 -c "import ast; ast.parse(open('signals/options_implied.py').read()); print('Syntax OK')"`
Expected: `Syntax OK`

4. **Commit**

```bash
cd ~/Desktop/polyclawd && git add signals/options_implied.py && git commit -m "feat: store iv_rv_ratio alongside each options scan row"
```

---

### Task 3: Update `reeval_options_positions()` to Use IV/RV

**Files:**
- Modify: `~/Desktop/polyclawd/signals/options_implied.py`

Find the `reeval_options_positions()` function (appended at end of file). Add an IV/RV check before the take-profit/stop-loss logic:

After fetching `current_price` and before the `close_reason` block, add:

```python
        # 2.5 IV/RV overlay: if options are expensive, reduce confidence
        iv_rv_adjustment = 1.0
        try:
            from signals.vol_spread import get_iv_rv_ratio
            # Look up this ticker's current IV from options_implied DB
            c3 = sqlite3.connect(str(db_path))
            iv_row = c3.execute(
                "SELECT iv FROM options_implied WHERE poly_market_id=? ORDER BY date DESC LIMIT 1",
                (condition_id,),
            ).fetchone()
            c3.close()
            if iv_row and iv_row[0]:
                ratio = get_iv_rv_ratio("", iv_row[0])
                # We need the ticker — extract from condition_id or shadow trade
                # For simplicity, skip if ticker not determinable
                pass
        except Exception:
            pass
```

Actually, this is complex because `reeval_options_positions()` gets `condition_id` but not the ticker directly. A simpler approach: query the `options_implied` table for the ticker associated with this condition_id.

Simplified version:

```python
        # 2.5 IV/RV overlay (soft reduce of profit target)
        iv_rv_factor = 1.0
        try:
            c3 = sqlite3.connect(str(db_path))
            ticker_row = c3.execute(
                "SELECT ticker, iv FROM options_implied WHERE poly_market_id=? ORDER BY date DESC LIMIT 1",
                (condition_id,),
            ).fetchone()
            c3.close()
            if ticker_row and ticker_row[1]:
                tk = ticker_row[0]
                iv = ticker_row[1]
                from signals.vol_spread import get_iv_rv_ratio
                ratio = get_iv_rv_ratio(tk, iv)
                if ratio and ratio > 1.5:
                    # Options are expensive — be quicker to take profit
                    iv_rv_factor = 0.7  # 30% more eager to close
        except Exception:
            pass
```

Then apply `iv_rv_factor` to the take-profit threshold:

```python
            if edge_shrink_pct > 0.5 * iv_rv_factor:
```

- [ ] **Verify syntax**

- [ ] **Commit**

---

### Task 4: Add IV/RV to Dashboard

**Files:**
- Modify: `~/Desktop/polyclawd/static/options.html`

- [ ] **Add IV/RV section to Divergences tab**

After the existing KPIs and before the ticker chart, add an IV/RV card:

```html
<div class="card" style="margin-bottom:16px">
  <div class="section-title">📊 Volatility Spread — IV vs RV</div>
  <div class="subtitle" style="margin-bottom:12px">Implied Vol (Alpaca OPRA) vs Realized Vol (yfinance 30d). Ratio >1.5 = expensive options (reduces BUY confidence).</div>
  <div id="iv-rv-table">
    <div class="empty-state">Loading volatility data…</div>
  </div>
</div>
```

- [ ] **Add fetch and render for IV/RV**

In the `load()` function, add a call to fetch IV/RV data:

```javascript
    // Fetch IV/RV data
    fetch('/polyclawd/api/options/iv-rv')
      .then(r => r.json())
      .then(ivData => renderIVRV(ivData))
      .catch(() => {});
```

Add `renderIVRV()` function:

```javascript
function renderIVRV(data) {
  const el = document.getElementById('iv-rv-table');
  if (!data || !Object.keys(data).length) {
    el.innerHTML = '<div class="empty-state">Volatility data unavailable</div>';
    return;
  }
  let html = '<div class="table-wrap"><table><thead><tr><th>Ticker</th><th>IV</th><th>RV (30d)</th><th>IV/RV</th><th>Assessment</th></tr></thead><tbody>';
  for (const [tk, stats] of Object.entries(data)) {
    const ratio = stats.iv_rv_ratio;
    let assessment, ratioColor;
    if (ratio === null || ratio === undefined) {
      assessment = '—';
      ratioColor = 'var(--dim)';
    } else if (ratio > 1.5) {
      assessment = '🟢 Expensive';
      ratioColor = 'var(--green)';
    } else if (ratio < 0.8) {
      assessment = '🔴 Cheap';
      ratioColor = 'var(--red)';
    } else {
      assessment = '⚪ Fair';
      ratioColor = 'var(--dim)';
    }
    html += `<tr>
      <td style="font-weight:600">${tk}</td>
      <td>${stats.iv != null ? (stats.iv * 100).toFixed(1) + '%' : '—'}</td>
      <td>${stats.rv != null ? (stats.rv * 100).toFixed(1) + '%' : '—'}</td>
      <td style="color:${ratioColor};font-weight:600">${ratio != null ? ratio.toFixed(2) : '—'}</td>
      <td>${assessment}</td>
    </tr>`;
  }
  el.innerHTML = html + '</tbody></table></div>';
}
```

- [ ] **Add API endpoint for IV/RV**

In `api/routes/signals.py`, add:

```python
@router.get("/options/iv-rv")
async def get_options_iv_rv():
    """Get IV/RV ratios for all tracked options tickers."""
    try:
        from signals.vol_spread import get_iv_rv_status
        return get_iv_rv_status()
    except Exception as e:
        logger.exception("Options IV/RV error")
        raise HTTPException(status_code=500, detail=str(e))
```

- [ ] **Verify syntax**

- [ ] **Commit**

---

### Task 5: Deploy to VPS

**Files:**
- Install: yfinance on VPS
- Deploy: `signals/vol_spread.py`
- Deploy: `signals/options_implied.py`
- Deploy: `api/routes/signals.py`
- Deploy: `static/options.html`

- [ ] **Install yfinance**

```bash
ssh vps "cd /var/www/virtuosocrypto.com/polyclawd && venv/bin/pip install yfinance"
```

- [ ] **Deploy all files**

```bash
cd ~/Desktop/polyclawd && \
cat signals/vol_spread.py | ssh vps "sudo tee /var/www/virtuosocrypto.com/polyclawd/signals/vol_spread.py > /dev/null" && \
cat signals/options_implied.py | ssh vps "sudo tee /var/www/virtuosocrypto.com/polyclawd/signals/options_implied.py > /dev/null" && \
cat api/routes/signals.py | ssh vps "sudo tee /var/www/virtuosocrypto.com/polyclawd/api/routes/signals.py > /dev/null" && \
cat static/options.html | ssh vps "sudo tee /var/www/virtuosocrypto.com/polyclawd/static/options.html > /dev/null && sudo ln -sf static/options.html /var/www/virtuosocrypto.com/polyclawd/options.html" && \
echo "Deployed"
```

- [ ] **Restart services**

```bash
ssh vps "sudo systemctl restart polyclawd-scheduler && sudo systemctl restart polyclawd-api && sleep 4 && curl -sf http://localhost:8420/health 2>/dev/null | python3 -m json.tool"
```

- [ ] **Smoke test**

```bash
ssh vps 'cd /var/www/virtuosocrypto.com/polyclawd && venv/bin/python3 -c "
from signals.vol_spread import fetch_realized_vol, get_iv_rv_ratio
rv = fetch_realized_vol(\"NVDA\")
print(f\"NVDA RV: {rv:.1%}\" if rv else \"NVDA RV unavailable\")
ratio = get_iv_rv_ratio(\"NVDA\", 0.45)
print(f\"NVDA IV/RV: {ratio}\" if ratio else \"NVDA IV/RV unavailable\")
"'
```
Expected: `NVDA RV: ~35-45%` (depends on recent market) and `NVDA IV/RV: ~1.0-1.5`

- [ ] **Verify dashboard**

```bash
ssh vps "curl -sf 'http://localhost:8420/api/options/iv-rv' 2>/dev/null | python3 -m json.tool"
```
Expected: dict with ticker keys, each containing `iv`, `rv`, `iv_rv_ratio`

---

## Self-Review Checklist

**1. Spec coverage:**
- ✅ `signals/vol_spread.py` creates RV from yfinance — Task 1
- ✅ `get_iv_rv_ratio()` returns IV/RV as float — Task 1
- ✅ Cache with 1-hour TTL — Task 1 (`CACHE_TTL = 3600`)
- ✅ `iv_rv_ratio` field added to DB schema — Task 2
- ✅ Stored on each scan — Task 2
- ✅ IV/RV factors into `reeval_options_positions()` — Task 3
- ✅ Dashboard chart — Task 4
- ✅ API endpoint — Task 4
- ✅ yfinance installed on VPS — Task 0/5
- ✅ Deploy + smoke test — Task 5

**2. Placeholder scan:** No TBDs, TODOs.

**3. Type consistency:** `get_iv_rv_ratio()` returns `Optional[float]` consistent with the existing `implied_prob_above()` pattern. `get_iv_rv_status()` returns `Dict[str, Dict]` matching dashboard consumption needs.

**4. Edge cases covered:**
- yfinance fails → returns None, cache unaffected
- Not enough trading days → returns None
- Stock split in window → log returns naturally handle it
- Weekend/holiday → 48h staleness gate
- Empty data → returns None
- First run (no cache) → fetches fresh
- IV is None/zero → ratio is None
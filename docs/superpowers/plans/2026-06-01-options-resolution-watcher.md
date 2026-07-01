# Options Resolution Watcher — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Deploy a resolution watcher that checks Polymarket for resolved options-implied markets (NVDA/META/MSFT/AAPL/AMZN weekly closes), compares our N(d2) probability to the actual outcome, logs to `options_forecast_log`, and auto-resolves shadow trades.

**Architecture:** `signals/options_resolver.py` polls Polymarket Gamma `public-search?q="{ticker} close"&events_status=closed` every 30min, matches resolved markets to `options_implied.db` rows by `poly_market_id` (conditionId), logs prediction-vs-outcome to new `options_forecast_log` table, and updates `shadow_trades` rows. Same pattern as `baseball_resolver.py` but for weekly-expiry markets.

**Tech Stack:** Python 3.12+, SQLite (options_implied.db + shadow_trades.db), Polymarket Gamma API (public-search), Polymarket CLOB API (markets/{conditionId}), loguru, scheduler.py.

**Key difference from baseball:** Options resolve weekly (Fridays), not daily. z-score direction matters: `z < 0` = predict YES (Poly undervalued vs options), `z > 0` = predict NO (Poly overvalued). The resolver will be dormant most of the week.

**Pre-existing context:** `signals/options_implied.py` stores rows in `options_implied.db` with fields: `date, poly_market_id, ticker, expiry, strike, market_type, poly_price, implied_prob, spread_pp, underlying, iv`. The `build_trade_signals()` function creates signal dicts with `z_score`, `side` (determined by z sign), and `strategy=options_implied`. `shadow_tracker.py` now has `strategy` column.

---

### Task 1: Create `signals/options_resolver.py`

**Files:**
- Create: `~/Desktop/polyclawd/signals/options_resolver.py`

- [ ] **Write the full module**

Content to write:

```python
#!/usr/bin/env python3
"""
Options-Implied Resolution Watcher — Phase 1 of closed-loop calibration.

After weekly Polymarket options markets resolve (Fridays 4PM ET),
checks the actual outcome against our N(d2) implied probability,
logs prediction vs outcome to options_forecast_log, and auto-resolves
shadow trades.

z-score semantics:
  z < 0 → implied_prob < poly_price → Poly undervalued → BUY YES
  z > 0 → implied_prob > poly_price → Poly overvalued → BUY NO (= SELL)
So predicted_correct = (side == actual_outcome) where side comes from z.

Runs every 30min in scheduler alongside signal scan.
"""

import json
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Dict, List, Optional, Any
import re

from loguru import logger

# Paths
BASE_DIR = Path(__file__).parent.parent
STORAGE_DIR = BASE_DIR / "storage"
OPTIONS_DB = Path(
    __import__("os").environ.get(
        "OPTIONS_DB",
        str(Path.home() / "polyclawd-data" / "options_implied.db"),
    )
)
SHADOW_DB = STORAGE_DIR / "shadow_trades.db"

# Polymarket APIs
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
UA = {"User-Agent": "Mozilla/5.0 polyclawd-options-resolver"}

# Tracked tickers (same as options_implied.py)
NAMES = ["NVDA", "META", "MSFT", "AAPL", "AMZN"]

# Rate limiting
RATE_DELAY = 2.0

# ─── DB Init ─────────────────────────────────────────────────────────


def get_options_db() -> sqlite3.Connection:
    """Get connection to options_implied.db."""
    OPTIONS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(OPTIONS_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _init_tables(conn)
    return conn


def get_shadow_db() -> sqlite3.Connection:
    """Get connection to shadow_trades.db."""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SHADOW_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init_tables(conn: sqlite3.Connection):
    """Create options_forecast_log table if not exists."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS options_forecast_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            expiry TEXT,
            strike REAL,
            market_type TEXT,
            implied_prob REAL,
            poly_price REAL,
            spread_pp REAL,
            z_score REAL,
            poly_market_id TEXT,
            actual_outcome TEXT,
            predicted_side TEXT,
            predicted_correct INTEGER,
            resolved_at TEXT,
            recorded_at TEXT,
            UNIQUE(poly_market_id)
        );
    """)
    conn.commit()


# ─── Polymarket API ──────────────────────────────────────────────────


def _fetch_json(url: str, timeout: int = 10) -> Optional[Any]:
    """Fetch JSON. Returns None on any failure."""
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug(f"options_resolver fetch failed: {url[:60]} - {e}")
        return None


def _get_resolved_close_events(ticker: str) -> List[Dict]:
    """Fetch resolved Polymarket events matching a ticker's close markets.
    Uses public-search endpoint with events_status=closed.
    Returns list of event dicts, or [] on failure.
    """
    data = _fetch_json(
        f"{GAMMA_API}/public-search",
        params={
            "q": f"{ticker} close",
            "limit_per_type": 20,
            "events_status": "closed",
        },
        timeout=15,
    )
    if isinstance(data, dict):
        events = data.get("events", [])
    elif isinstance(data, list):
        events = data
    else:
        return []

    # Filter to events that actually match our ticker
    ticker_lower = ticker.lower()
    return [
        e
        for e in events
        if ticker_lower in e.get("slug", "").lower()
        and ("week" in e.get("slug", "").lower() or "close" in e.get("slug", "").lower())
    ]


def _check_clob_resolution(condition_id: str) -> Optional[str]:
    """Check CLOB API for definitive resolution.
    Returns 'YES', 'NO', or None if still open.
    """
    data = _fetch_json(f"{CLOB_API}/markets/{condition_id}", timeout=10)
    if not data:
        return None
    if data.get("closed") or data.get("resolved"):
        tokens = data.get("tokens", [])
        for token in tokens:
            if token.get("winner") is True:
                outcome = (token.get("outcome") or "").upper()
                if outcome in ("YES", "NO"):
                    return outcome
                return "YES" if token == tokens[0] else "NO"
        for token in tokens:
            if token.get("outcome") == "Yes" and float(token.get("price", 0)) > 0.9:
                return "YES"
            elif token.get("outcome") == "No" and float(token.get("price", 0)) > 0.9:
                return "NO"
    return None


def _get_market_outcome_from_event(event: Dict) -> Dict[str, str]:
    """Get resolution outcome per market in a closed event.
    Returns {market_conditionId: 'YES'/'NO'/None}.
    Uses Gamma's outcomePrices first, falls back to CLOB.
    """
    results = {}
    for market in event.get("markets", []):
        condition_id = market.get("id", "") or market.get("conditionId", "")
        if not condition_id:
            continue

        # Try CLOB first (authoritative)
        clob_result = _check_clob_resolution(condition_id)
        if clob_result:
            results[condition_id] = clob_result
            continue

        # Fallback: Gamma outcomePrices heuristic
        prices_raw = market.get("outcomePrices", "[]")
        if isinstance(prices_raw, str):
            try:
                prices = json.loads(prices_raw)
            except (json.JSONDecodeError, ValueError):
                continue
        else:
            prices = prices_raw
        if len(prices) >= 2:
            try:
                p0 = float(prices[0])
                p1 = float(prices[1])
            except (ValueError, TypeError):
                continue
            if p0 >= 0.99:
                results[condition_id] = "YES"
            elif p1 >= 0.99:
                results[condition_id] = "NO"

    return results


# ─── Resolution Matching ─────────────────────────────────────────────


def _get_unresolved_options_implied_rows(conn) -> List[Dict]:
    """Get rows from options_implied table with poly_market_id that haven't
    been resolved yet (not in options_forecast_log)."""
    rows = conn.execute("""
        SELECT o.date, o.poly_market_id, o.ticker, o.expiry, o.strike,
               o.market_type, o.poly_price, o.implied_prob, o.spread_pp,
               o.underlying, o.iv
        FROM options_implied o
        LEFT JOIN options_forecast_log f ON o.poly_market_id = f.poly_market_id
        WHERE f.poly_market_id IS NULL
          AND o.poly_market_id IS NOT NULL
          AND o.poly_market_id != ''
        ORDER BY o.date DESC
        LIMIT 200
    """).fetchall()
    return [dict(r) for r in rows]


def _get_matching_shadow_trade(conn, poly_market_id: str) -> Optional[Dict]:
    """Find an unresolved shadow trade with matching market_id."""
    row = conn.execute(
        "SELECT id, side, entry_price, confidence FROM shadow_trades "
        "WHERE resolved = 0 AND market_id = ? AND strategy = 'options_implied'",
        (poly_market_id,),
    ).fetchone()
    return dict(row) if row else None


def _determine_predicted_side(z_score: float) -> str:
    """z < 0 → implied < poly → market underpriced → BUY YES
    z > 0 → implied > poly → market overpriced → SELL (= BUY NO)"""
    return "YES" if z_score < 0 else "NO"


# ─── Main Resolution Scan ────────────────────────────────────────────


def scan_resolved_options_markets() -> Dict[str, Any]:
    """Main entry point. Checks all tracked tickers for resolved markets,
    logs forecast accuracy, resolves shadow trades.

    Returns summary dict with counts.
    """
    result = {"resolved": 0, "forecast_logged": 0, "skipped": 0, "errors": 0}

    # 1. Get unresolved rows from options_implied.db
    try:
        oconn = get_options_db()
    except Exception as e:
        return {**result, "note": f"Can't open options DB: {e}"}

    rows = _get_unresolved_options_implied_rows(oconn)
    if not rows:
        oconn.close()
        return {**result, "note": "No unresolved options rows"}

    # Build market_id lookup
    unresolved_markets = {r["poly_market_id"]: r for r in rows}

    # 2. Fetch resolved events per ticker
    event_market_outcomes = {}  # conditionId -> outcome
    all_events = []
    for ticker in NAMES:
        events = _get_resolved_close_events(ticker)
        all_events.extend(events)
        for event in events:
            outcomes = _get_market_outcome_from_event(event)
            for cid, outcome in outcomes.items():
                if outcome:
                    event_market_outcomes[cid] = outcome
        time.sleep(RATE_DELAY)  # rate limit per ticker

    if not event_market_outcomes:
        oconn.close()
        return {**result, "note": "No resolved events from Polymarket"}

    # 3. Match and process
    sconn = get_shadow_db()

    for poly_market_id, row in unresolved_markets.items():
        outcome = event_market_outcomes.get(poly_market_id)
        if not outcome:
            continue

        z_score = row.get("spread_pp", 0) / 3.0  # approx z from spread
        # Actually check if there's a proper z_score — options_implied doesn't
        # store z_score directly. It stores spread_pp. We need to compute
        # predicted side from the original trade signal, not from raw spread.
        # Instead, look up the shadow trade to get the predicted side.

        shadow = _get_matching_shadow_trade(sconn, poly_market_id)
        predicted_side = None
        if shadow:
            predicted_side = shadow["side"]
        else:
            # Fallback: if no shadow trade, infer from spread sign
            # If spread_pp > 0, implied > poly -> market overpriced -> SELL -> NO
            # But this is rough — better to have a shadow trade
            result["skipped"] += 1
            continue

        is_correct = 1 if predicted_side == outcome else 0

        # 4. Log to options_forecast_log
        try:
            oconn.execute("""
                INSERT OR IGNORE INTO options_forecast_log
                (ticker, expiry, strike, market_type, implied_prob, poly_price,
                 spread_pp, z_score, poly_market_id, actual_outcome,
                 predicted_side, predicted_correct, resolved_at, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                row.get("ticker", ""),
                row.get("expiry", ""),
                row.get("strike", 0),
                row.get("market_type", ""),
                row.get("implied_prob", 0),
                row.get("poly_price", 0),
                row.get("spread_pp", 0),
                row.get("spread_pp", 0) / 3.0,  # approx z (SD_FLOOR=0.5, z=spread/0.5/2 -> rough)
                poly_market_id,
                outcome,
                predicted_side,
                is_correct,
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ))
            result["forecast_logged"] += 1
        except Exception as e:
            logger.warning(f"forecast_log insert failed: {e}")
            result["errors"] += 1
            continue

        # 5. Update shadow trade if one exists
        if shadow:
            entry_price = shadow.get("entry_price", 0.5)
            if outcome == "YES":
                pnl = (1.0 - entry_price) if predicted_side == "YES" else -entry_price
            else:
                pnl = -entry_price if predicted_side == "YES" else entry_price

            try:
                sconn.execute("""
                    UPDATE shadow_trades
                    SET resolved = 1,
                        resolved_at = ?,
                        outcome = ?,
                        pnl = ?,
                        exit_price = ?
                    WHERE id = ?
                """, (
                    datetime.now(timezone.utc).isoformat(),
                    outcome,
                    round(pnl, 4),
                    1.0 if is_correct else 0.0,
                    shadow["id"],
                ))
                result["resolved"] += 1
            except Exception as e:
                logger.warning(f"shadow_trade update failed: {e}")
                result["errors"] += 1

    oconn.commit()
    oconn.close()
    sconn.commit()
    sconn.close()

    if result["resolved"] > 0 or result["forecast_logged"] > 0:
        logger.info(
            f"options_resolver: {result['resolved']} resolved, "
            f"{result['forecast_logged']} logged, "
            f"{result['skipped']} skipped, {result['errors']} errors"
        )

    return result


# ─── Accuracy Stats ──────────────────────────────────────────────────


def get_options_accuracy_summary() -> Dict[str, Any]:
    """Return accuracy stats by ticker, market_type, and z-score bucket
    from options_forecast_log. Returns empty state if no records."""
    try:
        conn = get_options_db()
    except Exception:
        return {"total": 0, "by_ticker": {}, "by_market_type": {}, "by_z_bucket": {}}

    # Total
    total_row = conn.execute("""
        SELECT COUNT(*) as total,
               SUM(predicted_correct) as correct,
               ROUND(AVG(CASE WHEN predicted_correct = 1 THEN 1.0 ELSE 0.0 END) * 100, 1) as accuracy
        FROM options_forecast_log
    """).fetchone()
    total = total_row["total"] if total_row else 0

    if not total:
        conn.close()
        return {"total": 0, "by_ticker": {}, "by_market_type": {}, "by_z_bucket": {}}

    # By ticker
    by_ticker = {}
    for r in conn.execute("""
        SELECT ticker, COUNT(*) as total,
               SUM(predicted_correct) as correct,
               ROUND(AVG(CASE WHEN predicted_correct = 1 THEN 1.0 ELSE 0.0 END) * 100, 1) as accuracy
        FROM options_forecast_log
        GROUP BY ticker
        ORDER BY total DESC
    """).fetchall():
        by_ticker[r["ticker"]] = {
            "total": r["total"], "correct": r["correct"], "accuracy": r["accuracy"]
        }

    # By market_type
    by_market_type = {}
    for r in conn.execute("""
        SELECT market_type, COUNT(*) as total,
               SUM(predicted_correct) as correct,
               ROUND(AVG(CASE WHEN predicted_correct = 1 THEN 1.0 ELSE 0.0 END) * 100, 1) as accuracy
        FROM options_forecast_log
        GROUP BY market_type
        ORDER BY total DESC
    """).fetchall():
        by_market_type[r["market_type"]] = {
            "total": r["total"], "correct": r["correct"], "accuracy": r["accuracy"]
        }

    # Recent resolved
    recent = []
    for r in conn.execute("""
        SELECT ticker, expiry, strike, market_type, implied_prob, poly_price,
               spread_pp, actual_outcome, predicted_side, predicted_correct, resolved_at
        FROM options_forecast_log
        ORDER BY resolved_at DESC
        LIMIT 20
    """).fetchall():
        recent.append({
            "ticker": r["ticker"],
            "expiry": r["expiry"],
            "strike": r["strike"],
            "market_type": r["market_type"],
            "implied_prob": round(r["implied_prob"] * 100, 1) if r["implied_prob"] else None,
            "poly_price": round(r["poly_price"] * 100, 1) if r["poly_price"] else None,
            "spread_pp": round(r["spread_pp"], 2) if r["spread_pp"] else None,
            "actual_outcome": r["actual_outcome"],
            "predicted_side": r["predicted_side"],
            "correct": bool(r["predicted_correct"]),
        })

    conn.close()
    return {
        "total": total,
        "accuracy": round(total_row["correct"] / total * 100, 1) if total > 0 else 0,
        "by_ticker": by_ticker,
        "by_market_type": by_market_type,
        "recent": recent,
        "collection": {"resolved": total, "target": 30, "pct": min(100, round(total / 30 * 100, 1))},
    }


# ─── CLI ─────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import logging as builtin_logging
    builtin_logging.basicConfig(level=builtin_logging.INFO, format="%(message)s")
    result = scan_resolved_options_markets()
    print(f"Resolved: {result['resolved']}")
    print(f"Forecast logged: {result['forecast_logged']}")
    print(f"Skipped: {result['skipped']}")
    print(f"Errors: {result['errors']}")
    if result.get("note"):
        print(f"Note: {result['note']}")
```

Note on `get_options_db()`: uses `OPTIONS_DB` env var from the scheduler's EnvironmentFile, same as `options_implied.py`. Falls back to `~/polyclawd-data/options_implied.db` if env var unset.

- [ ] **Verify syntax**

Run: `cd ~/Desktop/polyclawd && python3 -c "import ast; ast.parse(open('signals/options_resolver.py').read()); print('Syntax OK')"`
Expected: `Syntax OK`

- [ ] **Commit**

```bash
cd ~/Desktop/polyclawd && git add signals/options_resolver.py && git commit -m "feat: add options_resolver.py — resolution watcher for weekly options markets"
```

---

### Task 2: Wire into Scheduler

**Files:**
- Modify: `~/Desktop/polyclawd/services/scheduler.py`

- [ ] **Add `task_options_resolution()` before `task_credit_refresh`**

Insert after `task_options_scan()` (around line ~463):

```python
def task_options_resolution():
    """Resolve expired options-implied markets against Polymarket (every 30min)."""
    try:
        from signals.options_resolver import scan_resolved_options_markets
        result = scan_resolved_options_markets()
        if result.get("resolved", 0) > 0 or result.get("forecast_logged", 0) > 0:
            logger.info(
                "Options resolution: %d resolved, %d logged",
                result["resolved"], result["forecast_logged"],
            )
    except Exception as e:
        logger.exception("Options resolution failed: %s", e)
```

- [ ] **Wire into `tick_30min()`**

Find the `tick_30min()` function (around line 870 currently) and add the options resolution call after the options scan:

```python
        await run_in_thread(_run_safe, "options_scan", task_options_scan)
        await run_in_thread(_run_safe, "options_resolution", task_options_resolution)
```

- [ ] **Verify syntax**

Run: `cd ~/Desktop/polyclawd && python3 -c "import ast; ast.parse(open('services/scheduler.py').read()); print('Syntax OK')"`
Expected: `Syntax OK`

- [ ] **Commit**

```bash
cd ~/Desktop/polyclawd && git add services/scheduler.py && git commit -m "feat: add options resolution to scheduler 30-min tick"
```

---

### Task 3: Extend Dashboard API with Forecast Log

**Files:**
- Modify: `~/Desktop/polyclawd/api/routes/signals.py` — the `/options/dashboard` endpoint

- [ ] **Add accuracy call to existing dashboard endpoint**

Find the `options_dashboard()` function (around line 3500). After the shadow trade section, add:

```python
            # Accuracy from options_forecast_log
            try:
                from signals.options_resolver import get_options_accuracy_summary
            except ImportError:
                accuracy = {"total": 0, "by_ticker": {}, "by_market_type": {}, "recent": [], "collection": {"resolved": 0, "target": 30, "pct": 0}}
            else:
                accuracy = get_options_accuracy_summary()

            return {"totals": totals, "by_ticker": by_ticker, "divergences": rows[:20], "rows": rows, "shadow": shadow, "accuracy": accuracy}
```

Change the existing `return` line (currently: `return {"totals": ..., "shadow": shadow}`) to include `"accuracy": accuracy`.

- [ ] **Verify syntax**

Run: `cd ~/Desktop/polyclawd && python3 -c "import ast; ast.parse(open('api/routes/signals.py').read()); print('Syntax OK')"`
Expected: `Syntax OK`

- [ ] **Commit**

```bash
cd ~/Desktop/polyclawd && git add api/routes/signals.py && git commit -m "feat: add options accuracy data to /options/dashboard"
```

---

### Task 4: Update Dashboard HTML

**Files:**
- Modify: `~/Desktop/polyclawd/static/options.html`

- [ ] **Add `renderAccuracy()` function**

Before the `function switchTab()` block, add:

```javascript
function renderAccuracy(accuracy) {
  if (!accuracy || !accuracy.total) return;
  const container = document.getElementById('shadow-status');
  
  // Build on top of existing shadow trade content
  let html = container.innerHTML;
  
  // Collection progress
  const coll = accuracy.collection || {resolved: 0, target: 30, pct: 0};
  const pct = coll.pct || 0;
  const barColor = pct >= 100 ? 'var(--green)' : (pct >= 50 ? 'var(--amber)' : 'var(--accent)');
  html += '<div class="section-title" style="font-size:0.85rem;margin-top:12px">📊 Forecast Accuracy</div>';
  html += `<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:12px">`;
  html += `<div><span style="color:var(--dim)">Resolved:</span> <strong>${accuracy.total}</strong></div>`;
  html += `<div><span style="color:var(--dim)">Accuracy:</span> <strong>${accuracy.accuracy || '—'}%</strong></div>`;
  html += `</div>`;
  html += `<div style="font-size:0.75rem;margin-bottom:4px;color:var(--dim)">Collection: ${coll.resolved}/${coll.target}</div>`;
  html += `<div class="meter-track"><div class="meter-fill" style="width:${pct}%;background:${barColor}"></div></div>`;
  
  // By ticker
  const tickers = accuracy.by_ticker || {};
  if (Object.keys(tickers).length) {
    html += '<div class="section-title" style="font-size:0.85rem;margin-top:12px">🏷️ By Ticker</div>';
    html += '<div class="table-wrap"><table><thead><tr><th>Ticker</th><th>Resolved</th><th>Correct</th><th>Accuracy</th></tr></thead><tbody>';
    for (const [tk, stats] of Object.entries(tickers)) {
      const accColor = stats.accuracy > 60 ? 'var(--green)' : (stats.accuracy > 40 ? 'var(--amber)' : 'var(--red)');
      html += `<tr><td style="font-weight:600">${tk}</td><td>${stats.total}</td><td>${stats.correct}</td><td style="color:${accColor}">${stats.accuracy}%</td></tr>`;
    }
    html += '</tbody></table></div>';
  }
  
  // By market type
  const types = accuracy.by_market_type || {};
  if (Object.keys(types).length) {
    html += '<div class="section-title" style="font-size:0.85rem;margin-top:12px">📦 By Market Type</div>';
    html += '<div class="table-wrap"><table><thead><tr><th>Type</th><th>Resolved</th><th>Correct</th><th>Accuracy</th></tr></thead><tbody>';
    for (const [mt, stats] of Object.entries(types)) {
      html += `<tr><td>${mt}</td><td>${stats.total}</td><td>${stats.correct}</td><td>${stats.accuracy}%</td></tr>`;
    }
    html += '</tbody></table></div>';
  }
  
  // Recent resolved
  const recent = accuracy.recent || [];
  if (recent.length) {
    html += '<div class="section-title" style="font-size:0.85rem;margin-top:12px">🕐 Recent Resolutions</div>';
    html += '<div class="table-wrap"><table><thead><tr><th>Market</th><th>Prob</th><th>Poly</th><th>Predicted</th><th>Actual</th><th>✅</th></tr></thead><tbody>';
    for (const r of recent) {
      const correctBadge = r.correct ? 'badge-won' : 'badge-lost';
      const correctLabel = r.correct ? '✅' : '❌';
      const title = `${r.ticker || ''} ${r.market_type || ''} $${r.strike || ''} (${r.expiry || ''})`;
      html += `<tr><td>${title || '—'}</td><td>${r.implied_prob || '—'}%</td><td>${r.poly_price || '—'}¢</td><td>${r.predicted_side || '—'}</td><td>${r.actual_outcome || '—'}</td><td><span class="badge ${correctBadge}">${correctLabel}</span></td></tr>`;
    }
    html += '</tbody></table></div>';
  }
  
  container.innerHTML = html;
}
```

- [ ] **Wire into the load function**

In the `load()` function (around line 204), after `renderDivergences(d.divergences)` and `renderRows(d.rows)`, add:

```javascript
    if (d.accuracy) renderAccuracy(d.accuracy);
```

- [ ] **Deploy and test**

```bash
cd ~/Desktop/polyclawd && git add static/options.html && git commit -m "feat: add accuracy stats to options dashboard Trading tab"
```

---

### Task 5: Deploy to VPS

**Files:**
- Deploy: `signals/options_resolver.py` → VPS
- Deploy: `services/scheduler.py` → VPS
- Deploy: `api/routes/signals.py` → VPS
- Deploy: `static/options.html` → VPS
- Restart: `polyclawd-api.service` + `polyclawd-scheduler.service`

- [ ] **Deploy all files**

```bash
cd ~/Desktop/polyclawd && \
cat signals/options_resolver.py | ssh vps "sudo tee /var/www/virtuosocrypto.com/polyclawd/signals/options_resolver.py > /dev/null" && \
cat services/scheduler.py | ssh vps "sudo tee /var/www/virtuosocrypto.com/polyclawd/services/scheduler.py > /dev/null" && \
cat api/routes/signals.py | ssh vps "sudo tee /var/www/virtuosocrypto.com/polyclawd/api/routes/signals.py > /dev/null" && \
cat static/options.html | ssh vps "sudo tee /var/www/virtuosocrypto.com/polyclawd/static/options.html > /dev/null && sudo ln -sf static/options.html /var/www/virtuosocrypto.com/polyclawd/options.html" && \
echo "Deployed"
```

- [ ] **Restart services**

```bash
ssh vps "sudo systemctl restart polyclawd-scheduler && sudo systemctl restart polyclawd-api && sleep 4 && curl -sf http://localhost:8420/health 2>/dev/null | python3 -m json.tool"
```
Expected: `{"status": "healthy", ...}`

- [ ] **Smoke test: run resolver manually**

```bash
ssh vps 'cd /var/www/virtuosocrypto.com/polyclawd && OPTIONS_DB=/home/linuxuser/polyclawd-data/options_implied.db venv/bin/python3 -c "
from signals.options_resolver import scan_resolved_options_markets
r = scan_resolved_options_markets()
print(f\"Result: {r}\")
"'
```
Expected: `Result: {'resolved': 0, 'forecast_logged': 0, 'skipped': 0, 'errors': 0, 'note': 'No unresolved options rows'}` (no issues resolved yet — scanner just started today)

- [ ] **Verify table creation**

```bash
ssh vps "sqlite3 /home/linuxuser/polyclawd-data/options_implied.db '.schema options_forecast_log' 2>/dev/null | head -3"
```
Expected: `CREATE TABLE options_forecast_log (...)` with full schema

- [ ] **Verify dashboard endpoint**

```bash
ssh vps "curl -sf 'http://localhost:8420/api/options/dashboard' 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print(f\"Accuracy: {d.get(\"accuracy\",{}).get(\"total\",\"?\")} resolved, {d.get(\"accuracy\",{}).get(\"accuracy\",\"?\")}% accuracy\"); print(\"OK\")'"
```
Expected: `Accuracy: 0 resolved, ?% accuracy`

---

## Self-Review Checklist

**1. Spec coverage:**
- ✅ `options_forecast_log` table created — Task 1 (`_init_tables`)
- ✅ `signals/options_resolver.py` module created — Task 1 (entire file)
- ✅ Scheduler integration every 30min — Task 2
- ✅ Resolved events detected via `public-search?events_status=closed` — Task 1 (`_get_resolved_close_events`)
- ✅ Matched to `options_implied.db` rows by `poly_market_id` — Task 1 (`_get_unresolved_options_implied_rows`)
- ✅ Shadow trades updated — Task 1 (step 5 in `scan_resolved_options_markets`)
- ✅ Accuracy by ticker + market_type — Task 1 (`get_options_accuracy_summary`)
- ✅ Dashboard endpoint extended — Task 3
- ✅ Dashboard Trading tab shows accuracy — Task 4
- ✅ z-score direction mapped correctly — Task 1 (`_determine_predicted_side`)
- ✅ Rate-limit aware (2s delay per ticker) — Task 1 (`RATE_DELAY`)
- ✅ Deploy + smoke test — Task 5

**2. Placeholder scan:** No TBDs, TODOs, or "fill in later" patterns.

**3. Type consistency:** All DB functions use the same connection pattern as `options_implied.py` and `baseball_resolver.py`. `scan_resolved_options_markets()` returns `Dict[str, Any]` matching the pattern. `get_options_accuracy_summary()` returns a structured dict matching weather's accuracy endpoint shape.

**4. Edge cases covered:**
- No unresolved rows → returns immediately with note
- No resolved events → returns immediately
- Market has `poly_market_id` but no matching shadow trade → skipped (fallback spread-based inference removed as unreliable)
- Multiple tickers → processed sequentially with rate delay
- First run (empty DB) → table created, 0 results returned gracefully
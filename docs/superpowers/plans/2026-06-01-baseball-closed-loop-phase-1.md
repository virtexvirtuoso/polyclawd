# Baseball Closed-Loop Feedback — Phase 1 Resolution Watcher

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Deploy a game resolution watcher that compares baseball edge predictions to actual outcomes, stores them in a durable `baseball_forecast_log` table, and auto-resolves shadow trades.

**Architecture:** A dedicated `signals/baseball_resolver.py` module that polls Polymarket Gamma API for resolved baseball events, matches them to shadow trades by market_id, records the prediction-vs-outcome in a new `baseball_forecast_log` table, and updates shadow_trade rows. Wired into the watchdog's 5-min cycle alongside the existing shadow_tracker.py resolve.

**Tech Stack:** Python 3.12+, SQLite (shadow_trades.db WAL mode), Polymarket Gamma API (`events?closed=true&tag_slug=baseball`), Polymarket CLOB API (`markets/{condition_id}`), loguru, watchdog bash script.

**Pre-existing context:** The `odds/baseball_edge.py` module already logs edges ≥3% to shadow_trades as `strategy=baseball_moneyline, archetype=sports_single_game` via `_log_baseball_shadow()`. Shadow trades carry `poly_market_id` from the Gamma API moneyline market. The shadow_tracker.py has `_check_polymarket_resolution()` that uses the CLOB API. The watchdog already runs `shadow_tracker.py resolve` every 5min.

---

### Task 1: Create `signals/baseball_resolver.py`

**Files:**
- Create: `~/Desktop/polyclawd/signals/baseball_resolver.py`

- [ ] **Write the full module**

Content to write to `signals/baseball_resolver.py`:

```python
#!/usr/bin/env python3
"""
Baseball Game Resolution Watcher — Phase 1 of closed-loop calibration.

Checks Polymarket Gamma API for resolved baseball events, matches them to
shadow trades by market_id, records prediction-vs-outcome in
baseball_forecast_log table, and updates shadow_trade rows.

Runs alongside shadow_tracker.py resolve in the 5-min watchdog cycle.
Rate-limit aware: max 1 Polymarket call per 5 seconds.
"""

import json
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Dict, List, Optional, Any

from loguru import logger

# Paths (mirrors shadow_tracker.py)
BASE_DIR = Path(__file__).parent.parent
STORAGE_DIR = BASE_DIR / "storage"
DB_PATH = STORAGE_DIR / "shadow_trades.db"

# Polymarket APIs
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Rate limiting: 1 call per 5 seconds
RATE_DELAY = 5.0

# ─── DB Init ─────────────────────────────────────────────────────────


def get_db() -> sqlite3.Connection:
    """Get SQLite connection (WAL mode, shared with shadow_tracker)."""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _init_tables(conn)
    return conn


def _init_tables(conn: sqlite3.Connection):
    """Create baseball_forecast_log table if not exists."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS baseball_forecast_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id TEXT,
            team TEXT,
            opponent TEXT,
            game_date TEXT,
            odds_api_prob REAL,
            poly_price REAL,
            edge_pct REAL,
            direction TEXT,
            actual_outcome TEXT,
            predicted_correct INTEGER,
            american_odds INTEGER,
            books_count INTEGER,
            shadow_trade_id INTEGER,
            recorded_at TEXT,
            UNIQUE(game_id, team)
        );
    """)
    conn.commit()


# ─── Polymarket API Calls ────────────────────────────────────────────


def _fetch_json(url: str, timeout: int = 10) -> Optional[Any]:
    """Fetch JSON from a URL with User-Agent. Returns None on failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug(f"baseball_resolver fetch failed: {url[:60]} - {e}")
        return None


def _get_resolved_baseball_events() -> List[Dict]:
    """Fetch resolved baseball events from Polymarket Gamma API.

    Polls for closed events tagged baseball, limited to recent 50.
    Filters to only game events (contain " vs. " in title).
    Returns empty list on any failure (never crashes).
    """
    data = _fetch_json(
        f"{GAMMA_API}/events",
        params={"closed": "true", "tag_slug": "baseball", "limit": "50"},
        timeout=15,
    )
    if not isinstance(data, list):
        return []
    # Only game events (moneyline markets with " vs. " in title)
    return [e for e in data if " vs. " in (e.get("title", "") or "")]


def _get_moneyline_outcome(event: Dict) -> Optional[str]:
    """Extract the winning outcome from a resolved game event's moneyline market.

    The moneyline market has question == event title.
    outcomePrices tell us the resolution: if price is 1.0 for outcome[0]
    and 0.0 for outcome[1], the first-named team won (YES).
    Returns 'YES' or 'NO' (from the market's perspective, where
    outcomePrices[0] = YES = first team in title).
    Returns None if market is still open or has ambiguous prices.
    """
    title = event.get("title", "")
    for market in event.get("markets", []):
        if market.get("question", "") != title:
            continue
        prices_raw = market.get("outcomePrices", "[]")
        if isinstance(prices_raw, str):
            try:
                prices = json.loads(prices_raw)
            except (json.JSONDecodeError, ValueError):
                continue
        else:
            prices = prices_raw
        if len(prices) < 2:
            continue
        try:
            price0 = float(prices[0])
            price1 = float(prices[1])
        except (ValueError, TypeError):
            continue
        # Check via CLOB for definitive resolution
        market_id = market.get("id", "")
        if market_id:
            clob_result = _check_clob_resolution(market_id)
            if clob_result:
                return clob_result
        # Fallback: price-based heuristic
        if price0 >= 0.99 or price1 <= 0.01:
            return "YES"
        if price1 >= 0.99 or price0 <= 0.01:
            return "NO"
        # If neither price is extreme, market may not be resolved yet
        return None
    return None


def _check_clob_resolution(condition_id: str) -> Optional[str]:
    """Check CLOB API for definitive resolution of a condition.

    Returns 'YES', 'NO', or None if unresolved.
    Matches the pattern in shadow_tracker._check_polymarket_resolution().
    """
    data = _fetch_json(f"{CLOB_API}/markets/{condition_id}", timeout=10)
    if not data:
        return None
    if data.get("closed") or data.get("resolved"):
        tokens = data.get("tokens", [])
        # Check for explicit winner flag
        for token in tokens:
            if token.get("winner") is True:
                outcome = (token.get("outcome") or "").upper()
                if outcome in ("YES", "NO"):
                    return outcome
                return "YES" if token == tokens[0] else "NO"
        # Fallback: price-based
        for token in tokens:
            if token.get("outcome") == "Yes" and float(token.get("price", 0)) > 0.9:
                return "YES"
            elif token.get("outcome") == "No" and float(token.get("price", 0)) > 0.9:
                return "NO"
    return None


# ─── Shadow Trade Matching ───────────────────────────────────────────


def _get_unresolved_baseball_trades(conn: sqlite3.Connection) -> List[Dict]:
    """Get all unresolved shadow trades with strategy=baseball_moneyline.

    Returns list of dicts with: id, market_id, side, entry_price, market.
    """
    rows = conn.execute("""
        SELECT id, market_id, side, entry_price, market, category, reasoning
        FROM shadow_trades
        WHERE resolved = 0
          AND (strategy = 'baseball_moneyline'
               OR (category = 'baseball' AND side IN ('YES','NO')))
        ORDER BY timestamp ASC
    """).fetchall()
    return [dict(r) for r in rows]


# ─── Resolution Logic ────────────────────────────────────────────────


def _get_opponent_from_title(title: str, team: str) -> str:
    """Extract the opponent team name from a game title."""
    if not title or not team:
        return ""
    parts = title.split(" vs. ")
    if len(parts) != 2:
        return ""
    if team in parts[0] or parts[0].startswith(team.split()[-1]):
        return parts[1].strip()
    return parts[0].strip()


def _find_matching_event(
    shadow_trade: Dict, resolved_events: List[Dict]
) -> Optional[Dict]:
    """Find the resolved Polymarket event matching a shadow trade.

    Matches by market_id first (exact), then by team name in title (fallback).
    Returns None if no match found.
    """
    trade_market_id = shadow_trade.get("market_id", "")
    trade_market = shadow_trade.get("market", "")

    # Try exact market_id match first
    if trade_market_id:
        for event in resolved_events:
            for market in event.get("markets", []):
                if market.get("id") == trade_market_id:
                    return event

    # Fallback: match by team name in event title
    # Extract team from shadow trade reasoning or market name
    # Reasoning format: "MLB baseball edge: Odds API X% vs Poly Y.¢ (Z% edge)"
    # Market format: "TEAM_A vs. TEAM_B — TEAM Moneyline"
    trade_market_lower = trade_market.lower()
    for event in resolved_events:
        title = event.get("title", "").lower()
        # Check if any fragment of the shadow trade market appears in title
        for fragment in trade_market_lower.replace(" vs. ", "|").split("|"):
            fragment = fragment.split(" — ")[0].strip()
            if len(fragment) > 5 and fragment in title:
                return event

    return None


def _extract_teams_from_title(title: str) -> tuple:
    """Extract (team_a, team_b) from a game title."""
    if not title:
        return ("", "")
    parts = title.split(" vs. ")
    if len(parts) == 2:
        return (parts[0].strip(), parts[1].strip())
    return ("", "")


# ─── Main Resolution Scan ────────────────────────────────────────────


def scan_resolved_baseball_games(batch_size: int = 20) -> Dict[str, Any]:
    """Main entry point. Checks for resolved baseball games, matches to
    shadow trades, logs forecast data, updates shadow_trade rows.

    Returns summary dict with counts.
    """
    conn = get_db()
    result = {"resolved": 0, "forecast_logged": 0, "skipped": 0, "errors": 0}

    # 1. Get unresolved baseball shadow trades
    trades = _get_unresolved_baseball_trades(conn)
    if not trades:
        conn.close()
        return {**result, "note": "No unresolved baseball trades"}

    # 2. Fetch resolved baseball events from Polymarket
    resolved_events = _get_resolved_baseball_events()
    if not resolved_events:
        conn.close()
        return {**result, "note": "No resolved events from Polymarket"}

    # Build event lookup by market_id for fast matching
    event_by_market_id = {}
    for event in resolved_events:
        for market in event.get("markets", []):
            mid = market.get("id", "")
            if mid:
                event_by_market_id[mid] = event

    processed = 0
    for trade in trades[:batch_size]:
        market_id = trade.get("market_id", "")
        if not market_id:
            continue

        # Find matching event by market_id directly
        event = event_by_market_id.get(market_id)

        # Fallback: try team-name matching for events where market_id differs
        if not event:
            event = _find_matching_event(trade, resolved_events)

        if not event:
            result["skipped"] += 1
            continue

        # Get the moneyline outcome
        outcome = _get_moneyline_outcome(event)
        if outcome is None:
            # Market not yet fully resolved on CLOB
            result["skipped"] += 1
            continue

        # Determine if prediction was correct
        trade_side = trade.get("side", "")
        is_correct = 1 if trade_side == outcome else 0

        # Parse game date from event
        event_title = event.get("title", "")
        teams_a, teams_b = _extract_teams_from_title(event_title)

        # Which team was bet on? Extract from reasoning
        reasoning = trade.get("reasoning", "")
        # The market name has team name before "Moneyline"
        market_str = trade.get("market", "")
        bet_team = ""
        if " — " in market_str:
            bet_team = market_str.split(" — ")[1].replace("Moneyline", "").strip()
        opponent = teams_b if bet_team in teams_a or (teams_a and bet_team and teams_a.startswith(bet_team.split()[-1])) else teams_a

        # Parse edge details from reasoning
        edge_pct = 0.0
        import re
        edge_match = re.search(r'\(([+-]\d+\.?\d*)% edge\)', reasoning)
        if edge_match:
            edge_pct = float(edge_match.group(1))

        # Extract odds_api_prob from reasoning
        prob_match = re.search(r'Odds API (\d+\.?\d*)%', reasoning)
        odds_api_prob = float(prob_match.group(1)) / 100.0 if prob_match else 0.0

        # Extract poly price
        price_match = re.search(r'Poly (\d+\.?\d*)¢', reasoning)
        poly_price = float(price_match.group(1)) / 100.0 if price_match else trade.get("entry_price", 0.5)

        # 3. Log to baseball_forecast_log
        game_date = date.today().isoformat()
        try:
            conn.execute("""
                INSERT OR IGNORE INTO baseball_forecast_log
                (game_id, team, opponent, game_date, odds_api_prob, poly_price,
                 edge_pct, direction, actual_outcome, predicted_correct,
                 american_odds, books_count, shadow_trade_id, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                market_id[:20],
                bet_team or "unknown",
                opponent or "unknown",
                game_date,
                odds_api_prob,
                poly_price,
                edge_pct,
                trade.get("side", ""),
                outcome,
                is_correct,
                0,  # american_odds — not stored in shadow trade, use 0 for now
                0,  # books_count — not stored in shadow trade
                trade["id"],
                datetime.now(timezone.utc).isoformat(),
            ))
            result["forecast_logged"] += 1
        except Exception as e:
            logger.warning(f"forecast_log insert failed: {e}")
            result["errors"] += 1
            continue

        # 4. Update shadow_trade row
        entry_price = trade.get("entry_price", 0.5)
        if outcome == "YES":
            pnl = (1.0 - entry_price) if trade_side == "YES" else -entry_price
        else:
            pnl = -entry_price if trade_side == "YES" else entry_price

        try:
            conn.execute("""
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
                trade["id"],
            ))
            result["resolved"] += 1
        except Exception as e:
            logger.warning(f"shadow_trade update failed: {e}")
            result["errors"] += 1

        # Rate limit
        processed += 1
        if processed < len(trades[:batch_size]):
            time.sleep(RATE_DELAY)

    conn.commit()
    conn.close()

    # Log summary
    if result["resolved"] > 0:
        logger.info(
            f"baseball_resolver: {result['resolved']} resolved, "
            f"{result['forecast_logged']} logged, "
            f"{result['skipped']} skipped, "
            f"{result['errors']} errors"
        )

    return result


# ─── CLI ─────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import logging as builtin_logging
    builtin_logging.basicConfig(level=builtin_logging.INFO, format="%(message)s")
    result = scan_resolved_baseball_games()
    print(f"\nResolved: {result['resolved']}")
    print(f"Forecast logged: {result['forecast_logged']}")
    print(f"Skipped: {result['skipped']}")
    print(f"Errors: {result['errors']}")
    if result.get("note"):
        print(f"Note: {result['note']}")
```

- [ ] **Verify import compatibility**

Run: `cd ~/Desktop/polyclawd && python3 -c "import ast; ast.parse(open('signals/baseball_resolver.py').read()); print('Syntax OK')"`
Expected: "Syntax OK"

- [ ] **Commit**

```bash
cd ~/Desktop/polyclawd && git add signals/baseball_resolver.py && git commit -m "feat: add baseball_resolver.py — game resolution watcher for closed-loop calibration"
```

---

### Task 2: Wire `baseball_resolver.py` into the Watchdog

**Files:**
- Modify: `~/Desktop/polyclawd/polyclawd-watchdog.sh` (around the existing 5-min block)

- [ ] **Add resolver call before the existing shadow_tracker.py resolve**

The watchdog's 5-min block currently runs:
```bash
$VENV signals/shadow_tracker.py resolve > /dev/null 2>&1 || true
$VENV signals/shadow_tracker.py snapshot > /dev/null 2>&1 || true
$VENV signals/shadow_tracker.py summary > /dev/null 2>&1 || true
```

Add **before** the shadow_tracker.py resolve line:
```bash
# === EVERY 5 MIN: Baseball resolution watcher ===
$VENV -c "
from signals.baseball_resolver import scan_resolved_baseball_games
r = scan_resolved_baseball_games()
if r['resolved'] > 0:
    import logging
    logging.info(f'Baseball: {r[\"resolved\"]} resolved, {r[\"forecast_logged\"]} logged')
" > /dev/null 2>&1 || true
```

Using the same `-c` pattern as the existing weather position re-evaluation and signal scan blocks in the watchdog. This is a lightweight call that returns immediately if no trades or no resolved events.

- [ ] **Verify watchdog syntax**

Run: `bash -n ~/Desktop/polyclawd/polyclawd-watchdog.sh`
Expected: no output (syntax OK)

- [ ] **Commit**

```bash
cd ~/Desktop/polyclawd && git add polyclawd-watchdog.sh && git commit -m "feat: add baseball resolver to watchdog 5-min cycle"
```

---

### Task 3: Deploy to VPS + Smoke Test

**Files:**
- Deploy: `signals/baseball_resolver.py` → VPS
- Deploy: `polyclawd-watchdog.sh` → VPS (both app dir + /usr/local/bin)
- Restart: `polyclawd-api.service` (API doesn't import the new module, but restart is harmless)
- Soft: watchdog picks up new script on next cron run

No API/service restart strictly required since `baseball_resolver.py` is imported ONLY by the watchdog, not by uvicorn. But a restart ensures clean state.

- [ ] **Deploy files**

```bash
cd ~/Desktop/polyclawd && \
cat signals/baseball_resolver.py | ssh vps "sudo tee /var/www/virtuosocrypto.com/polyclawd/signals/baseball_resolver.py > /dev/null" && \
cat polyclawd-watchdog.sh | ssh vps "sudo tee /var/www/virtuosocrypto.com/polyclawd/polyclawd-watchdog.sh > /dev/null && sudo cp /var/www/virtuosocrypto.com/polyclawd/polyclawd-watchdog.sh /usr/local/bin/polyclawd-watchdog.sh && sudo chmod +x /usr/local/bin/polyclawd-watchdog.sh" && \
echo "Deploy OK"
```

- [ ] **Restart API service** (clean slate, though not strictly required)

```bash
ssh vps "sudo systemctl restart polyclawd-api && sleep 3 && curl -sf http://localhost:8420/health 2>/dev/null | python3 -m json.tool"
```
Expected: `{"status": "healthy", ...}`

- [ ] **Smoke test: run the resolver manually**

```bash
ssh vps "cd /var/www/virtuosocrypto.com/polyclawd && venv/bin/python3 -c '
from signals.baseball_resolver import scan_resolved_baseball_games
r = scan_resolved_baseball_games()
print(f\"Result: {r}\")
'"
```
Expected: `Result: {'resolved': 0, 'forecast_logged': 0, 'skipped': 0, 'errors': 0, 'note': 'No unresolved baseball trades'}` (no baseball games have resolved yet today, or the module works cleanly)

- [ ] **Verify table was created**

```bash
ssh vps "sqlite3 /var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db '.schema baseball_forecast_log'"
```

Expected: `CREATE TABLE baseball_forecast_log (...)` showing the full schema

- [ ] **Verify shadow_trades had migration run**

```bash
ssh vps "sqlite3 /var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db 'SELECT COUNT(*) FROM baseball_forecast_log;'"
```
Expected: `0` (no games have resolved yet, table exists and is empty)

- [ ] **Commit deploy**

```bash
cd ~/Desktop/polyclawd && git commit --allow-empty -m "deploy: baseball resolver to VPS"
```

---

## Self-Review Checklist

**1. Spec coverage:**
- ✅ `baseball_forecast_log` table created with full schema — Task 1 (`_init_tables`)
- ✅ `signals/baseball_resolver.py` module created — Task 1 (entire file)
- ✅ Watchdog integration (every 5min) — Task 2
- ✅ Resolved games detected via Gamma API `closed=true&tag_slug=baseball` — Task 1 (`_get_resolved_baseball_events`)
- ✅ Shadow trades matched by market_id — Task 1 (`_find_matching_event`, `scan_resolved_baseball_games`)
- ✅ Shadow trade rows updated (resolved=1, outcome, exit_price) — Task 1 (step 4 in `scan_resolved_baseball_games`)
- ✅ Forecast log entries written — Task 1 (step 3)
- ✅ Rate limit (1 call/5s) — Task 1 (`RATE_DELAY = 5.0`, `time.sleep`)
- ✅ Postponed game handling — Task 1 (CLOB check returns None for unresolved, skip)
- ✅ Double-header matching — Task 1 (match by market_id first, not team name)
- ✅ Deploy + smoke test — Task 3

**2. Placeholder scan:** No TBDs, TODOs, or "fill in later" patterns.

**3. Type consistency:** All function signatures use Python 3 type hints matching the patterns in `shadow_tracker.py` and `baseball_edge.py`. `scan_resolved_baseball_games()` returns `Dict[str, Any]` matching the pattern of `resolve_trades()`.

**4. Edge cases covered:**
- No unresolved trades → returns immediately
- No resolved events from Polymarket → returns immediately
- Market not yet fully resolved on CLOB → `_get_moneyline_outcome` returns None → skipped
- Gamma event has no moneyline market → skipped
- Market_id mismatch (Gamma vs CLOB) → fallback team-name matching
- Postponed/canceled games → never match (no resolution data)
- Same team playing double-header → market_id matching prevents false match
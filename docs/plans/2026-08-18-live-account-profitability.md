# Live Account Profitability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the live Polymarket account from an unmanaged −75.7% experiment into a gated canary that only trades validated strategies, at sane sizes, on honest books — with a pre-registered pass/fail gate before any re-funding.

**Architecture:** Fix the three broken ledgers so live results are measurable (Tasks 1, 2, 5); make the risk governor the single choke point that enforces a strategy allowlist and fraction-of-bankroll sizing on EVERY leg including maker legs (Tasks 3, 4); add a fill-reconciliation tripwire (Task 6); pre-register the canary gate before any new trade (Task 7); deploy with both-unit restart (Task 8).

**Tech Stack:** Python 3.12, sqlite3 (WAL), pytest (`.venv/bin/pytest`), Polymarket SDK (`polymarket.SecureClient` via `execution/clob_client.py`), PM data-api. Deploy = file-splice to VPS + restart `polyclawd-api` AND `polyclawd-scheduler`.

**Context an engineer needs (2026-08-18 investigation):**
- Live wallet = relayer signer EOA (env `POLYMARKET_WALLET_ADDRESS`) + SDK deposit wallet `0xa495c42d…` where funds/positions actually live. Collateral is **pUSD** now, not USDC.
- Ground truth: deposited $98.53, current $23.87 liquid + 2 stale-open positions; lifetime PnL −$74.66. Positions 8 and 12 (`live_positions`) WON and were redeemed on-chain ~Jul 15 (+$13.51, +$18.81) but are still `status='open'` — `position_sync`'s resolvers all miss redeemed-and-gone tokens.
- `governor.check()` is currently only called on the TAKER path (`live_executor.py:493`); maker legs bypass all caps.
- Live traded a K2-killed archetype (BTC `price_above`) because no strategy/archetype gate exists on the live path.
- Repo tree is dirty with parallel WIP — `git add` ONLY the files named in each task, never `git add -A`.
- Canonical tree: `~/Desktop/polyclawd`. All tests: `.venv/bin/pytest`. VPS tree: `/var/www/virtuosocrypto.com/polyclawd`.

**File map:**
| File | Role in this plan |
|---|---|
| `scripts/position_sync.py` | Task 2 (close redeemed-absent positions), Task 6 (fill reconciliation) |
| `execution/risk_governor.py` | Task 3 (Rule 0 allowlist), Task 4 (fractional cap) |
| `execution/live_config.py` | Task 3 (allowlist config), Task 4 (frac config) |
| `execution/live_executor.py` | Task 3 (governor check at intent entry, incl. maker path) |
| `execution/live_position_tracker.py` | Task 5 (realized from positions ledger) |
| `api/routes/live.py` | Task 5 (real collateral balance) |
| `tests/test_position_sync_redeemed.py` | new — Task 2 |
| `tests/test_governor_allowlist.py` | new — Tasks 3, 4 |
| `tests/test_realized_pnl_source.py` | new — Task 5 |
| `tests/test_fill_reconciliation.py` | new — Task 6 |
| vault `02-Projects/Polyclawd/Scopes/Live-Canary-Gate-2026-08-18.md` | new — Task 7 (pre-registration) |

---

### Task 1: Migration — close the two redeemed winners (data only, VPS)

No code. Backup exists: `/home/linuxuser/backups/shadow_trades-pre-entryfix-2026-08-18.db` (pre-dates today's rows? No — it's from this morning and both rows are older: fine).

- [ ] **Step 1: Read the two rows' fees**

```bash
ssh vps "sqlite3 -readonly /var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db \
  \"SELECT id, shares, entry_price, cost_usd, fee_paid_total FROM live_positions WHERE id IN (8,12);\""
```
Expected: `8|13.51|0.74|9.9974|<feeA>` and `12|18.81|0.4|7.524|<feeB>` (fees likely 0.0).

- [ ] **Step 2: Close both as redeemed wins**

pnl = (1 − entry_price) × shares − fee_paid_total. With fee=0: pos 8 → +3.5126, pos 12 → +11.286.

```bash
ssh vps "sqlite3 /var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db \"
UPDATE live_positions SET status='closed',
  closed_at='2026-07-15T00:00:00+00:00', exit_price=1.0,
  pnl=ROUND((1.0-entry_price)*shares - COALESCE(fee_paid_total,0), 4),
  close_reason='redeemed_backfill_2026-08-18'
WHERE id IN (8,12) AND status='open';
SELECT id, status, pnl, close_reason FROM live_positions WHERE id IN (8,12);\""
```
Expected: both rows `closed`, pnl `3.5126` and `11.286`.

- [ ] **Step 3: Verify ledger now matches ground truth**

```bash
ssh vps "sqlite3 -readonly /var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db \
  \"SELECT ROUND(SUM(pnl),2) FROM live_positions WHERE status='closed';\""
```
Expected: `-72.16` (≈ −74.66 ground truth; residual ≈ June fees embedded in activity prices — record the number in the commit message).

- [ ] **Step 4: Trigger governor resync and confirm bankroll collapses to liquid**

`position_sync.run()` syncs bankroll = CLOB liquid + Σ open cost. With no open positions, bankroll should become ≈ $23.87 on its next 5-min scheduler tick.

```bash
sleep 360 && ssh vps "sqlite3 -readonly /var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db \
  \"SELECT ROUND(bankroll,2), ROUND(deployed_usd,2) FROM live_portfolio_state ORDER BY id DESC LIMIT 1;\""
```
Expected: `23.87|0.0` (±$0.05).

---

### Task 2: position_sync closes redeemed-absent positions

> [!warning] SPEC CORRECTED 2026-08-19 after code review verified against the live API.
> The original spec below joined REDEEM activity rows on `asset` — but real REDEEM rows carry `asset==""` (only `conditionId` + `usdcSize`), so that join is a silent no-op. The implemented design (commit series on fix/odds-api-credit-leak) bridges REDEEM `(conditionId, outcomeIndex)` → token id via the wallet's TRADE rows, requires `usdcSize > 0`, books `pnl = payout − cost − fees` with `exit_price = payout/shares` (redemption ≠ win: zero-payout claim-alls exist), refuses to attribute when `|payout − shares|` exceeds tolerance, and treats a failed SDK/activity fetch as "unknown" (skip the heuristic that run) rather than "absent" — a transient outage must never fabricate wins. Tests exercise the real payload shape by stubbing urllib, not the helpers.

**The bug:** a redeemed position vanishes from `client.list_positions()`, then the fallback `get_market(hex(token_id))` fails for rows where `market_id` stores a token id → skipped forever.

**Files:**
- Modify: `scripts/position_sync.py` (inside `check_resolutions`, after `sdk_price_map` is built)
- Test: `tests/test_position_sync_redeemed.py` (new)

- [ ] **Step 1: Write the failing test**

```python
"""Redeemed-and-gone positions must close as wins via data-api REDEEM activity."""
import sqlite3
import pytest

from scripts import position_sync as ps


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.db"))
    c.execute("""CREATE TABLE live_positions (
        id INTEGER PRIMARY KEY, opened_at TEXT, market_id TEXT, market_slug TEXT,
        market_title TEXT, token_id TEXT, side TEXT, entry_price REAL, shares REAL,
        cost_usd REAL, status TEXT, closed_at TEXT, exit_price REAL, pnl REAL,
        close_reason TEXT, fee_paid_total REAL, archetype TEXT)""")
    c.execute("""INSERT INTO live_positions
        (id, opened_at, market_id, market_title, token_id, side, entry_price,
         shares, cost_usd, status, fee_paid_total)
        VALUES (8, '2026-07-14T01:37:00+00:00', 'tok123', 'Granby tennis',
                'tok123', 'BUY', 0.74, 13.51, 9.9974, 'open', 0.0)""")
    c.commit()
    return c


def test_redeemed_absent_position_closes_as_win(conn, monkeypatch):
    monkeypatch.setattr(ps, "_already_resolution_alerted", lambda pid: False)
    monkeypatch.setattr(ps, "_mark_resolution_alerted", lambda pid: None)
    monkeypatch.setattr(ps, "_sdk_token_price_map", lambda: {})  # token gone from SDK
    monkeypatch.setattr(ps, "_fetch_redeem_assets", lambda: {"tok123"})
    resolved = ps.check_resolutions(conn)
    assert len(resolved) == 1
    row = conn.execute("SELECT status, pnl, close_reason FROM live_positions WHERE id=8").fetchone()
    assert row[0] == "closed"
    assert row[1] == pytest.approx((1 - 0.74) * 13.51, abs=0.001)
    assert row[2] == "redeemed_detected"


def test_absent_without_redeem_activity_stays_open(conn, monkeypatch):
    monkeypatch.setattr(ps, "_already_resolution_alerted", lambda pid: False)
    monkeypatch.setattr(ps, "_sdk_token_price_map", lambda: {})
    monkeypatch.setattr(ps, "_fetch_redeem_assets", lambda: set())
    ps.check_resolutions(conn)
    assert conn.execute("SELECT status FROM live_positions WHERE id=8").fetchone()[0] == "open"
```

- [ ] **Step 2: Run it — expect FAIL** (`AttributeError: no attribute '_sdk_token_price_map'`)

```bash
cd ~/Desktop/polyclawd && .venv/bin/pytest tests/test_position_sync_redeemed.py -q
```

- [ ] **Step 3: Implement.** In `scripts/position_sync.py`:

(a) Extract the existing inline sdk_price_map build (inside `check_resolutions`) into a module-level function so tests can stub it:

```python
def _sdk_token_price_map() -> dict:
    """token_id -> (cur_price, redeemable) from SDK open positions. {} on failure."""
    out = {}
    try:
        from execution.clob_client import _get_client
        client = _get_client()
        for page in client.list_positions(size_threshold=0.001):
            for sdk_pos in page.items:
                t = str(getattr(sdk_pos, "token_id", "") or "")
                if t:
                    out[t] = (getattr(sdk_pos, "cur_price", None),
                              bool(getattr(sdk_pos, "redeemable", False)))
    except Exception as exc:
        logger.debug("position_sync: sdk position map failed: %s", exc)
    return out


def _fetch_redeem_assets() -> set:
    """Asset (token) ids with a REDEEM row in our wallet's data-api activity."""
    try:
        url = f"https://data-api.polymarket.com/activity?user={_DEPOSIT_WALLET}&limit=500"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        acts = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
        return {str(a.get("asset") or "") for a in acts if a.get("type") == "REDEEM"}
    except Exception as exc:
        logger.debug("position_sync: redeem activity fetch failed: %s", exc)
        return set()
```

(b) In `check_resolutions`, replace the inline map build with `sdk_price_map = _sdk_token_price_map()` (called ONCE before the row loop, not per row), and fetch `redeem_assets = _fetch_redeem_assets()` once. Then, per row, BEFORE the existing get_market/Gamma fallback, add:

```python
        token_id = str(row[3] or market_id)
        if token_id not in sdk_price_map and token_id in redeem_assets:
            # Position gone from wallet + a REDEEM event exists → it WON and
            # was redeemed on-chain; the books never heard (pos 8/12, Jul 15).
            pnl = round((1.0 - entry_price) * shares - fee_total, 4)
            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE live_positions SET status='closed', closed_at=?, exit_price=1.0, "
                "pnl=?, close_reason='redeemed_detected' WHERE id=?",
                (now_iso, pnl, pos_id))
            conn.commit()
            resolved.append({"id": pos_id, "market_title": market_title, "pnl": pnl,
                             "entry_price": entry_price, "exit_price": 1.0, "shares": shares,
                             "opened_at": row[10], "result_emoji": "🏆", "result_label": "WIN (redeemed)"})
            _mark_resolution_alerted(pos_id)
            continue
```

- [ ] **Step 4: Run the new tests — expect PASS; then the adjacent suites**

```bash
.venv/bin/pytest tests/test_position_sync_redeemed.py tests/test_live_executor_title.py -q
```

- [ ] **Step 5: Commit** — `git add scripts/position_sync.py tests/test_position_sync_redeemed.py && git commit -m "fix(live): close redeemed-absent positions via data-api REDEEM activity"`

---

### Task 3: Strategy allowlist in the governor + governor check on EVERY leg

**Files:**
- Modify: `execution/live_config.py` (allowlist accessor)
- Modify: `execution/risk_governor.py` (Rule 0, before the KILL floor)
- Modify: `execution/live_executor.py` (check at intent entry — covers maker path; pass `category` into both check calls)
- Test: `tests/test_governor_allowlist.py` (new)

- [ ] **Step 1: Write the failing tests**

```python
"""Rule 0: live trades must carry an allowlisted strategy category."""
import sqlite3
import pytest

from execution import live_db, live_config
from execution.risk_governor import RiskGovernor


@pytest.fixture
def gov(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYCLAWD_LIVE_STRATEGY_ALLOWLIST",
                       "smart_wallet,baseball_total,soccer_match_3way")
    conn = live_db.connect(path=tmp_path / "t.db")
    g = RiskGovernor(conn, mode="LIVE")
    g.set_bankroll(100.0)
    return g


def test_allowlisted_strategy_passes_rule0(gov):
    d = gov.check({"size_usd": 5.0, "market_id": "m1", "category": "baseball_total"})
    assert "strategy_allowlist" not in d.reason


def test_unlisted_strategy_rejected(gov):
    d = gov.check({"size_usd": 5.0, "market_id": "m1", "category": "price_above"})
    assert d.allowed is False
    assert "strategy_allowlist" in d.reason


def test_missing_strategy_rejected_fail_closed(gov):
    d = gov.check({"size_usd": 5.0, "market_id": "m1"})
    assert d.allowed is False
    assert "strategy_allowlist" in d.reason


def test_empty_allowlist_env_blocks_everything(gov, monkeypatch):
    monkeypatch.setenv("POLYCLAWD_LIVE_STRATEGY_ALLOWLIST", "")
    d = gov.check({"size_usd": 5.0, "market_id": "m1", "category": "baseball_total"})
    assert d.allowed is False
```

- [ ] **Step 2: Run — expect FAIL** (`test_unlisted_strategy_rejected` gets `allowed=True`)

```bash
.venv/bin/pytest tests/test_governor_allowlist.py -q
```

- [ ] **Step 3: Implement.**

`execution/live_config.py` — add next to the other accessors:

```python
def live_strategy_allowlist() -> set:
    """Strategy categories allowed to touch real money. Empty set = trade NOTHING.
    Fail-closed by design: a strategy earns its slot via the canary gate doc
    (vault: Live-Canary-Gate-2026-08-18)."""
    raw = os.environ.get("POLYCLAWD_LIVE_STRATEGY_ALLOWLIST",
                         "smart_wallet,baseball_total,soccer_match_3way")
    return {s.strip() for s in raw.split(",") if s.strip()}
```

`execution/risk_governor.py` — in `check()`, immediately after `market_id` is read and BEFORE Rule 1:

```python
        # ── Rule 0: strategy allowlist (fail-closed) ────────────────────
        # The live account traded a K2-killed archetype in July because no
        # strategy gate existed on the live path. Missing category = reject.
        category = str(intent.get("category", "") or "")
        allowed_strategies = live_config.live_strategy_allowlist()
        if category not in allowed_strategies:
            return Decision(
                False,
                f"strategy_allowlist: category {category or '(missing)'} not in "
                f"{sorted(allowed_strategies)}",
            )
```

`execution/live_executor.py` — two changes:
1. The existing taker-path call at line ~493 gains the category:
```python
    decision = governor.check(
        {"size_usd": remainder_usd, "market_id": token_id, "token_id": token_id,
         "category": category}
    )
```
2. At the TOP of `execute_intent` (right after the `result` dict is initialised and the idempotency check), add an entry-check so MAKER legs are also governed:
```python
    entry_decision = governor.check(
        {"size_usd": size_usd, "market_id": token_id, "token_id": token_id,
         "category": category}
    )
    if not entry_decision.allowed:
        result["action"] = "dropped"
        result["reason"] = f"governor: {entry_decision.reason}"
        return result
```

- [ ] **Step 4: Run new tests + the executor/governor suites — expect PASS, no regressions**

```bash
.venv/bin/pytest tests/test_governor_allowlist.py tests/test_live_executor_title.py -q
.venv/bin/pytest tests/ -q -k "governor or executor or live" --tb=short | tail -5
```
Note: 26 pre-existing failures exist in the dirty tree (whale suite, smart_wallet_alert, weather_ensemble, options_implied, scorer_edge, sports_engines, security rate-limit) — compare against that baseline, not zero. If an EXISTING live-executor test fails because its intent lacks `category`, fix the TEST fixture to pass an allowlisted category (e.g. `category="weather"` intents must now be in the allowlist for those tests — set `POLYCLAWD_LIVE_STRATEGY_ALLOWLIST` in the test env, don't weaken Rule 0).

- [ ] **Step 5: Commit** — `git add execution/live_config.py execution/risk_governor.py execution/live_executor.py tests/test_governor_allowlist.py && git commit -m "feat(live): Rule 0 strategy allowlist + governor check on maker path"`

---

### Task 4: Sizing — fraction-of-bankroll cap

**Files:**
- Modify: `execution/live_config.py`, `execution/risk_governor.py` (Rule 3)
- Test: append to `tests/test_governor_allowlist.py`

- [ ] **Step 1: Write the failing test**

```python
def test_per_trade_cap_is_min_of_env_and_bankroll_fraction(gov, monkeypatch):
    monkeypatch.setenv("POLYCLAWD_WEATHER_PER_TRADE_CAP", "15.0")
    monkeypatch.setenv("POLYCLAWD_PER_TRADE_FRAC", "0.10")
    # bankroll fixture = 100 → frac cap = $10 < env $15 → effective cap $10
    d = gov.check({"size_usd": 12.0, "market_id": "m1", "category": "baseball_total"})
    assert d.allowed is False
    assert "per_trade_cap" in d.reason
    d = gov.check({"size_usd": 9.0, "market_id": "m1", "category": "baseball_total"})
    assert "per_trade_cap" not in d.reason
```

- [ ] **Step 2: Run — expect FAIL** ($12 ≤ env cap $15 so it currently passes Rule 3)

- [ ] **Step 3: Implement.**

`execution/live_config.py`:
```python
def per_trade_frac() -> float:
    """Per-trade cap as a fraction of current bankroll (default 10%).
    The Mariners trade was 46% of bankroll; one loss like that is fatal."""
    return _parse_float("POLYCLAWD_PER_TRADE_FRAC", "0.10")
```

`execution/risk_governor.py` Rule 3 — replace `cap = live_config.per_trade_cap()` with:
```python
        cap = min(live_config.per_trade_cap(),
                  self._bankroll * live_config.per_trade_frac())
```
(reason string unchanged — tests match on "per_trade_cap").

- [ ] **Step 4: Run — expect PASS** (`.venv/bin/pytest tests/test_governor_allowlist.py -q`)

- [ ] **Step 5: Commit** — `git add execution/live_config.py execution/risk_governor.py tests/test_governor_allowlist.py && git commit -m "feat(live): per-trade cap = min(env, 10% of bankroll)"`

---

### Task 5: One realized-PnL source + real collateral balance

**Files:**
- Modify: `execution/live_position_tracker.py` (`recompute_equity`, realized section ~line 183)
- Modify: `api/routes/live.py` (`get_live_portfolio_endpoint`, onchain_balance seed ~line 84)
- Test: `tests/test_realized_pnl_source.py` (new)

- [ ] **Step 1: Write the failing test**

```python
"""realized_pnl must come from the closed-positions ledger, not SELL fills
(resolution/manual closes never write SELL fills — the July closes proved it)."""
import sqlite3
import pytest

from execution import live_db
from execution import live_position_tracker as lpt


@pytest.fixture
def conn(tmp_path):
    return live_db.connect(path=tmp_path / "t.db")


def test_realized_comes_from_closed_positions(conn, monkeypatch):
    conn.execute(
        "INSERT INTO live_positions (opened_at, market_id, token_id, side, entry_price,"
        " shares, cost_usd, status, closed_at, exit_price, pnl, close_reason)"
        " VALUES ('2026-07-14T00:00:00+00:00','m1','t1','BUY',0.4,10,4.0,'closed',"
        " '2026-07-15T00:00:00+00:00',1.0,6.0,'resolution')")
    conn.commit()
    # no live_fills SELL rows at all — old formula would return 0.0
    monkeypatch.setattr(lpt, "get_orderbook", lambda tid: None, raising=False)
    snap = lpt.recompute_equity(conn, onchain_balance=23.87)
    assert snap["realized_pnl"] == pytest.approx(6.0)
```

- [ ] **Step 2: Run — expect FAIL** (`realized_pnl == 0.0`)

```bash
.venv/bin/pytest tests/test_realized_pnl_source.py -q
```

- [ ] **Step 3: Implement.** In `recompute_equity`, replace the SELL-fills realized block with:

```python
    # Realized P&L — authoritative source is the closed-positions ledger.
    # (Resolution + manual closes never write SELL fills; the SELL-fill sum
    # stays as a cross-check only.)
    cur = conn.execute(
        "SELECT COALESCE(SUM(pnl), 0.0) FROM live_positions WHERE status = 'closed'"
    )
    realized_pnl = float(cur.fetchone()[0])
    cur = conn.execute(
        "SELECT COALESCE(SUM(shares * (price - fair_price) - fee_paid), 0.0)"
        " FROM live_fills WHERE side = 'SELL'"
    )
    realized_from_fills = float(cur.fetchone()[0])
    if abs(realized_pnl - realized_from_fills) > 1.0 and realized_from_fills != 0.0:
        logger.warning(
            "recompute_equity: realized ledgers diverge — positions {} vs SELL-fills {}",
            realized_pnl, realized_from_fills)
```

In `api/routes/live.py` (~line 84), replace the circular seed:

```python
        # Real collateral (pUSD) via the SDK — the old seed read state.bankroll,
        # which is itself derived, making "onchain_balance" circular fiction.
        try:
            from execution.clob_client import _get_client
            raw = _get_client().get_balance_allowance(asset_type="COLLATERAL").balance
            onchain_balance = float(raw) / 1e6
        except Exception as exc:
            logger.warning("live/portfolio: balance fetch failed, falling back to state: %s", exc)
            state = live_db.get_state(conn)
            onchain_balance = float((state or {}).get("bankroll") or 0.0)
```

- [ ] **Step 4: Run — expect PASS; also run the live-route tests**

```bash
.venv/bin/pytest tests/test_realized_pnl_source.py -q
.venv/bin/pytest tests/ -q -k "live" --tb=short | tail -3
```

- [ ] **Step 5: Commit** — `git add execution/live_position_tracker.py api/routes/live.py tests/test_realized_pnl_source.py && git commit -m "fix(live): realized from positions ledger; real pUSD balance in portfolio endpoint"`

---

### Task 6: Fill-reconciliation tripwire (untracked-fill class)

The June Mariners position needed a manual `__untracked_fill_correction` — fills arrived that the books didn't record. Guard: compare data-api TRADE activity against `live_fills`, alert on drift.

**Files:**
- Modify: `scripts/position_sync.py` (new function + call in `run()`)
- Test: `tests/test_fill_reconciliation.py` (new)

- [ ] **Step 1: Write the failing test**

```python
"""data-api TRADE rows vs live_fills — drift means untracked fills."""
import sqlite3
import pytest

from scripts import position_sync as ps


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.db"))
    c.execute("""CREATE TABLE live_fills (id INTEGER PRIMARY KEY, ts TEXT,
        position_id INTEGER, order_id TEXT, side TEXT, liquidity TEXT,
        price REAL, shares REAL, usd REAL, fee_paid REAL,
        fair_price REAL, slippage_vs_fair REAL)""")
    c.commit()
    return c


def test_untracked_trade_activity_flags_drift(conn, monkeypatch):
    alerts = []
    monkeypatch.setattr(ps, "_fetch_trade_activity_usd",
                        lambda since_ts: (3, 25.0))  # 3 chain trades, $25
    monkeypatch.setattr(ps, "_alert_fill_drift", lambda msg: alerts.append(msg))
    drift = ps.check_fill_reconciliation(conn, since_ts=0)
    assert drift["chain_trades"] == 3 and drift["db_fills"] == 0
    assert alerts, "drift must alert"


def test_matching_counts_no_alert(conn, monkeypatch):
    conn.execute("INSERT INTO live_fills (ts, side, price, shares, usd, fee_paid)"
                 " VALUES ('2026-08-18T00:00:00+00:00','BUY',0.5,10,5.0,0)")
    conn.commit()
    alerts = []
    monkeypatch.setattr(ps, "_fetch_trade_activity_usd", lambda since_ts: (1, 5.0))
    monkeypatch.setattr(ps, "_alert_fill_drift", lambda msg: alerts.append(msg))
    ps.check_fill_reconciliation(conn, since_ts=0)
    assert alerts == []
```

- [ ] **Step 2: Run — expect FAIL** (`AttributeError: check_fill_reconciliation`)

- [ ] **Step 3: Implement** in `scripts/position_sync.py`:

```python
_FILL_RECON_TOLERANCE_USD = 1.0


def _fetch_trade_activity_usd(since_ts: int) -> tuple:
    """(count, total_usd) of TRADE rows in wallet activity newer than since_ts."""
    url = f"https://data-api.polymarket.com/activity?user={_DEPOSIT_WALLET}&limit=500"
    req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
    acts = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
    rows = [a for a in acts
            if a.get("type") == "TRADE" and int(a.get("timestamp") or 0) > since_ts]
    return len(rows), sum(float(a.get("usdcSize") or 0) for a in rows)


def _alert_fill_drift(msg: str) -> None:
    try:
        from scripts.alert_formatter import send_telegram
        send_telegram(msg)
    except Exception as exc:
        logger.warning("position_sync: fill-drift alert failed: %s", exc)


def check_fill_reconciliation(conn, since_ts: int = 0) -> dict:
    """Compare on-chain TRADE activity vs recorded live_fills since a cutoff.
    Untracked fills (June Mariners class) show up as chain > db."""
    try:
        chain_n, chain_usd = _fetch_trade_activity_usd(since_ts)
    except Exception as exc:
        logger.debug("position_sync: fill recon fetch failed: %s", exc)
        return {"error": str(exc)}
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(usd), 0) FROM live_fills"
    ).fetchone()
    db_n, db_usd = int(row[0]), float(row[1] or 0)
    drift = {"chain_trades": chain_n, "db_fills": db_n,
             "chain_usd": round(chain_usd, 2), "db_usd": round(db_usd, 2)}
    if chain_n != db_n or abs(chain_usd - db_usd) > _FILL_RECON_TOLERANCE_USD:
        _alert_fill_drift(
            "⚠️ LIVE FILL DRIFT — chain shows "
            f"{chain_n} trades (${chain_usd:,.2f}) vs {db_n} recorded fills "
            f"(${db_usd:,.2f}). Untracked fills? Check live_fills vs data-api activity.")
    return drift
```

Call it in `run()` right after `check_wallet_balance(conn)`, with a cutoff so June/July history (recorded before `live_fills` existed) doesn't false-alarm forever:

```python
        # Fill reconciliation from re-launch onward (2026-08-18T00:00:00Z);
        # earlier history predates the live_fills table and always drifts.
        check_fill_reconciliation(conn, since_ts=1787097600)
```
Note: with this cutoff, `_fetch_trade_activity_usd` must ALSO be the source for the DB side — change the SQL to `WHERE ts >= '2026-08-18T00:00:00+00:00'` so both sides use the same window:
```python
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(usd), 0) FROM live_fills WHERE ts >= '2026-08-18T00:00:00+00:00'"
    ).fetchone()
```
(Keep the test's `since_ts=0` behaviour working by making the SQL cutoff a parameter: `def check_fill_reconciliation(conn, since_ts=0, db_cutoff_iso="1970-01-01T00:00:00+00:00")` and pass `db_cutoff_iso="2026-08-18T00:00:00+00:00"` at the call site; the tests above then pass the default.)

- [ ] **Step 4: Run — expect PASS** (`.venv/bin/pytest tests/test_fill_reconciliation.py -q`)

- [ ] **Step 5: Commit** — `git add scripts/position_sync.py tests/test_fill_reconciliation.py && git commit -m "feat(live): fill reconciliation tripwire vs data-api activity"`

---

### Task 7: Pre-register the canary gate (vault doc — BEFORE any new live trade)

**Files:**
- Create (vault): `02-Projects/Polyclawd/Scopes/Live-Canary-Gate-2026-08-18.md` — stage with Write to scratchpad, then `~/bin/vault-write "02-Projects/Polyclawd/Scopes/Live-Canary-Gate-2026-08-18.md" < <staged>`

- [ ] **Step 1: Write the gate doc** with exactly this content (frontmatter per vault CLAUDE.md; status `active`; tags `[polyclawd, live-trading, gate, pre-registration]`):

```markdown
# Live Canary Gate — pre-registered 2026-08-18

> Written BEFORE the canary run. Numbers may not be adjusted after data arrives.

## Setup
- Bankroll: the existing $23.87 pUSD. No new deposits until PASS.
- Allowlist (env POLYCLAWD_LIVE_STRATEGY_ALLOWLIST): smart_wallet, baseball_total, soccer_match_3way.
- Sizing: min($15, 10% of bankroll) per trade → ~$2.40 at start.
- Duration: until 30 filled live trades on allowlisted strategies OR 2026-10-15, whichever first.

## PASS criteria (ALL required)
1. n ≥ 30 filled trades.
2. Realized live expectancy per trade ≥ (shadow expectancy for the same strategy mix − $0.02/share spread allowance).
3. Clean poly_delta_60 sample mean ≥ −0.02 (no adverse selection beyond spread).
4. Zero governor breaches (no fill ever exceeds the per-trade cap; no untracked-fill drift alerts).
5. Fill reconciliation clean: chain TRADE count == live_fills count over the window.

## On PASS → fund +$250, same allowlist, sizing cap 5% of new bankroll, re-gate at 100 trades.
## On FAIL → live halted (allowlist emptied), account stays shadow-only; post-mortem in Fixes/.
## Not adjustable: thresholds above. Adjustable freely: pausing, reducing size, killing individual strategies.
```

- [ ] **Step 2: Set the env on the VPS** — add to `/etc/default/polyclawd` (root-owned 0600; use sudo tee -a):

```bash
ssh vps "sudo grep -q POLYCLAWD_LIVE_STRATEGY_ALLOWLIST /etc/default/polyclawd || \
  echo 'POLYCLAWD_LIVE_STRATEGY_ALLOWLIST=smart_wallet,baseball_total,soccer_match_3way' | sudo tee -a /etc/default/polyclawd; \
  sudo grep -q POLYCLAWD_PER_TRADE_FRAC /etc/default/polyclawd || \
  echo 'POLYCLAWD_PER_TRADE_FRAC=0.10' | sudo tee -a /etc/default/polyclawd"
```

- [ ] **Step 3: Add the gate to vault Tasks.md** — one open checkbox: "Live canary gate running (opened 2026-08-18) — evaluate at 30 fills or 2026-10-15; doc: [[Live-Canary-Gate-2026-08-18]]".

---

### Task 8: Deploy and verify

- [ ] **Step 1: Drift-check every file against VPS before deploying** (VPS can be newer):

```bash
for f in scripts/position_sync.py execution/risk_governor.py execution/live_config.py \
         execution/live_executor.py execution/live_position_tracker.py api/routes/live.py; do
  L=$(md5 -q ~/Desktop/polyclawd/$f)
  R=$(ssh vps "md5sum /var/www/virtuosocrypto.com/polyclawd/$f | cut -d' ' -f1")
  [ "$L" = "$R" ] && echo "OK $f" || echo "DRIFT $f — diff before overwriting"
done
```
Any DRIFT: fetch the VPS copy, diff, adopt VPS-side hardening into the local file first (precedent: sqlite timeout args, 2026-08-18), re-run tests.

- [ ] **Step 2: scp the changed files + new tests to the VPS tree, compile-check with the VPS venv python** (`python -m py_compile` each).

- [ ] **Step 3: Restart BOTH units** (scheduler runs position_sync; api serves /live/*):

```bash
ssh vps "sudo systemctl restart polyclawd-api polyclawd-scheduler && sleep 6 && \
  systemctl is-active polyclawd-api polyclawd-scheduler && curl -s http://127.0.0.1:8420/health"
```

- [ ] **Step 4: Verify end-state**

```bash
ssh vps "curl -s http://127.0.0.1:8420/api/live/portfolio" | python3 -m json.tool | head -12
```
Expected: `onchain_balance` ≈ 23.87 (real pUSD), `realized_pnl` ≈ −72.16 (positions ledger), `deployed_usd` 0.0, governor ACTIVE.

- [ ] **Step 5: Re-grep the crontab watchdog lines if any cron was touched** (fleet ledger rule) and append the deploy note to `tasks/lessons.md`.

---

## Execution order & dependencies

Task 1 (data) → Task 2 (needs Task 1's ground truth to verify against) → Tasks 3, 4 (independent of 2; 4 depends on 3's test file) → Task 5 → Task 6 → Task 7 (must complete BEFORE any strategy is re-enabled) → Task 8. Nothing trades until Task 7's doc exists and Task 8's verification passes — until then the allowlist env being absent means the default allowlist applies but no live intents are being generated (executors dormant since July).

## Explicitly out of scope (YAGNI)
- Re-funding the account (gated on canary PASS).
- Porting the Kalshi weather FLB edge to this account (different platform).
- Rewriting the paper portfolio's mid-price fill model (separate study; the canary IS its reality check).
- Backfilling June's missing live_fills rows (predates the table; ground truth lives in data-api activity).

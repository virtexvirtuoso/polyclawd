# Phase 1 Structure-Audit Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute the safety-net phase of the 2026-07-01 structure audit: remove the dead route-file fork, route SQLite connects through the central `db.py` factory, make the scheduler restart-tolerant, lock the money rails with regression tests, and make the CLAUDE.md contract true.

**Architecture:** No behavior change except deliberately reviving dormant gated scheduler tasks (gated behind an explicit user sign-off checkpoint in Task 6). All edits land in the canonical repo `~/Desktop/polyclawd` on the current branch (`fix/odds-api-credit-leak`), committed per task, and deployed by per-file scp splice to `vps:/var/www/virtuosocrypto.com/polyclawd/` + unit restart + journal verification — never a full-tree deploy (tree carries unrelated WIP).

**Tech Stack:** Python 3 / FastAPI / asyncio scheduler / SQLite (WAL), pytest, systemd units `polyclawd-api` + `polyclawd-scheduler` on VPS (`ssh vps`).

**Ground rules for every task:**
- Before editing any file, diff it against the VPS copy (`ssh vps cat /var/www/virtuosocrypto.com/polyclawd/<relpath> | diff - <relpath>`); if VPS is newer, splice from the VPS version as base (known drift trap).
- Use `trash`, never `rm`. Never `git stash/checkout/reset` (tree has parallel WIP).
- Large files: the Edit tool silently rejects >~720-line files — verify every edit landed via `grep`, fall back to a python script if not.
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Archive the dead `api/signals.py` fork

**Files:**
- Delete (via trash): `api/signals.py`

- [ ] **Step 1: Re-verify zero importers at execution time**

```bash
cd ~/Desktop/polyclawd
grep -rn "from api.signals import\|from api import signals\b\|import api.signals\|from \.signals import\|from \.\.signals import" \
  --include='*.py' . | grep -v "api/routes\|signals/\|test"
grep -rn "api\.signals" --include='*.py' . | grep -v "api/routes\|api\.routes"
```
Expected: both empty. If ANY hit appears, STOP — report it, do not delete.

- [ ] **Step 2: Confirm only `api.routes.signals` is registered**

```bash
grep -n "signals" api/main.py | head -5
```
Expected: imports/router registration reference `api.routes.signals` (or `routes.signals`), never bare `api.signals`.

- [ ] **Step 3: Trash the file**

```bash
trash ~/Desktop/polyclawd/api/signals.py
ls api/signals.py 2>&1
```
Expected: `No such file or directory`.

- [ ] **Step 4: Smoke-test the API app still imports**

```bash
venv/bin/python -c "from api.main import app; print('app OK, routes:', len(app.routes))"
```
Expected: `app OK, routes: <N>` (N > 50). If ImportError, restore from git (`git checkout -- api/signals.py` is FORBIDDEN — instead recover from Trash) and STOP.

- [ ] **Step 5: Remove it on the VPS too (it's dead there as well)**

```bash
ssh vps "mv /var/www/virtuosocrypto.com/polyclawd/api/signals.py /tmp/api-signals-py.retired-20260702 && sudo systemctl restart polyclawd-api && sleep 3 && systemctl is-active polyclawd-api && curl -s -m 5 http://127.0.0.1:8420/api/health"
```
Expected: `active` + `{"status":"healthy"...}`. If not active: `ssh vps "mv /tmp/api-signals-py.retired-20260702 /var/www/virtuosocrypto.com/polyclawd/api/signals.py && sudo systemctl restart polyclawd-api"` and STOP.

- [ ] **Step 6: Commit**

```bash
git add -A api/signals.py
git commit -m "refactor: remove dead api/signals.py fork (0 importers; 100% function-name subset of api/routes/signals.py)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Make the CLAUDE.md contract true

**Files:**
- Modify: `CLAUDE.md` (Component Responsibilities table, ~line 40)

- [ ] **Step 1: Edit the component table**

Replace the row:
```
| Scanners (`signals/`, `odds/`, `src/indexers/`) | Gather intel, score raw signals, cache results | Execute trades, modify positions |
```
with:
```
| Scanners (`signals/`, `odds/`) | Gather intel, score raw signals, cache results | Execute trades, modify positions |
| Execution (`execution/` — clob_client, live_executor, risk_governor, fee_model) | Own order placement, risk caps, fee math | Make scanning/scoring decisions |
```
And replace the row:
```
| Simmer (`api/routes/trading.py`) | Execute trades, manage custody, enforce rate limits | Make scanning/scoring decisions |
```
with:
```
| Simmer (`api/routes/trading.py`, via `execution/`) | Execute trades, manage custody, enforce rate limits | Make scanning/scoring decisions |
```

- [ ] **Step 2: Add a known-violations note under the table**

Insert directly below the table:
```markdown
> **Known violations (2026-07-01 audit):** `signals/paper_portfolio.py` imports
> `execution.live_executor`/`clob_client` (scanner reaching into execution);
> `api/routes/markets.py` computes edges. Tracked in
> `docs/plans/2026-07-02-phase1-structure-fixes.md` — do not add new ones.
```

- [ ] **Step 3: Verify and commit**

```bash
grep -n "src/indexers" CLAUDE.md          # expected: no output
grep -n "execution/" CLAUDE.md | head -3  # expected: the new row + note
git add CLAUDE.md
git commit -m "docs: CLAUDE.md contract matches reality (add execution/ layer, drop nonexistent src/indexers)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Money-rail regression tests (characterization — expected to PASS)

**Files:**
- Test: `tests/unit/test_risk_rails.py` (new)

These lock in CURRENT behavior of the risk governor and phase limits. If any test FAILS, that is a real finding — STOP and report; do not bend the assertion to fit.

- [ ] **Step 1: Write the test file**

```python
"""Regression tests for the money rails: RiskGovernor state machine
(kill floor / daily halt / per-trade / deployed caps) and scaling-phase
daily limits. Pure + offline. Run:
    venv/bin/python -m pytest tests/unit/test_risk_rails.py -q --noconftest
"""

import sqlite3

import pytest

from execution import live_db
from execution.risk_governor import RiskGovernor
from config.scaling_phases import (
    Phase,
    get_phase,
    get_phase_config,
    check_daily_limits,
)


@pytest.fixture
def gov(monkeypatch):
    # Pin thresholds so env/config files can't skew assertions.
    monkeypatch.setenv("POLYCLAWD_DAILY_LOSS_HALT", "50.0")
    monkeypatch.setenv("POLYCLAWD_KILL_FLOOR", "250.0")
    monkeypatch.setenv("POLYCLAWD_WEATHER_PER_TRADE_CAP", "100.0")
    monkeypatch.setenv("POLYCLAWD_MAX_DEPLOYED_FRAC", "0.60")
    conn = sqlite3.connect(":memory:")
    live_db.init_live_tables(conn)
    g = RiskGovernor(conn, mode="PAPER")
    g.set_bankroll(1000.0)
    return g


def _intent(size_usd, market_id="m1"):
    return {"size_usd": size_usd, "market_id": market_id}


class TestDailyHalt:
    def test_combined_loss_trips_halt(self, gov):
        gov.record_realized_loss(30.0)
        gov.set_unrealized_loss(25.0)          # 30 + 25 >= 50
        d = gov.check(_intent(10))
        assert not d.allowed and "daily_loss_halt" in d.reason
        assert gov.state == "DAILY_HALT"

    def test_halt_is_sticky_until_reset_day(self, gov):
        gov.record_realized_loss(60.0)
        assert not gov.check(_intent(10)).allowed
        gov.set_unrealized_loss(0.0)
        gov._daily_loss = 0.0                   # loss gone, state must still hold
        assert not gov.check(_intent(10)).allowed
        gov.reset_day()
        assert gov.check(_intent(10)).allowed


class TestKillFloor:
    def test_bankroll_below_floor_kills(self, gov):
        gov.set_bankroll(200.0)                 # < 250 floor
        d = gov.check(_intent(10))
        assert not d.allowed and "kill_floor" in d.reason
        assert gov.state == "KILL"

    def test_kill_is_sticky_and_beats_daily_halt(self, gov):
        gov.set_bankroll(200.0)
        gov.record_realized_loss(60.0)          # would also trip daily halt
        d = gov.check(_intent(10))
        assert "kill_floor" in d.reason         # rule order: KILL wins
        gov.set_bankroll(1000.0)                # recovery does NOT auto-clear
        assert not gov.check(_intent(10)).allowed
        gov.reset_kill()
        assert gov.check(_intent(10)).allowed


class TestTradeCaps:
    def test_exactly_at_cap_allowed_above_denied(self, gov):
        assert gov.check(_intent(100.0)).allowed          # strict >
        d = gov.check(_intent(100.01))
        assert not d.allowed and "per_trade_cap" in d.reason

    def test_deployed_cap(self, gov):
        gov.record_fill("m0", 550.0)            # 550 + 100 > 600 (60% of 1000)
        d = gov.check(_intent(100.0))
        assert not d.allowed and "max_deployed" in d.reason


class TestPhaseLimits:
    def test_phase_boundaries(self):
        assert get_phase(999.99) == Phase.SEED
        assert get_phase(1_000) == Phase.GROWTH
        assert get_phase(10_000) == Phase.ACCELERATION
        assert get_phase(100_000) == Phase.PRESERVATION

    def test_daily_loss_limit_blocks_trading(self):
        balance = 5_000.0
        cfg = get_phase_config(balance)
        at_limit = check_daily_limits(
            balance=balance,
            daily_pnl=-(balance * cfg.max_daily_loss_pct),
            daily_trades=0,
            current_exposure=0.0,
        )
        assert at_limit["can_trade"] is False
        assert at_limit["limit_type"] == "daily_loss"

    def test_small_loss_allows_trading(self):
        ok = check_daily_limits(
            balance=5_000.0, daily_pnl=-1.0, daily_trades=0, current_exposure=0.0
        )
        assert ok["can_trade"] is True
```

Note: if `gov.state` is a method not a property in the current source, use `gov.state()` — check `execution/risk_governor.py:240` (`def state(self) -> str` — it is a plain method unless decorated `@property`; look at the two lines above it and match).

- [ ] **Step 2: Run the tests**

```bash
venv/bin/python -m pytest tests/unit/test_risk_rails.py -q --noconftest
```
Expected: all PASS. A failure = behavioral finding → STOP, report which rail deviates, await user decision.

- [ ] **Step 3: Run adjacent existing tests to confirm no interference**

```bash
venv/bin/python -m pytest tests/unit/test_archetype_classifier.py tests/test_scorer_sizing.py -q --noconftest
```
Expected: PASS (same count as before this task).

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_risk_rails.py
git commit -m "test: characterization tests for money rails (kill floor, daily halt, trade/deployed caps, phase daily limits)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: DB-connect migration — batch 1: `api/`

The codemod `scripts/migrate_db_connect.py <list> [--apply]` rewrites `sqlite3.connect(` → `db_connect(` and inserts `from db import connect as db_connect`. Safe ONLY where repo root is on sys.path — true for the `api` package (uvicorn + pytest).

**Files:**
- Modify: every `api/**/*.py` containing `sqlite3.connect(` (list built in Step 1)

- [ ] **Step 1: Build the file list**

```bash
cd ~/Desktop/polyclawd
grep -rl "sqlite3\.connect(" api/ --include='*.py' > /tmp/dbmig-api-2026-07-02.txt
wc -l /tmp/dbmig-api-2026-07-02.txt && cat /tmp/dbmig-api-2026-07-02.txt
```
Expected: ~10-25 files, all under `api/`.

- [ ] **Step 2: Dry-run**

```bash
venv/bin/python scripts/migrate_db_connect.py /tmp/dbmig-api-2026-07-02.txt
```
Expected: per-file report of would-change counts; note any SKIPPED files (already use db_connect, or no bare `import sqlite3` anchor) — skipped files must be migrated by hand in Step 3b.

- [ ] **Step 3: Apply**

```bash
venv/bin/python scripts/migrate_db_connect.py /tmp/dbmig-api-2026-07-02.txt --apply
```

- [ ] **Step 3b: Hand-migrate any files the codemod skipped**

For each SKIPPED file: add `from db import connect as db_connect` after the first top-level import, replace each `sqlite3.connect(` call with `db_connect(`, keep all other args identical. Verify with `grep -n "sqlite3\.connect(\|db_connect(" <file>`.

- [ ] **Step 4: Verify count + app imports + tests**

```bash
grep -rc "sqlite3\.connect(" api/ --include='*.py' | grep -v ":0" || echo "api/ clean"
venv/bin/python -c "from api.main import app; print('app OK')"
venv/bin/python -m pytest tests/ -q -x --ignore=tests/load 2>&1 | tail -3
```
Expected: `api/ clean`, `app OK`, pytest green (same failures-before == failures-after if the suite has pre-existing reds — record the before count FIRST via the same command on `git stash`-free HEAD… it's already the working tree, so just record the count before Step 3).

- [ ] **Step 5: Deploy batch to VPS + restart + verify**

```bash
cd ~/Desktop/polyclawd
for f in $(cat /tmp/dbmig-api-2026-07-02.txt) db.py; do scp "$f" "vps:/var/www/virtuosocrypto.com/polyclawd/$f"; done
ssh vps "sudo systemctl restart polyclawd-api && sleep 3 && systemctl is-active polyclawd-api && curl -s -m 5 http://127.0.0.1:8420/api/health && journalctl -u polyclawd-api --since '-2 min' -p err --no-pager | tail -5"
```
Expected: `active`, healthy JSON, no new errors. (db.py already exists on VPS but scp keeps them identical.)

- [ ] **Step 6: Commit**

```bash
git add api/ && git commit -m "refactor: route api/ SQLite connects through db.connect (busy_timeout+WAL) — batch 1 of lock-safety migration

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: DB-connect migration — batch 2: `services/` + import-only `signals/` + `odds/`

Standalone-executed modules (`python signals/foo.py` from cron/subprocess) do NOT have repo root on sys.path unless they set it themselves — migrating those without the guard breaks them with ImportError (silent dead cron). The list builder below excludes any file with a `__main__` block that lacks its own repo-root `sys.path` insert.

- [ ] **Step 1: Build the guarded file list**

```bash
cd ~/Desktop/polyclawd
venv/bin/python - <<'EOF'
import pathlib, re
out = []
for d in ("services", "signals", "odds", "execution"):
    for p in sorted(pathlib.Path(d).rglob("*.py")):
        src = p.read_text(errors="replace")
        if "sqlite3.connect(" not in src:
            continue
        runs_standalone = '__name__ == "__main__"' in src or "__name__ == '__main__'" in src
        has_root_hack = re.search(r"sys\.path\.insert\(0", src)
        if runs_standalone and not has_root_hack:
            print(f"EXCLUDED (standalone, no root hack): {p}")
            continue
        out.append(str(p))
pathlib.Path("/tmp/dbmig-b2-2026-07-02.txt").write_text("\n".join(out) + "\n")
print(f"\n{len(out)} files listed -> /tmp/dbmig-b2-2026-07-02.txt")
EOF
```
Expected: a list + explicit EXCLUDED lines. Excluded files are deferred to Phase 3 (packaging) — record them in the commit message.

- [ ] **Step 2: Dry-run, apply, hand-migrate skips**

```bash
venv/bin/python scripts/migrate_db_connect.py /tmp/dbmig-b2-2026-07-02.txt
venv/bin/python scripts/migrate_db_connect.py /tmp/dbmig-b2-2026-07-02.txt --apply
```
Then hand-migrate SKIPPED files exactly as Task 4 Step 3b.

- [ ] **Step 3: Verify imports of the two long-lived processes + tests**

```bash
venv/bin/python -c "from api.main import app; print('api OK')"
venv/bin/python -c "import sys; sys.path.insert(0,'.'); import services.scheduler; print('scheduler OK')" 2>&1 | tail -1
venv/bin/python -m pytest tests/ -q --ignore=tests/load 2>&1 | tail -3
```
Expected: `api OK`, `scheduler OK`, pytest same-or-better than the Task 4 baseline.

- [ ] **Step 4: Spot-check one standalone-run migrated file (if any had a root hack)**

Pick one migrated file that runs standalone (has `__main__` + root hack), run its import path exactly as production does:
```bash
venv/bin/python signals/shadow_tracker.py resolve  # if it was in the list
```
Expected: exits 0 in <60s (it did 10.4s on 2026-07-02). If it was excluded, skip this step.

- [ ] **Step 5: Deploy + restart both units + verify**

```bash
for f in $(cat /tmp/dbmig-b2-2026-07-02.txt); do scp "$f" "vps:/var/www/virtuosocrypto.com/polyclawd/$f"; done
ssh vps "sudo systemctl restart polyclawd-api polyclawd-scheduler && sleep 5 && systemctl is-active polyclawd-api polyclawd-scheduler && journalctl -u polyclawd-scheduler --since '-3 min' --no-pager | grep -ciE 'error|traceback' && echo scheduler-log-checked"
```
Expected: both `active`; error count 0 (the grep -c returning 0 exits 1 — that is fine, `scheduler-log-checked` may not print; treat count 0 as success).

- [ ] **Step 6: Commit**

```bash
git add services/ signals/ odds/ execution/
git commit -m "refactor: route services/signals/odds SQLite connects through db.connect — batch 2 (standalone-no-hack files excluded, deferred to P3 packaging)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Restart-tolerant scheduler state (+ dormant-task inventory & sign-off)

Counter-gated tasks (`_state[f"{name}_n"] % every_n`) reset on every restart; tasks gated longer than the restart interval may never fire. Replace counters with wall-clock last-run persisted in SQLite.

**Files:**
- Create: `services/task_state.py`
- Test: `tests/unit/test_task_state.py`
- Modify: `services/scheduler.py` (gated-task loops in `tick_5min` ~line 1712-1717 and `tick_30min` ~line 1759-1771)

- [ ] **Step 1: Write the failing test**

```python
"""Tests for services.task_state — restart-proof wall-clock task gating."""

from services import task_state


def test_first_call_runs_and_persists(tmp_path):
    db = tmp_path / "sched_state.db"
    assert task_state.should_run("scan_a", 1800, now=1000.0, db_path=db) is True
    # immediately again: gated
    assert task_state.should_run("scan_a", 1800, now=1001.0, db_path=db) is False


def test_runs_again_after_interval(tmp_path):
    db = tmp_path / "sched_state.db"
    assert task_state.should_run("scan_b", 1800, now=1000.0, db_path=db)
    assert not task_state.should_run("scan_b", 1800, now=2799.0, db_path=db)
    assert task_state.should_run("scan_b", 1800, now=2800.0, db_path=db)


def test_state_survives_restart(tmp_path):
    """A new connection (fresh process) sees the prior last-run."""
    db = tmp_path / "sched_state.db"
    assert task_state.should_run("scan_c", 3600, now=1000.0, db_path=db)
    # simulate restart: nothing cached in module — call again, still gated
    assert not task_state.should_run("scan_c", 3600, now=1100.0, db_path=db)


def test_tasks_are_independent(tmp_path):
    db = tmp_path / "sched_state.db"
    assert task_state.should_run("x", 600, now=50.0, db_path=db)
    assert task_state.should_run("y", 600, now=50.0, db_path=db)
```

- [ ] **Step 2: Run to verify it fails**

```bash
venv/bin/python -m pytest tests/unit/test_task_state.py -q --noconftest
```
Expected: FAIL — `ModuleNotFoundError: No module named 'services.task_state'` (or ImportError).

- [ ] **Step 3: Implement `services/task_state.py`**

```python
"""Restart-proof wall-clock gating for scheduler tasks.

Replaces in-memory tick counters (wiped by the health-check restarts) with
last-run timestamps persisted in SQLite. A task runs when
``now - last_run >= interval_secs``; the timestamp updates only when we say
"run" so a crash between gate and task simply re-runs it next tick.
"""

import pathlib
import time

from db import connect as db_connect

STATE_DB = pathlib.Path(__file__).resolve().parent.parent / "storage" / "scheduler_state.db"


def should_run(name: str, interval_secs: float, *, now: float | None = None,
               db_path=None) -> bool:
    """True if ``name`` hasn't run in the last ``interval_secs``; records the run."""
    ts = time.time() if now is None else now
    path = STATE_DB if db_path is None else db_path
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = db_connect(path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS task_last_run (name TEXT PRIMARY KEY, last_run REAL NOT NULL)"
        )
        row = conn.execute(
            "SELECT last_run FROM task_last_run WHERE name = ?", (name,)
        ).fetchone()
        if row is not None and ts - row[0] < interval_secs:
            return False
        conn.execute(
            "INSERT INTO task_last_run (name, last_run) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET last_run = excluded.last_run",
            (name, ts),
        )
        conn.commit()
        return True
    finally:
        conn.close()
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
venv/bin/python -m pytest tests/unit/test_task_state.py -q --noconftest
```
Expected: 4 passed.

- [ ] **Step 5: Wire into the scheduler's gated loops**

In `services/scheduler.py`, add near the other local imports at top (after the `sys.path.insert` at line 30): `from services import task_state`.

In `tick_5min` (~line 1715), replace:
```python
        for name, every_n in TICK_TASKS["5min_gated"].items():
            key = f"{name}_n"
            _state[key] = _state.get(key, 0) + 1
            if _state[key] % every_n == 0:
                await run_in_thread(_run_safe, name, _task_fn(name))
```
with:
```python
        for name, every_n in TICK_TASKS["5min_gated"].items():
            if task_state.should_run(name, every_n * 300):
                await run_in_thread(_run_safe, name, _task_fn(name))
```

In `tick_30min` (~line 1764), replace:
```python
        for name, every_n in TICK_TASKS["30min_gated"].items():
            key = f"{name}_n"
            _state[key] = _state.get(key, 0) + 1
            if _state[key] % every_n == 0:
                await run_in_thread(_run_safe, name, _task_fn(name))
```
with:
```python
        for name, every_n in TICK_TASKS["30min_gated"].items():
            if task_state.should_run(name, every_n * 1800):
                await run_in_thread(_run_safe, name, _task_fn(name))
```

BEHAVIOR NOTE: wall-clock gating fires each gated task on the FIRST tick after deploy (no prior row), then settles to its cadence. That is the deliberate "revival". Verify the edits landed (`grep -n "task_state.should_run" services/scheduler.py` → 2 hits; file is >720 lines — Edit-tool silent-reject trap applies).

- [ ] **Step 6: Dormant-task inventory — STOP FOR USER SIGN-OFF**

```bash
ssh vps "journalctl -u polyclawd-scheduler --since '-48 hours' --no-pager | grep -c 'Started polyclawd-scheduler'"
for t in ufc_edge_scan hf_spread_15m soccer_match_scan soccer_resolve scorer_edge_scan nfl_edge_scan betfair_scan hf_window_snapshot hf_spread_scan; do
  echo "== $t =="; ssh vps "journalctl -u polyclawd-scheduler --since '-48 hours' --no-pager | grep -c \"$t\""
done
```
Produce a table: task · configured cadence · times fired in 48h · will-revive?  Present it to the user and WAIT for explicit approval of which tasks may revive (options: all / all-except-listed / none→abort task). Do not proceed to Step 7 without it.

- [ ] **Step 7: Deploy + live restart-tolerance verification**

```bash
scp services/task_state.py services/scheduler.py vps:/var/www/virtuosocrypto.com/polyclawd/services/
ssh vps "sudo systemctl restart polyclawd-scheduler && sleep 10 && systemctl is-active polyclawd-scheduler"
# after ~6 min (one 5-min tick), confirm a gated task fired and state persisted:
ssh vps "journalctl -u polyclawd-scheduler --since '-8 min' --no-pager | grep -E 'ufc_edge_scan|hf_spread_15m' | head -3"
ssh vps "venv_p=/var/www/virtuosocrypto.com/polyclawd; \$venv_p/venv/bin/python3 -c \"import sqlite3;c=sqlite3.connect('\$venv_p/storage/scheduler_state.db');print(c.execute('SELECT name, last_run FROM task_last_run').fetchall())\""
# restart-tolerance: restart mid-interval, task must NOT re-fire early
ssh vps "sudo systemctl restart polyclawd-scheduler && sleep 360 && journalctl -u polyclawd-scheduler --since '-6 min' --no-pager | grep -c 'ufc_edge_scan'"
```
Expected: rows in `task_last_run`; after the mid-interval restart, gated tasks with unexpired intervals do NOT re-run (count 0 for a task inside its window).

- [ ] **Step 8: Commit**

```bash
git add services/task_state.py services/scheduler.py tests/unit/test_task_state.py
git commit -m "feat: restart-proof wall-clock gating for scheduler tasks (SQLite last-run state; replaces in-memory tick counters wiped by health-check restarts)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Wrap-up and status sync

- [ ] **Step 1: Final verification sweep**

```bash
cd ~/Desktop/polyclawd
echo "raw connects remaining:"; grep -rc "sqlite3\.connect(" --include='*.py' api/ services/ signals/ odds/ execution/ | grep -v ":0" | wc -l
venv/bin/python -m pytest tests/ -q --ignore=tests/load 2>&1 | tail -2
ssh vps "systemctl is-active polyclawd-api polyclawd-scheduler polyclawd-hf"
```
Expected: remaining raw connects only in the Step-5-excluded standalone files; pytest green vs baseline; all 3 units active.

- [ ] **Step 2: Update the scope doc status + repo todo**

Set `status: scoped` → `status: phase1-done` in the vault scope doc:
```bash
vault-edit "02-Projects/Polyclawd/Scopes/2026-07-02-structure-audit-fixes.md" --old "status: scoped" --new "status: phase1-done"
```
Append the Phase-1 completion line + excluded-files list (from Task 5 Step 1 output) to `tasks/todo.md` under a `## 2026-07-02 Phase 1 structure fixes` heading.

- [ ] **Step 3: Commit**

```bash
git add tasks/todo.md
git commit -m "chore: record Phase 1 structure-fix completion + P3-deferred file list

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

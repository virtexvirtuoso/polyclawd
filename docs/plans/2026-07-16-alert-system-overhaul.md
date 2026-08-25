# Alert System Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> Spec: vault `02-Projects/Polyclawd/Strategy/Alert-System-Overhaul-2026-07-16.md`

**Goal:** Cut Telegram alert volume from ~80/day to ~10-15/day, raise delivery success from 62% to >99%, and prove the stop-loss alert path works end-to-end.

**Architecture:** Diagnose first (the send ledger already records per-caller failures — the 38% has a queryable answer). Then harden the single shared send function (`scripts/openclaw_alerts.py`), prove the stop path with a synthetic position, fix the hex-ID bug at its source (`live_positions.market_title`), and add tiered batching by **extending the existing `signals/alert_governor.py` SQLite pattern** (`shadow_trades.db`, WAL, cross-process) — NOT a from-scratch dispatch service. Timing decisions live in the DB, not process memory, because the health-check cron restarts the scheduler ~every 15 min.

**Tech Stack:** Python 3 stdlib (urllib, sqlite3), pytest, systemd/cron on VPS, Telegram Bot API.

## Decision Points (resolved in refinement — flag to Mr. V if you disagree)

| # | Decision | Chosen default | Why |
|---|----------|----------------|-----|
| D1 | Tier-3 daily digest in v1? | **Deferred** to the refinement phase | Volume math: noise-kills (Phase 4) + smart-wallet batching alone reach the 10-15/day target. The existing daily P&L report is already the EOD vehicle — add digest lines there if missed. Cuts dispatch v1 from ~150 to ~80 lines. |
| D2 | Retry strategy | **One inline retry (5s), then enqueue for redelivery** on next 5-min drain | Scheduler tasks run via `loop.run_in_executor` (`scheduler.py:~1723`), so a sleep blocks an executor THREAD, not the event loop — but the thread pool is finite, and long backoffs still serialize/starve other tasks. More importantly the queue is the durable retry: it survives restarts, sleeps don't. |
| D3 | Stop heartbeat cadence | **Silence-alarm only** + 1 line in existing daily P&L report | The vault doc said hourly heartbeat, but +24 msgs/day contradicts the noise goal. Alarm fires only when the evaluator is dead >30 min = zero steady-state noise. |
| D4 | Mac launchd pipeline (`mlb-lag-verdict`) | Stays direct-send tier 1 | Can't share `shadow_trades.db`. Low volume; accepted. |

## Failure Modes (name them before they happen)

- **F1 — send-layer regression bricks ALL alerts** (every pipeline shares `alert_openclaw`): public signature frozen; deploy Phase 1 ALONE and watch the ledger for 1h before deploying anything else; rollback = revert one file.
- **F2 — queue grows unbounded** if drain stops running: drain drops rows >6h old with a suppressed-log entry; the Task 5.4 failure alarm catches a dead drain indirectly (sends stop).
- **F3 — dedup_key collisions suppress distinct alerts**: keys must encode entity+state (follow `alert_governor` conventions), never just pipeline name.
- **F4 — sqlite lock contention on enqueue**: mirror the governor's philosophy — fail OPEN to direct send (a duplicate is cheaper than a missed alert).
- **F5 — selftest row visible to other 1-min tick tasks** for a few seconds: row is `[SELFTEST]`-labeled and deleted in `finally`; a stray labeled alert is harmless.
- **F6 — ambiguous send timeouts double-deliver**: a timeout after Telegram accepted the message + queue redelivery = duplicate. Accepted (at-least-once is the right semantic for alerts); redeliveries carry a `(redelivery)` prefix.
- **F7 — total VPS death = total silence**: no on-VPS watchdog can report its own host's demise. Covered by the cross-host dead-man (Task 5.5).

**Deploy discipline (applies to EVERY task):** Canonical source is `~/Desktop/polyclawd`, but core VPS files drift NEWER. Before editing any existing file: `ssh vps "md5sum /var/www/virtuosocrypto.com/polyclawd/<file>"` vs local; if different, `scp` the VPS copy down, diff, and splice your edit onto the VPS version. Never blind-rsync. Deploy via `~/bin/polyclawd-deploy` (copies working tree). New standalone files are safe to copy directly.

---

## Phase 0 — Diagnose (VPS runbook, NO code changes)

### Task 0.1: Classify the 38% delivery failures from the send ledger

The ledger (`logs/telegram_sent.jsonl`) records `{ts, caller, channel, ok, parse_mode, len[, err]}` per attempt. The failure classes are queryable in one command.

**Files:** none (read-only VPS analysis)

- [ ] **Step 1: Group failures by caller and parse_mode**

```bash
ssh vps "cd /var/www/virtuosocrypto.com/polyclawd && python3 - <<'EOF'
import json, collections
from datetime import datetime, timedelta, timezone
cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
rows = []
for line in open('logs/telegram_sent.jsonl'):
    try: r = json.loads(line)
    except: continue
    if datetime.fromisoformat(r['ts']) >= cutoff: rows.append(r)
fails = [r for r in rows if not r['ok']]
print(f'{len(rows)} attempts, {len(fails)} failed ({100*len(fails)/max(len(rows),1):.0f}%)')
for key in ('caller','parse_mode'):
    print(f'--- failures by {key} ---')
    for k,v in collections.Counter(r.get(key) for r in fails).most_common():
        print(f'{v:4d}  {k}')
print('--- failed with len>4000 ---', sum(1 for r in fails if r.get('len',0)>4000))
print('--- err samples ---')
for r in fails[:10]: print(r.get('err','<no err recorded>'), '| caller:', r['caller'])
EOF"
```

Expected: a dominant failure class emerges (single caller, or all `parse_mode=Markdown`, or `<no err recorded>` = likely missing `TELEGRAM_BOT_TOKEN` in that caller's cron env).

- [ ] **Step 2: Check the missing-token hypothesis for the top failing callers**

The VPS delivery path is `_telegram_http_send()`, which drops the message and returns False if `TELEGRAM_BOT_TOKEN` is unset. Crons must `set -a && . ~/.config/polyclawd/alerts.env && set +a` first.

```bash
ssh vps "crontab -l | grep -n 'polyclawd' | grep -v 'alerts.env'"
```

Expected: any line invoking a python alert script WITHOUT sourcing `alerts.env` is a suspect. Cross-reference against Step 1's failing callers.

- [ ] **Step 3: Check stdout of the top failing caller's cron log for the printed error**

`_telegram_http_send` prints the exception (`telegram HTTP send failed: HTTP Error 400: ...`) to stdout, which lands in that cron's log file.

```bash
ssh vps "grep -h 'telegram' /var/log/polyclawd*.log /home/linuxuser/logs/*.log 2>/dev/null | grep -i 'failed\|dropped\|ok=false' | tail -30"
```

- [ ] **Step 4: Write the verdict**

Append a dated `#### Diagnosis 2026-07-16` block to the vault doc §2.3 with: failure class(es), owning caller(s), and which Phase 1 fix addresses each. If a failure class is NOT covered by Phase 1 (e.g., a dead cron, a network issue), STOP and re-plan before writing code.

### Task 0.2: Verify the stop evaluator is alive

`stop_evaluator` runs UNGATED in the 5-min tick and `stop_evaluator_urgent` in the 1-min tick (scheduler.py TICK_TASKS), so the 15-min restart should NOT starve it — but exceptions are swallowed into logs by the task wrapper, and positions may simply never have hit -40%.

**Files:** none (read-only VPS analysis)

- [ ] **Step 1: Confirm ticks are firing and count restarts**

```bash
ssh vps "journalctl -u polyclawd-scheduler --since '48 hours ago' | grep -c 'stop_evaluator'; journalctl -u polyclawd-scheduler --since '48 hours ago' | grep -i 'started\|stopping' | tail -20"
```

Expected: hundreds of stop_evaluator invocations. If ~0: the task is failing at import/registration — read the exception with `journalctl -u polyclawd-scheduler | grep -A5 'Task stop_evaluator failed'`.

- [ ] **Step 2: Check whether any position actually crossed -40% (hypothesis a)**

```bash
ssh vps "cd /var/www/virtuosocrypto.com/polyclawd && python3 -c \"
import sqlite3
con = sqlite3.connect('storage/shadow_trades.db'); con.row_factory = sqlite3.Row
for r in con.execute('SELECT market_title, side, entry_price, bet_size FROM paper_positions WHERE status=\\\"open\\\"'):
    print(dict(r))
\""
```

(`evaluate_stops` reads **`paper_positions`**, and current prices are fetched LIVE via `_fetch_price()` at evaluation time — there is no stored `current_price` column. Adjust columns to the actual schema in `execution/live_db.py`. If no open position ever drew down 40%, zero warnings is CORRECT behavior; record that and downgrade urgency.)

- [ ] **Step 3: Check hypothesis (d) — the warning block is DEAD CODE at default config (CONFIRMED in local source, verify on VPS copy).** In `services/stop_evaluator.py`: `UNIVERSAL_MAX_LOSS_PCT` and `PRE_RESOLVE_WARN_LOSS_PCT` BOTH default to `0.40` (lines 36, 43), the universal stop is checked FIRST and `continue`s (line ~593), so any position at ≥40% loss is closed before the ⚠️ pre-resolution warning at line ~600 can ever evaluate true — and the universal-stop notification goes to `_send_discord_alert`, NOT Telegram. Zero ⚠️ warnings is the *structurally guaranteed* outcome. Verify the VPS copy has the same thresholds/order, then fix: warn at a LOWER threshold than the universal stop (e.g. `PRE_RESOLVE_WARN_LOSS_PCT=0.30`) and/or route the universal-stop close notification to Telegram too.

- [ ] **Step 4: Write the verdict** into the vault doc §2.2 — one of: (a) confirmed no trigger, (b) evaluator dead + traceback, (c) warnings sent but delivery failed (cross-check ledger for `caller=scheduler` failures at matching timestamps), (d) warning threshold unreachable per Step 3.

---

> [!important] Phase 0 findings (executed 2026-07-16) — Phase 1 scope EXTENDED
> Ledger analysis (48h: 277 attempts, 120 failed, 43%): **100% of failures come from `scripts/alert_formatter.py::send_telegram`**, which hardcodes HTML parse mode and silently swallows all exceptions (no err, no logs). Verified cause: unescaped dynamic content in HTML mode (smoking gun `hf_spread_scanner.py:618` — raw `< 2 min left` → Telegram 400). The `openclaw_alerts.py` cron path had 0 failures. → **Task 1.6 added** (harden alert_formatter). Stop evaluator: hypothesis **(d) CONFIRMED on VPS** (md5-identical file; evaluator alive, ~6-min cadence, "all 7 positions within limits"; no position near -40%). Cron env gaps: 3 latent (kalshi_prop_resolver, backfill_weather_actuals, insider_detector resolve) — fix at deploy (Task 1.5). Drift: **VPS `scheduler.py` is AHEAD of local** (`task_tier1_whale_alerts`) — all scheduler edits must splice onto the captured VPS copy in the session scratchpad `vps-copies/`. Journald retention is only ~1.6h due to whale-scanner DEBUG spam (P2 cleanup candidate).

## Phase 1 — Harden the send layer (`scripts/openclaw_alerts.py` + `scripts/alert_formatter.py`)

### Task 1.6: Harden the SECOND send path — `alert_formatter.send_telegram` (added from Phase 0)

The actual source of all observed failures. TDD: (a) message containing `< 2 min left` sends without the 400 path; (b) forced HTTP 400 lands an `err` entry in the ledger, no retry; (c) intentional `<b>` template tags still render. Implement by escaping interpolated values (`html.escape`) or delegating delivery to `alert_openclaw` (one hardened path, preferred if clean); kill the silent `except Exception: return False`.

### Task 1.1: Record WHY sends fail (err detail into the ledger)

**Files:**
- Modify: `scripts/openclaw_alerts.py` (`_telegram_http_send`, `alert_openclaw`)
- Test: `tests/test_openclaw_alerts.py` (new)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_openclaw_alerts.py
import json, urllib.error
import scripts.openclaw_alerts as oa

def test_http_send_returns_err_detail_on_400(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("POLYCLAWD_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    def boom(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, None)
    monkeypatch.setattr(oa.urllib.request, "urlopen", boom)
    ok, err = oa._telegram_http_send("hi", parse_mode="Markdown")
    assert ok is False and "400" in err

def test_no_token_records_err(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("POLYCLAWD_LEDGER_PATH", str(ledger))
    ok, err = oa._telegram_http_send("hi")
    assert ok is False and err == "no_token"
```

- [ ] **Step 2: Run to verify failure** — `cd ~/Desktop/polyclawd && venv/bin/pytest tests/test_openclaw_alerts.py -v` → FAIL (returns bool, not tuple).

- [ ] **Step 3: Implement** — change `_telegram_http_send` to return `(ok, err)`:
  - no token → `return False, "no_token"`
  - `urllib.error.HTTPError as e` → `return False, f"http_{e.code}:{e.read()[:120]}"`
  - `urllib.error.URLError/TimeoutError as e` → `return False, f"net:{e}"`
  - Telegram `ok=false` → `return False, f"tg_api:{body[:120]}"`
  - success → `return True, ""`
  Thread the err into `_ledger_log(ok, channel, parse_mode, msg_len, err=err)` inside `alert_openclaw`. Update the one other internal caller (`FileNotFoundError` CLI fallback path) to unpack the tuple and return only `ok` to external callers — **the public `alert_openclaw()` signature must not change** (9 pipelines call it).

- [ ] **Step 4: Run tests** → PASS. **Step 5: Commit** `fix(alerts): record failure reason in send ledger`.

### Task 1.2: Transient-only retry (never retry 400s)

**Files:** Modify `scripts/openclaw_alerts.py`; extend `tests/test_openclaw_alerts.py`

- [ ] **Step 1: Failing test**

```python
def test_retries_transient_not_400(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("POLYCLAWD_LEDGER_PATH", str(tmp_path / "l.jsonl"))
    monkeypatch.setattr(oa.time, "sleep", lambda s: None)
    calls = {"n": 0}
    def flaky(req, timeout):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", {}, None)
    monkeypatch.setattr(oa.urllib.request, "urlopen", flaky)
    oa._telegram_http_send("hi")
    assert calls["n"] == 2          # 1 try + 1 retry on 5xx (D2: durable retry is the queue's job)
    calls["n"] = 0
    def bad(req, timeout):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, None)
    monkeypatch.setattr(oa.urllib.request, "urlopen", bad)
    oa._telegram_http_send("hi")
    assert calls["n"] == 1          # 400 = permanent, no retry
```

- [ ] **Step 2: FIRST verify the scheduler's task execution model** — read `services/scheduler.py` around the `_run_task` wrapper: are sync `task_*` fns run in a thread executor (`run_in_executor`/`to_thread`) or called inline from the async ticks? If inline, ANY sleep here blocks the event loop.

- [ ] **Step 3: Implement per decision D2** — ONE retry after `time.sleep(5)`, only on `http_429`/`http_5xx`/`net:*`; return immediately on `http_4xx` (≠429) and `no_token`. Durable retries belong to the dispatch queue (Task 5.1 enqueues failed tier-1 sends for redelivery), not to sleeps. NOTE: `openclaw_alerts.py` has no module-level `import time` today — add it, or the test's `monkeypatch.setattr(oa.time, ...)` dies with AttributeError instead of a clean red. Adjust the test above: expect `calls["n"] == 2` for 5xx, `1` for 400.

- [ ] **Step 3-4: Test → PASS, commit** `fix(alerts): retry transient send failures with backoff`.

### Task 1.3: Message length guard

- [ ] Add to `alert_openclaw`: if `len(message) > 4000`, split on line boundaries into ≤4000-char chunks, send sequentially, return AND of results. Test with a 9000-char message asserting 3 sends. Commit `fix(alerts): split messages over telegram 4096 limit`.

### Task 1.4: Standardize parse mode — eliminate the 400 class, don't just log it

Legacy `parse_mode="Markdown"` (the current default) is the industry-known 400 generator: unescaped `_`/`*` in market names or wallet addresses breaks the parser mid-entity. HTML mode or plain text eliminates the class. Note `alert_governor.Verdict.decorate()` already emits HTML tags (`<b>UPGRADE</b>`) — any governor-decorated message sent with Markdown renders tags literally today.

**Files:** Modify `scripts/openclaw_alerts.py`; audit all callers

- [ ] **Step 1:** Change the `alert_openclaw` default from `parse_mode="Markdown"` to `parse_mode=None` (plain text — safest for machine-generated content containing arbitrary market titles).
- [ ] **Step 2:** `grep -rn "alert_openclaw(" --include="*.py" | grep -v "parse_mode"` — every caller relying on the old Markdown default now sends plain text; spot-check the 3 highest-volume callers' message templates for `*bold*` markup that would now render literally, and strip it or convert to HTML with an `html.escape()` on interpolated values.
- [ ] **Step 3:** Test: message containing `_underscore_wallet*` sends without error under default mode. Commit `fix(alerts): default to plain text, kill the Markdown-400 class`.

### Task 1.5: Fix env sourcing for any cron identified in Task 0.1

- [ ] For each VPS crontab line flagged in Task 0.1 Step 2, prepend `set -a && . ~/.config/polyclawd/alerts.env && set +a && `. Verify by running the cron command manually and checking a new `ok=true` ledger line. (VPS-only change; document in vault doc §2.3 diagnosis block.)

---

## Phase 2 — Prove the stop path

### Task 2.0: Fix the dead warning path + Telegram routing (the 0.2(d) fix, promoted here so it gets full TDD + deploy discipline)

**Files:** Modify `services/stop_evaluator.py`; Test `tests/test_stop_thresholds.py` (new)

Phase 0 is diagnosis-only — the code change identified in Task 0.2 Step 3 lands HERE. Task 2.2 depends on this task.

- [ ] **Step 1: Failing tests** — (a) with a position at -35% loss near resolution, the ⚠️ pre-resolution warning fires (proves the warning threshold is now BELOW the universal stop); (b) with a position at -46%, the universal-stop close notification is sent via the Telegram sender (monkeypatch `alert_openclaw`), not only Discord.
- [ ] **Step 2: Implement** — default `PRE_RESOLVE_WARN_LOSS_PCT` to `0.30` (env override retained); in the universal-stop branch, alongside `_send_discord_alert(result)`, send a 🛑 stop-close alert through `alert_openclaw` (plain text). Diff the VPS copy of `stop_evaluator.py` FIRST — it is a core drift-prone file.
- [ ] **Step 3-4:** Tests PASS; commit `fix(stops): warning threshold below universal stop + telegram routing for stop-closes`.

### Task 2.1: Stop-evaluator heartbeat (DB-backed, restart-proof)

**Files:** Modify `services/stop_evaluator.py`, `services/scheduler.py`; Test `tests/test_stop_heartbeat.py` (new)

Per decision D3: **silence-alarm only** — no periodic "I'm alive" messages (that would add 24 msgs/day to a noise-reduction project).

- [ ] **Step 1:** In `evaluate_stops()`, after the position loop, write a row to `shadow_trades.db`: `INSERT OR REPLACE INTO stop_heartbeat(id, ts, positions_checked, warnings_fired) VALUES (1, strftime('%s','now'), ?, ?)` (CREATE TABLE IF NOT EXISTS first). Failing test: call `evaluate_stops()` against a temp DB with zero positions, assert the heartbeat row exists with `positions_checked=0`.
- [ ] **Step 2:** New scheduler task `task_stop_silence_alarm` in `TICK_TASKS["30min"]`: reads the row; ONLY if `ts` older than 30 min → send 🚨 "stop evaluator SILENT for Xm" (plain text, `parse_mode=None`). Alarm re-fires at most once per 6h (gate on a `last_silence_alarm` DB row, NOT `_state` — restarts wipe `_state`). Healthy = silent.
- [ ] **Step 3:** Append TWO lines to the EXISTING daily paper-P&L report (find its generator via `grep -rn "Paper P&L" --include="*.py" scripts/ services/`): `stops: checked N positions today, M warnings fired, last run Xm ago` and `delivery: X% success last 24h (from send ledger)` — the latter covers the spec's "add delivery success rate to Live Monitor" (P0 action 5) via the same zero-marginal-cost vehicle. Daily proof-of-life for both subsystems.
- [ ] **Step 4:** pytest silence + healthy paths with a temp DB → PASS. Commit `feat(stops): silence alarm + daily P&L proof-of-life line`.

### Task 2.2: Synthetic stop test (end-to-end acceptance) — REDESIGNED per critic review

> [!warning] Why the naive version is broken AND dangerous
> `evaluate_stops()` fetches prices LIVE via `_fetch_price()` keyed on `market_id` — a fake ID fetches `None` and the row is silently skipped (test can never pass). And reusing a REAL `market_id` as a workaround is FORBIDDEN: the universal-stop branch calls `_get_live_position(market_id)` and, if an open live position matches, `_close_live_position_early()` — **an actual live exit** (`stop_evaluator.py:579-585`).

**Files:** Create `scripts/stop_selftest.py`

- [ ] **Step 1:** In-process test design: the script imports `services.stop_evaluator`, **monkeypatches `_fetch_price`** so that ONLY rows whose `market_id` starts with `selftest-` return `0.27` (all other rows delegate to the real function), inserts a fake open paper row with `market_id='selftest-<uuid4>'` / `market_title='[SELFTEST] fake position'` / entry 0.50 (→ -46%), calls `evaluate_stops()`, and cleans up in a `finally:` block. The synthetic `market_id` can never collide with a real market, so `_get_live_position` finds nothing and the paper-close path runs. **The `finally` must undo ALL paper-accounting side effects, not just the position row:** `_close_position_early` records a closed losing trade and mutates paper bankroll state (`signals.paper_portfolio`). Snapshot the bankroll/closed-trade state before the run and restore it in `finally` — otherwise a `[SELFTEST]` -$ loss contaminates paper P&L stats. Belt-and-suspenders: also exclude `market_title LIKE '[SELFTEST]%'` rows from P&L reporting queries.
- [ ] **Step 2:** Assert on the path that ACTUALLY fires at -46%: the UNIVERSAL STOP close (the ⚠️ warning block is unreachable at pre-fix default thresholds — see Task 0.2 Step 3). The selftest asserts a `[SELFTEST]` stop-close notification arrives in **Telegram**, which requires Task 2.0's routing fix (universal-stop → Telegram). Dependency: run AFTER Task 2.0 lands.
- [ ] **Step 3:** Run on VPS: `ssh vps "cd /var/www/... && set -a && . ~/.config/polyclawd/alerts.env && set +a && venv/bin/python3 scripts/stop_selftest.py"` → Expected: 🛑 `[SELFTEST]` stop alert arrives in Telegram within seconds, and the fake row is gone (`SELECT COUNT(*) FROM paper_positions WHERE market_title LIKE '[SELFTEST]%'` → 0).
- [ ] **Step 4:** Commit. Record PASS in vault doc §6 success-criteria table.

---

## Phase 3 — Hex-ID fix at the source

### Task 3.1: Sweep all `market_title` consumers and fallbacks

- [ ] `grep -rn "market_id\[:24\]\|market_id\[:16\]\|token_id\[:\|market_title" --include="*.py" services/ execution/ signals/ scripts/ api/` — list every alert-formatting site that can emit a hex ID. Known: `services/stop_evaluator.py:368` (`market_id[:24]` fallback). Record the full list in the plan-execution notes before fixing.

### Task 3.2: Gamma title resolver with SQLite cache

**Files:** Create `odds/gamma_title.py`; Test `tests/test_gamma_title.py`

- [ ] **Step 1: Failing test** — `resolve_title("0xabc...")` returns cached value on second call without HTTP (monkeypatch `urlopen` to count calls); returns `None` gracefully on HTTP error.
- [ ] **Step 2: Implement** (~40 lines): `resolve_title(market_id) -> str | None` — **return None immediately unless `market_id.startswith("0x")`** (Kalshi tickers are not Gamma condition IDs and are already human-readable); check `title_cache` table in `shadow_trades.db`; miss → GET `https://gamma-api.polymarket.com/markets?condition_ids=<id>`, extract `question`, cache, return. 5s timeout, never raises.
- [ ] **Step 3-4:** Tests PASS, commit `feat(alerts): gamma title resolver with cache`.

### Task 3.3: Wire resolver into every fallback site from Task 3.1

- [ ] At each site, replace the raw-ID fallback with `resolve_title(market_id) or market_id[:24]`. E.g. stop_evaluator.py:368 becomes `market_title = live_pos_row.get("market_title") or resolve_title(market_id) or market_id[:24]`. Commit per file.

### Task 3.4: Backfill + fix at write time

- [ ] Create `scripts/backfill_market_titles.py`: `UPDATE live_positions SET market_title = ? WHERE (market_title = '' OR market_title = market_id OR length(market_title) > 60 AND market_title NOT LIKE '% %')` — for each such row, resolve via `gamma_title.resolve_title`. Dry-run flag prints planned updates; run on VPS with `--apply`. Then check every caller of `live_position_tracker.record_real_fill(...)` (NOT `record_fill` — that name belongs to `execution/live_db.py:231`): any passing `market_title=None`/token ids must pass the market question (the COALESCE patch in `live_position_tracker.py:108-109` already accepts late patches). Verify: `SELECT COUNT(*) FROM live_positions WHERE market_title='' OR market_title LIKE '0x%'` → 0.

---

## Phase 4 — Noise reduction

### Task 4.1: HF Spread Scan — stop alerting on sub-1h candles

**Files:** Modify `services/hf_spread_scanner.py` (TF_CONFIG, ~line 191); Modify `services/scheduler.py`

- [ ] **Step 1:** Diff VPS copy first (this file is actively developed). In TF_CONFIG, for the 5m and 15m entries set the alert thresholds to `None` (or add an `"alert": False` key honored by the alert-emitting code path at ~line 608) — keep scanning/data collection intact, kill only the sends. 1h+ entries: raise `intramarket` threshold to 0.15 AND require liquidity >$10K at the alert gate (spec P1 condition — check whether the scanner already carries a liquidity field per market; if not, pull it from the same market snapshot the spread is computed from).
- [ ] **Step 2:** Remove `hf_spread_5m` from `TICK_TASKS["5min"]` alert path only if it exists purely for alerts (read the task fn first; if it also logs data, keep the task, gate only the send).
- [ ] **Step 3:** Verify on VPS post-deploy: 24h with ≤2 HF alerts (was ~15/day). Commit `feat(hf): alert only on 1h+ candles, raise threshold`.

### Task 4.2: Status-report change detection (persisted hash)

- [ ] **Step 1:** Find the 6x/day status sender: `grep -rn "Live Monitor\|LIVE MONITOR\|🎯" --include="*.py" services/ scripts/ | grep -i "alert\|send"` (exact header text is in the received Telegram messages). 
- [ ] **Step 2:** Before its send: compute `h = hashlib.sha256(report_text_without_timestamps.encode()).hexdigest()`; compare to `SELECT v FROM kv WHERE k='status_report_hash'` in `shadow_trades.db` — the `kv` table does NOT exist yet, so `CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)` first (same pattern as Task 2.1's heartbeat table). Skip send if equal UNLESS now is the 20:00 ET slot (always send once daily). Store hash after send. (DB, not `_state` — restart-proof.)
- [ ] **Step 3:** Test with temp DB: same text twice → one send. Commit `feat(monitor): skip unchanged status reports`.

---

## Phase 5 — Tiered dispatch (extend the governor pattern)

### Task 5.1: `signals/alert_dispatch.py` — queue + tiers (TDD)

**Files:** Create `signals/alert_dispatch.py`; Test `tests/test_alert_dispatch.py`

API (mirrors `alert_governor` conventions — same DB, same `_connect`, `BEGIN IMMEDIATE`). Per decision D1, v1 has **no tier-3 digest** — tiers are 1 (immediate), 2 (batch 15min), 4 (suppress+log):

```python
TIER_CRITICAL, TIER_BATCH, TIER_SUPPRESS = 1, 2, 4   # 3 reserved for digest (deferred, D1)

def dispatch(pipeline: str, message: str, tier: int, *,
             parse_mode=None, dedup_key: str = "", shadow: bool = False,
             db_path=None) -> bool:
    """Tier 1 -> alert_openclaw() immediately; if that returns False, ALSO
               enqueue for redelivery on next drain (D2: queue = durable retry).
    Tier 2 -> INSERT INTO alert_queue(ts, pipeline, tier, dedup_key, message);
              duplicate (pipeline, dedup_key) within open batch is ignored.
    Tier 4 -> INSERT INTO alert_suppressed_log only. Returns True.
    shadow=True (rollout mode): enqueue with shadow=1 + log, but drain() only
              RECORDS what it would have batched (alert_shadow_log) — it never
              sends shadow rows. Caller keeps its direct send during shadow.
    On sqlite lock contention: fail OPEN to direct send (F4)."""

def drain(db_path=None, now=None, force=False) -> int:
    """Called every 5-min tick. Flushes tier-2 rows older than 15 min as ONE
    message per pipeline group; resends queued tier-1 redeliveries first.
    DB timestamps drive timing -> restart-proof. Returns messages sent."""
```

- [ ] **Step 1: Failing tests** (temp sqlite): (a) tier1 calls sender immediately (monkeypatch `alert_openclaw`); (b) tier1 with failing sender enqueues a redelivery row, and `drain()` resends it; (c) tier2 enqueues, `drain(now=t+16min)` sends exactly one combined message and empties the queue; (d) `drain(now=t+5min)` sends nothing; (e) duplicate dedup_key within window inserted once; (f) tier4 never sends; (g) two concurrent enqueues from separate connections both land (WAL); (h) enqueue under a held write lock falls back to direct send; (i) `shadow=True` rows are logged by `drain()` but NEVER sent.
- [ ] **Step 2: Implement** (~80 lines). Batch message format: header `📨 <pipeline> — N events (HH:MM–HH:MM)` + one line per event. If drain's send fails, rows STAY queued (retry next tick) — but drop rows older than 6h with a suppressed-log entry (no infinite replay, F2). **Delivery semantics are at-least-once, explicitly:** mark rows sent AFTER the send returns ok — a crash in between duplicates rather than drops (right trade for alerts). Prefix requeued tier-1 messages with `(redelivery)` so an ambiguous-timeout duplicate is self-explaining (F6).
- [ ] **Step 3-5:** Tests PASS, commit `feat(alerts): tiered dispatch queue on shadow_trades.db`.

### Task 5.2: Wire drain into the scheduler (ungated 5-min task)

- [ ] Add `task_alert_drain` to `TICK_TASKS["5min"]` (NOT `5min_gated` — gate counters reset on the 15-min restart and would starve it): `from signals.alert_dispatch import drain; drain()`. Commit.

### Task 5.3: Migrate pipelines — shadow first, lowest-risk first

- [ ] **Step 1 (shadow):** For `whale_resolutions`, `rising_wallets`, `leaderboard_wallets`, `graduation`: keep the direct `alert_openclaw` send AND call `dispatch(..., tier=2, shadow=True)` (tier 2 only — tier 3 is deferred per D1). After 48h compare `alert_shadow_log` vs actual sends — confirm nothing critical would have been delayed.
- [ ] **Step 2 (enforce):** Flip those pipelines to dispatch-only. Smart-wallet **exits/convergence** → tier 2. Smart-wallet **entries stay immediate** (tier 1) until the CLV-decay measurement from the vault doc clears batching. Bybit listings + oracle tests → tier 4. Commit per pipeline.
- [ ] **Step 3 (digest, deferred per D1):** Only if post-rollout volume still exceeds ~15/day: add tier 3 + a 20:00 ET digest flush, OR simply append summary lines to the existing daily P&L report (cheaper). Re-evaluate after 1 week of data.

### Task 5.4: Hourly delivery-failure alarm

- [ ] Extend `scripts/send_ledger_watchdog.py` with `--hours 1 --min-rate 0.10`: if failure rate over the window ≥ 10% and ≥3 failures, send 🚨 (plain text, parse_mode=None — the format least likely to share the failure cause). Add VPS cron at `:05` hourly, sourcing `alerts.env`. Verify with a forged failing ledger line. Commit.

### Task 5.5: Cross-host dead-man's switch (who watches the watchman)

Every layer so far — senders, ledger, watchdog, drain — lives on the VPS. If the VPS or scheduler dies wholesale, the result is silence, and silence triggers nothing. SRE practice (Grafana metamonitoring, Prometheus Watchdog pattern) is an *external* host checking for the heartbeat's absence.

**Files:** Create `~/bin/polyclawd-deadman.sh` (Mac Mini, NOT in the repo)

- [ ] **Step 1:** Mac Mini script: `ssh vps "tail -1 /var/www/virtuosocrypto.com/polyclawd/logs/telegram_sent.jsonl"` → parse `ts`; if older than 2h (Polyclawd normally sends far more often) OR ssh fails, send alarm via the Mac's OWN Telegram path (openclaw gateway — a fully independent delivery chain).
- [ ] **Step 2:** Install as Mac launchd job, hourly (use the `launchd-agent-author` skill conventions; log to `~/Library/Logs/polyclawd-deadman.log`).
- [ ] **Step 3:** Test both branches: normal (silent), and with the ssh host temporarily wrong (alarm arrives). Commit script to dotfiles/notes per Mac conventions; record in vault doc.

### Task 5.6: Interrogate the 15-min restart itself (root pathology, P2)

The plan *survives* `polyclawd-health-check.sh` restarting the scheduler every ~15 min — but an expert would ask why a health check restarts a healthy service 96x/day at all. That cadence erases in-memory state fleet-wide and masks real crashes.

- [ ] Read the script on the VPS: what does it actually probe, and why does a healthy scheduler fail the probe? Fix the probe (or its timeout) so restarts happen only on genuine hangs. Do NOT change this before Phase 5 lands (the dispatch layer must be restart-proof regardless — restarts will still happen on real failures).

---

## Phase 6 — Deploy + acceptance (STAGED — F1)

- [ ] **Stage 1: send layer alone.** Deploy only `scripts/openclaw_alerts.py` (+ its tests): md5-diff vs VPS, splice, deploy, restart scheduler. Watch the ledger for 1h: `ssh vps "tail -50 .../logs/telegram_sent.jsonl | grep ok.:false"` — new failures beyond the pre-existing rate = revert immediately (single file).
- [ ] **Stage 2: everything else.** Per-file: md5-diff vs VPS, splice, `~/bin/polyclawd-deploy`, `ssh vps "sudo systemctl restart polyclawd-scheduler && systemctl status polyclawd-scheduler"`.
- [ ] Run `scripts/stop_selftest.py` on VPS → 🛑 `[SELFTEST]` stop-close alert arrives in Telegram (per Task 2.2 — the ⚠️ warning path is unreachable at default thresholds).
- [ ] Restart-survival test: enqueue a tier-2 test alert, `sudo systemctl restart polyclawd-scheduler` before the 15-min window closes, confirm the batch still flushes on schedule.
- [ ] 24h later: ledger failure rate <1%; alert count ≤20; zero hex IDs in received alerts. Record all three in the vault doc §6 table.

## Rollback

Each phase reverts independently: send-layer changes are additive (revert file); heartbeat/selftest are new code paths; dispatch pipelines flip back to direct `alert_openclaw` per pipeline (one-line change each); drain task can be removed from TICK_TASKS leaving queue inert; HF thresholds are config values. The queue table is additive — no schema migration to unwind.

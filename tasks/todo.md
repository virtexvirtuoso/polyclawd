# Polyclawd — Pending Tasks

> Updated 2026-06-11. Scope: Kalshi weather fade strategy + shadows.
> Strategy registry: vault `02-Projects/Polyclawd/Strategy/Kalshi-Fade-Optimization-Knobs.md`
> Evidence base: vault `02-Projects/Polyclawd/Research/Weather-Edge-Analysis-Jun2026.md` (§10 + QA corrections)

## Dated decisions (evidence accumulates automatically — just decide on the day)

- [ ] **Jun 12** — First realized verdict: did the 4 taker positions win; were maker fills profitable or adverse-selected (join-mode `ev_per_dollar`, both venues). Arrives in the 10:00 Telegram report; one-shot Claude check at 10:23.
- [ ] **~Jun 13** — Flip knob 1 (ranked fill) after 2–3 clean baseline nights: `POST /api/engine/weather-strategies?kalshi_fade_ranked_fill=true` (no deploy needed). Consider the ET-eats-the-cap observation (vault knobs doc §3b) when judging week-one results.
- [ ] **~Jun 17** — Maker read (1 week of fills): decides knob 7 (PM maker) build-or-skip AND whether to design knob 2 implementation.
- [ ] **~Jun 24** — Favorite tier (0.50–0.70 buy-YES): resize ($50→$100) or kill, using executable-price EV from collected sheets. Mid-based +12% is UNVERIFIED.
- [ ] **~Jun 24–28** — Two-week gate review: realized taker EV vs the QA band [+0.4%, +2.0%]/$1. Pass → live-money discussion gated on the compliance checklist below. Fail → extend or kill.

## Pre-live compliance gate (none blocks paper; ALL block live capital)

- [ ] **Mr. V reads Kalshi Developer Agreement + Member Agreement** (~30 min; bot-blocked pages, human-only). kalshi.com/developer-agreement, kalshi.com/docs/kalshi-member-agreement.pdf
- [x] Maker blackout windows — DONE 2026-06-11: ±10min around 00/06/12/18Z synoptic times in both maker shadows (deployed; live maker logic inherits the same rule)
- [ ] Tax record pipeline: trade-level export (Kalshi issues NO 1099-B for trading P&L); CPA question on §1256 vs ordinary
- [ ] Real-time fill monitor (Kalshi erroneous-trade review window = 15 minutes)
- [ ] Encode $25k/contract position-limit hard ceiling (bids count toward it)
- [ ] Live funds policy: minimal float on exchange, scheduled profit sweeps (segregation ≠ insurance)
- [ ] If maker validates: apply to Kalshi Liquidity Incentive Program (paid resting orders)

## Engineering / research (this week, unblocked)

- [x] Knob 6 backfill spike — DONE 2026-06-11, verdict SPLIT: ensemble MEMBERS not recoverable (archive keeps members for today only; previous-runs serves none) → distribution engine trains on forward-collected snapshots. Deterministic lead-1 history IS recoverable and was archived same-day before the 90-day window rolls (`scratch/data/deterministic_lead1_archive_20260611.json`, 20 cities × 2 models, Apr 2 →) → bias-correction layer can be fitted against the 26k labels now
- [x] Knob 4 — DONE 2026-06-11, NULL RESULT: tail mispricing is FLAT across the whole evening (gap −4.5 to −5.1¢ at local 16/18/20/22/24, all sig, all CIs overlapping; spreads flat 2.2–2.5¢). Entry hour is not a lever — 19:30–20:30 window stays. Removes knob 4 from the board. Side hypothesis for later: window could be WIDENED for capacity (more candidates, same edge) — post knob-1, own validation. `scratch/kalshi_timing_curve.py`
- [ ] Watch: MLB-props 429 rate-limit bursts share Kalshi's API budget with the fade scanner — if a night shows mass `no_quotes` skips, this is suspect #1
- [ ] Watch: Sunday Jun 15 — repaired Weekly Signal + IC Calibration cron proves itself (timeout was the failure)

## Housekeeping (awaiting explicit go from Mr. V)

- [x] Formal `/decision-log-entry`: weather kill-trigger decision — logged 2026-06-11 in `00-Dashboard/Decision Log.md` + daily note
- [x] Skills Backlog: `kalshi-market-data` added 2026-06-11
- [x] Broken wikilinks fixed 2026-06-11: both evidence notes reconstructed (provenance-marked) in Polyclawd/Research/

## Done (this build, for context)

- [x] Two-venue tail-calibration audit + QA corrections (§10)
- [x] Kalshi fade paper strategy deployed (entries live 2026-06-10, cap bound night one)
- [x] Maker shadows live both venues; first fills measured (K 43%/74%, PM 29%/46%)
- [x] Ensemble recorder live (knob 6 training data)
- [x] Dashboards replaced; crons updated; void/zombie resolver fixes; blindspots audit

---

# Codebase Structure Cleanup (audit 2026-06-25)

> Source: vault `02-Projects/Polyclawd/Development/Codebase-Structure-Review-2026-06-25.md`.
> Branch `feature/test-hygiene-and-db-timeout` has landed the items under "Done" below.

## Done (2026-06-25, on feature/test-hygiene-and-db-timeout)
- [x] Repair 9 stale unit tests → unit suite green (255/255)
- [x] Fix 3 loguru %-format calls in volume_spike_detector (dropped args)
- [x] Auth + cap `GET /api/visitor-log` (also edge-locked in nginx, live)
- [x] `busy_timeout` on shadow_trades.db writers: scheduler.py (3) + markets.py (2)
- [x] `db.py` central `connect()` wrapper (WAL + busy_timeout) + `scripts/migrate_db_connect.py`
- [x] Migrate api/ package connect sites to `db_connect` (10 files, 19 sites)

## Finish the db.connect() rollout (#1)
- [ ] Migrate remaining ~85 CLEAN connect sites (signals/odds/services/execution) to `db_connect`.
      BLOCKED on packaging (#4): standalone-run modules can't import top-level `db` until installable.
      Then: `python scripts/migrate_db_connect.py <list> --apply` → py_compile + tests.
- [ ] Migrate the 27 WIP-contaminated connect files AFTER `feature/soccer-ufc-worldcup-engines` merges (avoid conflicts).
- [ ] Drop now-redundant inline `PRAGMA busy_timeout` lines in markets.py (wrapper sets it).

## Dependency reconciliation + CI (#6 → #3) — DONE 2026-06-25 (commit 06438a9)
- [x] Pinned web stack to VPS versions + `[tool.uv] override-dependencies=["starlette==0.52.1"]` → clean resolve reproduces prod.
- [x] requires-python / .python-version / CI aligned to 3.12 (VPS runs 3.12.3).
- [x] Regenerated uv.lock; added Tests (unit) job to pr-validation.yml. Clean `uv sync` + `pytest tests/unit` = 255 passed on 3.12; 7 TestClient errors fixed.
- [ ] FOLLOW-UP: requirements.txt is now superseded by pyproject (its pins were stale vs VPS) — reconcile/remove after confirming the deploy doesn't `pip install -r` it.
- [ ] FOLLOW-UP: widen the CI test job to integration/contract once #8 verifies them.

## CI lint/format jobs are RED — pre-existing ruff debt (separate from pytest)
- [ ] `ruff check .` reports ~3000 violations repo-wide (mostly UP006 `Dict`→`dict`, import sorting). The lint + format
      CI jobs fail independently of the now-green pytest job. Options: a `ruff check --fix` (+`--unsafe-fixes`) sweep
      on a clean tree, or narrow the ruff `select` set / bump `target-version` to py312. Do on a clean base, not mid-WIP.

## Config layer (H4)
- [ ] Promote `api/deps.py:Settings` to pydantic-settings `BaseSettings`; centralize DB_PATHS, RPC, Simmer URL, trade limits.
- [ ] Migrate ~81 scattered `os.getenv` sites to `get_settings()`, module by module.

## God-file decomposition (#2 / H1, H3) — behind green CI, in a worktree
- [ ] Split `api/routes/markets.py` (2,426 lines) → `api/routes/markets/{hf,vegas,espn,baseball,crossplatform}` (mostly mechanical).
- [ ] Split `api/routes/signals.py` (4,334) AND move embedded scanning/scoring out to `signals/` + `services/signal_aggregation.py`.
- [ ] After each move: contract tests + diff `/api/openapi.json` (endpoint set unchanged).

## Packaging (#4 / M1) — biggest; sequence LAST; coordinate with file-copy deploy
- [ ] `src/polyclawd/` layout + `[build-system]` + `pip install -e .`; delete the 65 `sys.path` hacks.
- [ ] Break the api↔services import cycle (extract a shared leaf package).
- [ ] Unblocks the full db.connect() migration above.

## Prod auth (#7) — separate ops task, own maintenance window
- [ ] Audit every client (dashboard JS, crons) sends `X-API-Key`, THEN set `POLYCLAWD_API_KEYS` in `/etc/default/polyclawd` + restart.
- [ ] Edge-harden other sensitive endpoints in nginx (defense-in-depth, like visitor-log).
- [ ] Until then app-layer auth is a no-op in prod — see vault `API-Keyless-Dev-Mode-And-Visitor-Log-Lockdown-2026-06-25`.

## Test tree (#8)
- [ ] Run + triage `tests/integration`, `tests/contract`, `tests/security` (likely more stale tests + mock wiring).
- [ ] Add the green ones to CI; keep `locust` load tests OUT of PR CI.

## CLAUDE.md doc drift (M2)
- [ ] Fix Key Directories / Development Workflow tables — they reference `src/`, `whale-tracker/`, `integrations/`, `frontend/` that don't exist.

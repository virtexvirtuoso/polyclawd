# Polyclawd Architecture Refactoring Plan

> **Date:** 2026-05-30 (v2 — with gaps analysis)  
> **Scope:** Full codebase analysis at `~/Desktop/polyclawd/`  
> **Goal:** Clean, scalable, maintainable architecture — no behavior changes.

---

## Table of Contents

1. [Current State Summary](#1-current-state-summary)
2. [Gaps & Breakage Analysis](#2-gaps--breakage-analysis)
3. [Pre-Mortem: What Will Break](#3-pre-mortem-what-will-break)
4. [Tech Lead Assessment](#4-tech-lead-assessment)
5. [Code Review of the Plan](#5-code-review-of-the-plan)
6. [Proposed Folder Structure](#6-proposed-folder-structure)
7. [Architecture Breakdown](#7-architecture-breakdown)
8. [Key Refactoring Recommendations](#8-key-refactoring-recommendations)
9. [Execution Phases](#9-execution-phases)
10. [Dependency Map](#10-dependency-map)
11. [Verification Strategy](#11-verification-strategy)

---

## 1. Current State Summary

### By the numbers

| Metric | Value |
|---|---|
| Total Python files | ~120 |
| Largest file | `api/routes/signals.py` — **3,389 lines** |
| 2nd largest | `signals/paper_portfolio.py` — **2,061 lines** |
| 3rd largest | `api/routes/markets.py` — **1,940 lines** |
| 4th largest | `signals/weather_ensemble.py` — **1,831 lines** |
| 5th largest | `signals/weather_scanner.py` — **1,569 lines** |
| 6th largest | `api/routes/engine.py` — **1,267 lines** |
| 7th largest | `signals/mispriced_category_signal.py` — **1,090 lines** |
| Total in top 7 | **13,147 lines** |
| `api/services/` files | 5 (underused) |
| `signals/` files | 27 (flat, no `__init__.py`) |
| `odds/` files | 17 (flat, has `__init__.py` with re-exports) |
| `services/` files | 12 (HF trading, no `__init__.py`) |
| `sys.path.insert` calls | **~80** across 6 files |
| `urllib.request` usage | **8 files** |
| Lazy imports (inside functions) | **~60** (by design, avoids circular imports) |
| Bare imports (no `signals.` prefix) | **~20** (will break when sys.path is removed) |

### Key structural problems

1. **God router in `api/routes/signals.py` (3,389 lines)** — handles signal aggregation, whale tracking, confidence scoring, volume spikes, resolution timing, correlations, portfolio management, weather, tweets, archetype classification, basket arb, copy-trade, calibration, IC tracking, election data, CLARITY Act, alert analytics, and more. This is 7-8 routers in one file.

2. **God signal in `signals/mispriced_category_signal.py` (1,090 lines)** — mixes archetype classification, kill rules, Kalshi API fetching, Polymarket API fetching, confidence scoring, cross-platform matching, caching, shadow logging.

3. **Flat directory structure** — `signals/` has 27 flat files with no `__init__.py`. No grouping by domain.

4. **Duplicate HTTP client logic** — `urllib.request` used directly in 8 files across `signals/` and `odds/`. The shared `http_client.py` in `api/services/` is barely used.

5. **Inline SQLite queries** — scattered across `signals.py`, `paper_portfolio.py`, `shadow_tracker.py`, `alpha_score_tracker.py`, `resolution_logger.py` — each with its own connection management and table schemas.

6. **~80 `sys.path.insert` calls** — in `signals.py`, `markets.py`, `engine.py`, `cross_platform_edge.py`, `hf_paper_trader.py`, `stop_evaluator.py`, `scheduler.py`, `conftest.py`, and test files. Every endpoint function does its own `sys.path.insert(0, signals_path)` + lazy import.

7. **`api/services/` is underused** — only 5 files. The service layer should be the primary business logic layer, not the routes.

8. **Mixed concerns in `api/main.py`** — visitor logging (SQLite + Discord webhook) lives in the app factory.

9. **No clear domain boundaries** — `signals/`, `odds/`, `services/`, and `src/strategies/` overlap in responsibility.

10. **`src/strategies/mispriced_category_whale.py` is orphaned** — zero imports from anywhere. Safe to delete.

---

## 2. Gaps & Breakage Analysis

### 2.1 `signals/` is NOT a Python package

There is no `signals/__init__.py`. Everything works today because of 80+ `sys.path.insert(0, signals_path)` calls in route handlers. The plan says "use proper package imports" but that requires creating `signals/__init__.py` first — and that changes how every single lazy import resolves.

**Impact:** Creating `signals/__init__.py` is Step 0, not Phase 1. Without it, no other refactoring can proceed.

### 2.2 `services/` (HF trading) is also not a package

No `services/__init__.py`. `services/scheduler.py`, `services/stop_evaluator.py`, and `services/hf_paper_trader.py` all do lazy imports from `signals.*` modules. If we restructure signals, these break.

### 2.3 Lazy imports are everywhere by design (circular imports)

Almost every cross-module import is done lazily inside function bodies to avoid circular imports at module load time. Moving to top-level imports will cause circular import crashes in at least these pairs:

| Cycle | Files involved |
|---|---|
| **C1** | `mispriced_category_signal.py` ↔ `shadow_tracker.py` |
| **C2** | `paper_portfolio.py` ↔ `discord_alerts.py` |
| **C3** | `paper_portfolio.py` ↔ `resolution_logger.py` |
| **C4** | `weather_scanner.py` ↔ `weather_ensemble.py` |
| **C5** | `paper_portfolio.py` ↔ `cv_kelly.py` |
| **C6** | `paper_portfolio.py` ↔ `alpha_score_tracker.py` |

These cycles are currently handled by lazy imports. Any refactoring that moves to top-level imports must break these cycles first.

### 2.4 Inconsistent import paths (bare vs. qualified)

Some files use `from paper_portfolio import X` (bare), others use `from signals.paper_portfolio import X`. The bare imports work because of sys.path manipulation. Every bare import will silently break when sys.path is removed.

**Bare imports in `api/routes/signals.py`:**
- `from paper_portfolio import ...` (10 occurrences)
- `from shadow_tracker import ...` (2 occurrences)
- `from weather_scanner import ...` (1 occurrence)
- `from weather_ensemble import ...` (1 occurrence)
- `from resolution_logger import ...` (1 occurrence)
- `from cv_kelly import ...` (1 occurrence)

**Bare imports in `services/hf_paper_trader.py`:**
- `from paper_portfolio import ...` (1 occurrence)

**Bare imports in tests:**
- `from paper_portfolio import ...` (1 occurrence)
- `from weather_scanner import ...` (1 occurrence)

### 2.5 `odds/` already has an `__init__.py` with re-exports

`odds/__init__.py` re-exports specific functions from `odds.edge_math`, `odds.smart_matcher`, `odds.vegas_scraper`, etc. If we move files into sub-packages, the existing `__init__.py` exports break, and everything that does `from odds.edge_math import shin_no_vig` breaks.

**Recommendation:** Don't split `odds/` into sub-packages. The existing `__init__.py` re-exports are already consumed by 6+ files. Sub-packages add complexity with no benefit for 17 files. Just organize within the flat namespace.

### 2.6 Sync/async mismatch in HTTP client

`urllib.request` is used in 8 files across `signals/` and `odds/`. The shared `api/services/http_client.py` is async (httpx), but most signal modules run synchronously (called from sync scheduler threads). Switching to async requires either:
- Making signal modules async (ripple effect through scheduler)
- Keeping a sync wrapper in the shared client

**Recommendation:** Create a sync-compatible `OddsHttpClient` that works in both contexts.

### 2.7 Database access is fragmented

Each of these files has its own connection management and table schemas:
- `paper_portfolio.py` — `DB_PATH` constant, own connection
- `shadow_tracker.py` — own connection management
- `alpha_score_tracker.py` — `_get_conn()` function
- `resolution_logger.py` — own DB access
- `api/routes/signals.py` — inline SQLite for visitor_log, wr_buckets, weather dashboard

A unified repository requires reconciling all of them. This is Phase 4 work, not Phase 1.

### 2.8 MCP server is safe

The MCP server (`mcp/server.py`) auto-discovers tools from the OpenAPI spec. It doesn't import any signal or odds modules directly. No changes needed.

### 2.9 Phantom imports — files imported but don't exist

`api/routes/signals.py` imports 4 files that **don't exist** in the filesystem:

| Import | Line | Wrapped in try/except? |
|---|---|---|
| `from signals.election_signal import generate_election_signals` | 833 | Yes |
| `from signals.election_tracker import generate_report` | 2995, 3045, 3142 | Yes |
| `from signals.polymarket_price_history import get_price_history` | 3298 | Yes |
| `from signals.congress_bill_tracker import build_clarity_bills_overlay` | 3350 | Yes |

These are **dead code paths** — the imports fail silently at runtime and the endpoints return fallback data. The plan doesn't mention these. They should be:
- Either created (if the functionality is needed)
- Or removed (if the functionality was abandoned)

**Recommendation:** Remove the dead import blocks in Phase 0. They're noise and will cause confusion during refactoring.

### 2.10 `requests` library used in 4 odds/ files

The plan only mentions `urllib.request` for HTTP consolidation. But 4 files in `odds/` use the `requests` library (synchronous):
- `odds/kalshi_edge.py`
- `odds/betfair_edge.py`
- `odds/soccer_edge.py`
- `odds/vegas_scraper.py`

These are a separate HTTP library from both `urllib.request` and `httpx`. Phase 3 (HTTP consolidation) must also migrate these 4 files.

### 2.11 `httpx` used directly in 5 signals/ files (not just urllib.request)

5 files in `signals/` import `httpx` directly instead of using the shared `api/services/http_client.py`:
- `signals/basket_arb_scanner.py`
- `signals/mispriced_category_signal.py` (line 813, inside function)
- `signals/cross_platform_arb.py`
- `signals/alpha_score_tracker.py`
- `signals/copy_trade_watcher.py`

These are already using the right library but bypassing the shared client. Phase 3 must also consolidate these.

### 2.12 Bare imports in tests (more than catalogued)

The plan only listed 2 test files with bare imports. There are actually **5**:

| Test file | Bare import |
|---|---|
| `tests/unit/test_correlation_cap.py` | `from paper_portfolio import ...` |
| `tests/unit/test_momentum_filter.py` | `from price_momentum_filter import ...` |
| `tests/unit/test_strike_probability.py` | `from strike_probability import ...` |
| `tests/unit/test_weather_scanner.py` | `from weather_scanner import ...` |
| `tests/unit/test_volume_spike.py` | `from volume_spike_detector import ...` |

All 5 must be fixed in Phase 0.

### 2.13 `scripts/prediction_market_backtest.py` uses bare top-level imports

Unlike the lazy imports in route handlers, `scripts/prediction_market_backtest.py` uses **top-level** bare imports:
```python
from mispriced_category_signal import (
    MISPRICED_CATEGORIES, POLYMARKET_MISPRICED_TAGS, ...
)
from empirical_confidence import (
    calculate_empirical_confidence, ...
)
```

These will crash **immediately** when `signals/__init__.py` is created (the imports resolve differently). This script is a standalone CLI tool, not part of the API. It needs its own import fix.

### 2.14 `services/hf_paper_trader.py` bare import is inside a function

`services/hf_paper_trader.py:211` does `from paper_portfolio import open_position, get_portfolio_status` inside a function body. This is a lazy import, so it won't crash at module load — but it will crash at runtime when the function is called. Same pattern as the route handlers.

---

## 3. Pre-Mortem: What Will Break

> Pre-mortem for Polyclawd Architecture Refactoring: top 3 failure modes ranked by combined risk score.

### 3.1 Circular import cascade — HIGH risk

**Story:** Phase 1 extracts `archetype_classifier.py` from `mispriced_category_signal.py`. The new file is imported by `empirical_confidence.py`, `paper_portfolio.py`, `shadow_tracker.py`, and `signals.py`. But `shadow_tracker.py` is also imported BY `mispriced_category_signal.py`. When we add `signals/__init__.py` and try to use proper package imports, the circular dependency between `mispriced_category_signal` and `shadow_tracker` crashes at module load time. The API won't start. Rollback takes 30 minutes.

**Leading indicators:**
- Any two files that import each other (directly or transitively) will crash
- Currently handled by lazy imports — moving to top-level imports exposes the cycle

**Mitigation:** Break cycles BEFORE creating `__init__.py`. Extract `archetype_classifier.py` and `shadow_logger.py` as standalone modules that nothing imports back from. Then `shadow_tracker.py` can import from `archetype_classifier` without cycles.

### 3.2 Bare import silent failures — HIGH risk

**Story:** Phase 1 removes `sys.path.insert(0, signals_path)` from `api/routes/signals.py`. But 10 endpoint functions use `from paper_portfolio import ...` (bare, no `signals.` prefix). These imports silently fail at runtime when the endpoint is called — not at startup. The first user to hit `/api/portfolio/status` gets a 500 error. No test covers this path. The bug lives in production for 3 days.

**Leading indicators:**
- 20 bare imports catalogued above
- No integration tests for most endpoint paths
- Lazy imports mean failures only surface on first call

**Mitigation:** Fix all bare imports to qualified (`from signals.paper_portfolio import ...`) BEFORE removing any `sys.path.insert` call. Do this in a single focused pass.

### 3.3 Test suite breakage — MEDIUM risk

**Story:** Tests in `tests/unit/` also use `sys.path.insert` and bare imports. When signals becomes a proper package, the test imports break. The test suite silently stops running (import errors at the top of test files). No one notices for 2 weeks because CI isn't set up.

**Leading indicators:**
- 6 test files use `sys.path.insert(0, .../signals)`
- 5 test files use bare imports (not 2 as originally catalogued)
- No CI pipeline to catch test failures

**Mitigation:** Fix all 5 test bare imports in Phase 0. Add `make test` target.

### 3.4 Phantom import crashes — MEDIUM risk

**Story:** Phase 0 creates `signals/__init__.py`. The API starts fine. But when an endpoint calls `from signals.election_tracker import generate_report`, the import fails because `election_tracker.py` doesn't exist. The endpoint returns a 500 error instead of the expected fallback data. The try/except was masking this — but the refactoring changes how imports resolve, and the except path might not handle it the same way.

**Leading indicators:**
- 4 phantom imports in `api/routes/signals.py` (lines 833, 2995, 3298, 3350)
- All wrapped in try/except — currently silent failures
- No test coverage for these endpoints

**Mitigation:** Remove the 4 dead import blocks in Phase 0. They're noise.

### 3.5 Script import crash — LOW risk

**Story:** `scripts/prediction_market_backtest.py` uses top-level bare imports from `mispriced_category_signal` and `empirical_confidence`. When `signals/__init__.py` is created, these imports crash immediately because Python resolves them differently. The script is a standalone CLI tool — not part of the API — so it doesn't affect production. But it breaks the development workflow.

**Leading indicators:**
- Top-level `from mispriced_category_signal import ...` at line 32
- Top-level `from empirical_confidence import ...` at line 48
- Not wrapped in try/except

**Mitigation:** Fix in Phase 0. Change to `from signals.mispriced_category_signal import ...` and `from signals.empirical_confidence import ...`.

**Mitigation:** Fix test imports in the same pass as fixing bare imports. Add a `pytest` smoke check to the Makefile.

---

## 4. Tech Lead Assessment

> Applied: `tech-lead-advisor` skill — Decision Framework (Technical Excellence, Business Impact, Team Dynamics, Long-term Vision)

### Decision Framework

| Criterion | Assessment | Score (1-5) |
|---|---|---|
| **Technical Excellence** | Current architecture has 6 god files, 80 sys.path hacks, 6 circular import cycles, and no package structure. This is unsustainable. The refactoring plan addresses all of these with concrete phases. | 4/5 — sound approach |
| **Business Impact** | The system works today. Refactoring doesn't add features. But the current architecture makes it impossible to add new signal sources, fix bugs confidently, or onboard contributors. The cost of NOT refactoring grows linearly with each new signal module. | 3/5 — medium urgency, high long-term value |
| **Team Dynamics** | Solo operator (Mr. V). No team to coordinate with. This reduces risk significantly — no merge conflicts, no communication overhead, no blocking dependencies. The main risk is time spent vs. feature development. | 5/5 — ideal conditions for refactoring |
| **Long-term Vision** | Polyclawd is growing (67 MCP tools, 120+ Python files, multiple signal sources). The current flat structure will not scale to 200+ files. The proposed layered architecture (routes → services → domain → infrastructure) is a standard pattern that will serve for years. | 5/5 — necessary for scale |

**Verdict:** Proceed with refactoring. The solo-operator context makes this low-risk. The 6-phase approach with commit-after-each-phase is correct.

### Build-vs-Buy Assessment

| Decision | Recommendation | Rationale |
|---|---|---|
| Shared HTTP client | **Build** (already partially exists) | `api/services/http_client.py` exists but is async-only. Add sync path. |
| Database repository layer | **Build** | 5 separate SQLite connection patterns. Consolidation is straightforward. |
| Archetype classifier as standalone module | **Build** (extract from existing) | Already written, just needs extraction. Zero new code. |
| MCP server rewrite | **Don't touch** | Auto-discovers from OpenAPI. No changes needed. |
| `odds/` sub-packages | **Don't do** | 17 files, existing `__init__.py` re-exports. Flat is fine. |

### Tech Debt Prioritization

| Debt Item | Severity | Effort | Phase |
|---|---|---|---|
| `signals/` not a package | 🔴 Blocking | 2h | 0 |
| Circular import cycles | 🔴 Blocking | 1h | 0 |
| Bare imports (20) | 🔴 Will break | 30min | 0 |
| God router signals.py (3389 lines) | 🟡 High | 4h | 1 |
| God signal mispriced (1090 lines) | 🟡 High | 2h | 1 |
| No service layer | 🟡 High | 6h | 2 |
| HTTP client fragmentation | 🟡 Medium | 2h | 3 |
| SQLite fragmentation | 🟡 Medium | 4h | 4 |
| Orphaned strategy file | 🟢 Low | 5min | 5 |
| `odds/` sub-packages | 🟢 Don't do | — | — |

### Key Recommendation from Tech Lead

**Don't over-layer.** The plan proposes `routes → services → domain → infrastructure`. This is the right number of layers. Don't add `controllers`, `repositories` (beyond the SQLite one), `adapters`, or `ports`. The system is a FastAPI app with signal generators — not a microservices mesh. Three layers (presentation, application, domain) plus infrastructure utilities is exactly right.

**Don't rename everything.** `paper_portfolio.py` is a well-understood name. Don't rename it to `portfolio_service.py` in the domain layer. Keep names that the team (Mr. V) already knows.

**Don't refactor `services/` (HF trading).** The 12 HF trading files are well-separated. They have their own scheduler, engine, triggers, velocity, risk gate, collector, paper trader, and bridge. This is already good architecture. Leave it alone.

---

## 5. Code Review of the Plan

> Applied: `code-review` skill — Security, Performance, Pattern Consistency, Breaking Changes checklists

### Security Checklist

| Check | Status | Notes |
|---|---|---|
| No hardcoded secrets in diff? | ✅ | Plan doesn't touch secrets |
| SQL queries parameterized? | ✅ | Plan creates repository layer with parameterized queries |
| No path traversal risk? | ✅ | No file path changes |
| Rate limiting preserved? | ✅ | `slowapi` Limiter is in `main.py`, not touched |
| Auth checks preserved? | ✅ | Security middleware in `middleware.py`, not touched |
| No secrets in logs? | ✅ | No logging changes |

**Security verdict:** No security concerns. The plan is purely structural.

### Performance Checklist

| Check | Status | Notes |
|---|---|---|
| No N+1 queries introduced? | ✅ | No new queries |
| Pagination preserved? | ✅ | Route params preserved |
| Connection pooling? | ⚠️ | SQLite is single-connection. Repository layer should use a single shared connection, not open new ones per call. Add this to Phase 4. |
| Caching preserved? | ✅ | `edge_cache.py` and `_cache` dicts not touched |
| Async/background preserved? | ✅ | `asyncio.create_task(prewarm_election_cache())` not touched |
| API timeouts preserved? | ✅ | `httpx.AsyncClient(timeout=30.0)` not touched |

**Performance finding:** Phase 4 (database layer) must use a single shared SQLite connection, not open a new one per repository call. The current code has 5 separate connection patterns — the repository should consolidate to one.

### Pattern Consistency Checklist

| Check | Status | Notes |
|---|---|---|
| Naming conventions consistent? | ⚠️ | Plan proposes `signals_whale.py`, `signals_confidence.py` etc. But existing routes use singular: `signals.py`, `markets.py`, `engine.py`. Either all plural or all singular. Recommend: `whale.py`, `confidence.py`, `portfolio.py` under `api/routes/signals/` sub-directory instead of prefix naming. |
| Error handling consistent? | ✅ | Existing `HTTPException` pattern preserved |
| Import style consistent? | ✅ | All moving to qualified `from signals.X import Y` |
| Logging consistent? | ✅ | `logger = logging.getLogger(__name__)` pattern preserved |

**Pattern finding:** The plan uses prefix naming (`signals_whale.py`, `signals_confidence.py`) which is inconsistent with the existing `signals.py`, `markets.py`, `engine.py` naming. Better approach: create `api/routes/signals/` as a sub-package with `__init__.py` that re-exports, and name files `whale.py`, `confidence.py`, `portfolio.py` inside it. This keeps the import path clean: `from api.routes.signals.whale import router`.

### Breaking Changes Checklist

| Check | Status | Notes |
|---|---|---|
| API response structure changed? | ❌ No | Plan explicitly says "no behavior changes" |
| Endpoint paths changed? | ❌ No | Same paths, just split across files |
| Function signatures changed? | ⚠️ | Only internal refactors — no public API changes |
| Database schema changed? | ❌ No | Phase 4 adds repository layer but doesn't change schema |
| Import paths for external consumers? | ❌ No | MCP auto-discovers from OpenAPI, not imports |

**Breaking changes verdict:** Zero breaking changes to the API surface. The MCP server auto-discovers from OpenAPI spec — it doesn't import any signal/odds modules directly. All changes are internal.

### Code Review Verdict

```
## Summary
Architecture refactoring plan for Polyclawd codebase. 6 phases, no behavior changes.

## Security
✅ No security concerns

## Performance
⚠️ Phase 4 must use single shared SQLite connection (not one per repository call)

## Patterns
⚠️ Use `api/routes/signals/` sub-package instead of `signals_whale.py` prefix naming

## Breaking Changes
✅ Zero API surface changes

## Verdict
✅ Approved — proceed with Phase 0
```

---

## 6. Proposed Folder Structure

```
polyclawd/
├── api/                          # FastAPI application (presentation layer)
│   ├── main.py                   # App factory, lifespan, CORS, static files (slim)
│   ├── deps.py                   # Settings/dependencies (keep)
│   ├── middleware.py             # Security headers, exception handler (keep)
│   ├── models.py                 # Pydantic models (keep)
│   ├── routes/                   # Route handlers (thin — delegate to services)
│   │   ├── __init__.py           # Router aggregation (keep)
│   │   ├── system.py             # /health, /ready, /metrics, /source-health
│   │   ├── trading.py            # /balance, /positions, /trade, /simmer, /paper
│   │   ├── markets.py            # /markets/*, /arb-scan, /rewards (slim to ~500 lines)
│   │   ├── signals.py            # /signals (slim to ~200 lines — just aggregation)
│   │   ├── engine.py             # /engine/*, /alerts/*, /kelly/*, /phase/*
│   │   ├── edge_scanner.py       # /edge/scan, /edge/calculate (keep)
│   │   ├── signals/              # NEW: sub-package for signal routers
│   │   │   ├── __init__.py       # Re-exports all sub-routers
│   │   │   ├── whale.py          # /predictors, /inverse-whale, /smart-money
│   │   │   ├── confidence.py     # /confidence/*, /conflicts/*
│   │   │   ├── portfolio.py      # /portfolio/*, /rotations
│   │   │   ├── elections.py      # /signals/elections, /signals/clarity
│   │   │   ├── weather.py        # /signals/weather, /weather/dashboard
│   │   │   ├── archetype.py      # /archetype/*, /signals/calibration
│   │   │   ├── arb.py            # /basket-arb, /copy-trade
│   │   │   ├── analytics.py      # /alerts/stats, /signals/scorecard
│   │   │   └── hf.py             # /hf/* (high-frequency scanner endpoints)
│   │   ├── vegas.py              # NEW: /vegas/* (odds, edge, sports, leagues)
│   │   ├── espn.py               # NEW: /espn/* (odds, edge, moneyline, injuries)
│   │   ├── kalshi.py             # NEW: /kalshi/* (markets, entertainment, all)
│   │   ├── polymarket.py         # NEW: /polymarket/* (events, orderbook, microstructure)
│   │   ├── manifold.py           # NEW: /manifold/* (edge, markets, bets, top-traders)
│   │   ├── predictit.py          # NEW: /predictit/* (edge, markets)
│   │   ├── metaculus.py          # NEW: /metaculus/* (questions, edge, divergence)
│   │   └── polyrouter.py         # NEW: /polyrouter/* (unified 7-platform API)
│   └── services/                 # Business logic layer (grow significantly)
│       ├── __init__.py           # (keep)
│       ├── http_client.py        # Shared async HTTP client (keep, expand usage)
│       ├── resilient_fetch.py    # Retry/circuit-breaker logic (keep)
│       ├── storage.py            # Async JSON storage service (keep)
│       ├── source_health.py      # API health registry (keep)
│       ├── cross_platform_edge.py# Edge scanner logic (keep)
│       ├── signal_aggregator.py  # NEW: aggregate_all_signals()
│       ├── whale_service.py      # NEW: whale tracking, inverse whale, smart money
│       ├── confidence_service.py # NEW: Bayesian confidence, calibration, IC tracking
│       ├── portfolio_service.py  # NEW: paper portfolio CRUD, equity curves
│       ├── election_service.py   # NEW: election data, caching, CLARITY filtering
│       ├── weather_service.py    # NEW: weather dashboard, ensemble status
│       ├── archetype_service.py  # NEW: archetype classification, kill rules
│       ├── alert_service.py      # NEW: alert management, analytics
│       ├── visitor_log.py        # NEW: visitor logging extracted from main.py
│       └── database.py           # NEW: repository classes for SQLite
│
├── signals/                      # Signal generation (domain layer)
│   ├── __init__.py               # NEW: makes signals a proper package
│   ├── mispriced/                # Mispriced category signals
│   │   ├── __init__.py
│   │   ├── archetype_classifier.py  # classify_archetype() + kill rules (standalone)
│   │   ├── kalshi_scanner.py        # Kalshi market fetching
│   │   ├── polymarket_scanner.py    # Polymarket market fetching
│   │   ├── confidence_scorer.py     # Confidence scoring + weights
│   │   ├── signal_aggregator.py     # Combine signals, apply kill rules
│   │   └── shadow_logger.py         # Shadow trade logging (standalone)
│   ├── weather/                  # Weather signals
│   │   ├── __init__.py
│   │   ├── scanner.py            # Weather market scanning
│   │   ├── ensemble.py           # Ensemble forecast logic
│   │   └── dashboard.py          # Dashboard data builder
│   ├── election/                 # Election signals
│   │   ├── __init__.py
│   │   ├── tracker.py            # generate_report()
│   │   ├── sentiment.py          # Election sentiment analysis
│   │   └── clarity.py            # CLARITY Act market filtering
│   ├── trading/                  # Trading-related signals
│   │   ├── __init__.py
│   │   ├── shadow_tracker.py     # Shadow trade resolution
│   │   ├── paper_portfolio.py    # Paper portfolio management
│   │   ├── cv_kelly.py           # CV Kelly haircut
│   │   └── time_decay_optimizer.py
│   ├── analytics/                # Signal analytics
│   │   ├── __init__.py
│   │   ├── ic_tracker.py         # Information Coefficient tracking
│   │   ├── calibrator.py         # Confidence calibration
│   │   ├── empirical_confidence.py
│   │   ├── alpha_score_tracker.py
│   │   └── resolution_logger.py
│   ├── monitoring/               # Market monitoring
│   │   ├── __init__.py
│   │   ├── volume_spike_detector.py
│   │   ├── price_momentum_filter.py
│   │   ├── whale_wall_scanner.py
│   │   ├── resolution_scanner.py
│   │   └── tweet_count_scanner.py
│   ├── arb/                      # Arbitrage signals
│   │   ├── __init__.py
│   │   ├── cross_platform_arb.py
│   │   ├── basket_arb_scanner.py
│   │   └── copy_trade_watcher.py
│   ├── ai/                       # AI model tracking
│   │   ├── __init__.py
│   │   ├── ai_model_tracker.py
│   │   └── keyword_learner.py
│   ├── news/                     # News signals
│   │   ├── __init__.py
│   │   └── news_signal.py
│   ├── alerts/                   # Alerting
│   │   ├── __init__.py
│   │   └── discord_alerts.py
│   └── utils/                    # Shared signal utilities
│       ├── __init__.py
│       ├── browser_bridge.py
│       └── strike_probability.py
│
├── odds/                         # Platform connectors (keep flat — has __init__.py)
│   ├── __init__.py               # (keep existing re-exports, add new ones)
│   ├── betfair_edge.py
│   ├── client.py
│   ├── correlation.py
│   ├── edge_math.py
│   ├── espn_odds.py
│   ├── hf_scanner.py
│   ├── kalshi_edge.py
│   ├── manifold.py
│   ├── metaculus.py
│   ├── polymarket_clob.py
│   ├── polyrouter.py
│   ├── predictit.py
│   ├── rate_limiter.py
│   ├── smart_matcher.py
│   ├── soccer_edge.py
│   ├── sports_odds.py
│   └── vegas_scraper.py
│
├── services/                     # HF trading services (keep flat — no __init__.py needed)
│   ├── hf_paper_trader.py
│   ├── hf_engine.py
│   ├── hf_triggers.py
│   ├── hf_velocity.py
│   ├── hf_risk_gate.py
│   ├── hf_collector.py
│   ├── hf_enrichment.py
│   ├── hf_backtest.py
│   ├── scheduler.py
│   ├── stop_evaluator.py
│   ├── virtuoso_bridge.py
│   └── price_logger.py
│
├── mcp/                          # MCP server (keep as-is)
│   ├── server.py
│   └── http_server.py
│
├── src/                          # Research/analysis library (keep as-is)
│   ├── common/
│   ├── analysis/
│   ├── indexers/
│   └── strategies/
│       └── mispriced_category_whale.py  # ORPHANED — delete in Phase 5
│
├── config/
│   └── scaling_phases.py
│
├── scripts/                      # CLI scripts (keep as-is)
├── tests/                        # Tests (update imports)
├── static/                       # Frontend (keep as-is)
└── storage/                      # Runtime data (keep as-is)
```

---

## 7. Architecture Breakdown

### Layer Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    PRESENTATION LAYER                        │
│  api/routes/* (thin routers, ~50-200 lines each)            │
│  - Parse request params                                      │
│  - Call service layer                                        │
│  - Return response                                           │
│  - NO business logic                                         │
│  - NO direct DB access                                       │
│  - NO sys.path manipulation                                  │
├─────────────────────────────────────────────────────────────┤
│                    APPLICATION LAYER                          │
│  api/services/* (business logic orchestrators)               │
│  - Signal aggregation                                         │
│  - Confidence scoring                                         │
│  - Portfolio management                                       │
│  - Election data caching                                      │
│  - Visitor logging                                            │
│  - Calls domain layer for data                                │
├─────────────────────────────────────────────────────────────┤
│                    DOMAIN LAYER                               │
│  signals/* (signal generation)                               │
│  odds/* (platform connectors)                                │
│  services/* (HF trading)                                     │
│  - Pure business logic                                        │
│  - No HTTP awareness                                          │
│  - Testable in isolation                                      │
│  - Domain models + algorithms                                 │
├─────────────────────────────────────────────────────────────┤
│                    INFRASTRUCTURE LAYER                        │
│  api/services/http_client.py                                 │
│  api/services/storage.py                                     │
│  api/services/resilient_fetch.py                             │
│  api/services/source_health.py                               │
│  api/services/database.py                                    │
│  - External API calls                                         │
│  - Database access                                            │
│  - File I/O                                                   │
│  - Caching                                                    │
└─────────────────────────────────────────────────────────────┘
```

### Dependency Direction

```
routes/ → services/ → domain/ → infrastructure/
                ↑           ↑
                └───────────┘
                (services orchestrate domain)

CRITICAL RULE: Inner layers NEVER import outer layers.
- domain/ NEVER imports from routes/ or services/
- services/ NEVER imports from routes/
- infrastructure/ NEVER imports from domain/, services/, or routes/
```

---

## 8. Key Refactoring Recommendations

### 8.1 `api/routes/signals.py` (3,389 lines → ~8 files × ~200 lines)

**Current:** One file handling: signal aggregation, whale tracking, confidence scoring, volume spikes, resolution timing, correlations, portfolio management, weather, tweets, archetype classification, basket arb, copy-trade, calibration, IC tracking, election data, CLARITY Act, alert analytics.

**Extract into:**

| New file | Lines to extract | Contents |
|---|---|---|
| `api/routes/signals.py` (slimmed) | ~200 | Just `/signals` (aggregation endpoint) |
| `api/routes/signals_whale.py` | ~300 | `/predictors`, `/inverse-whale`, `/smart-money`, `/copy-trade` |
| `api/routes/signals_confidence.py` | ~400 | `/confidence/*`, `/conflicts/*`, `/signals/ic-report`, `/signals/ic/{source}` |
| `api/routes/signals_portfolio.py` | ~400 | `/portfolio/*`, `/rotations`, `/signals/shadow-performance` |
| `api/routes/signals_elections.py` | ~600 | `/signals/elections`, `/signals/elections/core`, `/signals/clarity` |
| `api/routes/signals_weather.py` | ~300 | `/signals/weather`, `/weather/dashboard`, `/signals/weather/ensemble-status` |
| `api/routes/signals_archetype.py` | ~400 | `/archetype/*`, `/signals/calibration`, `/signals/source-weights` |
| `api/routes/signals_arb.py` | ~200 | `/basket-arb`, `/copy-trade`, `/signals/cross-platform-arb` |
| `api/routes/signals_analytics.py` | ~200 | `/alerts/stats`, `/signals/scorecard/{strategy}` |
| `api/routes/signals_hf.py` | ~200 | `/hf/*` (high-frequency scanner endpoints) |

**Why:** Single Responsibility Principle. Each file has one domain concern. Testable in isolation.

### 8.2 `signals/mispriced_category_signal.py` (1,090 lines → ~6 files)

**Current:** One file mixing archetype classification, kill rules, Kalshi API fetching, Polymarket API fetching, confidence scoring, cross-platform matching, caching, shadow logging.

**Extract into:**

| New file | Lines | Contents |
|---|---|---|
| `signals/mispriced/archetype_classifier.py` | ~150 | `classify_archetype()`, `_check_kill_rules()` — **standalone, no reverse imports** |
| `signals/mispriced/kalshi_scanner.py` | ~200 | Kalshi market fetching + pagination |
| `signals/mispriced/polymarket_scanner.py` | ~200 | Polymarket market fetching |
| `signals/mispriced/confidence_scorer.py` | ~200 | Confidence scoring weights, category maps |
| `signals/mispriced/signal_aggregator.py` | ~200 | `get_mispriced_category_signals()` — orchestrator |
| `signals/mispriced/shadow_logger.py` | ~100 | Shadow trade logging integration — **standalone, no reverse imports** |

**Why:** The archetype classifier and kill rules are used by multiple consumers (signals.py, empirical_confidence.py, paper_portfolio.py). Extracting them eliminates duplication and makes the rules testable. Making `archetype_classifier.py` and `shadow_logger.py` standalone breaks circular import cycles.

### 8.3 `api/routes/markets.py` (1,940 lines → ~6 files)

**Current:** One file handling: Polymarket arb/rewards, market discovery, Vegas odds, ESPN odds, Betfair, Kalshi, Manifold, PredictIt, PolyRouter, Metaculus, HF scanner, Polymarket CLOB, whale walls.

**Extract into:**

| New file | Lines | Contents |
|---|---|---|
| `api/routes/markets.py` (slimmed) | ~200 | `/arb-scan`, `/rewards`, `/markets/*` |
| `api/routes/vegas.py` | ~400 | `/vegas/*` (odds, edge, sports, leagues, NFL) |
| `api/routes/espn.py` | ~300 | `/espn/*` (odds, edge, moneyline, injuries, standings) |
| `api/routes/kalshi.py` | ~100 | `/kalshi/*` (markets, entertainment, all) |
| `api/routes/polymarket.py` | ~200 | `/polymarket/*` (events, orderbook, microstructure, whale-wall) |
| `api/routes/manifold.py` | ~100 | `/manifold/*` (edge, markets, bets, top-traders) |
| `api/routes/predictit.py` | ~50 | `/predictit/*` (edge, markets) |
| `api/routes/metaculus.py` | ~100 | `/metaculus/*` (questions, edge, divergence) |
| `api/routes/polyrouter.py` | ~200 | `/polyrouter/*` (unified 7-platform API) |
| `api/routes/signals_hf.py` | ~200 | `/hf/*` (high-frequency scanner) |

### 8.4 `api/routes/engine.py` (1,267 lines → ~3 files)

| New file | Lines | Contents |
|---|---|---|
| `api/routes/engine.py` (slimmed) | ~400 | `/engine/*` (status, start, stop, config, trigger) |
| `api/routes/alerts.py` | ~200 | `/alerts/*` (create, list, delete, check) |
| `api/routes/phase.py` | ~200 | `/phase/*`, `/kelly/*` |

### 8.5 `api/main.py` — Extract visitor logging

Extract into `api/services/visitor_log.py`. The app factory should only wire up middleware and routes.

### 8.6 Eliminate `sys.path` manipulation

**Strategy:** Replace with proper package imports. BUT: do this in a specific order:
1. Create `signals/__init__.py` (empty, or with minimal re-exports)
2. Fix all bare imports to qualified (`from signals.paper_portfolio import ...`)
3. Fix test imports
4. THEN remove `sys.path.insert` calls

### 8.7 Consolidate HTTP client usage

Create a sync-compatible `OddsHttpClient` in `api/services/http_client.py`:
```python
class OddsHttpClient:
    async def get(self, url, ...) -> dict: ...   # async path
    def get_sync(self, url, ...) -> dict: ...     # sync path for scheduler
```

Then migrate 8 files from `urllib.request` to the shared client.

### 8.8 Create data access layer for SQLite

Create `api/services/database.py` with repository classes. This is Phase 4 — after the service layer exists.

### 8.9 Delete `src/strategies/mispriced_category_whale.py`

Zero imports from anywhere. Safe to delete in Phase 5.

### 8.10 Keep `odds/` flat

The existing `__init__.py` re-exports are consumed by 6+ files. Sub-packages add complexity with no benefit for 17 files.

---

## 9. Execution Phases

### Phase 0: Foundation (1 session, ~2 hours)

**Goal:** Make `signals/` a proper package without breaking anything. Fix all bare imports. Break circular import cycles. Remove dead phantom imports.

**Steps:**

1. **Create `signals/__init__.py`** — empty file. This makes `signals` a package.

2. **Break circular import C1** (`mispriced_category_signal.py` ↔ `shadow_tracker.py`):
   - Extract `archetype_classifier.py` from `mispriced_category_signal.py` — standalone module with `classify_archetype()` and `_check_kill_rules()`. No reverse imports.
   - Extract `shadow_logger.py` from `mispriced_category_signal.py` — standalone module with `log_shadow_trade()` and `save_signal_snapshot()`. No reverse imports.
   - Update `mispriced_category_signal.py` to import from the new files.
   - Update `shadow_tracker.py` to import `classify_archetype` from `signals.mispriced.archetype_classifier` instead of from `mispriced_category_signal`.

3. **Fix all bare imports in routes** — change every `from paper_portfolio import X` to `from signals.paper_portfolio import X` in:
   - `api/routes/signals.py` (14 occurrences: paper_portfolio, shadow_tracker, weather_scanner, weather_ensemble, resolution_logger, cv_kelly)
   - `services/hf_paper_trader.py` (1 occurrence: paper_portfolio)

4. **Fix all bare imports in tests** — change to qualified imports in:
   - `tests/unit/test_correlation_cap.py` — `from signals.paper_portfolio import ...`
   - `tests/unit/test_momentum_filter.py` — `from signals.price_momentum_filter import ...`
   - `tests/unit/test_strike_probability.py` — `from signals.strike_probability import ...`
   - `tests/unit/test_weather_scanner.py` — `from signals.weather_scanner import ...`
   - `tests/unit/test_volume_spike.py` — `from signals.volume_spike_detector import ...`

5. **Fix bare imports in scripts** — change to qualified imports in:
   - `scripts/prediction_market_backtest.py` — `from signals.mispriced_category_signal import ...` and `from signals.empirical_confidence import ...`

6. **Remove 4 phantom import blocks** from `api/routes/signals.py`:
   - Line 833: `from signals.election_signal import generate_election_signals` — file doesn't exist
   - Line 2995: `from signals.election_tracker import generate_report` — file doesn't exist
   - Line 3298: `from signals.polymarket_price_history import get_price_history` — file doesn't exist
   - Line 3350: `from signals.congress_bill_tracker import build_clarity_bills_overlay` — file doesn't exist
   - Replace each with a comment: `# [REMOVED] file never existed — was dead code`
   - The try/except blocks around them will handle the missing functionality gracefully

7. **Verify** — run `pytest tests/unit/` and hit a few API endpoints.

**Verification:**
```bash
cd ~/Desktop/polyclawd && python3 -c "from signals.mispriced.archetype_classifier import classify_archetype; print('OK')"
cd ~/Desktop/polyclawd && python3 -c "from signals.paper_portfolio import get_portfolio_status; print('OK')"
cd ~/Desktop/polyclawd && python3 -m pytest tests/unit/ -x -q 2>&1 | tail -5
```

---

### Phase 1: Low-Risk Extractions (1-2 sessions, ~4 hours)

**Goal:** Extract standalone modules that don't change import behavior. Each is a pure extract-and-reimport.

**Steps:**

1. **Extract `api/services/visitor_log.py`** from `api/main.py`
   - Move visitor log endpoint logic to a service class
   - Import in `main.py`
   - Verify `/api/visitor-log` still works

2. **Split `signals/mispriced_category_signal.py`** into sub-package:
   - Move `archetype_classifier.py` (already done in Phase 0)
   - Extract `kalshi_scanner.py` — Kalshi market fetching
   - Extract `polymarket_scanner.py` — Polymarket market fetching
   - Extract `confidence_scorer.py` — confidence scoring weights
   - Extract `signal_aggregator.py` — orchestrator function
   - Keep `mispriced_category_signal.py` as a re-export shim:
     ```python
     # signals/mispriced_category_signal.py (shim)
     from signals.mispriced.signal_aggregator import get_mispriced_category_signals
     from signals.mispriced.archetype_classifier import classify_archetype, _check_kill_rules
     ```

3. **Split `api/routes/signals.py` into sub-package** — start with the most independent endpoints:
   - Create `api/routes/signals/` sub-package with `__init__.py`
   - Extract `signals/whale.py` — `/predictors`, `/inverse-whale`, `/smart-money`
   - Extract `signals/confidence.py` — `/confidence/*`, `/conflicts/*`
   - Extract `signals/portfolio.py` — `/portfolio/*`, `/rotations`
   - Register new sub-routers in `api/routes/signals/__init__.py`
   - Import sub-package in `api/routes/__init__.py`
   - Keep old `signals.py` importing from new files as a compatibility shim

**Verification:**
```bash
cd ~/Desktop/polyclawd && python3 -m pytest tests/unit/ -x -q
cd ~/Desktop/polyclawd && python3 api/main.py &  # starts on port 8000
curl -s http://localhost:8000/api/signals | head -c 200
curl -s http://localhost:8000/api/portfolio/status | head -c 200
```

---

### Phase 2: Service Layer Extraction (2-3 sessions, ~6 hours)

**Goal:** Move business logic from routes into `api/services/`. Routes become thin wrappers.

**Steps:**

1. **Create `api/services/signal_aggregator.py`**
   - Move `aggregate_all_signals()` from `signals.py`
   - Move helper functions: `calculate_bayesian_confidence()`, `combined_decision_score()`
   - Routes call the service instead of inline functions

2. **Create `api/services/whale_service.py`**
   - Move `get_inverse_whale_signals()`, `get_smart_money_flow()`, `fetch_polymarket_positions()`
   - Move predictor stats functions

3. **Create `api/services/confidence_service.py`**
   - Move `load_source_outcomes()`, `record_outcome()`, `get_source_win_rate()`
   - Move `calculate_bayesian_confidence_v2()`, `laplace_smoothed_win_rate()`

4. **Create `api/services/portfolio_service.py`**
   - Move portfolio CRUD operations from `signals.py`
   - Move equity curve functions

5. **Create `api/services/election_service.py`**
   - Move election data caching and CLARITY filtering

6. **Create `api/services/weather_service.py`**
   - Move weather dashboard and ensemble status functions

**Verification:**
```bash
cd ~/Desktop/polyclawd && python3 -m pytest tests/unit/ -x -q
cd ~/Desktop/polyclawd && python3 api/main.py &
curl -s http://localhost:8000/api/signals | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Signals: {d[\"total\"]}, OK')"
```

---

### Phase 3: HTTP Client Consolidation (1 session, ~2 hours)

**Goal:** Centralize all HTTP calls through a single client. Remove `urllib.request` and standalone `requests` usage.

**Steps:**

1. **Refactor `api/services/http_client.py`** into a proper `OddsHttpClient` class with both sync and async paths:
   ```python
   class OddsHttpClient:
       def __init__(self, timeout=15):
           self._async_client = httpx.AsyncClient(timeout=timeout)
           self._sync_client = httpx.Client(timeout=timeout)
       
       async def get(self, url, headers=None) -> dict:
           resp = await self._async_client.get(url, headers=headers or {})
           return resp.json()
       
       def get_sync(self, url, headers=None) -> dict:
           resp = self._sync_client.get(url, headers=headers or {})
           return resp.json()
   ```

2. **Migrate `urllib.request` usage** (8 files):
   - `signals/weather_scanner.py`
   - `signals/shadow_tracker.py`
   - `signals/resolution_scanner.py`
   - `signals/strike_probability.py`
   - `signals/ai_model_tracker.py`
   - `signals/browser_bridge.py`
   - `signals/paper_portfolio.py`
   - `signals/mispriced_category_signal.py`

3. **Migrate `requests` library usage** (4 files in `odds/`):
   - `odds/kalshi_edge.py`
   - `odds/betfair_edge.py`
   - `odds/soccer_edge.py`
   - `odds/vegas_scraper.py`

4. **Migrate standalone `httpx` usage** (5 files in `signals/` that bypass shared client):
   - `signals/basket_arb_scanner.py`
   - `signals/mispriced_category_signal.py` (line 813)
   - `signals/cross_platform_arb.py`
   - `signals/alpha_score_tracker.py`
   - `signals/copy_trade_watcher.py`

5. **Remove `import urllib.request` and `import requests`** from all migrated files

**Verification:**
```bash
cd ~/Desktop/polyclawd && grep -rn "urllib\.request" --include="*.py" | grep -v ".pyc" | grep -v ".venv" | grep -v "venv/"
# Should show 0 results for signals/ and odds/

cd ~/Desktop/polyclawd && grep -rn "^import requests" --include="*.py" | grep -v ".pyc" | grep -v ".venv" | grep -v "venv/"
# Should show 0 results for signals/ and odds/
```

---

### Phase 4: Database Layer (1-2 sessions, ~4 hours)

**Goal:** Centralize SQLite access through repository classes.

**Steps:**

1. **Create `api/services/database.py`** with repository classes:
   - `ShadowTradesRepository` — resolved trades, archetype win rates
   - `PortfolioRepository` — positions, equity series
   - `VisitorLogRepository` — visitor log CRUD
   - `AlphaScoreRepository` — alpha score tracking

2. **Migrate inline SQLite queries** in `api/routes/signals.py` to use repositories

3. **Migrate inline SQLite queries** in `signals/paper_portfolio.py` to use repositories

4. **Migrate inline SQLite queries** in `signals/shadow_tracker.py` to use repositories

5. **Migrate inline SQLite queries** in `signals/alpha_score_tracker.py` to use repositories

**Verification:**
```bash
cd ~/Desktop/polyclawd && python3 -m pytest tests/unit/ -x -q
cd ~/Desktop/polyclawd && python3 api/main.py &
curl -s http://localhost:8000/api/portfolio/status | head -c 200
curl -s http://localhost:8000/api/signals/shadow-performance | head -c 200
```

---

### Phase 5: Final Cleanup (1 session, ~1 hour)

**Goal:** Remove shims, delete orphaned files, finalize.

**Steps:**

1. **Remove `sys.path.insert` calls** from route handlers (now that all imports are qualified)

2. **Remove compatibility shims** — delete old route files that were re-exporting from new files

3. **Delete `src/strategies/mispriced_category_whale.py`** (orphaned, zero imports)

4. **Update `api/routes/__init__.py`** to register all new routers

5. **Run full test suite** to verify no regressions

6. **Update `Makefile`** with a `test` target

**Verification:**
```bash
cd ~/Desktop/polyclawd && python3 -m pytest tests/ -x -q
cd ~/Desktop/polyclawd && python3 api/main.py &
# Hit every endpoint category
for endpoint in /api/signals /api/portfolio/status /api/vegas/odds /api/espn/odds /api/kalshi/markets /api/polymarket/events /api/engine/status /api/health; do
    echo "$endpoint: $(curl -s -o /dev/null -w '%{http_code}' http://localhost:8000$endpoint)"
done
```

---

### Rollback Plan

If any phase breaks the API:

```bash
# Phase 0 rollback
cd ~/Desktop/polyclawd && git checkout -- signals/__init__.py signals/mispriced/

# Phase 1-5 rollback
cd ~/Desktop/polyclawd && git checkout -- api/routes/ api/services/ signals/ odds/
```

**Key rule:** Commit after each phase. Never proceed to the next phase without a clean commit.

---

## 10. Dependency Map

### Current (problematic)

```
signals.py (3,389 lines)
  ├── imports mispriced_category_signal.py (sys.path hack)
  ├── imports news_signal.py (sys.path hack)
  ├── imports ic_tracker.py (sys.path hack)
  ├── imports calibrator.py (sys.path hack)
  ├── imports paper_portfolio.py (sys.path hack) [bare import]
  ├── imports shadow_tracker.py (sys.path hack) [bare import]
  ├── imports copy_trade_watcher.py (sys.path hack)
  ├── imports cross_platform_arb.py (sys.path hack)
  ├── imports resolution_scanner.py (sys.path hack)
  ├── imports election_tracker.py (sys.path hack)
  ├── imports weather_ensemble.py (sys.path hack) [bare import]
  ├── imports weather_scanner.py (sys.path hack) [bare import]
  ├── imports tweet_count_scanner.py (sys.path hack)
  ├── imports alpha_score_tracker.py (sys.path hack)
  ├── imports ai_model_tracker.py (sys.path hack)
  ├── imports basket_arb_scanner.py (sys.path hack)
  ├── imports strike_probability.py (sys.path hack)
  ├── imports polymarket_price_history.py (sys.path hack)
  ├── imports congress_bill_tracker.py (sys.path hack)
  ├── uses urllib.request directly (bypasses http_client.py)
  └── uses sqlite3 directly (bypasses storage.py)
```

### Target (clean)

```
api/routes/signals.py (~200 lines)
  └── calls api/services/signal_aggregator.py

api/services/signal_aggregator.py
  ├── calls signals/mispriced/signal_aggregator.py
  ├── calls signals/whale/scanner.py
  ├── calls signals/news/news_signal.py
  ├── calls signals/election/tracker.py
  ├── calls signals/weather/scanner.py
  └── calls api/services/http_client.py (for all HTTP)

signals/mispriced/signal_aggregator.py
  ├── calls signals/mispriced/archetype_classifier.py
  ├── calls signals/mispriced/kalshi_scanner.py
  ├── calls signals/mispriced/polymarket_scanner.py
  ├── calls signals/mispriced/confidence_scorer.py
  └── calls api/services/http_client.py

odds/polymarket_clob.py
  └── calls api/services/http_client.py

odds/kalshi_edge.py
  └── calls api/services/http_client.py
```

### Key dependency rules enforced:

1. **Routes → Services only.** Routes never import domain modules directly.
2. **Services → Domain + Infrastructure.** Services orchestrate domain logic and call infrastructure for I/O.
3. **Domain → Infrastructure only.** Domain modules never import routes or services.
4. **Infrastructure → nothing.** Infrastructure modules (http_client, storage, database) are leaf nodes.
5. **No circular imports.** Domain modules don't import each other's services; they compose through the service layer.

---

## 11. Verification Strategy

> Applied: `code-review` skill — verification checklist

### Per-Phase Verification

Every phase must pass these checks before committing:

```bash
# 1. Import check — can all modules be imported without errors?
cd ~/Desktop/polyclawd
python3 -c "from api.main import app; print('API imports OK')"
python3 -c "from signals.mispriced.archetype_classifier import classify_archetype; print('Archetype imports OK')"

# 2. Unit tests
python3 -m pytest tests/unit/ -x -q 2>&1

# 3. API smoke test (start server, hit endpoints)
python3 api/main.py &
API_PID=$!
sleep 2
for endpoint in /api/health /api/signals /api/portfolio/status; do
    STATUS=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8000$endpoint)
    echo "$endpoint → $STATUS"
    if [ "$STATUS" = "000" ] || [ "$STATUS" = "500" ]; then
        echo "FAILED: $endpoint returned $STATUS"
        kill $API_PID 2>/dev/null
        exit 1
    fi
done
kill $API_PID 2>/dev/null

# 4. No regressions in import graph
grep -rn "sys\.path\.insert" --include="*.py" | grep -v ".pyc" | grep -v ".venv" | grep -v "venv/"
# Phase 0: should still show ~80 (we only add __init__.py, don't remove inserts yet)
# Phase 5: should show 0

echo "✅ Phase $(grep -c '^## Phase' ARCHITECTURE_REFACTOR.md) verification passed"
```

### Rollback Procedure

If any phase breaks the API:

```bash
cd ~/Desktop/polyclawd
git checkout -- .  # Revert all uncommitted changes
# Or for targeted rollback:
git checkout -- signals/__init__.py signals/mispriced/  # Phase 0
git checkout -- api/routes/signals/ api/services/visitor_log.py  # Phase 1
```

**Golden rule:** Commit after each phase. Never proceed to the next phase without a clean commit.

### Test Coverage Gaps

| Gap | Risk | Mitigation |
|---|---|---|
| No integration tests for most endpoint paths | Medium | Manual smoke test per phase (curl against localhost) |
| No CI pipeline | Medium | Add `make test` target in Phase 5 |
| Test files use sys.path.insert | Low | Fixed in Phase 0 |
| No characterization tests before refactoring | Low | Solo operator, can rollback instantly |

---

## Summary

| Metric | Before | After | Improvement |
|---|---|---|---|
| Largest file | 3,389 lines | ~400 lines | 8x smaller |
| Files >1,000 lines | 6 | 0 | Eliminated |
| Route files | 6 | ~20 | Proper separation |
| `sys.path` hacks | ~80 | 0 | Clean imports |
| `urllib.request` usage | 8 files | 0 | Centralized HTTP |
| Inline SQLite | 5 locations | 0 | Repository pattern |
| Bare imports | ~20 | 0 | Qualified imports |
| Circular imports | 6 cycles | 0 | Broken by standalone modules |
| Testable modules | ~30% | ~80% | Major improvement |

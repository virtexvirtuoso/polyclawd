# Polyclawd Refactoring Plan

**Version:** 1.0  
**Date:** 2026-02-07  
**Author:** Virt (Backend Architect Review)  
**Status:** Ready for Implementation

---

## Executive Summary

The current `api/main.py` is a 6,500-line monolith containing 109 API endpoints, 25+ global state files, and significant code duplication. This plan outlines a systematic refactoring into a modular architecture while maintaining 100% backward compatibility.

**Estimated Effort:** 6-8 hours  
**Risk Level:** Medium (mitigated by incremental approach)  
**Zero Downtime:** Yes, using feature flags and gradual migration

---

## Table of Contents

1. [Current Architecture Analysis](#1-current-architecture-analysis)
2. [Target Architecture](#2-target-architecture)
3. [Breaking Change Analysis](#3-breaking-change-analysis)
4. [File-by-File Migration Plan](#4-file-by-file-migration-plan)
5. [Shared State Management](#5-shared-state-management)
6. [Implementation Phases](#6-implementation-phases)
7. [Testing Strategy](#7-testing-strategy)
8. [Rollback Plan](#8-rollback-plan)
9. [Post-Refactor Checklist](#9-post-refactor-checklist)

---

## 1. Current Architecture Analysis

### 1.1 File Structure (Before)
```
polyclawd/
├── api/
│   ├── main.py              # 6,500 lines - EVERYTHING
│   └── edge_cache.py        # 150 lines - Edge signal caching
├── odds/
│   ├── __init__.py
│   ├── betfair_edge.py
│   ├── client.py
│   ├── espn_odds.py
│   ├── kalshi_edge.py
│   ├── manifold.py
│   ├── polyrouter.py
│   ├── predictit.py
│   ├── smart_matcher.py
│   ├── soccer_edge.py
│   └── vegas_scraper.py
├── signals/
│   ├── keyword_learner.py
│   └── news_signal.py
├── config/
│   └── scaling_phases.py
├── data/                    # Runtime JSON files
└── mcp/
    └── server.py
```

### 1.2 Global State Files (25 total)
| File | Purpose | Used By |
|------|---------|---------|
| `~/.openclaw/paper-trading/balance.json` | Paper account balance | trading, engine |
| `~/.openclaw/paper-trading/positions.json` | Open positions | trading, engine, whales |
| `~/.openclaw/paper-trading/trades.json` | Trade history | trading, engine |
| `data/engine_state.json` | Trading engine config | engine |
| `data/traded_signals.json` | Deduplication | engine |
| `data/volume_state.json` | Volume tracking | signals |
| `data/price_alerts.json` | User alerts | alerts |
| `data/predictor_stats.json` | Whale accuracy | whales |
| `data/auto_trades.json` | Auto-trade log | engine |
| `data/source_outcomes.json` | Source performance | confidence |
| `data/conflict_history.json` | Signal conflicts | confidence |
| `data/recent_trades.json` | Recent activity | engine |
| `~/.openclaw/edge_cache.json` | Edge signal cache | signals |
| `~/.config/simmer/credentials.json` | Simmer auth | simmer |

### 1.3 Endpoint Categories (109 total)

| Category | Count | Routes |
|----------|-------|--------|
| **Paper Trading** | 8 | `/api/balance`, `/api/positions`, `/api/trades`, `/api/trade`, `/api/reset`, `/api/positions/check`, `/api/positions/{id}/resolve` |
| **Simmer Integration** | 9 | `/api/simmer/*` |
| **Markets** | 6 | `/api/markets/*`, `/api/arb-scan`, `/api/rewards` |
| **Signals** | 5 | `/api/signals`, `/api/signals/news`, `/api/signals/auto-trade`, `/api/volume/spikes`, `/api/resolution/*` |
| **Alerts** | 4 | `/api/alerts`, `/api/alerts/check`, `/api/alerts/{id}` |
| **Whales** | 4 | `/api/predictors`, `/api/inverse-whale`, `/api/smart-money` |
| **Edge Detection** | 15 | `/api/vegas/*`, `/api/espn/*`, `/api/betfair/*`, `/api/kalshi/*`, `/api/manifold/*`, `/api/predictit/*`, `/api/polyrouter/*` |
| **Engine** | 8 | `/api/engine/*`, `/api/phase/*`, `/api/kelly/*` |
| **Confidence** | 3 | `/api/confidence/*`, `/api/conflicts/*` |
| **LLM** | 2 | `/api/llm/status`, `/api/llm/test` |
| **Paper Polymarket** | 5 | `/api/paper/*` |
| **Rotations** | 3 | `/api/rotations`, `/api/rotation/*` |
| **Other** | 37 | Misc endpoints |

### 1.4 Code Smells Identified

1. **Silent Exception Handling:** 42 bare `except: pass` blocks
2. **Repeated Import Pattern:** 15+ occurrences of dynamic `sys.path` manipulation
3. **Sync in Async:** `urllib.request` used in `async def` routes
4. **Global State:** 25+ JSON files with no centralized management
5. **No Logging:** Zero structured logging
6. **No Health Checks:** Missing `/health` and `/ready` endpoints

---

## 2. Target Architecture

### 2.1 File Structure (After)
```
polyclawd/
├── api/
│   ├── __init__.py          # FastAPI app factory
│   ├── main.py              # App assembly + startup (200 lines)
│   ├── deps.py              # Shared dependencies
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── trading.py       # Paper trading endpoints
│   │   ├── simmer.py        # Simmer integration
│   │   ├── markets.py       # Market discovery
│   │   ├── signals.py       # Signal aggregation
│   │   ├── alerts.py        # Price alerts
│   │   ├── whales.py        # On-chain tracking
│   │   ├── edges.py         # Edge detection (all sources)
│   │   ├── engine.py        # Trading engine control
│   │   ├── confidence.py    # Bayesian scoring
│   │   ├── llm.py           # LLM validation
│   │   ├── paper.py         # Paper Polymarket trading
│   │   ├── rotations.py     # Position rotations
│   │   └── health.py        # Health/ready endpoints
│   ├── services/
│   │   ├── __init__.py
│   │   ├── storage.py       # Centralized JSON storage
│   │   ├── trading_engine.py # Engine logic
│   │   ├── bayesian.py      # Confidence calculations
│   │   └── http_client.py   # Async HTTP wrapper
│   └── models/
│       ├── __init__.py
│       ├── trading.py       # Trade request/response
│       ├── signals.py       # Signal models
│       └── edges.py         # Edge detection models
├── odds/                    # (unchanged)
├── signals/                 # (unchanged)
├── config/                  # (unchanged)
├── data/                    # (unchanged)
└── mcp/                     # (unchanged)
```

### 2.2 Dependency Injection Pattern

```python
# api/deps.py
from pathlib import Path
from functools import lru_cache

class Settings:
    STORAGE_DIR = Path.home() / ".openclaw" / "paper-trading"
    DATA_DIR = Path(__file__).parent.parent / "data"
    DEFAULT_BALANCE = 10000.0
    
@lru_cache()
def get_settings() -> Settings:
    return Settings()

def get_storage_service():
    from api.services.storage import StorageService
    return StorageService(get_settings())
```

### 2.3 Router Registration Pattern

```python
# api/main.py (after refactor)
from fastapi import FastAPI
from api.routes import (
    trading, simmer, markets, signals, alerts,
    whales, edges, engine, confidence, llm,
    paper, rotations, health
)

app = FastAPI(title="Polyclawd API", version="2.0.0")

# Register all routers
app.include_router(health.router, tags=["Health"])
app.include_router(trading.router, prefix="/api", tags=["Trading"])
app.include_router(simmer.router, prefix="/api/simmer", tags=["Simmer"])
app.include_router(markets.router, prefix="/api/markets", tags=["Markets"])
app.include_router(signals.router, prefix="/api/signals", tags=["Signals"])
app.include_router(alerts.router, prefix="/api/alerts", tags=["Alerts"])
app.include_router(whales.router, prefix="/api", tags=["Whales"])
app.include_router(edges.router, prefix="/api", tags=["Edges"])
app.include_router(engine.router, prefix="/api/engine", tags=["Engine"])
app.include_router(confidence.router, prefix="/api/confidence", tags=["Confidence"])
app.include_router(llm.router, prefix="/api/llm", tags=["LLM"])
app.include_router(paper.router, prefix="/api/paper", tags=["Paper"])
app.include_router(rotations.router, prefix="/api", tags=["Rotations"])
```

---

## 3. Breaking Change Analysis

### 3.1 API Compatibility: ✅ 100% Backward Compatible

All existing endpoints will maintain:
- Same URL paths
- Same request/response schemas
- Same query parameters
- Same behavior

### 3.2 Potential Breaking Changes

| Change | Risk | Mitigation |
|--------|------|------------|
| Import path changes | Medium | Keep `main.py` imports working via `__init__.py` |
| State file locations | Low | No changes to file paths |
| Startup behavior | Low | Same `@app.on_event("startup")` logic |
| MCP server imports | Medium | Update `mcp/server.py` to use new paths |
| Cron job scripts | Low | They call HTTP endpoints, not Python imports |
| Edge cache paths | Low | Keep `edge_cache.py` in same location |

### 3.3 Files That Reference main.py

```bash
# These files may need updates:
mcp/server.py          # Imports from api.main
scripts/monitor.py     # May import helpers
```

### 3.4 External Dependencies

| Dependency | Current Usage | After Refactor |
|------------|---------------|----------------|
| systemd service | `uvicorn api.main:app` | **No change** |
| nginx proxy | `proxy_pass :8420` | **No change** |
| Cron jobs | HTTP calls to `/api/*` | **No change** |
| MCP server | Imports from main | **Update imports** |

---

## 4. File-by-File Migration Plan

### 4.1 Phase 1: Infrastructure (No Breaking Changes)

#### File: `api/deps.py` (NEW)
```python
"""Shared dependencies and settings"""
from pathlib import Path
from functools import lru_cache
from typing import Optional
import json

class Settings:
    # Storage paths
    STORAGE_DIR = Path.home() / ".openclaw" / "paper-trading"
    POLY_STORAGE_DIR = Path.home() / ".openclaw" / "paper-trading-polymarket"
    DATA_DIR = Path(__file__).parent.parent / "data"
    
    # Defaults
    DEFAULT_BALANCE = 10000.0
    
    # API keys (from env)
    ANTHROPIC_API_KEY: Optional[str] = None
    OPENAI_API_KEY: Optional[str] = None
    
    # External APIs
    GAMMA_API = "https://gamma-api.polymarket.com"
    SIMMER_API = "https://api.simmer.markets/api/sdk"

@lru_cache()
def get_settings() -> Settings:
    import os
    settings = Settings()
    settings.ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
    settings.OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    return settings
```

#### File: `api/services/storage.py` (NEW)
```python
"""Centralized JSON storage service"""
import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional
from datetime import datetime

class StorageService:
    _lock = threading.Lock()
    
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        base_dir.mkdir(parents=True, exist_ok=True)
    
    def load(self, filename: str, default: Any = None) -> Any:
        path = self.base_dir / filename
        if path.exists():
            with self._lock:
                with open(path) as f:
                    return json.load(f)
        return default if default is not None else {}
    
    def save(self, filename: str, data: Any) -> None:
        path = self.base_dir / filename
        with self._lock:
            with open(path, "w") as f:
                json.dump(data, f, indent=2, default=str)
    
    def append_to_list(self, filename: str, item: Dict, max_items: int = 1000) -> None:
        data = self.load(filename, [])
        data.append({**item, "timestamp": datetime.now().isoformat()})
        if len(data) > max_items:
            data = data[-max_items:]
        self.save(filename, data)
```

#### File: `api/services/http_client.py` (NEW)
```python
"""Async HTTP client wrapper"""
import httpx
from typing import Optional, Dict, Any

class AsyncHTTPClient:
    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout
    
    async def get(self, url: str, headers: Optional[Dict] = None) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=headers or {"User-Agent": "Polyclawd/2.0"})
            resp.raise_for_status()
            return resp.json()
    
    async def post(self, url: str, data: Dict, headers: Optional[Dict] = None) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, json=data, headers=headers)
            resp.raise_for_status()
            return resp.json()

# Singleton instance
http_client = AsyncHTTPClient()
```

### 4.2 Phase 2: Route Modules

#### File: `api/routes/health.py` (NEW)
```python
"""Health check endpoints"""
from fastapi import APIRouter
from datetime import datetime

router = APIRouter()

@router.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "version": "2.0.0"
    }

@router.get("/ready")
async def readiness_check():
    # Check critical dependencies
    checks = {
        "storage": True,  # Add real checks
        "polymarket_api": True,
    }
    all_ready = all(checks.values())
    return {
        "ready": all_ready,
        "checks": checks,
        "timestamp": datetime.now().isoformat()
    }
```

#### File: `api/routes/trading.py` (NEW)
```python
"""Paper trading endpoints"""
from fastapi import APIRouter, HTTPException, Query, Depends
from typing import List, Optional
from pydantic import BaseModel
from api.deps import get_settings
from api.services.storage import StorageService

router = APIRouter()

class TradeRequest(BaseModel):
    market_id: str
    side: str  # YES or NO
    amount: float
    
class TradeResponse(BaseModel):
    success: bool
    trade_id: Optional[str] = None
    message: str
    balance: Optional[float] = None

def get_storage():
    settings = get_settings()
    return StorageService(settings.STORAGE_DIR)

@router.get("/balance")
async def get_balance(storage: StorageService = Depends(get_storage)):
    data = storage.load("balance.json", {"usdc": 10000.0})
    return data

@router.get("/positions")
async def get_positions(storage: StorageService = Depends(get_storage)):
    positions = storage.load("positions.json", {"positions": []})
    return positions

@router.get("/trades")
async def get_trades(
    limit: int = Query(default=20, le=100),
    storage: StorageService = Depends(get_storage)
):
    trades = storage.load("trades.json", {"trades": []})
    return {"trades": trades.get("trades", [])[-limit:]}

@router.post("/trade", response_model=TradeResponse)
async def execute_trade(
    request: TradeRequest,
    storage: StorageService = Depends(get_storage)
):
    # Implementation moved from main.py
    # ... (full logic here)
    pass

@router.post("/reset")
async def reset_paper_trading(storage: StorageService = Depends(get_storage)):
    storage.save("balance.json", {"usdc": 10000.0})
    storage.save("positions.json", {"positions": []})
    storage.save("trades.json", {"trades": []})
    return {"success": True, "message": "Paper trading reset to $10,000"}
```

#### File: `api/routes/edges.py` (NEW)
```python
"""Edge detection endpoints - consolidated from all sources"""
from fastapi import APIRouter, Query
from typing import Optional
import logging

router = APIRouter()
logger = logging.getLogger(__name__)

@router.get("/vegas/edge")
async def get_vegas_edge(min_edge: float = Query(default=0.05)):
    try:
        from odds.vegas_scraper import get_vegas_odds_with_fallback
        # ... implementation
    except Exception as e:
        logger.error(f"Vegas edge error: {e}")
        return {"error": str(e), "edges": []}

@router.get("/espn/odds")
async def get_espn_odds():
    try:
        from odds.espn_odds import get_espn_summary
        return get_espn_summary()
    except Exception as e:
        logger.error(f"ESPN odds error: {e}")
        return {"error": str(e), "source": "espn"}

@router.get("/espn/edge")
async def get_espn_edge(min_edge: float = Query(default=5.0)):
    try:
        from odds.espn_odds import get_espn_edges
        return await get_espn_edges(min_edge)
    except Exception as e:
        logger.error(f"ESPN edge error: {e}")
        return {"error": str(e), "source": "espn"}

@router.get("/vegas/soccer")
async def get_soccer_edges():
    try:
        from odds.soccer_edge import get_soccer_edge_summary
        return await get_soccer_edge_summary()
    except Exception as e:
        logger.error(f"Soccer edge error: {e}")
        return {"error": str(e), "source": "soccer"}

@router.get("/betfair/edge")
async def get_betfair_edge():
    try:
        from odds.betfair_edge import get_betfair_edge_summary
        return await get_betfair_edge_summary()
    except Exception as e:
        logger.error(f"Betfair edge error: {e}")
        return {"error": str(e), "source": "betfair"}

@router.get("/kalshi/markets")
async def get_kalshi_markets():
    try:
        from odds.kalshi_edge import get_kalshi_polymarket_comparison
        return await get_kalshi_polymarket_comparison()
    except Exception as e:
        logger.error(f"Kalshi error: {e}")
        return {"error": str(e), "source": "kalshi"}

@router.get("/manifold/edge")
async def get_manifold_edge(min_edge: float = Query(default=5.0)):
    try:
        from odds.manifold import get_manifold_edges
        return await get_manifold_edges(min_edge)
    except Exception as e:
        logger.error(f"Manifold edge error: {e}")
        return {"error": str(e), "source": "manifold"}

@router.get("/manifold/markets")
async def get_manifold_markets():
    try:
        from odds.manifold import get_manifold_summary
        return get_manifold_summary()
    except Exception as e:
        logger.error(f"Manifold markets error: {e}")
        return {"error": str(e), "source": "manifold"}

@router.get("/predictit/edge")
async def get_predictit_edge(min_edge: float = Query(default=5.0)):
    try:
        from odds.predictit import get_predictit_edges
        return await get_predictit_edges(min_edge)
    except Exception as e:
        logger.error(f"PredictIt edge error: {e}")
        return {"error": str(e), "source": "predictit"}

@router.get("/predictit/markets")
async def get_predictit_markets():
    try:
        from odds.predictit import get_predictit_summary
        return get_predictit_summary()
    except Exception as e:
        logger.error(f"PredictIt markets error: {e}")
        return {"error": str(e), "source": "predictit"}
```

### 4.3 Phase 3: Signal & Engine Routes

*(Similar pattern for signals.py, engine.py, whales.py, etc.)*

### 4.4 Phase 4: Main.py Reduction

```python
# api/main.py (FINAL - ~200 lines)
"""
Polyclawd API - Prediction Market Trading Bot
Version 2.0.0 (Refactored)
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

# Import routers
from api.routes import (
    health, trading, simmer, markets, signals,
    alerts, whales, edges, engine, confidence,
    llm, paper, rotations
)

# Create app
app = FastAPI(
    title="Polyclawd API",
    description="AI-Powered Prediction Market Trading Bot",
    version="2.0.0"
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files
static_path = Path(__file__).parent.parent / "static"
if static_path.exists():
    app.mount("/static", StaticFiles(directory=static_path), name="static")

# Register routers
app.include_router(health.router, tags=["Health"])
app.include_router(trading.router, prefix="/api", tags=["Trading"])
app.include_router(simmer.router, prefix="/api/simmer", tags=["Simmer"])
app.include_router(markets.router, prefix="/api/markets", tags=["Markets"])
app.include_router(signals.router, prefix="/api", tags=["Signals"])
app.include_router(alerts.router, prefix="/api/alerts", tags=["Alerts"])
app.include_router(whales.router, prefix="/api", tags=["Whales"])
app.include_router(edges.router, prefix="/api", tags=["Edges"])
app.include_router(engine.router, prefix="/api/engine", tags=["Engine"])
app.include_router(confidence.router, prefix="/api/confidence", tags=["Confidence"])
app.include_router(llm.router, prefix="/api/llm", tags=["LLM"])
app.include_router(paper.router, prefix="/api/paper", tags=["Paper"])
app.include_router(rotations.router, prefix="/api", tags=["Rotations"])

@app.on_event("startup")
async def startup_event():
    logging.info("Polyclawd API starting up...")
    # Initialize services
    from api.deps import get_settings
    settings = get_settings()
    settings.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    (settings.DATA_DIR).mkdir(parents=True, exist_ok=True)
    logging.info("Polyclawd API ready")

@app.get("/")
async def root():
    return FileResponse(static_path / "index.html")
```

---

## 5. Shared State Management

### 5.1 Current State (Problematic)
```python
# Scattered throughout main.py
BALANCE_FILE = Path.home() / ".openclaw/paper-trading/balance.json"
def load_json(path): ...
def save_json(path, data): ...
```

### 5.2 New State Management
```python
# Centralized in api/services/storage.py
storage = StorageService(Path.home() / ".openclaw/paper-trading")
balance = storage.load("balance.json")
storage.save("balance.json", {"usdc": 9500})
```

### 5.3 State File Migration

No file location changes needed. The `StorageService` uses the same paths:

| State | Path | Change |
|-------|------|--------|
| Balance | `~/.openclaw/paper-trading/balance.json` | None |
| Positions | `~/.openclaw/paper-trading/positions.json` | None |
| Trades | `~/.openclaw/paper-trading/trades.json` | None |
| Engine | `data/engine_state.json` | None |
| Edge Cache | `~/.openclaw/edge_cache.json` | None |

---

## 6. Implementation Phases

### Phase 1: Infrastructure (30 min)
- [ ] Create `api/deps.py`
- [ ] Create `api/services/__init__.py`
- [ ] Create `api/services/storage.py`
- [ ] Create `api/services/http_client.py`
- [ ] Create `api/routes/__init__.py`
- [ ] Create `api/models/__init__.py`

### Phase 2: Health & Trading Routes (45 min)
- [ ] Create `api/routes/health.py`
- [ ] Create `api/routes/trading.py`
- [ ] Create `api/models/trading.py`
- [ ] Test: `/health`, `/api/balance`, `/api/trade`

### Phase 3: Edge Detection Routes (45 min)
- [ ] Create `api/routes/edges.py`
- [ ] Move all edge endpoints
- [ ] Test: `/api/espn/odds`, `/api/vegas/soccer`, etc.

### Phase 4: Signal & Engine Routes (60 min)
- [ ] Create `api/routes/signals.py`
- [ ] Create `api/routes/engine.py`
- [ ] Create `api/services/trading_engine.py`
- [ ] Move engine logic
- [ ] Test: `/api/signals`, `/api/engine/status`

### Phase 5: Remaining Routes (60 min)
- [ ] Create `api/routes/simmer.py`
- [ ] Create `api/routes/markets.py`
- [ ] Create `api/routes/alerts.py`
- [ ] Create `api/routes/whales.py`
- [ ] Create `api/routes/confidence.py`
- [ ] Create `api/routes/llm.py`
- [ ] Create `api/routes/paper.py`
- [ ] Create `api/routes/rotations.py`

### Phase 6: Main.py Reduction (30 min)
- [ ] Update `main.py` to import routers
- [ ] Remove migrated code from `main.py`
- [ ] Verify all 109 endpoints work
- [ ] Update MCP server imports

### Phase 7: Cleanup (30 min)
- [ ] Add logging to all routes
- [ ] Replace bare `except:` with proper handling
- [ ] Run full test suite
- [ ] Update README

---

## 7. Testing Strategy

### 7.1 Endpoint Verification Script

```bash
#!/bin/bash
# scripts/verify_endpoints.sh

BASE_URL="http://localhost:8420"
ENDPOINTS=(
    "/health"
    "/api/balance"
    "/api/positions"
    "/api/signals"
    "/api/espn/odds"
    "/api/vegas/soccer"
    "/api/engine/status"
    "/api/paper/status"
    # ... all 109 endpoints
)

echo "Testing all endpoints..."
for ep in "${ENDPOINTS[@]}"; do
    status=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL$ep")
    if [ "$status" = "200" ]; then
        echo "✅ $ep"
    else
        echo "❌ $ep (HTTP $status)"
    fi
done
```

### 7.2 Critical Path Tests

1. **Trading Flow:**
   - GET `/api/balance` → 200
   - POST `/api/trade` → 200, balance updated
   - GET `/api/positions` → new position exists

2. **Signal Flow:**
   - GET `/api/signals` → returns 13+ sources
   - Check Bayesian scoring applied

3. **Engine Flow:**
   - GET `/api/engine/status` → running
   - POST `/api/engine/trigger` → evaluates signals

### 7.3 Load Test

```bash
# Using Apache Bench
ab -n 1000 -c 10 http://localhost:8420/api/signals
```

---

## 8. Rollback Plan

### 8.1 Git Strategy

```bash
# Create feature branch
git checkout -b refactor/modular-architecture

# Incremental commits per phase
git commit -m "Phase 1: Infrastructure"
git commit -m "Phase 2: Trading routes"
# ...

# If issues, instant rollback
git checkout main
```

### 8.2 Deployment Rollback

```bash
# On VPS, keep backup of working version
cp -r /var/www/virtuosocrypto.com/polyclawd /var/www/virtuosocrypto.com/polyclawd.backup

# Rollback if needed
mv /var/www/virtuosocrypto.com/polyclawd /var/www/virtuosocrypto.com/polyclawd.broken
mv /var/www/virtuosocrypto.com/polyclawd.backup /var/www/virtuosocrypto.com/polyclawd
sudo systemctl restart polyclawd-api
```

---

## 9. Post-Refactor Checklist

### 9.1 Verification

- [ ] All 109 endpoints return 200
- [ ] Paper trading balance persists
- [ ] Engine trades execute correctly
- [ ] Edge detection runs without errors
- [ ] MCP server works
- [ ] Cron jobs execute successfully
- [ ] No errors in systemd logs

### 9.2 Documentation Updates

- [ ] Update README.md with new structure
- [ ] Add API documentation (auto-generated from FastAPI)
- [ ] Update MEMORY.md with refactor notes

### 9.3 Performance Baseline

Before/After comparison:
- [ ] `/api/signals` response time
- [ ] `/api/engine/trigger` response time
- [ ] Memory usage
- [ ] CPU usage during edge scan

---

## Appendix A: Full File List

### New Files to Create (17 files)

```
api/deps.py
api/routes/__init__.py
api/routes/health.py
api/routes/trading.py
api/routes/simmer.py
api/routes/markets.py
api/routes/signals.py
api/routes/alerts.py
api/routes/whales.py
api/routes/edges.py
api/routes/engine.py
api/routes/confidence.py
api/routes/llm.py
api/routes/paper.py
api/routes/rotations.py
api/services/__init__.py
api/services/storage.py
api/services/http_client.py
api/services/trading_engine.py
api/services/bayesian.py
api/models/__init__.py
api/models/trading.py
api/models/signals.py
api/models/edges.py
```

### Files to Modify (2 files)

```
api/main.py              # Reduce from 6500 to ~200 lines
mcp/server.py            # Update imports
```

### Files Unchanged (all others)

```
odds/*.py
signals/*.py
config/*.py
api/edge_cache.py
```

---

## Appendix B: Logging Setup

```python
# api/logging_config.py
import logging
import sys

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("/var/log/polyclawd/api.log")
        ]
    )
    
    # Reduce noise from libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
```

---

**End of Refactoring Plan**

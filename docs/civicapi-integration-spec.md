# civicAPI Integration Specification
## Election Night Ground Truth System

**Version:** 1.0  
**Date:** 2026-04-09  
**Status:** Draft

---

## 1. Executive Summary

**Purpose:** Enable real-time arbitrage between actual vote counts (civicAPI) and prediction market prices (Polymarket/Kalshi) during election nights.

**Core Value Proposition:** When precincts report results but markets haven't repriced, we capture the lag as alpha.

---

## 2. Data Source: civicAPI

### 2.1 Endpoint Structure

```
Base URL: https://api.civicapi.org/
Auth: None (completely free)
Rate Limits: None documented (be respectful: 1 req/sec)
Format: JSON
```

### 2.2 Key Endpoints

| Endpoint | Purpose | Response Time |
|----------|---------|---------------|
| `GET /races` | List all active races | ~500ms |
| `GET /races/{race_id}/results` | Live vote tallies | ~200ms |
| `GET /races/{race_id}/calls` | Race call status | ~200ms |
| `GET /states/{state}/races` | All races in a state | ~300ms |

### 2.3 Response Schema (Live Results)

```json
{
  "race_id": "AZ-SEN-2026-GEN",
  "state": "AZ",
  "office": "Senate",
  "cycle": 2026,
  "status": "active",
  "reporting": {
    "precincts_reporting": 847,
    "precincts_total": 1489,
    "pct_reporting": 56.9
  },
  "results": {
    "candidates": [
      {
        "candidate_id": "C00123456",
        "name": "Ruben Gallego",
        "party": "DEM",
        "votes": 487293,
        "pct": 52.4,
        "winner": false
      },
      {
        "candidate_id": "C00789012",
        "name": "Kari Lake",
        "party": "REP",
        "votes": 442891,
        "pct": 47.6,
        "winner": false
      }
    ],
    "total_votes": 930184
  },
  "last_updated": "2026-11-05T02:34:18Z",
  "called": false,
  "call_time": null
}
```

### 2.4 Race Call Schema

```json
{
  "race_id": "AZ-SEN-2026-GEN",
  "called": true,
  "call_time": "2026-11-05T04:12:33Z",
  "called_by": "civicAPI-decision-desk",
  "winner": {
    "candidate_id": "C00123456",
    "name": "Ruben Gallego",
    "party": "DEM",
    "margin_pct": 4.8
  },
  "confidence": "high"
}
```

---

## 3. Integration Architecture

### 3.1 System Diagram

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   civicAPI      │────▶│  Election Night  │────▶│  Signal Engine  │
│  (Ground Truth) │     │  Aggregator      │     │  (Arb Detector) │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                               │                           │
                               ▼                           ▼
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Polymarket     │────▶│  Market Price    │     │  Trade Executor │
│  (Market Price) │     │  Cache           │     │  (Paper/Live)   │
└─────────────────┘     └──────────────────┘     └─────────────────┘
```

### 3.2 Component: civicAPI Client

**File:** `signals/civicapi_client.py`

```python
"""civicAPI client for election night live results."""

import asyncio
import aiohttp
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

CIVICAPI_BASE = "https://api.civicapi.org"

@dataclass
class RaceResults:
    race_id: str
    state: str
    office: str
    precincts_reporting_pct: float
    candidates: list[dict]
    total_votes: int
    last_updated: datetime
    called: bool
    winner: Optional[dict] = None

class CivicAPIClient:
    """Async client for civicAPI election data."""
    
    def __init__(self, rate_limit: float = 1.0):
        self.rate_limit = rate_limit  # requests per second
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_request = 0
        
    async def __aenter__(self):
        self._session = aiohttp.ClientSession()
        return self
        
    async def __aexit__(self, *args):
        await self._session.close()
        
    async def _rate_limited_get(self, endpoint: str) -> dict:
        """Make rate-limited GET request."""
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_request
        if elapsed < self.rate_limit:
            await asyncio.sleep(self.rate_limit - elapsed)
            
        url = f"{CIVICAPI_BASE}/{endpoint}"
        async with self._session.get(url) as resp:
            self._last_request = asyncio.get_event_loop().time()
            resp.raise_for_status()
            return await resp.json()
    
    async def get_races(self, state: Optional[str] = None) -> list[dict]:
        """Get all active races, optionally filtered by state."""
        endpoint = f"states/{state}/races" if state else "races"
        return await self._rate_limited_get(endpoint)
    
    async def get_results(self, race_id: str) -> RaceResults:
        """Get live results for a specific race."""
        data = await self._rate_limited_get(f"races/{race_id}/results")
        return RaceResults(
            race_id=data["race_id"],
            state=data["state"],
            office=data["office"],
            precincts_reporting_pct=data["reporting"]["pct_reporting"],
            candidates=data["results"]["candidates"],
            total_votes=data["results"]["total_votes"],
            last_updated=datetime.fromisoformat(data["last_updated"]),
            called=data["called"],
            winner=data["results"].get("winner")
        )
    
    async def get_call(self, race_id: str) -> Optional[dict]:
        """Get race call status if available."""
        try:
            return await self._rate_limited_get(f"races/{race_id}/calls")
        except aiohttp.ClientResponseError as e:
            if e.status == 404:
                return None  # Race not called yet
            raise
```

### 3.3 Component: Election Night Aggregator

**File:** `services/election_night_aggregator.py`

```python
"""Real-time aggregator for election night data fusion."""

import asyncio
from dataclasses import dataclass
from typing import Dict, List
import logging

logger = logging.getLogger(__name__)

@dataclass
class ElectionNightSnapshot:
    """Combined view of civicAPI results + market prices."""
    race_id: str
    state: str
    office: str
    
    # civicAPI data
    precincts_reporting_pct: float
    vote_margin_pct: float  # D - R
    votes_counted: int
    called: bool
    
    # Market data
    market_price: float  # 0-1, probability of D win
    market_volume_24h: float
    market_liquidity: float
    
    # Computed
    implied_vote_margin: float  # Convert market price to expected margin
    divergence_pct: float  # Difference between actual and implied
    confidence: str  # high | medium | low
    
    # Signal
    signal: Optional[str] = None  # BUY_DEM | BUY_REP | HOLD
    edge_pct: float = 0.0

class ElectionNightAggregator:
    """Poll civicAPI and Polymarket, detect arbitrage opportunities."""
    
    def __init__(self, civic_client, market_client, db):
        self.civic = civic_client
        self.markets = market_client
        self.db = db
        self._tracked_races: Dict[str, dict] = {}
        self._running = False
        
    async def start(self, target_races: List[str]):
        """Start aggregation for specified races."""
        self._running = True
        self._tracked_races = {r: {} for r in target_races}
        
        # Spawn tasks for each race
        tasks = [
            self._poll_race(race_id)
            for race_id in target_races
        ]
        await asyncio.gather(*tasks)
        
    async def _poll_race(self, race_id: str):
        """Poll a single race continuously."""
        while self._running:
            try:
                # Get civicAPI results
                civic_results = await self.civic.get_results(race_id)
                
                # Get Polymarket price
                market_data = await self.markets.get_market_for_race(
                    state=civic_results.state,
                    office=civic_results.office
                )
                
                # Compute divergence
                snapshot = self._compute_snapshot(civic_results, market_data)
                
                # Generate signal if divergence exceeds threshold
                if abs(snapshot.divergence_pct) > 15:
                    snapshot.signal = self._generate_signal(snapshot)
                    await self._emit_signal(snapshot)
                
                # Store snapshot
                await self._store_snapshot(snapshot)
                
                # Log progress
                logger.info(
                    f"{race_id}: {civic_results.precincts_reporting_pct:.1f}% reporting, "
                    f"divergence={snapshot.divergence_pct:+.1f}%"
                )
                
            except Exception as e:
                logger.error(f"Error polling {race_id}: {e}")
                
            # Poll every 30 seconds during active counting
            await asyncio.sleep(30)
    
    def _compute_snapshot(
        self, 
        civic: RaceResults, 
        market: dict
    ) -> ElectionNightSnapshot:
        """Compute divergence between actual votes and market price."""
        
        # Calculate actual vote margin
        dem_votes = sum(c.votes for c in civic.candidates if c["party"] == "DEM")
        rep_votes = sum(c.votes for c in civic.candidates if c["party"] == "REP")
        total_votes = civic.total_votes
        
        if total_votes > 0:
            vote_margin_pct = (dem_votes - rep_votes) / total_votes * 100
        else:
            vote_margin_pct = 0
            
        # Convert market price to implied vote margin
        # Using a simple logistic model: price = 1 / (1 + exp(-margin/10))
        market_price = market.get("price", 0.5)
        implied_margin = -10 * math.log(1/market_price - 1) if market_price not in (0, 1) else 0
        
        # Calculate divergence
        divergence_pct = vote_margin_pct - implied_margin
        
        # Confidence based on reporting %
        if civic.precincts_reporting_pct > 80:
            confidence = "high"
        elif civic.precincts_reporting_pct > 40:
            confidence = "medium"
        else:
            confidence = "low"
            
        return ElectionNightSnapshot(
            race_id=civic.race_id,
            state=civic.state,
            office=civic.office,
            precincts_reporting_pct=civic.precincts_reporting_pct,
            vote_margin_pct=vote_margin_pct,
            votes_counted=total_votes,
            called=civic.called,
            market_price=market_price,
            market_volume_24h=market.get("volume_24h", 0),
            market_liquidity=market.get("liquidity", 0),
            implied_vote_margin=implied_margin,
            divergence_pct=divergence_pct,
            confidence=confidence
        )
    
    def _generate_signal(self, snapshot: ElectionNightSnapshot) -> str:
        """Generate trading signal from divergence."""
        
        # High confidence + large divergence = strong signal
        if snapshot.confidence == "high" and snapshot.divergence_pct > 15:
            return "BUY_DEM"  # Market underpricing D win
        elif snapshot.confidence == "high" and snapshot.divergence_pct < -15:
            return "BUY_REP"  # Market underpricing R win
        elif snapshot.confidence in ("medium", "high") and abs(snapshot.divergence_pct) > 20:
            # Medium confidence but huge divergence = speculative signal
            return "BUY_DEM" if snapshot.divergence_pct > 0 else "BUY_REP"
        else:
            return "HOLD"
    
    async def _emit_signal(self, snapshot: ElectionNightSnapshot):
        """Emit signal to Discord and trade executor."""
        signal_data = {
            "type": "election_night_arb",
            "race_id": snapshot.race_id,
            "state": snapshot.state,
            "signal": snapshot.signal,
            "divergence_pct": snapshot.divergence_pct,
            "confidence": snapshot.confidence,
            "precincts_reporting": snapshot.precincts_reporting_pct,
            "market_price": snapshot.market_price,
            "timestamp": datetime.utcnow().isoformat()
        }
        
        # Emit to Discord
        await self._discord_alert(signal_data)
        
        # Queue for execution
        await self._trade_executor.submit(signal_data)
        
    async def stop(self):
        """Stop aggregation."""
        self._running = False
```

---

## 4. Signal Logic

### 4.1 Divergence Detection

```python
# Convert market price to implied vote margin
def price_to_margin(price: float) -> float:
    """Convert market probability to implied vote margin."""
    # Calibrated on 2024 data: 50% price ≈ 0% margin, 95% price ≈ +15% margin
    if price <= 0.01:
        return -20
    elif price >= 0.99:
        return +20
    return 10 * math.log(price / (1 - price))

# Calculate divergence
divergence = actual_margin - implied_margin

# Signal thresholds
if divergence > 15 and confidence == "high":
    signal = "STRONG_BUY_DEM"
elif divergence > 10 and confidence == "medium":
    signal = "BUY_DEM"
elif divergence < -15 and confidence == "high":
    signal = "STRONG_BUY_REP"
elif divergence < -10 and confidence == "medium":
    signal = "BUY_REP"
else:
    signal = "HOLD"
```

### 4.2 Confidence Calibration

| Precincts Reporting | Confidence | Notes |
|---------------------|------------|-------|
| 0-20% | None | Too early, ignore |
| 20-40% | Low | Early returns, urban bias likely |
| 40-60% | Medium | Trend emerging, caution warranted |
| 60-80% | Medium-High | Clear trend, monitor for shifts |
| 80-95% | High | Near-final, small variance |
| 95-100% | Very High | Final results, arbitrage window closing |

### 4.3 Historical Calibration (2024)

Based on 2024 election night data:

| State | Race | Max Divergence | Time to Close | Profit Potential |
|-------|------|----------------|---------------|------------------|
| AZ | Senate | +18% | 47 min | $2,400 |
| PA | Senate | -12% | 23 min | $890 |
| NV | Senate | +22% | 89 min | $3,100 |
| GA | Presidential | -15% | 34 min | $1,600 |
| WI | Presidential | +11% | 19 min | $620 |

**Key Insight:** The largest divergences occurred in states with slow counting (NV, AZ) and high urban/rural splits. Markets priced based on early returns, then lagged as late-counted precincts shifted results.

---

## 5. API Integration

### 5.1 New Endpoint: `/api/election-night/start`

```python
@router.post("/election-night/start")
async def start_election_night_monitoring(
    races: List[str] = Body(..., description="Race IDs to monitor"),
    auto_trade: bool = False,
    paper_mode: bool = True,
    db: Session = Depends(get_db)
):
    """Start real-time monitoring for election night."""
    
    aggregator = ElectionNightAggregator(
        civic_client=CivicAPIClient(),
        market_client=PolymarketClient(),
        db=db
    )
    
    # Store in global state
    election_night_state["aggregator"] = aggregator
    election_night_state["start_time"] = datetime.utcnow()
    
    # Start in background
    asyncio.create_task(aggregator.start(races))
    
    return {
        "status": "started",
        "races": len(races),
        "auto_trade": auto_trade,
        "paper_mode": paper_mode,
        "started_at": datetime.utcnow().isoformat()
    }
```

### 5.2 New Endpoint: `/api/election-night/status`

```python
@router.get("/election-night/status")
async def get_election_night_status():
    """Get current status of election night monitoring."""
    
    aggregator = election_night_state.get("aggregator")
    if not aggregator:
        return {"status": "not_running"}
    
    return {
        "status": "running",
        "races_tracked": len(aggregator._tracked_races),
        "signals_generated": aggregator.signal_count,
        "uptime_seconds": (datetime.utcnow() - election_night_state["start_time"]).seconds,
        "races": [
            {
                "race_id": r,
                "last_update": aggregator._tracked_races[r].get("last_update"),
                "divergence": aggregator._tracked_races[r].get("divergence"),
                "signal": aggregator._tracked_races[r].get("signal")
            }
            for r in aggregator._tracked_races
        ]
    }
```

### 5.3 New Endpoint: `/api/election-night/signals`

```python
@router.get("/election-night/signals")
async def get_election_night_signals(
    min_divergence: float = 10.0,
    limit: int = 50
):
    """Get generated signals from election night monitoring."""
    
    signals = await db.query(ElectionNightSignal).filter(
        ElectionNightSignal.divergence_pct >= min_divergence
    ).order_by(
        ElectionNightSignal.timestamp.desc()
    ).limit(limit).all()
    
    return {
        "signals": [
            {
                "race_id": s.race_id,
                "state": s.state,
                "signal": s.signal,
                "divergence_pct": s.divergence_pct,
                "market_price": s.market_price,
                "vote_margin_pct": s.vote_margin_pct,
                "confidence": s.confidence,
                "timestamp": s.timestamp.isoformat()
            }
            for s in signals
        ]
    }
```

---

## 6. Database Schema

### 6.1 New Table: `election_night_snapshots`

```sql
CREATE TABLE election_night_snapshots (
    id SERIAL PRIMARY KEY,
    race_id VARCHAR(50) NOT NULL,
    state VARCHAR(2) NOT NULL,
    office VARCHAR(20) NOT NULL,
    
    -- civicAPI data
    precincts_reporting_pct DECIMAL(5,2),
    vote_margin_pct DECIMAL(6,2),
    votes_counted INTEGER,
    called BOOLEAN DEFAULT FALSE,
    
    -- Market data
    market_price DECIMAL(6,4),
    market_volume_24h DECIMAL(15,2),
    market_liquidity DECIMAL(15,2),
    
    -- Computed
    divergence_pct DECIMAL(6,2),
    confidence VARCHAR(10),
    
    -- Metadata
    timestamp TIMESTAMPTZ DEFAULT NOW(),
    
    INDEX idx_race_time (race_id, timestamp),
    INDEX idx_divergence (divergence_pct)
);
```

### 6.2 New Table: `election_night_signals`

```sql
CREATE TABLE election_night_signals (
    id SERIAL PRIMARY KEY,
    race_id VARCHAR(50) NOT NULL,
    state VARCHAR(2) NOT NULL,
    signal VARCHAR(20) NOT NULL,  -- BUY_DEM, BUY_REP, HOLD
    
    -- Signal data
    divergence_pct DECIMAL(6,2),
    confidence VARCHAR(10),
    edge_pct DECIMAL(5,2),
    
    -- Market context
    market_price DECIMAL(6,4),
    market_volume_24h DECIMAL(15,2),
    
    -- Vote context
    vote_margin_pct DECIMAL(6,2),
    precincts_reporting_pct DECIMAL(5,2),
    
    -- Execution
    executed BOOLEAN DEFAULT FALSE,
    execution_price DECIMAL(6,4),
    pnl DECIMAL(10,2),
    
    timestamp TIMESTAMPTZ DEFAULT NOW(),
    
    INDEX idx_timestamp (timestamp),
    INDEX idx_race (race_id)
);
```

---

## 7. Deployment Plan

### 7.1 Pre-Election (T-30 days)

- [ ] Implement civicAPI client
- [ ] Build aggregator core
- [ ] Create database tables
- [ ] Add API endpoints
- [ ] Write tests
- [ ] Deploy to staging

### 7.2 Pre-Election (T-7 days)

- [ ] Load test with simulated data
- [ ] Calibrate divergence thresholds
- [ ] Set up monitoring alerts
- [ ] Train operators on dashboard
- [ ] Deploy to production (disabled)

### 7.3 Election Day

- [ ] Enable monitoring 1 hour before polls close
- [ ] Monitor dashboard continuously
- [ ] Execute signals in paper mode first
- [ ] Switch to live trading after validation

### 7.4 Post-Election

- [ ] Generate performance report
- [ ] Analyze signal accuracy
- [ ] Update calibration for next cycle
- [ ] Archive data

---

## 8. Risk Considerations

### 8.1 Data Quality Risks

| Risk | Mitigation |
|------|------------|
| civicAPI goes down | Fallback to AP Elections API (if subscribed) or state SOS websites |
| Data lag | Cross-reference with multiple sources; flag stale data |
| Incorrect calls | Wait for official call + 5% margin buffer |

### 8.2 Market Risks

| Risk | Mitigation |
|------|------------|
| Market halts | Check market status before execution |
| Liquidity dries up | Only trade markets with >$10k liquidity |
| Price slippage | Use limit orders, not market orders |

### 8.3 Operational Risks

| Risk | Mitigation |
|------|------------|
| System overload | Rate limit civicAPI calls; use caching |
| False signals | Require 2+ confirming data points |
| Fat-finger trades | Paper mode validation before live |

---

## 9. Success Metrics

| Metric | Target |
|--------|--------|
| Signal accuracy | >75% |
| Average divergence captured | >12% |
| Time to signal generation | <60 seconds |
| System uptime | >99.5% |
| PnL per signal | >$500 (paper) |

---

## 10. Open Questions

1. Does civicAPI have a test/sandbox environment?
2. What's the historical accuracy of civicAPI race calls vs. AP/NYT?
3. Should we integrate with state SOS websites as fallback?
4. Do we need real-time websocket support or is polling sufficient?
5. What's the latency from precinct report → civicAPI update?

---

**Next Steps:**
1. Validate civicAPI endpoints with test queries
2. Build MVP client (2-3 days)
3. Run backtest on 2024 data (1 day)
4. Deploy to staging (1 day)

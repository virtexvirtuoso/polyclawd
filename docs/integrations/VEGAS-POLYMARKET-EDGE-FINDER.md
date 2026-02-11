# Vegas-Polymarket Edge Finder

> **Implementation Plan v1.0**  
> Last Updated: 2026-02-07  
> Status: Proposed  
> Author: Virt (via Technical Writer Agent)

---

## Executive Summary

The Vegas-Polymarket Edge Finder is a systematic arbitrage detection system that identifies mispriced prediction markets by comparing real-time sportsbook odds against Polymarket prices. When Vegas implies a 70% probability but Polymarket prices it at 50%, we've found a 20% edge.

**Goal:** Automate the discovery of +EV (positive expected value) betting opportunities by exploiting information asymmetries between traditional sportsbooks and crypto prediction markets.

---

## Table of Contents

1. [The Opportunity](#the-opportunity)
2. [System Architecture](#system-architecture)
3. [Data Sources](#data-sources)
4. [Core Components](#core-components)
5. [Implementation Phases](#implementation-phases)
6. [API Specifications](#api-specifications)
7. [Signal Generation](#signal-generation)
8. [Position Sizing](#position-sizing)
9. [Operational Considerations](#operational-considerations)
10. [Risk Management](#risk-management)
11. [Success Metrics](#success-metrics)
12. [Appendix](#appendix)

---

## The Opportunity

### Real-World Example: Super Bowl 2026

On February 7, 2026, we identified:

| Source | Seahawks Win | Implied Probability |
|--------|--------------|---------------------|
| Vegas (DraftKings) | -230 ML | 69.7% (raw), 67% (vig-adjusted) |
| Polymarket | $0.50 | 50% |
| **Edge** | — | **+17%** |

A $1,000 bet at 50¢ with true 67% probability yields:
- Expected Value: $170
- Kelly Optimal: 34% of bankroll

### Why Do Gaps Exist?

1. **User Base Divergence** — Crypto natives vs. professional sports bettors
2. **Information Velocity** — Vegas reacts to injuries/news in seconds; Polymarket lags minutes to hours
3. **Liquidity Asymmetry** — Vegas handles $100M+; Polymarket markets often have <$50K depth
4. **Vig Arbitrage** — Vegas embeds 4-5% juice; Polymarket is peer-to-peer with no house edge
5. **Market Maturity** — Sports betting: centuries old; Prediction markets: nascent

---

## System Architecture

### High-Level Design

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         EDGE FINDER SYSTEM                              │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌─────────────────┐         ┌─────────────────┐                       │
│  │  The Odds API   │         │   Polymarket    │                       │
│  │  (Vegas Lines)  │         │   (Gamma API)   │                       │
│  └────────┬────────┘         └────────┬────────┘                       │
│           │                           │                                 │
│           ▼                           ▼                                 │
│  ┌─────────────────────────────────────────────────────────────┐       │
│  │                    DATA NORMALIZATION                        │       │
│  │  • Convert American odds → Probability                       │       │
│  │  • Remove vig (overround)                                    │       │
│  │  • Standardize team/event names                              │       │
│  └─────────────────────────────────────────────────────────────┘       │
│                              │                                          │
│                              ▼                                          │
│  ┌─────────────────────────────────────────────────────────────┐       │
│  │                    MARKET MATCHER                            │       │
│  │  • Entity extraction (teams, fighters, candidates)           │       │
│  │  • Event matching (Super Bowl, Week 12, UFC 315)            │       │
│  │  • Fuzzy matching with confidence scores                     │       │
│  │  • Manual override mappings                                  │       │
│  └─────────────────────────────────────────────────────────────┘       │
│                              │                                          │
│                              ▼                                          │
│  ┌─────────────────────────────────────────────────────────────┐       │
│  │                    EDGE CALCULATOR                           │       │
│  │  • Compare Vegas true probability vs Polymarket price        │       │
│  │  • Calculate edge percentage                                 │       │
│  │  • Assess confidence based on:                               │       │
│  │    - Pinnacle availability (sharpest line)                  │       │
│  │    - Number of books agreeing                                │       │
│  │    - Time to event                                           │       │
│  └─────────────────────────────────────────────────────────────┘       │
│                              │                                          │
│                              ▼                                          │
│  ┌─────────────────────────────────────────────────────────────┐       │
│  │                    SIGNAL GENERATOR                          │       │
│  │  • Edge > 10% → ALERT signal                                 │       │
│  │  • Edge > 15% → STRONG signal                                │       │
│  │  • Edge > 20% → EXTREME signal (notify immediately)          │       │
│  │  • Calculate Kelly position size                             │       │
│  └─────────────────────────────────────────────────────────────┘       │
│                              │                                          │
│              ┌───────────────┼───────────────┐                         │
│              ▼               ▼               ▼                         │
│  ┌──────────────────┐ ┌──────────────┐ ┌──────────────────┐           │
│  │ Telegram Alert   │ │ Polyclawd    │ │ Auto-Trader      │           │
│  │ (Notification)   │ │ Dashboard    │ │ (Future)         │           │
│  └──────────────────┘ └──────────────┘ └──────────────────┘           │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Directory Structure

```
polyclawd/
├── api/
│   └── main.py                 # Existing FastAPI app
├── odds/
│   ├── __init__.py
│   ├── client.py               # The Odds API client
│   ├── converter.py            # Odds format conversions
│   ├── matcher.py              # Market matching logic
│   ├── edge_finder.py          # Core edge detection
│   └── models.py               # Data models
├── signals/
│   ├── vegas_edge.py           # New signal source
│   └── ...                     # Existing signal sources
├── config/
│   ├── odds_api.json           # API configuration
│   └── team_mappings.json      # Manual team name mappings
└── docs/
    └── VEGAS-POLYMARKET-EDGE-FINDER.md  # This document
```

---

## Data Sources

### 1. The Odds API (Primary)

**Overview:** Aggregates real-time odds from 40+ licensed sportsbooks worldwide.

| Attribute | Details |
|-----------|---------|
| **Website** | https://the-odds-api.com |
| **Pricing** | Free: 500 calls/mo, $30/mo: 20K calls, $59/mo: 100K calls |
| **Coverage** | NFL, NBA, MLB, NHL, UFC, Soccer, Politics |
| **Bookmakers** | DraftKings, FanDuel, BetMGM, Caesars, Pinnacle, Bovada, 40+ more |
| **Markets** | Moneyline (h2h), Spread, Totals, Futures |
| **Format** | REST API, JSON response |
| **Latency** | ~1 second for fresh odds |

**API Endpoints:**

```bash
# List available sports
GET https://api.the-odds-api.com/v4/sports?apiKey={key}

# Get odds for a sport
GET https://api.the-odds-api.com/v4/sports/{sport}/odds?apiKey={key}&regions=us&markets=h2h

# Get scores/results
GET https://api.the-odds-api.com/v4/sports/{sport}/scores?apiKey={key}
```

**Sample Response:**

```json
{
  "id": "e2b4c7a8d1f5e3b2a9c8d7e6f5a4b3c2",
  "sport_key": "americanfootball_nfl",
  "sport_title": "NFL",
  "commence_time": "2026-02-08T18:30:00Z",
  "home_team": "Seattle Seahawks",
  "away_team": "New England Patriots",
  "bookmakers": [
    {
      "key": "draftkings",
      "title": "DraftKings",
      "last_update": "2026-02-07T17:30:00Z",
      "markets": [
        {
          "key": "h2h",
          "outcomes": [
            {"name": "Seattle Seahawks", "price": -230},
            {"name": "New England Patriots", "price": 190}
          ]
        }
      ]
    },
    {
      "key": "pinnacle",
      "title": "Pinnacle",
      "last_update": "2026-02-07T17:30:00Z",
      "markets": [
        {
          "key": "h2h",
          "outcomes": [
            {"name": "Seattle Seahawks", "price": -225},
            {"name": "New England Patriots", "price": 195}
          ]
        }
      ]
    }
  ]
}
```

### 2. Polymarket Gamma API (Existing)

**Currently Integrated:** Yes, via `polyclawd/api/main.py`

**Endpoints Used:**
- `GET /markets` — List all markets
- `GET /markets/{id}` — Market details
- `GET /prices` — Current prices

**Enhancement Needed:**
- Add category filtering for sports markets
- Improve caching for reduced API calls

### 3. Kalshi API (Future)

**Use Case:** Cross-arbitrage on politics, economics, weather events.

| Attribute | Details |
|-----------|---------|
| **Website** | https://kalshi.com |
| **Pricing** | Free API access |
| **Coverage** | Politics, Economics, Weather, Fed rates |
| **Regulation** | CFTC-regulated (legal in all US states) |

---

## Core Components

### 1. Odds Converter (`odds/converter.py`)

Converts between odds formats and calculates true probabilities.

```python
"""
Odds Converter Module

Handles conversion between:
- American odds (-230, +190)
- Decimal odds (1.43, 2.90)
- Implied probability (0.70, 0.34)
- Vig-adjusted (true) probability
"""

def american_to_probability(odds: int) -> float:
    """
    Convert American odds to implied probability.
    
    Args:
        odds: American odds (e.g., -230 or +190)
    
    Returns:
        Implied probability as decimal (e.g., 0.697)
    
    Examples:
        >>> american_to_probability(-230)
        0.6969...
        >>> american_to_probability(190)
        0.3448...
    """
    if odds < 0:
        return abs(odds) / (abs(odds) + 100)
    else:
        return 100 / (odds + 100)


def remove_vig(probs: list[float]) -> list[float]:
    """
    Remove the bookmaker's vig (overround) to get true probabilities.
    
    The sum of implied probabilities typically exceeds 1.0 (e.g., 1.04).
    This "overround" is the book's edge. We remove it proportionally.
    
    Args:
        probs: List of implied probabilities (should sum > 1.0)
    
    Returns:
        List of true probabilities (sums to 1.0)
    
    Example:
        >>> remove_vig([0.697, 0.345])  # Sum = 1.042 (4.2% vig)
        [0.669, 0.331]                   # Sum = 1.0
    """
    total = sum(probs)
    return [p / total for p in probs]


def calculate_edge(true_prob: float, market_price: float) -> float:
    """
    Calculate the edge (expected value) of a bet.
    
    Args:
        true_prob: True probability of outcome (from sharp books)
        market_price: Current market price on Polymarket
    
    Returns:
        Edge as decimal (e.g., 0.17 = 17% edge)
    
    Example:
        >>> calculate_edge(0.67, 0.50)
        0.17
    """
    return true_prob - market_price
```

### 2. Market Matcher (`odds/matcher.py`)

Matches Vegas events to Polymarket markets.

```python
"""
Market Matcher Module

Solves the core problem of matching:
  The Odds API:  "Seattle Seahawks vs New England Patriots"
  Polymarket:    "Will the Seattle Seahawks win Super Bowl 2026?"

Uses:
1. Entity extraction (team/fighter/candidate names)
2. Event type detection (Super Bowl, playoffs, regular season)
3. Fuzzy string matching with confidence scores
4. Manual override mappings for known markets
"""

from dataclasses import dataclass
from typing import Optional
import re
from rapidfuzz import fuzz, process


@dataclass
class MatchResult:
    """Result of a market matching attempt."""
    vegas_event_id: str
    polymarket_market_id: str
    confidence: float  # 0.0 to 1.0
    match_type: str    # "exact", "fuzzy", "manual"
    team_matched: str
    event_matched: str


# Manual mappings for high-value markets
MANUAL_MAPPINGS = {
    # Super Bowl 2026
    "americanfootball_nfl_super_bowl": {
        "Seattle Seahawks": "540234",  # Polymarket market ID
        "New England Patriots": "540227"
    },
    # Add more as discovered
}


# Team name variations
TEAM_ALIASES = {
    "Seattle Seahawks": ["Seahawks", "Seattle", "SEA"],
    "New England Patriots": ["Patriots", "New England", "NE", "Pats"],
    # ... extend for all teams
}


def extract_entities(text: str) -> dict:
    """
    Extract team names, event types, and dates from text.
    
    Args:
        text: Market question or event title
    
    Returns:
        Dict with extracted entities
    """
    entities = {
        "teams": [],
        "event_type": None,
        "date": None
    }
    
    # Team extraction
    for canonical, aliases in TEAM_ALIASES.items():
        for alias in [canonical] + aliases:
            if alias.lower() in text.lower():
                entities["teams"].append(canonical)
                break
    
    # Event type
    if "super bowl" in text.lower():
        entities["event_type"] = "super_bowl"
    elif "playoff" in text.lower():
        entities["event_type"] = "playoff"
    # ... more patterns
    
    return entities


def match_markets(
    vegas_event: dict,
    polymarket_markets: list[dict],
    min_confidence: float = 0.7
) -> Optional[MatchResult]:
    """
    Find the best Polymarket match for a Vegas event.
    
    Args:
        vegas_event: Event from The Odds API
        polymarket_markets: List of Polymarket markets
        min_confidence: Minimum confidence to return a match
    
    Returns:
        MatchResult if found, None otherwise
    """
    # Try manual mapping first
    sport_key = vegas_event.get("sport_key", "")
    home_team = vegas_event.get("home_team", "")
    
    if sport_key in MANUAL_MAPPINGS:
        if home_team in MANUAL_MAPPINGS[sport_key]:
            return MatchResult(
                vegas_event_id=vegas_event["id"],
                polymarket_market_id=MANUAL_MAPPINGS[sport_key][home_team],
                confidence=1.0,
                match_type="manual",
                team_matched=home_team,
                event_matched=sport_key
            )
    
    # Fuzzy matching fallback
    vegas_entities = extract_entities(
        f"{vegas_event['home_team']} vs {vegas_event['away_team']}"
    )
    
    best_match = None
    best_score = 0
    
    for market in polymarket_markets:
        poly_entities = extract_entities(market.get("question", ""))
        
        # Score based on team overlap and event type
        team_overlap = len(
            set(vegas_entities["teams"]) & set(poly_entities["teams"])
        )
        event_match = vegas_entities["event_type"] == poly_entities["event_type"]
        
        score = (team_overlap * 0.5) + (0.5 if event_match else 0)
        
        if score > best_score and score >= min_confidence:
            best_score = score
            best_match = MatchResult(
                vegas_event_id=vegas_event["id"],
                polymarket_market_id=market["id"],
                confidence=score,
                match_type="fuzzy",
                team_matched=str(vegas_entities["teams"]),
                event_matched=vegas_entities["event_type"] or "unknown"
            )
    
    return best_match
```

### 3. Edge Finder (`odds/edge_finder.py`)

Core logic for detecting mispriced markets.

```python
"""
Edge Finder Module

The brain of the system. Orchestrates:
1. Fetching odds from The Odds API
2. Fetching prices from Polymarket
3. Matching markets
4. Calculating edges
5. Generating signals
"""

from dataclasses import dataclass
from typing import Optional
from datetime import datetime
import httpx

from .converter import american_to_probability, remove_vig, calculate_edge
from .matcher import match_markets, MatchResult


@dataclass
class EdgeSignal:
    """A detected edge opportunity."""
    vegas_event_id: str
    polymarket_market_id: str
    team: str
    event: str
    
    vegas_odds: int           # American odds (e.g., -230)
    vegas_prob_raw: float     # Implied prob with vig
    vegas_prob_true: float    # Vig-removed prob
    
    polymarket_price: float   # Current price (e.g., 0.50)
    
    edge: float               # True prob - market price
    edge_pct: float           # Edge as percentage
    
    books_count: int          # Number of books with this line
    has_pinnacle: bool        # Pinnacle is sharpest
    
    recommended_side: str     # "YES" or "NO"
    kelly_fraction: float     # Optimal bet size
    
    confidence: str           # "low", "medium", "high"
    signal_strength: str      # "alert", "strong", "extreme"
    
    detected_at: datetime
    event_time: datetime
    hours_until: float


class EdgeFinder:
    """
    Main class for finding Vegas-Polymarket edges.
    """
    
    EDGE_THRESHOLDS = {
        "alert": 0.10,    # 10% edge
        "strong": 0.15,   # 15% edge
        "extreme": 0.20   # 20% edge
    }
    
    def __init__(self, odds_api_key: str):
        self.odds_api_key = odds_api_key
        self.base_url = "https://api.the-odds-api.com/v4"
    
    async def fetch_vegas_odds(self, sport: str) -> list[dict]:
        """Fetch current odds from The Odds API."""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/sports/{sport}/odds",
                params={
                    "apiKey": self.odds_api_key,
                    "regions": "us",
                    "markets": "h2h",
                    "oddsFormat": "american"
                }
            )
            return response.json()
    
    async def fetch_polymarket_sports(self) -> list[dict]:
        """Fetch sports-related markets from Polymarket."""
        # Implementation depends on Polymarket API structure
        # Filter for sports category
        pass
    
    def calculate_consensus_odds(self, bookmakers: list[dict]) -> dict:
        """
        Calculate consensus odds across bookmakers.
        
        Prioritizes:
        1. Pinnacle (sharpest line)
        2. Average of top 5 books
        3. Any available line
        """
        pinnacle = None
        all_odds = []
        
        for book in bookmakers:
            if book["key"] == "pinnacle":
                pinnacle = book
            for market in book.get("markets", []):
                if market["key"] == "h2h":
                    for outcome in market["outcomes"]:
                        all_odds.append({
                            "book": book["key"],
                            "team": outcome["name"],
                            "odds": outcome["price"]
                        })
        
        # Use Pinnacle if available, else average
        if pinnacle:
            return {
                "source": "pinnacle",
                "odds": pinnacle["markets"][0]["outcomes"],
                "confidence": "high"
            }
        else:
            # Calculate average odds
            # ... implementation
            return {
                "source": "average",
                "odds": [],  # averaged
                "confidence": "medium"
            }
    
    async def find_edges(
        self,
        sports: list[str] = ["americanfootball_nfl", "basketball_nba"]
    ) -> list[EdgeSignal]:
        """
        Main method: Find all current edges.
        
        Args:
            sports: List of sport keys to scan
        
        Returns:
            List of EdgeSignal objects, sorted by edge size
        """
        signals = []
        
        for sport in sports:
            # 1. Fetch Vegas odds
            vegas_events = await self.fetch_vegas_odds(sport)
            
            # 2. Fetch Polymarket sports markets
            poly_markets = await self.fetch_polymarket_sports()
            
            # 3. Match and calculate edges
            for event in vegas_events:
                match = match_markets(event, poly_markets)
                if not match:
                    continue
                
                # Get consensus Vegas odds
                consensus = self.calculate_consensus_odds(event["bookmakers"])
                
                for outcome in consensus["odds"]:
                    # Calculate probabilities
                    raw_prob = american_to_probability(outcome["odds"])
                    # Get opponent's odds for vig removal
                    opponent_odds = [
                        o["odds"] for o in consensus["odds"] 
                        if o["team"] != outcome["team"]
                    ][0]
                    opponent_prob = american_to_probability(opponent_odds)
                    
                    true_probs = remove_vig([raw_prob, opponent_prob])
                    true_prob = true_probs[0]
                    
                    # Get Polymarket price
                    poly_price = self._get_poly_price(match.polymarket_market_id)
                    
                    # Calculate edge
                    edge = calculate_edge(true_prob, poly_price)
                    
                    if edge >= self.EDGE_THRESHOLDS["alert"]:
                        signal = EdgeSignal(
                            vegas_event_id=event["id"],
                            polymarket_market_id=match.polymarket_market_id,
                            team=outcome["team"],
                            event=sport,
                            vegas_odds=outcome["odds"],
                            vegas_prob_raw=raw_prob,
                            vegas_prob_true=true_prob,
                            polymarket_price=poly_price,
                            edge=edge,
                            edge_pct=edge * 100,
                            books_count=len(event["bookmakers"]),
                            has_pinnacle=consensus["source"] == "pinnacle",
                            recommended_side="YES",
                            kelly_fraction=self._calculate_kelly(edge, poly_price),
                            confidence=consensus["confidence"],
                            signal_strength=self._get_signal_strength(edge),
                            detected_at=datetime.now(),
                            event_time=datetime.fromisoformat(
                                event["commence_time"].replace("Z", "+00:00")
                            ),
                            hours_until=(
                                datetime.fromisoformat(
                                    event["commence_time"].replace("Z", "+00:00")
                                ) - datetime.now()
                            ).total_seconds() / 3600
                        )
                        signals.append(signal)
        
        # Sort by edge size (highest first)
        signals.sort(key=lambda s: s.edge, reverse=True)
        return signals
    
    def _calculate_kelly(self, edge: float, price: float) -> float:
        """Calculate Kelly criterion fraction."""
        # kelly = edge / (1 - price)
        # Cap at 25% for safety (quarter-Kelly)
        raw_kelly = edge / (1 - price) if price < 1 else 0
        return min(raw_kelly * 0.25, 0.25)  # Quarter-Kelly, max 25%
    
    def _get_signal_strength(self, edge: float) -> str:
        """Determine signal strength based on edge size."""
        if edge >= self.EDGE_THRESHOLDS["extreme"]:
            return "extreme"
        elif edge >= self.EDGE_THRESHOLDS["strong"]:
            return "strong"
        else:
            return "alert"
```

---

## Implementation Phases

### Phase 1: MVP (2-3 hours)

**Goal:** Get alerts flowing for manually-mapped high-value events.

**Tasks:**

- [ ] Sign up for The Odds API (free tier)
- [ ] Create `odds/converter.py` with probability functions
- [ ] Create `odds/client.py` with basic API client
- [ ] Manually map Super Bowl + NFL playoffs to Polymarket IDs
- [ ] Add `/api/vegas/edges` endpoint to Polyclawd
- [ ] Test with live Super Bowl data
- [ ] Send first alert to Telegram

**Deliverables:**
- Working edge detection for NFL
- Manual market mapping file
- Telegram alerts on 10%+ edges

**API Budget:** ~50 calls (well within free tier)

---

### Phase 2: Automation (1-2 days)

**Goal:** Automatic market matching and scheduled scanning.

**Tasks:**

- [ ] Build `odds/matcher.py` with fuzzy matching
- [ ] Create team aliases database (all NFL, NBA teams)
- [ ] Auto-discover Polymarket sports markets
- [ ] Add cron job for hourly scans
- [ ] Implement edge caching (avoid duplicate alerts)
- [ ] Add `/api/vegas/markets` endpoint (list matched markets)
- [ ] Dashboard widget for edge finder

**Deliverables:**
- Automatic market matching (>80% accuracy)
- Hourly scan cron job
- Edge history tracking
- Dashboard integration

**API Budget:** ~300 calls/month (within free tier)

---

### Phase 3: Position Sizing (3-4 days)

**Goal:** Kelly-optimal position sizing with risk management.

**Tasks:**

- [ ] Implement Kelly criterion calculator
- [ ] Add bankroll tracking per sport
- [ ] Create position sizing recommendations
- [ ] Integrate with existing rotation system
- [ ] Add edge decay tracking (how fast do edges close?)
- [ ] Historical edge performance tracking

**Deliverables:**
- Kelly-based position recommendations
- Bankroll management system
- Edge decay analytics
- Performance attribution

---

### Phase 4: Advanced Features (1-2 weeks)

**Goal:** Cross-platform arbitrage and ML optimization.

**Tasks:**

- [ ] Integrate Kalshi API for politics/economics
- [ ] Build cross-arb detector (Kalshi ↔ Polymarket)
- [ ] Order book depth analysis
- [ ] Optimal entry timing model
- [ ] Line movement alerts (Vegas line moving toward/away from Poly)
- [ ] Backtesting framework

**Deliverables:**
- Multi-platform arbitrage
- ML-based entry timing
- Comprehensive backtesting
- Performance dashboard

---

## API Specifications

### New Endpoints

#### GET `/api/vegas/edges`

Fetch current edge opportunities.

**Query Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `sport` | string | No | all | Filter by sport (nfl, nba, etc.) |
| `min_edge` | float | No | 0.10 | Minimum edge to return |
| `limit` | int | No | 20 | Max results |

**Response:**

```json
{
  "edges": [
    {
      "id": "edge_1234567890",
      "team": "Seattle Seahawks",
      "event": "Super Bowl 2026",
      "vegas_odds": -230,
      "vegas_prob": 0.67,
      "polymarket_price": 0.50,
      "edge": 0.17,
      "edge_pct": 17.0,
      "signal_strength": "extreme",
      "kelly_fraction": 0.085,
      "recommended_size_usd": 850,
      "polymarket_url": "https://polymarket.com/event/...",
      "hours_until_event": 18.5,
      "detected_at": "2026-02-07T17:30:00Z"
    }
  ],
  "meta": {
    "total_edges": 1,
    "api_calls_remaining": 450,
    "last_scan": "2026-02-07T17:30:00Z"
  }
}
```

#### GET `/api/vegas/markets`

List all matched Vegas-Polymarket markets.

**Response:**

```json
{
  "markets": [
    {
      "vegas_event_id": "abc123",
      "polymarket_id": "540234",
      "sport": "americanfootball_nfl",
      "event": "Super Bowl 2026",
      "teams": ["Seattle Seahawks", "New England Patriots"],
      "match_confidence": 1.0,
      "match_type": "manual",
      "commence_time": "2026-02-08T18:30:00Z"
    }
  ],
  "stats": {
    "total_matched": 15,
    "manual_matches": 5,
    "fuzzy_matches": 10,
    "unmatched_vegas": 3,
    "unmatched_poly": 7
  }
}
```

#### POST `/api/vegas/refresh`

Force refresh of odds data (uses API quota).

**Response:**

```json
{
  "success": true,
  "events_fetched": 24,
  "edges_found": 3,
  "api_calls_used": 2,
  "api_calls_remaining": 448
}
```

---

## Signal Generation

### Integration with Polyclawd Signals

The Vegas edge finder becomes a new signal source in Polyclawd's multi-signal system.

```python
# signals/vegas_edge.py

async def generate_vegas_signals() -> list[dict]:
    """
    Generate trading signals from Vegas-Polymarket edges.
    
    Returns signals compatible with Polyclawd's signal aggregator.
    """
    edge_finder = EdgeFinder(os.getenv("ODDS_API_KEY"))
    edges = await edge_finder.find_edges()
    
    signals = []
    for edge in edges:
        signals.append({
            "source": "vegas_edge",
            "platform": "polymarket",
            "market_id": edge.polymarket_market_id,
            "market": f"{edge.team} - {edge.event}",
            "side": edge.recommended_side,
            "confidence": int(edge.edge_pct * 2),  # 17% edge = 34 confidence
            "price": edge.polymarket_price,
            "reasoning": (
                f"Vegas {edge.vegas_odds} ({edge.vegas_prob_true:.0%}) vs "
                f"Poly {edge.polymarket_price:.0%} = {edge.edge_pct:.1f}% edge"
            ),
            "value": edge.edge_pct,
            "url": f"https://polymarket.com/event/{edge.polymarket_market_id}"
        })
    
    return signals
```

### Signal Weighting

In Polyclawd's Bayesian composite scoring:

| Source | Base Weight | Notes |
|--------|-------------|-------|
| `vegas_edge` | 1.5x | High weight: Vegas is sharp |
| `whale_activity` | 1.2x | |
| `volume_spike` | 1.0x | |
| `momentum` | 0.8x | |

Vegas edge signals should be weighted higher because sportsbooks represent the sharpest probability estimates available.

---

## Position Sizing

### Kelly Criterion Implementation

```python
def calculate_kelly_position(
    bankroll: float,
    true_prob: float,
    market_price: float,
    max_fraction: float = 0.25,
    kelly_multiplier: float = 0.25  # Quarter-Kelly for safety
) -> dict:
    """
    Calculate optimal position size using Kelly Criterion.
    
    The Kelly formula maximizes long-term growth rate:
    f* = (bp - q) / b
    
    Where:
    - f* = fraction of bankroll to bet
    - b = net odds received (payout - 1)
    - p = probability of winning
    - q = probability of losing (1 - p)
    
    Args:
        bankroll: Current bankroll in USD
        true_prob: True probability from Vegas
        market_price: Polymarket price
        max_fraction: Maximum allowed fraction (risk limit)
        kelly_multiplier: Fraction of Kelly to use (0.25 = quarter-Kelly)
    
    Returns:
        Position sizing details
    """
    # Calculate edge
    edge = true_prob - market_price
    
    if edge <= 0:
        return {"position_usd": 0, "reason": "No positive edge"}
    
    # Kelly formula
    # For binary options: f* = (p - price) / (1 - price)
    # Where p = true_prob, price = market_price
    
    b = (1 / market_price) - 1  # Net odds
    q = 1 - true_prob
    
    full_kelly = (b * true_prob - q) / b
    
    # Apply Kelly multiplier (quarter-Kelly is conservative)
    adjusted_kelly = full_kelly * kelly_multiplier
    
    # Apply maximum fraction limit
    final_fraction = min(adjusted_kelly, max_fraction)
    
    # Calculate position
    position_usd = bankroll * final_fraction
    
    return {
        "position_usd": round(position_usd, 2),
        "fraction": round(final_fraction, 4),
        "full_kelly": round(full_kelly, 4),
        "adjusted_kelly": round(adjusted_kelly, 4),
        "edge": round(edge, 4),
        "expected_value": round(position_usd * edge, 2),
        "max_loss": round(position_usd, 2),
        "max_gain": round(position_usd * (1 / market_price - 1), 2)
    }
```

### Example Calculation

For the Seahawks bet:
- Bankroll: $10,000
- True probability: 67%
- Market price: 50¢

```
edge = 0.67 - 0.50 = 0.17
b = (1/0.50) - 1 = 1.0
full_kelly = (1.0 × 0.67 - 0.33) / 1.0 = 0.34 (34%)
quarter_kelly = 0.34 × 0.25 = 0.085 (8.5%)
position = $10,000 × 0.085 = $850
```

---

## Operational Considerations

### API Rate Limiting

**The Odds API Free Tier:**
- 500 requests/month
- ~16 requests/day

**Recommended Schedule:**

```python
SCAN_SCHEDULE = {
    "normal": "0 */4 * * *",      # Every 4 hours (6/day)
    "game_day": "*/30 * * * *",    # Every 30 min on game days
    "high_value": "*/15 * * * *"   # Every 15 min for playoffs/Super Bowl
}
```

**Budget Allocation:**
- Regular scans: 180 calls/month (6/day × 30 days)
- Game day boosts: 200 calls/month
- Manual/on-demand: 120 calls/month (reserve)

### Caching Strategy

```python
CACHE_TTL = {
    "odds": 300,           # 5 minutes (odds change frequently)
    "matched_markets": 3600,  # 1 hour (markets don't change often)
    "team_mappings": 86400    # 24 hours (static data)
}
```

### Error Handling

```python
class OddsAPIError(Exception):
    """Base exception for Odds API errors."""
    pass

class RateLimitError(OddsAPIError):
    """API rate limit exceeded."""
    pass

class MarketMatchError(Exception):
    """Could not match Vegas event to Polymarket."""
    pass

# Implement exponential backoff
async def fetch_with_retry(url: str, max_retries: int = 3):
    for attempt in range(max_retries):
        try:
            response = await client.get(url)
            if response.status_code == 429:
                raise RateLimitError("Rate limit exceeded")
            return response.json()
        except RateLimitError:
            wait = 2 ** attempt
            await asyncio.sleep(wait)
    raise OddsAPIError(f"Failed after {max_retries} attempts")
```

---

## Risk Management

### Position Limits

| Risk Parameter | Value | Rationale |
|----------------|-------|-----------|
| Max single position | 10% of bankroll | Diversification |
| Max daily exposure | 30% of bankroll | Prevent overtrading |
| Max per sport | 20% of bankroll | Sport-specific risk |
| Min edge to trade | 10% | Filter noise |
| Kelly multiplier | 25% (quarter-Kelly) | Conservative sizing |

### Edge Decay Monitoring

Edges close over time as markets become efficient. Track:

```python
@dataclass
class EdgeDecay:
    edge_id: str
    initial_edge: float
    current_edge: float
    time_elapsed_hours: float
    decay_rate: float  # edge points per hour
    
    @property
    def half_life_hours(self) -> float:
        """Time for edge to decay by 50%."""
        if self.decay_rate <= 0:
            return float('inf')
        return (self.initial_edge * 0.5) / self.decay_rate
```

### Circuit Breakers

```python
CIRCUIT_BREAKERS = {
    "daily_loss_pct": 0.10,      # Stop if down 10% today
    "consecutive_losses": 5,     # Pause after 5 losses
    "api_error_threshold": 3,    # Stop scanning after 3 API errors
    "edge_false_positive_rate": 0.30  # Reduce confidence if 30% of edges are wrong
}
```

---

## Success Metrics

### Key Performance Indicators

| Metric | Target | Measurement |
|--------|--------|-------------|
| Edge detection accuracy | >80% | Edges that were real (post-resolution) |
| Average edge captured | >12% | (entry price - fair value) for winning bets |
| Win rate on edge signals | >60% | Trades that were profitable |
| ROI on edge trades | >20% annualized | Net profit / capital deployed |
| False positive rate | <20% | Edges that weren't real |
| Market match accuracy | >90% | Correct Vegas-Poly matches |

### Tracking Dashboard

Add to Polyclawd dashboard:

```
┌─────────────────────────────────────────────────────────┐
│                 VEGAS EDGE FINDER                       │
├─────────────────────────────────────────────────────────┤
│  Active Edges: 3          │  Avg Edge: 14.2%           │
│  API Calls Today: 12/16   │  Last Scan: 5 min ago      │
├─────────────────────────────────────────────────────────┤
│  PERFORMANCE (Last 30 Days)                             │
│  ───────────────────────────────────────────────────── │
│  Trades: 47  │  Won: 31 (66%)  │  ROI: +23.4%          │
│  Avg Edge: 13.1%  │  Avg Size: $420  │  Total P&L: +$982│
├─────────────────────────────────────────────────────────┤
│  CURRENT EDGES                                          │
│  ───────────────────────────────────────────────────── │
│  🏈 Seahawks WIN    │  Edge: 17%  │  Kelly: $850       │
│  🏀 Lakers WIN      │  Edge: 11%  │  Kelly: $340       │
│  🥊 Jones WIN       │  Edge: 14%  │  Kelly: $520       │
└─────────────────────────────────────────────────────────┘
```

---

## Appendix

### A. Sport Keys (The Odds API)

| Sport | Key | Active Season |
|-------|-----|---------------|
| NFL | `americanfootball_nfl` | Sep - Feb |
| NBA | `basketball_nba` | Oct - Jun |
| MLB | `baseball_mlb` | Mar - Oct |
| NHL | `icehockey_nhl` | Oct - Jun |
| UFC | `mma_mixed_martial_arts` | Year-round |
| Soccer (EPL) | `soccer_epl` | Aug - May |
| Politics | `politics_us_presidential_winner` | Election years |

### B. American Odds Conversion Table

| American | Decimal | Implied Prob |
|----------|---------|--------------|
| -500 | 1.20 | 83.3% |
| -300 | 1.33 | 75.0% |
| -200 | 1.50 | 66.7% |
| -150 | 1.67 | 60.0% |
| -110 | 1.91 | 52.4% |
| +100 | 2.00 | 50.0% |
| +150 | 2.50 | 40.0% |
| +200 | 3.00 | 33.3% |
| +300 | 4.00 | 25.0% |
| +500 | 6.00 | 16.7% |

### C. Environment Variables

```bash
# .env
ODDS_API_KEY=your_key_here
ODDS_API_TIER=free  # free, 20k, 100k
VEGAS_EDGE_MIN=0.10
VEGAS_EDGE_KELLY_MULT=0.25
VEGAS_SCAN_INTERVAL=4h
```

### D. References

1. [The Odds API Documentation](https://the-odds-api.com/liveapi/guides/v4/)
2. [Polymarket API](https://docs.polymarket.com/)
3. [Kelly Criterion Explained](https://en.wikipedia.org/wiki/Kelly_criterion)
4. [Pinnacle: Why Sharp Lines Matter](https://www.pinnacle.com/en/betting-resources/betting-tools/closing-line-value)
5. [Removing Vig from Betting Lines](https://www.sportsbettingdime.com/guides/betting-101/removing-juice-from-odds/)

---

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-02-07 | Initial implementation plan |

---

*Document generated by Virt using Technical Writer Agent methodology.*

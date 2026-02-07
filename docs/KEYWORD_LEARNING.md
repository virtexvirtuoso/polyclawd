# Keyword Learning System

> Self-improving signal intelligence that learns from every trade.

---

## Table of Contents

1. [Overview](#overview)
2. [How It Works](#how-it-works)
3. [The Learning Loop](#the-learning-loop)
4. [Technical Architecture](#technical-architecture)
5. [API Reference](#api-reference)
6. [Data Storage](#data-storage)
7. [Configuration](#configuration)
8. [Performance Metrics](#performance-metrics)
9. [Examples](#examples)
10. [Troubleshooting](#troubleshooting)

---

## Overview

The Keyword Learning System is a **self-improving intelligence layer** that tracks which keywords lead to winning trades and automatically adjusts future signal confidence based on historical performance.

### Key Features

| Feature | Description |
|---------|-------------|
| **Zero Configuration** | Works out of the box, no API keys needed |
| **Automatic Learning** | Updates weights when trades resolve |
| **Dynamic Extraction** | Discovers keywords from any market title |
| **Confidence Boosting** | Winning keywords boost future signals |
| **No Maintenance** | Gets smarter with every trade |

### Why It Matters

```
Traditional System:
  Signal → Fixed Confidence → Trade → Outcome (learning lost)

Learning System:
  Signal → Confidence + Keyword History → Trade → Outcome → Updated Weights
                ↑                                              │
                └──────────────────────────────────────────────┘
```

---

## How It Works

### The Core Concept

Every news-based trading signal uses **keywords** to find relevant news. The system tracks:

1. Which keywords were used for each trade
2. Whether that trade won or lost
3. The cumulative win rate per keyword

Keywords with high win rates **boost** future signal confidence.  
Keywords with low win rates **reduce** future signal confidence.

### Weight Calculation

```
Weight = Win Rate / 0.50

Examples:
  60% win rate → 1.20x weight (20% boost)
  50% win rate → 1.00x weight (neutral)
  40% win rate → 0.80x weight (20% reduction)
  75% win rate → 1.50x weight (50% boost)
```

### Confidence Adjustment

```python
adjustment = (average_keyword_weight - 1.0) × 50

Examples:
  Keywords avg 1.20 → +10 confidence points
  Keywords avg 1.50 → +25 confidence points
  Keywords avg 0.80 → -10 confidence points
```

---

## The Learning Loop

### Visual Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│   ┌─────────────┐    ┌─────────────┐    ┌─────────────┐        │
│   │   Market    │    │  Keyword    │    │   Google    │        │
│   │   Title     │───▶│ Extraction  │───▶│    News     │        │
│   └─────────────┘    └─────────────┘    └─────────────┘        │
│                            │                   │                │
│                            ▼                   ▼                │
│                      ["bitcoin",         Breaking news         │
│                       "ETF",              articles              │
│                       "SEC"]                  │                 │
│                            │                  │                 │
│                            ▼                  ▼                 │
│                      ┌─────────────────────────┐                │
│                      │    Sentiment Analysis   │                │
│                      │    + Confidence Score   │                │
│                      └───────────┬─────────────┘                │
│                                  │                              │
│                                  ▼                              │
│   ┌─────────────┐    ┌─────────────────────────┐               │
│   │  Keyword    │◀───│   Apply Keyword Boost   │               │
│   │  Weights    │    │   (if historical data)  │               │
│   └─────────────┘    └───────────┬─────────────┘               │
│         ▲                        │                              │
│         │                        ▼                              │
│         │            ┌─────────────────────────┐               │
│         │            │     Execute Trade       │               │
│         │            │  Record: market_id +    │               │
│         │            │  keywords + "pending"   │               │
│         │            └───────────┬─────────────┘               │
│         │                        │                              │
│         │              (days/weeks pass)                        │
│         │                        │                              │
│         │                        ▼                              │
│         │            ┌─────────────────────────┐               │
│         │            │    Market Resolves      │               │
│         │            │    YES wins / NO wins   │               │
│         │            └───────────┬─────────────┘               │
│         │                        │                              │
│         │                        ▼                              │
│         │            ┌─────────────────────────┐               │
│         │            │  check_and_resolve_     │               │
│         │            │  positions() runs       │               │
│         │            └───────────┬─────────────┘               │
│         │                        │                              │
│         │                        ▼                              │
│         │            ┌─────────────────────────┐               │
│         └────────────│  update_keyword_outcome │               │
│                      │  ("win" or "loss")      │               │
│                      └─────────────────────────┘               │
│                                                                 │
│                         🔄 LOOP CONTINUES                       │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Step-by-Step Process

#### Step 1: Signal Generation

When a news signal fires:

```python
# Market: "Will Bitcoin ETF see $50B inflows?"

# 1. Extract keywords
keywords = extract_keywords(market_title)
# Result: ["bitcoin", "ETF", "$50B"]

# 2. Check learned weights
weights = get_keyword_weights()
# Result: {"bitcoin": 1.33, "etf": 1.50}

# 3. Apply boost to confidence
base_confidence = 50
boosted = boost_confidence_by_keywords(base_confidence, keywords)
# Result: 66.5 (base 50 + 16.5 boost)

# 4. Record keyword usage
record_keyword_usage(keywords, market_id)
```

#### Step 2: Trade Execution

```python
# Position created:
{
    "market_id": "btc-etf-50b",
    "market": "Will Bitcoin ETF see $50B inflows?",
    "side": "YES",
    "amount": 50.00,
    "price": 0.45,
    "source": "news_breaking",  # ← Important: marks as news-based
    "keywords": ["bitcoin", "ETF", "$50B"],
    "status": "open"
}
```

#### Step 3: Trade Resolution

When `check_and_resolve_positions()` runs:

```python
# Market resolved: YES won
# Your position: WIN (+$55.56)

# If source == "news_breaking":
update_keyword_outcome(market_id, "win")

# This updates:
#   bitcoin: 4W/2L → 5W/2L (71%)
#   ETF: 3W/0L → 4W/0L (100%)
#   $50B: 0W/0L → 1W/0L (tracked, no weight yet)
```

#### Step 4: Future Signals Benefit

```python
# Next signal: "Bitcoin ETF approval in Europe"
keywords = ["bitcoin", "ETF", "Europe"]

# Weights now:
#   bitcoin: 1.42x (71% win rate)
#   ETF: 2.00x (100% win rate)
#   Europe: 1.00x (no data)

# Average weight: 1.47x
# Confidence boost: +23.5 points
```

---

## Technical Architecture

### File Structure

```
polyclawd/
├── signals/
│   ├── news_signal.py       # News signal generation
│   └── keyword_learner.py   # Learning system core
├── api/
│   └── main.py              # API endpoints + resolution hook
└── data/
    └── keyword_learner/     # Learned data storage
        ├── keyword_stats.json
        ├── learned_keywords.json
        └── market_entities.json
```

### Core Functions

| Function | Location | Purpose |
|----------|----------|---------|
| `extract_keywords()` | news_signal.py | Pattern + dynamic extraction |
| `extract_entities()` | keyword_learner.py | NLP entity extraction |
| `record_keyword_usage()` | keyword_learner.py | Track keyword → market |
| `update_keyword_outcome()` | keyword_learner.py | Update win/loss stats |
| `get_keyword_weights()` | keyword_learner.py | Get learned weights |
| `boost_confidence_by_keywords()` | keyword_learner.py | Apply confidence boost |
| `get_smart_keywords()` | keyword_learner.py | Get weighted keywords |

### Integration Points

```python
# 1. Signal Generation (news_signal.py:520)
from keyword_learner import get_smart_keywords, boost_confidence_by_keywords
keywords = get_smart_keywords(market_title)
confidence = boost_confidence_by_keywords(base_confidence, keywords)

# 2. Trade Resolution (main.py:1805-1806)
if source == "news_breaking" and market_id:
    from keyword_learner import update_keyword_outcome
    update_keyword_outcome(market_id, "win" if won else "loss")
```

---

## API Reference

### GET /api/keywords/stats

Returns learned keyword performance statistics.

**Response:**
```json
{
    "enabled": true,
    "top_keywords": [
        {
            "keyword": "bitcoin",
            "win_rate": 0.67,
            "total_trades": 6,
            "wins": 4,
            "losses": 2,
            "pending": 1
        }
    ],
    "trending_entities": [
        {
            "entity": "Bitcoin",
            "type": "PROPER_NOUN",
            "count": 15,
            "market_count": 12
        }
    ],
    "weights": {
        "bitcoin": 1.33,
        "trump": 1.50,
        "etf": 2.00
    },
    "generated_at": "2026-02-06T20:30:00Z"
}
```

### POST /api/keywords/learn

Manually teach the keyword learner.

**Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| title | string | Yes | Market title to extract keywords from |
| market_id | string | No | Unique market identifier |
| outcome | string | No | "win", "loss", or null for pending |

**Example:**
```bash
curl -X POST "https://virtuosocrypto.com/polyclawd/api/keywords/learn?\
title=Will%20Bitcoin%20hit%20%24200k%3F&\
market_id=btc-200k&\
outcome=win"
```

**Response:**
```json
{
    "title": "Will Bitcoin hit $200k?",
    "entities": [
        {"entity": "Bitcoin", "type": "PROPER_NOUN", "confidence": 0.9}
    ],
    "search_terms": ["Bitcoin", "$200k"],
    "smart_keywords": [["Bitcoin", 1.33], ["$200k", 1.0]],
    "outcome_recorded": true
}
```

### POST /api/keywords/update-outcome

Update keyword stats when a trade resolves.

**Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| market_id | string | Yes | Market identifier |
| outcome | string | Yes | "win" or "loss" |

**Example:**
```bash
curl -X POST "https://virtuosocrypto.com/polyclawd/api/keywords/update-outcome?\
market_id=btc-200k&\
outcome=win"
```

---

## Data Storage

### keyword_stats.json

Stores win/loss statistics per keyword.

```json
{
    "keywords": {
        "bitcoin": {
            "total_uses": 15,
            "wins": 10,
            "losses": 5,
            "pending": 2,
            "markets": ["btc-100k", "btc-etf", "btc-halving"],
            "first_seen": "2026-01-15T10:30:00Z",
            "last_used": "2026-02-06T20:15:00Z"
        },
        "trump": {
            "total_uses": 8,
            "wins": 6,
            "losses": 2,
            "pending": 1,
            "markets": ["trump-tariff", "trump-2028"],
            "first_seen": "2026-01-20T14:00:00Z",
            "last_used": "2026-02-06T19:45:00Z"
        }
    }
}
```

### market_entities.json

Stores discovered entities across all markets.

```json
{
    "entities": {
        "bitcoin": {
            "original": "Bitcoin",
            "type": "PROPER_NOUN",
            "count": 45,
            "markets": ["btc-100k", "btc-etf", ...],
            "first_seen": "2026-01-10T08:00:00Z"
        },
        "elon musk": {
            "original": "Elon Musk",
            "type": "PERSON_OR_ORG",
            "count": 12,
            "markets": ["musk-tiktok", "musk-twitter"],
            "first_seen": "2026-01-22T16:30:00Z"
        }
    },
    "last_scan": "2026-02-06T20:00:00Z"
}
```

### Storage Location

```
~/.openclaw/polyclawd/keyword_learner/
├── keyword_stats.json      # Win/loss stats
├── learned_keywords.json   # Manual additions
└── market_entities.json    # Discovered entities
```

---

## Configuration

### Thresholds

| Setting | Default | Description |
|---------|---------|-------------|
| MIN_TRADES_FOR_WEIGHT | 3 | Minimum resolved trades before keyword gets a weight |
| MAX_CONFIDENCE_BOOST | 50 | Maximum confidence points to add/subtract |
| KEYWORD_CACHE_SIZE | 500 | Maximum keywords to track |
| MARKET_CACHE_SIZE | 50 | Markets per keyword to remember |

### Modifying Thresholds

Edit `signals/keyword_learner.py`:

```python
# Line 165: Minimum trades for weight
if total >= 3:  # Change to 5 for more conservative learning
    win_rate = entry["wins"] / total
    weights[kw] = win_rate / 0.5
```

---

## Performance Metrics

### Expected Learning Curve

| Trades | Accuracy | Notes |
|--------|----------|-------|
| 0-10 | Baseline | No learned weights yet |
| 10-50 | +5-10% | Key patterns emerging |
| 50-100 | +10-15% | Strong keyword signals |
| 100+ | +15-20% | Mature system |

### Measuring Effectiveness

```bash
# Check keyword performance
curl -s https://virtuosocrypto.com/polyclawd/api/keywords/stats | \
  python3 -c "
import json, sys
data = json.load(sys.stdin)
keywords = data.get('top_keywords', [])
if keywords:
    avg_wr = sum(k['win_rate'] for k in keywords) / len(keywords)
    print(f'Keywords tracked: {len(keywords)}')
    print(f'Average win rate: {avg_wr:.1%}')
    print(f'Best keyword: {keywords[0][\"keyword\"]} ({keywords[0][\"win_rate\"]:.0%})')
"
```

---

## Examples

### Example 1: Bitcoin Trade Lifecycle

```
Day 1: Signal fires
├── Market: "Will Bitcoin ETF approval happen in Q1?"
├── Keywords extracted: ["bitcoin", "ETF", "Q1"]
├── No historical data → base confidence 55
└── Trade executed: YES @ $0.52

Day 14: Market resolves
├── Outcome: YES wins
├── Your position: WIN (+$48)
├── update_keyword_outcome("btc-etf-q1", "win")
└── Stats updated:
    ├── bitcoin: 0W/0L → 1W/0L
    ├── ETF: 0W/0L → 1W/0L
    └── Q1: 0W/0L → 1W/0L

Day 15: New signal
├── Market: "Will Bitcoin hit $150k?"
├── Keywords: ["bitcoin", "$150k"]
├── bitcoin weight: 1.0 (only 1 trade, need 3)
└── Confidence: 55 (no boost yet)

Day 45: After 10 Bitcoin trades (7W/3L)
├── New market: "Bitcoin mining regulation"
├── Keywords: ["bitcoin", "mining"]
├── bitcoin weight: 1.40 (70% win rate)
├── Confidence: 55 → 75 (+20 boost)
└── Higher confidence = larger position size
```

### Example 2: Multi-Keyword Boost

```python
# Market: "Will Trump impose 100% tariffs on China?"
keywords = ["trump", "tariff", "china"]

# Historical performance:
#   trump: 8W/2L = 80% → weight 1.60
#   tariff: 5W/1L = 83% → weight 1.67
#   china: 2W/3L = 40% → weight 0.80

# Average weight: (1.60 + 1.67 + 0.80) / 3 = 1.36

# Confidence calculation:
base = 50
adjustment = (1.36 - 1.0) × 50 = +18
final = 50 + 18 = 68

# Result: 68 confidence (vs 50 without learning)
```

### Example 3: Negative Learning

```python
# Keywords that consistently lose get penalized

# "crash" keyword history: 1W/4L = 20% → weight 0.40

# Market: "Will Bitcoin crash below $50k?"
keywords = ["bitcoin", "crash"]

# Weights:
#   bitcoin: 1.40 (good)
#   crash: 0.40 (bad)

# Average: 0.90

# Confidence adjustment: (0.90 - 1.0) × 50 = -5
# Result: 50 → 45 (reduced confidence)

# System learns: "crash" headlines often wrong
```

---

## Troubleshooting

### Keywords Not Updating

**Symptom:** Weights stay at 1.0 after trades resolve.

**Causes:**
1. Trade source isn't "news_breaking"
2. Less than 3 resolved trades for keyword
3. market_id not matching

**Fix:**
```bash
# Check if trades have correct source
curl -s https://virtuosocrypto.com/polyclawd/api/paper/positions | \
  python3 -c "
import json, sys
for p in json.load(sys.stdin).get('positions', []):
    print(f\"{p.get('source', 'none'):20} {p.get('market', '')[:40]}\")"
```

### No Confidence Boost

**Symptom:** `boost_confidence_by_keywords()` returns base value.

**Cause:** Keywords don't have enough trades (minimum 3).

**Fix:** Wait for more trades to resolve, or manually teach:
```bash
curl -X POST "https://virtuosocrypto.com/polyclawd/api/keywords/learn?\
title=Bitcoin%20test&market_id=test1&outcome=win"
# Repeat 3+ times
```

### Entity Extraction Missing Keywords

**Symptom:** Important words not extracted.

**Cause:** Word not capitalized or not in pattern list.

**Fix:** Add to `MARKET_PATTERNS` in `news_signal.py`:
```python
MARKET_PATTERNS = {
    # Add new pattern
    r"your_keyword": ["your_keyword", "related_term"],
}
```

Or add to `IMPORTANT_NOUNS` in `keyword_learner.py`:
```python
IMPORTANT_NOUNS = [
    "inflation", "recession", ...,
    "your_new_noun",  # Add here
]
```

---

## Summary

The Keyword Learning System creates a **feedback loop** between trading outcomes and signal generation:

```
┌──────────────────────────────────────────────────┐
│                                                  │
│   Trade → Outcome → Keyword Stats Updated        │
│     ↑                         │                  │
│     │                         ▼                  │
│     └──── Future Confidence Adjusted ◀──────────┘
│                                                  │
└──────────────────────────────────────────────────┘
```

**Key Takeaways:**

1. ✅ **Zero maintenance** - learns automatically
2. ✅ **No API keys** - pure local NLP
3. ✅ **Improves over time** - more trades = smarter signals
4. ✅ **Transparent** - check `/api/keywords/stats` anytime
5. ✅ **Safe** - minimum 3 trades before weight applied

---

*Document version: 1.0*  
*Last updated: 2026-02-06*  
*Author: Virt ⚡*

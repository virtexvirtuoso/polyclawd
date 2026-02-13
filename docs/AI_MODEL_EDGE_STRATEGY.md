# AI Model Market Edge Strategy

## Overview

Prediction markets on AI model performance (e.g., "Which company will top Chatbot Arena?") have structural inefficiencies that create tradeable edges. This document defines our approach.

## Edge Sources

### 1. Arena Leaderboard Monitoring
**What:** Daily scraping of [lmarena.ai](https://lmarena.ai/leaderboard) (now arena.ai) rankings.  
**Edge:** Market prices lag leaderboard updates by hours to days. Retail bettors check once and set-and-forget.  
**Implementation:** `signals/ai_model_tracker.py` — fetches, snapshots to SQLite, tracks score deltas.

### 2. Score Gap Analysis  
**What:** Quantify how far each company is from #1.  
**Edge:** A 20+ point gap in Arena score is nearly impossible to close in <30 days without a new model. Markets consistently overprice longshots.  
**Example (Feb 2026):**
| Company | Best Rank | Score | Gap from #1 |
|---------|-----------|-------|-------------|
| Anthropic | 1 | ~1506 | — |
| Google | 3 | ~1486 | ~20 pts |
| OpenAI | 10 | ~1460 | ~46 pts |
| xAI | 4 | ~1475 | ~31 pts |

Google at 25% implied to overtake Anthropic's 20-point lead in 16 days → overpriced by 10-15%.

### 3. New Model Release Detection
**What:** Track when new models appear on Arena leaderboard.  
**Edge:** A surprise model submission is the main disruption risk. Early detection (within hours of submission) gives a window before market prices react.  
**Sources to monitor:**
- Arena leaderboard (new entries)
- Company blogs/Twitter (Anthropic, Google DeepMind, OpenAI)
- GitHub repos (model cards, API changelog)
- HuggingFace model hub (new uploads)

### 4. Vote Velocity & Stability
**What:** Track how many votes/battles a model has accumulated on Arena.  
**Edge:** Models with <500 votes have volatile scores. Markets overreact to preliminary rankings.  
**Strategy:** Fade early hype on newly submitted models until vote count stabilizes (>1000 battles).

### 5. Cross-Platform Arbitrage
**What:** Same AI question on Polymarket vs Kalshi may price differently.  
**Edge:** Polymarket skews crypto-native (bullish on open-source/decentralized AI). Kalshi skews mainstream (overweights big tech brands).

## Signal Generation Logic

```
For each AI model market:
  1. Fetch current Arena leaderboard
  2. Map each market outcome to a company
  3. Estimate fair probability:
     - Current #1: 65-80% (scaled by gap to #2)
     - Rank 2-3: 10-25% (scaled by score gap)
     - Rank 4-5: 3-5%
     - Rank 6+: 1-2%
  4. Compare fair value vs market price
  5. If edge > 5%: generate signal
  6. Adjust for:
     - New model releases (increase uncertainty)
     - Vote instability (<500 battles)
     - Days to resolution (more time = more uncertainty)
```

## Confidence Modifiers

| Factor | Modifier | Rationale |
|--------|----------|-----------|
| Score gap > 20 pts | +10% conf | Very hard to close |
| Score gap < 5 pts | -15% conf | Tight race, uncertain |
| New model from challenger | -10% conf | Disruption risk |
| < 500 votes on leader | -10% conf | Ranking unstable |
| > 2000 votes on leader | +5% conf | Ranking locked in |
| < 7 days to resolution | +5% conf | Less time for disruption |
| > 30 days to resolution | -10% conf | Anything can happen |

## Risk Factors

1. **Surprise releases** — Google/OpenAI can drop a model that immediately tops Arena
2. **Metric gaming** — Companies optimizing specifically for Arena benchmark
3. **Leaderboard methodology changes** — Style control, category weights could shift rankings
4. **Vote manipulation** — Coordinated voting could temporarily distort scores
5. **Resolution criteria ambiguity** — "Style control on vs off" can give different #1

## Market Categories This Applies To

- "Which company will have #1 LLM?" (Arena-based)
- "Will [Model X] beat [Model Y]?" (head-to-head)
- "Will [Company] release a new model by [date]?" (release timing)
- AI benchmark markets (MMLU, HumanEval, etc.)
- "Best coding model" / "Best reasoning model" (category-specific)

## Integration with Pipeline

The `ai_model_tracker` module feeds into `mispriced_category_signal.py` as a specialized sub-signal:

```
mispriced_category_signal.py
  └── ai_model_tracker.py (tech/AI markets)
  └── standard category edge (all other categories)
```

When a market is detected as AI/tech category, the tracker provides:
- Arena-based fair value estimate
- Score gap confidence adjustment
- New model risk factor

## Data Storage

- **SQLite:** `storage/ai_model_tracker.db` — snapshots, deltas, releases
- **JSON snapshots:** `storage/arena_snapshots/arena_YYYYMMDD_HHMMSS.json`
- **Daily cron:** Watchdog runs `ai_model_tracker.py snapshot` every 6 hours

## Backtest Validation Needed

- [ ] Historical Arena score data (if available) to validate gap-based probabilities
- [ ] Past Polymarket AI model markets — check if our fair values would have been profitable
- [ ] Vote velocity correlation with final ranking stability

## Status

- [x] Module built (`signals/ai_model_tracker.py`)
- [x] Strategy documented
- [ ] Integrated into mispriced_category pipeline
- [ ] API endpoint added (`/api/signals/ai-models`)
- [ ] MCP tool added (`polyclawd_ai_model_tracker`)
- [ ] Cron job for periodic snapshots
- [ ] Backtest against historical markets
- [ ] Head-to-head market evaluation
- [ ] Release tracker (blog/GitHub monitoring)

## First Live Signal

**Market:** "Which company will have #1 model on Chatbot Arena on Feb 28, 2026?"  
**Signal:** NO Google @ ~75% confidence  
**Edge:** Market prices Google at 25%, fair value 8-12% → 13-17% edge  
**Arena data:** Anthropic #1/#2, Google (Gemini 3 Pro) #3, 20-point gap  

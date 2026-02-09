# Polyclawd Cron Jobs

Automated monitoring and alerting for all 12 intelligence types.

---

## Active Cron Jobs

| Name | Schedule | Intelligence Type | Description |
|------|----------|-------------------|-------------|
| `polyclawd-rotation-alert` | Every 30m | Trading | Position rotation opportunities |
| `resolution-timing-alert` | Every 2h | #7 Resolution | Markets resolving <24h |
| `vegas-edge-scanner` | Every 2h | #10 Vegas Edge | Sharp books vs Polymarket |
| `polyclawd-monitor` | Every 2h | System | Health + whale signals + engine |
| `injury-impact-scanner` | Every 3h | #6 Injuries | Key injuries + stale lines |
| `correlation-violation-scanner` | Every 4h | #8 Correlation | Constraint violations (arb) |
| `orderbook-whale-walls` | Every 4h | #5 Microstructure | Large walls = whale activity |
| `edge-scanner-6h` | Every 6h | #1 Cross-platform | Full arb scan |
| `kalshi-edge-scanner` | 9am/3pm/9pm | #11 Entertainment | Kalshi vs Polymarket |
| `weekly-signal-calibration` | Sun 9am | #12 Calibration | Win rates by signal source |

---

## Intelligence Coverage

| # | Intelligence Type | Monitored By | Alert Threshold |
|---|-------------------|--------------|-----------------|
| 1 | Cross-platform arb | edge-scanner-6h | >10% edge |
| 2 | Sharp vs soft | vegas-edge-scanner | >8% edge |
| 3 | Expert divergence | *(via signals endpoint)* | >10% divergence |
| 4 | Whale behavior | polyclawd-monitor | >$1k whale signals |
| 5 | Orderbook microstructure | orderbook-whale-walls | >$50k walls |
| 6 | Injury impact | injury-impact-scanner | Key player + stale line |
| 7 | Resolution timing | resolution-timing-alert | <24h + 80-95% price |
| 8 | Correlation violations | correlation-violation-scanner | >2% violation |
| 9 | Manifold wisdom | *(via signals endpoint)* | Top trader consensus |
| 10 | Vegas edge | vegas-edge-scanner | >8% edge |
| 11 | Entertainment props | kalshi-edge-scanner | >5% edge |
| 12 | Calibration feedback | weekly-signal-calibration | Weekly report |

---

## Job Details

### injury-impact-scanner

**Schedule:** `0 */3 * * *` (every 3 hours)  
**Timezone:** America/New_York  
**Priority:** HIGH (time-sensitive)

**Purpose:**
Detect key player injuries before Vegas lines adjust. This is one of the highest-value signals because lines move within minutes of injury news.

**Logic:**
1. Fetch ESPN injury reports
2. Check for recent updates (last 6 hours)
3. Identify key players (QB, star RB/WR)
4. Compare to current Vegas lines
5. Alert if line appears stale

**Historical Impact:**
- Starting QB out: ~3-4 point swing
- Star RB out: ~1-2 point swing
- Key defender out: ~0.5-1 point swing

**Alert Format:**
```
🏥 INJURY EDGE DETECTED

Player: Patrick Mahomes (KC)
Status: Questionable → Doubtful
Current Line: Chiefs -7.5
Expected Move: Chiefs -3.5 to -4.5
Action: Bet AGAINST Chiefs before line moves

⏰ Time-sensitive! Lines move fast.
```

---

### resolution-timing-alert

**Schedule:** `0 */2 * * *` (every 2 hours)  
**Timezone:** America/New_York  
**Priority:** MEDIUM

**Purpose:**
Find markets approaching resolution with high probability of one outcome. These are theta collection opportunities with low risk.

**Logic:**
1. Fetch markets resolving in <24 hours
2. Filter for prices 80-95% (likely winners)
3. Check volume >$50k (liquidity)
4. Calculate expected profit

**Why This Works:**
Markets at 90% with 12 hours left will likely drift to 100%. Buying at 90¢ to collect 10¢ is low-risk when resolution is near and no catalyst expected.

**Alert Format:**
```
⏰ RESOLUTION OPPORTUNITY

Market: Will X happen by Feb 9?
Price: 92¢ (YES)
Resolves: 8 hours
Volume: $125k
Expected Profit: 8¢ per share

Action: Buy YES, hold to resolution
```

---

### correlation-violation-scanner

**Schedule:** `0 */4 * * *` (every 4 hours)  
**Timezone:** America/New_York  
**Priority:** HIGH (when found)

**Purpose:**
Detect probability constraint violations between related markets. These are mathematical mispricings with high conviction.

**Constraints Checked:**
- P(Team wins Championship) ≤ P(Team wins Conference)
- P(Candidate wins Election) ≤ P(Candidate wins Primary)
- P(Player wins Award) ≤ P(Player nominated)

**Logic:**
1. Group markets by entity (team/person)
2. Identify parent/child relationships
3. Check if child price > parent price
4. Alert on violations >2%

**Alert Format:**
```
🚨 CORRELATION VIOLATION (ARB)

Entity: Kansas City Chiefs
Parent: Win AFC @ 45%
Child: Win Super Bowl @ 52%  ← VIOLATION!

Constraint: P(SB) ≤ P(AFC)
Violation: 7%

Action: Buy NO on Super Bowl market
This is FREE MONEY - math constraint broken
```

---

### orderbook-whale-walls

**Schedule:** `0 */4 * * *` (every 4 hours)  
**Timezone:** America/New_York  
**Priority:** MEDIUM

**Purpose:**
Detect large orders (walls) in orderbooks that indicate whale accumulation or distribution.

**What Walls Mean:**
- **Bid wall:** Large buy order = whale accumulating (bullish)
- **Ask wall:** Large sell order = whale distributing (bearish)
- **Imbalance >3:1:** Strong directional pressure

**Logic:**
1. Fetch trending markets
2. Check orderbook depth for top 5
3. Identify walls >$50k at single price
4. Calculate bid/ask imbalance

**Alert Format:**
```
🐋 WHALE WALL DETECTED

Market: Will Trump win 2028?
Wall Side: BID (accumulation)
Size: $85,000 at 42¢
Imbalance: 4.2:1 bullish

Interpretation: Whale building position
Signal: Bullish (follow whale)
```

---

### weekly-signal-calibration

**Schedule:** `0 9 * * 0` (Sunday 9am)  
**Timezone:** America/New_York  
**Priority:** LOW (informational)

**Purpose:**
Weekly review of signal source performance. Helps tune which signals to trust more/less.

**Report Contents:**
1. Win rate by signal source
2. Sample size per source
3. Best/worst performers this week
4. Recommendations for trust adjustments
5. Overall P&L summary

**Sources Tracked:**
- inverse_whale
- smart_money
- vegas_edge
- metaculus
- volume_spike
- manifold_traders
- cross_platform

**Report Format:**
```
📊 WEEKLY CALIBRATION REPORT

Signal Performance (7 days):
┌─────────────────┬─────────┬────────┐
│ Source          │ Win Rate│ Trades │
├─────────────────┼─────────┼────────┤
│ vegas_edge      │ 68%     │ 12     │
│ inverse_whale   │ 62%     │ 8      │
│ metaculus       │ 58%     │ 5      │
│ volume_spike    │ 51%     │ 15     │
└─────────────────┴─────────┴────────┘

Recommendations:
✅ Trust vegas_edge more (consistent)
⚠️ volume_spike underperforming (near coin flip)

Overall: +$340 realized P&L
```

---

### vegas-edge-scanner

**Schedule:** `0 */2 * * *` (every 2 hours)  
**Timezone:** America/New_York  
**Priority:** HIGH

**Purpose:**
Compare Vegas sharp book lines (Pinnacle, Circa) against Polymarket prices to find sports mispricings.

**Logic:**
1. Fetch odds from The Odds API
2. Prioritize sharp books (Pinnacle > DraftKings)
3. Devig using Shin method for heavy favorites
4. Compare to Polymarket prices
5. Alert on edge >8%

**Sports Covered:**
- NFL (including Super Bowl)
- NBA
- MLB
- NHL
- Soccer (EPL, UCL, La Liga, Bundesliga)

**Alert Format:**
```
🏈 VEGAS EDGE DETECTED

Match: Chiefs vs Eagles (Super Bowl)
Pinnacle (devigged): Chiefs 52.3%
Polymarket: Chiefs 48¢
Edge: +4.3% on Chiefs

Kelly (25%, $10k): $107
Action: Buy Chiefs YES on Polymarket
```

---

### polyclawd-monitor

**Schedule:** `0 */2 * * *` (every 2 hours)  
**Timezone:** America/New_York  
**Priority:** MEDIUM

**Purpose:**
System health monitoring and whale signal aggregation.

**Checks:**
1. Rotation opportunities (weak positions)
2. Whale signals >$1k
3. Engine status (adaptive boost, drawdown halt)
4. Position EVs (identify worst performers)

**Alert Triggers:**
- Any position rotations
- Whale signals detected
- Drawdown halt triggered
- Adaptive boost >20

---

### polyclawd-rotation-alert

**Schedule:** `*/30 * * * *` (every 30 minutes)  
**Timezone:** America/New_York  
**Priority:** MEDIUM

**Purpose:**
Detect when existing positions should be exited for better opportunities (opportunity cost optimization).

**Logic:**
1. Calculate current position EVs
2. Compare to available new signals
3. If new signal EV > current + transaction cost
4. Recommend rotation

---

### kalshi-edge-scanner

**Schedule:** `0 9,15,21 * * *` (9am, 3pm, 9pm)  
**Timezone:** America/New_York  
**Priority:** MEDIUM

**Purpose:**
Scan Kalshi-exclusive markets for edges, especially entertainment props.

**Focus Areas:**
- Super Bowl props (halftime, anthem)
- Award shows (Grammys, Oscars)
- Political events
- Economic indicators

---

### edge-scanner-6h

**Schedule:** Every 6 hours  
**Priority:** MEDIUM

**Purpose:**
Comprehensive cross-platform arbitrage scan across all prediction market sources.

**Platforms Scanned:**
- Polymarket
- Kalshi
- PredictIt
- Manifold
- Metaculus
- Betfair
- Vegas books

---

## Managing Jobs

### List All Jobs
```bash
openclaw cron list
```

### Run Job Manually
```bash
openclaw cron run <job-id>
```

### Pause/Resume
```bash
openclaw cron pause <job-id>
openclaw cron resume <job-id>
```

### View Job History
```bash
openclaw cron runs <job-id>
```

---

## Best Practices

1. **Stagger schedules:** Jobs are spread across different intervals to avoid API rate limits
2. **Use NO_REPLY:** Jobs only alert when actionable items found
3. **Time-sensitive first:** Injury scanner runs most frequently (every 3h)
4. **Weekly calibration:** Tune signal trust based on actual performance
5. **Isolated sessions:** Each job runs in its own context

---

## Schedule Overview

```
:00  :30  :00  :30  :00  :30  :00  :30  (minutes)
 │    │    │    │    │    │    │    │
 │    └────│────└────│────└────│────└── rotation-alert (every 30m)
 │         │         │         │
 └─────────└─────────└─────────└─────── resolution + vegas + monitor (every 2h)
 │                   │
 └───────────────────└───────────────── injury-impact (every 3h)
 │                             │
 └─────────────────────────────└─────── correlation + orderbook (every 4h)
 │
 └───────────────────────────────────── edge-scanner (every 6h)

Daily: kalshi-edge at 9am, 3pm, 9pm
Weekly: calibration report Sunday 9am
```

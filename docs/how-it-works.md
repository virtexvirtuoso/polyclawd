# How Polyclawd Works

## The One-Liner

**People overpay for YES. We sell it to them. Math says we win 7 out of 10 times.**

---

## Step 1: Find Markets

Every 10 minutes, the system scans **Polymarket** — a site where people bet YES or NO on questions like:

> *"Will Bitcoin be above $64,000 on March 1st?"*
> *"Will Sean Penn win Best Supporting Actor?"*

Each question has a YES price and a NO price. They always add up to $1.

So if YES is 55¢, NO is 45¢.

---

## Step 2: Decide If The Market Is Wrong

Here's the core insight from the **Becker dataset** (408,000 real resolved markets):

**Most things people bet YES on... don't happen.**

People are optimistic. They bid up YES on exciting outcomes. "Will X reach $100K?" "Will Y win the award?" The crowd overpays for YES.

Result: **NO wins 59-82% of the time** depending on the type of market.

### NO Win Rate by Duration

| Duration    | NO Win Rate |
|-------------|-------------|
| Same day    | 53.3%       |
| 1-7 days    | 59.8%       |
| 1-4 weeks   | 63.0%       |
| 1-3 months  | 77.6%       |
| 3-12 months | 82.6%       |

### NO Win Rate by Category

| Category     | NO Win Rate | Sample Size |
|-------------|-------------|-------------|
| Sports       | 69.7%       | 6,267       |
| Geopolitical | 68.8%       | 6,715       |
| Politics     | 68.2%       | 12,201      |
| AI           | 67.6%       | 1,687       |
| Other        | 60.6%       | 232,308     |
| Crypto/Price | 54.4%       | 117,723     |

### The Filters

The system looks at every market and asks:

- ✅ Is this the kind of market where NO historically wins?
- ✅ Is there enough volume ($50K+) to trust the price?
- ✅ Is the duration 7+ days? (Short markets are coin flips)
- ✅ Is the NO price reasonable? (Not too cheap, not too expensive)

If all pass → **bet NO**.

---

## Step 3: Decide How Much To Bet

We use the **Kelly Criterion** — a formula that sizes bets based on edge.

### What Is Kelly?

Kelly answers: *"Given my win rate and the payout, what percentage of my bankroll should I bet?"*

- **Full Kelly** = the mathematically optimal amount (too aggressive in practice)
- **We use 1/6 Kelly** = bet 1/6th of what Kelly says (safer, still captures most of the growth)

### Example

- NO costs 40¢ on a market where NO wins 75% of the time
- Our edge = 75% - 40% = 35%
- Kelly says bet X% of our $10,000 bankroll
- We divide by 6 → bet ~$400

### Why Not Full Kelly?

| Fraction | Risk Level   | Bet on $10K |
|----------|-------------|-------------|
| Full     | Reckless    | $6,400      |
| 1/4      | Aggressive  | $1,600      |
| **1/6**  | **Current** | **$1,000**  |
| 1/8      | Conservative| $800        |

Full Kelly assumes your edge estimate is perfect. It never is. 1/6 Kelly survives bad streaks while still growing.

---

## Step 4: Wait For Resolution

The market closes on its end date. Either:

- **NO wins** → We paid 40¢, get $1 back = **60¢ profit per share** ✅
- **YES wins** → We paid 40¢, get $0 back = **40¢ loss per share** ❌

Since NO wins ~70-80% of the time on our filtered markets, we profit over many bets.

---

## Step 5: That's It

The whole system is:

1. **Scan** → Find markets every 10 minutes
2. **Filter** → Only keep high-edge markets (7-365 days, $50K+ volume)
3. **Bet NO** → The crowd overpays for YES
4. **Size with Kelly** → Bet proportional to our edge, divided by 6
5. **Resolve** → Collect winnings or take the loss
6. **Repeat** → Law of large numbers does the rest

---

## Current Settings

| Setting         | Value   | Why                                      |
|----------------|---------|------------------------------------------|
| Bankroll        | $10,000 | Paper trading starting capital           |
| Kelly Fraction  | 1/6     | Balanced risk — backed by 79% WR data   |
| Max Bet         | $1,000  | Cap per position                         |
| Min Volume      | $50,000 | Only liquid, trustworthy markets         |
| Duration        | 7-365d  | Skip coin-flip dailies, capture monthly+ edge |
| Side            | NO only | Structural YES overpricing               |

---

## The Data Behind It

All of this is backed by the **Becker dataset** — 408,000 real Polymarket markets with known outcomes.

### The Sweet Spot

| Filter                        | Markets | NO Win Rate | EV per $1 bet |
|------------------------------|---------|-------------|----------------|
| Everything                    | 76,781  | 58.8%       | $0.24          |
| 7-365d, $50K+ volume         | 24,040  | 67.2%       | $0.32          |
| 30-365d, $100K+ volume       | 6,493   | 79.0%       | $0.44          |

The tighter the filter, the higher the win rate — but fewer bets. Our current settings balance edge with opportunity.

---

## Status

🟡 **Paper trading** — no real money. Tracking what would happen if we placed these bets.

Once we see enough resolved trades to confirm the edge holds live, the system can be connected to real exchange accounts.

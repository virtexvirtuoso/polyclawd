"""
Synthetic Stop-Loss Backtest

Uses REAL price dynamics extracted from 1,648 weather market observations:
- Vol per 5-min tick: 0.081 (8.1% per tick)  
- Jump frequency: 4.1% of ticks are >2σ jumps
- Jump size: +33% up, -30% down on average
- Mean reverting: autocorrelation -0.14 (prices bounce back)
- Biggest single-tick move: +113% / -57%

For each of 4,877 brackets:
1. Generate realistic price path from entry to resolution
2. Apply 50% stop-loss on each tick
3. Score against actual outcome
"""
import sqlite3
import math
import random
from collections import Counter, defaultdict

random.seed(42)

db = sqlite3.connect('/var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db')
db.row_factory = sqlite3.Row

bt = db.execute("""
    SELECT city, target_date, bracket_low_f, bracket_high_f,
           actual_high_f, hit, volume, comparison
    FROM backtest_brackets
    ORDER BY target_date, city
""").fetchall()

# ── CALIBRATED PRICE DYNAMICS (from real data) ────────────────────────

# Per-tick volatility (5-min intervals)
BASE_VOL = 0.081

# Jump process: 4.1% of ticks have jumps > 2σ
JUMP_PROB = 0.041
JUMP_UP_MEAN = 0.33    # average positive jump: +33%
JUMP_DOWN_MEAN = -0.30  # average negative jump: -30%
JUMP_UP_STD = 0.20
JUMP_DOWN_STD = 0.15

# Mean reversion strength (autocorrelation = -0.14)
MEAN_REVERSION = 0.14

# Weather markets typically have 48h of trading
# ~48h * 12 ticks/hr = 576 ticks, but price moves cluster around forecast updates
# Major forecast updates: 4x/day (00Z, 06Z, 12Z, 18Z GFS runs)
# So effectively: ~8 major update windows in 48h, each with ~10-15 active ticks
TICKS_PER_TRADE = 200  # conservative: not all ticks have price movement


def simulate_price_path(entry_yes_price, final_outcome_yes, n_ticks=TICKS_PER_TRADE):
    """
    Simulate a realistic YES price path from entry to resolution.
    
    Uses jump-diffusion with mean reversion, calibrated from real weather market data.
    
    entry_yes_price: YES price when we enter (NO position)
    final_outcome_yes: 1.0 if bracket hits (YES wins), 0.0 if not (NO wins)
    n_ticks: number of 5-min price ticks
    
    Returns: list of YES prices at each tick
    """
    prices = [entry_yes_price]
    p = entry_yes_price
    prev_return = 0
    
    # The price needs to drift toward the final outcome
    # Use a bridge process: drift increases as we approach resolution
    for t in range(1, n_ticks + 1):
        progress = t / n_ticks  # 0 -> 1
        remaining = 1 - progress
        
        # Base drift toward outcome (bridge)
        if remaining > 0.01:
            target = final_outcome_yes
            drift = (target - p) * 0.01 * (1 + 2 * progress**2)  # accelerates near end
        else:
            drift = (final_outcome_yes - p) * 0.5
        
        # Volatility decreases as we approach resolution (price becomes more certain)
        vol = BASE_VOL * max(0.1, 1.0 - 0.7 * progress**1.5)
        
        # Normal return with mean reversion
        normal_return = drift + vol * random.gauss(0, 1) - MEAN_REVERSION * prev_return
        
        # Jump component
        if random.random() < JUMP_PROB:
            if random.random() < 0.5:
                jump = random.gauss(JUMP_UP_MEAN, JUMP_UP_STD)
            else:
                jump = random.gauss(JUMP_DOWN_MEAN, JUMP_DOWN_STD)
            # Scale jump by remaining time (bigger jumps further from resolution)
            normal_return += jump * max(0.2, remaining)
        
        p = p * (1 + normal_return)
        p = max(0.01, min(0.99, p))  # bound to valid price range
        prices.append(p)
        prev_return = normal_return
    
    # Final tick: resolve to outcome
    prices.append(final_outcome_yes)
    
    return prices


def run_backtest(bet_size, stop_pct, label=""):
    """Run full backtest with given bet size and stop-loss %."""
    
    outcomes = Counter()
    total_pnl = 0
    city_results = defaultdict(lambda: {'n': 0, 'wins': 0, 'losses': 0, 
                                         'stopped_saved': 0, 'stopped_lost': 0, 'pnl': 0})
    weekly_pnl = defaultdict(float)
    daily_pnl = defaultdict(float)
    max_dd = 0
    peak = 0
    running = 0
    stop_ticks = []  # when stops trigger (% through trade)
    
    for b in bt:
        city = b['city']
        actual = b['actual_high_f']
        lo = b['bracket_low_f']
        hi = b['bracket_high_f']
        hit = b['hit']
        
        if actual is None or (lo is None and hi is None):
            outcomes['skip_data'] += 1
            continue
        
        # Determine entry YES price (what market is pricing the bracket at)
        # Use a reasonable range: 5-20% for 2°F brackets
        if lo is not None and hi is not None:
            width = hi - lo
            if width <= 2:
                entry_yes = random.uniform(0.05, 0.15)
            elif width <= 5:
                entry_yes = random.uniform(0.10, 0.25)
            else:
                entry_yes = random.uniform(0.15, 0.35)
        else:
            entry_yes = random.uniform(0.10, 0.25)
        
        entry_no = 1 - entry_yes
        
        # Skip if NO price too high (no edge)
        if entry_no > 0.95:
            outcomes['skip_no_edge'] += 1
            continue
        
        # Final outcome
        final_yes = 1.0 if hit else 0.0
        
        # Simulate price path
        prices = simulate_price_path(entry_yes, final_yes)
        
        # Check stop-loss at each tick
        stopped = False
        stop_pnl = None
        
        for tick, yes_p in enumerate(prices[:-1]):  # exclude final resolution
            no_current = 1 - yes_p
            unrealized = bet_size * (no_current / entry_no - 1)
            loss_pct = abs(unrealized) / bet_size if unrealized < 0 else 0
            
            if unrealized < 0 and loss_pct >= stop_pct:
                stopped = True
                stop_pnl = unrealized
                stop_ticks.append(tick / len(prices))
                
                if hit:
                    outcomes['stopped_saved'] += 1
                    city_results[city]['stopped_saved'] += 1
                else:
                    outcomes['stopped_lost'] += 1
                    city_results[city]['stopped_lost'] += 1
                break
        
        if stopped:
            pnl = stop_pnl
        elif hit:
            # NO loses fully
            pnl = -bet_size
            outcomes['loss'] += 1
            city_results[city]['losses'] += 1
        else:
            # NO wins
            pnl = bet_size * (1 - entry_no) / entry_no
            outcomes['win'] += 1
            city_results[city]['wins'] += 1
        
        city_results[city]['n'] += 1
        city_results[city]['pnl'] += pnl
        total_pnl += pnl
        running += pnl
        peak = max(peak, running)
        dd = peak - running
        max_dd = max(max_dd, dd)
        
        d = b['target_date']
        daily_pnl[d] += pnl
        
        from datetime import datetime
        dt = datetime.strptime(d, '%Y-%m-%d')
        week = dt.strftime('%Y-W%U')
        weekly_pnl[week] += pnl
    
    # ── Report ──
    total_trades = outcomes['win'] + outcomes['loss'] + outcomes['stopped_saved'] + outcomes['stopped_lost']
    
    print(f"\n{'='*80}")
    print(f"{label} | Bet: ${bet_size} | Stop: {stop_pct:.0%}")
    print(f"{'='*80}")
    print(f"  Trades: {total_trades}")
    print(f"  Wins (held): {outcomes['win']}")
    print(f"  Losses (held): {outcomes['loss']}")
    print(f"  Stopped (saved): {outcomes['stopped_saved']} (would have lost ${outcomes['stopped_saved'] * bet_size:,})")
    print(f"  Stopped (cost): {outcomes['stopped_lost']} (would have won)")
    print(f"  Skipped: {outcomes['skip_data'] + outcomes['skip_no_edge']}")
    
    wr = outcomes['win'] / total_trades * 100 if total_trades else 0
    print(f"\n  Win Rate: {wr:.1f}%")
    print(f"  Total P&L: ${total_pnl:+,.0f}")
    print(f"  P&L/trade: ${total_pnl/total_trades:+.2f}" if total_trades else "")
    print(f"  Max Drawdown: ${max_dd:,.0f}")
    
    # What would P&L be WITHOUT stops?
    no_stop_wins = outcomes['win'] + outcomes['stopped_lost']
    no_stop_losses = outcomes['loss'] + outcomes['stopped_saved']
    avg_win = bet_size * 0.111  # NO at ~90c wins ~$11.11 per $100
    no_stop_pnl = no_stop_wins * avg_win - no_stop_losses * bet_size
    print(f"\n  WITHOUT stops: ${no_stop_pnl:+,.0f}")
    print(f"  WITH stops:    ${total_pnl:+,.0f}")
    diff = total_pnl - no_stop_pnl
    print(f"  Stop impact:   ${diff:+,.0f} ({'helps' if diff > 0 else 'hurts'})")
    
    # Stop timing
    if stop_ticks:
        avg_stop = sum(stop_ticks) / len(stop_ticks) * 100
        print(f"\n  Avg stop fires at: {avg_stop:.0f}% through trade")
        early = sum(1 for t in stop_ticks if t < 0.33)
        mid = sum(1 for t in stop_ticks if 0.33 <= t < 0.66)
        late = sum(1 for t in stop_ticks if t >= 0.66)
        print(f"  Early (first third): {early} | Mid: {mid} | Late (last third): {late}")
    
    # Per city
    print(f"\n  {'City':<18} {'N':>5} {'W':>5} {'L':>5} {'StS':>4} {'StL':>4} {'WR':>6} {'P&L':>10}")
    for c in sorted(city_results, key=lambda x: -city_results[x]['pnl']):
        s = city_results[c]
        n = s['n']
        wr_c = s['wins'] / n * 100 if n > 0 else 0
        print(f"  {c:<18} {n:>5} {s['wins']:>5} {s['losses']:>5} {s['stopped_saved']:>4} {s['stopped_lost']:>4} {wr_c:>5.0f}% ${s['pnl']:>+9,.0f}")
    
    # Weekly
    print(f"\n  Weekly P&L:")
    cum = 0
    losing = 0
    for w in sorted(weekly_pnl):
        p = weekly_pnl[w]
        cum += p
        flag = " ⚠️" if p < 0 else ""
        if p < 0: losing += 1
        print(f"    {w}: ${p:+,.0f} (cum: ${cum:+,.0f}){flag}")
    print(f"  Losing weeks: {losing}/{len(weekly_pnl)}")
    
    # Worst/best days
    worst = sorted(daily_pnl.items(), key=lambda x: x[1])[:3]
    best = sorted(daily_pnl.items(), key=lambda x: -x[1])[:3]
    print(f"\n  Worst days: {', '.join(f'{d}: ${p:+,.0f}' for d, p in worst)}")
    print(f"  Best days: {', '.join(f'{d}: ${p:+,.0f}' for d, p in best)}")
    
    return total_pnl, max_dd, total_trades


# ── Run all scenarios ───────────────────────────────────────────────

print(f"Brackets: {len(bt)}")
print(f"Using synthetic price paths calibrated from 1,648 real weather market observations")
print(f"Dynamics: vol=8.1%/tick, jumps=4.1%@±30%, mean_reversion=-0.14, 200 ticks/trade")

results = []
for size in [100, 500]:
    for stop in [0.30, 0.40, 0.50, 0.60, 0.70, 1.00]:
        stop_label = f"{stop:.0%}" if stop < 1.0 else "NONE"
        pnl, dd, n = run_backtest(size, stop, f"SIZE ${size} | STOP {stop_label}")
        results.append((size, stop, pnl, dd, n))

# Summary comparison
print("\n" + "=" * 80)
print("SUMMARY: STOP-LOSS SENSITIVITY")
print("=" * 80)
print(f"  {'Size':>6} {'Stop':>6} {'Trades':>7} {'P&L':>12} {'Max DD':>10} {'P&L/Trade':>10}")
for size, stop, pnl, dd, n in results:
    stop_label = f"{stop:.0%}" if stop < 1.0 else "NONE"
    pt = pnl/n if n else 0
    print(f"  ${size:>5} {stop_label:>6} {n:>7} ${pnl:>+11,.0f} ${dd:>9,.0f} ${pt:>+9.2f}")

db.close()

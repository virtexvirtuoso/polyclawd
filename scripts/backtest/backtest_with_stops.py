"""
Backtest WITH Stop-Loss Simulation

The key challenge: we don't have real price history for closed Polymarket markets.
So we SIMULATE price movement using weather forecast error decay.

Model:
- At entry (48h out), forecast has city-specific RMSE
- Every ~6h, forecast updates with decreasing error
- Each update changes the implied bracket probability (= YES price)
- If NO position unrealized loss >= 50%, stop triggers
- Otherwise, hold to resolution

This gives us realistic stop-loss behavior without needing actual orderbook data.
"""
import sqlite3
import math
import random
import sys
from collections import Counter

sys.path.insert(0, '/var/www/virtuosocrypto.com/polyclawd')
from signals.weather_ensemble import _calibrate_probability, _norm_cdf

random.seed(42)

db = sqlite3.connect('/var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db')
db.row_factory = sqlite3.Row

bt = db.execute("""
    SELECT city, target_date, bracket_low_f, bracket_high_f,
           actual_high_f, hit, volume, comparison
    FROM backtest_brackets
    ORDER BY target_date, city
""").fetchall()

print(f"Total brackets: {len(bt)}")

# City RMSE at different time horizons (modeled from weather forecast error decay)
# RMSE drops roughly: 48h=full, 24h=70%, 12h=50%, 6h=35%
CITY_RMSE_48H = {
    "miami": 2.6, "dallas": 3.1, "atlanta": 4.3, "chicago": 5.5,
    "seattle": 2.9, "toronto": 4.3, "london": 2.1, "paris": 1.6,
    "tokyo": 3.3, "seoul": 4.2, "buenos-aires": 4.4, "sao-paulo": 2.8,
    "munich": 2.9, "ankara": 2.0, "lucknow": 2.8, "singapore": 1.2,
    "wellington": 3.7, "new-york-city": 4.1,
}

CITY_BIAS = {
    "miami": 0.6, "dallas": -1.3, "atlanta": 1.8, "chicago": 2.0,
    "seattle": -0.2, "toronto": 1.9, "london": 1.0, "paris": 0.5,
    "tokyo": -0.7, "seoul": 1.3, "buenos-aires": 2.2, "sao-paulo": 1.1,
    "munich": 1.4, "ankara": 1.0, "lucknow": -1.3, "singapore": 0.3,
    "wellington": 1.6, "new-york-city": 0.0,
}

# Forecast error decay multipliers at each checkpoint
# time_hours_before_event → RMSE multiplier
RMSE_DECAY = [
    (48, 1.00),  # Entry point
    (36, 0.85),
    (24, 0.70),
    (12, 0.50),
    (6,  0.35),
    (0,  0.0),   # Resolution (actual known)
]

STOP_LOSS_PCT = 0.50  # Close if unrealized loss >= 50% of bet

def bracket_prob(forecast_mean, rmse, bracket_low, bracket_high):
    """Calculate YES probability for a bracket given forecast."""
    eff_std = max(rmse, 1.0)
    if bracket_low is not None and bracket_high is not None:
        z_lo = (bracket_low - forecast_mean) / eff_std
        z_hi = (bracket_high - forecast_mean) / eff_std
        return _norm_cdf(z_hi) - _norm_cdf(z_lo)
    elif bracket_high is not None:  # below
        z = (bracket_high - forecast_mean) / eff_std
        return _norm_cdf(z)
    elif bracket_low is not None:  # above
        z = (bracket_low - forecast_mean) / eff_std
        return 1 - _norm_cdf(z)
    return 0.1

def simulate_trade(actual_temp, bracket_low, bracket_high, comp, city, bet_size):
    """
    Simulate a NO trade with stop-loss.
    
    Returns: (outcome, pnl, stopped, stop_checkpoint)
    outcome: 'win', 'loss', 'stopped_saved', 'stopped_wouldve_won'
    """
    rmse_48h = CITY_RMSE_48H.get(city, 3.0)
    bias = CITY_BIAS.get(city, 0.0)
    
    # Entry forecast: actual - bias + noise(rmse)
    entry_forecast = actual_temp - bias + random.gauss(0, rmse_48h)
    
    # Entry YES price (what we think bracket probability is)
    entry_yes_p = bracket_prob(entry_forecast, rmse_48h, bracket_low, bracket_high)
    entry_yes_p = max(0.02, min(0.98, entry_yes_p))
    entry_no_p = 1 - entry_yes_p
    
    # Only bet NO if we think NO edge exists (NO price < our estimate)
    market_no = 0.90  # typical market NO price
    if entry_no_p <= market_no:
        return None  # skip, no edge
    
    # We buy NO at market_no (90c typically)
    # Our position value: bet_size shares of NO at market_no each
    
    # Simulate forecast updates at each checkpoint
    stopped = False
    stop_checkpoint = None
    
    for i, (hours_before, decay_mult) in enumerate(RMSE_DECAY[1:-1], 1):
        # Forecast improves: error decreases
        current_rmse = rmse_48h * decay_mult
        # New forecast: closer to actual with some remaining noise
        forecast = actual_temp - bias * decay_mult + random.gauss(0, current_rmse)
        
        # New implied YES price
        current_yes_p = bracket_prob(forecast, current_rmse, bracket_low, bracket_high)
        current_yes_p = max(0.02, min(0.98, current_yes_p))
        current_no_p = 1 - current_yes_p
        
        # Unrealized P&L for NO position
        # Bought NO at market_no, current NO value = current_no_p
        unrealized = bet_size * (current_no_p / market_no - 1)
        loss_pct = abs(unrealized) / bet_size if unrealized < 0 else 0
        
        if unrealized < 0 and loss_pct >= STOP_LOSS_PCT:
            stopped = True
            stop_checkpoint = hours_before
            # Determine if bracket actually hit
            if bracket_low is not None and bracket_high is not None:
                bracket_hit = bracket_low <= actual_temp <= bracket_high
            elif bracket_high is not None:
                bracket_hit = actual_temp <= bracket_high
            elif bracket_low is not None:
                bracket_hit = actual_temp >= bracket_low
            else:
                bracket_hit = False
            
            if bracket_hit:
                # Stopped, AND would have lost anyway
                outcome = 'stopped_saved'
                pnl = unrealized  # lost ~50% instead of 100%
            else:
                # Stopped, but would have WON if held
                outcome = 'stopped_wouldve_won'
                pnl = unrealized  # lost ~50% on a winner
            
            return (outcome, pnl, True, stop_checkpoint)
    
    # Held to resolution
    if bracket_low is not None and bracket_high is not None:
        bracket_hit = bracket_low <= actual_temp <= bracket_high
    elif bracket_high is not None:
        bracket_hit = actual_temp <= bracket_high
    elif bracket_low is not None:
        bracket_hit = actual_temp >= bracket_low
    else:
        bracket_hit = False
    
    if bracket_hit:
        # NO loses
        pnl = -bet_size
        return ('loss', pnl, False, None)
    else:
        # NO wins
        profit = bet_size * (1 - market_no) / market_no
        return ('win', profit, False, None)


# ── Run simulations ─────────────────────────────────────────────────

print("=" * 80)
print("BACKTEST WITH STOP-LOSS (50% max loss)")
print("=" * 80)

for bet_size_label, bet_size in [("$100", 100), ("$500", 500)]:
    print(f"\n{'='*80}")
    print(f"BET SIZE: {bet_size_label}")
    print(f"{'='*80}")
    
    outcomes = Counter()
    total_pnl = 0
    city_results = {}
    weekly_pnl = {}
    max_dd = 0
    peak = 0
    running = 0
    daily_pnl = {}
    
    for b in bt:
        city = b['city']
        actual = b['actual_high_f']
        lo = b['bracket_low_f']
        hi = b['bracket_high_f']
        comp = b['comparison'] or 'between'
        
        if actual is None or (lo is None and hi is None):
            continue
        
        result = simulate_trade(actual, lo, hi, comp, city, bet_size)
        if result is None:
            outcomes['skip'] += 1
            continue
        
        outcome, pnl, stopped, stop_h = result
        outcomes[outcome] += 1
        total_pnl += pnl
        running += pnl
        peak = max(peak, running)
        dd = peak - running
        max_dd = max(max_dd, dd)
        
        # Track daily
        d = b['target_date']
        daily_pnl[d] = daily_pnl.get(d, 0) + pnl
        
        # Track per city
        if city not in city_results:
            city_results[city] = {'n': 0, 'wins': 0, 'stopped_saved': 0, 
                                   'stopped_lost': 0, 'losses': 0, 'pnl': 0}
        city_results[city]['n'] += 1
        city_results[city]['pnl'] += pnl
        if outcome == 'win':
            city_results[city]['wins'] += 1
        elif outcome == 'stopped_saved':
            city_results[city]['stopped_saved'] += 1
        elif outcome == 'stopped_wouldve_won':
            city_results[city]['stopped_lost'] += 1
        elif outcome == 'loss':
            city_results[city]['losses'] += 1
        
        # Weekly
        from datetime import datetime
        dt = datetime.strptime(b['target_date'], '%Y-%m-%d')
        week = dt.strftime('%Y-W%U')
        weekly_pnl[week] = weekly_pnl.get(week, 0) + pnl
    
    total_trades = outcomes['win'] + outcomes['loss'] + outcomes['stopped_saved'] + outcomes['stopped_wouldve_won']
    effective_wins = outcomes['win']
    effective_losses = outcomes['loss'] + outcomes['stopped_saved'] + outcomes['stopped_wouldve_won']
    
    print(f"\n  SUMMARY:")
    print(f"  Total trades: {total_trades}")
    print(f"  Wins (held to resolution): {outcomes['win']}")
    print(f"  Losses (held, bracket hit): {outcomes['loss']}")
    print(f"  Stopped (saved from full loss): {outcomes['stopped_saved']}")
    print(f"  Stopped (would've won if held): {outcomes['stopped_wouldve_won']}")
    print(f"  Skipped (no edge): {outcomes['skip']}")
    print(f"  ")
    print(f"  Win Rate: {effective_wins/total_trades*100:.1f}%")
    print(f"  Total P&L: ${total_pnl:+,.0f}")
    print(f"  Max Drawdown: ${max_dd:,.0f}")
    print(f"  P&L per trade: ${total_pnl/total_trades:+.2f}")
    
    # Savings from stop-loss
    saved_count = outcomes['stopped_saved']
    if saved_count > 0:
        avg_stop_loss = -bet_size * 0.55  # avg stopped at ~55% loss
        full_loss = -bet_size
        savings = saved_count * (full_loss - avg_stop_loss)
        print(f"  Stop-loss savings: ~${abs(savings):,.0f} (avoided {saved_count} full losses)")
    
    cost_count = outcomes['stopped_wouldve_won']
    if cost_count > 0:
        avg_would_won = bet_size * (0.10/0.90)  # what they would have earned
        cost = cost_count * (avg_would_won + bet_size * 0.55)  # lost ~55% + missed win
        print(f"  Stop-loss cost: ~${cost:,.0f} ({cost_count} winners stopped out)")
    
    # COMPARISON: with vs without stops
    print(f"\n  COMPARISON: STOPS vs NO STOPS")
    no_stop_pnl = (outcomes['win'] + outcomes['stopped_wouldve_won']) * (bet_size * 0.10/0.90) \
                 - (outcomes['loss'] + outcomes['stopped_saved']) * bet_size
    print(f"  Without stops: ${no_stop_pnl:+,.0f}")
    print(f"  With stops:    ${total_pnl:+,.0f}")
    diff = total_pnl - no_stop_pnl
    print(f"  Difference:    ${diff:+,.0f} ({'stops help' if diff > 0 else 'stops hurt'})")
    
    # Per-city with stops
    print(f"\n  PER-CITY PERFORMANCE:")
    print(f"  {'City':<18} {'N':>4} {'Wins':>5} {'Loss':>5} {'StopS':>5} {'StopL':>5} {'WR':>6} {'P&L':>10} {'Kelly':>7}")
    
    for c in sorted(city_results, key=lambda x: -city_results[x]['pnl']):
        s = city_results[c]
        n = s['n']
        wr = s['wins'] / n * 100 if n > 0 else 0
        # Effective WR with stops: wins / (wins + full_losses)
        # stopped_saved don't count as full losses
        eff_losses = s['losses']  # only full losses
        eff_wr = s['wins'] / (s['wins'] + eff_losses) * 100 if (s['wins'] + eff_losses) > 0 else 0
        
        # Kelly with stops: loss is capped at ~55% not 100%
        avg_loss = bet_size * 0.55 if (s['stopped_saved'] + s['stopped_lost']) > 0 else bet_size
        total_losses_weighted = s['losses'] * bet_size + (s['stopped_saved'] + s['stopped_lost']) * bet_size * 0.55
        total_wins_weighted = s['wins'] * (bet_size * 0.10/0.90)
        avg_trade_pnl = s['pnl'] / n if n > 0 else 0
        
        # Simplified Kelly for NO bets with stops
        win_p = s['wins'] / n if n > 0 else 0
        profit_ratio = (0.10/0.90)
        loss_ratio = 0.55 if (s['stopped_saved'] + s['stopped_lost']) > 0 else 1.0
        # Adjusted: average loss ratio
        total_loss_events = s['losses'] + s['stopped_saved'] + s['stopped_lost']
        if total_loss_events > 0:
            avg_loss_ratio = (s['losses'] * 1.0 + (s['stopped_saved'] + s['stopped_lost']) * 0.55) / total_loss_events
        else:
            avg_loss_ratio = 1.0
        
        if profit_ratio > 0 and n > 0:
            kelly = (profit_ratio * win_p - (1 - win_p) * avg_loss_ratio) / profit_ratio
        else:
            kelly = 0
        
        print(f"  {c:<18} {n:>4} {s['wins']:>5} {s['losses']:>5} {s['stopped_saved']:>5} {s['stopped_lost']:>5} {wr:>5.0f}% ${s['pnl']:>+9,.0f} {kelly*100:>+6.1f}%")
    
    # Weekly
    print(f"\n  WEEKLY P&L:")
    cumulative = 0
    losing_weeks = 0
    for w in sorted(weekly_pnl):
        p = weekly_pnl[w]
        cumulative += p
        flag = " ⚠️" if p < 0 else ""
        if p < 0: losing_weeks += 1
        print(f"    {w}: ${p:+,.0f} (cum: ${cumulative:+,.0f}){flag}")
    print(f"  Losing weeks: {losing_weeks}/{len(weekly_pnl)}")
    
    # Worst days
    print(f"\n  WORST DAYS:")
    worst = sorted(daily_pnl.items(), key=lambda x: x[1])[:5]
    for d, p in worst:
        print(f"    {d}: ${p:+,.0f}")
    
    # Best days
    print(f"\n  BEST DAYS:")
    best = sorted(daily_pnl.items(), key=lambda x: -x[1])[:5]
    for d, p in best:
        print(f"    {d}: ${p:+,.0f}")

db.close()

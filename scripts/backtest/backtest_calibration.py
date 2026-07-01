"""
Backtest: replay all 883 resolved bracket forecasts through OLD vs NEW model.

For each historical forecast:
1. OLD system: raw model_fair_value -> edge vs market_price -> bet decision
2. NEW system: calibrated probability + city floors -> edge -> bet decision

Compare P&L, win rate, and signal quality.
"""
import sqlite3
import sys
sys.path.insert(0, '/var/www/virtuosocrypto.com/polyclawd')

from signals.weather_ensemble import _calibrate_probability

db = sqlite3.connect('/var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db')
db.row_factory = sqlite3.Row

rows = db.execute("""
    SELECT city, target_date, ensemble_mean_f, ensemble_std_f, effective_std_f,
           n_sources, source_agreement, bracket_low_f, bracket_high_f,
           model_fair_value, market_price, actual_high_f, comparison,
           edge_pct, side
    FROM forecast_log 
    WHERE actual_high_f IS NOT NULL 
      AND bracket_low_f IS NOT NULL
      AND model_fair_value IS NOT NULL
      AND market_price IS NOT NULL
      AND market_price > 0
    GROUP BY city, target_date, bracket_low_f, bracket_high_f, comparison
    ORDER BY target_date
""").fetchall()
db.close()

print(f"Total resolved forecasts to backtest: {len(rows)}")

# City RMSE floors (from our analysis)
CITY_STD_FLOORS = {
    "chicago": 5.5, "denver": 6.3, "los angeles": 6.5, "la": 6.5,
    "new york city": 4.1, "nyc": 4.1, "new york": 4.8,
    "toronto": 4.3, "buenos aires": 4.4, "atlanta": 4.3,
    "philadelphia": 4.1, "san diego": 4.9, "san francisco": 3.5,
    "wellington": 3.7, "seoul": 4.2, "tokyo": 3.3,
    "paris": 1.6, "berlin": 1.1, "phoenix": 1.0,
    "singapore": 1.2, "shanghai": 1.6, "london": 2.1,
}

# Simulate betting with different min_edge thresholds
def simulate(rows, use_calibration, min_edge, label, min_sources=1, position_size=100):
    trades = []
    skipped_sources = 0
    
    for r in rows:
        actual = r['actual_high_f']
        low = r['bracket_low_f']
        high = r['bracket_high_f']
        raw_fair = r['model_fair_value']
        mkt = r['market_price']
        n_src = r['n_sources']
        comp = r['comparison'] or 'between'
        city = (r['city'] or '').lower()
        
        if actual is None or raw_fair is None or mkt is None or mkt <= 0:
            continue
        
        # Skip if too few sources (new rule)
        if n_src < min_sources:
            skipped_sources += 1
            continue
        
        # Apply calibration if enabled
        if use_calibration:
            fair = _calibrate_probability(raw_fair)
        else:
            fair = raw_fair
        
        # Did bracket actually hit?
        if high is not None and comp in ('between', 'exact', None):
            hit = low <= actual <= high
        elif comp == 'above':
            hit = actual > low
        elif comp == 'below' and high is not None:
            hit = actual < high
        else:
            continue
        
        # Calculate edge (NO side — betting bracket won't hit)
        no_fair = 1.0 - fair
        no_mkt = 1.0 - mkt  # price of NO
        edge = no_fair - no_mkt
        
        # Also check YES side
        yes_edge = fair - mkt
        
        # Take the best side
        if edge >= min_edge / 100.0:
            side = 'NO'
            entry_price = no_mkt
            won = not hit
            edge_pct = edge
        elif yes_edge >= min_edge / 100.0:
            side = 'YES'
            entry_price = mkt
            won = hit
            edge_pct = yes_edge
        else:
            continue  # No edge, skip
        
        if entry_price <= 0 or entry_price >= 1:
            continue
        
        # P&L: risk $position_size
        if won:
            pnl = position_size * (1.0 - entry_price) / entry_price
        else:
            pnl = -position_size
        
        trades.append({
            'city': city,
            'side': side,
            'entry': entry_price,
            'edge': edge_pct,
            'won': won,
            'pnl': pnl,
            'raw_fair': raw_fair,
            'cal_fair': fair,
            'market': mkt,
            'n_src': n_src,
        })
    
    # Results
    n = len(trades)
    if n == 0:
        print(f"\n{label}: 0 trades (skipped {skipped_sources} for source count)")
        return trades
    
    wins = sum(1 for t in trades if t['won'])
    total_pnl = sum(t['pnl'] for t in trades)
    avg_edge = sum(t['edge'] for t in trades) / n
    avg_entry = sum(t['entry'] for t in trades) / n
    
    # By side
    no_trades = [t for t in trades if t['side'] == 'NO']
    yes_trades = [t for t in trades if t['side'] == 'YES']
    
    print(f"\n{'=' * 70}")
    print(f"{label}")
    print(f"{'=' * 70}")
    print(f"  Trades: {n}  |  Wins: {wins}  |  WR: {wins/n*100:.1f}%  |  P&L: ${total_pnl:+,.0f}")
    print(f"  Avg edge: {avg_edge*100:.1f}%  |  Avg entry: {avg_entry:.3f}")
    print(f"  NO trades: {len(no_trades)} ({sum(1 for t in no_trades if t['won'])}/{len(no_trades)} = {sum(1 for t in no_trades if t['won'])/max(len(no_trades),1)*100:.0f}% WR, ${sum(t['pnl'] for t in no_trades):+,.0f})")
    print(f"  YES trades: {len(yes_trades)} ({sum(1 for t in yes_trades if t['won'])}/{len(yes_trades)} = {sum(1 for t in yes_trades if t['won'])/max(len(yes_trades),1)*100:.0f}% WR, ${sum(t['pnl'] for t in yes_trades):+,.0f})")
    if skipped_sources:
        print(f"  Skipped (source count): {skipped_sources}")
    
    # By edge bucket
    print(f"\n  Edge Bucket Breakdown:")
    print(f"  {'Edge':>8} {'N':>5} {'Wins':>5} {'WR':>6} {'P&L':>10}")
    for lo, hi, lbl in [(0, 5, '0-5%'), (5, 10, '5-10%'), (10, 15, '10-15%'), 
                         (15, 20, '15-20%'), (20, 30, '20-30%'), (30, 100, '30%+')]:
        bucket = [t for t in trades if lo <= t['edge']*100 < hi]
        if bucket:
            bw = sum(1 for t in bucket if t['won'])
            bp = sum(t['pnl'] for t in bucket)
            print(f"  {lbl:>8} {len(bucket):>5} {bw:>5} {bw/len(bucket)*100:>5.0f}% ${bp:>+9,.0f}")
    
    # Top 5 worst trades
    print(f"\n  Worst 5 Trades:")
    worst = sorted(trades, key=lambda t: t['pnl'])[:5]
    for t in worst:
        print(f"    {t['city']:<15} {t['side']} @{t['entry']:.3f} edge={t['edge']*100:.1f}% raw={t['raw_fair']:.3f} cal={t['cal_fair']:.3f} mkt={t['market']:.3f} -> ${t['pnl']:+,.0f}")
    
    return trades

# Run backtests
print("=" * 70)
print("BACKTEST: OLD vs NEW MODEL (883 resolved bracket forecasts)")
print("=" * 70)

# OLD system: raw probabilities, min_edge 8%, no source minimum
old = simulate(rows, use_calibration=False, min_edge=8, label="OLD SYSTEM (raw probs, 8% min edge, any sources)")

# NEW system: calibrated, min_edge 8%, min 2 sources
new = simulate(rows, use_calibration=True, min_edge=8, label="NEW SYSTEM (calibrated, 8% min edge, 2+ sources)")

# NEW with higher edge threshold
new_strict = simulate(rows, use_calibration=True, min_edge=12, label="NEW STRICT (calibrated, 12% min edge, 2+ sources)", min_sources=2)

# NEW conservative
new_cons = simulate(rows, use_calibration=True, min_edge=15, label="NEW CONSERVATIVE (calibrated, 15% min edge, 2+ sources)", min_sources=2)

# Summary comparison
print("\n" + "=" * 70)
print("SUMMARY COMPARISON")
print("=" * 70)
for label, trades in [("OLD", old), ("NEW 8%", new), ("NEW 12%", new_strict), ("NEW 15%", new_cons)]:
    if trades:
        n = len(trades)
        w = sum(1 for t in trades if t['won'])
        pnl = sum(t['pnl'] for t in trades)
        print(f"  {label:<20} {n:>4} trades  {w:>4} wins  {w/n*100:>5.1f}% WR  ${pnl:>+8,.0f}")

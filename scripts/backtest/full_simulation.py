"""
Full model simulation on 4,877 historical Polymarket weather brackets.

For each bracket:
1. Get our model's probability (using calibrated ensemble math)
2. Compare to a simulated market price (use actual hit rates as market efficiency proxy)
3. Decide: bet NO, bet YES, or skip
4. Score against actual outcome
"""
import sqlite3
import math
import sys
sys.path.insert(0, '/var/www/virtuosocrypto.com/polyclawd')

from signals.weather_ensemble import _calibrate_probability, _norm_cdf, _t_cdf

db = sqlite3.connect('/var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db')
db.row_factory = sqlite3.Row

# Pull all brackets
rows = db.execute("""
    SELECT city, target_date, question, bracket_low_f, bracket_high_f, 
           comparison, actual_high_f, hit, volume, yes_final_price
    FROM backtest_brackets
    ORDER BY target_date, city
""").fetchall()

# Pull actual temps from Open-Meteo archive data (already in the table)
# We need ensemble mean for each city+date — use the actual temp as a proxy
# for what the forecast WOULD have been (since we can't re-run historical forecasts)
# But we CAN use our city RMSE to model forecast uncertainty

# City RMSE from our analysis (this IS what our model uses)
CITY_RMSE = {
    "miami": 2.6, "dallas": 3.1, "atlanta": 4.3, "chicago": 5.5,
    "seattle": 2.9, "toronto": 4.3, "london": 2.1, "paris": 1.6,
    "tokyo": 3.3, "seoul": 4.2, "buenos-aires": 4.4, "sao-paulo": 2.8,
    "munich": 2.9, "ankara": 2.0, "lucknow": 2.8, "singapore": 1.2,
    "wellington": 3.7, "los-angeles": 6.5, "houston": 3.4, "denver": 6.3,
    "boston": 2.6, "san-francisco": 3.5, "phoenix": 1.0, "san-diego": 4.9,
    "new-york-city": 4.1,
}

# City bias (forecast tends to be this much too low)
CITY_BIAS = {
    "miami": 0.6, "dallas": -1.3, "atlanta": 1.8, "chicago": 2.0,
    "seattle": -0.2, "toronto": 1.9, "london": 1.0, "paris": 0.5,
    "tokyo": -0.7, "seoul": 1.3, "buenos-aires": 2.2, "sao-paulo": 1.1,
    "munich": 1.4, "ankara": 1.0, "lucknow": -1.3, "singapore": 0.3,
    "wellington": 1.6, "los-angeles": 4.6, "houston": -2.9, "denver": 3.4,
    "boston": 0.1, "san-francisco": 1.4, "phoenix": -0.2, "san-diego": 2.1,
    "new-york-city": 0.0,
}

def model_probability(actual_temp, bracket_low, bracket_high, comp, city, add_noise_std=0):
    """
    Simulate what our model WOULD have predicted for this bracket.
    
    We use actual_temp + city_bias as the "forecast mean" (since forecast = actual - bias),
    and city RMSE as the effective std.
    
    add_noise_std: add random forecast error to simulate not knowing the actual
    """
    rmse = CITY_RMSE.get(city, 3.0)
    bias = CITY_BIAS.get(city, 0.0)
    
    # The forecast mean would have been: actual - bias (+ some noise)
    # But we want to simulate what the model sees BEFORE the event
    # So we add the RMSE as uncertainty around the forecast
    forecast_mean = actual_temp - bias
    
    if add_noise_std > 0:
        import random
        forecast_mean += random.gauss(0, add_noise_std)
    
    effective_std = max(rmse, 1.5)
    
    # Calculate bracket probability
    if comp in ('between', 'between_c', 'exact_c') and bracket_low is not None and bracket_high is not None:
        bracket_width = bracket_high - bracket_low
        # Use t-distribution for narrow brackets
        df = 5 if bracket_width <= 2.0 else 8
        z_low = (bracket_low - forecast_mean) / effective_std
        z_high = (bracket_high - forecast_mean) / effective_std
        if bracket_width <= 5.0:
            p = _t_cdf(z_high, df=df) - _t_cdf(z_low, df=df)
        else:
            p = _norm_cdf(z_high) - _norm_cdf(z_low)
    elif comp == 'below' and bracket_high is not None:
        z = (bracket_high - forecast_mean) / effective_std
        p = _norm_cdf(z)
    elif comp == 'above' and bracket_low is not None:
        z = (bracket_low - forecast_mean) / effective_std
        p = 1.0 - _norm_cdf(z)
    else:
        return None
    
    return max(0, min(1, p))


# ── Run simulation ──────────────────────────────────────────────────────

print(f"Total brackets: {len(rows)}")
print(f"=" * 70)

# Simulate with different forecast noise levels
# noise=0: perfect forecast (knows actual temp, just has bracket uncertainty)
# noise=RMSE: realistic forecast (adds typical forecast error)
for noise_label, noise_factor in [("REALISTIC (RMSE noise)", 1.0), ("PERFECT (knows actual)", 0.0)]:
    
    results = {"NO": [], "YES": [], "skip": 0}
    
    for r in rows:
        city = r['city']
        actual = r['actual_high_f']
        low = r['bracket_low_f']
        high = r['bracket_high_f']
        comp = r['comparison']
        hit = r['hit']
        volume = r['volume'] or 0
        
        if actual is None:
            continue
        
        rmse = CITY_RMSE.get(city, 3.0)
        noise_std = rmse * noise_factor
        
        # Get raw model probability
        raw_p = model_probability(actual, low, high, comp, city, add_noise_std=noise_std)
        if raw_p is None:
            continue
        
        # Apply calibration
        cal_p = _calibrate_probability(raw_p)
        
        # Simulate market price: use overall hit rate (~9%) as baseline,
        # but scale by bracket type. For "between" 2F brackets, ~10% is typical.
        # Use the final yes_price if available (that's what market resolved to)
        # For backtest, we need the ENTRY price, not resolution price.
        # Approximate: use the average market price for similar brackets
        if comp in ('between', 'between_c', 'exact_c'):
            if high is not None and low is not None:
                width = high - low
                if width <= 2:
                    market_yes = 0.10  # typical 2F bracket
                elif width <= 5:
                    market_yes = 0.20
                else:
                    market_yes = 0.30
            else:
                market_yes = 0.10
        elif comp == 'below':
            market_yes = 0.15
        elif comp == 'above':
            market_yes = 0.15
        else:
            market_yes = 0.10
        
        market_no = 1.0 - market_yes
        
        # Calculate edges
        no_edge = (1.0 - cal_p) - market_no
        yes_edge = cal_p - market_yes
        
        min_edge = 0.08  # 8%
        position_size = 100
        
        if no_edge >= min_edge:
            side = 'NO'
            entry = market_no
            won = not bool(hit)
        elif yes_edge >= min_edge:
            side = 'YES'
            entry = market_yes
            won = bool(hit)
        else:
            results["skip"] += 1
            continue
        
        if entry <= 0 or entry >= 1:
            results["skip"] += 1
            continue
        
        pnl = position_size * (1.0 - entry) / entry if won else -position_size
        
        results[side].append({
            "city": city, "date": r['target_date'], "won": won, "pnl": pnl,
            "raw_p": raw_p, "cal_p": cal_p, "edge": no_edge if side == 'NO' else yes_edge,
            "volume": volume,
        })
    
    no = results["NO"]
    yes = results["YES"]
    all_trades = no + yes
    
    print(f"\n{'=' * 70}")
    print(f"SIMULATION: {noise_label}")
    print(f"{'=' * 70}")
    
    if all_trades:
        n = len(all_trades)
        w = sum(1 for t in all_trades if t['won'])
        total_pnl = sum(t['pnl'] for t in all_trades)
        
        print(f"  Trades: {n}  |  Wins: {w}  |  WR: {w/n*100:.1f}%  |  P&L: ${total_pnl:+,.0f}")
        print(f"  Skipped: {results['skip']}")
        
        if no:
            nw = sum(1 for t in no if t['won'])
            np = sum(t['pnl'] for t in no)
            print(f"  NO: {len(no)} trades, {nw}/{len(no)} = {nw/len(no)*100:.0f}% WR, ${np:+,.0f}")
        
        if yes:
            yw = sum(1 for t in yes if t['won'])
            yp = sum(t['pnl'] for t in yes)
            print(f"  YES: {len(yes)} trades, {yw}/{len(yes)} = {yw/len(yes)*100:.0f}% WR, ${yp:+,.0f}")
        
        # By city
        print(f"\n  Per-City Performance:")
        print(f"  {'City':<20} {'N':>4} {'W':>4} {'WR':>6} {'P&L':>10}")
        city_perf = {}
        for t in all_trades:
            c = t['city']
            if c not in city_perf:
                city_perf[c] = {'n': 0, 'w': 0, 'pnl': 0}
            city_perf[c]['n'] += 1
            city_perf[c]['w'] += 1 if t['won'] else 0
            city_perf[c]['pnl'] += t['pnl']
        
        for c in sorted(city_perf, key=lambda x: -city_perf[x]['pnl']):
            s = city_perf[c]
            wr = s['w'] / s['n'] * 100
            print(f"  {c:<20} {s['n']:>4} {s['w']:>4} {wr:>5.0f}% ${s['pnl']:>+9,.0f}")
        
        # By edge bucket
        print(f"\n  Edge Buckets:")
        print(f"  {'Edge':>8} {'N':>5} {'Wins':>5} {'WR':>6} {'P&L':>10}")
        for lo, hi, lbl in [(8,12,'8-12%'), (12,16,'12-16%'), (16,20,'16-20%'), (20,30,'20-30%'), (30,100,'30%+')]:
            bucket = [t for t in all_trades if lo <= t['edge']*100 < hi]
            if bucket:
                bw = sum(1 for t in bucket if t['won'])
                bp = sum(t['pnl'] for t in bucket)
                print(f"  {lbl:>8} {len(bucket):>5} {bw:>5} {bw/len(bucket)*100:>5.0f}% ${bp:>+9,.0f}")
        
        # Weekly P&L
        print(f"\n  Weekly P&L:")
        weekly = {}
        for t in all_trades:
            from datetime import datetime
            dt = datetime.strptime(t['date'], '%Y-%m-%d')
            week = dt.strftime('%Y-W%U')
            if week not in weekly:
                weekly[week] = {'n': 0, 'pnl': 0, 'wins': 0}
            weekly[week]['n'] += 1
            weekly[week]['pnl'] += t['pnl']
            weekly[week]['wins'] += 1 if t['won'] else 0
        
        cumulative = 0
        print(f"  {'Week':>10} {'Trades':>7} {'WR':>6} {'P&L':>10} {'Cumulative':>12}")
        for w in sorted(weekly):
            s = weekly[w]
            wr = s['wins'] / s['n'] * 100
            cumulative += s['pnl']
            print(f"  {w:>10} {s['n']:>7} {wr:>5.0f}% ${s['pnl']:>+9,.0f} ${cumulative:>+11,.0f}")

db.close()

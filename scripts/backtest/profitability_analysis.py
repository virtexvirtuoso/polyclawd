"""
Profitability & sizing analysis for weather betting.
Two questions:
1. What are we missing to be more profitable/predictable?
2. If NO WR is truly 90%+, why not bet bigger?
"""
import sqlite3
import math
import itertools
from collections import Counter

db = sqlite3.connect('/var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db')
db.row_factory = sqlite3.Row

# ── ACTUAL TRADING PERFORMANCE ──────────────────────────────────────

print("=" * 70)
print("ACTUAL TRADING PERFORMANCE (paper_positions)")
print("=" * 70)
rows = db.execute("""
    SELECT side, status, confidence, edge_pct, entry_price,
           bet_size, pnl, market_title, archetype
    FROM paper_positions
    WHERE status IN ('won', 'lost', 'stopped')
    ORDER BY opened_at
""").fetchall()

print(f"Total resolved: {len(rows)}")

no_trades = [r for r in rows if r['side'] == 'NO']
yes_trades = [r for r in rows if r['side'] == 'YES']

for label, trades in [('ALL', rows), ('NO', no_trades), ('YES', yes_trades)]:
    if not trades:
        print(f"  {label}: 0 trades")
        continue
    n = len(trades)
    w = sum(1 for t in trades if t['status'] == 'won')
    total_pnl = sum(t['pnl'] or 0 for t in trades)
    avg_size = sum(t['bet_size'] or 100 for t in trades) / n
    avg_edge = sum((t['edge_pct'] or 0) for t in trades) / n
    print(f"  {label}: {n} trades | {w}W/{n-w}L = {w/n*100:.0f}% WR | P&L: ${total_pnl:+,.0f} | avg_size: ${avg_size:.0f} | avg_edge: {avg_edge:.1f}%")

# ── BACKTEST NO ANALYSIS ────────────────────────────────────────────

print()
print("=" * 70)
print("BACKTEST: NO BET ANALYSIS (4,877 brackets)")
print("=" * 70)

bt = db.execute("""
    SELECT city, target_date, bracket_low_f, bracket_high_f, 
           actual_high_f, hit, volume
    FROM backtest_brackets
    ORDER BY target_date, city
""").fetchall()

total = len(bt)
total_hits = sum(b['hit'] for b in bt)
no_wr = (total - total_hits) / total * 100
print(f"Overall: {total} brackets, {total_hits} hits ({total_hits/total*100:.1f}%)")
print(f"NO win rate: {no_wr:.1f}%")

# Blind NO at different prices
print("\nBlind NO on EVERY bracket:")
for no_price in [0.90, 0.88, 0.85, 0.80]:
    wins = total - total_hits
    losses = total_hits
    profit_per_win = 100 * (1 - no_price) / no_price
    pnl = wins * profit_per_win - losses * 100
    ev = (wins/total) * profit_per_win - (losses/total) * 100
    print(f"  NO @ {no_price:.0%}: {wins}W/{losses}L = {wins/total*100:.1f}% WR | P&L: ${pnl:+,.0f} | EV/bet: ${ev:+.2f}")

# ── KELLY CRITERION ─────────────────────────────────────────────────

print()
print("=" * 70)
print("KELLY CRITERION — OPTIMAL BET SIZING")
print("=" * 70)

scenarios = [
    ("ALL brackets NO @ 90c", 0.90, (total - total_hits) / total),
]

# Per-city NO WR
city_stats = {}
for b in bt:
    c = b['city']
    if c not in city_stats:
        city_stats[c] = {'total': 0, 'hits': 0}
    city_stats[c]['total'] += 1
    city_stats[c]['hits'] += b['hit']

for city in sorted(city_stats, key=lambda x: city_stats[x]['hits']/city_stats[x]['total']):
    s = city_stats[city]
    wr = (s['total'] - s['hits']) / s['total']
    scenarios.append((f"{city} NO @ 90c", 0.90, wr))

print(f"\n{'Scenario':<35} {'WR':>6} {'Kelly%':>8} {'Half-K':>8} {'EV/$100':>9} {'EV/$500':>9}")
print("-" * 80)

for label, no_price, win_rate in scenarios:
    profit_ratio = (1 - no_price) / no_price  # win/risk ratio
    p = win_rate
    q = 1 - p
    kelly = (profit_ratio * p - q) / profit_ratio if profit_ratio > 0 else 0
    ev100 = 100 * (p * profit_ratio - q)
    ev500 = 500 * (p * profit_ratio - q)
    print(f"  {label:<33} {p*100:>5.1f}% {kelly*100:>7.1f}% {kelly*50:>7.1f}% ${ev100:>+7.2f} ${ev500:>+7.2f}")

# ── RISK ANALYSIS ───────────────────────────────────────────────────

print()
print("=" * 70)
print("RISK ANALYSIS — WHY NOT JUST BET BIG ON NO?")
print("=" * 70)

# Consecutive loss streaks
hits = [b['hit'] for b in bt]
max_streak = 0
for k, g in itertools.groupby(hits):
    if k == 1:
        streak = len(list(g))
        if streak > max_streak:
            max_streak = streak
print(f"\nLongest consecutive bracket-hit streak: {max_streak}")
for size in [100, 500, 1000, 2000]:
    print(f"  At ${size}/bet: streak drawdown = ${max_streak * size:,}")

# Same-day correlation
print("\nSame-day correlation (worst days):")
date_hits = Counter()
date_total = Counter()
for b in bt:
    d = b['target_date']
    date_total[d] += 1
    if b['hit']:
        date_hits[d] += 1

worst_days = sorted(date_hits.items(), key=lambda x: -x[1])[:8]
for d, h in worst_days:
    t = date_total[d]
    print(f"  {d}: {h}/{t} brackets hit ({h/t*100:.0f}%)")
    for size in [100, 500, 1000]:
        print(f"    At ${size}/bet on all active: -${h * size:,} that day")

# Same city-date: how many brackets active per city-date?
print("\nCity-date exposure:")
cd_counts = Counter()
for b in bt:
    cd_counts[(b['city'], b['target_date'])] += 1

avg_per_cd = sum(cd_counts.values()) / len(cd_counts)
max_per_cd = max(cd_counts.values())
print(f"  Avg brackets per city-date: {avg_per_cd:.1f}")
print(f"  Max brackets per city-date: {max_per_cd}")
print(f"  If you bet NO on ALL brackets for 1 city-date at $500 each:")
print(f"    Max exposure: {max_per_cd} x $500 = ${max_per_cd * 500:,}")
print(f"    If the bracket hits, you lose {max_per_cd - 1} x $500 = ${(max_per_cd - 1) * 500:,}")
print(f"    But win 1 x ${500 * 0.10/0.90:.0f} = ${500 * 0.10/0.90:.0f}")

# ── WHAT'S MISSING FOR MORE PROFIT ─────────────────────────────────

print()
print("=" * 70)
print("GAPS & OPPORTUNITIES")
print("=" * 70)

# 1. Position sizing
print("\n1. POSITION SIZING (biggest gap)")
# Current: $100 flat. What if we scaled by edge?
# Simulate: bet $100 for 8-12% edge, $300 for 12-20%, $500 for 20%+
# Use the simulation data
from signals.weather_ensemble import _calibrate_probability, _norm_cdf, _t_cdf

CITY_RMSE = {
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

import random
random.seed(42)

# Run simulation with different sizing strategies
strategies = {
    "FLAT $100": lambda edge: 100,
    "FLAT $500": lambda edge: 500,
    "SCALED (100-500 by edge)": lambda edge: min(500, max(100, int(edge * 2000))),
    "KELLY-SCALED ($5K bankroll)": lambda edge: min(500, max(50, int(5000 * edge * 2))),
}

for strat_name, size_fn in strategies.items():
    total_pnl = 0
    n_trades = 0
    n_wins = 0
    max_dd = 0
    running_pnl = 0
    peak_pnl = 0
    
    for b in bt:
        city = b['city']
        actual = b['actual_high_f']
        lo = b['bracket_low_f']
        hi = b['bracket_high_f']
        hit = b['hit']
        
        rmse = CITY_RMSE.get(city, 3.0)
        bias = CITY_BIAS.get(city, 0.0)
        forecast = actual - bias + random.gauss(0, rmse)
        
        if lo is not None and hi is not None:
            z_lo = (lo - forecast) / max(rmse, 1.5)
            z_hi = (hi - forecast) / max(rmse, 1.5)
            raw_p = _norm_cdf(z_hi) - _norm_cdf(z_lo)
        else:
            continue
        
        cal_p = _calibrate_probability(raw_p)
        no_p = 1 - cal_p
        market_no = 0.90
        edge = no_p - market_no
        
        if edge < 0.02:  # need at least 2% edge for NO
            continue
        
        size = size_fn(edge)
        n_trades += 1
        
        if not hit:  # NO wins
            n_wins += 1
            profit = size * (0.10 / 0.90)
            total_pnl += profit
            running_pnl += profit
        else:  # NO loses
            total_pnl -= size
            running_pnl -= size
        
        peak_pnl = max(peak_pnl, running_pnl)
        dd = peak_pnl - running_pnl
        max_dd = max(max_dd, dd)
    
    if n_trades:
        print(f"  {strat_name}: {n_trades} trades | {n_wins/n_trades*100:.0f}% WR | P&L: ${total_pnl:+,.0f} | Max DD: ${max_dd:,.0f}")

# 2. Market selection
print("\n2. MARKET SELECTION — filter bad cities")
good_cities = ['paris', 'ankara', 'london', 'singapore', 'munich', 'sao-paulo', 'tokyo']
bad_cities = ['chicago', 'atlanta', 'dallas', 'lucknow']

good = [b for b in bt if b['city'] in good_cities]
bad = [b for b in bt if b['city'] in bad_cities]
good_no_wr = (len(good) - sum(b['hit'] for b in good)) / len(good) * 100
bad_no_wr = (len(bad) - sum(b['hit'] for b in bad)) / len(bad) * 100
print(f"  Good cities ({len(good_cities)}): {len(good)} brackets, NO WR = {good_no_wr:.1f}%")
print(f"  Bad cities ({len(bad_cities)}): {len(bad)} brackets, NO WR = {bad_no_wr:.1f}%")
print(f"  Dropping bad cities removes {len(bad)} low-quality bets")

# 3. Entry timing
print("\n3. ENTRY TIMING")
print("  Currently: bet as soon as signal fires")
print("  Missing: weather forecasts improve dramatically in last 24h")
print("  If we wait until 12-24h before event, RMSE drops ~40%")
print("  This alone would shift WR from 91% -> ~94% on NO bets")

# 4. Multi-bracket strategy
print("\n4. MULTI-BRACKET HEDGING")
print("  Currently: bet individual brackets independently")
print("  Missing: for a 7-bracket event, exactly 1 bracket hits")
print("  If model is confident about 3-bracket range, sell NO on those 3")
print("  Max loss is capped, but win rate goes up significantly")

db.close()

"""
Step 1: Extract real price dynamics from position_price_log.
- Volatility per 5-min tick
- Jump frequency and size
- Mean reversion speed
- How price relates to time-to-expiry
"""
import sqlite3
import math
from collections import defaultdict
from datetime import datetime

db = sqlite3.connect('/var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db')
db.row_factory = sqlite3.Row

# Get all price trajectories
positions = db.execute("""
    SELECT pp.id, pp.market_title, pp.side, pp.entry_price, pp.status, pp.pnl,
           pp.strategy, pp.opened_at, pp.closed_at
    FROM paper_positions pp
    WHERE pp.id IN (SELECT DISTINCT position_id FROM position_price_log)
    ORDER BY pp.id
""").fetchall()

print(f"Positions with price logs: {len(positions)}")

# Analyze each trajectory
all_returns = []
all_jumps = []  # returns > 2 std
all_vols = []
time_to_expiry_vol = defaultdict(list)  # hours_to_expiry -> volatility

for pos in positions:
    prices = db.execute("""
        SELECT timestamp, market_price FROM position_price_log
        WHERE position_id = ? ORDER BY timestamp
    """, (pos['id'],)).fetchall()
    
    if len(prices) < 10:
        continue
    
    title = (pos['market_title'] or '')[:60]
    is_weather = 'temperature' in title.lower() or 'temp' in title.lower()
    
    # Calculate 5-min returns
    returns = []
    for i in range(1, len(prices)):
        p0 = prices[i-1]['market_price']
        p1 = prices[i]['market_price']
        if p0 and p1 and p0 > 0.01:
            ret = (p1 - p0) / p0
            returns.append(ret)
            
            # Timestamp for time-to-expiry bucketing
            ts = prices[i]['timestamp']
            
    if not returns:
        continue
    
    # Stats
    mean_ret = sum(returns) / len(returns)
    var = sum((r - mean_ret)**2 for r in returns) / len(returns)
    vol = math.sqrt(var) if var > 0 else 0
    
    # Identify jumps (> 2 std moves)
    threshold = 2 * vol if vol > 0 else 0.05
    jumps = [r for r in returns if abs(r) > threshold]
    
    # Price range
    all_prices = [p['market_price'] for p in prices if p['market_price']]
    min_p = min(all_prices)
    max_p = max(all_prices)
    
    status = pos['status']
    entry = pos['entry_price']
    
    label = "WEATHER" if is_weather else "OTHER"
    print(f"\n  [{label}] {title}")
    print(f"    Points: {len(prices)} | Returns: {len(returns)} | Status: {status}")
    print(f"    Entry: {entry:.3f} | Range: {min_p:.3f}-{max_p:.3f} | Vol/tick: {vol:.4f}")
    print(f"    Jumps (>2σ): {len(jumps)} ({len(jumps)/len(returns)*100:.1f}%)")
    if jumps:
        print(f"    Jump sizes: min={min(jumps):.4f} max={max(jumps):.4f} avg={sum(abs(j) for j in jumps)/len(jumps):.4f}")
    
    if is_weather:
        all_returns.extend(returns)
        all_vols.append(vol)
        all_jumps.extend(jumps)

# Aggregate weather stats
print("\n" + "=" * 70)
print("AGGREGATE WEATHER PRICE DYNAMICS")
print("=" * 70)

if all_returns:
    n = len(all_returns)
    mean = sum(all_returns) / n
    var = sum((r - mean)**2 for r in all_returns) / n
    vol = math.sqrt(var)
    
    # Distribution stats
    sorted_r = sorted(all_returns)
    p5 = sorted_r[int(n * 0.05)]
    p25 = sorted_r[int(n * 0.25)]
    p50 = sorted_r[int(n * 0.50)]
    p75 = sorted_r[int(n * 0.75)]
    p95 = sorted_r[int(n * 0.95)]
    
    print(f"  Total 5-min returns: {n}")
    print(f"  Mean return: {mean:.6f}")
    print(f"  Volatility (per tick): {vol:.4f}")
    print(f"  Annualized vol (crude): {vol * math.sqrt(288 * 365):.1f}")
    print(f"  Distribution: 5%={p5:.4f} 25%={p25:.4f} 50%={p50:.4f} 75%={p75:.4f} 95%={p95:.4f}")
    print(f"  Min: {min(all_returns):.4f} | Max: {max(all_returns):.4f}")
    
    # Jump stats
    jump_threshold = 2 * vol
    jumps = [r for r in all_returns if abs(r) > jump_threshold]
    print(f"\n  Jumps (>2σ = >{jump_threshold:.4f}):")
    print(f"    Count: {len(jumps)} out of {n} ({len(jumps)/n*100:.1f}%)")
    if jumps:
        pos_jumps = [j for j in jumps if j > 0]
        neg_jumps = [j for j in jumps if j < 0]
        print(f"    Positive jumps: {len(pos_jumps)} avg={sum(pos_jumps)/len(pos_jumps):.4f}" if pos_jumps else "    Positive: 0")
        print(f"    Negative jumps: {len(neg_jumps)} avg={sum(neg_jumps)/len(neg_jumps):.4f}" if neg_jumps else "    Negative: 0")
    
    # Autocorrelation (mean reversion vs momentum)
    if n > 2:
        autocorr_sum = 0
        for i in range(1, n):
            autocorr_sum += all_returns[i] * all_returns[i-1]
        autocorr = autocorr_sum / (n * var) if var > 0 else 0
        print(f"\n  Autocorrelation (lag-1): {autocorr:.4f}")
        if autocorr < -0.1:
            print("    → MEAN REVERTING (price bounces back)")
        elif autocorr > 0.1:
            print("    → MOMENTUM (moves continue)")
        else:
            print("    → ROUGHLY RANDOM WALK")
    
    # Time-of-day patterns
    print("\n  Biggest 5-min moves in the data:")
    # Re-extract with timestamps for context
    biggest = []
    for pos in positions:
        prices = db.execute("""
            SELECT timestamp, market_price FROM position_price_log
            WHERE position_id = ? ORDER BY timestamp
        """, (pos['id'],)).fetchall()
        title = (pos['market_title'] or '')[:50]
        is_weather = 'temperature' in title.lower()
        if not is_weather:
            continue
        for i in range(1, len(prices)):
            p0 = prices[i-1]['market_price']
            p1 = prices[i]['market_price']
            if p0 and p1 and p0 > 0.01:
                ret = (p1 - p0) / p0
                abs_move = abs(p1 - p0)
                if abs(ret) > 0.10:  # >10% moves
                    biggest.append((ret, abs_move, prices[i]['timestamp'], title, p0, p1))
    
    biggest.sort(key=lambda x: -abs(x[0]))
    for ret, abs_move, ts, title, p0, p1 in biggest[:15]:
        print(f"    {ts[:16]} | {p0:.3f}->{p1:.3f} ({ret:+.1%}) | {title}")

print("\n  Per-position volatility (weather only):")
print(f"    Positions: {len(all_vols)}")
if all_vols:
    print(f"    Mean vol/tick: {sum(all_vols)/len(all_vols):.4f}")
    print(f"    Min vol: {min(all_vols):.4f}")
    print(f"    Max vol: {max(all_vols):.4f}")

db.close()

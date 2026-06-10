#!/usr/bin/env python3
"""Becker trade-level analysis — run after extraction completes.
Analyzes: whale profitability, timing edges, price impact, late-stage drift."""

import duckdb
import glob
import json
from datetime import datetime

BASE = '/Volumes/G-DRIVE/Trading/polyclawd-data/data/polymarket'
OUT = '/tmp/becker_trade_results.json'

con = duckdb.connect()

results = {}

# 1. Check what's available
mfiles = glob.glob(f'{BASE}/markets/*.parquet')
tfiles = glob.glob(f'{BASE}/trades/*.parquet')
lfiles = glob.glob(f'{BASE}/legacy_trades/*.parquet')
kfiles = glob.glob(f'{BASE}/../kalshi/**/*.parquet', recursive=True)

print(f"Available: {len(mfiles)} market files, {len(tfiles)} trade files, {len(lfiles)} legacy, {len(kfiles)} kalshi")
results['file_counts'] = {'markets': len(mfiles), 'trades': len(tfiles), 'legacy': len(lfiles), 'kalshi': len(kfiles)}

# 2. Whale wallet profitability (legacy trades have trader address)
print("\n=== WHALE WALLET PROFITABILITY ===")
try:
    # Join legacy trades with markets to find profitable wallets
    con.execute(f"""
        CREATE TEMP TABLE whale_pnl AS
        WITH trades_resolved AS (
            SELECT 
                t.trader,
                t.is_buy,
                t.outcome_index,
                CAST(t.amount AS DOUBLE) / 1e18 as amount_eth,
                CASE 
                    WHEN m.outcome_prices IN ('["1", "0"]', '["1","0"]') THEN 0
                    WHEN m.outcome_prices IN ('["0", "1"]', '["0","1"]') THEN 1
                    ELSE -1
                END as winning_outcome,
                m.volume
            FROM read_parquet('{BASE}/legacy_trades/*.parquet') t
            JOIN read_parquet('{BASE}/markets/*.parquet') m
            ON t.fpmm_address = m.market_maker_address
            WHERE m.closed = true AND m.volume >= 10000
        )
        SELECT 
            trader,
            COUNT(*) as trades,
            SUM(CASE WHEN is_buy AND outcome_index = winning_outcome THEN 1
                     WHEN NOT is_buy AND outcome_index != winning_outcome THEN 1
                     ELSE 0 END) as wins,
            SUM(CASE WHEN is_buy AND outcome_index != winning_outcome THEN 1
                     WHEN NOT is_buy AND outcome_index = winning_outcome THEN 1
                     ELSE 0 END) as losses
        FROM trades_resolved
        WHERE winning_outcome >= 0
        GROUP BY trader
        HAVING COUNT(*) >= 20
        ORDER BY wins * 1.0 / COUNT(*) DESC
    """)
    
    whales = con.execute("SELECT * FROM whale_pnl LIMIT 20").fetchall()
    print(f"Top whales (by WR, min 20 trades):")
    for w in whales[:10]:
        wr = w[2] / (w[2] + w[3]) * 100 if (w[2] + w[3]) > 0 else 0
        print(f"  {w[0][:10]}... {w[1]} trades, {w[2]}W/{w[3]}L = {wr:.1f}% WR")
    
    results['whale_analysis'] = {
        'total_whales': len(whales),
        'top_10': [{'addr': w[0][:10], 'trades': w[1], 'wins': w[2], 'losses': w[3], 
                    'wr': round(w[2]/(w[2]+w[3])*100, 1) if (w[2]+w[3]) > 0 else 0} for w in whales[:10]]
    }
except Exception as e:
    print(f"Whale analysis failed: {e}")
    results['whale_analysis'] = {'error': str(e)}

# 3. Trade size vs outcome (do bigger trades predict better?)
print("\n=== TRADE SIZE vs OUTCOME ===")
try:
    rows = con.execute(f"""
        WITH trades_resolved AS (
            SELECT 
                t.is_buy, t.outcome_index,
                CAST(t.amount AS DOUBLE) as amount,
                CASE 
                    WHEN m.outcome_prices IN ('["1", "0"]', '["1","0"]') THEN 0
                    ELSE 1
                END as winning_outcome
            FROM read_parquet('{BASE}/legacy_trades/*.parquet') t
            JOIN read_parquet('{BASE}/markets/*.parquet') m
            ON t.fpmm_address = m.market_maker_address
            WHERE m.closed = true AND m.volume >= 10000
        )
        SELECT 
            CASE 
                WHEN amount < 1e17 THEN 'small'
                WHEN amount < 1e18 THEN 'medium'
                WHEN amount < 1e19 THEN 'large'
                ELSE 'whale'
            END as size_bucket,
            COUNT(*) total,
            SUM(CASE WHEN (is_buy AND outcome_index = winning_outcome) OR 
                         (NOT is_buy AND outcome_index != winning_outcome) THEN 1 ELSE 0 END) as correct
        FROM trades_resolved
        WHERE winning_outcome >= 0
        GROUP BY size_bucket
        ORDER BY MIN(amount)
    """).fetchall()
    
    for r in rows:
        wr = r[2] / r[1] * 100 if r[1] > 0 else 0
        print(f"  {r[0]:>8}: {r[1]:>8,} trades, {wr:.1f}% correct")
    results['size_analysis'] = [{'size': r[0], 'trades': r[1], 'correct': r[2], 'wr': round(r[2]/r[1]*100, 1)} for r in rows]
except Exception as e:
    print(f"Size analysis failed: {e}")

# 4. Kalshi analysis (if available)
if kfiles:
    print(f"\n=== KALSHI DATA ({len(kfiles)} files) ===")
    try:
        sample = con.execute(f"SELECT * FROM read_parquet('{kfiles[0]}') LIMIT 1").fetchall()
        cols = [d[0] for d in con.description]
        print(f"Kalshi columns: {cols}")
        results['kalshi'] = {'files': len(kfiles), 'columns': cols}
    except Exception as e:
        print(f"Kalshi read failed: {e}")

# Save results
with open(OUT, 'w') as f:
    json.dump(results, f, indent=2, default=str)
print(f"\nResults saved to {OUT}")

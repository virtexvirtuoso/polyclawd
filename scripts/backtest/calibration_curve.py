"""
Build calibration data: for each raw model probability bucket, 
what's the ACTUAL hit rate?

If model says "10% chance this bracket hits" (= 90% NO confidence),
how often does the bracket actually hit?
"""
import sqlite3
import json

db = sqlite3.connect('/var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db')
db.row_factory = sqlite3.Row

# Get all resolved forecast_log entries with bracket probabilities
rows = db.execute("""
    SELECT city, target_date, ensemble_mean_f, ensemble_std_f, effective_std_f,
           n_sources, source_agreement, bracket_low_f, bracket_high_f,
           model_fair_value, market_price, actual_high_f, comparison
    FROM forecast_log 
    WHERE actual_high_f IS NOT NULL 
      AND bracket_low_f IS NOT NULL
      AND model_fair_value IS NOT NULL
    GROUP BY city, target_date, bracket_low_f, bracket_high_f, comparison
    ORDER BY target_date
""").fetchall()

print(f"Total resolved bracket forecasts: {len(rows)}")

# For each forecast, compute: did the actual temp fall in the bracket?
# Then bucket by model_fair_value (our predicted probability)
buckets = {}  # {bucket: [actual_hit_0_or_1, ...]}

for r in rows:
    actual = r['actual_high_f']
    low = r['bracket_low_f']
    high = r['bracket_high_f']
    fair = r['model_fair_value']
    comp = r['comparison'] or 'between'
    
    if actual is None or fair is None or low is None:
        continue
    
    # Did the bracket hit?
    if high is not None and (comp == 'between' or comp is None or comp == 'exact'):
        hit = 1 if low <= actual <= high else 0
    elif comp == 'above':
        hit = 1 if actual > low else 0
    elif comp == 'below' and high is not None:
        hit = 1 if actual < high else 0
    else:
        continue
    
    # Bucket by model fair value (0.05 buckets)
    bucket = round(fair * 20) / 20  # round to nearest 0.05
    bucket = max(0.0, min(1.0, bucket))
    
    if bucket not in buckets:
        buckets[bucket] = []
    buckets[bucket].append(hit)

print(f"\n{'Model P':>8} {'N':>6} {'Actual':>8} {'Gap':>8} {'Assessment'}")
print("-" * 55)
calibration_data = []
for b in sorted(buckets):
    hits = buckets[b]
    n = len(hits)
    if n < 3:
        continue
    actual_rate = sum(hits) / n
    gap = actual_rate - b
    assessment = "✅" if abs(gap) < 0.05 else "⚠️" if abs(gap) < 0.15 else "🔴"
    print(f"{b:>8.2f} {n:>6} {actual_rate:>8.3f} {gap:>+8.3f} {assessment}")
    calibration_data.append({"predicted": b, "actual": actual_rate, "n": n})

# Also break down by bracket width
print(f"\n\nBy bracket width:")
width_buckets = {}
for r in rows:
    actual = r['actual_high_f']
    low = r['bracket_low_f']
    high = r['bracket_high_f']
    fair = r['model_fair_value']
    if actual is None or fair is None or low is None or high is None:
        continue
    
    width = round(high - low)
    hit = 1 if low <= actual <= high else 0
    
    if width not in width_buckets:
        width_buckets[width] = {"hits": [], "fairs": []}
    width_buckets[width]["hits"].append(hit)
    width_buckets[width]["fairs"].append(fair)

print(f"{'Width':>6} {'N':>6} {'Hit%':>8} {'AvgFair':>8} {'Gap':>8}")
for w in sorted(width_buckets):
    d = width_buckets[w]
    n = len(d["hits"])
    if n < 5:
        continue
    hit_rate = sum(d["hits"]) / n
    avg_fair = sum(d["fairs"]) / n
    gap = hit_rate - avg_fair
    print(f"{w:>5}°F {n:>6} {hit_rate:>8.3f} {avg_fair:>8.3f} {gap:>+8.3f}")

# By n_sources
print(f"\n\nBy n_sources:")
src_buckets = {}
for r in rows:
    actual = r['actual_high_f']
    low = r['bracket_low_f']
    high = r['bracket_high_f']
    fair = r['model_fair_value']
    ns = r['n_sources']
    if actual is None or fair is None or low is None or high is None:
        continue
    hit = 1 if low <= actual <= high else 0
    if ns not in src_buckets:
        src_buckets[ns] = {"hits": [], "fairs": []}
    src_buckets[ns]["hits"].append(hit)
    src_buckets[ns]["fairs"].append(fair)

print(f"{'Srcs':>6} {'N':>6} {'Hit%':>8} {'AvgFair':>8} {'Gap':>8}")
for s in sorted(src_buckets):
    d = src_buckets[s]
    n = len(d["hits"])
    hit_rate = sum(d["hits"]) / n
    avg_fair = sum(d["fairs"]) / n
    gap = hit_rate - avg_fair
    print(f"{s:>6} {n:>6} {hit_rate:>8.3f} {avg_fair:>8.3f} {gap:>+8.3f}")

# Output the calibration map for embedding in code
print("\n\n# Calibration map for code:")
print("CALIBRATION_MAP = {")
for d in sorted(calibration_data, key=lambda x: x['predicted']):
    if d['n'] >= 5:
        print(f"    {d['predicted']:.2f}: {d['actual']:.3f},  # n={d['n']}")
print("}")

db.close()

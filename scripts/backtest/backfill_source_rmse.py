"""
Backfill source_city_rmse with actual temperatures from forecast_log,
then compute per-source accuracy.
"""
import sqlite3

db = sqlite3.connect('/var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db')
db.row_factory = sqlite3.Row

# Step 1: Get actual temps from forecast_log (one per city+date)
actuals = {}
rows = db.execute("""
    SELECT LOWER(city) as city, target_date, actual_high_f
    FROM forecast_log
    WHERE actual_high_f IS NOT NULL
    GROUP BY LOWER(city), target_date
""").fetchall()
for r in rows:
    actuals[(r['city'], r['target_date'])] = r['actual_high_f']
print(f"Actuals available: {len(actuals)} city-dates")

# Step 2: Update source_city_rmse
updated = 0
rows = db.execute("SELECT id, LOWER(city) as city, target_date, forecast_high_f FROM source_city_rmse WHERE actual_high_f IS NULL").fetchall()
for r in rows:
    key = (r['city'], r['target_date'])
    if key in actuals and r['forecast_high_f'] is not None:
        actual = actuals[key]
        error = actual - r['forecast_high_f']
        db.execute("UPDATE source_city_rmse SET actual_high_f=?, error_f=? WHERE id=?",
                   (actual, round(error, 2), r['id']))
        updated += 1

db.commit()
print(f"Backfilled {updated} rows")

# Step 3: Now compute per-source accuracy
print("\n" + "=" * 70)
print("PER-SOURCE ACCURACY (all cities)")
print("=" * 70)
rows = db.execute("""
    SELECT source,
           COUNT(*) as n,
           ROUND(AVG(error_f), 2) as bias,
           ROUND(AVG(ABS(error_f)), 2) as mae,
           ROUND(SQRT(AVG(error_f * error_f)), 2) as rmse,
           ROUND(MAX(ABS(error_f)), 1) as max_err
    FROM source_city_rmse
    WHERE error_f IS NOT NULL
    GROUP BY source
    ORDER BY mae
""").fetchall()
print(f"{'Source':<25} {'N':>5} {'Bias':>7} {'MAE':>6} {'RMSE':>6} {'MaxErr':>7}")
print("-" * 62)
for r in rows:
    print(f"{r['source']:<25} {r['n']:>5} {r['bias']:>+7.2f} {r['mae']:>6.2f} {r['rmse']:>6.2f} {r['max_err']:>7.1f}")

# Step 4: Per-source per-city
print("\n" + "=" * 70)
print("PER-SOURCE PER-CITY (worst offenders)")
print("=" * 70)
rows = db.execute("""
    SELECT source, LOWER(city) as city,
           COUNT(*) as n,
           ROUND(AVG(error_f), 2) as bias,
           ROUND(AVG(ABS(error_f)), 2) as mae
    FROM source_city_rmse
    WHERE error_f IS NOT NULL
    GROUP BY source, LOWER(city)
    HAVING COUNT(*) >= 3
    ORDER BY mae DESC
    LIMIT 20
""").fetchall()
print(f"{'Source':<25} {'City':<18} {'N':>4} {'Bias':>7} {'MAE':>6}")
for r in rows:
    print(f"{r['source']:<25} {r['city']:<18} {r['n']:>4} {r['bias']:>+7.2f} {r['mae']:>6.2f}")

# Step 5: Which source is closest to Weather.com (resolution source)?
print("\n" + "=" * 70)
print("AGREEMENT WITH WEATHER.COM (the judge)")
print("=" * 70)
# Compare each source's forecast to weather_com's forecast for same city+date
wcom = {}
rows = db.execute("""
    SELECT LOWER(city) as city, target_date, forecast_high_f 
    FROM source_city_rmse 
    WHERE source='weather_com' AND forecast_high_f IS NOT NULL
""").fetchall()
for r in rows:
    wcom[(r['city'], r['target_date'])] = r['forecast_high_f']

source_diffs = {}
rows = db.execute("""
    SELECT source, LOWER(city) as city, target_date, forecast_high_f
    FROM source_city_rmse
    WHERE source != 'weather_com' AND forecast_high_f IS NOT NULL
""").fetchall()
for r in rows:
    key = (r['city'], r['target_date'])
    if key in wcom:
        diff = r['forecast_high_f'] - wcom[key]
        src = r['source']
        if src not in source_diffs:
            source_diffs[src] = []
        source_diffs[src].append(diff)

print(f"{'Source':<25} {'N':>5} {'vs WCom Bias':>12} {'vs WCom MAE':>12} {'vs WCom Std':>12}")
for src in sorted(source_diffs, key=lambda s: sum(abs(d) for d in source_diffs[s])/len(source_diffs[s])):
    diffs = source_diffs[src]
    n = len(diffs)
    bias = sum(diffs) / n
    mae = sum(abs(d) for d in diffs) / n
    std = (sum(d**2 for d in diffs) / n - bias**2) ** 0.5
    print(f"{src:<25} {n:>5} {bias:>+12.2f} {mae:>12.2f} {std:>12.2f}")

db.close()

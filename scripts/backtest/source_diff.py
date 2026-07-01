"""
Compare weather sources: do they actually give different numbers?
Or are they all basically saying the same thing?
"""
import sqlite3, json

db = sqlite3.connect('/var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db')
db.row_factory = sqlite3.Row

# Get forecast_log entries that have multiple sources, and look at the raw source data
# The forecast_log doesn't store per-source values, but source_city_rmse does
# Let's check what's actually in source_city_rmse
print("=" * 70)
print("1. SOURCE_CITY_RMSE — do we have actual forecast values?")
print("=" * 70)
rows = db.execute("""
    SELECT * FROM source_city_rmse LIMIT 5
""").fetchall()
for r in rows:
    cols = r.keys()
    for c in cols:
        print(f"  {c}: {r[c]}")
    print()

# Check if forecast values are populated
print("=" * 70)
print("2. How many source forecasts have actual values?")
print("=" * 70)
rows = db.execute("""
    SELECT source, 
           COUNT(*) as total,
           SUM(CASE WHEN forecast_high_f IS NOT NULL THEN 1 ELSE 0 END) as has_forecast,
           SUM(CASE WHEN actual_high_f IS NOT NULL THEN 1 ELSE 0 END) as has_actual,
           SUM(CASE WHEN error_f IS NOT NULL THEN 1 ELSE 0 END) as has_error
    FROM source_city_rmse
    GROUP BY source
    ORDER BY total DESC
""").fetchall()
for r in rows:
    print(f"  {r['source']:<25} total={r['total']:>4}  forecast={r['has_forecast']:>4}  actual={r['has_actual']:>4}  error={r['has_error']:>4}")

# Since source_city_rmse might not have the data, let's look at the ensemble cache
# by reading the forecast_log sources field or checking the ensemble directly
print("\n" + "=" * 70)
print("3. FORECAST_LOG — source agreement vs actual error")
print("=" * 70)
rows = db.execute("""
    SELECT n_sources, source_agreement,
           COUNT(*) as n,
           ROUND(AVG(ABS(forecast_error_f)),2) as mae,
           ROUND(AVG(ensemble_std_f),2) as avg_std,
           ROUND(AVG(effective_std_f),2) as avg_eff_std
    FROM forecast_log
    WHERE actual_high_f IS NOT NULL
    GROUP BY n_sources, ROUND(source_agreement, 1)
    HAVING n >= 10
    ORDER BY n_sources, source_agreement
""").fetchall()
print(f"{'Srcs':>5} {'Agree':>6} {'N':>6} {'MAE':>6} {'Std':>6} {'EffStd':>7}")
for r in rows:
    print(f"{r['n_sources']:>5} {r['source_agreement']:>6.1f} {r['n']:>6} {r['mae']:>6} {r['avg_std']:>6} {r['avg_eff_std']:>7}")

# Now let's look at the actual ensemble data stored in cache
# Check if there's a way to see per-source forecasts
print("\n" + "=" * 70)
print("4. Same city+date — how much do forecasts spread?")
print("=" * 70)
# Use forecast_log: ensemble_mean vs actual, grouped by std buckets
rows = db.execute("""
    SELECT 
        CASE 
            WHEN ensemble_std_f < 1.5 THEN '<1.5'
            WHEN ensemble_std_f < 3.0 THEN '1.5-3'
            WHEN ensemble_std_f < 5.0 THEN '3-5'
            ELSE '5+'
        END as std_bucket,
        COUNT(DISTINCT city || target_date) as n_forecasts,
        ROUND(AVG(ABS(forecast_error_f)),2) as mae,
        ROUND(AVG(ensemble_std_f),2) as avg_std,
        ROUND(AVG(n_sources),1) as avg_src
    FROM forecast_log
    WHERE actual_high_f IS NOT NULL
    GROUP BY std_bucket
    ORDER BY avg_std
""").fetchall()
print(f"{'StdBucket':>10} {'N':>6} {'MAE':>6} {'AvgStd':>7} {'AvgSrc':>7}")
for r in rows:
    print(f"{r['std_bucket']:>10} {r['n_forecasts']:>6} {r['mae']:>6} {r['avg_std']:>7} {r['avg_src']:>7}")

db.close()

import sqlite3, json, os
db = sqlite3.connect('/var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db')
db.row_factory = sqlite3.Row

print("=" * 80)
print("1. FORECAST LOG: Resolved forecasts — ensemble vs actual")
print("=" * 80)
rows = db.execute("""
    SELECT city, target_date, ensemble_mean_f, ensemble_std_f, effective_std_f,
           n_sources, source_agreement, bracket_low_f, bracket_high_f, 
           side, edge_pct, model_fair_value, market_price, 
           actual_high_f, forecast_error_f, comparison
    FROM forecast_log 
    WHERE actual_high_f IS NOT NULL
    GROUP BY city, target_date, bracket_low_f, bracket_high_f, comparison
    ORDER BY target_date, city
""").fetchall()
print(f"Total unique resolved forecasts: {len(rows)}")

# Per-city error stats
city_errors = {}
for r in rows:
    c = r['city']
    if c not in city_errors:
        city_errors[c] = []
    if r['forecast_error_f'] is not None:
        city_errors[c].append(r['forecast_error_f'])

print(f"\n{'City':<20} {'N':>4} {'Bias':>7} {'MAE':>7} {'RMSE':>7} {'Max':>7}")
for city in sorted(city_errors):
    errs = city_errors[city]
    n = len(errs)
    bias = sum(errs) / n
    mae = sum(abs(e) for e in errs) / n
    rmse = (sum(e**2 for e in errs) / n) ** 0.5
    mx = max(abs(e) for e in errs)
    print(f"{city:<20} {n:>4} {bias:>+7.1f} {mae:>7.1f} {rmse:>7.1f} {mx:>7.1f}")

print("\n" + "=" * 80)
print("2. SOURCE WEIGHTS (latest)")
print("=" * 80)
rows = db.execute("""
    SELECT source, weight, ic_value, sample_size, reason,
           datetime(timestamp, 'unixepoch') as ts
    FROM source_weights 
    ORDER BY timestamp DESC
    LIMIT 20
""").fetchall()
for r in rows:
    print(f"  {r['source']:<25} w={r['weight']:.2f}  ic={r['ic_value']}  n={r['sample_size']}  {r['reason']}")

print("\n" + "=" * 80)
print("3. SOURCE HEALTH")
print("=" * 80)
rows = db.execute("SELECT * FROM source_health ORDER BY source").fetchall()
for r in rows:
    print(f"  {r['source']:<25} ok={r['total_successes']:>5}  fail={r['total_failures']:>5}  streak_fail={r['consecutive_failures']:>3}  lat={r['avg_latency_ms']:.0f}ms")

print("\n" + "=" * 80)
print("4. ENSEMBLE STD vs ACTUAL ERROR — the overconfidence test")
print("=" * 80)
rows = db.execute("""
    SELECT ensemble_std_f, effective_std_f, forecast_error_f, n_sources, source_agreement
    FROM forecast_log 
    WHERE actual_high_f IS NOT NULL
    GROUP BY city, target_date
    ORDER BY target_date
""").fetchall()
print(f"Unique city-dates: {len(rows)}")
if rows:
    ens_stds = [r['ensemble_std_f'] for r in rows if r['ensemble_std_f']]
    eff_stds = [r['effective_std_f'] for r in rows if r['effective_std_f']]
    abs_errs = [abs(r['forecast_error_f']) for r in rows if r['forecast_error_f'] is not None]
    agreements = [r['source_agreement'] for r in rows if r['source_agreement'] is not None]
    n_srcs = [r['n_sources'] for r in rows]
    
    print(f"  Avg ensemble_std:  {sum(ens_stds)/len(ens_stds):.2f}°F")
    print(f"  Avg effective_std: {sum(eff_stds)/len(eff_stds):.2f}°F")
    print(f"  Avg actual |error|: {sum(abs_errs)/len(abs_errs):.2f}°F")
    print(f"  Avg n_sources:     {sum(n_srcs)/len(n_srcs):.1f}")
    print(f"  Avg agreement:     {sum(agreements)/len(agreements):.3f}")
    
    # How often does actual error exceed ensemble std?
    exceeded = sum(1 for r in rows if r['forecast_error_f'] is not None and r['ensemble_std_f'] and abs(r['forecast_error_f']) > r['ensemble_std_f'])
    exceeded_eff = sum(1 for r in rows if r['forecast_error_f'] is not None and r['effective_std_f'] and abs(r['forecast_error_f']) > r['effective_std_f'])
    valid = sum(1 for r in rows if r['forecast_error_f'] is not None and r['ensemble_std_f'])
    print(f"  |error| > ensemble_std:  {exceeded}/{valid} = {exceeded/valid*100:.0f}%  (should be ~32%)")
    print(f"  |error| > effective_std: {exceeded_eff}/{valid} = {exceeded_eff/valid*100:.0f}%  (should be ~32%)")

    # 2-sigma exceedance
    exceeded_2s = sum(1 for r in rows if r['forecast_error_f'] is not None and r['ensemble_std_f'] and abs(r['forecast_error_f']) > 2 * r['ensemble_std_f'])
    exceeded_2s_eff = sum(1 for r in rows if r['forecast_error_f'] is not None and r['effective_std_f'] and abs(r['forecast_error_f']) > 2 * r['effective_std_f'])
    print(f"  |error| > 2x ens_std:    {exceeded_2s}/{valid} = {exceeded_2s/valid*100:.0f}%  (should be ~5%)")
    print(f"  |error| > 2x eff_std:    {exceeded_2s_eff}/{valid} = {exceeded_2s_eff/valid*100:.0f}%  (should be ~5%)")

print("\n" + "=" * 80)
print("5. RESOLUTION DATA — paper positions (weather)")
print("=" * 80)
rows = db.execute("""
    SELECT market_title, side, entry_price, status, pnl, archetype
    FROM paper_positions
    WHERE archetype = 'weather' OR market_title LIKE '%temperature%' OR market_title LIKE '%°%'
    ORDER BY rowid DESC
    LIMIT 30
""").fetchall()
print(f"Weather positions: {len(rows)}")
for r in rows:
    pnl_str = f"${r['pnl']:.0f}" if r['pnl'] else "n/a"
    print(f"  [{r['status']:<8}] {r['side']} @ {r['entry_price']:.3f} → {pnl_str:>6}  {r['market_title'][:60]}")

print("\n" + "=" * 80)
print("6. FORECAST LOG: n_sources distribution")
print("=" * 80)
rows = db.execute("""
    SELECT n_sources, COUNT(*) as cnt, ROUND(AVG(ABS(forecast_error_f)),2) as mae
    FROM forecast_log
    WHERE actual_high_f IS NOT NULL
    GROUP BY n_sources
    ORDER BY n_sources
""").fetchall()
for r in rows:
    print(f"  n_sources={r['n_sources']}: {r['cnt']} forecasts, MAE={r['mae']}°F")

db.close()

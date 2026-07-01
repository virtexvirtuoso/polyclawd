"""
Build isotonic calibration function for weather_ensemble.

Isotonic regression: monotone non-decreasing mapping from raw probability
to calibrated probability. The gold standard for probability calibration.

We fit it from our 1,190 resolved bracket forecasts.
Output: a lookup table that gets embedded in weather_ensemble.py
"""
import sqlite3

db = sqlite3.connect('/var/www/virtuosocrypto.com/polyclawd/storage/shadow_trades.db')
db.row_factory = sqlite3.Row

rows = db.execute("""
    SELECT model_fair_value, actual_high_f, bracket_low_f, bracket_high_f, comparison
    FROM forecast_log 
    WHERE actual_high_f IS NOT NULL 
      AND bracket_low_f IS NOT NULL
      AND model_fair_value IS NOT NULL
    GROUP BY city, target_date, bracket_low_f, bracket_high_f, comparison
    ORDER BY model_fair_value
""").fetchall()
db.close()

# Build (predicted, actual_hit) pairs
pairs = []
for r in rows:
    actual = r['actual_high_f']
    low = r['bracket_low_f']
    high = r['bracket_high_f']
    fair = r['model_fair_value']
    comp = r['comparison'] or 'between'
    
    if actual is None or fair is None or low is None:
        continue
    if high is not None and (comp in ('between', 'exact', None)):
        hit = 1 if low <= actual <= high else 0
    elif comp == 'above':
        hit = 1 if actual > low else 0
    elif comp == 'below' and high is not None:
        hit = 1 if actual < high else 0
    else:
        continue
    pairs.append((fair, hit))

pairs.sort(key=lambda x: x[0])
print(f"Total calibration pairs: {len(pairs)}")

# Pool-Adjacent-Violators (PAV) algorithm for isotonic regression
# Simple, no dependencies needed
def pav_isotonic(pairs):
    """Pool Adjacent Violators - isotonic regression."""
    # Group into blocks of similar predicted values
    n = len(pairs)
    # Start with each point as its own block
    blocks = [{"sum_y": y, "n": 1, "x_min": x, "x_max": x} for x, y in pairs]
    
    # Merge adjacent blocks that violate monotonicity
    changed = True
    while changed:
        changed = False
        new_blocks = [blocks[0]]
        for i in range(1, len(blocks)):
            prev_mean = new_blocks[-1]["sum_y"] / new_blocks[-1]["n"]
            curr_mean = blocks[i]["sum_y"] / blocks[i]["n"]
            if curr_mean < prev_mean:
                # Violation: merge
                new_blocks[-1]["sum_y"] += blocks[i]["sum_y"]
                new_blocks[-1]["n"] += blocks[i]["n"]
                new_blocks[-1]["x_max"] = blocks[i]["x_max"]
                changed = True
            else:
                new_blocks.append(blocks[i])
        blocks = new_blocks
    
    # Extract calibration points
    cal_points = []
    for b in blocks:
        x_mid = (b["x_min"] + b["x_max"]) / 2
        y_cal = b["sum_y"] / b["n"]
        cal_points.append((round(x_mid, 4), round(y_cal, 4), b["n"]))
    
    return cal_points

cal = pav_isotonic(pairs)

print(f"\nIsotonic calibration points ({len(cal)} blocks):")
print(f"{'Raw P':>8} {'Cal P':>8} {'N':>6}")
for x, y, n in cal:
    print(f"{x:>8.4f} {y:>8.4f} {n:>6}")

# Create a clean lookup table for embedding
# Sample at regular intervals using linear interpolation
def interpolate(cal_points, x):
    """Linear interpolation between calibration points."""
    if x <= cal_points[0][0]:
        return cal_points[0][1]
    if x >= cal_points[-1][0]:
        return cal_points[-1][1]
    for i in range(len(cal_points) - 1):
        x0, y0, _ = cal_points[i]
        x1, y1, _ = cal_points[i + 1]
        if x0 <= x <= x1:
            t = (x - x0) / (x1 - x0) if x1 != x0 else 0
            return y0 + t * (y1 - y0)
    return cal_points[-1][1]

print("\n\n# Calibration lookup table (embed in weather_ensemble.py):")
print("# Maps raw model probability → calibrated probability")  
print("# Built from", len(pairs), "resolved bracket forecasts via isotonic regression")
print("_CALIBRATION_POINTS = [")
for x, y, n in cal:
    print(f"    ({x:.4f}, {y:.4f}),  # n={n}")
print("]")

print("\n# Sampled at 0.05 intervals:")
print("# Raw  →  Calibrated")
for raw in [i/20 for i in range(21)]:
    calibrated = interpolate(cal, raw)
    print(f"# {raw:.2f}  →  {calibrated:.3f}")

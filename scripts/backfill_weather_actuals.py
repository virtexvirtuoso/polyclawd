#!/usr/bin/env python3
"""
backfill_weather_actuals.py — Phase 1 weather ensemble accuracy fix.
v2: uses connection with 30s timeout to handle DB lock from scheduler.
"""
from __future__ import annotations
import argparse, sqlite3, sys, time
from datetime import date, timedelta

sys.path.insert(0, "/var/www/virtuosocrypto.com/polyclawd")
from signals.weather_ensemble import _DB_PATH, _fetch_twc_actuals

def resolve_with_retry(city: str, target_date: str, high_f: float, retries: int = 5) -> bool:
    for attempt in range(retries):
        try:
            con = sqlite3.connect(_DB_PATH, timeout=30)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute(
                "UPDATE source_city_rmse SET actual_high_f=?, error_f=forecast_high_f-? "
                "WHERE city=? AND target_date=? AND actual_high_f IS NULL",
                (high_f, high_f, city.lower(), target_date),
            )
            con.commit()
            con.close()
            return True
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < retries - 1:
                time.sleep(1 + attempt)
                continue
            print(f"  [err]  {target_date} {city}: {e}")
            return False
    return False

def run(dry_run: bool = False, days: int | None = None) -> None:
    con = sqlite3.connect(_DB_PATH, timeout=30)
    today = date.today().isoformat()
    params = [today]
    extra = ""
    if days:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        extra = " AND target_date >= ?"
        params.append(cutoff)
    rows = con.execute(
        f"SELECT DISTINCT city, target_date FROM source_city_rmse "
        f"WHERE actual_high_f IS NULL AND target_date < ?{extra} ORDER BY target_date DESC",
        params,
    ).fetchall()
    con.close()

    print(f"[backfill] {len(rows)} city/date pairs to resolve (dry_run={dry_run})")
    ok = skipped = errors = 0
    for city, target_date in rows:
        actuals = _fetch_twc_actuals(city, target_date)
        if not actuals or actuals.get("high_f") is None:
            print(f"  [skip] {target_date} {city}: no TWC actuals")
            skipped += 1
            continue
        high_f = actuals["high_f"]
        if dry_run:
            print(f"  [dry]  {target_date} {city}: {high_f:.1f}°F")
            ok += 1
        else:
            if resolve_with_retry(city, target_date, high_f):
                print(f"  [ok]   {target_date} {city}: {high_f:.1f}°F")
                ok += 1
            else:
                errors += 1
        time.sleep(0.15)

    print(f"[backfill] done: {ok} resolved, {skipped} skipped, {errors} errors")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int, default=None)
    args = ap.parse_args()
    run(dry_run=args.dry_run, days=args.days)

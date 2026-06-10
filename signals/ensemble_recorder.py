#!/usr/bin/env python3
"""
Evening Ensemble Recorder — knob 6's perishable training data (2026-06-10).

Snapshots the full 82-member ensemble forecast (GEFS ~31 + ECMWF IFS ~51, free
via Open-Meteo) for TOMORROW's daily high AND low at each Kalshi city, once per
evening in the same local 19:30-20:30 window the fade strategy trades in. This
is exactly the information state the eve-before market prices embed, captured
forward so the distribution engine ([[Weather-Ensemble-Distribution-Engine]])
has training data whenever it gets built — historical day-ahead member data is
NOT reliably backfillable from public archives (verified 2026-06-10), so every
un-recorded evening is lost forever.

Pure data capture: one Open-Meteo call per in-window city per evening, appended
to data/ensemble_snapshots.jsonl. No trading, no DB. Engine-state flag:
ensemble_recorder_enabled (default True).

NOTE: coordinates are settlement-station approximations (airport/park). Exact
station mapping + per-station bias correction is part of the engine build, not
the recorder — raw members are recorded untouched.

CLI: python -m signals.ensemble_recorder [--force-window] [--cities nyc,chi]
"""

import argparse
import json
import logging
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = PROJECT_ROOT / "data" / "ensemble_snapshots.jsonl"

API = "https://ensemble-api.open-meteo.com/v1/ensemble"
MODELS = "gfs_seamless,ecmwf_ifs025"
UA = {"User-Agent": "polyclawd-ensemble-recorder/1.0"}

WINDOW_START, WINDOW_END = (19, 30), (20, 30)

# city -> (lat, lon, tz)  — approximate settlement stations for the Kalshi series
CITIES = {
    "nyc":  (40.78,  -73.97, "America/New_York"),      # Central Park (KNYC)
    "dc":   (38.85,  -77.04, "America/New_York"),
    "phil": (39.87,  -75.23, "America/New_York"),
    "mia":  (25.79,  -80.32, "America/New_York"),
    "atl":  (33.63,  -84.44, "America/New_York"),
    "bos":  (42.36,  -71.01, "America/New_York"),
    "chi":  (41.79,  -87.75, "America/Chicago"),        # Midway
    "aus":  (30.18,  -97.68, "America/Chicago"),
    "hou":  (29.98,  -95.34, "America/Chicago"),
    "min":  (44.88,  -93.22, "America/Chicago"),
    "okc":  (35.39,  -97.60, "America/Chicago"),
    "dal":  (32.85,  -96.85, "America/Chicago"),        # Love Field
    "satx": (29.53,  -98.47, "America/Chicago"),
    "nola": (29.99,  -90.25, "America/Chicago"),
    "den":  (39.85, -104.66, "America/Denver"),
    "phx":  (33.43, -112.01, "America/Phoenix"),
    "lax":  (33.94, -118.41, "America/Los_Angeles"),
    "sfo":  (37.62, -122.37, "America/Los_Angeles"),
    "sea":  (47.45, -122.31, "America/Los_Angeles"),
    "lv":   (36.08, -115.15, "America/Los_Angeles"),
}


def _jget(url, timeout=30, retries=3):
    for a in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return json.load(urllib.request.urlopen(req, timeout=timeout))
        except Exception:
            if a == retries - 1:
                return None
            time.sleep(1.0 * (a + 1))
    return None


def cities_in_window(now_utc: datetime, force: bool = False, only=None) -> list:
    out = []
    for city, (lat, lon, tz_name) in CITIES.items():
        if only and city not in only:
            continue
        local = now_utc.astimezone(ZoneInfo(tz_name))
        if force or (WINDOW_START <= (local.hour, local.minute) <= WINDOW_END):
            out.append((city, lat, lon, tz_name, str(local.date() + timedelta(days=1))))
    return out


def _members_for_date(daily: dict, var: str, date_str: str) -> list:
    """Collect all ensemble member values of `var` for one calendar date."""
    times = daily.get("time") or []
    if date_str not in times:
        return []
    i = times.index(date_str)
    vals = []
    for key, series in daily.items():
        if key == "time" or not key.startswith(var):
            continue
        try:
            v = series[i]
        except (IndexError, TypeError):
            continue
        if v is not None:
            vals.append(round(float(v), 1))
    return vals


def _already_recorded(city: str, target_date: str) -> bool:
    if not OUT_PATH.exists():
        return False
    try:
        for line in open(OUT_PATH):
            if f'"city": "{city}"' in line and f'"target_date": "{target_date}"' in line:
                return True
    except OSError:
        pass
    return False


def record(now: datetime = None, force_window: bool = False, only=None) -> dict:
    """Scheduler + CLI entrypoint. One snapshot per (city, target_date)."""
    now = now or datetime.now(timezone.utc)
    targets = cities_in_window(now, force=force_window, only=only)
    recorded, skipped = 0, 0
    for city, lat, lon, tz_name, target_date in targets:
        if _already_recorded(city, target_date):
            skipped += 1
            continue
        d = _jget(f"{API}?latitude={lat}&longitude={lon}"
                  f"&daily=temperature_2m_max,temperature_2m_min"
                  f"&models={MODELS}&forecast_days=3&temperature_unit=fahrenheit")
        time.sleep(0.3)
        daily = (d or {}).get("daily") or {}
        hi = _members_for_date(daily, "temperature_2m_max", target_date)
        lo = _members_for_date(daily, "temperature_2m_min", target_date)
        if len(hi) < 20:  # need a real ensemble, not a degraded response
            logger.warning("ensemble-recorder: %s %s only %d members — skipped",
                           city, target_date, len(hi))
            continue
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(OUT_PATH, "a") as f:
            f.write(json.dumps({
                "ts": now.isoformat(), "city": city, "tz": tz_name,
                "lat": lat, "lon": lon, "target_date": target_date,
                "models": MODELS, "n_members": len(hi),
                "high_members_f": hi, "low_members_f": lo,
            }) + "\n")
        recorded += 1
    if recorded:
        logger.info("ensemble-recorder: %d cities snapshotted (%d already done)",
                    recorded, skipped)
    return {"in_window": len(targets), "recorded": recorded,
            "already_recorded": skipped}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--force-window", action="store_true")
    ap.add_argument("--cities", help="comma-separated subset, e.g. nyc,chi")
    a = ap.parse_args()
    only = set(a.cities.split(",")) if a.cities else None
    print(json.dumps(record(force_window=a.force_window, only=only), indent=2))

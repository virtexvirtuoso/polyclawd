"""
Weather Resolution-Source Edge — Dual-path edge signal for weather markets.

Kalshi path: NWS gridpoint forecast (resolution source = NWS CLI) vs market price.
Polymarket path: TWC/WU forecast (resolution source) vs market price.

The key insight: for each platform, the resolution source's own forecast
disagreeing with the market price is the highest-conviction signal.

Requires: scipy (for normal CDF), weather_ensemble.py (for source data + RMSE)
"""

import os
import re
import time
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Tuple

import requests
from scipy.stats import norm

logger = logging.getLogger(__name__)

_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "storage", "shadow_trades.db")
_HEADERS = {"User-Agent": "polyclawd/1.0 (weather@virtuosocrypto.com)", "Accept": "application/geo+json"}

# ── Cache ────────────────────────────────────────────────────────────────
_raw_gridpoint_cache: Dict[str, Tuple[dict, float]] = {}  # key -> (data, timestamp)
_RAW_CACHE_TTL = 7200  # 2hr — NWS updates ~4x/day

# ── Constants ────────────────────────────────────────────────────────────
MIN_EDGE_PP = 3.0       # minimum edge in percentage points to report
RMSE_GATE = 4.0         # suppress NWS edge for cities with RMSE > this
THRESHOLD_PROXIMITY = 2.0  # flag when forecast is within 2°F of threshold


# ── Kalshi CLI Station Mapping ───────────────────────────────────────────
# Kalshi resolves against NWS Daily Climate Report (CLI) for each city.
# CLI station is the primary ASOS station. These are the same stations
# the NWS uses for its climate data.
# Source: NWS CLI reports + Predict & Profit ASOS research
KALSHI_CLI_STATIONS: Dict[str, dict] = {
    "atlanta":       {"icao": "KATL", "lat": 33.6407, "lon": -84.4277, "name": "Hartsfield-Jackson"},
    "boston":         {"icao": "KBOS", "lat": 42.3656, "lon": -71.0096, "name": "Logan Intl"},
    "chicago":       {"icao": "KORD", "lat": 41.9742, "lon": -87.9073, "name": "O'Hare Intl"},
    "dallas":        {"icao": "KDFW", "lat": 32.8998, "lon": -97.0403, "name": "DFW Intl"},
    "denver":        {"icao": "KDEN", "lat": 39.8561, "lon": -104.6737, "name": "Denver Intl"},
    "houston":       {"icao": "KIAH", "lat": 29.9844, "lon": -95.3414, "name": "Bush Intercontinental"},
    "las vegas":     {"icao": "KLAS", "lat": 36.0840, "lon": -115.1537, "name": "Harry Reid Intl"},
    "los angeles":   {"icao": "KLAX", "lat": 33.9425, "lon": -118.4081, "name": "LAX"},
    "miami":         {"icao": "KMIA", "lat": 25.7959, "lon": -80.2870, "name": "Miami Intl"},
    "minneapolis":   {"icao": "KMSP", "lat": 44.8848, "lon": -93.2223, "name": "MSP Intl"},
    "new orleans":   {"icao": "KMSY", "lat": 29.9934, "lon": -90.2580, "name": "Louis Armstrong"},
    "new york":      {"icao": "KNYC", "lat": 40.7789, "lon": -73.9692, "name": "Central Park"},
    "nyc":           {"icao": "KNYC", "lat": 40.7789, "lon": -73.9692, "name": "Central Park"},
    "oklahoma city": {"icao": "KOKC", "lat": 35.3931, "lon": -97.6007, "name": "Will Rogers"},
    "phoenix":       {"icao": "KPHX", "lat": 33.4373, "lon": -112.0078, "name": "Sky Harbor"},
    "san antonio":   {"icao": "KSAT", "lat": 29.5337, "lon": -98.4698, "name": "San Antonio Intl"},
    "san francisco": {"icao": "KSFO", "lat": 37.6213, "lon": -122.3790, "name": "SFO"},
    "seattle":       {"icao": "KSEA", "lat": 47.4502, "lon": -122.3088, "name": "Sea-Tac"},
    "austin":        {"icao": "KAUS", "lat": 30.1945, "lon": -97.6699, "name": "Bergstrom Intl"},
    "washington dc": {"icao": "KDCA", "lat": 38.8512, "lon": -77.0402, "name": "Reagan National"},
    "dc":            {"icao": "KDCA", "lat": 38.8512, "lon": -77.0402, "name": "Reagan National"},
    "philadelphia":  {"icao": "KPHL", "lat": 39.8721, "lon": -75.2412, "name": "Philadelphia Intl"},
}


# ── ISO 8601 Duration Parser ─────────────────────────────────────────────

def parse_iso8601_duration(duration: str) -> float:
    """Parse ISO 8601 duration to hours. PT4H → 4.0, PT1H → 1.0, PT30M → 0.5."""
    if not duration:
        return 1.0
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", duration)
    if not match:
        return 1.0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    return hours + minutes / 60.0


def _c_to_f(celsius: float) -> float:
    """Convert Celsius to Fahrenheit."""
    return celsius * 9.0 / 5.0 + 32.0


# ── NWS Raw Gridpoint Fetcher ────────────────────────────────────────────

def _get_nws_gridpoint(lat: float, lon: float) -> Optional[Tuple[str, int, int]]:
    """Get NWS office + grid coordinates for a lat/lon. Cached forever."""
    from signals.weather_ensemble import _NWS_GRIDPOINT_CACHE
    grid_key = (round(lat, 4), round(lon, 4))
    if grid_key in _NWS_GRIDPOINT_CACHE:
        return _NWS_GRIDPOINT_CACHE[grid_key]

    try:
        r = requests.get(
            f"https://api.weather.gov/points/{lat},{lon}",
            headers=_HEADERS, timeout=10,
        )
        if r.status_code != 200:
            return None
        props = r.json().get("properties", {})
        result = (props["gridId"], props["gridX"], props["gridY"])
        _NWS_GRIDPOINT_CACHE[grid_key] = result
        return result
    except Exception as e:
        logger.debug("NWS gridpoint lookup failed for %s,%s: %s", lat, lon, e)
        return None


def fetch_nws_raw_gridpoint(lat: float, lon: float) -> Optional[dict]:
    """Fetch raw NWS gridpoint data (NOT /forecast).

    Returns dict with parsed arrays:
      temperature: [(datetime_str, value_f, duration_hours), ...]
      maxTemperature: [(date_str, value_f), ...]
      minTemperature: [(date_str, value_f), ...]
      probabilityOfPrecipitation: [(datetime_str, value_pct, duration_hours), ...]
    """
    cache_key = f"{round(lat, 3)},{round(lon, 3)}"
    cached = _raw_gridpoint_cache.get(cache_key)
    if cached and (time.time() - cached[1]) < _RAW_CACHE_TTL:
        return cached[0]

    grid = _get_nws_gridpoint(lat, lon)
    if not grid:
        return None
    office, gx, gy = grid

    # Raw gridpoint — NOT /forecast
    url = f"https://api.weather.gov/gridpoints/{office}/{gx},{gy}"
    try:
        r = requests.get(url, headers=_HEADERS, timeout=15)
        if r.status_code != 200:
            logger.debug("NWS raw gridpoint %s returned %d", url, r.status_code)
            return None
        data = r.json()
    except Exception as e:
        logger.debug("NWS raw gridpoint fetch failed: %s", e)
        return None

    props = data.get("properties", {})
    result = {}

    # Parse each property's values array
    for prop_name in ["temperature", "maxTemperature", "minTemperature",
                      "probabilityOfPrecipitation", "quantitativePrecipitation",
                      "windSpeed", "skyCover"]:
        raw = props.get(prop_name, {})
        uom = raw.get("uom", "")
        is_celsius = "degC" in uom or "Cel" in uom
        values = raw.get("values", [])
        parsed = []
        for v in values:
            val = v.get("value")
            if val is None:
                continue
            valid_time = v.get("validTime", "")
            # validTime format: "2026-06-24T06:00:00+00:00/PT1H"
            parts = valid_time.split("/")
            dt_str = parts[0] if parts else ""
            duration = parts[1] if len(parts) > 1 else "PT1H"
            duration_hours = parse_iso8601_duration(duration)

            # Convert temperature from °C to °F
            if is_celsius and prop_name in ("temperature", "maxTemperature", "minTemperature"):
                val = _c_to_f(val)

            parsed.append({
                "time": dt_str,
                "value": round(val, 1),
                "duration_hours": duration_hours,
            })
        result[prop_name] = parsed

    _raw_gridpoint_cache[cache_key] = (result, time.time())
    return result


def get_nws_raw_max_temp(lat: float, lon: float, target_date: str) -> Optional[float]:
    """Get NWS raw gridpoint max temperature for a specific date.

    Uses maxTemperature array from raw gridpoint (more precise than /forecast).
    Falls back to computing max from hourly temperature array.
    """
    raw = fetch_nws_raw_gridpoint(lat, lon)
    if not raw:
        return None

    target = datetime.strptime(target_date, "%Y-%m-%d").date()

    # Try maxTemperature first (daily values)
    for entry in raw.get("maxTemperature", []):
        try:
            entry_date = datetime.fromisoformat(entry["time"].replace("Z", "+00:00")).date()
            if entry_date == target:
                return entry["value"]
        except (ValueError, KeyError):
            continue

    # Fallback: compute max from hourly temperature for that date
    hourly_temps = []
    for entry in raw.get("temperature", []):
        try:
            entry_dt = datetime.fromisoformat(entry["time"].replace("Z", "+00:00"))
            if entry_dt.date() == target:
                hourly_temps.append(entry["value"])
        except (ValueError, KeyError):
            continue

    if hourly_temps:
        return round(max(hourly_temps), 1)

    return None


def get_nws_precip_prob_24hr(lat: float, lon: float, target_date: str) -> Optional[float]:
    """Get 24hr precipitation probability for a date.

    Converts NWS 3-6hr block probabilities to 24hr using:
    P(rain 24hr) = 1 - Π(1 - p_block_i)
    NOT max(p_block_i) — that's a common mistake.
    """
    raw = fetch_nws_raw_gridpoint(lat, lon)
    if not raw:
        return None

    target = datetime.strptime(target_date, "%Y-%m-%d").date()
    block_probs = []

    for entry in raw.get("probabilityOfPrecipitation", []):
        try:
            entry_dt = datetime.fromisoformat(entry["time"].replace("Z", "+00:00"))
            if entry_dt.date() == target:
                block_probs.append(entry["value"] / 100.0)
        except (ValueError, KeyError):
            continue

    if not block_probs:
        return None

    # P(rain in 24hr) = 1 - Π(1 - p_i)
    prob_no_rain = 1.0
    for p in block_probs:
        prob_no_rain *= (1.0 - p)
    return round(1.0 - prob_no_rain, 4)


# ── Probability Conversion ───────────────────────────────────────────────

def compute_threshold_probability(
    forecast_f: float,
    threshold_f: float,
    std_f: float,
    horizon_hours: float,
) -> dict:
    """Compute P(actual > threshold) using normal CDF with horizon-aware std floor.

    The std floor prevents overconfident CDF when upstream models are correlated.
    From April 2026 crisis fix: std_floor = 2.0 + 0.3 * sqrt(hours_out)
    """
    std_floor = 2.0 + 0.3 * (max(horizon_hours, 0) ** 0.5)
    effective_std = max(std_f, std_floor)
    z = (threshold_f - forecast_f) / effective_std
    prob_above = 1.0 - norm.cdf(z)
    threshold_edge_flag = abs(forecast_f - threshold_f) < THRESHOLD_PROXIMITY

    return {
        "prob_above": round(prob_above, 4),
        "prob_below": round(1.0 - prob_above, 4),
        "effective_std": round(effective_std, 2),
        "std_floor": round(std_floor, 2),
        "threshold_edge": threshold_edge_flag,
        "z_score": round(z, 3),
    }


def compute_range_probability(
    forecast_f: float,
    low_f: float,
    high_f: float,
    std_f: float,
    horizon_hours: float,
) -> dict:
    """Compute P(low <= actual <= high) for range/bracket markets."""
    std_floor = 2.0 + 0.3 * (max(horizon_hours, 0) ** 0.5)
    effective_std = max(std_f, std_floor)
    prob = norm.cdf((high_f - forecast_f) / effective_std) - norm.cdf((low_f - forecast_f) / effective_std)
    return {
        "prob_in_range": round(prob, 4),
        "effective_std": round(effective_std, 2),
        "threshold_edge": abs(forecast_f - (low_f + high_f) / 2) < THRESHOLD_PROXIMITY,
    }


# ── Per-City RMSE Lookup ─────────────────────────────────────────────────

def get_source_rmse(city: str, source: str) -> Optional[float]:
    """Get per-city RMSE for a forecast source from source_city_rmse table."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        row = conn.execute("""
            SELECT AVG(ABS(error_f)) FROM source_city_rmse
            WHERE city = ? AND source = ? AND actual_high_f IS NOT NULL
        """, (city.lower(), source)).fetchone()
        conn.close()
        if row and row[0] is not None:
            return round(row[0], 2)
    except Exception:
        pass
    return None


# ── Resolution-Source Edge Scanner ────────────────────────────────────────

def compute_nws_edge(
    city: str,
    target_date: str,
    threshold_f: float,
    market_price: float,
    comparison: str = "above",
) -> Optional[dict]:
    """Compute NWS resolution-source edge for a Kalshi weather market.

    NWS is Kalshi's resolution source (CLI) — when NWS disagrees with market,
    that's the highest-conviction signal.
    """
    station = KALSHI_CLI_STATIONS.get(city.lower())
    if not station:
        return None

    # Use station coords for raw gridpoint (CLI station, not city center)
    nws_max = get_nws_raw_max_temp(station["lat"], station["lon"], target_date)
    if nws_max is None:
        return None

    # Get ensemble std for probability conversion
    try:
        from signals.weather_ensemble import get_ensemble_forecast
        ens = get_ensemble_forecast(city, target_date)
        ens_std = ens["ensemble"]["high_std_f"] if ens else 3.0
        ens_mean = ens["ensemble"]["high_mean_f"] if ens else nws_max
    except Exception:
        ens_std = 3.0
        ens_mean = nws_max

    # Compute horizon
    now = datetime.now(timezone.utc)
    target_dt = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    horizon_hours = max(0, (target_dt - now).total_seconds() / 3600)

    # Probability conversion
    prob_result = compute_threshold_probability(nws_max, threshold_f, ens_std, horizon_hours)

    if comparison == "above":
        nws_implied = prob_result["prob_above"]
    elif comparison == "below":
        nws_implied = prob_result["prob_below"]
    else:
        nws_implied = prob_result["prob_above"]

    edge_pp = (nws_implied - market_price) * 100
    direction = "buy_yes" if edge_pp > 0 else "buy_no"
    abs_edge = abs(edge_pp)

    # RMSE gate
    nws_rmse = get_source_rmse(city, "nws")
    rmse_gated = nws_rmse is not None and nws_rmse > RMSE_GATE

    # Conviction tier
    ens_agrees = abs(ens_mean - nws_max) < 2.0
    if rmse_gated or prob_result["threshold_edge"]:
        conviction = "LOW"
    elif abs_edge > 5 and ens_agrees:
        conviction = "HIGH"
    elif abs_edge > 5:
        conviction = "MEDIUM"
    else:
        conviction = "LOW"

    return {
        "city": city.lower(),
        "target_date": target_date,
        "platform": "kalshi",
        "resolution_source": "nws",
        "station": station["icao"],
        "station_name": station["name"],
        "nws_raw_max_f": nws_max,
        "ensemble_forecast_f": round(ens_mean, 1),
        "ensemble_std_f": round(ens_std, 1),
        "threshold_f": threshold_f,
        "comparison": comparison,
        "nws_implied_prob": round(nws_implied, 4),
        "market_price": market_price,
        "edge_pp": round(edge_pp, 1),
        "direction": direction,
        "conviction_tier": conviction,
        "nws_rmse": nws_rmse,
        "rmse_gated": rmse_gated,
        "threshold_edge": prob_result["threshold_edge"],
        "effective_std": prob_result["effective_std"],
        "horizon_hours": round(horizon_hours, 1),
        "z_score": prob_result["z_score"],
    }


def compute_twc_edge(
    city: str,
    target_date: str,
    threshold_f: float,
    market_price: float,
    comparison: str = "above",
    twc_high: Optional[float] = None,
) -> Optional[dict]:
    """Compute TWC resolution-source edge for a Polymarket weather market.

    TWC is PM's resolution source (Weather Underground) — when TWC disagrees
    with market, that's the highest-conviction signal for PM.
    TWC RMSE is 1-8x better than NWS per-city.

    Args:
        twc_high: Pre-fetched TWC high temp. If None, fetches via get_ensemble_forecast.
    """
    if twc_high is None:
        try:
            from signals.weather_ensemble import get_ensemble_forecast
            ens = get_ensemble_forecast(city, target_date)
            if not ens:
                return None
            twc_data = ens.get("sources", {}).get("weather_com")
            if not twc_data:
                return None
            twc_high = twc_data.get("high_f")
        except Exception:
            return None

    if twc_high is None:
        return None

    # Use defaults for ensemble stats (only used for conviction tier)
    # The probability computation uses TWC RMSE directly, not ensemble std
    ens_std = 2.0
    ens_mean = twc_high

    # Compute horizon
    now = datetime.now(timezone.utc)
    target_dt = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    horizon_hours = max(0, (target_dt - now).total_seconds() / 3600)

    # Probability conversion using TWC forecast as the point estimate
    # Use TWC's actual per-city RMSE as std, NOT the generic std floor.
    # The std floor (2.0 + 0.3*sqrt(hours)) was designed for the 7-source ensemble
    # where correlated sources inflate confidence. TWC is a single source with
    # known RMSE — the floor inflates std by 2-3x, destroying the CDF signal.
    twc_rmse = get_source_rmse(city, "weather_com")
    effective_std = max(twc_rmse, 1.0) if twc_rmse else max(ens_std, 2.0)

    # Compute CDF directly (bypass compute_threshold_probability which re-applies std_floor)
    if comparison == "above":
        z = (threshold_f - twc_high) / effective_std
        twc_implied = 1.0 - norm.cdf(z)
    elif comparison == "below":
        z = (threshold_f - twc_high) / effective_std
        twc_implied = norm.cdf(z)
    else:
        z = (threshold_f - twc_high) / effective_std
        twc_implied = 1.0 - norm.cdf(z)

    threshold_edge_flag = abs(twc_high - threshold_f) < 2.0

    edge_pp = (twc_implied - market_price) * 100
    direction = "buy_yes" if edge_pp > 0 else "buy_no"
    abs_edge = abs(edge_pp)

    # RMSE (already fetched above for std computation)

    # Conviction tier
    ens_agrees = abs(ens_mean - twc_high) < 2.0
    if threshold_edge_flag:
        conviction = "LOW"
    elif abs_edge > 5 and ens_agrees:
        conviction = "HIGH"
    elif abs_edge > 5:
        conviction = "MEDIUM"
    else:
        conviction = "LOW"

    return {
        "city": city.lower(),
        "target_date": target_date,
        "platform": "polymarket",
        "resolution_source": "twc",
        "twc_forecast_f": round(twc_high, 1),
        "ensemble_forecast_f": round(ens_mean, 1),
        "ensemble_std_f": round(ens_std, 1),
        "threshold_f": threshold_f,
        "comparison": comparison,
        "twc_implied_prob": round(twc_implied, 4),
        "market_price": market_price,
        "edge_pp": round(edge_pp, 1),
        "direction": direction,
        "conviction_tier": conviction,
        "twc_rmse": twc_rmse,
        "threshold_edge": threshold_edge_flag,
        "effective_std": round(effective_std, 2),
        "horizon_hours": round(horizon_hours, 1),
        "z_score": round(z, 3),
    }


def compute_twc_range_edge(
    city: str,
    target_date: str,
    low_f: float,
    high_f: float,
    market_price: float,
    twc_high: Optional[float] = None,
) -> Optional[dict]:
    """Compute TWC resolution-source edge for a Polymarket bracket market.

    PM bracket markets ask "between 92-93F" - YES pays if actual falls in range.
    Uses TWC's per-city RMSE as std (not the ensemble std floor).

    Args:
        twc_high: Pre-fetched TWC high temp. If None, fetches via get_ensemble_forecast.
    """
    if twc_high is None:
        try:
            from signals.weather_ensemble import get_ensemble_forecast
            ens = get_ensemble_forecast(city, target_date)
            if not ens:
                return None
            twc_data = ens.get("sources", {}).get("weather_com")
            if not twc_data:
                return None
            twc_high = twc_data.get("high_f")
        except Exception:
            return None

    if twc_high is None:
        return None

    now = datetime.now(timezone.utc)
    target_dt = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    horizon_hours = max(0, (target_dt - now).total_seconds() / 3600)

    # Use TWC RMSE as std (same fix as compute_twc_edge)
    twc_rmse = get_source_rmse(city, "weather_com")
    effective_std = max(twc_rmse, 1.0) if twc_rmse else 2.0

    # Probability that actual falls in [low, high]
    prob = norm.cdf((high_f - twc_high) / effective_std) - norm.cdf((low_f - twc_high) / effective_std)
    prob = max(0.0, min(1.0, prob))

    edge_pp = (prob - market_price) * 100
    direction = "buy_yes" if edge_pp > 0 else "buy_no"
    abs_edge = abs(edge_pp)

    if abs_edge > 8:
        conviction = "HIGH"
    elif abs_edge > 5:
        conviction = "MEDIUM"
    else:
        conviction = "LOW"

    return {
        "city": city.lower(),
        "target_date": target_date,
        "platform": "polymarket",
        "resolution_source": "twc",
        "market_type": "bracket",
        "twc_forecast_f": round(twc_high, 1),
        "bracket_low_f": low_f,
        "bracket_high_f": high_f,
        "twc_implied_prob": round(prob, 4),
        "market_price": market_price,
        "edge_pp": round(edge_pp, 1),
        "direction": direction,
        "conviction_tier": conviction,
        "twc_rmse": twc_rmse,
        "effective_std": round(effective_std, 2),
        "horizon_hours": round(horizon_hours, 1),
    }


# ── Forecast Delta Tracking ──────────────────────────────────────────────

def _ensure_delta_table():
    """Create resolution_edge_delta table if not exists."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS resolution_edge_delta (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city TEXT NOT NULL,
                target_date TEXT NOT NULL,
                source TEXT NOT NULL,
                forecast_f REAL NOT NULL,
                previous_f REAL,
                delta_f REAL,
                scanned_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_red_city_date
            ON resolution_edge_delta(city, target_date, source)
        """)
        conn.commit()
        conn.close()
    except Exception:
        pass

_ensure_delta_table()


def track_forecast_delta(city: str, target_date: str, source: str, forecast_f: float) -> Optional[float]:
    """Track forecast changes. Returns delta from previous reading, or None if first."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        prev = conn.execute("""
            SELECT forecast_f FROM resolution_edge_delta
            WHERE city = ? AND target_date = ? AND source = ?
            ORDER BY scanned_at DESC LIMIT 1
        """, (city.lower(), target_date, source)).fetchone()

        delta = None
        previous = None
        if prev:
            previous = prev[0]
            delta = round(forecast_f - previous, 1)

        conn.execute("""
            INSERT INTO resolution_edge_delta (city, target_date, source, forecast_f, previous_f, delta_f)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (city.lower(), target_date, source, forecast_f, previous, delta))
        conn.commit()
        conn.close()
        return delta
    except Exception:
        return None


# ── Unified Scanner ──────────────────────────────────────────────────────

def scan_resolution_edges() -> List[dict]:
    """Scan all active weather markets for resolution-source edge signals.

    For Kalshi markets → compute NWS edge (NWS = resolution source)
    For PM markets → compute TWC edge (TWC = resolution source)

    Returns list of edge signals with conviction tiers.
    """
    signals = []

    # Scan Kalshi weather markets
    try:
        _scan_kalshi_edges(signals)
    except Exception as e:
        logger.warning("Kalshi edge scan error: %s", e)

    # Scan Polymarket weather markets
    try:
        _scan_polymarket_edges(signals)
    except Exception as e:
        logger.warning("PM edge scan error: %s", e)

    # Sort by absolute edge
    signals.sort(key=lambda s: abs(s.get("edge_pp", 0)), reverse=True)

    return signals


def _scan_kalshi_edges(signals: list):
    """Scan Kalshi weather markets for NWS resolution-source edge."""
    KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

    # Per-city high temp series
    high_series = [
        ("KXHIGHTATL", "atlanta"), ("KXHIGHTBOS", "boston"),
        ("KXHIGHTDAL", "dallas"), ("KXHIGHTHOU", "houston"),
        ("KXHIGHTSEA", "seattle"), ("KXHIGHTSFO", "san francisco"),
        ("KXHIGHTLV", "las vegas"), ("KXHIGHTPHX", "phoenix"),
        ("KXHIGHTMIN", "minneapolis"), ("KXHIGHTNOLA", "new orleans"),
        ("KXHIGHTOKC", "oklahoma city"), ("KXHIGHTSATX", "san antonio"),
    ]

    for series_ticker, city in high_series:
        try:
            r = requests.get(
                f"{KALSHI_API}/events?series_ticker={series_ticker}&status=open&limit=10&with_nested_markets=true",
                headers={"accept": "application/json"}, timeout=10,
            )
            if r.status_code != 200:
                continue
            events = r.json().get("events", [])

            for event in events:
                for market in event.get("markets", []):
                    title = market.get("title", "")
                    yes_price = market.get("yes_price", 0) / 100.0
                    volume = market.get("volume", 0)

                    if volume < 50 or yes_price <= 0.01 or yes_price >= 0.99:
                        continue

                    # Parse threshold from title: "Will the maximum temperature be <83°"
                    threshold_match = re.search(r'([<>≥≤])\s*(\d+)°', title)
                    if not threshold_match:
                        # Try range: "between X° and Y°"
                        range_match = re.search(r'between\s+(\d+)°?\s+and\s+(\d+)°', title, re.I)
                        if range_match:
                            continue  # Skip ranges for now — need range probability
                        continue

                    operator = threshold_match.group(1)
                    threshold = float(threshold_match.group(2))
                    target_date = _extract_date_from_kalshi_event(event)
                    if not target_date:
                        continue

                    # Determine comparison direction
                    if operator in ("<", "≤"):
                        comparison = "below"
                        market_price = yes_price  # YES = below threshold
                    else:
                        comparison = "above"
                        market_price = yes_price

                    edge = compute_nws_edge(city, target_date, threshold, market_price, comparison)
                    if edge and abs(edge["edge_pp"]) >= MIN_EDGE_PP:
                        edge["market_id"] = market.get("ticker", "")
                        edge["market_title"] = title
                        edge["volume"] = volume

                        # Track forecast delta
                        delta = track_forecast_delta(city, target_date, "nws",
                                                     edge["nws_raw_max_f"])
                        edge["forecast_delta"] = delta

                        signals.append(edge)
        except Exception as e:
            logger.debug("Kalshi edge scan for %s: %s", series_ticker, e)


def _scan_polymarket_edges(signals: list):
    """Scan Polymarket weather markets for TWC resolution-source edge."""
    try:
        from signals.weather_scanner import CITY_COORDS
    except ImportError:
        return

    GAMMA_API = "https://gamma-api.polymarket.com"

    def _fetch_json(url: str, timeout: int = 15) -> Optional[dict]:
        """Fetch JSON via curl (urllib hangs on VPS)."""
        import subprocess, json
        try:
            result = subprocess.run(
                ["curl", "-s", "-m", str(timeout), url],
                capture_output=True, text=True, timeout=timeout + 5
            )
            if result.returncode == 0 and result.stdout:
                return json.loads(result.stdout)
            elif result.returncode != 0:
                logger.warning("PM scan curl failed (exit=%d): %s", result.returncode, url[:80])
            elif not result.stdout:
                logger.warning("PM scan curl empty response: %s", url[:80])
        except json.JSONDecodeError as e:
            logger.warning("PM scan JSON parse error: %s — %s", e, url[:80])
        except subprocess.TimeoutExpired:
            logger.warning("PM scan curl timeout (%ds): %s", timeout, url[:80])
        except Exception as e:
            logger.warning("PM scan fetch error: %s — %s", e, url[:80])
        return None

    # Hardcoded US city slugs — skip _discover_weather_cities() which makes 29 Gamma
    # probes just for discovery (17s each = 68s wall time, blows 90s timeout).
    # TWC API key only works for US ICAO stations anyway.
    US_CITY_SLUGS = [
        "chicago", "miami", "dallas", "atlanta", "seattle",
        "houston", "new-york", "los-angeles", "phoenix", "denver",
        "boston", "san-francisco", "austin", "philadelphia", "san-antonio",
    ]

    now = datetime.now(timezone.utc)

    month_names = {
        1: 'january', 2: 'february', 3: 'march', 4: 'april',
        5: 'may', 6: 'june', 7: 'july', 8: 'august',
        9: 'september', 10: 'october', 11: 'november', 12: 'december',
    }

    # Batch TWC data: fetch once per city+date, cache for all markets
    _twc_cache: Dict[str, Optional[float]] = {}  # key = "city|date" -> twc_high_f

    def _get_twc_high(city_name: str, target_date: str) -> Optional[float]:
        """Get TWC high temp for a city+date, cached per scan."""
        key = f"{city_name}|{target_date}"
        if key in _twc_cache:
            return _twc_cache[key]
        try:
            from signals.weather_ensemble import _fetch_weather_com
            coords = CITY_COORDS.get(city_name)
            if not coords:
                _twc_cache[key] = None
                return None
            lat, lon = coords[0], coords[1]
            twc = _fetch_weather_com(lat, lon, target_date, city_name)
            if twc:
                val = twc.get("high_f")
                _twc_cache[key] = val
                return val
        except Exception:
            pass
        _twc_cache[key] = None
        return None

    # Build list of (city_slug, city_name, target_date, slug) tuples to fetch
    from concurrent.futures import ThreadPoolExecutor, as_completed

    fetch_tasks = []
    # Scan tomorrow + day-after (2 days). Day 3 rarely has live markets.
    for days_ahead in range(1, 3):
        dt = now + timedelta(days=days_ahead)
        target_date = dt.strftime("%Y-%m-%d")
        month = month_names[dt.month]
        day = dt.day
        year = dt.year

        for city_slug in US_CITY_SLUGS:
            city_name = city_slug.replace('-', ' ')
            slug = f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"
            fetch_tasks.append((city_slug, city_name, target_date, slug))

    # Parallel Gamma API fetches — max_workers=20 to fire all 30 calls at once
    # Each Gamma call takes ~17s. With 20 workers, 30 tasks ≈ 2 rounds × 17s = ~34s.
    gamma_results: Dict[str, tuple] = {}  # key = "city|date" -> (city_slug, city_name, target_date, data)

    def _fetch_gamma(slug: str, city_slug: str, city_name: str, target_date: str):
        url = f"{GAMMA_API}/events?slug={slug}"
        data = _fetch_json(url, timeout=45)
        if data:
            return (city_slug, city_name, target_date, data)
        return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_gamma, slug, cs, cn, td): (cs, cn, td)
                   for cs, cn, td, slug in fetch_tasks}
        for future in as_completed(futures):
            result = future.result()
            if result:
                cs, cn, td, data = result
                gamma_results[f"{cn}|{td}"] = (cs, cn, td, data)

    logger.info("PM weather scan: %d/%d Gamma slugs returned data", len(gamma_results), len(fetch_tasks))

    # Process results
    for (city_slug, city_name, target_date, data) in gamma_results.values():
        # Pre-fetch TWC data once for this city+date
        twc_high = _get_twc_high(city_name, target_date)
        if twc_high is None:
            continue

        events = data if isinstance(data, list) else [data]
        for event in events:
            for market in event.get("markets", []):
                question = market.get("question", "") or market.get("groupItemTitle", "")
                outcomes = market.get("outcomePrices", "")

                # Parse YES price
                try:
                    import json as _json
                    if isinstance(outcomes, str):
                        prices = [float(p) for p in _json.loads(outcomes)]
                    elif isinstance(outcomes, list):
                        prices = [float(p) for p in outcomes]
                    else:
                        continue
                    yes_price = prices[0] if prices else None
                except (ValueError, IndexError, TypeError):
                    continue

                if yes_price is None or yes_price <= 0.02 or yes_price >= 0.98:
                    continue

                # Parse threshold from question
                is_celsius = '°C' in question or 'celsius' in question.lower()
                threshold_match = re.search(r'(\d+)\s*°[FC]?\s*(or higher|or more|or above|\+)', question, re.I)
                below_match = re.search(r'(\d+)\s*°[FC]?\s*(or below|or less|or under)', question, re.I)
                if not below_match:
                    below_match = re.search(r'(below|under|less than)\s*(\d+)\s*°', question, re.I)
                bracket_match = re.search(r'between\s+(\d+)\s*(?:°[FC]?\s*)?[-–]\s*(\d+)\s*°[FC]?', question, re.I)

                if threshold_match:
                    threshold = float(threshold_match.group(1))
                    if is_celsius:
                        threshold = _c_to_f(threshold)
                    comparison = "above"
                    market_price = yes_price
                elif below_match:
                    g1 = below_match.group(1)
                    g2 = below_match.group(2) if below_match.lastindex >= 2 else None
                    threshold = float(g1 if g1 and g1.lstrip('-').isdigit() else g2)
                    if is_celsius:
                        threshold = _c_to_f(threshold)
                    comparison = "below"
                    market_price = yes_price
                elif bracket_match:
                    low = float(bracket_match.group(1))
                    high = float(bracket_match.group(2))
                    if is_celsius:
                        low = _c_to_f(low)
                        high = _c_to_f(high)
                    edge = compute_twc_range_edge(city_name, target_date, low, high, yes_price, twc_high=twc_high)
                    if edge and abs(edge["edge_pp"]) >= MIN_EDGE_PP:
                        edge["market_id"] = market.get("id", "")
                        edge["condition_id"] = market.get("conditionId", "")
                        edge["market_title"] = question
                        # Reconstruct slug from current city+date (not the stale loop variable)
                        edge["slug"] = f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"
                        signals.append(edge)
                    continue
                else:
                    continue

                edge = compute_twc_edge(city_name, target_date, threshold, market_price, comparison, twc_high=twc_high)
                if edge and abs(edge["edge_pp"]) >= MIN_EDGE_PP:
                    edge["market_id"] = market.get("id", "")
                    edge["condition_id"] = market.get("conditionId", "")
                    edge["market_title"] = question
                    # Reconstruct slug from current city+date (not the stale loop variable)
                    edge["slug"] = f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"
                    delta = track_forecast_delta(city_name, target_date, "twc", edge["twc_forecast_f"])
                    edge["forecast_delta"] = delta
                    signals.append(edge)


def _extract_date_from_kalshi_event(event: dict) -> Optional[str]:
    """Extract target date from Kalshi event title like 'Highest temperature in Atlanta on Jun 23, 2026?'"""
    title = event.get("title", "")
    match = re.search(r'on\s+(\w+)\s+(\d+),?\s*(\d{4})', title)
    if not match:
        return None
    month_str, day, year = match.group(1), match.group(2), match.group(3)
    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    month_num = months.get(month_str[:3].lower())
    if not month_num:
        return None
    return f"{year}-{month_num:02d}-{int(day):02d}"


# ── CLI / API ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    signals = scan_resolution_edges()
    print(f"\nResolution-source edge scan: {len(signals)} signals")
    for s in signals:
        src = s.get("resolution_source", "?")
        city = s.get("city", "?")
        edge = s.get("edge_pp", 0)
        conv = s.get("conviction_tier", "?")
        direction = s.get("direction", "?")
        threshold = s.get("threshold_f", "?")
        print(f"  [{conv}] {city:15s} {src} edge={edge:+.1f}pp {direction} (thr={threshold}°F)")

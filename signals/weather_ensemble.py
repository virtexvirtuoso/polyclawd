"""
Weather Ensemble — multi-source forecast aggregator with calibrated probabilities.

Sources:
  1. Open-Meteo Ensemble (30+ models, no key)
  2. Pirate Weather (GEFS/ECMWF/HRRR, free key)
  3. Tomorrow.io (proprietary AI, free key)
  4. WeatherAPI.com (station blend, free key)
  5. Weather.com / TWC (resolution source, 1.5x weight) — HIGHEST PRIORITY
     This is the exact backend Polymarket resolves against via Weather Underground.
  6. Bright Sky / DWD MOSMIX (statistical MOS of ICON+ECMWF, no key)
  7. Met Office DataHub (UKMO Unified Model, free key)
  8. AccuWeather (SWIFT engine + 100 human meteorologists, trial key)
  9. Visual Crossing (ECMWF+GFS+GDPS blend + station obs, free key)
 10. OpenWeatherMap (multi-model blend, One Call 3.0, free key)
 11. Meteosource (ML-blended 6+ NWP models, free key)

Produces probability distributions for temperature markets instead of
hardcoded fair-value buckets.
"""

import asyncio
import json
import logging
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── API Keys (from env) ─────────────────────────────────────────────────
PIRATE_API_KEY = os.environ.get("PIRATE_WEATHER_KEY", "")
TOMORROW_API_KEY = os.environ.get("TOMORROW_IO_KEY", "")
WEATHERAPI_KEY = os.environ.get("WEATHERAPI_KEY", "")
METOFFICE_API_KEY = os.environ.get("METOFFICE_API_KEY", "")
ACCUWEATHER_API_KEY = os.environ.get("ACCUWEATHER_API_KEY", "")
VISUALCROSSING_API_KEY = os.environ.get("VISUALCROSSING_API_KEY", "")
OPENWEATHERMAP_API_KEY = os.environ.get("OPENWEATHERMAP_API_KEY", "")
METEOSOURCE_API_KEY = os.environ.get("METEOSOURCE_API_KEY", "")

# ── City coordinates ─────────────────────────────────────────────────────
CITIES: Dict[str, Tuple[float, float, str]] = {
    "nyc": (40.71, -74.01, "America/New_York"),
    "new york": (40.71, -74.01, "America/New_York"),
    "london": (51.51, -0.13, "Europe/London"),
    "buenos aires": (-34.60, -58.38, "America/Argentina/Buenos_Aires"),
    "wellington": (-41.29, 174.78, "Pacific/Auckland"),
    "miami": (25.76, -80.19, "America/New_York"),
    "dallas": (32.78, -96.80, "America/Chicago"),
    "atlanta": (33.75, -84.39, "America/New_York"),
    "sao paulo": (-23.55, -46.63, "America/Sao_Paulo"),
    "são paulo": (-23.55, -46.63, "America/Sao_Paulo"),
    "toronto": (43.65, -79.38, "America/Toronto"),
    "seoul": (37.57, 126.98, "Asia/Seoul"),
    "seattle": (47.61, -122.33, "America/Los_Angeles"),
    "chicago": (41.88, -87.63, "America/Chicago"),
    "paris": (48.86, 2.35, "Europe/Paris"),
    "sydney": (-33.87, 151.21, "Australia/Sydney"),
    "tokyo": (35.68, 139.69, "Asia/Tokyo"),
    # Extended US cities
    "los angeles": (34.05, -118.24, "America/Los_Angeles"),
    "houston": (29.76, -95.37, "America/Chicago"),
    "phoenix": (33.45, -112.07, "America/Phoenix"),
    "denver": (39.74, -104.99, "America/Denver"),
    "boston": (42.36, -71.06, "America/New_York"),
    "san francisco": (37.77, -122.42, "America/Los_Angeles"),
    "washington": (38.91, -77.04, "America/New_York"),
    "dc": (38.91, -77.04, "America/New_York"),
    "philadelphia": (39.95, -75.17, "America/New_York"),
    "san diego": (32.72, -117.16, "America/Los_Angeles"),
    "austin": (30.27, -97.74, "America/Chicago"),
    "berlin": (52.52, 13.41, "Europe/Berlin"),
    "ankara": (39.93, 32.86, "Europe/Istanbul"),
    "lucknow": (26.85, 80.95, "Asia/Kolkata"),
    "munich": (48.14, 11.58, "Europe/Berlin"),
}

# ── Ensemble models to request from Open-Meteo ──────────────────────────
# These are genuinely independent weather models from different agencies
ENSEMBLE_MODELS = [
    "icon_seamless",       # DWD Germany
    "gfs_seamless",        # NOAA USA (GFS)
    "ecmwf_ifs04",         # ECMWF European
    "gem_global",          # Canada
    "bom_access_global",   # Australia BOM
    "jma_seamless",        # Japan Meteorological Agency
    "kma_seamless",        # Korea Meteorological Administration
    "cma_grapes_global",   # China Meteorological Administration
]

# ── Cache ────────────────────────────────────────────────────────────────
_cache: Dict[str, dict] = {}
_cache_ts: Dict[str, float] = {}
CACHE_TTL = 900  # 15 min — faster refresh catches forecast updates sooner (edge decays quickly)
MAX_CACHE_SIZE = 500  # per cache dict — evict oldest when exceeded

# All cache pairs for centralized eviction
_ALL_CACHES: List[Tuple] = []  # populated after declarations below

_last_eviction_ts = 0.0
_EVICTION_INTERVAL = 300  # sweep every 5 min


def _evict_expired():
    """Sweep all caches and remove expired entries. Called periodically."""
    global _last_eviction_ts
    now = time.time()
    if now - _last_eviction_ts < _EVICTION_INTERVAL:
        return
    _last_eviction_ts = now
    total_evicted = 0
    for data_dict, ts_dict, ttl in _ALL_CACHES:
        expired = [k for k, ts in ts_dict.items() if now - ts > ttl]
        for k in expired:
            data_dict.pop(k, None)
            ts_dict.pop(k, None)
        total_evicted += len(expired)
        # Hard cap: if still too large, evict oldest
        if len(data_dict) > MAX_CACHE_SIZE:
            sorted_keys = sorted(ts_dict, key=ts_dict.get)
            to_remove = sorted_keys[:len(data_dict) - MAX_CACHE_SIZE]
            for k in to_remove:
                data_dict.pop(k, None)
                ts_dict.pop(k, None)
            total_evicted += len(to_remove)
    if total_evicted > 0:
        logger.info("Cache eviction: removed %d expired entries", total_evicted)


# Rate limit tracking per source
# Note: Pirate & WeatherAPI return multi-day forecasts per call, so each call
# covers all 7 days for one city. With 44 cities we need ~44 calls/scan.
_rate_limits = {
    "pirate_weather": {"calls": 0, "reset_ts": 0, "max_per_hour": 50, "max_per_month": 10000},
    "tomorrow_io": {"calls": 0, "reset_ts": 0, "max_per_hour": 20, "max_per_day": 450},
    "weatherapi": {"calls": 0, "reset_ts": 0, "max_per_hour": 50, "max_per_month": 95000},
    "metoffice": {"calls": 0, "reset_ts": 0, "max_per_hour": 15, "max_per_day": 360},
    "accuweather": {"calls": 0, "reset_ts": 0, "max_per_hour": 10, "max_per_day": 50},
    "visualcrossing": {"calls": 0, "reset_ts": 0, "max_per_hour": 50, "max_per_day": 1000},
    "openweathermap": {"calls": 0, "reset_ts": 0, "max_per_hour": 40, "max_per_day": 1000},
    "meteosource": {"calls": 0, "reset_ts": 0, "max_per_hour": 10, "max_per_day": 400},
}

def _rate_check(source: str) -> bool:
    """Check if we can make another API call for this source."""
    if source not in _rate_limits:
        return True
    rl = _rate_limits[source]
    now = time.time()
    # Reset hourly counter
    if now - rl["reset_ts"] > 3600:
        rl["calls"] = 0
        rl["reset_ts"] = now
    return rl["calls"] < rl["max_per_hour"]

def _rate_track(source: str):
    """Record an API call for rate limiting."""
    if source in _rate_limits:
        _rate_limits[source]["calls"] += 1


def _cache_key(city: str, date: str) -> str:
    return f"{city.lower()}:{date}"


def _cache_get(city: str, date: str) -> Optional[dict]:
    _evict_expired()
    key = _cache_key(city, date)
    if key in _cache and (time.time() - _cache_ts.get(key, 0)) < CACHE_TTL:
        return _cache[key]
    return None


def _cache_set(city: str, date: str, data: dict):
    key = _cache_key(city, date)
    _cache[key] = data
    _cache_ts[key] = time.time()


# ── HTTP helper ──────────────────────────────────────────────────────────

def _fetch_json(url: str, timeout: int = 12, headers: dict = None) -> Optional[dict]:
    try:
        hdrs = {"User-Agent": "Polyclawd-WeatherEnsemble/1.0"}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug("fetch failed %s: %s", url, e)
        return None


def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


# ── Source 1: Open-Meteo Ensemble (PRIMARY — no key needed) ─────────────

# Circuit breaker: time-based backoff on Open-Meteo failures
_open_meteo_lock = threading.Lock()
_open_meteo_blocked_until = 0.0  # Unix timestamp — skip all requests until this time
_open_meteo_consecutive_fails = 0
_OPEN_METEO_BACKOFF = [60, 300, 900, 1800]  # 1min, 5min, 15min, 30min max

def _fetch_open_meteo_ensemble(lat: float, lon: float, date: str) -> Optional[dict]:
    """
    Fetch ensemble forecasts from multiple independent models.
    Returns dict with high temps from each ensemble member.
    """
    global _open_meteo_blocked_until, _open_meteo_consecutive_fails
    now = time.time()
    if now < _open_meteo_blocked_until:
        return None  # Still in backoff window — skip silently
    models_param = ",".join(ENSEMBLE_MODELS)
    url = (
        f"https://ensemble-api.open-meteo.com/v1/ensemble"
        f"?latitude={lat}&longitude={lon}"
        f"&daily=temperature_2m_max,temperature_2m_min"
        f"&models={models_param}"
        f"&timezone=auto"
        f"&start_date={date}&end_date={date}"
    )
    data = _fetch_json(url, timeout=15)
    if not data:
        with _open_meteo_lock:
            _open_meteo_consecutive_fails += 1
            backoff_idx = min(_open_meteo_consecutive_fails - 1, len(_OPEN_METEO_BACKOFF) - 1)
            backoff_secs = _OPEN_METEO_BACKOFF[backoff_idx]
            _open_meteo_blocked_until = time.time() + backoff_secs
            logger.info("Open-Meteo blocked for %ds (fail #%d)", backoff_secs, _open_meteo_consecutive_fails)
        return None

    # Success — reset circuit breaker
    with _open_meteo_lock:
        if _open_meteo_consecutive_fails > 0:
            logger.info("Open-Meteo recovered after %d failures", _open_meteo_consecutive_fails)
            _open_meteo_consecutive_fails = 0
            _open_meteo_blocked_until = 0.0

    highs_c = []
    lows_c = []
    models_used = []

    daily = data.get("daily", {})
    
    # Open-Meteo ensemble format: keys like
    #   temperature_2m_max_icon_seamless_eps (control run)
    #   temperature_2m_max_member01_icon_seamless_eps (ensemble member)
    #   temperature_2m_max_ncep_gefs_seamless (control)
    #   temperature_2m_max_member01_ncep_gefs_seamless (member)
    # We want ALL values — control runs + all members from all models
    
    for key, vals in daily.items():
        if key == "time":
            continue
        if not vals or vals[0] is None:
            continue
        
        if "temperature_2m_max" in key:
            highs_c.append(vals[0])
            # Track which model family this belongs to
            for model in ENSEMBLE_MODELS:
                if model in key or model.replace("_", "") in key.replace("_", ""):
                    if model not in models_used:
                        models_used.append(model)
                    break
        elif "temperature_2m_min" in key:
            lows_c.append(vals[0])

    if not highs_c:
        logger.debug("Open-Meteo ensemble returned no highs for %s", date)
        return _fetch_open_meteo_ensemble_fallback(lat, lon, date)

    highs_f = [_c_to_f(c) for c in highs_c]
    lows_f = [_c_to_f(c) for c in lows_c] if lows_c else []

    mean_high = sum(highs_f) / len(highs_f)
    std_high = (sum((h - mean_high) ** 2 for h in highs_f) / len(highs_f)) ** 0.5 if len(highs_f) > 1 else 2.0

    sorted_highs = sorted(highs_f)
    n = len(sorted_highs)

    return {
        "source": "open_meteo_ensemble",
        "high_f": round(mean_high, 1),
        "high_std_f": round(max(std_high, 0.5), 2),  # Floor at 0.5°F
        "low_f": round(sum(lows_f) / len(lows_f), 1) if lows_f else None,
        "p10_f": round(sorted_highs[max(0, int(0.1 * n))], 1),
        "p90_f": round(sorted_highs[min(n - 1, int(0.9 * n))], 1),
        "n_members": n,
        "models": models_used,
        "raw_highs_f": [round(h, 1) for h in highs_f],
    }


def _fetch_open_meteo_ensemble_fallback(lat: float, lon: float, date: str) -> Optional[dict]:
    """Fallback: fetch each model individually from standard Open-Meteo API."""
    highs_f = []
    models_used = []
    
    for model in ENSEMBLE_MODELS:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            f"&daily=temperature_2m_max,temperature_2m_min"
            f"&models={model}"
            f"&timezone=auto"
            f"&start_date={date}&end_date={date}"
        )
        data = _fetch_json(url, timeout=8)
        if data and "daily" in data:
            daily = data["daily"]
            maxes = daily.get("temperature_2m_max", [])
            if maxes and maxes[0] is not None:
                highs_f.append(_c_to_f(maxes[0]))
                models_used.append(model)

    if not highs_f:
        return None

    mean_high = sum(highs_f) / len(highs_f)
    std_high = (sum((h - mean_high) ** 2 for h in highs_f) / len(highs_f)) ** 0.5 if len(highs_f) > 1 else 2.0
    sorted_highs = sorted(highs_f)
    n = len(sorted_highs)

    return {
        "source": "open_meteo_multi_model",
        "high_f": round(mean_high, 1),
        "high_std_f": round(max(std_high, 0.5), 2),
        "low_f": None,
        "p10_f": round(sorted_highs[max(0, int(0.1 * n))], 1),
        "p90_f": round(sorted_highs[min(n - 1, int(0.9 * n))], 1),
        "n_members": n,
        "models": models_used,
        "raw_highs_f": [round(h, 1) for h in highs_f],
    }


# ── Source 2: Pirate Weather ────────────────────────────────────────────
# Returns 7 days in one call — cache all days per city

_pirate_cache: Dict[str, dict] = {}  # "lat,lon" → {date_str: result}
_pirate_cache_ts: Dict[str, float] = {}

def _fetch_pirate_weather(lat: float, lon: float, date: str) -> Optional[dict]:
    if not PIRATE_API_KEY:
        return None
    
    loc_key = f"{lat},{lon}"
    if loc_key in _pirate_cache and (time.time() - _pirate_cache_ts.get(loc_key, 0)) < CACHE_TTL:
        return _pirate_cache[loc_key].get(date)
    
    if not _rate_check("pirate_weather"):
        logger.debug("Pirate Weather rate limited, skipping")
        return None
    url = (
        f"https://api.pirateweather.net/forecast/{PIRATE_API_KEY}"
        f"/{lat},{lon}?extend=hourly&units=us"
    )
    data = _fetch_json(url, timeout=10)
    if not data or "daily" not in data:
        return None

    _rate_track("pirate_weather")
    city_days = {}
    for day in data["daily"].get("data", []):
        day_dt = datetime.fromtimestamp(day["time"], tz=timezone.utc).date()
        day_str = day_dt.strftime("%Y-%m-%d")
        city_days[day_str] = {
            "source": "pirate_weather",
            "high_f": round(day.get("temperatureHigh", 0), 1),
            "high_std_f": None,
            "low_f": round(day.get("temperatureLow", 0), 1),
            "model": "GEFS+GFS+HRRR",
        }
    _pirate_cache[loc_key] = city_days
    _pirate_cache_ts[loc_key] = time.time()
    return city_days.get(date)


# ── Source 3: Tomorrow.io ────────────────────────────────────────────────
# Returns multi-day forecast — cache all days per city

_tomorrow_cache: Dict[str, dict] = {}
_tomorrow_cache_ts: Dict[str, float] = {}

def _fetch_tomorrow_io(lat: float, lon: float, date: str) -> Optional[dict]:
    if not TOMORROW_API_KEY:
        return None
    
    loc_key = f"{lat},{lon}"
    if loc_key in _tomorrow_cache and (time.time() - _tomorrow_cache_ts.get(loc_key, 0)) < CACHE_TTL:
        return _tomorrow_cache[loc_key].get(date)
    
    if not _rate_check("tomorrow_io"):
        logger.debug("Tomorrow.io rate limited, skipping")
        return None
    url = (
        f"https://api.tomorrow.io/v4/weather/forecast"
        f"?location={lat},{lon}"
        f"&timesteps=1d"
        f"&units=imperial"
        f"&apikey={TOMORROW_API_KEY}"
    )
    data = _fetch_json(url, timeout=10)
    if not data:
        return None
    _rate_track("tomorrow_io")

    # Cache ALL days from response
    timelines = data.get("timelines", {})
    daily = timelines.get("daily", [])
    city_days = {}
    
    for day in daily:
        try:
            day_dt = datetime.fromisoformat(day["time"].replace("Z", "+00:00")).date()
            day_str = day_dt.strftime("%Y-%m-%d")
            vals = day.get("values", {})
            city_days[day_str] = {
                "source": "tomorrow_io",
                "high_f": round(vals.get("temperatureMax", 0), 1),
                "high_std_f": None,
                "low_f": round(vals.get("temperatureMin", 0), 1),
                "model": "Tomorrow_AI",
            }
        except Exception:
            continue
    
    _tomorrow_cache[loc_key] = city_days
    _tomorrow_cache_ts[loc_key] = time.time()
    return city_days.get(date)


# ── Source 4: WeatherAPI.com ─────────────────────────────────────────────
# Always request max days (3 for free tier) — cache all days per city

_weatherapi_cache: Dict[str, dict] = {}
_weatherapi_cache_ts: Dict[str, float] = {}

def _fetch_weatherapi(lat: float, lon: float, date: str) -> Optional[dict]:
    if not WEATHERAPI_KEY:
        return None
    
    loc_key = f"{lat},{lon}"
    if loc_key in _weatherapi_cache and (time.time() - _weatherapi_cache_ts.get(loc_key, 0)) < CACHE_TTL:
        return _weatherapi_cache[loc_key].get(date)
    
    if not _rate_check("weatherapi"):
        logger.debug("WeatherAPI rate limited, skipping")
        return None

    # Always fetch 3 days (covers our today + next 2 days scan window)
    url = (
        f"http://api.weatherapi.com/v1/forecast.json"
        f"?key={WEATHERAPI_KEY}"
        f"&q={lat},{lon}"
        f"&days=3"
    )
    data = _fetch_json(url, timeout=10)
    if not data or "forecast" not in data:
        return None
    _rate_track("weatherapi")

    city_days = {}
    for day in data["forecast"].get("forecastday", []):
        d = day["day"]
        city_days[day["date"]] = {
            "source": "weatherapi",
            "high_f": round(d.get("maxtemp_f", 0), 1),
            "high_std_f": None,
            "low_f": round(d.get("mintemp_f", 0), 1),
            "model": "WeatherAPI_Blend",
        }
    _weatherapi_cache[loc_key] = city_days
    _weatherapi_cache_ts[loc_key] = time.time()
    return city_days.get(date)


# ── Source 5: Weather.com / TWC (resolution source — highest weight) ─────
# This is the EXACT data Polymarket resolves against (Weather Underground backend).
# Free public API key, ICAO station codes, 5-day forecast + historical.
# Double-weighted in ensemble because it IS the judge.

TWC_API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"  # Public key from WU website

# ICAO station codes for Polymarket weather cities
# These match the stations in Polymarket market descriptions
CITY_ICAO: Dict[str, str] = {
    "nyc": "KJFK", "new york": "KJFK",
    "miami": "KMIA",
    "dallas": "KDFW",
    "atlanta": "KATL",
    "seattle": "KSEA",
    "chicago": "KORD",
    "london": "EGLL",
    "buenos aires": "SAEZ",
    "wellington": "NZWN",
    "sao paulo": "SBGR", "são paulo": "SBGR",
    "toronto": "CYYZ",
    "seoul": "RKSS",
    "paris": "LFPG",
    "sydney": "YSSY",
    "tokyo": "RJTT",
    "los angeles": "KLAX",
    "houston": "KIAH",
    "phoenix": "KPHX",
    "denver": "KDEN",
    "boston": "KBOS",
    "san francisco": "KSFO",
    "washington": "KIAD", "dc": "KIAD",
    "austin": "KAUS",
    "berlin": "EDDB",
    "philadelphia": "KPHL",
    "san diego": "KSAN",
    "ankara": "LTAC",
    "lucknow": "VILK",
    "munich": "EDDM",
}

_twc_cache: Dict[str, dict] = {}
_twc_cache_ts: Dict[str, float] = {}
_actuals_cache: Dict[str, dict] = {}
_actuals_cache_ts: Dict[str, float] = {}
ACTUALS_CACHE_TTL = 3600  # 1h — actuals don't change

# Register all cache pairs for centralized eviction
_ALL_CACHES.extend([
    (_cache, _cache_ts, CACHE_TTL),
    (_pirate_cache, _pirate_cache_ts, CACHE_TTL),
    (_tomorrow_cache, _tomorrow_cache_ts, CACHE_TTL),
    (_weatherapi_cache, _weatherapi_cache_ts, CACHE_TTL),
    (_twc_cache, _twc_cache_ts, CACHE_TTL),
    (_actuals_cache, _actuals_cache_ts, ACTUALS_CACHE_TTL),
])


def _fetch_twc_actuals(city: str, date: str) -> Optional[dict]:
    """Fetch actual observed high/low from TWC historical observations.
    
    Used when the target date has already ended in the city's local timezone.
    Returns the REAL temperature — no forecast uncertainty, zero std.
    This is what Weather Underground will use to resolve the market.
    """
    city_lower = city.lower().strip()
    icao = CITY_ICAO.get(city_lower, "")
    if not icao:
        return None

    cache_key = f"{icao}:{date}"
    if cache_key in _actuals_cache and (time.time() - _actuals_cache_ts.get(cache_key, 0)) < ACTUALS_CACHE_TTL:
        return _actuals_cache[cache_key]

    # Country code lookup for the API URL
    # 2-char prefix → country (checked first), then 1-char fallback
    icao_cc_2 = {
        "SB": "BR", "SA": "AR", "EG": "GB", "ED": "DE", "LF": "FR",
        "NZ": "NZ", "YS": "AU", "RK": "KR", "RJ": "JP", "CY": "CA",
        "LT": "TR", "VI": "IN",
    }
    icao_cc_1 = {"K": "US", "C": "CA", "N": "NZ", "Y": "AU", "R": "KR"}
    prefix2 = icao[:2] if len(icao) >= 2 else ""
    prefix1 = icao[0] if icao else ""
    cc = icao_cc_2.get(prefix2) or icao_cc_1.get(prefix1, "US")

    date_compact = date.replace("-", "")  # "20260305"
    url = (
        f"https://api.weather.com/v1/location/{icao}:9:{cc}"
        f"/observations/historical.json"
        f"?apiKey={TWC_API_KEY}&units=e"
        f"&startDate={date_compact}&endDate={date_compact}"
    )

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/1.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        obs = data.get("observations", [])
        if not obs:
            return None

        temps = [o.get("temp") for o in obs if o.get("temp") is not None]
        if not temps:
            return None

        actual_high = max(temps)
        actual_low = min(temps)

        result = {
            "source": "twc_actuals",
            "high_f": round(float(actual_high), 1),
            "high_std_f": 0.0,  # Zero uncertainty — this is the real number
            "low_f": round(float(actual_low), 1),
            "model": f"TWC_OBS_{icao}",
            "icao": icao,
            "is_actual": True,
            "is_resolution_source": True,
            "n_observations": len(temps),
        }

        _actuals_cache[cache_key] = result
        _actuals_cache_ts[cache_key] = time.time()
        logger.info("TWC actuals %s/%s: high=%.1f°F low=%.1f°F (%d obs)",
                     icao, date, actual_high, actual_low, len(temps))
        return result

    except Exception as e:
        logger.debug("TWC actuals fetch failed for %s/%s: %s", icao, date, e)
        return None


def _date_has_ended(city: str, date: str) -> bool:
    """Check if the target date has fully ended in the city's local timezone."""
    coords = _resolve_city(city)
    if not coords:
        return False
    _, _, tz_name = coords

    try:
        # Get current time in city's timezone
        # Using UTC offset calculation (no pytz dependency)
        import subprocess
        result = subprocess.run(
            ["date", "+%Y-%m-%d", f"--date=TZ=\"{tz_name}\" now"],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode == 0:
            local_today = result.stdout.strip()
            return date < local_today
    except Exception:
        pass

    # Fallback: use known UTC offsets (approximate)
    tz_offsets = {
        "Pacific/Auckland": 13, "Australia/Sydney": 11, "Asia/Tokyo": 9,
        "Asia/Seoul": 9, "Europe/Paris": 1, "Europe/Berlin": 1,
        "Europe/London": 0, "America/Sao_Paulo": -3,
        "America/Argentina/Buenos_Aires": -3, "America/New_York": -5,
        "America/Toronto": -5, "America/Chicago": -6,
        "America/Denver": -7, "America/Phoenix": -7,
        "America/Los_Angeles": -8,
        "Europe/Istanbul": 3, "Asia/Kolkata": 5,
    }
    offset = tz_offsets.get(tz_name, 0)
    local_now = datetime.now(timezone.utc) + timedelta(hours=offset)
    local_today = local_now.strftime("%Y-%m-%d")
    return date < local_today


def _fetch_weather_com(lat: float, lon: float, date: str, city: str = "") -> Optional[dict]:
    """Fetch forecast from Weather.com (TWC) API — the WU resolution source.
    
    Uses ICAO station code for exact station match. Falls back to lat/lon.
    """
    city_lower = city.lower().strip()
    icao = CITY_ICAO.get(city_lower, "")
    if not icao:
        return None

    cache_key = icao
    if cache_key in _twc_cache and (time.time() - _twc_cache_ts.get(cache_key, 0)) < CACHE_TTL:
        return _twc_cache[cache_key].get(date)

    url = (
        f"https://api.weather.com/v3/wx/forecast/daily/5day"
        f"?icaoCode={icao}&units=e&language=en-US&format=json"
        f"&apiKey={TWC_API_KEY}"
    )
    data = _fetch_json(url, timeout=10)
    if not data:
        return None

    highs = data.get("temperatureMax", [])
    lows = data.get("temperatureMin", [])
    valid_times = data.get("validTimeLocal", [])

    city_days = {}
    for i, ts in enumerate(valid_times):
        if not ts:
            continue
        # validTimeLocal format: "2026-03-02T07:00:00-0500"
        day_str = ts[:10]
        h = highs[i] if i < len(highs) and highs[i] is not None else None
        l = lows[i] if i < len(lows) and lows[i] is not None else None
        if h is not None:
            city_days[day_str] = {
                "source": "weather_com",
                "high_f": round(float(h), 1),
                "high_std_f": None,
                "low_f": round(float(l), 1) if l is not None else None,
                "model": f"TWC_{icao}",
                "icao": icao,
                "is_resolution_source": True,
            }

    _twc_cache[cache_key] = city_days
    _twc_cache_ts[cache_key] = time.time()
    logger.debug("Weather.com %s: %d days fetched", icao, len(city_days))
    return city_days.get(date)


# ── Source 6: Bright Sky (DWD MOSMIX — free, no key) ─────────────────────
# Statistical MOS correction of ICON+ECMWF, optimized for station accuracy.
# Returns hourly data — we aggregate to daily max/min.

_brightsky_cache: Dict[str, dict] = {}
_brightsky_cache_ts: Dict[str, float] = {}

def _fetch_bright_sky(lat: float, lon: float, date: str) -> Optional[dict]:
    loc_key = f"{lat},{lon}"
    if loc_key in _brightsky_cache and (time.time() - _brightsky_cache_ts.get(loc_key, 0)) < CACHE_TTL:
        return _brightsky_cache[loc_key].get(date)

    # Fetch 7 days of hourly data in one call
    end_date = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d")
    url = (
        f"https://api.brightsky.dev/weather"
        f"?lat={lat}&lon={lon}&date={date}&last_date={end_date}&units=dwd"
    )
    data = _fetch_json(url, timeout=12)
    if not data or "weather" not in data:
        return None

    # Aggregate hourly temps into daily max/min
    from collections import defaultdict
    daily_temps: Dict[str, list] = defaultdict(list)
    for record in data["weather"]:
        ts = record.get("timestamp", "")
        temp = record.get("temperature")
        if ts and temp is not None:
            day_str = ts[:10]
            daily_temps[day_str].append(temp)

    city_days = {}
    for day_str, temps in daily_temps.items():
        if temps:
            high_c = max(temps)
            low_c = min(temps)
            city_days[day_str] = {
                "source": "bright_sky",
                "high_f": round(_c_to_f(high_c), 1),
                "high_std_f": None,
                "low_f": round(_c_to_f(low_c), 1),
                "model": "DWD_MOSMIX",
            }

    _brightsky_cache[loc_key] = city_days
    _brightsky_cache_ts[loc_key] = time.time()
    logger.debug("Bright Sky: %d days fetched for %.2f,%.2f", len(city_days), lat, lon)
    return city_days.get(date)


# ── Source 7: Met Office DataHub (UKMO Unified Model — free key) ─────────
# Completely independent global NWP model — the "big four" model missing
# from the rest of our stack.

_metoffice_cache: Dict[str, dict] = {}
_metoffice_cache_ts: Dict[str, float] = {}

def _fetch_met_office(lat: float, lon: float, date: str) -> Optional[dict]:
    if not METOFFICE_API_KEY:
        return None

    loc_key = f"{lat},{lon}"
    if loc_key in _metoffice_cache and (time.time() - _metoffice_cache_ts.get(loc_key, 0)) < CACHE_TTL:
        return _metoffice_cache[loc_key].get(date)

    if not _rate_check("metoffice"):
        logger.debug("Met Office rate limited, skipping")
        return None

    url = (
        f"https://data.hub.api.metoffice.gov.uk/sitespecific/v0/point/daily"
        f"?latitude={lat}&longitude={lon}&excludeParameterMetadata=true"
    )
    data = _fetch_json(url, timeout=12, headers={"apikey": METOFFICE_API_KEY})
    if not data:
        return None
    _rate_track("metoffice")

    city_days = {}
    try:
        features = data.get("features", [])
        if not features:
            return None
        time_series = features[0].get("properties", {}).get("timeSeries", [])
        for entry in time_series:
            ts = entry.get("time", "")
            day_str = ts[:10] if ts else ""
            high_c = entry.get("dayMaxScreenTemperature")
            low_c = entry.get("nightMinScreenTemperature")
            if day_str and high_c is not None:
                city_days[day_str] = {
                    "source": "met_office",
                    "high_f": round(_c_to_f(high_c), 1),
                    "high_std_f": None,
                    "low_f": round(_c_to_f(low_c), 1) if low_c is not None else None,
                    "model": "UKMO_Unified",
                }
    except Exception as e:
        logger.debug("Met Office parse error: %s", e)
        return None

    _metoffice_cache[loc_key] = city_days
    _metoffice_cache_ts[loc_key] = time.time()
    logger.debug("Met Office: %d days fetched for %.2f,%.2f", len(city_days), lat, lon)
    return city_days.get(date)


# ── Source 8: AccuWeather (SWIFT engine + human meteorologists) ──────────
# Only source with a 100+ meteorologist layer reviewing output.
# Very tight free tier (50 calls/day) — cache location keys permanently.

_accu_cache: Dict[str, dict] = {}
_accu_cache_ts: Dict[str, float] = {}
_accu_location_keys: Dict[str, str] = {}  # "lat,lon" → location key (permanent cache)

def _fetch_accuweather(lat: float, lon: float, date: str) -> Optional[dict]:
    if not ACCUWEATHER_API_KEY:
        return None

    loc_key = f"{lat},{lon}"
    if loc_key in _accu_cache and (time.time() - _accu_cache_ts.get(loc_key, 0)) < CACHE_TTL:
        return _accu_cache[loc_key].get(date)

    if not _rate_check("accuweather"):
        logger.debug("AccuWeather rate limited, skipping")
        return None

    # Step 1: Get location key (cached permanently in memory)
    if loc_key not in _accu_location_keys:
        geo_url = (
            f"https://dataservice.accuweather.com/locations/v1/cities/geoposition/search"
            f"?apikey={ACCUWEATHER_API_KEY}&q={lat},{lon}"
        )
        geo_data = _fetch_json(geo_url, timeout=10)
        if not geo_data or "Key" not in geo_data:
            return None
        _rate_track("accuweather")
        _accu_location_keys[loc_key] = geo_data["Key"]

    location_key = _accu_location_keys[loc_key]

    # Step 2: Get 5-day forecast (returns Fahrenheit by default)
    forecast_url = (
        f"https://dataservice.accuweather.com/forecasts/v1/daily/5day/{location_key}"
        f"?apikey={ACCUWEATHER_API_KEY}"
    )
    data = _fetch_json(forecast_url, timeout=10)
    if not data:
        return None
    _rate_track("accuweather")

    city_days = {}
    for day in data.get("DailyForecasts", []):
        day_date = day.get("Date", "")[:10]
        temp = day.get("Temperature", {})
        high_f = temp.get("Maximum", {}).get("Value")
        low_f = temp.get("Minimum", {}).get("Value")
        if day_date and high_f is not None:
            city_days[day_date] = {
                "source": "accuweather",
                "high_f": round(float(high_f), 1),
                "high_std_f": None,
                "low_f": round(float(low_f), 1) if low_f is not None else None,
                "model": "AccuWeather_SWIFT",
            }

    _accu_cache[loc_key] = city_days
    _accu_cache_ts[loc_key] = time.time()
    logger.debug("AccuWeather: %d days fetched for %.2f,%.2f", len(city_days), lat, lon)
    return city_days.get(date)


# ── Source 9: Visual Crossing (ECMWF+GFS+GDPS blend — free key) ──────────
# Blends multiple NWP models with station observations and proprietary
# interpolation. 1,000 calls/day free, 15-day forecast, global coverage.

_vc_cache: Dict[str, dict] = {}
_vc_cache_ts: Dict[str, float] = {}

def _fetch_visual_crossing(lat: float, lon: float, date: str) -> Optional[dict]:
    if not VISUALCROSSING_API_KEY:
        return None

    loc_key = f"{lat},{lon}"
    if loc_key in _vc_cache and (time.time() - _vc_cache_ts.get(loc_key, 0)) < CACHE_TTL:
        return _vc_cache[loc_key].get(date)

    if not _rate_check("visualcrossing"):
        logger.debug("Visual Crossing rate limited, skipping")
        return None

    # Returns 15 days by default in Fahrenheit
    url = (
        f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"
        f"/{lat},{lon}?unitGroup=us&include=days"
        f"&elements=datetime,tempmax,tempmin&contentType=json"
        f"&key={VISUALCROSSING_API_KEY}"
    )
    data = _fetch_json(url, timeout=12)
    if not data or "days" not in data:
        return None
    _rate_track("visualcrossing")

    city_days = {}
    for day in data["days"]:
        day_str = day.get("datetime", "")
        high_f = day.get("tempmax")
        low_f = day.get("tempmin")
        if day_str and high_f is not None:
            city_days[day_str] = {
                "source": "visual_crossing",
                "high_f": round(float(high_f), 1),
                "high_std_f": None,
                "low_f": round(float(low_f), 1) if low_f is not None else None,
                "model": "VC_ECMWF_GFS_Blend",
            }

    _vc_cache[loc_key] = city_days
    _vc_cache_ts[loc_key] = time.time()
    logger.debug("Visual Crossing: %d days fetched for %.2f,%.2f", len(city_days), lat, lon)
    return city_days.get(date)


# ── Source 10: OpenWeatherMap (2.5 Forecast — free key, 1000/day) ─────────
_owm_cache: Dict[str, dict] = {}
_owm_cache_ts: Dict[str, float] = {}


def _fetch_openweathermap(lat: float, lon: float, date: str) -> Optional[dict]:
    """Fetch forecast from OpenWeatherMap 2.5 API (3-hour intervals, 5 days)."""
    if not OPENWEATHERMAP_API_KEY:
        return None
    if not _rate_check("openweathermap"):
        return None

    loc_key = f"{lat:.2f},{lon:.2f}"
    if loc_key in _owm_cache and (time.time() - _owm_cache_ts.get(loc_key, 0)) < CACHE_TTL:
        return _owm_cache[loc_key].get(date)

    try:
        url = (
            f"https://api.openweathermap.org/data/2.5/forecast"
            f"?lat={lat}&lon={lon}&units=imperial"
            f"&appid={OPENWEATHERMAP_API_KEY}"
        )
        data = _fetch_json(url, timeout=10)
        if not data or "list" not in data:
            return None

        # 2.5 returns 3-hour intervals — aggregate to daily max/min
        daily_temps: Dict[str, List[float]] = {}
        for entry in data["list"]:
            dt = entry.get("dt_txt", "")[:10]  # "2026-04-07 12:00:00" → "2026-04-07"
            if not dt:
                continue
            temp = entry.get("main", {}).get("temp")
            if temp is not None:
                daily_temps.setdefault(dt, []).append(temp)

        city_days: Dict[str, dict] = {}
        for dt, temps in daily_temps.items():
            if len(temps) >= 4:  # Need at least 4 of 8 intervals for reliable daily
                city_days[dt] = {
                    "high_f": round(max(temps), 1),
                    "low_f": round(min(temps), 1),
                    "model": "OWM_MultiModel",
                    "n_members": 1,
                }

        _owm_cache[loc_key] = city_days
        _owm_cache_ts[loc_key] = time.time()
        logger.debug("OpenWeatherMap: %d days fetched for %.2f,%.2f", len(city_days), lat, lon)
        return city_days.get(date)
    except Exception as exc:
        logger.debug("OpenWeatherMap error: %s", exc)
        return None


# ── Source 11: Meteosource (ML-blended multi-model — free key, 400/day) ──
_ms_cache: Dict[str, dict] = {}
_ms_cache_ts: Dict[str, float] = {}


def _fetch_meteosource(lat: float, lon: float, date: str) -> Optional[dict]:
    """Fetch forecast from Meteosource ML-blended ensemble (6+ NWP models)."""
    if not METEOSOURCE_API_KEY:
        return None
    if not _rate_check("meteosource"):
        return None

    loc_key = f"{lat:.2f},{lon:.2f}"
    if loc_key in _ms_cache and (time.time() - _ms_cache_ts.get(loc_key, 0)) < CACHE_TTL:
        return _ms_cache[loc_key].get(date)

    try:
        url = (
            f"https://www.meteosource.com/api/v1/free/point"
            f"?lat={lat}&lon={lon}&sections=daily&units=us"
            f"&key={METEOSOURCE_API_KEY}"
        )
        data = _fetch_json(url, timeout=12)
        if not data or "daily" not in data:
            return None

        daily = data["daily"].get("data", [])
        city_days: Dict[str, dict] = {}
        for day in daily:
            dt = day.get("day")
            if not dt:
                continue
            all_day = day.get("all_day", {})
            high_f = all_day.get("temperature_max")
            low_f = all_day.get("temperature_min")
            if high_f is not None and low_f is not None:
                city_days[dt] = {
                    "high_f": round(float(high_f), 1),
                    "low_f": round(float(low_f), 1),
                    "model": "Meteosource_ML_Blend",
                    "n_members": 1,
                }

        _ms_cache[loc_key] = city_days
        _ms_cache_ts[loc_key] = time.time()
        logger.debug("Meteosource: %d days fetched for %.2f,%.2f", len(city_days), lat, lon)
        return city_days.get(date)
    except Exception as exc:
        logger.debug("Meteosource error: %s", exc)
        return None


# Register new source caches for centralized eviction
_ALL_CACHES.extend([
    (_brightsky_cache, _brightsky_cache_ts, CACHE_TTL),
    (_metoffice_cache, _metoffice_cache_ts, CACHE_TTL),
    (_accu_cache, _accu_cache_ts, CACHE_TTL),
    (_vc_cache, _vc_cache_ts, CACHE_TTL),
    (_owm_cache, _owm_cache_ts, CACHE_TTL),
    (_ms_cache, _ms_cache_ts, CACHE_TTL),
])


# ── Ensemble aggregation ─────────────────────────────────────────────────

def _resolve_city(city: str) -> Optional[Tuple[float, float, str]]:
    city_lower = city.lower().strip()
    if city_lower in CITIES:
        return CITIES[city_lower]
    # Fuzzy match
    for key, val in CITIES.items():
        if key in city_lower or city_lower in key:
            return val
    return None


def get_ensemble_forecast(city: str, date: str) -> Optional[dict]:
    """
    Get aggregated forecast from all available sources.
    
    If the target date has already ended in the city's timezone, returns
    actual observed temperatures from TWC (the resolution source) instead
    of forecasts. This gives zero-uncertainty edge calculations.
    
    Returns:
        {
            "city": "miami",
            "date": "2026-02-28",
            "sources": { ... per-source data ... },
            "ensemble": {
                "high_mean_f": 78.3,
                "high_std_f": 1.6,
                "high_min_f": 76.1,
                "high_max_f": 80.5,
                "low_mean_f": 65.2,
                "n_sources": 4,
                "n_models": 8,
                "source_agreement": 0.85,  # 1.0 = perfect agreement
            }
        }
    """
    # Check cache
    cached = _cache_get(city, date)
    if cached:
        logger.debug("Cache hit: %s/%s", city, date)
        return cached

    coords = _resolve_city(city)
    if not coords:
        logger.warning("Unknown city: %s", city)
        return None

    lat, lon, tz = coords

    # ── Smart routing: actuals for past dates, forecasts for future ──
    if _date_has_ended(city, date):
        actuals = _fetch_twc_actuals(city, date)
        if actuals:
            result = {
                "city": city.lower(),
                "date": date,
                "is_actual": True,
                "sources": {"twc_actuals": actuals},
                "ensemble": {
                    "high_mean_f": actuals["high_f"],
                    "high_std_f": 0.5,  # Near-zero but not exactly 0 (rounding/station variance)
                    "high_min_f": actuals["high_f"],
                    "high_max_f": actuals["high_f"],
                    "low_mean_f": actuals["low_f"],
                    "n_sources": 1,
                    "n_models": 1,
                    "source_agreement": 1.0,
                    "is_actual": True,
                },
            }
            _cache_set(city, date, result)
            logger.info("Using TWC actuals for %s/%s: high=%.1f°F (date ended in local tz)",
                        city, date, actuals["high_f"])
            return result
        # If actuals fetch failed, fall through to forecast (better than nothing)
        logger.debug("Actuals unavailable for %s/%s, falling back to forecast", city, date)

    # Fetch all sources (synchronous — called from sync weather_scanner)
    sources = {}
    
    # Source 1: Open-Meteo Ensemble (always available)
    om = _fetch_open_meteo_ensemble(lat, lon, date)
    if om:
        sources["open_meteo_ensemble"] = om

    # Source 2: Pirate Weather
    pw = _fetch_pirate_weather(lat, lon, date)
    if pw:
        sources["pirate_weather"] = pw

    # Source 3: Tomorrow.io
    ti = _fetch_tomorrow_io(lat, lon, date)
    if ti:
        sources["tomorrow_io"] = ti

    # Source 4: WeatherAPI.com
    wa = _fetch_weatherapi(lat, lon, date)
    if wa:
        sources["weatherapi"] = wa

    # Source 5: Weather.com / TWC (resolution source — double weight)
    twc = _fetch_weather_com(lat, lon, date, city=city)
    if twc:
        sources["weather_com"] = twc

    # Source 6: Bright Sky (DWD MOSMIX — free, no key)
    bs = _fetch_bright_sky(lat, lon, date)
    if bs:
        sources["bright_sky"] = bs

    # Source 7: Met Office (UKMO Unified Model)
    mo = _fetch_met_office(lat, lon, date)
    if mo:
        sources["met_office"] = mo

    # Source 8: AccuWeather (SWIFT + human meteorologists)
    aw = _fetch_accuweather(lat, lon, date)
    if aw:
        sources["accuweather"] = aw

    # Source 9: Visual Crossing (ECMWF+GFS+GDPS blend + station obs)
    vc = _fetch_visual_crossing(lat, lon, date)
    if vc:
        sources["visual_crossing"] = vc

    # Source 10: OpenWeatherMap (multi-model blend, 1000 calls/day free)
    owm = _fetch_openweathermap(lat, lon, date)
    if owm:
        sources["openweathermap"] = owm

    # Source 11: Meteosource (ML-blended 6+ NWP models, 400 calls/day free)
    ms = _fetch_meteosource(lat, lon, date)
    if ms:
        sources["meteosource"] = ms

    if not sources:
        logger.warning("No sources returned data for %s/%s", city, date)
        return None

    # ── Aggregate ────────────────────────────────────────────────────
    # Weighted lists: (value, weight) — Weather.com gets 1.5x as resolution source
    weighted_highs = []  # [(temp_f, weight), ...]
    weighted_lows = []
    all_highs_f = []  # Flat list for std/spread calculations
    all_lows_f = []
    n_models = 0

    for name, src in sources.items():
        w = 1.5 if src.get("is_resolution_source") else 1.0
        h = src.get("high_f")
        if h is not None and h != 0:
            weighted_highs.append((h, w))
            all_highs_f.append(h)
        l = src.get("low_f")
        if l is not None and l != 0:
            weighted_lows.append((l, w))
            all_lows_f.append(l)
        nm = src.get("n_members", 1)
        n_models += nm

    if not all_highs_f:
        return None

    # Weighted mean (Weather.com 1.5x, others 1.0x)
    total_w = sum(w for _, w in weighted_highs)
    high_mean = sum(h * w for h, w in weighted_highs) / total_w if total_w > 0 else all_highs_f[0]
    
    # Std from cross-source disagreement
    cross_std = (
        (sum((h - high_mean) ** 2 for h in all_highs_f) / len(all_highs_f)) ** 0.5
        if len(all_highs_f) > 1 else 3.0  # Default 3°F if single source
    )
    
    # Internal std from ensemble (if available)
    internal_stds = [s["high_std_f"] for s in sources.values() if s.get("high_std_f")]
    internal_std = sum(internal_stds) / len(internal_stds) if internal_stds else 0
    
    # Combined std: max of cross-source disagreement and internal ensemble spread
    # If sources disagree by >3°F, widen the distribution
    # Floor 1.5°F — forecasts are never that precise (prevents overconfident signals)
    combined_std = max(cross_std, internal_std, 1.5)
    if cross_std > 3.0:
        combined_std *= 1.3  # Fat tail penalty for disagreement
        logger.debug("Source disagreement >3°F for %s/%s: widening std %.1f → %.1f",
                      city, date, cross_std, combined_std)

    low_total_w = sum(w for _, w in weighted_lows)
    low_mean = sum(l * w for l, w in weighted_lows) / low_total_w if low_total_w > 0 else None

    # Source agreement: 1.0 if all sources within 1°F, decays with spread
    spread = max(all_highs_f) - min(all_highs_f) if len(all_highs_f) > 1 else 0
    agreement = max(0.0, 1.0 - spread / 10.0)  # 0°F spread = 1.0, 10°F = 0.0

    result = {
        "city": city.lower(),
        "date": date,
        "sources": sources,
        "ensemble": {
            "high_mean_f": round(high_mean, 1),
            "high_std_f": round(combined_std, 2),
            "high_min_f": round(min(all_highs_f), 1),
            "high_max_f": round(max(all_highs_f), 1),
            "low_mean_f": round(low_mean, 1) if low_mean else None,
            "n_sources": len(sources),
            "n_models": n_models,
            "source_agreement": round(agreement, 2),
        },
    }

    _cache_set(city, date, result)
    return result


# ── Probability calculations ─────────────────────────────────────────────
# Using normal CDF approximation (no scipy dependency)

def _norm_cdf(x: float) -> float:
    """Standard normal CDF approximation (Abramowitz & Stegun)."""
    import math
    if x < -8:
        return 0.0
    if x > 8:
        return 1.0
    a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
    p = 0.3275911
    sign = 1 if x >= 0 else -1
    x_abs = abs(x)
    t = 1.0 / (1.0 + p * x_abs)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x_abs * x_abs / 2.0)
    return 0.5 * (1.0 + sign * y)


def _t_cdf(x: float, df: float) -> float:
    """Student-t CDF approximation via normal CDF with correction."""
    import math
    # For df >= 5, normal is close enough
    if df >= 30:
        return _norm_cdf(x)
    # Hill's approx for small df
    g = math.lgamma((df + 1) / 2) - math.lgamma(df / 2)
    s = math.exp(g) / math.sqrt(df * math.pi)
    # Use beta incomplete function approx — fall back to normal with wider spread
    # Multiply x by correction factor to approximate fatter tails
    correction = math.sqrt(df / (df - 2)) if df > 2 else 1.5
    return _norm_cdf(x / correction)


def prob_below(city: str, date: str, threshold_f: float) -> Optional[dict]:
    """P(high temp < threshold_f)"""
    forecast = get_ensemble_forecast(city, date)
    if not forecast:
        return None
    
    ens = forecast["ensemble"]
    mean = ens["high_mean_f"]
    std = ens["high_std_f"]
    n_sources = ens["n_sources"]
    
    z = (threshold_f - mean) / std
    
    # Use Student-t for fewer sources (fatter tails = more uncertainty)
    df = max(n_sources * 2, 4)  # Minimum df=4 for fat tails
    if n_sources <= 2:
        p = _t_cdf(z, df=4)
    else:
        p = _norm_cdf(z)
    
    return {
        "probability": round(p, 4),
        "threshold_f": threshold_f,
        "forecast_mean_f": mean,
        "forecast_std_f": std,
        "z_score": round(z, 2),
        "distribution": "t(df=4)" if n_sources <= 2 else "normal",
        "n_sources": n_sources,
        "agreement": ens["source_agreement"],
    }


def prob_above(city: str, date: str, threshold_f: float) -> Optional[dict]:
    """P(high temp > threshold_f)"""
    result = prob_below(city, date, threshold_f)
    if not result:
        return None
    result["probability"] = round(1.0 - result["probability"], 4)
    return result


def prob_in_range(city: str, date: str, low_f: float, high_f: float) -> Optional[dict]:
    """P(low_f <= high temp <= high_f)"""
    forecast = get_ensemble_forecast(city, date)
    if not forecast:
        return None
    
    ens = forecast["ensemble"]
    mean = ens["high_mean_f"]
    std = ens["high_std_f"]
    n_sources = ens["n_sources"]
    
    z_low = (low_f - mean) / std
    z_high = (high_f - mean) / std
    
    if n_sources <= 2:
        p = _t_cdf(z_high, df=4) - _t_cdf(z_low, df=4)
    else:
        p = _norm_cdf(z_high) - _norm_cdf(z_low)
    
    return {
        "probability": round(max(0, p), 4),
        "range_f": (low_f, high_f),
        "forecast_mean_f": mean,
        "forecast_std_f": std,
        "n_sources": n_sources,
        "agreement": ens["source_agreement"],
    }


def source_health() -> dict:
    """Report which sources are configured and responding."""
    return {
        "open_meteo_ensemble": {"configured": True, "key_required": False},
        "pirate_weather": {
            "configured": bool(PIRATE_API_KEY),
            "key_required": True,
            "key_set": bool(PIRATE_API_KEY),
        },
        "tomorrow_io": {
            "configured": bool(TOMORROW_API_KEY),
            "key_required": True,
            "key_set": bool(TOMORROW_API_KEY),
        },
        "weatherapi": {
            "configured": bool(WEATHERAPI_KEY),
            "key_required": True,
            "key_set": bool(WEATHERAPI_KEY),
        },
        "bright_sky": {"configured": True, "key_required": False},
        "met_office": {
            "configured": bool(METOFFICE_API_KEY),
            "key_required": True,
            "key_set": bool(METOFFICE_API_KEY),
        },
        "accuweather": {
            "configured": bool(ACCUWEATHER_API_KEY),
            "key_required": True,
            "key_set": bool(ACCUWEATHER_API_KEY),
        },
        "visual_crossing": {
            "configured": bool(VISUALCROSSING_API_KEY),
            "key_required": True,
            "key_set": bool(VISUALCROSSING_API_KEY),
        },
        "cache_entries": len(_cache),
        "rate_limits": {k: {"calls_this_hour": v["calls"], "max_per_hour": v["max_per_hour"]} for k, v in _rate_limits.items()},
    }


# ── Convenience: evaluate a market using ensemble ────────────────────────

def ensemble_fair_value(
    city: str,
    date: str,
    comparison: str,
    threshold_f: float,
    threshold_high_f: float = None,
) -> Optional[dict]:
    """
    Calculate fair value for a weather market using ensemble probabilities.
    
    Args:
        city: City name
        date: YYYY-MM-DD
        comparison: "above", "below", "between", "exact"
        threshold_f: Temperature threshold in °F (or low bound for between)
        threshold_high_f: High bound for "between" comparison
    
    Returns:
        {
            "fair_value": 0.73,
            "confidence": 0.85,
            "forecast_mean_f": 78.3,
            "forecast_std_f": 1.6,
            "n_sources": 3,
            "n_models": 8,
            ...
        }
    """
    if comparison == "above":
        result = prob_above(city, date, threshold_f)
    elif comparison == "below":
        result = prob_below(city, date, threshold_f)
    elif comparison in ("between", "exact"):
        if threshold_high_f is None:
            # "exact" — use ±0.5°F range
            threshold_high_f = threshold_f + 0.5
            threshold_f = threshold_f - 0.5
        result = prob_in_range(city, date, threshold_f, threshold_high_f)
    else:
        logger.warning("Unknown comparison type: %s", comparison)
        return None

    if not result:
        return None

    prob = result["probability"]
    n_sources = result["n_sources"]
    agreement = result["agreement"]

    # Confidence based on source count + agreement
    # 1 source = low confidence, 4 sources with agreement = high
    confidence = min(1.0, (n_sources / 4) * 0.6 + agreement * 0.4)

    return {
        "fair_value": round(prob, 3),
        "confidence": round(confidence, 2),
        "forecast_mean_f": result["forecast_mean_f"],
        "forecast_std_f": result["forecast_std_f"],
        "n_sources": n_sources,
        "agreement": agreement,
        "distribution": result.get("distribution", "normal"),
    }


# ── CLI demo ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG)
    
    city = sys.argv[1] if len(sys.argv) > 1 else "miami"
    date = sys.argv[2] if len(sys.argv) > 2 else (
        datetime.now(timezone.utc) + timedelta(days=1)
    ).strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"Weather Ensemble: {city} on {date}")
    print(f"{'='*60}")
    
    # Source health
    health = source_health()
    print(f"\nSources configured:")
    for src, info in health.items():
        if isinstance(info, dict):
            status = "✅" if info.get("configured") else "❌"
            print(f"  {status} {src}")
    
    # Ensemble forecast
    forecast = get_ensemble_forecast(city, date)
    if forecast:
        ens = forecast["ensemble"]
        print(f"\nEnsemble forecast:")
        print(f"  High: {ens['high_mean_f']}°F ± {ens['high_std_f']}°F")
        print(f"  Range: {ens['high_min_f']}°F — {ens['high_max_f']}°F")
        print(f"  Sources: {ens['n_sources']} ({ens['n_models']} models)")
        print(f"  Agreement: {ens['source_agreement']}")
        
        # Per-source
        print(f"\nPer source:")
        for name, src in forecast["sources"].items():
            print(f"  {name}: {src['high_f']}°F" + 
                  (f" ± {src['high_std_f']}°F" if src.get('high_std_f') else "") +
                  (f" ({src.get('n_members', 1)} members)" if src.get('n_members', 1) > 1 else ""))
        
        # Example probability calculations
        mean = ens["high_mean_f"]
        print(f"\nProbabilities:")
        for thresh in [mean - 5, mean - 2, mean, mean + 2, mean + 5]:
            r = prob_below(city, date, thresh)
            if r:
                print(f"  P(high < {thresh:.0f}°F) = {r['probability']:.1%}")
        
        # Range example
        r = prob_in_range(city, date, mean - 1, mean + 1)
        if r:
            print(f"  P({mean-1:.0f} ≤ high ≤ {mean+1:.0f}) = {r['probability']:.1%}")
    else:
        print("No forecast data available")


# ── Ensemble status (for dashboard) ──────────────────────────────────────

_SOURCE_META = [
    {"key": "open_meteo_ensemble", "display": "Open-Meteo Ensemble", "cache_ts": None, "api_key": None, "rate_key": None},
    {"key": "pirate_weather", "display": "Pirate Weather", "cache_ts": "_pirate_cache_ts", "api_key": "PIRATE_API_KEY", "rate_key": "pirate_weather"},
    {"key": "tomorrow_io", "display": "Tomorrow.io", "cache_ts": "_tomorrow_cache_ts", "api_key": "TOMORROW_API_KEY", "rate_key": "tomorrow_io"},
    {"key": "weatherapi", "display": "WeatherAPI", "cache_ts": "_weatherapi_cache_ts", "api_key": "WEATHERAPI_KEY", "rate_key": "weatherapi"},
    {"key": "weather_com", "display": "Weather.com / TWC", "cache_ts": "_twc_cache_ts", "api_key": None, "rate_key": None},
    {"key": "bright_sky", "display": "Bright Sky (MOSMIX)", "cache_ts": "_brightsky_cache_ts", "api_key": None, "rate_key": None},
    {"key": "met_office", "display": "Met Office (UKMO)", "cache_ts": "_metoffice_cache_ts", "api_key": "METOFFICE_API_KEY", "rate_key": "metoffice"},
    {"key": "accuweather", "display": "AccuWeather (SWIFT)", "cache_ts": "_accu_cache_ts", "api_key": "ACCUWEATHER_API_KEY", "rate_key": "accuweather"},
    {"key": "visual_crossing", "display": "Visual Crossing", "cache_ts": "_vc_cache_ts", "api_key": "VISUALCROSSING_API_KEY", "rate_key": "visualcrossing"},
    {"key": "openweathermap", "display": "OpenWeatherMap", "cache_ts": "_owm_cache_ts", "api_key": "OPENWEATHERMAP_API_KEY", "rate_key": "openweathermap"},
    {"key": "meteosource", "display": "Meteosource", "cache_ts": "_ms_cache_ts", "api_key": "METEOSOURCE_API_KEY", "rate_key": "meteosource"},
]


def get_ensemble_status() -> dict:
    """Return ensemble health from in-memory state. No API calls made."""
    now = time.time()
    g = globals()

    # ── Per-source status ──
    source_statuses = []
    for meta in _SOURCE_META:
        # Last success timestamp from per-source cache
        last_ts = None
        cache_entries = 0
        if meta["cache_ts"]:
            ts_dict = g.get(meta["cache_ts"], {})
            if ts_dict:
                last_ts = max(ts_dict.values())
                cache_entries = len(ts_dict)
        elif meta["key"] == "open_meteo_ensemble":
            # Open-Meteo uses the main _cache; check which entries have it in sources
            for k, v in _cache.items():
                if isinstance(v, dict) and "open_meteo_ensemble" in v.get("sources", {}):
                    ts = _cache_ts.get(k, 0)
                    if last_ts is None or ts > last_ts:
                        last_ts = ts
                    cache_entries += 1

        # API key check
        api_key_configured = None
        if meta["api_key"]:
            api_key_configured = bool(g.get(meta["api_key"], ""))

        # Rate limit info
        rate_info = None
        if meta["rate_key"] and meta["rate_key"] in _rate_limits:
            rl = _rate_limits[meta["rate_key"]]
            max_h = rl.get("max_per_hour", 0)
            rate_info = {
                "calls": rl["calls"],
                "max_per_hour": max_h,
                "pct": round(rl["calls"] / max_h * 100, 1) if max_h > 0 else 0,
            }

        # Circuit breaker (Open-Meteo only)
        cb = None
        if meta["key"] == "open_meteo_ensemble":
            cb = {
                "blocked_until": _open_meteo_blocked_until,
                "consecutive_fails": _open_meteo_consecutive_fails,
                "blocked": now < _open_meteo_blocked_until,
            }

        # Derive status
        ago = (now - last_ts) if last_ts else None
        if api_key_configured is False:
            status = "offline"
        elif last_ts is None or cache_entries == 0:
            status = "no_data"
        elif cb and cb["blocked"]:
            status = "degraded"
        elif rate_info and rate_info["pct"] > 90:
            status = "degraded"
        elif ago and ago > 3600:
            status = "degraded"
        else:
            status = "active"

        source_statuses.append({
            "key": meta["key"],
            "name": meta["display"],
            "status": status,
            "last_success_ts": round(last_ts, 1) if last_ts else None,
            "last_success_ago_s": round(ago) if ago else None,
            "rate_limit": rate_info,
            "api_key_configured": api_key_configured,
            "circuit_breaker": cb,
            "cache_entries": cache_entries,
        })

    # ── Source distribution from main cache ──
    dist: Dict[int, int] = {}
    city_sources: Dict[str, set] = {}
    for key, val in _cache.items():
        if not isinstance(val, dict):
            continue
        # Skip expired entries
        if (now - _cache_ts.get(key, 0)) > CACHE_TTL:
            continue
        sources = val.get("sources", {})
        n = len(sources)
        dist[n] = dist.get(n, 0) + 1

        city = key.split(":")[0] if ":" in key else key
        if city not in city_sources:
            city_sources[city] = set()
        city_sources[city].update(sources.keys())

    # Convert city_sources sets to sorted lists
    city_coverage = {c: sorted(list(s)) for c, s in sorted(city_sources.items())}

    # ── Summary ──
    active_count = sum(1 for s in source_statuses if s["status"] == "active")
    degraded_count = sum(1 for s in source_statuses if s["status"] == "degraded")
    offline_count = sum(1 for s in source_statuses if s["status"] in ("offline", "no_data"))
    total_cached = sum(dist.values())
    avg_sources = sum(k * v for k, v in dist.items()) / total_cached if total_cached > 0 else 0

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sources": source_statuses,
        "source_distribution": {str(k): v for k, v in sorted(dist.items())},
        "city_coverage": city_coverage,
        "summary": {
            "total_sources": len(_SOURCE_META),
            "active": active_count,
            "degraded": degraded_count,
            "offline": offline_count,
            "cached_forecasts": total_cached,
            "avg_sources_per_signal": round(avg_sources, 1),
        },
    }

"""
Weather Ensemble — multi-source forecast aggregator with calibrated probabilities.

Sources:
  1. Open-Meteo Ensemble (30+ models, no key)
  2. Pirate Weather (GEFS/ECMWF/HRRR, free key)
  3. Tomorrow.io (proprietary AI, free key)
  4. WeatherAPI.com (station blend, free key)
  5. Weather.com / TWC (resolution source, 2x weight) — HIGHEST PRIORITY
     This is the exact backend Polymarket resolves against via Weather Underground.

Produces probability distributions for temperature markets instead of
hardcoded fair-value buckets.
"""

import asyncio
import json
import os
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from loguru import logger


# ── API Keys (from env) ─────────────────────────────────────────────────
PIRATE_API_KEY = os.environ.get("PIRATE_WEATHER_KEY", "")
TOMORROW_API_KEY = os.environ.get("TOMORROW_IO_KEY", "")
WEATHERAPI_KEY = os.environ.get("WEATHERAPI_KEY", "")

# ── City coordinates ─────────────────────────────────────────────────────
CITIES: Dict[str, Tuple[float, float, str]] = {
    "nyc": (40.71, -74.01, "America/New_York"),
    "new york": (40.71, -74.01, "America/New_York"),
    "new york city": (40.71, -74.01, "America/New_York"),
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
    "tel-aviv": (32.07, 34.77, "Asia/Jerusalem"), "tel aviv": (32.07, 34.77, "Asia/Jerusalem"),
    "singapore": (1.35, 103.82, "Asia/Singapore"),
    "shanghai": (31.23, 121.47, "Asia/Shanghai"),
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
]

# ── Cache ────────────────────────────────────────────────────────────────
_cache: Dict[str, dict] = {}
_cache_ts: Dict[str, float] = {}
CACHE_TTL = 1800  # 30 min — balances freshness vs API rate limits (Open-Meteo 429s at 15min)

# Rate limit tracking per source
_rate_limits = {
    "pirate_weather": {"calls": 0, "reset_ts": 0, "max_per_hour": 15, "max_per_month": 10000},
    "tomorrow_io": {"calls": 0, "reset_ts": 0, "max_per_hour": 15, "max_per_day": 450},
    "weatherapi": {"calls": 0, "reset_ts": 0, "max_per_hour": 50, "max_per_month": 95000},
    "visual_crossing": {"calls": 0, "reset_ts": 0, "max_per_hour": 35, "max_per_day": 1000},
    "weather_com": {"calls": 0, "reset_ts": 0, "max_per_hour": 80, "max_per_day": 1500},
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
        logger.debug("fetch failed {}: {}", url, e)
        return None


def _record_weather_fetch(source: str, success: bool, latency_ms: float, error: str = "") -> None:
    """Single instrumentation point for every weather fetcher.

    Records success/failure to the shared `source_health` SQLite table so the
    health-gate framework can read aggregate state. Failure to instrument never
    breaks the fetch — wrapped in broad try/except by design.

    Source names are stable identifiers (open_meteo, pirate_weather, ...) used
    by Gate 4 in services/health_gates.py. Keep this list in sync.
    """
    try:
        from api.services import source_health as _sh
        if success:
            _sh.record_success(source, latency_ms)
        else:
            _sh.record_failure(source, error or "fetch returned None")
    except Exception:
        pass  # never let instrumentation interfere with weather scanning


def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


# ── Source 1: Open-Meteo Ensemble (PRIMARY — no key needed) ─────────────

# Circuit breaker: skip Open-Meteo if rate limited (exponential backoff)
_open_meteo_blocked = False
_open_meteo_blocked_ts = 0.0
_open_meteo_backoff = 600  # starts at 10min, doubles each trip, max 1h
_open_meteo_cache = {}      # per-source cache: {(lat,lon,date): (result, ts)}
_OPEN_METEO_SOURCE_TTL = 7200  # 2h per-source cache — forecasts don't change fast

def _fetch_open_meteo_ensemble(lat: float, lon: float, date: str) -> Optional[dict]:
    """
    Fetch ensemble forecasts from multiple independent models.
    Returns dict with high temps from each ensemble member.
    """
    global _open_meteo_blocked, _open_meteo_blocked_ts, _open_meteo_backoff
    if _open_meteo_blocked and (time.time() - _open_meteo_blocked_ts) < _open_meteo_backoff:
        return None
    if _open_meteo_blocked:
        _open_meteo_blocked = False  # Reset after backoff period
    # Per-source 2h cache — much longer than top-level 30min cache
    _om_key = (round(lat, 2), round(lon, 2), date)
    if _om_key in _open_meteo_cache:
        cached_result, cached_ts = _open_meteo_cache[_om_key]
        if time.time() - cached_ts < _OPEN_METEO_SOURCE_TTL:
            logger.debug("Open-Meteo source cache hit: {}", date)
            return cached_result

    models_param = ",".join(ENSEMBLE_MODELS)
    # Use standard forecast API with multi-model (less aggressive rate limits than ensemble-api)
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&daily=temperature_2m_max,temperature_2m_min"
        f"&models={models_param}"
        f"&timezone=auto"
        f"&start_date={date}&end_date={date}"
    )
    _t0 = time.time()
    data = _fetch_json(url, timeout=15)
    _record_weather_fetch("open_meteo", data is not None, (time.time() - _t0) * 1000.0,
                          error="open-meteo /v1/forecast returned None")
    if not data:
        # Only trip circuit breaker — the 429 detection is inside _fetch_json
        # which returns None. But avoid doubling backoff on non-429 failures.
        if not _open_meteo_blocked:
            _open_meteo_blocked = True
            _open_meteo_blocked_ts = time.time()
            # Don't double if already at max
            logger.info("Open-Meteo circuit breaker tripped (backoff={}s)", _open_meteo_backoff)
        return None

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
        logger.debug("Open-Meteo ensemble returned no highs for {}", date)
        return _fetch_open_meteo_ensemble_fallback(lat, lon, date)

    highs_f = [_c_to_f(c) for c in highs_c]
    lows_f = [_c_to_f(c) for c in lows_c] if lows_c else []

    mean_high = sum(highs_f) / len(highs_f)
    std_high = (sum((h - mean_high) ** 2 for h in highs_f) / len(highs_f)) ** 0.5 if len(highs_f) > 1 else 2.0

    sorted_highs = sorted(highs_f)
    n = len(sorted_highs)

    _result = {
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
    _open_meteo_cache[_om_key] = (_result, time.time())
    _open_meteo_backoff = 600  # Reset backoff on success
    return _result


def _fetch_open_meteo_ensemble_fallback(lat: float, lon: float, date: str) -> Optional[dict]:
    """Fallback: fetch each model individually from standard Open-Meteo API."""
    highs_f = []
    models_used = []
    _t0 = time.time()

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

    # Aggregate health record: success = at least one model returned data.
    # (Don't record per-model — same source name, would double-count.)
    _record_weather_fetch("open_meteo", bool(highs_f), (time.time() - _t0) * 1000.0,
                          error="open-meteo fallback: zero models returned data")

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
    _t0 = time.time()
    # Track before the result check — failures count against our internal cap too.
    _rate_track("pirate_weather")
    data = _fetch_json(url, timeout=10)
    _ok = bool(data and "daily" in data)
    _record_weather_fetch("pirate_weather", _ok, (time.time() - _t0) * 1000.0,
                          error="pirate_weather missing 'daily' key" if data else "pirate_weather returned None")
    if not _ok:
        return None

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

# Circuit breaker: trip on persistent 429s so we don't burn quota retrying.
# Mirrors _open_meteo_blocked / _vc_blocked. Exponential backoff capped at 1h.
_tomorrow_blocked = False
_tomorrow_blocked_ts = 0.0
_tomorrow_backoff = 1800  # 30min, doubles each trip, max 1h

def _fetch_tomorrow_io(lat: float, lon: float, date: str) -> Optional[dict]:
    global _tomorrow_blocked, _tomorrow_blocked_ts, _tomorrow_backoff
    if not TOMORROW_API_KEY:
        return None

    loc_key = f"{lat},{lon}"
    if loc_key in _tomorrow_cache and (time.time() - _tomorrow_cache_ts.get(loc_key, 0)) < CACHE_TTL:
        return _tomorrow_cache[loc_key].get(date)

    # Circuit breaker check — skip entirely if recently rate-limited.
    if _tomorrow_blocked and (time.time() - _tomorrow_blocked_ts) < _tomorrow_backoff:
        return None
    if _tomorrow_blocked:
        _tomorrow_blocked = False  # backoff window elapsed, try once

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
    _t0 = time.time()
    # Track BEFORE the result check so failed attempts also count toward our
    # internal rate limit. Prior bug: only successes were tracked, so we'd
    # keep retrying through 429s without our own guard ever firing.
    _rate_track("tomorrow_io")
    data = _fetch_json(url, timeout=10)
    _record_weather_fetch("tomorrow_io", data is not None, (time.time() - _t0) * 1000.0,
                          error="tomorrow_io returned None (likely 429)")
    if not data:
        if not _tomorrow_blocked:
            _tomorrow_blocked = True
            _tomorrow_blocked_ts = time.time()
            _tomorrow_backoff = min(_tomorrow_backoff * 2, 3600)  # max 1h
            logger.info("Tomorrow.io circuit breaker tripped (backoff={}s)", _tomorrow_backoff)
        return None
    # Success — reset backoff so the next failure starts at 10min again
    _tomorrow_backoff = 1800

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
    _t0 = time.time()
    # Track before the result check — failures count against our internal cap too.
    _rate_track("weatherapi")
    data = _fetch_json(url, timeout=10)
    _ok = bool(data and "forecast" in data)
    _record_weather_fetch("weatherapi", _ok, (time.time() - _t0) * 1000.0,
                          error="weatherapi missing 'forecast' key" if data else "weatherapi returned None")
    if not _ok:
        return None


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
    "nyc": "KJFK", "new york": "KJFK", "new york city": "KJFK",
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
    "tel-aviv": "LLBG", "tel aviv": "LLBG",
    "singapore": "WSSS",
    "shanghai": "ZSPD",
}

_twc_cache: Dict[str, dict] = {}
_twc_cache_ts: Dict[str, float] = {}
_actuals_cache: Dict[str, dict] = {}
_actuals_cache_ts: Dict[str, float] = {}
ACTUALS_CACHE_TTL = 3600  # 1h — actuals don't change


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

    _t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/1.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        obs = data.get("observations", [])
        if not obs:
            _record_weather_fetch("twc_actuals", False, (time.time() - _t0) * 1000.0,
                                  error="twc_actuals empty observations array")
            return None

        temps = [o.get("temp") for o in obs if o.get("temp") is not None]
        if not temps:
            _record_weather_fetch("twc_actuals", False, (time.time() - _t0) * 1000.0,
                                  error="twc_actuals no usable temps in observations")
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
        logger.info("TWC actuals {}/{}: high={}°F low={}°F ({} obs)",
                     icao, date, actual_high, actual_low, len(temps))
        _record_weather_fetch("twc_actuals", True, (time.time() - _t0) * 1000.0)
        return result

    except Exception as e:
        logger.debug("TWC actuals fetch failed for {}/{}: {}", icao, date, e)
        _record_weather_fetch("twc_actuals", False, (time.time() - _t0) * 1000.0, error=str(e)[:200])
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
    _t0 = time.time()
    data = _fetch_json(url, timeout=10)
    _record_weather_fetch("weather_com", data is not None, (time.time() - _t0) * 1000.0,
                          error="weather_com returned None")
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
    logger.debug("Weather.com {}: {} days fetched", icao, len(city_days))
    return city_days.get(date)


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



# ── Source 6: Visual Crossing (free, 1K calls/day) ───────────────────────
VISUAL_CROSSING_KEY = os.environ.get("VISUAL_CROSSING_KEY", "")
_vc_cache = {}  # per-source 4h cache
_vc_blocked = False
_vc_blocked_ts = 0.0
_vc_backoff = 600  # 10min start, max 1h
_VC_SOURCE_TTL = 14400  # 4h — conservative for 1000 calls/day free tier

def _fetch_visual_crossing(lat: float, lon: float, date: str, city: str = "") -> Optional[dict]:
    """Fetch from Visual Crossing Weather API (free tier: 1000 calls/day)."""
    global _vc_blocked, _vc_blocked_ts, _vc_backoff
    if not VISUAL_CROSSING_KEY:
        return None
    
    # Rate limiter
    if not _rate_check("visual_crossing"):
        return None
    # Circuit breaker
    if _vc_blocked and (time.time() - _vc_blocked_ts) < _vc_backoff:
        return None
    if _vc_blocked:
        _vc_blocked = False
    
    _vc_key = (round(lat, 2), round(lon, 2), date)
    if _vc_key in _vc_cache and (time.time() - _vc_cache[_vc_key][1]) < _VC_SOURCE_TTL:
        return _vc_cache[_vc_key][0]
    
    # Use city name if available for better station matching
    location = city.replace(" ", "%20") if city else f"{lat},{lon}"
    url = (
        f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"
        f"/{location}/{date}/{date}"
        f"?unitGroup=us&include=days&key={VISUAL_CROSSING_KEY}&contentType=json"
    )
    _t0 = time.time()
    data = _fetch_json(url, timeout=12)
    _record_weather_fetch("visual_crossing", data is not None, (time.time() - _t0) * 1000.0,
                          error="visual_crossing returned None")
    if not data:
        if not _vc_blocked:
            _vc_blocked = True
            _vc_blocked_ts = time.time()
            _vc_backoff = min(_vc_backoff * 2, 3600)  # max 1h
            logger.info("Visual Crossing circuit breaker tripped (backoff={}s)", _vc_backoff)
        return None
    _vc_backoff = 600  # reset on success
    _rate_track("visual_crossing")
    
    try:
        day = data.get("days", [{}])[0]
        high_f = day.get("tempmax")
        low_f = day.get("tempmin")
        if high_f is None:
            return None
        result = {
            "source": "visual_crossing",
            "high_f": round(float(high_f), 1),
            "low_f": round(float(low_f), 1) if low_f else None,
            "high_std_f": 0,
            "model": "visual_crossing",
        }
        _vc_cache[_vc_key] = (result, time.time())
        return result
    except Exception as e:
        logger.debug("Visual Crossing parse failed: {}", e)
        return None


# ── Source 7: NWS API (free, unlimited, US cities only) ──────────────────
_NWS_GRIDPOINT_CACHE = {}  # (lat, lon) -> (office, gridX, gridY)
_nws_cache = {}

# US cities we trade on
_US_CITIES = {
    "new york", "chicago", "miami", "dallas", "seattle", "atlanta",
    "houston", "phoenix", "denver", "san francisco", "los angeles",
    "boston", "washington dc", "portland", "minneapolis", "detroit",
    "philadelphia", "san diego", "tampa", "charlotte", "nashville",
}

def _is_us_city(city: str) -> bool:
    return city.lower().strip() in _US_CITIES

def _fetch_nws(lat: float, lon: float, date: str, city: str = "") -> Optional[dict]:
    """Fetch from NWS API (free, US only). Uses gridpoint forecast."""
    if not _is_us_city(city):
        return None
    
    _nws_key = (round(lat, 2), round(lon, 2), date)
    if _nws_key in _nws_cache and (time.time() - _nws_cache[_nws_key][1]) < 7200:
        return _nws_cache[_nws_key][0]
    
    # Step 1: Get gridpoint (cached forever — doesn't change)
    grid_key = (round(lat, 4), round(lon, 4))
    if grid_key not in _NWS_GRIDPOINT_CACHE:
        points = _fetch_json(f"https://api.weather.gov/points/{lat},{lon}", timeout=10,
                             headers={"User-Agent": "polyclawd/1.0 (weather@virtuosocrypto.com)"})
        if not points or "properties" not in points:
            return None
        props = points["properties"]
        _NWS_GRIDPOINT_CACHE[grid_key] = (
            props.get("gridId", ""),
            props.get("gridX", 0),
            props.get("gridY", 0),
        )
    
    office, gx, gy = _NWS_GRIDPOINT_CACHE[grid_key]
    if not office:
        return None
    
    # Step 2: Get forecast
    url = f"https://api.weather.gov/gridpoints/{office}/{gx},{gy}/forecast"
    _t0 = time.time()
    data = _fetch_json(url, timeout=10,
                       headers={"User-Agent": "polyclawd/1.0 (weather@virtuosocrypto.com)"})
    _record_weather_fetch("nws", data is not None, (time.time() - _t0) * 1000.0,
                          error="nws gridpoint forecast returned None")
    if not data:
        return None
    
    try:
        from datetime import datetime as _dt
        target = _dt.strptime(date, "%Y-%m-%d").date()
        
        for period in data.get("properties", {}).get("periods", []):
            # NWS periods: "Tuesday" daytime, "Tuesday Night" etc.
            if not period.get("isDaytime", True):
                continue
            start = period.get("startTime", "")
            if not start:
                continue
            period_date = _dt.fromisoformat(start.replace("Z", "+00:00")).date()
            if period_date == target:
                temp_f = period.get("temperature")
                if temp_f is not None:
                    result = {
                        "source": "nws",
                        "high_f": round(float(temp_f), 1),
                        "low_f": None,
                        "high_std_f": 0,
                        "model": f"NWS_{office}",
                    }
                    _nws_cache[_nws_key] = (result, time.time())
                    return result
    except Exception as e:
        logger.debug("NWS parse failed: {}", e)
    
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
        logger.debug("Cache hit: {}/{}", city, date)
        return cached

    coords = _resolve_city(city)
    if not coords:
        logger.warning("Unknown city: {}", city)
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
            # IMPROVEMENT 3d: Resolve per-source forecasts with actual temp
            try:
                resolve_source_forecasts(city, date, actuals["high_f"])
            except Exception:
                pass
            logger.info("Using TWC actuals for {}/{}: high={}°F (date ended in local tz)",
                        city, date, actuals["high_f"])
            return result
        # If actuals fetch failed, fall through to forecast (better than nothing)
        logger.debug("Actuals unavailable for {}/{}, falling back to forecast", city, date)

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

    # Source 6: Visual Crossing
    vc = _fetch_visual_crossing(lat, lon, date, city=city)
    if vc:
        sources["visual_crossing"] = vc

    # Source 7: NWS (US cities only — free, unlimited)
    nws = _fetch_nws(lat, lon, date, city=city)
    if nws:
        sources["nws"] = nws

    if not sources:
        logger.warning("No sources returned data for {}/{}", city, date)
        return None

    # ── Aggregate ────────────────────────────────────────────────────
    # Weighted lists: (value, weight) — Weather.com gets 1.5x as resolution source
    weighted_highs = []  # [(temp_f, weight), ...]
    weighted_lows = []
    all_highs_f = []  # Flat list for std/spread calculations
    all_lows_f = []
    n_models = 0

    for name, src in sources.items():
        # IMPROVEMENT 3: Dynamic per-city source weighting
        _dynamic_weights = get_source_weights_for_city(city)
        w = _dynamic_weights.get(name, 1.5 if src.get("is_resolution_source") else 1.0)
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
        logger.debug("Source disagreement >3°F for {}/{}: widening std {} → {}",
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

    # IMPROVEMENT 3: Log individual source forecasts for RMSE tracking
    try:
        log_source_forecasts(city, date, sources)
    except Exception:
        pass
    
    # GAP 2: Log ensemble forecast for accuracy tracking
    try:
        log_ensemble_forecast(city, date, result["ensemble"])
    except Exception:
        pass
    
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



# ── Empirical forecast error std (learned from forecast_log) ─────────
_EMPIRICAL_DB = None
_EMPIRICAL_CACHE = {}
_EMPIRICAL_CACHE_TS = 0

def _get_empirical_std(city: str, horizon_hours: float, min_samples: int = 30) -> Optional[float]:
    """Look up empirical forecast error std from resolved forecast_log entries.
    
    Returns None if insufficient data (< min_samples), meaning caller
    should fall back to static floor. Caches results for 1 hour.
    """
    global _EMPIRICAL_CACHE, _EMPIRICAL_CACHE_TS
    import time as _time
    now = _time.time()
    
    # Refresh cache hourly
    if now - _EMPIRICAL_CACHE_TS > 3600:
        _EMPIRICAL_CACHE = {}
        _EMPIRICAL_CACHE_TS = now
    
    # Horizon bucket: 0-6, 6-24, 24-48, 48-72
    if horizon_hours <= 6:
        h_lo, h_hi = 0, 6
    elif horizon_hours <= 24:
        h_lo, h_hi = 6, 24
    elif horizon_hours <= 48:
        h_lo, h_hi = 24, 48
    else:
        h_lo, h_hi = 48, 72
    
    cache_key = f"{city.lower()}:{h_lo}-{h_hi}"
    if cache_key in _EMPIRICAL_CACHE:
        return _EMPIRICAL_CACHE[cache_key]
    
    try:
        import sqlite3
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'storage', 'shadow_trades.db')
        db = sqlite3.connect(db_path)
        rows = db.execute("""
            SELECT AVG(forecast_error_f) as avg_err FROM forecast_log
            WHERE LOWER(city) = ? 
              AND forecast_horizon_hours >= ? AND forecast_horizon_hours < ?
              AND forecast_error_f IS NOT NULL
            GROUP BY target_date
        """, (city.lower(), h_lo, h_hi)).fetchall()
        
        if len(rows) < min_samples:
            # Fallback: try ALL horizons for this city
            if h_lo != 0 or h_hi != 999:
                all_key = f"{city.lower()}:ALL"
                if all_key not in _EMPIRICAL_CACHE:
                    all_rows = db.execute("""
                        SELECT AVG(forecast_error_f) as avg_err FROM forecast_log
                        WHERE LOWER(city) = ?
                          AND forecast_error_f IS NOT NULL
                        GROUP BY target_date
                    """, (city.lower(),)).fetchall()
                    db.close()  # close after fallback query
                    if len(all_rows) >= min_samples:
                        all_errors = [r[0] for r in all_rows]
                        all_rmse = (sum(e ** 2 for e in all_errors) / len(all_errors)) ** 0.5
                        _EMPIRICAL_CACHE[all_key] = round(all_rmse, 2)
                        _EMPIRICAL_CACHE[cache_key] = _EMPIRICAL_CACHE[all_key]
                        logger.info("Empirical std for {} [ALL horizons fallback]: {}°F ({} samples)",
                                    city, _EMPIRICAL_CACHE[all_key], len(all_rows))
                        return _EMPIRICAL_CACHE[all_key]
                    else:
                        _EMPIRICAL_CACHE[all_key] = None
                elif _EMPIRICAL_CACHE[all_key] is not None:
                    _EMPIRICAL_CACHE[cache_key] = _EMPIRICAL_CACHE[all_key]
                    return _EMPIRICAL_CACHE[all_key]
            _EMPIRICAL_CACHE[cache_key] = None
            db.close()
            return None
        
        db.close()
        errors = [r[0] for r in rows]
        # Use RMSE, not std-of-errors. RMSE captures both bias and variance.
        # A city with consistent 5°F bias should get high RMSE (=low confidence),
        # not low std (which would cause overconfidence in the wrong direction).
        rmse = (sum(e ** 2 for e in errors) / len(errors)) ** 0.5
        result = round(rmse, 2)
        _EMPIRICAL_CACHE[cache_key] = result
        logger.info("Empirical std for {} [{}-{}h]: {}°F ({} samples)",
                     city, h_lo, h_hi, result, len(rows))
        return result
    except Exception as e:
        logger.debug("Empirical std lookup failed: {}", e)
        _EMPIRICAL_CACHE[cache_key] = None
        return None



# ── Empirical forecast bias (learned from forecast_log) ─────────
_BIAS_CACHE = {}
_BIAS_CACHE_TS = 0

def _get_empirical_bias(city: str, min_dates: int = 30) -> Optional[float]:
    """Look up mean forecast error (bias) from resolved forecast_log entries.
    
    Returns AVG(actual - forecast) across target_dates. Positive means forecast
    is too LOW (actual runs hotter); negative means forecast is too HIGH.
    
    Caches results for 1 hour. Returns None if < min_dates resolved dates.
    """
    global _BIAS_CACHE, _BIAS_CACHE_TS
    import time as _time
    now = _time.time()
    
    # Refresh cache hourly
    if now - _BIAS_CACHE_TS > 3600:
        _BIAS_CACHE = {}
        _BIAS_CACHE_TS = now
    
    # Normalize city aliases to DB names
    _CITY_ALIASES = {"nyc": "new york", "são paulo": "sao paulo", "dc": "washington"}
    city_norm = _CITY_ALIASES.get(city.lower(), city.lower())
    cache_key = city_norm
    if cache_key in _BIAS_CACHE:
        return _BIAS_CACHE[cache_key]
    
    try:
        import sqlite3
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'storage', 'shadow_trades.db')
        db = sqlite3.connect(db_path)
        # Get per-date average errors, then average those (avoids weighting dates with more forecasts)
        rows = db.execute("""
            SELECT AVG(forecast_error_f) as avg_err FROM forecast_log
            WHERE LOWER(city) = ?
              AND forecast_error_f IS NOT NULL
            GROUP BY target_date
        """, (city_norm,)).fetchall()
        db.close()
        
        if len(rows) < min_dates:
            _BIAS_CACHE[cache_key] = None
            return None
        
        per_date_errors = [r[0] for r in rows]
        mean_bias = sum(per_date_errors) / len(per_date_errors)
        result = round(mean_bias, 2)
        _BIAS_CACHE[cache_key] = result
        logger.info("Empirical bias for {}: {:.2f}°F ({} dates)", city, result, len(rows))
        return result
    except Exception as e:
        logger.debug("Empirical bias lookup failed: {}", e)
        _BIAS_CACHE[cache_key] = None
        return None

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
    """P(low_f <= high temp <= high_f)
    
    For narrow brackets (<=3F), inflates std to reflect the fundamental
    precision limit of weather forecasting. Ensemble std captures MODEL
    disagreement but not station-to-resolution error (1-3F historically)
    or fat tails in real forecast distributions.
    
    Market-implied std for 1F brackets is ~3.5-4.0F (peak bracket ~12-15%).
    Calibrated against 75 resolved Polymarket trades (Mar 2026):
      std=1.5F -> 33% peak bracket -> 0% YES win rate (broken)
      std=3.5F -> 15% peak bracket -> matches market pricing
    """
    forecast = get_ensemble_forecast(city, date)
    if not forecast:
        return None
    
    ens = forecast["ensemble"]
    mean = ens["high_mean_f"]
    std = ens["high_std_f"]
    n_sources = ens["n_sources"]
    
    bracket_width = high_f - low_f
    
    # Bracket-width-aware std inflation.
    # Uses empirical forecast error when available (from forecast_log),
    # falls back to static floor calibrated from 75 resolved trades.
    
    # Try empirical std first (per-city, per-horizon)
    forecast = get_ensemble_forecast(city, date)
    _horizon_hours = None
    if forecast and not forecast.get("is_actual"):
        try:
            _target_dt = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            _horizon_hours = ((_target_dt - datetime.now(timezone.utc)).total_seconds()) / 3600
        except Exception:
            pass
    
    empirical = _get_empirical_std(city, _horizon_hours or 24) if _horizon_hours else None
    
    if bracket_width <= 2.0:
        # 1-2F brackets: use empirical if available, else 3.5F floor
        floor = empirical if empirical else 3.5
        effective_std = max(std, floor)
    elif bracket_width <= 5.0:
        # 3-5F brackets: use empirical if available, else 2.5F floor
        floor = empirical if empirical else 2.5
        effective_std = max(std, floor)
    else:
        # Wide ranges (>5F): ensemble std is adequate
        effective_std = max(std, empirical) if empirical else std
    
    # ── Bias correction: shift mean by empirical forecast error ──
    _CITY_ALIASES_PIR = {"nyc": "new york", "são paulo": "sao paulo", "dc": "washington"}
    bias = _get_empirical_bias(_CITY_ALIASES_PIR.get(city.lower(), city))
    if bias is not None:
        raw_mean = mean
        mean = mean + bias  # bias = AVG(actual - forecast); positive = forecast too low
        logger.info("Bias correction {}: raw_mean={:.1f}, bias={:+.2f}, corrected_mean={:.1f}",
                     city, raw_mean, bias, mean)
    
    z_low = (low_f - mean) / effective_std
    z_high = (high_f - mean) / effective_std
    
    # Always use Student-t for brackets (fatter tails = more realistic)
    if bracket_width <= 5.0 or n_sources <= 2:
        df = 5 if bracket_width <= 2.0 else 8
        p = _t_cdf(z_high, df=df) - _t_cdf(z_low, df=df)
    else:
        p = _norm_cdf(z_high) - _norm_cdf(z_low)
    
    if bracket_width <= 3.0:
        logger.debug(
            "prob_in_range %s [%.0f-%.0fF] (%.0fF bracket): "
            "mean=%.1f, raw_std=%.2f, eff_std=%.2f, p=%.4f",
            city, low_f, high_f, bracket_width, mean, std, effective_std, p
        )
    
    return {
        "probability": round(max(0, p), 4),
        "range_f": (low_f, high_f),
        "forecast_mean_f": mean,
        "forecast_std_f": effective_std,
        "raw_std_f": std,
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
        "visual_crossing": {
            "configured": bool(VISUAL_CROSSING_KEY),
            "key_required": True,
        },
        "nws": {
            "configured": True,
            "key_required": False,
            "note": "US cities only",
        },
        "weatherapi": {
            "configured": bool(WEATHERAPI_KEY),
            "key_required": True,
            "key_set": bool(WEATHERAPI_KEY),
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
            # "exact" °F market — use ±0.5°F range (rounds to nearest °F)
            # NOTE: Celsius exact markets should pre-compute the °F range
            # in weather_scanner.py (±0.5°C = ±0.9°F), so threshold_high_f
            # should already be set. This fallback is for °F-native markets only.
            threshold_high_f = threshold_f + 0.5
            threshold_f = threshold_f - 0.5
        result = prob_in_range(city, date, threshold_f, threshold_high_f)
    else:
        logger.warning("Unknown comparison type: {}", comparison)
        return None

    if not result:
        return None

    prob = result["probability"]
    n_sources = result["n_sources"]
    agreement = result["agreement"]

    # IMPROVEMENT 2: Spread-based confidence (Mar 17 2026)
    # Instead of counting sources, measure how TIGHTLY they agree.
    # Tight spread (std < 1.5°F) = high confidence in the forecast mean.
    # Wide spread (std > 5°F) = low confidence, sources disagree.
    # This directly reflects forecast precision, not just sample size.
    raw_std = result.get("raw_std_f", result.get("forecast_std_f", 3.0))
    cross_source_std = raw_std if raw_std > 0 else 3.0
    
    # Spread-based confidence: 0°F std → 95%, 2°F → 70%, 5°F → 40%, 8°F → 20%
    # Formula: conf = max(0.15, min(0.95, 1.0 - (std - 0.5) / 10))
    spread_conf = max(0.15, min(0.95, 1.0 - (cross_source_std - 0.5) / 10.0))
    
    # Source count bonus: more sources = slightly more confidence (max +10%)
    source_bonus = min(0.10, (n_sources - 1) * 0.02) if n_sources > 1 else 0
    
    # Agreement factor: penalize if sources spread > 5°F even with many sources
    agreement_factor = agreement  # 0-1, already computed from spread
    
    # Combined: spread drives 70%, agreement 20%, source bonus 10%
    confidence = min(0.70, spread_conf * 0.70 + agreement_factor * 0.20 + source_bonus)

    return {
        "fair_value": round(prob, 3),
        "confidence": round(confidence, 2),
        "forecast_mean_f": result["forecast_mean_f"],
        "forecast_std_f": result["forecast_std_f"],
        "n_sources": n_sources,
        "agreement": agreement,
        "distribution": result.get("distribution", "normal"),
    }



# ── IMPROVEMENT 3: Per-city per-source RMSE tracking ────────────────────
# Track each source's forecast accuracy per city. When calculating the
# ensemble mean, weight sources by their inverse RMSE for that city.
# Weather.com still gets a base 1.5x boost (resolution source), but a
# source that consistently nails a city gets up to 2x.

import sqlite3 as _sqlite3
import os as _os

_DB_PATH = _os.path.join(_os.path.dirname(__file__), "..", "storage", "shadow_trades.db")

def _ensure_source_rmse_table():
    """Create per-source RMSE tracking table if not exists."""
    try:
        conn = _sqlite3.connect(_DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS source_city_rmse (
                id INTEGER PRIMARY KEY,
                city TEXT NOT NULL,
                source TEXT NOT NULL,
                target_date TEXT NOT NULL,
                forecast_high_f REAL,
                actual_high_f REAL,
                error_f REAL,
                forecast_horizon_hours REAL,
                logged_at TEXT DEFAULT (datetime('now')),
                UNIQUE(city, source, target_date)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_scr_city_source 
            ON source_city_rmse(city, source)
        """)
        # Add horizon column if missing (migration for existing tables)
        try:
            conn.execute("ALTER TABLE source_city_rmse ADD COLUMN forecast_horizon_hours REAL")
        except Exception:
            pass  # Column already exists
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug("source_city_rmse table init: {}", e)

_ensure_source_rmse_table()


def log_source_forecasts(city: str, date: str, sources: dict):
    """Log individual source forecasts for later RMSE calculation.
    
    Called when we first get a forecast. Actual temps are filled in
    when the market resolves (via resolve_source_forecasts).
    """
    try:
        # GAP 5: Compute forecast horizon
        now = datetime.now(timezone.utc)
        target = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
        horizon_hours = max(0, (target - now).total_seconds() / 3600)
        
        conn = _sqlite3.connect(_DB_PATH)
        for name, src in sources.items():
            high_f = src.get("high_f")
            if high_f is None or high_f == 0:
                continue
            conn.execute("""
                INSERT OR IGNORE INTO source_city_rmse 
                (city, source, target_date, forecast_high_f, forecast_horizon_hours)
                VALUES (?, ?, ?, ?, ?)
            """, (city.lower(), name, date, high_f, round(horizon_hours, 1)))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug("log_source_forecasts error: {}", e)


def resolve_source_forecasts(city: str, date: str, actual_high_f: float):
    """Fill in actual temps for all source forecasts for a city/date.
    
    Called when we know the actual temperature (from TWC actuals or
    market resolution).
    """
    try:
        conn = _sqlite3.connect(_DB_PATH)
        conn.execute("""
            UPDATE source_city_rmse 
            SET actual_high_f = ?, error_f = forecast_high_f - ?
            WHERE city = ? AND target_date = ? AND actual_high_f IS NULL
        """, (actual_high_f, actual_high_f, city.lower(), date))
        
        # GAP 2: Also backfill forecast_log actuals
        conn.execute("""
            UPDATE forecast_log 
            SET actual_high_f = ?,
                forecast_error_f = ensemble_mean_f - ?,
                resolved_at = datetime('now')
            WHERE city = ? AND target_date = ? AND actual_high_f IS NULL
        """, (actual_high_f, actual_high_f, city.lower(), date))
        
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug("resolve_source_forecasts error: {}", e)


def log_ensemble_forecast(city: str, date: str, ensemble: dict):
    """Log ensemble forecast for accuracy tracking.
    
    Writes to forecast_log. Actual temps filled in later via
    resolve_source_forecasts().
    """
    try:
        # Compute forecast horizon: hours from now until target date ends
        now = datetime.now(timezone.utc)
        target = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
        horizon_hours = max(0, (target - now).total_seconds() / 3600)
        
        conn = _sqlite3.connect(_DB_PATH)
        conn.execute("""
            INSERT OR IGNORE INTO forecast_log
            (city, target_date, forecast_horizon_hours,
             ensemble_mean_f, ensemble_std_f, effective_std_f,
             n_sources, source_agreement)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            city.lower(), date, round(horizon_hours, 1),
            ensemble.get("high_mean_f"), ensemble.get("high_std_f"),
            ensemble.get("high_std_f"),
            ensemble.get("n_sources"), ensemble.get("source_agreement"),
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug("log_ensemble_forecast error: {}", e)


def get_source_weights_for_city(city: str, min_samples: int = 3) -> dict:
    """Get dynamic source weights based on per-city RMSE.
    
    Returns dict of {source_name: weight} where weight is relative
    (higher = more accurate for this city).
    
    Default weight = 1.0. Resolution source (weather_com) gets base 1.5x.
    Sources with lower RMSE than average get up to 2.0x.
    Sources with higher RMSE get scaled down to 0.5x minimum.
    
    Static adjustments (applied before dynamic scaling):
    - Pirate Weather: baseline 0.5x (consistently worst RMSE across cities)
    - Visual Crossing: 1.3x base for coastal cities (best on maritime climates)
    """
    # Coastal cities where Visual Crossing excels
    coastal_cities = {"wellington", "ankara", "tokyo", "paris", "miami", "seattle",
                      "san francisco", "los angeles", "san diego", "boston",
                      "new york", "new york city", "london", "sydney", "melbourne",
                      "vancouver", "portland", "honolulu", "lisbon", "barcelona"}
    
    default_weights = {
        "open_meteo_ensemble": 1.0,
        "pirate_weather": 0.5,  # Consistently worst RMSE — baseline penalty
        "tomorrow_io": 1.0,
        "weatherapi": 1.0,
        "weather_com": 1.5,  # Base boost for resolution source
        "visual_crossing": 1.3 if city.lower() in coastal_cities else 1.0,  # Coastal specialist
        "nws": 1.0,
    }
    
    try:
        conn = _sqlite3.connect(_DB_PATH)
        rows = conn.execute("""
            SELECT source, 
                   COUNT(*) as n,
                   AVG(error_f * error_f) as mse
            FROM source_city_rmse
            WHERE city = ? AND actual_high_f IS NOT NULL AND error_f IS NOT NULL
            GROUP BY source
            HAVING COUNT(*) >= ?
        """, (city.lower(), min_samples)).fetchall()
        conn.close()
        
        if not rows or len(rows) < 2:
            return default_weights
        
        # Calculate RMSE per source
        source_rmse = {}
        for source, n, mse in rows:
            rmse = mse ** 0.5 if mse and mse > 0 else 5.0
            source_rmse[source] = rmse
        
        if not source_rmse:
            return default_weights
        
        # Median RMSE as reference point
        rmse_values = sorted(source_rmse.values())
        median_rmse = rmse_values[len(rmse_values) // 2]
        
        if median_rmse <= 0:
            return default_weights
        
        # Weight = median / source_rmse (inverse RMSE, normalized)
        # Better source (lower RMSE) → higher weight
        # Clamped to [0.5, 2.0] range
        weights = dict(default_weights)  # Start with defaults
        for source, rmse in source_rmse.items():
            raw_weight = median_rmse / rmse if rmse > 0 else 1.0
            # Apply base weight (weather_com starts at 1.5x)
            base = default_weights.get(source, 1.0)
            dynamic_weight = base * max(0.5, min(2.0, raw_weight))
            weights[source] = round(dynamic_weight, 3)
        
        logger.info("Dynamic source weights for {}: {}", city, 
                    {k: f"{v:.2f}" for k, v in weights.items() if k in source_rmse})
        
        return weights
        
    except Exception as e:
        logger.debug("get_source_weights_for_city error: {}", e)
        return default_weights


# ── Dashboard status ─────────────────────────────────────────────────────


def get_ensemble_status() -> dict:
    """Dashboard-facing ensemble health snapshot.

    Reads source_health (uptime/latency), source_city_rmse (forecast skill),
    source_weights (current per-source weight), forecast_log (resolved
    ensemble forecasts), backtest_brackets (calibration), and paper_positions
    (weather P&L). Used by /api/signals/weather/ensemble-status.
    """
    weather_sources = (
        "weather_com", "open_meteo_ensemble", "visual_crossing",
        "weatherapi", "nws", "tomorrow_io", "pirate_weather", "open_meteo",
    )
    conn = _sqlite3.connect(_DB_PATH)
    conn.row_factory = _sqlite3.Row
    try:
        placeholders = ",".join("?" * len(weather_sources))
        # Name normalization: source_health uses `open_meteo` while
        # source_city_rmse uses `open_meteo_ensemble` for the same provider.
        # Map both ways in the join so health and RMSE row up under one record.
        sources = []
        for row in conn.execute(f"""
            SELECT sh.source,
                   sh.total_successes AS ok, sh.total_failures AS fail,
                   sh.avg_latency_ms, sh.last_success, sh.consecutive_failures,
                   r.n AS rmse_n, r.rmse_f, r.bias_f
            FROM source_health sh
            LEFT JOIN (
                SELECT source, COUNT(*) AS n,
                       SQRT(AVG(error_f * error_f)) AS rmse_f,
                       AVG(error_f) AS bias_f
                FROM source_city_rmse GROUP BY source
            ) r ON r.source = sh.source
                OR r.source = sh.source || '_ensemble'
                OR REPLACE(r.source, '_ensemble', '') = sh.source
            WHERE sh.source IN ({placeholders})
            ORDER BY r.rmse_f IS NULL, r.rmse_f ASC
        """, weather_sources):
            total = (row["ok"] or 0) + (row["fail"] or 0)
            sources.append({
                "name": row["source"],
                "uptime_pct": round(100.0 * row["ok"] / total, 2) if total else None,
                "avg_latency_ms": round(row["avg_latency_ms"] or 0, 0),
                "rmse_f": round(row["rmse_f"], 2) if row["rmse_f"] is not None else None,
                "bias_f": round(row["bias_f"], 2) if row["bias_f"] is not None else None,
                "n_samples": row["rmse_n"] or 0,
                "last_success": row["last_success"],
                "status": ("degraded" if (row["consecutive_failures"] or 0) >= 3
                           else ("ok" if total else "no_data")),
            })

        ens = conn.execute("""
            SELECT COUNT(*) AS n,
                   SQRT(AVG(forecast_error_f * forecast_error_f)) AS rmse_f,
                   AVG(ABS(forecast_error_f)) AS mae_f,
                   AVG(forecast_error_f) AS bias_f
            FROM forecast_log WHERE actual_high_f IS NOT NULL
        """).fetchone()

        cal = conn.execute("""
            SELECT COUNT(*) AS n,
                   AVG(CAST(hit AS REAL)) * 100.0 AS hit_rate_pct
            FROM backtest_brackets WHERE actual_high_f IS NOT NULL
        """).fetchone()

        by_comp = [dict(r) for r in conn.execute("""
            SELECT comparison, COUNT(*) AS n,
                   ROUND(100.0 * AVG(CAST(hit AS REAL)), 1) AS hit_pct,
                   ROUND(AVG(yes_final_price), 2) AS avg_market_price
            FROM backtest_brackets WHERE actual_high_f IS NOT NULL
            GROUP BY comparison ORDER BY n DESC
        """)]

        pnl = [dict(r) for r in conn.execute("""
            SELECT status, COUNT(*) AS n, ROUND(SUM(pnl), 2) AS pnl_usd
            FROM paper_positions WHERE archetype = 'weather' GROUP BY status
        """)]

        # Per-source, per-city accuracy
        src_city = [dict(r) for r in conn.execute("""
            SELECT source, city, COUNT(*) AS n,
                   ROUND(SQRT(AVG(error_f * error_f)), 2) AS rmse_f,
                   ROUND(AVG(error_f), 2) AS bias_f
            FROM source_city_rmse
            WHERE error_f IS NOT NULL
            GROUP BY source, city
            ORDER BY source, city
        """)]

        # GAP 4: Source correlation matrix — pairwise error correlation
        # For each pair of sources, compute how often they agree on direction
        src_corr = []
        src_list = [s["name"] for s in sources]
        for i in range(len(src_list)):
            for j in range(i + 1, len(src_list)):
                s1, s2 = src_list[i], src_list[j]
                rows = conn.execute(f"""
                    SELECT r1.error_f AS e1, r2.error_f AS e2
                    FROM source_city_rmse r1
                    JOIN source_city_rmse r2
                      ON r1.city = r2.city AND r1.target_date = r2.target_date
                    WHERE r1.source = ? AND r2.source = ?
                      AND r1.error_f IS NOT NULL AND r2.error_f IS NOT NULL
                """, (s1, s2)).fetchall()
                if len(rows) >= 3:
                    e1s = [r["e1"] for r in rows]
                    e2s = [r["e2"] for r in rows]
                    # Direction agreement: same sign (both over or both under)
                    same_dir = sum(1 for a, b in zip(e1s, e2s) if (a > 0) == (b > 0))
                    dir_agree = round(same_dir / len(rows), 3)
                    # Mean absolute difference
                    mad = round(sum(abs(a - b) for a, b in zip(e1s, e2s)) / len(rows), 2)
                    src_corr.append({
                        "source_a": s1, "source_b": s2,
                        "n": len(rows),
                        "direction_agreement": dir_agree,
                        "mean_abs_diff_f": mad,
                    })
        src_corr.sort(key=lambda x: x["direction_agreement"])

        # GAP 5: Horizon-bucketed accuracy from source_city_rmse
        horizon_accuracy = [dict(r) for r in conn.execute("""
            SELECT
              CASE
                WHEN forecast_horizon_hours <= 24 THEN '0-24h'
                WHEN forecast_horizon_hours <= 48 THEN '24-48h'
                WHEN forecast_horizon_hours <= 72 THEN '48-72h'
                ELSE '72h+'
              END AS horizon,
              COUNT(*) AS n,
              ROUND(SQRT(AVG(error_f * error_f)), 2) AS rmse_f,
              ROUND(AVG(ABS(error_f)), 2) AS mae_f,
              ROUND(AVG(error_f), 2) AS bias_f
            FROM source_city_rmse
            WHERE error_f IS NOT NULL AND forecast_horizon_hours IS NOT NULL
            GROUP BY horizon
            ORDER BY horizon
        """)]

        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "sources": sources,
            "ensemble": {
                "n_resolved": (ens["n"] if ens else 0) or 0,
                "rmse_f": round(ens["rmse_f"], 2) if ens and ens["rmse_f"] is not None else None,
                "mae_f": round(ens["mae_f"], 2) if ens and ens["mae_f"] is not None else None,
                "bias_f": round(ens["bias_f"], 2) if ens and ens["bias_f"] is not None else None,
            },
            "calibration": {
                "n_brackets": (cal["n"] if cal else 0) or 0,
                "hit_rate_pct": round(cal["hit_rate_pct"], 1) if cal and cal["hit_rate_pct"] is not None else None,
                "by_comparison": by_comp,
            },
            "paper_pnl": pnl,
            "source_city_accuracy": src_city,
            "source_correlation": src_corr,
            "horizon_accuracy": horizon_accuracy,
        }
    finally:
        conn.close()


# ── CLI demo ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG)
    
    city = sys.argv[1] if len(sys.argv) > 1 else "miami"
    date = sys.argv[2] if len(sys.argv) > 2 else (
        datetime.now(timezone.utc) + timedelta(days=1)
    ).strftime("%Y-%m-%d")

    logger.info(f"\n{'='*60}")
    logger.info(f"Weather Ensemble: {city} on {date}")
    logger.info(f"{'='*60}")
    
    # Source health
    health = source_health()
    logger.info(f"\nSources configured:")
    for src, info in health.items():
        if isinstance(info, dict):
            status = "✅" if info.get("configured") else "❌"
            logger.info(f"  {status} {src}")
    
    # Ensemble forecast
    forecast = get_ensemble_forecast(city, date)
    if forecast:
        ens = forecast["ensemble"]
        logger.info(f"\nEnsemble forecast:")
        logger.info(f"  High: {ens['high_mean_f']}°F ± {ens['high_std_f']}°F")
        logger.info(f"  Range: {ens['high_min_f']}°F — {ens['high_max_f']}°F")
        logger.info(f"  Sources: {ens['n_sources']} ({ens['n_models']} models)")
        logger.info(f"  Agreement: {ens['source_agreement']}")
        
        # Per-source
        logger.info(f"\nPer source:")
        for name, src in forecast["sources"].items():
            print(f"  {name}: {src['high_f']}°F" + 
                  (f" ± {src['high_std_f']}°F" if src.get('high_std_f') else "") +
                  (f" ({src.get('n_members', 1)} members)" if src.get('n_members', 1) > 1 else ""))
        
        # Example probability calculations
        mean = ens["high_mean_f"]
        logger.info(f"\nProbabilities:")
        for thresh in [mean - 5, mean - 2, mean, mean + 2, mean + 5]:
            r = prob_below(city, date, thresh)
            if r:
                logger.info(f"  P(high < {thresh:.0f}°F) = {r['probability']:.1%}")
        
        # Range example
        r = prob_in_range(city, date, mean - 1, mean + 1)
        if r:
            logger.info(f"  P({mean-1:.0f} ≤ high ≤ {mean+1:.0f}) = {r['probability']:.1%}")
    else:
        logger.info("No forecast data available")

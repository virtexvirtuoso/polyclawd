"""
Weather Ensemble — multi-source forecast aggregator with calibrated probabilities.

Sources:
  1. Open-Meteo Ensemble (30+ models, no key) — PRIMARY
  2. Pirate Weather (GEFS/ECMWF/HRRR, free key)
  3. Tomorrow.io (proprietary AI, free key)
  4. WeatherAPI.com (station blend, free key)

Produces probability distributions for temperature markets instead of
hardcoded fair-value buckets.
"""

import asyncio
import json
import logging
import os
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── API Keys (from env) ─────────────────────────────────────────────────
PIRATE_API_KEY = os.environ.get("PIRATE_WEATHER_KEY", "")
TOMORROW_API_KEY = os.environ.get("TOMORROW_IO_KEY", "")
WEATHERAPI_KEY = os.environ.get("WEATHERAPI_KEY", "")

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
CACHE_TTL = 3600  # 1 hour — forecasts update every 3-12h, no need to poll faster

# Rate limit tracking per source
_rate_limits = {
    "pirate_weather": {"calls": 0, "reset_ts": 0, "max_per_hour": 15, "max_per_month": 10000},
    "tomorrow_io": {"calls": 0, "reset_ts": 0, "max_per_hour": 20, "max_per_day": 450},
    "weatherapi": {"calls": 0, "reset_ts": 0, "max_per_hour": 50, "max_per_month": 95000},
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
        logger.debug("fetch failed %s: %s", url, e)
        return None


def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


# ── Source 1: Open-Meteo Ensemble (PRIMARY — no key needed) ─────────────

# Circuit breaker: skip Open-Meteo if rate limited (resets after 1h)
_open_meteo_blocked = False
_open_meteo_blocked_ts = 0.0

def _fetch_open_meteo_ensemble(lat: float, lon: float, date: str) -> Optional[dict]:
    """
    Fetch ensemble forecasts from multiple independent models.
    Returns dict with high temps from each ensemble member.
    """
    global _open_meteo_blocked, _open_meteo_blocked_ts
    if _open_meteo_blocked and (time.time() - _open_meteo_blocked_ts) < 3600:
        return None
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

        _open_meteo_blocked = True
        _open_meteo_blocked_ts = time.time()
        logger.info("Open-Meteo circuit breaker tripped (likely 429)")
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

    if not sources:
        logger.warning("No sources returned data for %s/%s", city, date)
        return None

    # ── Aggregate ────────────────────────────────────────────────────
    all_highs_f = []
    all_lows_f = []
    n_models = 0

    for name, src in sources.items():
        h = src.get("high_f")
        if h is not None and h != 0:
            all_highs_f.append(h)
        l = src.get("low_f")
        if l is not None and l != 0:
            all_lows_f.append(l)
        # Count models
        nm = src.get("n_members", 1)
        n_models += nm

    if not all_highs_f:
        return None

    high_mean = sum(all_highs_f) / len(all_highs_f)
    
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
    combined_std = max(cross_std, internal_std, 0.8)  # Floor 0.8°F
    if cross_std > 3.0:
        combined_std *= 1.3  # Fat tail penalty for disagreement
        logger.debug("Source disagreement >3°F for %s/%s: widening std %.1f → %.1f",
                      city, date, cross_std, combined_std)

    low_mean = sum(all_lows_f) / len(all_lows_f) if all_lows_f else None

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

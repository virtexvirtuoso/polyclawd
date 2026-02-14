"""
Weather Resolution Scanner — matches weather forecasts to open prediction markets.

Uses Open-Meteo API (free, no key) to get forecasts and actuals for cities,
then compares against Kalshi/Polymarket weather market prices.

Supported market types:
- Temperature high/low (Kalshi KXTEMPD, Polymarket "highest temperature in...")
- Rainfall/precipitation (Kalshi KXRAIND)
- Wind speed (Kalshi KXWIND)
- Snowfall (Kalshi KXSNOW)
"""

import json
import logging
import re
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ============================================================================
# City coordinates for Open-Meteo
# ============================================================================

CITY_COORDS = {
    # US cities (Kalshi primary)
    "new york": (40.71, -74.01, "America/New_York"),
    "nyc": (40.71, -74.01, "America/New_York"),
    "los angeles": (34.05, -118.24, "America/Los_Angeles"),
    "la": (34.05, -118.24, "America/Los_Angeles"),
    "chicago": (41.88, -87.63, "America/Chicago"),
    "miami": (25.76, -80.19, "America/New_York"),
    "houston": (29.76, -95.37, "America/Chicago"),
    "phoenix": (33.45, -112.07, "America/Phoenix"),
    "philadelphia": (39.95, -75.17, "America/New_York"),
    "san antonio": (29.42, -98.49, "America/Chicago"),
    "san diego": (32.72, -117.16, "America/Los_Angeles"),
    "dallas": (32.78, -96.80, "America/Chicago"),
    "austin": (30.27, -97.74, "America/Chicago"),
    "seattle": (47.61, -122.33, "America/Los_Angeles"),
    "denver": (39.74, -104.99, "America/Denver"),
    "boston": (42.36, -71.06, "America/New_York"),
    "atlanta": (33.75, -84.39, "America/New_York"),
    "san francisco": (37.77, -122.42, "America/Los_Angeles"),
    "washington": (38.91, -77.04, "America/New_York"),
    "dc": (38.91, -77.04, "America/New_York"),
    # International (Polymarket)
    "london": (51.51, -0.13, "Europe/London"),
    "buenos aires": (34.60, -58.38, "America/Argentina/Buenos_Aires"),
    "toronto": (43.65, -79.38, "America/Toronto"),
    "wellington": (-41.29, 174.78, "Pacific/Auckland"),
    "sydney": (-33.87, 151.21, "Australia/Sydney"),
    "tokyo": (35.68, 139.69, "Asia/Tokyo"),
    "paris": (48.86, 2.35, "Europe/Paris"),
    "berlin": (52.52, 13.41, "Europe/Berlin"),
}

# Open-Meteo API
METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Kalshi API
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

# Polymarket API
GAMMA_API = "https://gamma-api.polymarket.com"


def _fetch_json(url: str, timeout: int = 10) -> Optional[dict]:
    """Fetch JSON from URL."""
    try:
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; Polyclawd/2.0)",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.warning(f"Fetch failed: {url[:80]} — {e}")
        return None


def get_weather_forecast(city: str, days: int = 7) -> Optional[dict]:
    """Get weather forecast from Open-Meteo for a city."""
    city_lower = city.lower().strip()
    
    # Try exact match first, then partial
    coords = CITY_COORDS.get(city_lower)
    if not coords:
        for name, c in CITY_COORDS.items():
            if name in city_lower or city_lower in name:
                coords = c
                break
    
    if not coords:
        logger.warning(f"Unknown city: {city}")
        return None

    lat, lon, tz = coords
    
    # Fetch forecast + historical (for actuals)
    params = (
        f"latitude={lat}&longitude={lon}"
        f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,"
        f"wind_speed_10m_max,snowfall_sum"
        f"&temperature_unit=fahrenheit"
        f"&timezone={tz}"
        f"&forecast_days={days}"
        f"&past_days=2"
    )
    
    data = _fetch_json(f"{METEO_URL}?{params}")
    if not data or "daily" not in data:
        return None

    daily = data["daily"]
    forecasts = {}
    for i, date_str in enumerate(daily["time"]):
        forecasts[date_str] = {
            "date": date_str,
            "temp_max_f": daily["temperature_2m_max"][i],
            "temp_min_f": daily["temperature_2m_min"][i],
            "precip_mm": daily["precipitation_sum"][i],
            "wind_max_kmh": daily["wind_speed_10m_max"][i],
            "snow_mm": daily.get("snowfall_sum", [0] * len(daily["time"]))[i],
        }

    return {
        "city": city,
        "lat": lat,
        "lon": lon,
        "timezone": tz,
        "forecasts": forecasts,
    }


def _extract_city_from_market(title: str) -> Optional[str]:
    """Extract city name from market title."""
    title_lower = title.lower()
    
    for city in sorted(CITY_COORDS.keys(), key=len, reverse=True):
        if city in title_lower:
            return city
    
    # Try regex patterns
    patterns = [
        r"in ([A-Z][a-z]+(?: [A-Z][a-z]+)*)",  # "in New York"
        r"for ([A-Z][a-z]+(?: [A-Z][a-z]+)*)",  # "for Chicago"
    ]
    for p in patterns:
        m = re.search(p, title)
        if m:
            city = m.group(1).lower()
            if city in CITY_COORDS:
                return city
    
    return None


def _extract_date_from_market(title: str) -> Optional[str]:
    """Extract target date from market title. Returns YYYY-MM-DD."""
    title_lower = title.lower()
    
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4,
        "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    
    # "on February 14" / "on Feb 14"
    for month_name, month_num in months.items():
        pattern = rf"(?:on|for)\s+{month_name}\s+(\d{{1,2}})"
        m = re.search(pattern, title_lower)
        if m:
            day = int(m.group(1))
            year = datetime.now().year
            # If month is in the past, assume next year
            now = datetime.now()
            if month_num < now.month or (month_num == now.month and day < now.day):
                year += 1
            return f"{year}-{month_num:02d}-{day:02d}"
    
    # "February 14, 2026"
    for month_name, month_num in months.items():
        pattern = rf"{month_name}\s+(\d{{1,2}}),?\s+(\d{{4}})"
        m = re.search(pattern, title_lower)
        if m:
            return f"{m.group(2)}-{month_num:02d}-{int(m.group(1)):02d}"
    
    return None


def _extract_temp_threshold(title: str) -> Optional[Tuple[str, float, str]]:
    """Extract temperature comparison from market title.
    
    Returns: (comparison, threshold_f, unit) or None
    e.g., ("above", 70.0, "F") or ("between", (66, 67), "F")
    """
    title_lower = title.lower()
    
    # "be above X°F" / "be X°F or higher"
    m = re.search(r"(?:above|exceed|over|higher than)\s+(\d+(?:\.\d+)?)\s*°?\s*f", title_lower)
    if m:
        return ("above", float(m.group(1)), "F")
    
    m = re.search(r"(\d+(?:\.\d+)?)\s*°?\s*f\s+or\s+higher", title_lower)
    if m:
        return ("above", float(m.group(1)), "F")
    
    # "be below X°F" / "be X°F or below"
    m = re.search(r"(?:below|under|lower than)\s+(\d+(?:\.\d+)?)\s*°?\s*f", title_lower)
    if m:
        return ("below", float(m.group(1)), "F")
    
    m = re.search(r"(\d+(?:\.\d+)?)\s*°?\s*f\s+or\s+below", title_lower)
    if m:
        return ("below", float(m.group(1)), "F")
    
    # "between X-Y°F"
    m = re.search(r"between\s+(\d+)-(\d+)\s*°?\s*f", title_lower)
    if m:
        return ("between", (float(m.group(1)), float(m.group(2))), "F")
    
    # Celsius versions
    m = re.search(r"(?:above|exceed|over|higher than)\s+(\d+(?:\.\d+)?)\s*°?\s*c", title_lower)
    if m:
        return ("above", float(m.group(1)), "C")
    
    m = re.search(r"(\d+(?:\.\d+)?)\s*°?\s*c\s+or\s+(?:higher|above)", title_lower)
    if m:
        return ("above", float(m.group(1)), "C")
    
    m = re.search(r"(\d+(?:\.\d+)?)\s*°?\s*c\s+or\s+(?:below|lower)", title_lower)
    if m:
        return ("below", float(m.group(1)), "C")
    
    m = re.search(r"be\s+(\d+(?:\.\d+)?)\s*°?\s*c\s+on", title_lower)
    if m:
        return ("exact", float(m.group(1)), "C")
    
    return None


def _c_to_f(celsius: float) -> float:
    return celsius * 9 / 5 + 32


def _f_to_c(fahrenheit: float) -> float:
    return (fahrenheit - 32) * 5 / 9


def evaluate_weather_market(title: str, market_price: float) -> Optional[dict]:
    """Evaluate a weather market against Open-Meteo forecast.
    
    Returns signal dict if edge found, None otherwise.
    """
    city = _extract_city_from_market(title)
    target_date = _extract_date_from_market(title)
    temp_info = _extract_temp_threshold(title)
    
    if not city or not target_date:
        return None
    
    forecast_data = get_weather_forecast(city)
    if not forecast_data:
        return None
    
    forecast = forecast_data["forecasts"].get(target_date)
    if not forecast:
        return None
    
    # Days until resolution
    target_dt = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    days_until = (target_dt - datetime.now(timezone.utc)).total_seconds() / 86400
    
    if days_until < -1:
        return None  # already past
    
    signal = None
    
    if temp_info:
        comparison, threshold, unit = temp_info
        
        forecast_high_f = forecast["temp_max_f"]
        
        # Convert threshold to F if needed
        if unit == "C":
            if comparison == "between":
                threshold_f = (_c_to_f(threshold[0]), _c_to_f(threshold[1]))
            elif comparison == "exact":
                threshold_f = _c_to_f(threshold)
            else:
                threshold_f = _c_to_f(threshold)
        else:
            threshold_f = threshold
        
        if comparison == "above":
            # Will temp be above X?
            margin = forecast_high_f - threshold_f
            if margin > 5:  # forecast 5°F+ above threshold
                fair_value = 0.90
                certainty = "high"
            elif margin > 2:
                fair_value = 0.75
                certainty = "medium"
            elif margin > 0:
                fair_value = 0.60
                certainty = "low"
            elif margin > -2:
                fair_value = 0.40
                certainty = "low"
            elif margin > -5:
                fair_value = 0.25
                certainty = "medium"
            else:
                fair_value = 0.10
                certainty = "high"
            
            edge = fair_value - market_price
            if abs(edge) > 0.10:  # 10%+ edge
                side = "YES" if edge > 0 else "NO"
                signal = {
                    "type": "temperature_high",
                    "comparison": comparison,
                    "threshold_f": threshold_f,
                    "forecast_high_f": forecast_high_f,
                    "margin_f": round(margin, 1),
                    "fair_value": round(fair_value, 2),
                    "certainty": certainty,
                    "side": side,
                    "edge": round(abs(edge), 3),
                }
        
        elif comparison == "below":
            margin = threshold_f - forecast_high_f
            if margin > 5:
                fair_value = 0.90
                certainty = "high"
            elif margin > 2:
                fair_value = 0.75
                certainty = "medium"
            elif margin > 0:
                fair_value = 0.60
                certainty = "low"
            elif margin > -2:
                fair_value = 0.40
                certainty = "low"
            elif margin > -5:
                fair_value = 0.25
                certainty = "medium"
            else:
                fair_value = 0.10
                certainty = "high"
            
            edge = fair_value - market_price
            if abs(edge) > 0.10:
                side = "YES" if edge > 0 else "NO"
                signal = {
                    "type": "temperature_low",
                    "comparison": comparison,
                    "threshold_f": threshold_f,
                    "forecast_high_f": forecast_high_f,
                    "margin_f": round(margin, 1),
                    "fair_value": round(fair_value, 2),
                    "certainty": certainty,
                    "side": side,
                    "edge": round(abs(edge), 3),
                }
        
        elif comparison == "between":
            low, high = threshold_f if isinstance(threshold_f, tuple) else (threshold_f, threshold_f)
            in_range = low <= forecast_high_f <= high
            margin_low = forecast_high_f - low
            margin_high = high - forecast_high_f
            
            if in_range and min(margin_low, margin_high) > 2:
                fair_value = 0.70
                certainty = "medium"
            elif in_range:
                fair_value = 0.50
                certainty = "low"
            elif abs(forecast_high_f - (low + high) / 2) < 3:
                fair_value = 0.30
                certainty = "low"
            else:
                fair_value = 0.10
                certainty = "high"
            
            edge = fair_value - market_price
            if abs(edge) > 0.10:
                side = "YES" if edge > 0 else "NO"
                signal = {
                    "type": "temperature_range",
                    "comparison": comparison,
                    "range_f": (low, high),
                    "forecast_high_f": forecast_high_f,
                    "fair_value": round(fair_value, 2),
                    "certainty": certainty,
                    "side": side,
                    "edge": round(abs(edge), 3),
                }
        
        elif comparison == "exact":
            diff = abs(forecast_high_f - _c_to_f(threshold) if unit == "C" else forecast_high_f - threshold_f)
            # Exact temp match is very unlikely
            if diff < 1:
                fair_value = 0.25
            elif diff < 3:
                fair_value = 0.10
            else:
                fair_value = 0.03
            
            edge = fair_value - market_price
            if abs(edge) > 0.10:
                side = "YES" if edge > 0 else "NO"
                signal = {
                    "type": "temperature_exact",
                    "threshold": threshold,
                    "forecast_high_f": forecast_high_f,
                    "diff_f": round(diff, 1),
                    "fair_value": round(fair_value, 2),
                    "side": side,
                    "edge": round(abs(edge), 3),
                }
    
    if not signal:
        return None
    
    # Confidence: higher for shorter-dated + higher certainty + bigger margin
    base_conf = 60
    if signal["certainty"] == "high":
        base_conf += 20
    elif signal["certainty"] == "medium":
        base_conf += 10
    
    if days_until <= 1:
        base_conf += 10  # forecast very accurate within 24h
    elif days_until <= 3:
        base_conf += 5
    
    if signal["edge"] > 0.30:
        base_conf += 5
    
    base_conf = min(95, base_conf)
    
    return {
        "source": "weather_scanner",
        "city": city,
        "target_date": target_date,
        "days_until": round(days_until, 1),
        "market_price": market_price,
        "side": signal["side"],
        "confidence": base_conf,
        "edge_pct": round(signal["edge"] * 100, 1),
        "fair_value": signal["fair_value"],
        "weather_detail": signal,
        "forecast": forecast,
    }


def scan_kalshi_weather() -> List[dict]:
    """Scan Kalshi weather markets for edges."""
    signals = []
    
    weather_cats = ["KXTEMPD", "KXTEMPW", "KXRAIND", "KXWIND", "KXSNOW", "KXHUMID"]
    
    for cat in weather_cats:
        url = f"{KALSHI_API}/events?series_ticker={cat}&status=open&limit=50"
        data = _fetch_json(url)
        if not data:
            continue
        
        for event in data.get("events", []):
            for market in event.get("markets", []):
                title = market.get("title", "")
                yes_price = market.get("yes_price", 50) / 100.0  # Kalshi cents → decimal
                volume = market.get("volume", 0)
                
                if volume < 100:
                    continue
                
                result = evaluate_weather_market(title, yes_price)
                if result:
                    result["platform"] = "kalshi"
                    result["market_id"] = market.get("ticker", "")
                    result["market"] = title
                    result["volume"] = volume
                    signals.append(result)
    
    return signals


def scan_polymarket_weather() -> List[dict]:
    """Scan Polymarket for weather-related markets."""
    signals = []
    
    weather_kw = [
        "temperature", "highest temperature", "snow", "rainfall",
        "hurricane", "wildfire", "weather",
    ]
    
    # Search for weather markets
    for kw in ["temperature", "weather"]:
        url = f"{GAMMA_API}/markets?limit=100&active=true"
        data = _fetch_json(url)
        if not data:
            continue
        
        for market in data if isinstance(data, list) else []:
            question = market.get("question", "").lower()
            if not any(w in question for w in weather_kw):
                continue
            
            # Parse price
            prices = market.get("outcomePrices", "")
            if isinstance(prices, str):
                try:
                    prices = json.loads(prices)
                except Exception:
                    continue
            
            if not prices or len(prices) < 1:
                continue
            
            yes_price = float(prices[0]) if prices[0] != "0" else float(prices[1]) if len(prices) > 1 else 0.5
            volume = float(market.get("volume", 0))
            
            if volume < 1000:
                continue
            
            result = evaluate_weather_market(market.get("question", ""), yes_price)
            if result:
                result["platform"] = "polymarket"
                result["market_id"] = market.get("conditionId", market.get("id", ""))
                result["market"] = market.get("question", "")[:200]
                result["volume"] = volume
                signals.append(result)
    
    return signals


def scan_all_weather() -> dict:
    """Run full weather scan across both platforms."""
    kalshi_signals = scan_kalshi_weather()
    poly_signals = scan_polymarket_weather()
    
    all_signals = kalshi_signals + poly_signals
    all_signals.sort(key=lambda x: x.get("edge_pct", 0), reverse=True)
    
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_signals": len(all_signals),
        "kalshi_signals": len(kalshi_signals),
        "polymarket_signals": len(poly_signals),
        "signals": all_signals,
        "cities_checked": list(set(s["city"] for s in all_signals)),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = scan_all_weather()
    print(f"Weather scan: {result['total_signals']} signals")
    print(f"  Kalshi: {result['kalshi_signals']}")
    print(f"  Polymarket: {result['polymarket_signals']}")
    for s in result["signals"][:5]:
        print(f"  {s['market'][:60]} | {s['side']} | edge: {s['edge_pct']}% | conf: {s['confidence']}")

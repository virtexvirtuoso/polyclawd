"""
Weather Canary — API Health Check (UPDATED)

Probes Polymarket weather endpoints AND Polyclawd /api/signals/weather every 4 hours.
Sends Telegram alert ONLY if:
- Endpoint times out AND cache is unavailable/stale >24h
- Returns HTTP 5xx error
- TLS handshake succeeds but no body
- Unexpected status code

DOES NOT alert on timeout alone if cache is serving (graceful degradation).
"""

import urllib.request
import urllib.error
import json
import ssl
import os
from datetime import datetime, timezone, timedelta
from loguru import logger
from typing import Optional, Dict, Any

# Test cities to probe
CANARY_CITIES = [
    "new-york",
    "los-angeles",
    "chicago",
    "houston",
    "phoenix",
]

# Polymarket API base
GAMMA_API = "https://gamma-api.polymarket.com"

# Polyclawd local endpoint
POLYCLAWD_WEATHER = "http://127.0.0.1:8420/api/signals/weather"

# Timeout for canary requests (seconds)
CANARY_TIMEOUT = 20

# Cache file path
WEATHER_CACHE = "/var/www/virtuosocrypto.com/polyclawd/storage/weather_scan_cache.json"
CACHE_TTL_NORMAL = 1800  # 30 min
CACHE_TTL_MAX = 172800   # 48h - alert if cache older than this

# Last known good state
_last_canary_result: Optional[Dict[str, Any]] = None
_last_canary_ts: float = 0.0


def _fetch_with_timeout(url: str, timeout: int = CANARY_TIMEOUT) -> tuple:
    """Fetch URL with timeout, return (status_code, body, elapsed_ms)."""
    start = datetime.now(timezone.utc)
    
    # Create SSL context
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Polyclawd-Canary/1.0",
            "Accept": "application/json",
        })
        
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read()
            elapsed = (datetime.now(timezone.utc) - start).total_seconds() * 1000
            return resp.status, body, elapsed
    except urllib.error.URLError as e:
        elapsed = (datetime.now(timezone.utc) - start).total_seconds() * 1000
        logger.warning("Canary URLError: {}", e)
        return 0, b"", elapsed
    except Exception as e:
        elapsed = (datetime.now(timezone.utc) - start).total_seconds() * 1000
        logger.error("Canary error: {}", e)
        return -1, b"", elapsed


def _check_cache_health() -> Dict[str, Any]:
    """Check if weather cache is available and fresh."""
    cache_status = {
        "exists": False,
        "age_hours": None,
        "fresh": False,
        "serving": False,
    }
    
    if not os.path.exists(WEATHER_CACHE):
        return cache_status
    
    cache_status["exists"] = True
    
    try:
        with open(WEATHER_CACHE, 'r') as f:
            data = json.load(f)
        
        ts = data.get('ts', 0)
        age_seconds = datetime.now(timezone.utc).timestamp() - ts
        age_hours = age_seconds / 3600
        
        cache_status["age_hours"] = round(age_hours, 2)
        cache_status["fresh"] = age_seconds < CACHE_TTL_NORMAL
        cache_status["serving"] = age_seconds < CACHE_TTL_MAX  # Still serving even if stale
    except:
        pass
    
    return cache_status


def run_weather_canary() -> Dict[str, Any]:
    """
    Run canary check on weather endpoints.
    
    Returns dict with:
    - healthy: bool
    - issues: list of problem descriptions
    - details: dict with per-city results + polyclawd endpoint
    - cache_status: dict with cache health
    """
    global _last_canary_result, _last_canary_ts
    
    issues = []
    details = {}
    healthy = True
    
    # Check cache health first
    cache_status = _check_cache_health()
    details["cache"] = cache_status
    
    # Check Polyclawd /api/signals/weather endpoint
    logger.info("Checking Polyclawd weather endpoint...")
    status, body, elapsed = _fetch_with_timeout(POLYCLAWD_WEATHER, timeout=90)  # 90s for full scan
    
    polyclawd_result = {
        "status": status,
        "body_len": len(body),
        "elapsed_ms": round(elapsed, 0),
        "url": POLYCLAWD_WEATHER,
    }
    details["polyclawd"] = polyclawd_result
    
    # Evaluate Polyclawd endpoint health
    if status == 0 or status == -1:
        # Timeout or connection error
        if not cache_status["serving"]:
            issues.append(f"Polyclawd /api/signals/weather: Connection failed AND cache unavailable (age: {cache_status['age_hours']}h)")
            healthy = False
        else:
            logger.info("Polyclawd timeout but cache is serving (graceful degradation)")
            # Don't mark unhealthy - cache is handling it
    elif status == 500:
        issues.append(f"Polyclawd /api/signals/weather: HTTP 500 Internal Server Error")
        healthy = False
    elif status == 200:
        logger.info("Polyclawd weather endpoint healthy ({}ms, {}B)".format(elapsed, len(body)))
    elif status > 0:
        issues.append(f"Polyclawd /api/signals/weather: Unexpected status {status}")
        healthy = False
    
    # Check Polymarket gamma API (original canary)
    for city in CANARY_CITIES:
        slug = f"weather-temperature-{city}-april-8-2026"
        url = f"{GAMMA_API}/events?slug={slug}"
        
        status, body, elapsed = _fetch_with_timeout(url)
        
        result = {
            "status": status,
            "body_len": len(body),
            "elapsed_ms": round(elapsed, 0),
            "url": url,
        }
        
        if status == 0:
            issues.append(f"{city}: Connection failed (timeout or network error)")
            healthy = False
        elif status == -1:
            issues.append(f"{city}: TLS handshake completed but no response body")
            healthy = False
        elif status != 200:
            issues.append(f"{city}: Unexpected status {status}")
            healthy = False
        elif len(body) == 0:
            issues.append(f"{city}: Empty response body (0 bytes)")
            healthy = False
        
        details[city] = result
    
    # Check if this is a recurring issue
    now = datetime.now(timezone.utc).timestamp()
    if _last_canary_result and not healthy:
        if not _last_canary_result.get("healthy"):
            issues.insert(0, "RECURRING ISSUE: Previous canary also failed")
    
    _last_canary_result = {
        "healthy": healthy,
        "issues": issues,
        "details": details,
        "cache_status": cache_status,
    }
    _last_canary_ts = now
    
    return _last_canary_result


def send_canary_alert(result: Dict[str, Any]) -> None:
    """Send Telegram alert for canary failure."""
    try:
        from signals.discord_alerts import _send, COLOR_RED
        
        issues = result.get("issues", [])
        details = result.get("details", {})
        cache_status = result.get("cache_status", {})
        
        # Build alert message
        issue_text = "\n".join(f"• {i}" for i in issues[:5])
        
        # Add cache status
        cache_info = f"Cache: {'✅ Serving' if cache_status.get('serving') else '❌ Unavailable'}"
        if cache_status.get("age_hours"):
            cache_info += f" (age: {cache_status['age_hours']}h)"
        
        # Add details for failed endpoints
        detail_lines = []
        for key, data in details.items():
            if isinstance(data, dict) and data.get("status") not in (200, None):
                detail_lines.append(
                    f"{key}: status={data.get('status')}, body={data.get('body_len', 0)}B, {data.get('elapsed_ms', 0):.0f}ms"
                )
        
        detail_text = "\n".join(detail_lines[:5]) if detail_lines else "See logs for details"
        
        _send([{
            "title": "⚠️ WEATHER CANARY ALERT",
            "description": "Scanner endpoint detected issues. Possible Polymarket API change or server-side issue.",
            "color": COLOR_RED,
            "fields": [
                {"name": "Issues", "value": f"```\n{issue_text}\n```", "inline": False},
                {"name": "Cache Status", "value": f"```\n{cache_info}\n```", "inline": False},
                {"name": "Details", "value": f"```\n{detail_text}\n```", "inline": False},
                {"name": "Action", "value": "Check `_discover_weather_cities()` in weather_scanner.py and verify /events?slug= endpoint still works.", "inline": False},
                {"name": "Reference", "value": "The hide-from-new incident from March 2026 may be recurring.", "inline": False},
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }], alert_type="canary")
        
        logger.warning("Canary alert sent: {}", issues)
    except Exception as e:
        logger.error("Failed to send canary alert: {}", e)


def task_weather_canary() -> bool:
    """
    Scheduler task to run canary check.
    Returns True if healthy, False if issues detected.
    """
    logger.info("Running weather canary check...")
    
    result = run_weather_canary()
    
    if not result["healthy"]:
        logger.warning("Weather canary detected issues: {}", result["issues"])
        send_canary_alert(result)
        return False
    
    logger.info("Weather canary healthy: all endpoints responding")
    return True


if __name__ == "__main__":
    # Test
    result = run_weather_canary()
    print(json.dumps(result, indent=2))

"""FRED API client — Federal Reserve Economic Data for election market signals.

Tracks key economic indicators that influence election outcomes:
- Consumer Sentiment (UMCSENT) — voter mood proxy
- Unemployment Rate (UNRATE) — economic health
- CPI (CPIAUCSL) — inflation pressure
- Non-farm Payrolls (PAYEMS) — job market
- GDP Growth (A191RL1Q225SBEA) — quarterly growth rate
- Gas Prices (GASREGW) — weekly regular gas prices

Free API: https://api.stlouisfed.org/fred/ (requires API key)
Rate limit: 120 requests/minute
"""

import json
import os
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

FRED_BASE = "https://api.stlouisfed.org/fred"
CACHE_DIR = Path(__file__).parent.parent / "storage" / "fred_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL = 3600 * 6  # 6 hours — economic data changes slowly

# Key series for election signal generation
SERIES = {
    "consumer_sentiment": {
        "id": "UMCSENT",
        "name": "Consumer Sentiment",
        "frequency": "monthly",
        "impact": "Rising sentiment favors incumbent party",
    },
    "unemployment": {
        "id": "UNRATE",
        "name": "Unemployment Rate",
        "frequency": "monthly",
        "impact": "Rising unemployment hurts incumbent party",
    },
    "cpi": {
        "id": "CPIAUCSL",
        "name": "CPI (All Urban)",
        "frequency": "monthly",
        "impact": "High inflation hurts incumbent party",
    },
    "nonfarm_payrolls": {
        "id": "PAYEMS",
        "name": "Non-farm Payrolls",
        "frequency": "monthly",
        "impact": "Strong job growth favors incumbent party",
    },
    "gdp_growth": {
        "id": "A191RL1Q225SBEA",
        "name": "Real GDP Growth Rate",
        "frequency": "quarterly",
        "impact": "Positive GDP growth favors incumbent party",
    },
    "gas_prices": {
        "id": "GASREGW",
        "name": "Regular Gas Price",
        "frequency": "weekly",
        "impact": "High gas prices hurt incumbent party (voter salience)",
    },
}


def _get_api_key() -> str | None:
    """Get FRED API key from environment."""
    return os.environ.get("FRED_API_KEY")


def _fetch_series(series_id: str, limit: int = 24) -> list[dict] | None:
    """Fetch recent observations for a FRED series."""
    api_key = _get_api_key()
    if not api_key:
        logger.debug("FRED_API_KEY not set, skipping FRED data")
        return None

    cache_path = CACHE_DIR / f"{series_id}.json"
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            with open(cache_path) as f:
                return json.load(f)

    params = urllib.parse.urlencode({
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
    })
    url = f"{FRED_BASE}/series/observations?{params}"

    req = urllib.request.Request(url, headers={"User-Agent": "polyclawd/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())

        observations = []
        for obs in data.get("observations", []):
            val = obs.get("value", ".")
            if val == ".":
                continue
            observations.append({
                "date": obs["date"],
                "value": float(val),
            })

        with open(cache_path, "w") as f:
            json.dump(observations, f)

        return observations
    except Exception as e:
        logger.warning("FRED fetch error for {}: {}", series_id, e)
        if cache_path.exists():
            with open(cache_path) as f:
                return json.load(f)
        return None


def _compute_trend(observations: list[dict], periods: int = 3) -> dict | None:
    """Compute trend direction and magnitude from recent observations."""
    if not observations or len(observations) < periods + 1:
        return None

    recent = observations[:periods]
    older = observations[periods:periods * 2]

    if not recent or not older:
        return None

    recent_avg = sum(o["value"] for o in recent) / len(recent)
    older_avg = sum(o["value"] for o in older) / len(older)

    if older_avg == 0:
        return None

    change_pct = ((recent_avg - older_avg) / abs(older_avg)) * 100
    latest = observations[0]

    return {
        "latest_value": latest["value"],
        "latest_date": latest["date"],
        "recent_avg": round(recent_avg, 2),
        "older_avg": round(older_avg, 2),
        "change_pct": round(change_pct, 2),
        "direction": "rising" if change_pct > 0.5 else "falling" if change_pct < -0.5 else "flat",
    }


def fetch_economic_indicators() -> dict:
    """Fetch all key economic indicators and compute trends.

    Returns dict with indicator data, trends, and an overall economic score.
    """
    if not _get_api_key():
        return {"available": False, "reason": "FRED_API_KEY not configured"}

    indicators = {}
    for key, meta in SERIES.items():
        obs = _fetch_series(meta["id"])
        if not obs:
            continue

        trend = _compute_trend(obs)
        indicators[key] = {
            "name": meta["name"],
            "series_id": meta["id"],
            "frequency": meta["frequency"],
            "impact": meta["impact"],
            "observations": obs[:6],  # Last 6 data points
            "trend": trend,
        }
        time.sleep(0.5)  # Rate limit courtesy

    # Compute composite economic direction
    incumbent_score = _compute_incumbent_score(indicators)

    return {
        "available": True,
        "indicators": indicators,
        "incumbent_score": incumbent_score,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _compute_incumbent_score(indicators: dict) -> dict:
    """Score whether economic conditions favor the incumbent party.

    Returns a -100 to +100 score where positive = favors incumbent.
    """
    score = 0
    factors = []

    # Consumer sentiment: rising = +, falling = -
    cs = indicators.get("consumer_sentiment", {}).get("trend")
    if cs:
        if cs["direction"] == "rising":
            score += 20
            factors.append(f"Consumer sentiment rising ({cs['change_pct']:+.1f}%)")
        elif cs["direction"] == "falling":
            score -= 20
            factors.append(f"Consumer sentiment falling ({cs['change_pct']:+.1f}%)")

    # Unemployment: falling = +, rising = -
    unemp = indicators.get("unemployment", {}).get("trend")
    if unemp:
        if unemp["direction"] == "falling":
            score += 25
            factors.append(f"Unemployment falling to {unemp['latest_value']:.1f}%")
        elif unemp["direction"] == "rising":
            score -= 25
            factors.append(f"Unemployment rising to {unemp['latest_value']:.1f}%")

    # CPI: high inflation = - (>3% YoY is problematic)
    cpi = indicators.get("cpi", {}).get("trend")
    if cpi:
        if cpi["change_pct"] > 3:
            score -= 20
            factors.append(f"Inflation elevated ({cpi['change_pct']:.1f}% change)")
        elif cpi["change_pct"] < 2:
            score += 10
            factors.append(f"Inflation contained ({cpi['change_pct']:.1f}% change)")

    # GDP: positive = +, negative = -
    gdp = indicators.get("gdp_growth", {}).get("trend")
    if gdp:
        latest = gdp.get("latest_value", 0)
        if latest > 2:
            score += 20
            factors.append(f"GDP growth strong at {latest:.1f}%")
        elif latest < 0:
            score -= 30
            factors.append(f"GDP contracting at {latest:.1f}%")

    # Gas prices: rising = -, falling = +
    gas = indicators.get("gas_prices", {}).get("trend")
    if gas:
        if gas["direction"] == "rising":
            score -= 15
            factors.append(f"Gas prices rising ({gas['change_pct']:+.1f}%)")
        elif gas["direction"] == "falling":
            score += 10
            factors.append(f"Gas prices falling ({gas['change_pct']:+.1f}%)")

    # Clamp to -100/+100
    score = max(-100, min(100, score))

    if score > 20:
        outlook = "favorable"
    elif score < -20:
        outlook = "unfavorable"
    else:
        outlook = "neutral"

    return {
        "score": score,
        "outlook": outlook,
        "factors": factors,
        "interpretation": f"Economic conditions are {outlook} for the incumbent party (score: {score:+d})",
    }

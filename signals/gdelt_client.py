#!/usr/bin/env python3
"""GDELT DOC 2.0 client — news sentiment tracking for election candidates."""

import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, quote

from loguru import logger

GDELT_API = "https://api.gdeltproject.org/api/v2/doc/doc"
CACHE_DIR = Path(__file__).parent.parent / "storage" / "gdelt_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL = 7200  # 2 hours — GDELT 7-day spans don't need sub-hour freshness;
                  # shorter TTL causes both uvicorn workers to race on expiry → 429s

# Key candidates/topics to track sentiment for
# Keep query count low to respect GDELT rate limits (~12 req/min max)
ELECTION_QUERIES = {
    # Broad party sentiment (most valuable for market-level signals)
    "senate_dem": '("democrat" OR "democratic") senate 2026 sourcecountry:US',
    "senate_gop": '("republican" OR "GOP") senate 2026 sourcecountry:US',
    # Broad election sentiment
    "midterms_2026": '"2026 election" OR "2026 midterm" sourcecountry:US',
}

# State-level queries — limited to top 5 competitive races to manage rate limits
SWING_STATE_QUERIES = {
    "PA": "pennsylvania senate 2026",
    "MI": "michigan senate 2026",
    "NC": "north carolina senate 2026",
    "NJ": "new jersey senate 2026",
    "GA": "georgia senate 2026",
}


def _gdelt_get(query: str, mode: str = "timelinetone", timespan: str = "7d",
               timeout: int = 20) -> dict | list | None:
    """GET request to GDELT DOC 2.0 API with file-based caching."""
    params = {
        "query": query,
        "mode": mode,
        "format": "json",
        "timespan": timespan,
    }
    url = f"{GDELT_API}?{urlencode(params, quote_via=quote)}"

    # Cache key
    cache_key = f"gdelt_{mode}_{query[:60]}_{timespan}".replace(" ", "_").replace("/", "_").replace('"', "")[:120]
    cache_path = CACHE_DIR / f"{cache_key}.json"
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            with open(cache_path) as f:
                return json.load(f)

    req = urllib.request.Request(url, headers={"User-Agent": "polyclawd/1.0"})
    max_retries = 3
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                content_type = resp.headers.get("Content-Type", "")
                if "text/html" in content_type:
                    logger.warning("GDELT returned HTML instead of JSON for query: {}", query[:50])
                    if cache_path.exists():
                        with open(cache_path) as f:
                            return json.load(f)
                    return None
                data = json.loads(raw)
            with open(cache_path, "w") as f:
                json.dump(data, f)
            return data
        except Exception as e:
            is_429 = "429" in str(e) or "Too Many" in str(e)
            if is_429 and attempt < max_retries - 1:
                backoff = (attempt + 1) * 8  # 8s, 16s
                logger.info("GDELT rate-limited, retry {}/{} in {}s: {}", attempt + 1, max_retries, backoff, query[:50])
                time.sleep(backoff)
                continue
            logger.warning("GDELT API error for '{}': {}", query[:50], e)
            if cache_path.exists():
                with open(cache_path) as f:
                    return json.load(f)
            return None


def _extract_tone_series(data: dict | None) -> list[dict]:
    """Extract (date, tone_value) pairs from timelinetone response."""
    if not data or "timeline" not in data:
        return []
    for series in data["timeline"]:
        if "data" in series:
            return series["data"]
    return []


def _extract_volume_series(data: dict | None) -> list[dict]:
    """Extract (date, article_count) pairs from timelinevolraw response."""
    if not data or "timeline" not in data:
        return []
    for series in data["timeline"]:
        name = series.get("series", "")
        # Skip the "All Articles" normalization series
        if name.lower() == "all articles":
            continue
        if "data" in series:
            return series["data"]
    return []


def _avg_tone(series: list[dict]) -> float:
    """Calculate average tone from a time series."""
    if not series:
        return 0.0
    vals = [pt.get("value", 0) for pt in series]
    return sum(vals) / len(vals) if vals else 0.0


def _tone_trend(series: list[dict]) -> float:
    """Calculate tone trend (recent avg - older avg) from a time series.

    Positive = sentiment improving, Negative = sentiment deteriorating.
    """
    if len(series) < 4:
        return 0.0
    mid = len(series) // 2
    older = [pt.get("value", 0) for pt in series[:mid]]
    recent = [pt.get("value", 0) for pt in series[mid:]]
    if not older or not recent:
        return 0.0
    return (sum(recent) / len(recent)) - (sum(older) / len(older))


def fetch_candidate_sentiment(timespan: str = "7d") -> list[dict]:
    """Fetch sentiment data for all tracked election queries.

    Returns list of dicts with tone averages and trends per topic.
    """
    results = []
    for label, query in ELECTION_QUERIES.items():
        tone_data = _gdelt_get(query, mode="timelinetone", timespan=timespan)
        vol_data = _gdelt_get(query, mode="timelinevolraw", timespan=timespan)

        tone_series = _extract_tone_series(tone_data)
        vol_series = _extract_volume_series(vol_data)

        avg = _avg_tone(tone_series)
        trend = _tone_trend(tone_series)
        total_articles = sum(pt.get("value", 0) for pt in vol_series)

        results.append({
            "label": label,
            "query": query,
            "avg_tone": round(avg, 3),
            "tone_trend": round(trend, 3),
            "total_articles": int(total_articles),
            "data_points": len(tone_series),
            "timespan": timespan,
        })

        # Rate limit: 2 requests per query (tone + volume), ~12 req/min max
        # 10s sleep (was 8s) gives headroom when both uvicorn workers run simultaneously
        time.sleep(10)

    return results


def fetch_state_sentiment(states: list[str] | None = None, timespan: str = "7d") -> list[dict]:
    """Fetch news sentiment for competitive state races.

    Args:
        states: List of 2-letter state codes. None = all swing states.
        timespan: GDELT timespan (e.g. "7d", "1m")
    """
    queries = SWING_STATE_QUERIES
    if states:
        queries = {k: v for k, v in queries.items() if k in states}

    results = []
    for state, query in queries.items():
        tone_data = _gdelt_get(query, mode="timelinetone", timespan=timespan)
        vol_data = _gdelt_get(query, mode="timelinevolraw", timespan=timespan)

        tone_series = _extract_tone_series(tone_data)
        vol_series = _extract_volume_series(vol_data)

        avg = _avg_tone(tone_series)
        trend = _tone_trend(tone_series)
        total_articles = sum(pt.get("value", 0) for pt in vol_series)

        results.append({
            "state": state,
            "query": query,
            "avg_tone": round(avg, 3),
            "tone_trend": round(trend, 3),
            "total_articles": int(total_articles),
            "data_points": len(tone_series),
            "timespan": timespan,
        })

        time.sleep(6)  # was 0.5 — state queries also need inter-query pacing

    return results


def compute_narrative_shifts(candidate_sentiment: list[dict],
                              state_sentiment: list[dict]) -> list[dict]:
    """Detect significant narrative shifts that could move prediction markets.

    Returns list of shift alerts sorted by significance.
    """
    shifts = []

    # Candidate-level shifts: tone_trend magnitude > 1.0 is significant
    for s in candidate_sentiment:
        trend = s["tone_trend"]
        if abs(trend) < 0.8:
            continue
        direction = "improving" if trend > 0 else "deteriorating"
        shifts.append({
            "type": "candidate_narrative",
            "label": s["label"],
            "direction": direction,
            "magnitude": abs(trend),
            "avg_tone": s["avg_tone"],
            "articles": s["total_articles"],
            "detail": (
                f"News sentiment for '{s['label']}' is {direction} "
                f"(trend: {trend:+.2f}, avg tone: {s['avg_tone']:.2f}, "
                f"{s['total_articles']} articles)"
            ),
        })

    # State-level shifts: compare tone trends across states
    for s in state_sentiment:
        trend = s["tone_trend"]
        if abs(trend) < 1.0:
            continue
        direction = "positive" if trend > 0 else "negative"
        shifts.append({
            "type": "state_narrative",
            "state": s["state"],
            "direction": direction,
            "magnitude": abs(trend),
            "avg_tone": s["avg_tone"],
            "articles": s["total_articles"],
            "detail": (
                f"{s['state']} senate race sentiment shifting {direction} "
                f"(trend: {trend:+.2f}, {s['total_articles']} articles)"
            ),
        })

    # Sort by magnitude (biggest shifts first)
    shifts.sort(key=lambda x: x["magnitude"], reverse=True)
    return shifts


def build_gdelt_overlay() -> dict:
    """Build GDELT overlay for election report — called from generate_report().

    Returns dict with candidate_sentiment, state_sentiment, and narrative_shifts.
    """
    try:
        candidate = fetch_candidate_sentiment(timespan="7d")
        states = fetch_state_sentiment(timespan="7d")
        shifts = compute_narrative_shifts(candidate, states)
        logger.info("GDELT overlay: {} candidate topics, {} states, {} shifts",
                     len(candidate), len(states), len(shifts))
        return {
            "candidate_sentiment": candidate,
            "state_sentiment": states,
            "narrative_shifts": shifts,
        }
    except Exception as e:
        logger.warning("GDELT overlay failed: {}", e)
        return {
            "candidate_sentiment": [],
            "state_sentiment": [],
            "narrative_shifts": [],
        }

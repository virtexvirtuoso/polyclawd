#!/usr/bin/env python3
"""Wikipedia Pageviews API client — candidate attention spikes as election signals."""

import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

WIKI_API = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/all-agents"
CACHE_DIR = Path(__file__).parent.parent / "storage" / "wiki_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL = 3600  # 1 hour (daily data doesn't change fast)

# Competitive 2026 races — candidates to track
# Format: {wikipedia_title: {state, race, party, name}}
TRACKED_CANDIDATES = {
    # Senate — competitive races
    "Jon_Ossoff": {"state": "GA", "race": "senate", "party": "D", "name": "Jon Ossoff"},
    "Gary_Black_(politician)": {"state": "GA", "race": "senate", "party": "R", "name": "Gary Black"},
    "Elissa_Slotkin": {"state": "MI", "race": "senate", "party": "D", "name": "Elissa Slotkin"},
    "Susan_Collins": {"state": "ME", "race": "senate", "party": "R", "name": "Susan Collins"},
    "Thom_Tillis": {"state": "NC", "race": "senate", "party": "R", "name": "Thom Tillis"},
    "Jeanne_Shaheen": {"state": "NH", "race": "senate", "party": "D", "name": "Jeanne Shaheen"},
    "Tina_Smith": {"state": "MN", "race": "senate", "party": "D", "name": "Tina Smith"},
    "Ted_Cruz": {"state": "TX", "race": "senate", "party": "R", "name": "Ted Cruz"},
    "John_Cornyn": {"state": "TX", "race": "senate", "party": "R", "name": "John Cornyn"},
    "Ken_Paxton": {"state": "TX", "race": "senate", "party": "R", "name": "Ken Paxton"},
    "J._D._Vance": {"state": "OH", "race": "senate", "party": "R", "name": "JD Vance"},
    "Lisa_Murkowski": {"state": "AK", "race": "senate", "party": "R", "name": "Lisa Murkowski"},
    "Joni_Ernst": {"state": "IA", "race": "senate", "party": "R", "name": "Joni Ernst"},
    # Governor — competitive races
    "Stacey_Abrams": {"state": "GA", "race": "governor", "party": "D", "name": "Stacey Abrams"},
    "Gretchen_Whitmer": {"state": "MI", "race": "governor", "party": "D", "name": "Gretchen Whitmer"},
    "Tony_Evers": {"state": "WI", "race": "governor", "party": "D", "name": "Tony Evers"},
    # Presidential 2028
    "Gavin_Newsom": {"state": "", "race": "presidential", "party": "D", "name": "Gavin Newsom"},
    "Ron_DeSantis": {"state": "", "race": "presidential", "party": "R", "name": "Ron DeSantis"},
    "Alexandria_Ocasio-Cortez": {"state": "", "race": "presidential", "party": "D", "name": "AOC"},
    "Marco_Rubio": {"state": "", "race": "presidential", "party": "R", "name": "Marco Rubio"},
}

# Minimum days of history needed for baseline
MIN_HISTORY_DAYS = 14
# Z-score threshold for "spike"
SPIKE_THRESHOLD = 2.0


def _fetch_pageviews(title: str, days: int = 60) -> list[dict] | None:
    """Fetch daily pageviews for a Wikipedia article."""
    cache_path = CACHE_DIR / f"{title}.json"
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            with open(cache_path) as f:
                return json.load(f)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    start_str = start.strftime("%Y%m%d")
    end_str = end.strftime("%Y%m%d")

    url = f"{WIKI_API}/{title}/daily/{start_str}/{end_str}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "polyclawd/1.0 (election market analysis)",
    })

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        items = data.get("items", [])
        with open(cache_path, "w") as f:
            json.dump(items, f)
        return items
    except Exception as e:
        logger.debug("Wiki pageviews error for {}: {}", title, e)
        if cache_path.exists():
            with open(cache_path) as f:
                return json.load(f)
        return None


def _compute_spike(items: list[dict], window: int = 30) -> dict | None:
    """Compute z-score of latest pageviews vs rolling average.

    Returns spike info or None if insufficient data.
    """
    if not items or len(items) < MIN_HISTORY_DAYS:
        return None

    views = [item.get("views", 0) for item in items]

    # Use last `window` days as baseline (excluding the most recent day)
    if len(views) < window + 1:
        baseline = views[:-1]
    else:
        baseline = views[-(window + 1):-1]

    if not baseline:
        return None

    mean = sum(baseline) / len(baseline)
    if mean < 50:
        return None  # Too low traffic to be meaningful

    variance = sum((v - mean) ** 2 for v in baseline) / len(baseline)
    stdev = variance ** 0.5
    if stdev < 1:
        stdev = 1  # Prevent division by zero

    latest = views[-1]
    z_score = (latest - mean) / stdev

    return {
        "latest_views": latest,
        "avg_30d": round(mean, 0),
        "stdev": round(stdev, 1),
        "z_score": round(z_score, 2),
        "pct_above_avg": round((latest / mean - 1) * 100, 0) if mean > 0 else 0,
        "is_spike": z_score >= SPIKE_THRESHOLD,
    }


def fetch_candidate_pageviews() -> list[dict]:
    """Fetch pageviews for all tracked candidates and detect attention spikes."""
    results = []

    for title, meta in TRACKED_CANDIDATES.items():
        items = _fetch_pageviews(title)
        if not items:
            continue

        spike = _compute_spike(items)
        if not spike:
            continue

        results.append({
            "candidate": meta["name"],
            "wikipedia_title": title,
            "state": meta["state"],
            "race": meta["race"],
            "party": meta["party"],
            **spike,
        })

    # Sort by z-score descending (biggest spikes first)
    results.sort(key=lambda x: -x["z_score"])

    spikes = [r for r in results if r["is_spike"]]
    logger.info("Wiki pageviews: tracked {} candidates, {} spikes detected",
                len(results), len(spikes))
    return results


def build_wiki_overlay() -> dict:
    """Build Wikipedia pageviews overlay for election report."""
    candidates = fetch_candidate_pageviews()
    spikes = [c for c in candidates if c["is_spike"]]

    return {
        "wiki_pageviews": candidates,
        "wiki_spikes": spikes,
        "wiki_tracked": len(candidates),
    }

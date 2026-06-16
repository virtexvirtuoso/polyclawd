#!/usr/bin/env python3
"""Manifold Markets API client — election prediction market data for cross-platform comparison."""

import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from loguru import logger

from signals.election_tracker import classify_race, _extract_state

MANIFOLD_API = "https://api.manifold.markets/v0"
CACHE_DIR = Path(__file__).parent.parent / "storage" / "manifold_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL = 900  # 15 minutes

# Search terms to discover election markets
SEARCH_TERMS = [
    "2026 senate",
    "2028 president",
    "2026 governor",
    "2026 house",
]


def _manifold_get(endpoint: str, params: dict = None, timeout: int = 15) -> any:
    """GET request to Manifold API with file-based caching."""
    params = params or {}
    url = f"{MANIFOLD_API}{endpoint}?{urlencode(params)}"

    cache_key = url.replace("/", "_").replace("?", "_").replace("&", "_")[:120]
    cache_path = CACHE_DIR / f"{cache_key}.json"
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL:
            with open(cache_path) as f:
                return json.load(f)

    req = urllib.request.Request(url, headers={"User-Agent": "polyclawd/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        with open(cache_path, "w") as f:
            json.dump(data, f)
        return data
    except Exception as e:
        logger.warning("Manifold API error on {}: {}", endpoint, e)
        if cache_path.exists():
            with open(cache_path) as f:
                return json.load(f)
        return []


def _ts_to_iso(ts_ms: any) -> str:
    """Convert millisecond timestamp to ISO date string."""
    if not ts_ms:
        return ""
    try:
        ts = int(ts_ms) / 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError, OSError):
        return ""


def fetch_manifold_elections() -> list[dict]:
    """Search Manifold for election markets, dedupe, return normalized list."""
    seen_ids = set()
    results = []

    for term in SEARCH_TERMS:
        data = _manifold_get("/search-markets", {
            "term": term,
            "sort": "liquidity",
            "limit": 50,
        })
        if not isinstance(data, list):
            continue

        for m in data:
            market_id = m.get("id", "")
            if not market_id or market_id in seen_ids:
                continue
            seen_ids.add(market_id)

            question = m.get("question", "")
            probability = m.get("probability")
            if probability is None:
                continue  # Skip non-binary or resolved markets

            race_cat = classify_race(question)
            state = _extract_state(question)
            close_time = _ts_to_iso(m.get("closeTime"))
            volume = m.get("volume", 0) or 0

            results.append({
                "id": f"manifold_{market_id}",
                "platform": "manifold",
                "question": question,
                "race_category": race_cat,
                "state": state,
                "outcomes": [
                    {"name": "Yes", "price": round(probability, 4)},
                    {"name": "No", "price": round(1 - probability, 4)},
                ],
                "volume": round(volume, 2),
                "end_date": close_time,
            })

    logger.info("Manifold: fetched {} election markets", len(results))
    return results


def compute_manifold_spreads(
    manifold_markets: list[dict],
    polymarket_markets: list[dict],
) -> list[dict]:
    """Find divergences between Manifold and Polymarket election markets.

    Matches by (state, race_category). Returns list sorted by spread magnitude.
    """
    def _build_lookup(markets: list[dict]) -> dict:
        lookup = {}
        for m in markets:
            state = m.get("state", "")
            race = m.get("race_category", "")
            if not state or race == "other":
                continue
            key = (state, race)
            outcomes = m.get("outcomes", [])
            d_price = None
            for o in outcomes:
                name = o.get("name", "").lower()
                if "democrat" in name or name == "yes":
                    d_price = o.get("price", 0)
                    break
            if d_price is None and outcomes:
                d_price = outcomes[0].get("price", 0)
            if d_price is not None:
                lookup[key] = d_price
        return lookup

    mf_lookup = _build_lookup(manifold_markets)
    poly_lookup = _build_lookup(polymarket_markets)

    common_keys = set(mf_lookup.keys()) & set(poly_lookup.keys())

    spreads = []
    for key in common_keys:
        state, race = key
        mf_d = mf_lookup[key]
        poly_d = poly_lookup[key]
        spread = abs(mf_d - poly_d)

        if spread < 0.01:
            continue  # Less than 1pp

        spreads.append({
            "state": state,
            "race": race,
            "manifold_d": round(mf_d, 4),
            "polymarket_d": round(poly_d, 4),
            "spread_pp": round(spread * 100, 1),
        })

    spreads.sort(key=lambda x: -x["spread_pp"])
    logger.info("Manifold spreads: found {} divergences vs Polymarket", len(spreads))
    return spreads

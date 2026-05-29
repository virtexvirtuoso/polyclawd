#!/usr/bin/env python3
"""Polymarket price-history fetcher for CLARITY passage markets.

Two-step fetch:
  1. gamma-api.polymarket.com/markets?slug=... -> clobTokenIds[0] (YES token)
  2. clob.polymarket.com/prices-history?market={token}&interval=max&fidelity=1440

Returns downsampled daily points so the dashboard can render a real
price trajectory instead of fabricating confidence bands.
"""

import json
import time
import urllib.request
from pathlib import Path

from loguru import logger

CACHE_DIR = Path(__file__).parent.parent / "storage" / "polymarket_price_history_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL = 15 * 60  # 15 min — prices move intraday

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


def _fetch_json(url: str, timeout: int = 15):
    req = urllib.request.Request(url, headers={"User-Agent": "polyclawd/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _cache_get(key: str):
    p = CACHE_DIR / f"{key}.json"
    if not p.exists():
        return None
    if time.time() - p.stat().st_mtime > CACHE_TTL:
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _cache_set(key: str, value):
    try:
        (CACHE_DIR / f"{key}.json").write_text(json.dumps(value))
    except Exception as e:
        logger.warning("polymarket price cache write failed for {}: {}", key, e)


def get_yes_token_id(slug: str) -> str | None:
    """Resolve a Polymarket slug → YES clob token id via gamma API."""
    if not slug:
        return None
    cached = _cache_get(f"token_{slug}")
    if cached and cached.get("token_id"):
        return cached["token_id"]
    try:
        data = _fetch_json(f"{GAMMA_BASE}/markets?slug={slug}")
        if not isinstance(data, list) or not data:
            return None
        m = data[0]
        raw = m.get("clobTokenIds")
        # clobTokenIds can be a JSON-encoded string or a real list
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                return None
        if not isinstance(raw, list) or not raw:
            return None
        token_id = str(raw[0])  # [0] = YES, [1] = NO
        _cache_set(f"token_{slug}", {"token_id": token_id})
        return token_id
    except Exception as e:
        logger.warning("polymarket slug->token lookup failed for {}: {}", slug, e)
        return None


def get_price_history(slug: str, max_points: int = 60) -> list[dict] | None:
    """Fetch price history for a Polymarket market by slug.

    Returns a list of {t, p} dicts (t = unix seconds, p = YES price 0..1),
    downsampled to at most `max_points` evenly spaced samples.
    """
    if not slug:
        return None

    cache_key = f"hist_{slug}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached.get("points")

    token_id = get_yes_token_id(slug)
    if not token_id:
        return None

    try:
        url = f"{CLOB_BASE}/prices-history?market={token_id}&interval=max&fidelity=1440"
        data = _fetch_json(url)
        raw = data.get("history") or []
        if not raw:
            return None

        # Normalize + downsample
        pts = [{"t": int(x["t"]), "p": float(x["p"])} for x in raw if "t" in x and "p" in x]
        pts.sort(key=lambda x: x["t"])

        if len(pts) > max_points:
            step = len(pts) / max_points
            sampled = [pts[int(i * step)] for i in range(max_points)]
            # Always include the very last point so "now" is accurate
            if sampled[-1]["t"] != pts[-1]["t"]:
                sampled.append(pts[-1])
            pts = sampled

        _cache_set(cache_key, {"points": pts})
        return pts
    except Exception as e:
        logger.warning("polymarket price history fetch failed for {}: {}", slug, e)
        # Fall back to stale cache if present
        stale = CACHE_DIR / f"{cache_key}.json"
        if stale.exists():
            try:
                return json.loads(stale.read_text()).get("points")
            except Exception:
                pass
        return None


if __name__ == "__main__":
    import sys
    slug = sys.argv[1] if len(sys.argv) > 1 else "clarity-act-signed-into-law-in-2026"
    pts = get_price_history(slug)
    if pts:
        print(f"{slug}: {len(pts)} points")
        for p in pts[:5] + [{"t": "...", "p": "..."}] + pts[-5:]:
            print(f"  {p}")
    else:
        print(f"{slug}: no data")

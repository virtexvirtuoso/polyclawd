"""
Statcast / Baseball Savant enrichment — xStats, exit velocity, spin rate, chase rate.

Free CSV endpoints, no API key needed. Cache per player per day to avoid throttling.
"""
from __future__ import annotations

import csv
import io
import time
import urllib.request
from datetime import date
from typing import Dict, Optional

from loguru import logger

SAVANT_BASE = "https://baseballsavant.mlb.com/leaderboard"

# ── Caches (daily TTL) ────────────────────────────────────────────────────────
_batter_cache: Dict[str, dict] = {}
_pitcher_cache: Dict[str, dict] = {}
_cache_date: Optional[str] = None


def _flush_if_stale():
    global _cache_date
    today = date.today().isoformat()
    if _cache_date != today:
        _batter_cache.clear()
        _pitcher_cache.clear()
        _cache_date = today


def _fetch_csv(url: str) -> list[dict]:
    """Fetch a Savant CSV endpoint and return list of dicts."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(raw))
        return [dict(r) for r in reader]
    except Exception as e:
        logger.warning(f"Savant fetch failed: {e}")
        return []


def _load_batters(year: int = 2026) -> Dict[int, dict]:
    """Load all qualified batters' xStats."""
    _flush_if_stale()
    if _batter_cache:
        return _batter_cache

    url = f"{SAVANT_BASE}/expected_statistics?type=batter&year={year}&position=&team=&min=50&csv=true"
    rows = _fetch_csv(url)
    for r in rows:
        try:
            pid = int(r.get("player_id", 0))
            _batter_cache[pid] = {
                "name": r.get("last_name, first_name", "").strip('"'),
                "pa": int(r.get("pa", 0)),
                "ba": float(r.get("ba", 0) or 0),
                "xba": float(r.get("est_ba", 0) or 0),
                "slg": float(r.get("slg", 0) or 0),
                "xslg": float(r.get("est_slg", 0) or 0),
                "woba": float(r.get("woba", 0) or 0),
                "xwoba": float(r.get("est_woba", 0) or 0),
            }
        except (ValueError, TypeError):
            continue
    logger.info(f"Statcast: loaded {len(_batter_cache)} batters")
    return _batter_cache


def _load_pitchers(year: int = 2026) -> Dict[int, dict]:
    """Load all qualified pitchers' xStats."""
    _flush_if_stale()
    if _pitcher_cache:
        return _pitcher_cache

    url = f"{SAVANT_BASE}/expected_statistics?type=pitcher&year={year}&position=&team=&min=50&csv=true"
    rows = _fetch_csv(url)
    for r in rows:
        try:
            pid = int(r.get("player_id", 0))
            _pitcher_cache[pid] = {
                "name": r.get("last_name, first_name", "").strip('"'),
                "pa": int(r.get("pa", 0)),
                "ba": float(r.get("ba", 0) or 0),
                "xba": float(r.get("est_ba", 0) or 0),
                "era": r.get("era", ""),
                "xera": r.get("xera", ""),
                "woba": float(r.get("woba", 0) or 0),
                "xwoba": float(r.get("est_woba", 0) or 0),
            }
        except (ValueError, TypeError):
            continue
    logger.info(f"Statcast: loaded {len(_pitcher_cache)} pitchers")
    return _pitcher_cache


def get_batter_xstats(player_id: int) -> Optional[dict]:
    """Get xBA, xSLG, xwOBA for a batter."""
    batters = _load_batters()
    return batters.get(player_id)


def get_pitcher_xstats(player_id: int) -> Optional[dict]:
    """Get xBA-against, xERA, xwOBA-against for a pitcher."""
    pitchers = _load_pitchers()
    return pitchers.get(player_id)


def statcast_adjustment(player_id: int, market_key: str) -> Optional[float]:
    """
    Returns a multiplier (0.8-1.2) based on Statcast xStats.

    For batters (HR/hits/TB/RBI): xSLG vs SLG tells us if a batter is
    over/underperforming on contact quality. xSLG > SLG = unlucky = positive adj.

    For pitchers (K): xwOBA-against vs wOBA-against. Higher xwOBA = batters
    hitting the ball harder than results show = pitcher is lucky = negative adj
    (fewer Ks expected as luck corrects).
    """
    is_pitcher = market_key.startswith("pitcher_")

    if is_pitcher:
        stats = get_pitcher_xstats(player_id)
        if not stats:
            return None
        # Pitcher K props: if xwOBA > wOBA, pitcher is getting lucky
        # (batters making harder contact than results show) → fewer Ks coming
        xw = stats.get("xwoba", 0)
        w = stats.get("woba", 0)
        if w == 0:
            return None
        # Ratio: if xwOBA < wOBA (pitcher better than results), boost K expectation
        ratio = w / xw if xw > 0 else 1.0
        # Clamp to 0.85-1.15 range
        return max(0.85, min(1.15, ratio))
    else:
        stats = get_batter_xstats(player_id)
        if not stats:
            return None
        # Batter props: xSLG vs SLG
        xslg = stats.get("xslg", 0)
        slg = stats.get("slg", 0)
        if slg == 0:
            return None
        # If xSLG > SLG, batter is unlucky (positive regression expected)
        ratio = xslg / slg if slg > 0 else 1.0
        return max(0.85, min(1.15, ratio))


def enrich_with_statcast(row: dict) -> dict:
    """
    Add Statcast adjustment to a prop scout row.
    Expects row to have '_player_id' and 'market' keys.
    """
    row = dict(row)
    pid = row.get("_player_id")
    market = row.get("market", "")
    if not pid:
        return row

    adj = statcast_adjustment(pid, market)
    if adj is not None:
        row["statcast_adj"] = round(adj, 3)
        # Adjust hit rate by statcast factor
        base_hr = row.get("hit_rate_pct", 0) or 0
        if base_hr > 0:
            adj_hr = min(base_hr * adj, 99.0)
            row["statcast_adj_hit_rate"] = round(adj_hr, 1)

        # Get raw xStats for display
        is_pitcher = market.startswith("pitcher_")
        stats = get_pitcher_xstats(pid) if is_pitcher else get_batter_xstats(pid)
        if stats:
            row["xstats"] = stats

    return row

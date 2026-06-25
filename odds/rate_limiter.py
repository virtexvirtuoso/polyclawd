"""
The Odds API Rate Limiter

Free tier: 500 calls/month
Strategy: Smart allocation based on event importance

Usage tracking stored in JSON file, resets monthly.
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple
from dataclasses import dataclass, asdict
from loguru import logger

# Config — The Odds API LIVE plan: 100K credits/month (free 500 tier is dead).
# Daily soft budget is computed dynamically (remaining // days_left) which yields
# ~3.2K/day at full balance, matching the plan's ~3K/day target.
MONTHLY_LIMIT = 5_000_000  # the-odds-api plan upgraded 100K -> 5M on 2026-06-25
DAILY_BUDGET = MONTHLY_LIMIT // 31  # ~161K/day soft budget (was ~3.2K on 100K plan)
CACHE_DIR = Path("/var/www/virtuosocrypto.com/polyclawd/cache")
RATE_FILE = CACHE_DIR / "odds_api_usage.json"

# Fallback for local dev
if not CACHE_DIR.exists():
    CACHE_DIR = Path.home() / "Desktop/polyclawd/cache"
    RATE_FILE = CACHE_DIR / "odds_api_usage.json"
    CACHE_DIR.mkdir(exist_ok=True)

# Hard floor: do not spend below this many REAL credits remaining (reserved for
# "critical" priority only). 5% of the 5M plan (was 5,000 on the dead 100K plan).
CREDIT_FLOOR = 250_000
# Low-credit Discord alert fires when real remaining drops below this (20% of the
# 5M plan) — early warning with weeks of runway, not minutes (was 20,000 on 100K).
LOW_CREDIT_WATERMARK = 1_000_000

# Authoritative real-credit balance, written by odds.the_odds_api from the live
# `x-requests-remaining` header on every fetch so it survives process restarts.
# rate_limiter's own JSON counter is only an estimate; this file is the truth.
REAL_CREDIT_FILE = CACHE_DIR / "odds_api_credit.json"


@dataclass
class UsageStats:
    month: str  # "2026-02"
    calls_used: int
    calls_remaining: int
    last_call: Optional[str]
    daily_calls: dict  # {"2026-02-08": 5, ...}


def _load_usage() -> UsageStats:
    """Load current usage stats from file."""
    current_month = datetime.now().strftime("%Y-%m")

    if RATE_FILE.exists():
        try:
            with open(RATE_FILE) as f:
                data = json.load(f)
                # Reset if new month
                if data.get("month") != current_month:
                    return UsageStats(
                        month=current_month, calls_used=0, calls_remaining=MONTHLY_LIMIT, last_call=None, daily_calls={}
                    )
                # Recompute remaining against the CURRENT limit — persisted
                # values written under an older (smaller) MONTHLY_LIMIT would
                # otherwise pin calls_remaining at 0 for the rest of the month.
                data["calls_remaining"] = max(0, MONTHLY_LIMIT - int(data.get("calls_used", 0)))
                return UsageStats(**data)
        except:
            pass

    return UsageStats(month=current_month, calls_used=0, calls_remaining=MONTHLY_LIMIT, last_call=None, daily_calls={})


def _save_usage(stats: UsageStats):
    """Save usage stats to file."""
    RATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RATE_FILE, "w") as f:
        json.dump(asdict(stats), f, indent=2)


def persist_real_remaining(remaining: Optional[int], used: Optional[int] = None) -> None:
    """Persist the REAL `x-requests-remaining` balance to disk so it survives
    process restarts. Called by odds.the_odds_api on every fetch that returns
    live credit headers. This is the authoritative balance; the JSON counter
    above is only an estimate used for daily-pace soft-gating."""
    if remaining is None:
        return
    try:
        REAL_CREDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "remaining": int(remaining),
            "used": int(used) if used is not None else None,
            "updated": datetime.now().isoformat(),
        }
        with open(REAL_CREDIT_FILE, "w") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:  # pragma: no cover — never let credit logging break a fetch
        logger.debug(f"persist_real_remaining failed: {e}")


def read_real_remaining() -> Optional[int]:
    """Read the last-known REAL credit balance from disk. None if never written."""
    try:
        if REAL_CREDIT_FILE.exists():
            with open(REAL_CREDIT_FILE) as f:
                val = json.load(f).get("remaining")
                return int(val) if val is not None else None
    except Exception:
        pass
    return None


def get_usage() -> dict:
    """Get current API usage stats."""
    stats = _load_usage()
    today = datetime.now().strftime("%Y-%m-%d")
    today_calls = stats.daily_calls.get(today, 0)

    days_left = _days_remaining_in_month()
    daily_budget = stats.calls_remaining // max(days_left, 1)

    return {
        "month": stats.month,
        "calls_used": stats.calls_used,
        "calls_remaining": stats.calls_remaining,
        "monthly_limit": MONTHLY_LIMIT,
        "today_calls": today_calls,
        "daily_budget": daily_budget,
        "days_remaining": days_left,
        "last_call": stats.last_call,
        "can_call": stats.calls_remaining > 0 and today_calls < daily_budget * 2,
    }


def _days_remaining_in_month() -> int:
    """Days left in current month."""
    now = datetime.now()
    if now.month == 12:
        next_month = datetime(now.year + 1, 1, 1)
    else:
        next_month = datetime(now.year, now.month + 1, 1)
    return (next_month - now).days


def can_make_call(priority: str = "normal") -> Tuple[bool, str]:
    """
    Check if we can make an API call based on budget.

    Priority levels:
    - "critical": Super Bowl, major events (always allow if any budget left)
    - "high": Game day scans
    - "normal": Regular scheduled scans
    - "low": Exploratory/testing

    Returns: (can_call, reason)
    """
    stats = _load_usage()
    today = datetime.now().strftime("%Y-%m-%d")
    today_calls = stats.daily_calls.get(today, 0)

    days_left = _days_remaining_in_month()
    daily_budget = stats.calls_remaining // max(days_left, 1)

    # Priority multipliers for daily budget
    priority_limits = {
        "critical": daily_budget * 3,  # Can use 3x daily budget
        "high": daily_budget * 1.5,
        "normal": daily_budget,
        "low": daily_budget * 0.5,
    }

    limit = priority_limits.get(priority, daily_budget)

    # Authoritative hard floor against the REAL balance (header-sourced, persisted)
    # — preferred over the local estimate when available. Reserve CREDIT_FLOOR for
    # critical-priority calls only.
    real_remaining = read_real_remaining()
    if real_remaining is not None:
        if real_remaining <= 0:
            return False, "Real credit balance exhausted"
        if real_remaining <= CREDIT_FLOOR and priority != "critical":
            return False, f"Real balance {real_remaining} <= floor {CREDIT_FLOOR}, reserved for critical"

    # Hard stop if monthly estimate exhausted
    if stats.calls_remaining <= 0:
        return False, f"Monthly limit exhausted ({MONTHLY_LIMIT} calls used)"

    # Soft limit based on priority (daily pacing)
    if today_calls >= limit:
        return False, f"Daily budget exceeded for {priority} priority ({today_calls}/{int(limit)})"

    return True, "OK"


def record_call(calls_made: int = 1, endpoint: str = None):
    """Record that we made API call(s)."""
    stats = _load_usage()
    today = datetime.now().strftime("%Y-%m-%d")

    stats.calls_used += calls_made
    stats.calls_remaining = max(0, MONTHLY_LIMIT - stats.calls_used)
    stats.last_call = datetime.now().isoformat()

    if today not in stats.daily_calls:
        stats.daily_calls[today] = 0
    stats.daily_calls[today] += calls_made

    _save_usage(stats)

    return {"recorded": calls_made, "total_used": stats.calls_used, "remaining": stats.calls_remaining}


def update_from_headers(headers: dict):
    """
    Update usage from API response headers.
    The Odds API returns: x-requests-used, x-requests-remaining
    """
    stats = _load_usage()

    if "x-requests-used" in headers:
        stats.calls_used = int(headers["x-requests-used"])
    if "x-requests-remaining" in headers:
        stats.calls_remaining = int(headers["x-requests-remaining"])

    stats.last_call = datetime.now().isoformat()
    _save_usage(stats)


def get_scan_schedule() -> dict:
    """
    Get recommended scan schedule based on remaining budget.

    Returns optimal scanning intervals.
    """
    stats = _load_usage()
    days_left = _days_remaining_in_month()
    daily_budget = stats.calls_remaining // max(days_left, 1)

    # Each full scan = ~4 calls (NFL, NBA, NHL, soccer)
    scans_per_day = daily_budget // 4

    if scans_per_day >= 4:
        interval = "every 6h"
    elif scans_per_day >= 2:
        interval = "every 12h"
    elif scans_per_day >= 1:
        interval = "once daily"
    else:
        interval = "every 2 days"

    return {
        "daily_budget": daily_budget,
        "scans_per_day": scans_per_day,
        "recommended_interval": interval,
        "calls_remaining": stats.calls_remaining,
        "days_remaining": days_left,
    }


# Event importance scoring
EVENT_PRIORITY = {
    "super_bowl": "critical",
    "nfl_playoff": "critical",
    "nba_finals": "critical",
    "world_series": "critical",
    "march_madness": "high",
    "nfl_regular": "normal",
    "nba_regular": "normal",
    "mlb_regular": "low",
    "nhl_regular": "low",
}


def should_scan_sport(sport: str, has_games_today: bool = True) -> Tuple[bool, str]:
    """
    Decide if we should scan a sport based on importance and budget.
    """
    priority = "normal"

    # Super Bowl Sunday special case
    if sport == "americanfootball_nfl" and datetime.now().month == 2:
        priority = "critical"

    # No games today = low priority
    if not has_games_today:
        priority = "low"

    return can_make_call(priority)


# Quick test
if __name__ == "__main__":
    print("Current usage:", json.dumps(get_usage(), indent=2))
    print("Schedule:", json.dumps(get_scan_schedule(), indent=2))
    logger.info("Can call (normal):", can_make_call("normal"))
    logger.error("Can call (critical):", can_make_call("critical"))

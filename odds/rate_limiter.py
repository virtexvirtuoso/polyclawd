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

# Config
MONTHLY_LIMIT = 500
DAILY_BUDGET = 16  # 500 / 31 days
CACHE_DIR = Path("/var/www/virtuosocrypto.com/polyclawd/cache")
RATE_FILE = CACHE_DIR / "odds_api_usage.json"

# Fallback for local dev
if not CACHE_DIR.exists():
    CACHE_DIR = Path.home() / "Desktop/polyclawd/cache"
    RATE_FILE = CACHE_DIR / "odds_api_usage.json"
    CACHE_DIR.mkdir(exist_ok=True)


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
                        month=current_month,
                        calls_used=0,
                        calls_remaining=MONTHLY_LIMIT,
                        last_call=None,
                        daily_calls={}
                    )
                return UsageStats(**data)
        except:
            pass
    
    return UsageStats(
        month=current_month,
        calls_used=0,
        calls_remaining=MONTHLY_LIMIT,
        last_call=None,
        daily_calls={}
    )


def _save_usage(stats: UsageStats):
    """Save usage stats to file."""
    RATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RATE_FILE, "w") as f:
        json.dump(asdict(stats), f, indent=2)


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
        "can_call": stats.calls_remaining > 0 and today_calls < daily_budget * 2
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
        "low": daily_budget * 0.5
    }
    
    limit = priority_limits.get(priority, daily_budget)
    
    # Hard stop if monthly exhausted
    if stats.calls_remaining <= 0:
        return False, f"Monthly limit exhausted ({MONTHLY_LIMIT} calls used)"
    
    # Soft limit based on priority
    if today_calls >= limit:
        return False, f"Daily budget exceeded for {priority} priority ({today_calls}/{int(limit)})"
    
    # Reserve last 10 calls for critical only
    if stats.calls_remaining <= 10 and priority != "critical":
        return False, f"Only {stats.calls_remaining} calls left, reserved for critical"
    
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
    
    return {
        "recorded": calls_made,
        "total_used": stats.calls_used,
        "remaining": stats.calls_remaining
    }


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
        "days_remaining": days_left
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
    print("Can call (normal):", can_make_call("normal"))
    print("Can call (critical):", can_make_call("critical"))

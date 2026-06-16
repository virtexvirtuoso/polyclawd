"""Portfolio resolution-date clustering analysis — detect concentration risk.

When 4+ positions resolve on the same day, max drawdown is concentrated.
Tags each position with resolution_date_source: "explicit" | "estimated" | "unknown".
"""
import json
import logging
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "storage" / "shadow_trades.db"
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_API = "https://gamma-api.polymarket.com"

# ── Archetype → estimated-hours fallback ──────────────────────────────────
ARCHETYPE_ESTIMATE_HOURS: dict[str, tuple[int, int]] = {
    "weather":         (0, 0),     # resolves same day (Kalshi scheduled_start)
    "weather_fade":    (0, 0),     # same day
    "kalshi_weather_fade": (0, 0),# same day
    "social_count":    (24, 48),   # 24-48h
    "prediction":      (24, 48),   # 24-48h
    "crypto_price":    (24, 168),  # end of day/week
    "sports":          (12, 24),   # end of game day
    "mlb":             (12, 24),
    "nfl":             (12, 24),
    "nba":             (12, 24),
    "soccer":          (12, 24),
    "ufc":             (12, 24),
    "election":        (72, 168),  # results known within a few days
    "political":       (72, 168),
    "event":           (48, 72),
    "crypto":          (24, 168),
    "options_implied": (24, 168),
    "other":           (48, 72),
}

MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _get_db() -> sqlite3.Connection:
    """Open read-only connection to shadow_trades.db."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_json(url: str, timeout: int = 8) -> Optional[dict]:
    """Lightweight JSON GET with timeout — returns None on failure."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Polyclawd/2.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        logger.debug("Fetch failed for %s: %s", url[:60], exc)
        return None


def _fetch_kalshi_market(ticker: str) -> Optional[dict]:
    """Fetch a Kalshi market by ticker, returning close_time if available."""
    data = _fetch_json(f"{KALSHI_API}/markets/{ticker}", timeout=6)
    return data


def _fetch_polymarket_market(market_id: str) -> Optional[dict]:
    """Fetch a Polymarket market by ID, slug, or conditionId."""
    for param, val in [("id", market_id), ("slug", market_id), ("conditionId", market_id)]:
        data = _fetch_json(f"{GAMMA_API}/markets?{param}={urllib.request.quote(str(val))}", timeout=6)
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
    return None


def _try_extract_date_from_title(title: str) -> Optional[str]:
    """Try to parse a resolution date from market title."""
    if not title:
        return None
    t = title.lower()

    # "on January 20" or "on March 5, 2026"
    m = re.search(r'on (january|february|march|april|may|june|july|august|september|october|november|december) (\d{1,2})(?:,?\s*(20[2-3]\d))?', t, re.I)
    if m:
        month = MONTH_NAMES[m.group(1).lower()]
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else datetime.now(timezone.utc).year
        return f"{year:04d}-{month:02d}-{day:02d}"

    # "for the week ending June 15" / "week of June 15"
    m = re.search(r'week (?:ending|of) (january|february|march|april|may|june|july|august|september|october|november|december) (\d{1,2})(?:,?\s*(20[2-3]\d))?', t, re.I)
    if m:
        month = MONTH_NAMES[m.group(1).lower()]
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else datetime.now(timezone.utc).year
        return f"{year:04d}-{month:02d}-{day:02d}"

    # "this month" → end of current month
    m = re.search(r'(?:end of|this) month', t)
    if m:
        now = datetime.now(timezone.utc)
        last_day = (datetime(now.year, now.month % 12 + 1, 1) - timedelta(days=1)).day
        return f"{now.year:04d}-{now.month:02d}-{last_day:02d}"

    # "before [dow]" → next occurrence of that day
    m = re.search(r'before (mon|tue|wed|thu|fri|sat|sun)', t)
    if m:
        target_wd = WEEKDAYS[m.group(1).lower()]
        now = datetime.now(timezone.utc)
        days_ahead = (target_wd - now.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        target = now + timedelta(days=days_ahead)
        return target.strftime("%Y-%m-%d")

    # "25 Jun 2026" style
    m = re.search(r'(\d{1,2}) (january|february|march|april|may|june|july|august|september|october|november|december) (20[2-3]\d)', t, re.I)
    if m:
        day = int(m.group(1))
        month = MONTH_NAMES[m.group(2).lower()]
        year = int(m.group(3))
        return f"{year:04d}-{month:02d}-{day:02d}"

    return None


def _extract_date_from_market_slug(slug: str) -> Optional[str]:
    """Try to extract a date from a Kalshi market slug like INDPX-24JUN26."""
    if not slug:
        return None
    # Pattern: DDMMMYY or DDMMMYYYY like 24JUN26, 15MAR2026
    m = re.search(r'(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2,4})', slug.upper())
    if m:
        day = int(m.group(1))
        month_abbr = m.group(2).title()
        month_map = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,"Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}
        month = month_map.get(month_abbr)
        if month:
            yr_str = m.group(3)
            if len(yr_str) == 2:
                year = 2000 + int(yr_str)
            else:
                year = int(yr_str)
            return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def _resolve_date(position: dict) -> tuple[str, str]:
    """Determine resolution date and its source for a position.

    Returns (YYYY-MM-DD, source) where source is "explicit", "estimated", or "unknown".
    """
    market_id = position.get("market_id", "") or ""
    market_slug = position.get("market_slug", "") or ""
    title = position.get("market_title", "") or ""
    platform = position.get("platform", "") or ""
    archetype = (position.get("archetype", "") or "other").lower()
    opened_at = position.get("opened_at", "") or ""

    # ── 1. Try market API for explicit date ──
    if platform == "kalshi":
        data = _fetch_kalshi_market(market_id)
        if data:
            # Try close_time first, then settlement_date, then scheduled_start
            for date_key in ("close_time", "settlement_date", "result_set_time", "scheduled_start"):
                raw = data.get(date_key)
                if raw:
                    try:
                        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                        return (dt.strftime("%Y-%m-%d"), "explicit")
                    except (ValueError, AttributeError):
                        pass
            # Also try nested market data
            for mk in data.get("market", data):
                if isinstance(mk, dict):
                    raw = mk.get("close_time") or mk.get("settlement_date")
                    if raw:
                        try:
                            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                            return (dt.strftime("%Y-%m-%d"), "explicit")
                        except (ValueError, AttributeError):
                            pass

    elif platform == "polymarket":
        data = _fetch_polymarket_market(market_id)
        if data:
            for date_key in ("end_date_iso", "close_time", "resolves_at", "resolution_date"):
                raw = data.get(date_key)
                if raw:
                    try:
                        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                        return (dt.strftime("%Y-%m-%d"), "explicit")
                    except (ValueError, AttributeError):
                        pass

    # ── 2. Try slug-based date extraction ──
    slug_date = _extract_date_from_market_slug(market_slug)
    if slug_date:
        return (slug_date, "explicit")

    # ── 3. Try title-based date extraction ──
    title_date = _try_extract_date_from_title(title)
    if title_date:
        return (title_date, "estimated")

    # ── 4. Archetype-based estimation ──
    if archetype in ARCHETYPE_ESTIMATE_HOURS:
        min_h, max_h = ARCHETYPE_ESTIMATE_HOURS[archetype]
        if opened_at:
            try:
                open_dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
                # Use midpoint of estimated range
                est_hours = (min_h + max_h) // 2 if max_h > 0 else 0
                est_dt = open_dt + timedelta(hours=est_hours) if est_hours > 0 else open_dt
                # If the estimated date is today or in the future, use it
                if est_dt >= datetime.now(timezone.utc) - timedelta(days=1):
                    return (est_dt.strftime("%Y-%m-%d"), "estimated")
                # If past, use the max estimate
                est_dt = open_dt + timedelta(hours=max_h) if max_h > 0 else open_dt
                return (est_dt.strftime("%Y-%m-%d"), "estimated")
            except (ValueError, TypeError):
                pass

    # ── 5. Last resort: estimate from opened_at + 72h ──
    if opened_at:
        try:
            open_dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
            est_dt = open_dt + timedelta(hours=72)
            return (est_dt.strftime("%Y-%m-%d"), "estimated")
        except (ValueError, TypeError):
            pass

    return ("unknown", "unknown")


def compute_resolution_clusters(positions: Optional[list[dict]] = None) -> dict:
    """Analyze resolution-date concentration across open positions.

    Args:
        positions: List of position dicts from paper_positions table.
                   If None, queries the database directly.

    Returns:
        {
          "clusters": [...],
          "coverage": {"known": N, "estimated": N, "unknown": N, "total": N, "coverage_pct": 55.6},
          "concentrated_windows": [...],
          "max_single_day_exposure": 2000.0,
          "portfolio_balance": 10000.0
        }
    """
    from collections import defaultdict

    # ── Load positions if not provided ──
    if positions is None:
        conn = _get_db()
        rows = conn.execute(
            "SELECT * FROM paper_positions WHERE status='open'"
        ).fetchall()
        positions = [dict(r) for r in rows]
        conn.close()

    # ── Get current bankroll ──
    portfolio_balance = 10000.0  # default fallback
    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT bankroll FROM paper_portfolio_state ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            portfolio_balance = float(row["bankroll"])
        conn.close()
    except Exception:
        logger.debug("Could not query portfolio balance, using default")

    if not positions:
        return {
            "clusters": [],
            "coverage": {"known": 0, "estimated": 0, "unknown": 0, "total": 0, "coverage_pct": 0.0},
            "concentrated_windows": [],
            "max_single_day_exposure": 0.0,
            "portfolio_balance": portfolio_balance,
            "note": "No open positions",
        }

    # ── Resolve each position's date ──
    cluster_data: dict[str, dict] = defaultdict(lambda: {
        "positions": [],
        "count": 0,
        "exposure": 0.0,
        "worst_case": 0.0,
    })
    coverage = {"known": 0, "estimated": 0, "unknown": 0, "total": len(positions)}

    for pos in positions:
        date_str, source = _resolve_date(pos)

        # Track coverage
        if source == "explicit":
            coverage["known"] += 1
        elif source == "estimated":
            coverage["estimated"] += 1
        else:
            coverage["unknown"] += 1

        bet = abs(float(pos.get("bet_size", 0) or 0))
        entry_price = float(pos.get("entry_price", 0.5) or 0.5)
        side = pos.get("side", "YES")
        # Worst-case = losing the entire bet
        worst_case = -bet

        entry = {
            "id": pos.get("id"),
            "market_id": pos.get("market_id"),
            "market_title": (pos.get("market_title") or "")[:80],
            "platform": pos.get("platform"),
            "side": side,
            "entry_price": entry_price,
            "bet_size": bet,
            "archetype": pos.get("archetype"),
            "resolution_date_source": source,
            "resolution_date": date_str,
        }

        if date_str and date_str != "unknown":
            cluster_data[date_str]["positions"].append(entry)
            cluster_data[date_str]["count"] += 1
            cluster_data[date_str]["exposure"] += bet
            cluster_data[date_str]["worst_case"] += worst_case

    coverage_pct = round(
        ((coverage["known"] + coverage["estimated"]) / coverage["total"]) * 100, 1
    ) if coverage["total"] > 0 else 0.0

    # Build clusters list sorted by date
    clusters = [
        {
            "date": date_str,
            "positions": data["positions"],
            "count": data["count"],
            "exposure": round(data["exposure"], 2),
            "worst_case": round(data["worst_case"], 2),
        }
        for date_str, data in sorted(cluster_data.items())
    ]

    # Identify concentrated windows: ≥4 positions OR >20% of balance
    concentrated_windows = []
    for c in clusters:
        alert = False
        if c["count"] >= 4:
            alert = True
        if portfolio_balance > 0 and c["exposure"] > portfolio_balance * 0.20:
            alert = True
        if alert:
            concentrated_windows.append({
                "date": c["date"],
                "count": c["count"],
                "exposure": c["exposure"],
                "exposure_pct": round((c["exposure"] / portfolio_balance) * 100, 1) if portfolio_balance > 0 else 0,
                "alert": True,
            })

    max_single_day_exposure = max((c["exposure"] for c in clusters), default=0.0)

    return {
        "clusters": clusters,
        "coverage": {"known": coverage["known"], "estimated": coverage["estimated"],
                     "unknown": coverage["unknown"], "total": coverage["total"],
                     "coverage_pct": coverage_pct},
        "concentrated_windows": concentrated_windows,
        "max_single_day_exposure": round(max_single_day_exposure, 2),
        "portfolio_balance": portfolio_balance,
    }
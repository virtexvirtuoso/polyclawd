"""
kalshi_sports.py — Kalshi game-winner market fetcher for cross-platform edge detection.

Fetches live prices from:
  - KXMLBGAME  series: MLB game winners
  - KXNBAWIN   series: NBA game winners (playoffs)
  - KXWC26WIN  series: FIFA World Cup game winners (Jun 11+)

Returns normalized dicts ready for 3-way comparison with Polymarket + Pinnacle.

Price fields from Kalshi API:
  yes_bid_dollars  — best YES bid (what buyer gets if they buy YES)
  no_bid_dollars   — best NO bid
  last_price_dollars — last traded price

Fair value for comparison: midpoint of (yes_bid + no_ask) or use last_price as proxy.

Usage:
    from odds.kalshi_sports import fetch_mlb_game_markets, fetch_wc_game_markets
    markets = fetch_mlb_game_markets()
    for m in markets:
        print(m["home_team"], m["away_team"], m["home_yes"], m["away_yes"])
"""

from __future__ import annotations

import os
import re
import json
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    import requests as _req
    def _get(url: str, params: dict = None, timeout: int = 15) -> dict:
        headers = {
            "accept": "application/json",
            "Authorization": f"Bearer {os.environ.get('KALSHI_API_KEY', '')}",
        }
        r = _req.get(url, params=params or {}, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()
except ImportError:
    import urllib.request, urllib.parse
    def _get(url: str, params: dict = None, timeout: int = 15) -> dict:
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        headers = {
            "accept": "application/json",
            "Authorization": f"Bearer {os.environ.get('KALSHI_API_KEY', '')}",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())


KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Kalshi ticker team codes → canonical team names
# Pattern: KXMLBGAME-26JUN092205MILATH-MIL → teams are MILATH, side is MIL
TEAM_CODE_MAP = {
    # MLB — 3-letter codes as they appear in Kalshi tickers
    "MIL": "Milwaukee Brewers",
    "ATH": "Oakland Athletics",
    "CIN": "Cincinnati Reds",
    "SD":  "San Diego Padres",
    "LAA": "Los Angeles Angels",
    "HOU": "Houston Astros",
    "NYY": "New York Yankees",
    "BOS": "Boston Red Sox",
    "LAD": "Los Angeles Dodgers",
    "SF":  "San Francisco Giants",
    "CHC": "Chicago Cubs",
    "CHW": "Chicago White Sox",
    "MIN": "Minnesota Twins",
    "KC":  "Kansas City Royals",
    "CLE": "Cleveland Guardians",
    "DET": "Detroit Tigers",
    "TOR": "Toronto Blue Jays",
    "BAL": "Baltimore Orioles",
    "TB":  "Tampa Bay Rays",
    "NYM": "New York Mets",
    "PHI": "Philadelphia Phillies",
    "ATL": "Atlanta Braves",
    "WSH": "Washington Nationals",
    "MIA": "Miami Marlins",
    "PIT": "Pittsburgh Pirates",
    "STL": "St. Louis Cardinals",
    "COL": "Colorado Rockies",
    "AZ":  "Arizona Diamondbacks",
    "SEA": "Seattle Mariners",
    "OAK": "Oakland Athletics",
    "TEX": "Texas Rangers",
}

# Also accept team name substrings → canonical name (for matching Odds API output)
TEAM_ALIASES: dict[str, str] = {
    "athletics": "Oakland Athletics",
    "a's": "Oakland Athletics",
    "padres": "San Diego Padres",
    "brewers": "Milwaukee Brewers",
    "reds": "Cincinnati Reds",
    "angels": "Los Angeles Angels",
    "astros": "Houston Astros",
    "yankees": "New York Yankees",
    "red sox": "Boston Red Sox",
    "dodgers": "Los Angeles Dodgers",
    "giants": "San Francisco Giants",
    "cubs": "Chicago Cubs",
    "white sox": "Chicago White Sox",
    "twins": "Minnesota Twins",
    "royals": "Kansas City Royals",
    "guardians": "Cleveland Guardians",
    "tigers": "Detroit Tigers",
    "blue jays": "Toronto Blue Jays",
    "orioles": "Baltimore Orioles",
    "rays": "Tampa Bay Rays",
    "mets": "New York Mets",
    "phillies": "Philadelphia Phillies",
    "braves": "Atlanta Braves",
    "nationals": "Washington Nationals",
    "marlins": "Miami Marlins",
    "pirates": "Pittsburgh Pirates",
    "cardinals": "St. Louis Cardinals",
    "rockies": "Colorado Rockies",
    "diamondbacks": "Arizona Diamondbacks",
    "mariners": "Seattle Mariners",
    "rangers": "Texas Rangers",
}


def _parse_ticker_teams(ticker: str) -> tuple[str, str] | None:
    """
    Parse 'KXMLBGAME-26JUN092205MILATH-MIL' → ('MIL', 'ATH')
    Returns (side_team, other_team) where side_team is the YES side.
    """
    # ticker format: SERIES-DATETIMEDUALTEAM-TEAM
    parts = ticker.split("-")
    if len(parts) < 3:
        return None
    # Last part is the YES-side team code
    yes_code = parts[-1].upper()
    # Second-to-last contains datetime + both team codes concatenated
    dt_teams = parts[-2].upper()
    # Date is 6-char (26JUN09), time is 4-char (2205), then the pair e.g. MILATH
    # Strip digits from front until we hit letters-only team pair
    m = re.search(r'\d{2}[A-Z]{3}\d{2}\d{4}([A-Z]+)', dt_teams)
    if not m:
        return None
    pair = m.group(1)  # e.g. "MILATH"
    # Split pair: yes_code prefix + remainder
    if not pair.startswith(yes_code):
        # YES code may be the second team
        other = pair[: len(pair) - len(yes_code)]
        if pair.endswith(yes_code):
            return yes_code, other
        return None
    other = pair[len(yes_code):]
    return yes_code, other


def _mid_price(yes_bid: str | float, no_bid: str | float) -> Optional[float]:
    """
    Kalshi fair value: midpoint between yes_bid and (1 - no_bid).
    yes_ask ≈ 1 - no_bid on a binary market.
    """
    try:
        yb = float(yes_bid)
        nb = float(no_bid)
        yes_ask = 1.0 - nb
        return round((yb + yes_ask) / 2, 4)
    except (TypeError, ValueError):
        return None


def _fetch_series(series_ticker: str, max_pages: int = 5) -> list[dict]:
    """Fetch all open markets for a Kalshi series ticker."""
    markets = []
    cursor = None
    for _ in range(max_pages):
        params = {"limit": 100, "status": "open", "series_ticker": series_ticker}
        if cursor:
            params["cursor"] = cursor
        try:
            data = _get(f"{KALSHI_BASE}/markets", params=params)
        except Exception as e:
            break
        batch = data.get("markets", [])
        markets.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
    return markets


def fetch_mlb_game_markets() -> list[dict]:
    """
    Fetch all open KXMLBGAME markets and return normalized game dicts.

    Each dict:
        ticker_home   : Kalshi ticker for home team YES
        ticker_away   : Kalshi ticker for away team YES
        home_code     : 3-letter Kalshi team code for home
        away_code     : 3-letter Kalshi team code for away
        home_team     : Full team name (may be None if code unknown)
        away_team     : Full team name
        home_yes      : Midpoint fair price for home YES (0-1)
        away_yes      : Midpoint fair price for away YES (0-1)
        home_bid      : Best YES bid for home
        away_bid      : Best YES bid for away
        close_time    : ISO timestamp
        game_date     : YYYY-MM-DD
    """
    raw = _fetch_series("KXMLBGAME")
    # Group by game (pair tickers with same game segment)
    pairs: dict[str, dict] = {}
    for m in raw:
        ticker = m.get("ticker", "")
        parsed = _parse_ticker_teams(ticker)
        if not parsed:
            continue
        yes_code, other_code = parsed
        # Game key = everything before the last dash
        game_key = "-".join(ticker.split("-")[:-1])
        if game_key not in pairs:
            pairs[game_key] = {"game_key": game_key, "close_time": m.get("close_time", "")}
        entry = pairs[game_key]
        mid = _mid_price(m.get("yes_bid_dollars"), m.get("no_bid_dollars"))
        entry[yes_code] = {
            "ticker": ticker,
            "mid": mid,
            "bid": float(m.get("yes_bid_dollars", 0) or 0),
            "last": float(m.get("last_price_dollars", 0) or 0),
        }

    results = []
    for game_key, data in pairs.items():
        # Need exactly 2 teams
        codes = [k for k in data if k not in ("game_key", "close_time")]
        if len(codes) != 2:
            continue
        c1, c2 = codes[0], codes[1]
        close = data.get("close_time", "")
        game_date = close[:10] if close else ""
        results.append({
            "ticker_home": data[c1]["ticker"],
            "ticker_away": data[c2]["ticker"],
            "home_code": c1,
            "away_code": c2,
            "home_team": TEAM_CODE_MAP.get(c1, c1),
            "away_team": TEAM_CODE_MAP.get(c2, c2),
            "home_yes": data[c1]["mid"],
            "away_yes": data[c2]["mid"],
            "home_bid": data[c1]["bid"],
            "away_bid": data[c2]["bid"],
            "close_time": close,
            "game_date": game_date,
        })
    return results


def fetch_wc_game_markets() -> list[dict]:
    """Fetch FIFA WC game markets (KXWC26WIN or similar). Returns same format as fetch_mlb_game_markets."""
    # WC series tickers — try multiple possibilities
    for series in ("KXWC26", "KXWC2026", "KXWC26WIN", "KXWCGAME"):
        raw = _fetch_series(series)
        if raw:
            break
    else:
        return []

    # Simplified: return raw with price midpoints
    out = []
    for m in raw:
        mid = _mid_price(m.get("yes_bid_dollars"), m.get("no_bid_dollars"))
        if mid is None:
            continue
        out.append({
            "ticker": m.get("ticker", ""),
            "title": m.get("title", ""),
            "mid": mid,
            "bid": float(m.get("yes_bid_dollars", 0) or 0),
            "last": float(m.get("last_price_dollars", 0) or 0),
            "close_time": m.get("close_time", ""),
        })
    return out


def match_team_name(kalshi_code: str, odds_api_name: str) -> bool:
    """Fuzzy match between Kalshi team code and Odds API team name."""
    canonical = TEAM_CODE_MAP.get(kalshi_code.upper(), "").lower()
    odds_lower = odds_api_name.lower()
    if not canonical:
        return False
    # Check if any word of canonical name is in odds name or vice versa
    canon_words = set(canonical.split())
    odds_words  = set(odds_lower.split())
    return bool(canon_words & odds_words)


if __name__ == "__main__":
    import sys
    markets = fetch_mlb_game_markets()
    print(f"\n{len(markets)} MLB game markets on Kalshi\n")
    print(f"{'Home':<25} {'Away':<25} {'Home YES':>9} {'Away YES':>9}  Close")
    print("-" * 80)
    for m in markets:
        print(
            f"{m['home_team']:<25} {m['away_team']:<25} "
            f"{m['home_yes']:>8.1%} {m['away_yes']:>8.1%}  {m['close_time'][:10]}"
        )

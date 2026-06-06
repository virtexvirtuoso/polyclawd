"""
MLB prop enrichment — adds lineup gate, park factor, and platoon adjustment
to raw mlb_prop_scout rows.

All data from free MLB Stats API (no key required).

Adjustments applied:
  1. Lineup gate   — is player confirmed in today's starting lineup?
  2. Park factor   — hardcoded per-stat venue multipliers (home team → factor)
  3. Platoon mult  — batter vs pitcher hand (from season statSplits), or
                     pitcher K rate weighted by opposing lineup handedness

Adjusted edge formula:
  adj_hit_rate = base_hit_rate × park_factor × platoon_mult   (capped at 99%)
  adj_edge     = adj_hit_rate - book_ip
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import date
from typing import Dict, List, Optional

from loguru import logger

MLB_STATS_API = "https://statsapi.mlb.com/api/v1"

# ── Park factors (2024–26 multi-year, normalized 1.0 = league avg) ─────────────
# Keyed by exact team name returned by The Odds API / MLB Stats API.
# Sources: Fangraphs park factors + altitude adjustment for Coors.
PARK_FACTORS: Dict[str, Dict[str, float]] = {
    "Colorado Rockies":      {"batter_home_runs": 1.38, "batter_hits": 1.22, "batter_total_bases": 1.28, "batter_rbis": 1.20, "pitcher_strikeouts": 0.90},
    "Cincinnati Reds":       {"batter_home_runs": 1.25, "batter_hits": 1.10, "batter_total_bases": 1.16, "batter_rbis": 1.12, "pitcher_strikeouts": 0.95},
    "New York Yankees":      {"batter_home_runs": 1.20, "batter_hits": 1.05, "batter_total_bases": 1.10, "batter_rbis": 1.08, "pitcher_strikeouts": 0.97},
    "Boston Red Sox":        {"batter_home_runs": 1.12, "batter_hits": 1.15, "batter_total_bases": 1.13, "batter_rbis": 1.10, "pitcher_strikeouts": 0.97},
    "Texas Rangers":         {"batter_home_runs": 1.05, "batter_hits": 1.02, "batter_total_bases": 1.03, "batter_rbis": 1.04, "pitcher_strikeouts": 1.00},
    "Atlanta Braves":        {"batter_home_runs": 1.08, "batter_hits": 1.04, "batter_total_bases": 1.05, "batter_rbis": 1.05, "pitcher_strikeouts": 0.99},
    "Baltimore Orioles":     {"batter_home_runs": 1.07, "batter_hits": 1.03, "batter_total_bases": 1.05, "batter_rbis": 1.04, "pitcher_strikeouts": 0.99},
    "Philadelphia Phillies": {"batter_home_runs": 1.10, "batter_hits": 1.03, "batter_total_bases": 1.06, "batter_rbis": 1.05, "pitcher_strikeouts": 0.98},
    "Chicago Cubs":          {"batter_home_runs": 1.05, "batter_hits": 1.04, "batter_total_bases": 1.04, "batter_rbis": 1.04, "pitcher_strikeouts": 0.98},
    "Chicago White Sox":     {"batter_home_runs": 1.10, "batter_hits": 1.03, "batter_total_bases": 1.06, "batter_rbis": 1.05, "pitcher_strikeouts": 0.97},
    "Arizona Diamondbacks":  {"batter_home_runs": 1.08, "batter_hits": 1.02, "batter_total_bases": 1.04, "batter_rbis": 1.03, "pitcher_strikeouts": 0.98},
    "Toronto Blue Jays":     {"batter_home_runs": 1.05, "batter_hits": 1.00, "batter_total_bases": 1.02, "batter_rbis": 1.01, "pitcher_strikeouts": 0.99},
    "Houston Astros":        {"batter_home_runs": 1.00, "batter_hits": 0.99, "batter_total_bases": 0.99, "batter_rbis": 0.99, "pitcher_strikeouts": 1.01},
    "Minnesota Twins":       {"batter_home_runs": 1.02, "batter_hits": 1.00, "batter_total_bases": 1.01, "batter_rbis": 1.00, "pitcher_strikeouts": 1.00},
    "Kansas City Royals":    {"batter_home_runs": 0.90, "batter_hits": 0.99, "batter_total_bases": 0.93, "batter_rbis": 0.95, "pitcher_strikeouts": 1.02},
    "Milwaukee Brewers":     {"batter_home_runs": 0.97, "batter_hits": 0.98, "batter_total_bases": 0.97, "batter_rbis": 0.97, "pitcher_strikeouts": 1.01},
    "Washington Nationals":  {"batter_home_runs": 0.97, "batter_hits": 1.00, "batter_total_bases": 0.98, "batter_rbis": 0.98, "pitcher_strikeouts": 1.01},
    "Cleveland Guardians":   {"batter_home_runs": 0.95, "batter_hits": 1.00, "batter_total_bases": 0.97, "batter_rbis": 0.97, "pitcher_strikeouts": 1.01},
    "Pittsburgh Pirates":    {"batter_home_runs": 0.92, "batter_hits": 0.99, "batter_total_bases": 0.95, "batter_rbis": 0.95, "pitcher_strikeouts": 1.01},
    "Los Angeles Dodgers":   {"batter_home_runs": 0.93, "batter_hits": 0.97, "batter_total_bases": 0.95, "batter_rbis": 0.95, "pitcher_strikeouts": 1.02},
    "Los Angeles Angels":    {"batter_home_runs": 0.97, "batter_hits": 1.00, "batter_total_bases": 0.98, "batter_rbis": 0.98, "pitcher_strikeouts": 1.00},
    "St. Louis Cardinals":   {"batter_home_runs": 0.93, "batter_hits": 0.99, "batter_total_bases": 0.95, "batter_rbis": 0.94, "pitcher_strikeouts": 1.02},
    "New York Mets":         {"batter_home_runs": 0.88, "batter_hits": 0.96, "batter_total_bases": 0.91, "batter_rbis": 0.92, "pitcher_strikeouts": 1.03},
    "San Diego Padres":      {"batter_home_runs": 0.85, "batter_hits": 0.94, "batter_total_bases": 0.90, "batter_rbis": 0.92, "pitcher_strikeouts": 1.04},
    "San Francisco Giants":  {"batter_home_runs": 0.85, "batter_hits": 0.98, "batter_total_bases": 0.91, "batter_rbis": 0.92, "pitcher_strikeouts": 1.03},
    "Oakland Athletics":     {"batter_home_runs": 0.90, "batter_hits": 0.97, "batter_total_bases": 0.93, "batter_rbis": 0.93, "pitcher_strikeouts": 1.03},
    "Athletics":             {"batter_home_runs": 0.90, "batter_hits": 0.97, "batter_total_bases": 0.93, "batter_rbis": 0.93, "pitcher_strikeouts": 1.03},
    "Detroit Tigers":        {"batter_home_runs": 0.88, "batter_hits": 0.98, "batter_total_bases": 0.92, "batter_rbis": 0.93, "pitcher_strikeouts": 1.03},
    "Seattle Mariners":      {"batter_home_runs": 0.80, "batter_hits": 0.96, "batter_total_bases": 0.87, "batter_rbis": 0.90, "pitcher_strikeouts": 1.04},
    "Tampa Bay Rays":        {"batter_home_runs": 0.87, "batter_hits": 0.95, "batter_total_bases": 0.90, "batter_rbis": 0.91, "pitcher_strikeouts": 1.04},
    "Miami Marlins":         {"batter_home_runs": 0.90, "batter_hits": 0.96, "batter_total_bases": 0.92, "batter_rbis": 0.93, "pitcher_strikeouts": 1.03},
}

# Batter stat field per market key (used to compute split rate)
_SPLIT_STAT_FIELD: Dict[str, str] = {
    "batter_home_runs":   "homeRuns",
    "batter_hits":        "hits",
    "batter_total_bases": "totalBases",
    "batter_rbis":        "rbi",
}

# ── Caches ────────────────────────────────────────────────────────────────────
_player_id_cache:     Dict[str, Optional[int]] = {}
_pitcher_hand_cache:  Dict[str, str]           = {}
_batter_splits_cache: Dict[int, Dict]          = {}
_pitcher_k_splits_cache: Dict[int, Dict]       = {}
_game_schedule_cache: Dict[str, List]          = {}  # date_str → [game_dicts]


# ── MLB Stats API helper ───────────────────────────────────────────────────────
def _mlb_get(path: str) -> dict:
    url = f"{MLB_STATS_API}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


# ── Schedule / lineup loader ───────────────────────────────────────────────────
def _load_schedule(date_str: Optional[str] = None) -> List[Dict]:
    if date_str is None:
        date_str = date.today().isoformat()
    if date_str in _game_schedule_cache:
        return _game_schedule_cache[date_str]
    try:
        data = _mlb_get(
            f"/schedule?sportId=1&hydrate=lineups,probablePitcher&date={date_str}"
        )
        games = [g for d in data.get("dates", []) for g in d.get("games", [])]
    except Exception as e:
        logger.warning(f"enrichment: schedule fetch failed: {e}")
        games = []
    _game_schedule_cache[date_str] = games
    return games


def _find_game(home_team: str, away_team: str) -> Optional[Dict]:
    """Return the schedule game dict matching home/away team (fuzzy team name match)."""
    for g in _load_schedule():
        teams = g.get("teams", {})
        h = teams.get("home", {}).get("team", {}).get("name", "")
        a = teams.get("away", {}).get("team", {}).get("name", "")
        if (home_team.lower() in h.lower() or h.lower() in home_team.lower()) and (
            away_team.lower() in a.lower() or a.lower() in away_team.lower()
        ):
            return g
    return None


# ── Player ID lookup ───────────────────────────────────────────────────────────
def _get_player_id(name: str) -> Optional[int]:
    if name in _player_id_cache:
        return _player_id_cache[name]
    try:
        data = _mlb_get(f"/people/search?names={urllib.parse.quote(name)}&sportId=1")
        pid = data.get("people", [{}])[0].get("id")
    except Exception as e:
        logger.debug(f"enrichment: player lookup failed for '{name}': {e}")
        pid = None
    _player_id_cache[name] = pid
    return pid


# ── Pitcher handedness ─────────────────────────────────────────────────────────
def get_pitcher_hand(name: str) -> str:
    """Return 'L', 'R', 'S', or '?' for a pitcher's throwing hand."""
    if not name or name in ("TBD", ""):
        return "?"
    if name in _pitcher_hand_cache:
        return _pitcher_hand_cache[name]
    try:
        data = _mlb_get(f"/people/search?names={urllib.parse.quote(name)}&sportId=1")
        people = data.get("people", [])
        hand = people[0].get("pitchHand", {}).get("code", "?") if people else "?"
    except Exception as e:
        logger.debug(f"enrichment: pitcher hand lookup failed for '{name}': {e}")
        hand = "?"
    _pitcher_hand_cache[name] = hand
    return hand


# ── Batter platoon splits ──────────────────────────────────────────────────────
def _get_batter_splits(pid: int) -> Dict:
    """Return {vl: {stat: val}, vr: {stat: val}, overall: {stat: val}}."""
    if pid in _batter_splits_cache:
        return _batter_splits_cache[pid]
    result: Dict = {}
    try:
        data = _mlb_get(
            f"/people/{pid}/stats?stats=statSplits&group=hitting&season=2026&sitCodes=vl,vr"
        )
        for s in data.get("stats", [{}])[0].get("splits", []):
            code = s.get("split", {}).get("code", "").lower()
            result[code] = s.get("stat", {})
    except Exception as e:
        logger.debug(f"enrichment: batter splits failed for pid={pid}: {e}")
    try:
        data2 = _mlb_get(f"/people/{pid}/stats?stats=season&group=hitting&season=2026")
        splits = data2.get("stats", [{}])[0].get("splits", [])
        if splits:
            result["overall"] = splits[0].get("stat", {})
    except Exception as e:
        logger.debug(f"enrichment: batter overall stats failed for pid={pid}: {e}")
    _batter_splits_cache[pid] = result
    return result


# ── Pitcher K splits ───────────────────────────────────────────────────────────
def _get_pitcher_k_splits(pid: int) -> Dict:
    """Return {vl: k_per_bf, vr: k_per_bf, overall: k_per_bf}. None if no data."""
    if pid in _pitcher_k_splits_cache:
        return _pitcher_k_splits_cache[pid]
    result: Dict = {}
    try:
        data = _mlb_get(
            f"/people/{pid}/stats?stats=statSplits&group=pitching&season=2026&sitCodes=vl,vr"
        )
        for s in data.get("stats", [{}])[0].get("splits", []):
            code = s.get("split", {}).get("code", "").lower()
            stat = s.get("stat", {})
            bf = stat.get("battersFaced") or 0
            k = stat.get("strikeOuts") or 0
            result[code] = (k / bf) if bf > 0 else None
    except Exception as e:
        logger.debug(f"enrichment: pitcher K splits failed for pid={pid}: {e}")
    try:
        data2 = _mlb_get(f"/people/{pid}/stats?stats=season&group=pitching&season=2026")
        splits = data2.get("stats", [{}])[0].get("splits", [])
        if splits:
            stat = splits[0].get("stat", {})
            bf = stat.get("battersFaced") or 0
            k = stat.get("strikeOuts") or 0
            result["overall"] = (k / bf) if bf > 0 else None
    except Exception as e:
        logger.debug(f"enrichment: pitcher overall K failed for pid={pid}: {e}")
    _pitcher_k_splits_cache[pid] = result
    return result


# ── Park factor ────────────────────────────────────────────────────────────────
def get_park_factor(home_team: str, market_key: str) -> float:
    return PARK_FACTORS.get(home_team, {}).get(market_key, 1.0)


# ── Lineup gate ────────────────────────────────────────────────────────────────
def _player_full_name(p: Dict) -> str:
    """Extract fullName from a lineup player dict (flat or nested under 'person')."""
    return p.get("fullName") or p.get("person", {}).get("fullName", "")


def get_lineup_info(player_name: str, home_team: str, away_team: str) -> Dict:
    """
    Returns {"confirmed": bool|None, "batting_slot": int|None, "game_pk": int|None}.
    confirmed=None  → lineups not yet posted (do not suppress)
    confirmed=False → lineups posted but player not in them (suppress)
    confirmed=True  → player confirmed in starting lineup
    """
    game = _find_game(home_team, away_team)
    if not game:
        return {"confirmed": None, "batting_slot": None, "game_pk": None}

    game_pk = game.get("gamePk")
    lineups_raw = game.get("lineups", {})
    if not lineups_raw:
        return {"confirmed": None, "batting_slot": None, "game_pk": game_pk}

    player_lower = player_name.lower()
    for side_key in ("homePlayers", "awayPlayers"):
        for i, p in enumerate(lineups_raw.get(side_key, [])):
            name = _player_full_name(p).lower()
            if name == player_lower or (name and player_lower.startswith(name[:6])):
                return {"confirmed": True, "batting_slot": i + 1, "game_pk": game_pk}

    return {"confirmed": False, "batting_slot": None, "game_pk": game_pk}


# ── Probable pitcher for a game ────────────────────────────────────────────────
def get_probable_pitcher(home_team: str, away_team: str, pitcher_side: str) -> str:
    """Return probable pitcher name for 'home' or 'away' side. '' if not found."""
    game = _find_game(home_team, away_team)
    if not game:
        return ""
    return (
        game.get("teams", {})
        .get(pitcher_side, {})
        .get("probablePitcher", {})
        .get("fullName", "")
    )


# ── Opposing lineup handedness (for pitcher K props) ──────────────────────────
def get_opposing_lhb_pct(home_team: str, away_team: str, pitcher_side: str) -> Optional[float]:
    """
    Fraction of the opposing lineup (9 confirmed starters) that bats left-handed.
    Returns None if lineup not yet posted or batSide data unavailable.
    pitcher_side: "home" or "away" — which side is the pitcher on.
    """
    game = _find_game(home_team, away_team)
    if not game:
        return None
    lineups_raw = game.get("lineups", {})
    if not lineups_raw:
        return None
    # Opposing batters = the other side's players
    opp_key = "awayPlayers" if pitcher_side == "home" else "homePlayers"
    players = lineups_raw.get(opp_key, [])[:9]
    if not players:
        return None
    # The lineup hydration from /schedule doesn't include batSide; look up by player ID
    lhb = 0
    total = 0
    for p in players:
        pid = p.get("id") or (p.get("person", {}).get("id"))
        if pid:
            try:
                pdata = _mlb_get(f"/people/{pid}")
                code = pdata.get("people", [{}])[0].get("batSide", {}).get("code", "?")
            except Exception:
                code = "?"
        else:
            code = "?"
        if code in ("L", "R", "S"):
            total += 1
            if code == "L":
                lhb += 1
    return (lhb / total) if total > 0 else None


# ── Detect which side a batter is on ──────────────────────────────────────────
def _detect_pitcher_side(player_name: str, home_team: str, away_team: str) -> str:
    """
    Returns the pitcher's side ("home" or "away") that the batter will face.
    Away batter → faces home pitcher → "home"
    Home batter → faces away pitcher → "away"
    Defaults to "home" (most common: visiting team batter vs home pitcher).
    """
    game = _find_game(home_team, away_team)
    if not game:
        return "home"
    lineups = game.get("lineups", {})
    player_lower = player_name.lower()
    away_names = [
        _player_full_name(p).lower()
        for p in lineups.get("awayPlayers", [])
    ]
    if any(player_lower == n or (n and player_lower.startswith(n[:6])) for n in away_names):
        return "home"   # away batter faces home pitcher
    return "away"       # home batter faces away pitcher


# ── Platoon multiplier ─────────────────────────────────────────────────────────
def compute_platoon_mult(
    player_name: str,
    market_key: str,
    pitcher_hand: str,
    is_pitcher_prop: bool = False,
    opp_lhb_pct: Optional[float] = None,
) -> float:
    """
    Batter props: (split stat-per-AB vs pitcher_hand) / (overall stat-per-AB).
    Pitcher Ks: weighted K/BF using opposing lineup LHB% vs pitcher's vl/vr rates.
    Returns 1.0 if data is insufficient (no penalty, no boost — neutral).
    """
    pid = _get_player_id(player_name)
    if not pid:
        return 1.0

    if is_pitcher_prop:
        splits = _get_pitcher_k_splits(pid)
        overall = splits.get("overall")
        k_vl = splits.get("vl")
        k_vr = splits.get("vr")
        if not overall or k_vl is None or k_vr is None:
            return 1.0
        pct_l = opp_lhb_pct if opp_lhb_pct is not None else 0.40  # league avg ~40% LHB
        weighted = pct_l * k_vl + (1 - pct_l) * k_vr
        return round(weighted / overall, 4)

    else:
        if pitcher_hand not in ("L", "R"):
            return 1.0
        stat_field = _SPLIT_STAT_FIELD.get(market_key)
        if not stat_field:
            return 1.0
        splits = _get_batter_splits(pid)
        split_code = "vl" if pitcher_hand == "L" else "vr"
        split_stat = splits.get(split_code, {})
        overall_stat = splits.get("overall", {})
        split_ab = split_stat.get("atBats") or 0
        overall_ab = overall_stat.get("atBats") or 0
        split_val = split_stat.get(stat_field) or 0
        overall_val = overall_stat.get(stat_field) or 0
        # Require minimum sample to trust the split
        if split_ab < 20 or overall_ab < 40 or overall_val == 0:
            return 1.0
        # For HR: require at least 3 HR in the split to avoid 1-homer flukes
        if market_key == "batter_home_runs" and split_val < 3:
            return 1.0
        split_rate = split_val / split_ab
        overall_rate = overall_val / overall_ab
        if overall_rate == 0:
            return 1.0
        # Cap at ±50% of overall rate — max credible platoon swing
        raw_mult = split_rate / overall_rate
        return round(max(0.67, min(1.50, raw_mult)), 4)


# ── Main enrichment entry point ────────────────────────────────────────────────
def enrich_row(row: Dict, home_team: str, away_team: str) -> Dict:
    """
    Enrich a scout row with lineup confirmation, park factor, and platoon mult.
    Returns augmented dict (does not mutate input).
    """
    row = dict(row)
    market_key = row.get("market", "")
    player = row.get("player", "")
    base_hit_rate = row.get("hit_rate_pct", 0.0) / 100.0
    book_ip = row.get("book_over_pct", 0.0) / 100.0
    is_pitcher_prop = market_key == "pitcher_strikeouts"

    # 1. Lineup gate
    lu = get_lineup_info(player, home_team, away_team)
    row["lineup_confirmed"] = lu["confirmed"]
    row["batting_slot"] = lu["batting_slot"]

    # 2. Park factor (home team is venue)
    pf = get_park_factor(home_team, market_key)
    row["park_factor"] = round(pf, 3)

    # 3. Platoon adjustment
    if is_pitcher_prop:
        # Which side is the pitcher on?
        game = _find_game(home_team, away_team)
        pitcher_side = "home"
        if game:
            teams = game.get("teams", {})
            home_pp = teams.get("home", {}).get("probablePitcher", {}).get("fullName", "")
            away_pp = teams.get("away", {}).get("probablePitcher", {}).get("fullName", "")
            player_lower = player.lower()
            if away_pp and player_lower in away_pp.lower():
                pitcher_side = "away"
            elif home_pp and player_lower in home_pp.lower():
                pitcher_side = "home"
        opp_lhb_pct = get_opposing_lhb_pct(home_team, away_team, pitcher_side)
        platoon_mult = compute_platoon_mult(
            player, market_key,
            pitcher_hand="",
            is_pitcher_prop=True,
            opp_lhb_pct=opp_lhb_pct,
        )
        row["opp_pitcher"] = ""
        row["pitcher_hand"] = get_pitcher_hand(player)
        row["opp_lhb_pct"] = round(opp_lhb_pct * 100, 1) if opp_lhb_pct is not None else None
    else:
        # Batter prop: find opposing pitcher and their hand
        pitcher_side = _detect_pitcher_side(player, home_team, away_team)
        pitcher_name = get_probable_pitcher(home_team, away_team, pitcher_side)
        pitcher_hand = get_pitcher_hand(pitcher_name) if pitcher_name else "?"
        platoon_mult = compute_platoon_mult(player, market_key, pitcher_hand)
        row["opp_pitcher"] = pitcher_name
        row["pitcher_hand"] = pitcher_hand
        row["opp_lhb_pct"] = None

    row["platoon_mult"] = round(platoon_mult, 3)

    # 4. Adjusted hit rate + edge
    adj_hit_rate = min(base_hit_rate * pf * platoon_mult, 0.99)
    row["adj_hit_rate_pct"] = round(adj_hit_rate * 100, 1)
    row["adj_edge_pct"] = round((adj_hit_rate - book_ip) * 100, 1)

    return row

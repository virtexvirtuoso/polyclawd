"""Composite implied game win probability from player props.

Monte Carlo simulation: sample each player's prop performance from the
sportsbook line distribution, aggregate into team-level performance,
compare against moneyline and Polymarket prediction market price.

Design (CE-8):
  - Fetch ALL player props per game (batter_*, pitcher_* markets)
  - For each prop, compute implied performance distribution from devigged
    over/under probabilities (spline-based truncated normal approximation)
  - Monte Carlo N=10,000: sample each player's performance, aggregate to
    team-level stats, map to win probability via logistic-regression heuristic
  - Compare MC win prob vs moneyline devig vs Polymarket game price
  - Flag divergences > 3pp as structural mispricing signals

Dependencies:
  - odds/mlb_props.py pattern (get_event_markets, get_games_with_markets)
  - odds/sports_edge_common.consensus_devig_2way
  - Polymarket Gamma API /events with tag_slug=baseball
"""

from __future__ import annotations

import math
import os
import random
import statistics
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import json
import requests
from loguru import logger
from config.polymarket_urls import GAMMA_API as POLYMARKET_GAMMA  # polyproxy: central URL config

try:
    from odds.odds_api_fetch import get_event_markets, get_games_with_markets, upcoming_window
    from odds.mlb_props import PROP_MARKETS, SHARP_BOOK_KEYS, _american_to_ip
except ImportError:
    from ..odds.odds_api_fetch import get_event_markets, get_games_with_markets, upcoming_window
    from ..odds.mlb_props import PROP_MARKETS, SHARP_BOOK_KEYS, _american_to_ip

# ── Configuration ───────────────────────────────────────────────────────────
MLB_KEY = "baseball_mlb"
N_SIMULATIONS_DEFAULT = 10_000
SIGNAL_THRESHOLD_PP = 3.0  # flag divergences > 3 percentage points
MIN_PROPS_FOR_ANALYSIS = 3  # minimum props needed per game
CACHE_TTL_S = 1800  # 30 minute cache per game

# Sharp books for moneyline devig
SHARP_2WAY_KEYS = "pinnacle,fanduel,draftkings,betmgm,williamhill_us"

# Map aggregate team stats → win probability (simplified logistic regression).
# These coefficients are from domain knowledge (team wRC+ / pitching run-prevention):
#   - More hits → higher WP
#   - More total bases → higher WP
#   - More home runs → higher WP
#   - More RBI (runners driven in) → higher WP
#   - More pitcher strikeouts → lower opponent WP
#   - More pitcher hits allowed → lower WP
# The weight vector is a heuristic. These get normalized per-simulation.
STAT_WEIGHTS = {
    "batter_hits": 0.25,
    "batter_total_bases": 0.20,
    "batter_home_runs": 0.15,
    "batter_rbis": 0.15,
    "pitcher_strikeouts": 0.15,  # positive for this team
    "pitcher_hits_allowed": -0.10,  # negative for this team (pitcher gave up hits)
}

# Baseline expected per-game team stat values (MLB 2025-ish averages)
# Used as the midpoint of the sigmoid function.
BASELINE_HITS = 8.0
BASELINE_TOTAL_BASES = 14.0
BASELINE_HR = 1.2
BASELINE_RBI = 3.8
BASELINE_K = 8.5
BASELINE_HITS_ALLOWED = 8.0

# ── Cache ───────────────────────────────────────────────────────────────────
_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}  # for scan_all

def _normalize_name(name: str) -> str:
    """Normalize player name for consistent matching."""
    return name.strip().lower().replace(".", "").replace("-", " ").replace("\u2019", "'").replace("\u2018", "'")

# ═══════════════════════════════════════════════════════════════════════════════
# Section 1: Data Fetch (from existing Odds API integration)
# ═══════════════════════════════════════════════════════════════════════════════

async def get_all_player_props_for_game(event_id: str) -> Dict[str, Any]:
    """Fetch ALL player props for one game from The Odds API event-odds endpoint.

    Returns a structured dict:
    {
        "away_team": str, "home_team": str,
        "commence_time": str,
        "props": {
            "batter_home_runs": [{"player": str, "book": str, "line": float,
                                 "over_ip": float, "under_ip": float}, ...],
            ...
        },
        "away_props": {grouped by player for the away team},
        "home_props": {grouped by player for the home team},
    }
    """
    payload = await get_event_markets(
        MLB_KEY,
        event_id,
        markets=",".join(PROP_MARKETS),
        regions="us",
        bookmakers=SHARP_BOOK_KEYS,
    )
    if not payload:
        return {"props": {}, "away_props": {}, "home_props": {}, "away_team": "", "home_team": "", "commence_time": ""}

    away_team = payload.get("away_team", "Away")
    home_team = payload.get("home_team", "Home")
    commence_time = payload.get("commence_time", "")

    # Parse props per market (same logic as mlb_props._parse_event_props)
    raw_props: Dict[str, List[Dict]] = {}
    for bk in payload.get("bookmakers", []):
        for market in bk.get("markets", []):
            mkt = market.get("key", "")
            if mkt not in PROP_MARKETS and mkt not in ["pitcher_hits_allowed", "pitcher_outs"]:
                continue
            outcomes = market.get("outcomes", []) or []
            by_player: Dict[str, Dict[str, Dict]] = {}
            for o in outcomes:
                player = o.get("description") or o.get("name") or "?"
                side = (o.get("name") or "").lower()
                by_player.setdefault(player, {})[side] = o
            rows = raw_props.setdefault(mkt, [])
            for player, sides in by_player.items():
                over = sides.get("over")
                under = sides.get("under")
                ref = over or under
                if not ref:
                    continue
                rows.append({
                    "player": player,
                    "book": bk.get("key", ""),
                    "line": float(ref.get("point", 0.5)),
                    "over_ip": _american_to_ip(over.get("price")) / 100.0 if over else 0.5,
                    "under_ip": _american_to_ip(under.get("price")) / 100.0 if under else 0.5,
                })

    # Group props by player per team using lineup data
    team_players = _get_team_rosters_from_lineups()
    away_players: Dict[str, str] = {}
    home_players: Dict[str, str] = {}
    if away_team in team_players:
        for p in team_players[away_team]:
            away_players[_normalize_name(p)] = p
    if home_team in team_players:
        for p in team_players[home_team]:
            home_players[_normalize_name(p)] = p

    away_props: Dict[str, List[Dict]] = {}
    home_props: Dict[str, List[Dict]] = {}
    all_prop_rows: List[Dict] = []

    for mkt, rows in raw_props.items():
        for row in rows:
            all_prop_rows.append(row)
            pname = _normalize_name(row["player"])
            if pname in away_players:
                away_props.setdefault(mkt, []).append(row)
            elif pname in home_players:
                home_props.setdefault(mkt, []).append(row)
            else:
                # Can't determine team — assign to whichever has fewer props
                target = away_props if len(away_props.get(mkt, [])) <= len(home_props.get(mkt, [])) else home_props
                target.setdefault(mkt, []).append(row)

    return {
        "away_team": away_team,
        "home_team": home_team,
        "commence_time": commence_time,
        "props": raw_props,
        "all_prop_rows": all_prop_rows,
        "away_props": away_props,
        "home_props": home_props,
    }

def _get_team_rosters_from_lineups() -> Dict[str, List[str]]:
    """Get probable lineups from MLB StatsAPI to assign players to teams.

    Returns {team_name: [player_names]}.
    """
    teams: Dict[str, List[str]] = {}
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&hydrate=probablePitcher,lineups&date={today}"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        for d in data.get("dates", []):
            for g in d.get("games", []):
                for side in ("home", "away"):
                    team_info = g.get("teams", {}).get(side, {})
                    team_name = team_info.get("team", {}).get("name", "")
                    if not team_name:
                        continue
                    players: List[str] = []
                    # Add probable pitcher
                    pitcher = team_info.get("probablePitcher", {}).get("fullName", "")
                    if pitcher:
                        players.append(pitcher)
                    # Add batting order from lineups
                    lineup_info = g.get("lineups", {})
                    lineup = lineup_info.get(side, []) or []
                    for batter in lineup:
                        full_name = batter.get("person", {}).get("fullName", "")
                        if full_name:
                            players.append(full_name)
                    teams.setdefault(team_name, []).extend(players)
    except Exception as e:
        logger.debug(f"prop_composite: lineup fetch failed — {e}")
    return teams

# ═══════════════════════════════════════════════════════════════════════════════
# Section 2: Prop Line → Implied Performance Distribution
# ═══════════════════════════════════════════════════════════════════════════════

def prop_line_to_distribution(
    over_ip: float, under_ip: float, line: float, prop_type: str = "batter_hits"
) -> Tuple[float, float]:
    """Convert devigged over/under probabilities into implied performance distribution.

    Uses a spline-based approximation:
    - over_ip = P(performance > line)
    - under_ip = P(performance < line)
    - The push probability (performance == line) is 1 - over_ip - under_ip (usually ~0)

    For discrete count props (hits, HR, Ks), model as truncated normal
    around the line with variance scaled to the sport/prop type.

    Returns (mean, variance) of the implied distribution.

    Args:
        over_ip: Devigged probability that player goes OVER the line (0-1)
        under_ip: Devigged probability that player goes UNDER the line (0-1)
        line: The prop line value (e.g. 1.5 for hits, 5.5 for strikeouts)
        prop_type: Type of prop for variance scaling

    Returns:
        (mean, variance) tuple
    """
    # Normalize probabilities (handle rounding errors)
    total = over_ip + under_ip
    if total > 0.99:
        over_p = over_ip / total
        under_p = under_ip / total
    else:
        over_p = over_ip
        under_p = under_ip

    # Typical variance per prop type (from MLB 2024-2025 historical data)
    variance_scale = {
        "batter_home_runs": 0.6,
        "batter_hits": 1.5,
        "batter_total_bases": 4.0,
        "batter_rbis": 1.0,
        "pitcher_strikeouts": 4.0,
        "pitcher_hits_allowed": 3.0,
        "pitcher_outs": 6.0,
    }
    base_var = variance_scale.get(prop_type, 1.5)

    # Mean estimation from over/under probabilities
    if over_p > 0.5:
        edge = over_p - 0.5
        mean = line + 0.5 + edge * base_var * 0.3
    elif under_p > 0.5:
        edge = under_p - 0.5
        mean = line - 0.5 - edge * base_var * 0.3
    else:
        mean = line + (over_p - 0.5) * 1.0

    # Variance: extreme probabilities = more market certainty = lower variance
    confidence_adjustment = max(over_p, under_p)
    variance = base_var * (1.0 - confidence_adjustment * 0.4)
    variance = max(variance, base_var * 0.3)

    return (mean, variance)

def sample_performance(mean: float, variance: float, prop_type: str = "batter_hits") -> float:
    """Sample a player's prop performance from the implied distribution.

    Uses a truncated normal (left-truncated at 0 for count stats),
    implemented via Box-Muller with rejection sampling.
    """
    std = math.sqrt(variance)
    u1 = random.random()
    u2 = random.random()
    z = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
    sample = mean + z * std

    # Truncate: count stats can't be negative
    sample = max(0.0, sample)

    # For HR props, round to nearest integer (it's a count)
    if prop_type in ("batter_home_runs",):
        sample = round(sample)

    return sample

# ═══════════════════════════════════════════════════════════════════════════════
# Section 3: Monte Carlo Simulation
# ═══════════════════════════════════════════════════════════════════════════════

def _team_stat_score(
    team_props: Dict[str, List[Dict]], sampled_performance: Dict[str, float]
) -> float:
    """Compute a single-team 'score' from sampled player performances.

    Higher score = more likely to win.
    Uses a weighted linear combination of aggregated team stats normalized
    to MLB baselines.
    """
    agg_stats: Dict[str, float] = {
        "batter_hits": 0.0,
        "batter_total_bases": 0.0,
        "batter_home_runs": 0.0,
        "batter_rbis": 0.0,
        "pitcher_strikeouts": 0.0,
        "pitcher_hits_allowed": 0.0,
    }

    for mkt, props in team_props.items():
        for prop in props:
            pname = _normalize_name(prop["player"])
            perf = sampled_performance.get(pname, prop["line"])
            if mkt in agg_stats:
                agg_stats[mkt] += perf

    # Normalized score using MLB baselines
    norm_score = (
        agg_stats["batter_hits"] / BASELINE_HITS * 0.25
        + agg_stats["batter_total_bases"] / BASELINE_TOTAL_BASES * 0.20
        + agg_stats["batter_home_runs"] / BASELINE_HR * 0.15
        + agg_stats["batter_rbis"] / BASELINE_RBI * 0.15
        + agg_stats["pitcher_strikeouts"] / BASELINE_K * 0.15
        - agg_stats["pitcher_hits_allowed"] / BASELINE_HITS_ALLOWED * 0.10
    )

    return norm_score

def monte_carlo_game_outcome(
    game_props: Dict[str, Any], n_simulations: int = N_SIMULATIONS_DEFAULT
) -> Dict[str, Any]:
    """Run Monte Carlo simulation to compute implied game win probability.

    For each simulation:
      1. Sample each player's performance from prop-implied distribution
      2. Aggregate team-level stats
      3. Compare normalized scores -> win/lose
      4. Track which props were most impactful

    Args:
        game_props: Output from get_all_player_props_for_game()
        n_simulations: Number of Monte Carlo trials

    Returns:
        {
            "away_team": str, "home_team": str,
            "mc_win_prob_away": float, "mc_win_prob_home": float,
            "n_simulations": int,
            "n_props_away": int, "n_props_home": int,
            "key_props_away": [str, ...], "key_props_home": [str, ...],
        }
    """
    away_props = game_props.get("away_props", {})
    home_props = game_props.get("home_props", {})
    away_team = game_props.get("away_team", "Away")
    home_team = game_props.get("home_team", "Home")

    # Count unique prop rows per team
    n_away = sum(len(v) for v in away_props.values())
    n_home = sum(len(v) for v in home_props.values())

    if n_away + n_home < MIN_PROPS_FOR_ANALYSIS:
        logger.debug(
            f"prop_composite: insufficient props for {away_team} vs {home_team} "
            f"({n_away + n_home} total)"
        )
        return {
            "away_team": away_team,
            "home_team": home_team,
            "mc_win_prob_away": 0.5,
            "mc_win_prob_home": 0.5,
            "n_simulations": 0,
            "n_props_away": n_away,
            "n_props_home": n_home,
            "key_props_away": [],
            "key_props_home": [],
            "note": "insufficient data - fewer than 3 props total",
        }

    # Pre-compute distributions for each prop
    away_dists: List[Tuple[str, float, float, str]] = []
    for mkt, props in away_props.items():
        for prop in props:
            mean, var = prop_line_to_distribution(
                prop["over_ip"], prop["under_ip"], prop["line"], mkt
            )
            away_dists.append((_normalize_name(prop["player"]), mean, var, mkt))

    home_dists: List[Tuple[str, float, float, str]] = []
    for mkt, props in home_props.items():
        for prop in props:
            mean, var = prop_line_to_distribution(
                prop["over_ip"], prop["under_ip"], prop["line"], mkt
            )
            home_dists.append((_normalize_name(prop["player"]), mean, var, mkt))

    # Track key props by impact
    key_props_away = _rank_key_props(away_dists)
    key_props_home = _rank_key_props(home_dists)

    away_wins = 0
    for _ in range(n_simulations):
        # Sample away team performance
        away_perf: Dict[str, float] = {}
        for pname, mean, var, mkt in away_dists:
            away_perf[pname] = sample_performance(mean, var, mkt)

        # Sample home team performance
        home_perf: Dict[str, float] = {}
        for pname, mean, var, mkt in home_dists:
            home_perf[pname] = sample_performance(mean, var, mkt)

        # Score each team
        away_score = _team_stat_score(away_props, away_perf)
        home_score = _team_stat_score(home_props, home_perf)

        if away_score > home_score:
            away_wins += 1
        elif away_score == home_score:
            away_wins += 0.5

    away_win_prob = away_wins / n_simulations

    return {
        "away_team": away_team,
        "home_team": home_team,
        "mc_win_prob_away": round(away_win_prob, 4),
        "mc_win_prob_home": round(1.0 - away_win_prob, 4),
        "n_simulations": n_simulations,
        "n_props_away": n_away,
        "n_props_home": n_home,
        "key_props_away": key_props_away[:5],
        "key_props_home": key_props_home[:5],
    }

def _rank_key_props(
    dists: List[Tuple[str, float, float, str]]
) -> List[str]:
    """Rank props by how informative they are (high variance = high signal impact).

    Returns display strings like "Judge home_runs" or "Cole strikeouts".
    """
    scores: List[Tuple[float, str]] = []
    for pname, mean, var, mkt in dists:
        impact = var * abs(mean - 0.5)
        display_mkt = mkt.replace("batter_", "").replace("pitcher_", "")
        label = f"{pname.title()} {display_mkt}"
        scores.append((impact, label))
    scores.sort(reverse=True)
    return [label for _, label in scores]

# ═══════════════════════════════════════════════════════════════════════════════
# Section 4: Moneyline & Polymarket Price Fetch
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_polymarket_game_price(
    away_team: str, home_team: str
) -> Dict[str, Optional[float]]:
    """Fetch Polymarket prediction market price for this game.

    Uses Gamma API with tag_slug=baseball, then finds matching event
    by team name in the title.

    Returns {away: price, home: price, event_slug} in 0-1 range,
    or None values if not found.
    """
    try:
        url = f"{POLYMARKET_GAMMA}/events?tag_slug=baseball&limit=100&closed=false"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        # Find matching event by team name overlap
        match_event = None
        away_l = away_team.lower()
        home_l = home_team.lower()
        for ev in data:
            title = (ev.get("title") or "").lower()
            away_words = set(away_l.split())
            home_words = set(home_l.split())
            title_words = set(title.split())
            if away_words.intersection(title_words) and home_words.intersection(title_words):
                match_event = ev
                break

        if not match_event:
            return {"away": None, "home": None, "event_slug": None}

        event_slug = match_event.get("slug", "")
        event_id = match_event.get("id", "")
        markets_url = f"{POLYMARKET_GAMMA}/events/{event_id}/markets"
        req2 = urllib.request.Request(markets_url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req2, timeout=10) as resp2:
            markets_data = json.loads(resp2.read().decode())

        # Find winner-take-all markets matching team names
        away_price = None
        home_price = None
        for m in markets_data:
            outcomes = m.get("outcomes", [])
            if len(outcomes) != 2:
                continue
            for outcome in outcomes:
                out_name = _normalize_name(
                    outcome.get("name", "") or outcome.get("outcome", "")
                )
                if not out_name:
                    continue
                outcome_price = outcome.get("price", None)
                if outcome_price is None:
                    continue
                try:
                    price = float(outcome_price)
                except (TypeError, ValueError):
                    continue
                if _normalize_name(home_team) in out_name or home_l in out_name:
                    home_price = price
                elif _normalize_name(away_team) in out_name or away_l in out_name:
                    away_price = price

        return {
            "away": away_price / 100.0 if away_price is not None else None,
            "home": home_price / 100.0 if home_price is not None else None,
            "event_slug": event_slug,
        }
    except Exception as e:
        logger.warning(f"prop_composite: Polymarket fetch failed -- {e}")
        return {"away": None, "home": None, "event_slug": None}

# ═══════════════════════════════════════════════════════════════════════════════
# Section 5: Composite Comparison
# ═══════════════════════════════════════════════════════════════════════════════

async def _fetch_moneyline_for_event(event_id: str) -> Dict[str, float]:
    """Fetch moneyline h2h odds for an event and compute devigged implied prob."""
    payload = await get_event_markets(
        MLB_KEY,
        event_id,
        markets="h2h",
        regions="us",
        bookmakers=SHARP_2WAY_KEYS,
    )
    if not payload:
        return {}

    # Collect 2-way prices from sharp books
    outcomes_by_book: Dict[str, Dict[str, int]] = {}
    for bk in payload.get("bookmakers", []):
        bk_key = bk.get("key", "")
        for market in bk.get("markets", []):
            if market.get("key") != "h2h":
                continue
            outcomes = market.get("outcomes", [])
            if len(outcomes) != 2:
                continue
            outcomes_by_book[bk_key] = {
                outcomes[0].get("name", ""): outcomes[0].get("price", 0),
                outcomes[1].get("name", ""): outcomes[1].get("price", 0),
            }

    if not outcomes_by_book:
        return {}

    # Use Pinnacle if available, else first available sharp book
    ref_book = outcomes_by_book.get("pinnacle") or list(outcomes_by_book.values())[0]
    names = list(ref_book.keys())
    prices = list(ref_book.values())

    # Simple devig: scale implied probabilities to sum to 1
    ips = []
    for p in prices:
        try:
            ip = (100.0 / (p + 100.0)) if p > 0 else (abs(p) / (abs(p) + 100.0))
            ips.append(ip)
        except (TypeError, ValueError):
            return {}

    total_ip = sum(ips)
    if total_ip == 0:
        return {}

    devigged = [ip / total_ip for ip in ips]

    return {
        _normalize_name(names[0]): round(devigged[0], 4),
        _normalize_name(names[1]): round(devigged[1], 4),
    }

def _match_team_name(moneyline_probs: Dict[str, float], team_name: str) -> Optional[float]:
    """Match a canonical team name against moneyline probability keys."""
    normalized = _normalize_name(team_name)
    for key, prob in moneyline_probs.items():
        if normalized in key or key in normalized:
            return prob
    return None

async def implied_game_prob_from_props(
    event_id: str, event_data: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Full composite: prop MC simulation + moneyline devig + Polymarket price.

    Args:
        event_id: The Odds API event ID
        event_data: Optional pre-fetched event data (saves an API call)

    Returns dict with all comparisons and signal flags.
    """
    # 1. Fetch all player props for this game
    if event_data and "away_props" in event_data:
        game_props = event_data
    else:
        game_props = await get_all_player_props_for_game(event_id)

    away_team = game_props.get("away_team", "Away")
    home_team = game_props.get("home_team", "Home")
    all_rows = game_props.get("all_prop_rows", [])
    n_props_used = len(all_rows)

    if n_props_used < MIN_PROPS_FOR_ANALYSIS:
        return {
            "event": f"{away_team} vs {home_team}",
            "event_id": event_id,
            "sufficient_data": False,
            "n_props_used": n_props_used,
            "note": f"insufficient data -- {n_props_used} props found, need {MIN_PROPS_FOR_ANALYSIS}",
        }

    # 2. Monte Carlo simulation
    mc_result = monte_carlo_game_outcome(game_props)

    # 3. Moneyline
    ml_probs = await _fetch_moneyline_for_event(event_id)
    ml_away = _match_team_name(ml_probs, away_team)
    ml_home = _match_team_name(ml_probs, home_team)

    # 4. Polymarket price
    pm = _fetch_polymarket_game_price(away_team, home_team)

    # 5. Compare differences
    mc_away = mc_result["mc_win_prob_away"]
    mc_home = mc_result["mc_win_prob_home"]

    diffs = []
    if ml_away is not None:
        diffs.append(abs(mc_away - ml_away))
    if ml_home is not None:
        diffs.append(abs(mc_home - ml_home))
    if pm.get("away") is not None:
        diffs.append(abs(mc_away - pm["away"]))
    if pm.get("home") is not None:
        diffs.append(abs(mc_home - pm["home"]))

    max_diff = max(diffs) if diffs else 0.0

    # Calculate specific cross-comparison diffs
    mc_vs_ml = None
    if ml_away is not None or ml_home is not None:
        mc_vs_ml = round(
            max(
                abs(mc_away - ml_away) if ml_away is not None else 0,
                abs(mc_home - ml_home) if ml_home is not None else 0,
            ) * 100,
            2,
        )

    mc_vs_pm = None
    if pm.get("away") is not None or pm.get("home") is not None:
        mc_vs_pm = round(
            max(
                abs(mc_away - pm["away"]) if pm.get("away") is not None else 0,
                abs(mc_home - pm["home"]) if pm.get("home") is not None else 0,
            ) * 100,
            2,
        )

    return {
        "event": f"{away_team} vs {home_team}",
        "event_id": event_id,
        "away_team": away_team,
        "home_team": home_team,
        "commence_time": game_props.get("commence_time", ""),
        "prop_composite_prob": {
            "away": round(mc_away, 4),
            "home": round(mc_home, 4),
        },
        "moneyline_prob": {
            "away": round(ml_away, 4) if ml_away is not None else None,
            "home": round(ml_home, 4) if ml_home is not None else None,
        },
        "polymarket_price": {
            "away": round(pm.get("away"), 4) if pm.get("away") is not None else None,
            "home": round(pm.get("home"), 4) if pm.get("home") is not None else None,
        },
        "mc_vs_moneyline_diff_pp": mc_vs_ml,
        "mc_vs_polymarket_diff_pp": mc_vs_pm,
        "max_diff_pp": round(max_diff * 100, 2),
        "signal": max_diff > (SIGNAL_THRESHOLD_PP / 100.0),
        "n_simulations": mc_result["n_simulations"],
        "n_props_used": n_props_used,
        "sufficient_data": n_props_used >= MIN_PROPS_FOR_ANALYSIS,
        "key_props_away": mc_result.get("key_props_away", []),
        "key_props_home": mc_result.get("key_props_home", []),
        "n_props_away": mc_result.get("n_props_away", 0),
        "n_props_home": mc_result.get("n_props_home", 0),
    }

async def scan_all_games_prop_composite(
    force: bool = False,
    max_games: int = 12,
) -> List[Dict[str, Any]]:
    """Scan all today's MLB games for prop composite signals.

    Iterates over today's games from Odds API, fetches props for each,
    runs Monte Carlo simulation, and returns results sorted by max_diff_pp.

    Args:
        force: Skip cache
        max_games: Max games to process

    Returns:
        List of per-game composite results, sorted by max_diff_pp descending
    """
    now = time.time()
    if not force and _CACHE["data"] is not None and (now - float(_CACHE["ts"])) < CACHE_TTL_S:
        return _CACHE["data"]

    cf, ct = upcoming_window(30)
    events = await get_games_with_markets(
        MLB_KEY,
        markets="h2h",
        regions="us",
        bookmakers=SHARP_2WAY_KEYS,
        commence_from=cf,
        commence_to=ct,
    )
    events = sorted(events, key=lambda g: g.get("commence_time", ""))[:max_games]

    if not events:
        logger.warning("prop_composite: no games found from Odds API")
        return []

    results: List[Dict[str, Any]] = []
    import asyncio

    async def process_event(ev: Dict) -> Optional[Dict[str, Any]]:
        eid = ev.get("id", "")
        team_str = f"{ev.get('away_team', '?')} vs {ev.get('home_team', '?')}"
        try:
            game_props = await get_all_player_props_for_game(eid)
            all_rows = game_props.get("all_prop_rows", [])
            if len(all_rows) < MIN_PROPS_FOR_ANALYSIS:
                logger.debug(
                    f"prop_composite: skipping {team_str} -- only {len(all_rows)} props"
                )
                return None
            result = await implied_game_prob_from_props(eid, game_props)
            logger.info(
                f"prop_composite: {team_str} -- "
                f"max_diff={result.get('max_diff_pp', 0)}pp"
            )
            return result
        except Exception as e:
            logger.warning(f"prop_composite: error processing {team_str} -- {e}")
            return None

    tasks = [process_event(ev) for ev in events]
    gathered = await asyncio.gather(*tasks)
    for res in gathered:
        if res is not None:
            results.append(res)

    # Sort by max_diff_pp descending
    results.sort(key=lambda r: r.get("max_diff_pp", 0), reverse=True)

    _CACHE["data"] = results
    _CACHE["ts"] = now

    # Phase 2F: persist for reconciliation
    try:
        persist_ce8_results(results)
    except Exception:
        pass

    return results

# ═══════════════════════════════════════════════════════════════════════════════
# Section 6: CLI entry point for testing
# ═══════════════════════════════════════════════════════════════════════════════

async def main():
    """CLI entry point -- scan all games and print results."""
    import json

    logger.info("prop_composite: scanning all games...")
    results = await scan_all_games_prop_composite(force=True)
    print(json.dumps(results, indent=2, default=str))

# ─── Phase 2F: DB persistence for CE-8/per-sport reconciliation ────────────
_CE8_DB_INIT = False

def _init_ce8_cache(conn):
    global _CE8_DB_INIT
    if _CE8_DB_INIT:
        return
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ce8_signal_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scanned_at TEXT NOT NULL,
            event_title TEXT NOT NULL,
            away_team TEXT,
            home_team TEXT,
            mc_away_prob REAL,
            mc_home_prob REAL,
            ml_away_prob REAL,
            ml_home_prob REAL,
            max_diff_pp REAL,
            signal INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_ce8_event ON ce8_signal_cache(event_title);
    """)
    _CE8_DB_INIT = True

def persist_ce8_results(results: list) -> int:
    """Write CE-8 composite results to DB for reconciliation."""
    if not results:
        return 0
    try:
        import sqlite3 as _sq
        from pathlib import Path as _P
        db = _P(__file__).parent.parent / "storage" / "shadow_trades.db"
        conn = _sq.connect(str(db), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _init_ce8_cache(conn)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("DELETE FROM ce8_signal_cache WHERE scanned_at < datetime('now', '-24 hours')")
        n = 0
        for r in results:
            if not r.get("sufficient_data"):
                continue
            mc = r.get("mc_result", {})
            conn.execute(
                """INSERT INTO ce8_signal_cache
                   (scanned_at, event_title, away_team, home_team,
                    mc_away_prob, mc_home_prob, ml_away_prob, ml_home_prob,
                    max_diff_pp, signal)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (now, r.get("event", "")[:180],
                 r.get("away_team", ""), r.get("home_team", ""),
                 mc.get("mc_win_prob_away"), mc.get("mc_win_prob_home"),
                 r.get("ml_away_prob"), r.get("ml_home_prob"),
                 r.get("max_diff_pp", 0),
                 1 if r.get("max_diff_pp", 0) >= 5 else 0),
            )
            n += 1
        conn.commit()
        conn.close()
        return n
    except Exception as e:
        logger.debug(f"CE-8 persist failed: {e}")
        return 0

def check_ce8_agrees(event_title: str, participant: str, direction: str) -> Optional[bool]:
    """Check if CE-8 prop composite agrees with the per-sport edge direction.
    Returns True (MC simulation supports the bet), False (contradicts), None (no data)."""
    try:
        import sqlite3 as _sq
        from pathlib import Path as _P
        db = _P(__file__).parent.parent / "storage" / "shadow_trades.db"
        conn = _sq.connect(str(db), timeout=5)
        conn.row_factory = _sq.Row
        _init_ce8_cache(conn)
        rows = conn.execute(
            """SELECT away_team, home_team, mc_away_prob, mc_home_prob,
                      ml_away_prob, ml_home_prob
               FROM ce8_signal_cache
               WHERE (event_title LIKE ? OR away_team LIKE ? OR home_team LIKE ?)
               AND scanned_at > datetime('now', '-6 hours')
               ORDER BY scanned_at DESC LIMIT 1""",
            (f"%{participant[:20]}%", f"%{participant[:20]}%", f"%{participant[:20]}%"),
        ).fetchall()
        conn.close()
        if not rows:
            return None
        r = rows[0]
        # Determine which team the participant is
        mc_prob = None
        ml_prob = None
        part_lower = participant.lower()
        if part_lower in (r["away_team"] or "").lower():
            mc_prob = r["mc_away_prob"]
            ml_prob = r["ml_away_prob"]
        elif part_lower in (r["home_team"] or "").lower():
            mc_prob = r["mc_home_prob"]
            ml_prob = r["ml_home_prob"]
        if mc_prob is None or ml_prob is None:
            return None
        # CE-8 agrees if MC thinks the team is more likely to win than the moneyline says
        # (i.e., MC sees value on the same side as the per-sport engine)
        is_buy = direction.upper() in ("YES", "BUY")
        mc_says_value = mc_prob > ml_prob
        return mc_says_value == is_buy
    except Exception:
        return None

if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
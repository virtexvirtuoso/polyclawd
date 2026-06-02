"""
MLB Baseball Edge — The Odds API × Polymarket game moneylines

Compares devigged bookmaker odds for MLB game moneylines to Polymarket prices.
The Odds API provides per-game h2h data; Polymarket has game events tagged baseball.

Key difference from soccer_edge.py: markets are per-game (not season futures).
Each game event has exactly one moneyline market where question == event title.

Polymarket game market structure (from Gamma API, tag_slug=baseball):
  Event title: "Philadelphia Phillies vs. Los Angeles Dodgers"
  Moneyline market question: (same as title)
  outcomePrices: ["<first_team_price>", "<second_team_price>"]
  → outcomePrices[0] = P(first-named team wins) as YES price

Usage:
    from baseball_edge import get_baseball_edge_summary
    summary = await get_baseball_edge_summary()
"""

import asyncio
import json
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests
from loguru import logger

try:  # shared order-book executable-edge enrichment
    from . import poly_executable_edge as pee
except ImportError:  # pragma: no cover
    import poly_executable_edge as pee

try:
    from .the_odds_api import (
        get_baseball_games_with_odds,
        get_baseball_games_with_all_markets,
        _american_to_implied_prob,
    )
    from .client import devig_multiway
except ImportError:
    from the_odds_api import (
        get_baseball_games_with_odds,
        get_baseball_games_with_all_markets,
        _american_to_implied_prob,
    )
    from client import devig_multiway

# Shadow tracker (soft import — degrade gracefully)
try:
    from signals.shadow_tracker import log_shadow_trade
    HAS_SHADOW = True
except ImportError:
    try:
        import sys
        sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent / 'signals'))
        from shadow_tracker import log_shadow_trade
        HAS_SHADOW = True
    except ImportError:
        HAS_SHADOW = False
        log_shadow_trade = None

# Empirical confidence (soft import — degrade gracefully)
try:
    from signals.empirical_confidence import calculate_empirical_confidence
    HAS_EMPIRICAL = True
except ImportError:
    try:
        import sys
        sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent / 'signals'))
        from empirical_confidence import calculate_empirical_confidence
        HAS_EMPIRICAL = True
    except ImportError:
        HAS_EMPIRICAL = False
        calculate_empirical_confidence = None

# ─── Line movement store ─────────────────────────────────────────────
# In-memory dict tracking last seen best odds per (game_id, team)
# Key: "{game_id}|{team}" → {"odds": int, "timestamp": float, "delta_3h": int}
# Cross-request state (uvicorn process lives long enough)
_LINE_MOVEMENT: Dict[str, Dict] = {}
_LINE_MOVEMENT_WINDOW = 10800  # 3 hours for movement tracking


def _is_stale_game(commence_time: str) -> bool:
    """Check if a game starts in <30min or has already started.
    Returns True if too stale to trade."""
    if not commence_time:
        return True
    try:
        game_time = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        mins_to_start = (game_time - now).total_seconds() / 60
        return mins_to_start < 30
    except (ValueError, TypeError):
        return True


def _update_line_movement(game_id: str, team: str, best_odds: int, market_type: str = "moneyline") -> Dict:
    """Record a line movement observation and return delta info.
    
    Returns {"delta_3h": int or None, "delta_ticks": int, "direction": str}
    delta_3h = cumulative change over 3h window (None if first observation)
    """
    key = f"{game_id}|{team}|{market_type}"
    now = time.time()
    entry = _LINE_MOVEMENT.get(key)
    
    # Clean stale entries (>3h old)
    if entry and (now - entry["timestamp"]) > _LINE_MOVEMENT_WINDOW:
        entry["delta_3h"] = 0
        entry["first_odds"] = best_odds
        entry["timestamp"] = now
        entry["odds"] = best_odds
        _LINE_MOVEMENT[key] = entry
        return {"delta_3h": 0, "delta_ticks": 0, "direction": "flat"}
    
    if entry:
        delta = best_odds - entry["odds"]
        delta_3h = best_odds - entry.get("first_odds", best_odds)
        dir_str = "sharp_buy" if delta_3h > 3 else ("sharp_sell" if delta_3h < -3 else "flat")
        entry["odds"] = best_odds
        entry["timestamp"] = now
        if entry.get("first_odds") is None:
            entry["first_odds"] = best_odds
        res = {"delta_3h": delta_3h, "delta_ticks": delta, "direction": dir_str}
        return res
    else:
        # First observation
        _LINE_MOVEMENT[key] = {
            "odds": best_odds,
            "first_odds": best_odds,
            "timestamp": now,
            "delta_3h": 0,
        }
        return {"delta_3h": None, "delta_ticks": 0, "direction": "first_observation"}


async def _log_baseball_shadow(edge: "MLBEdge", edge_pct: float, game_id: str):
    """Log a baseball edge to the shadow tracker for empirical validation."""
    if not HAS_SHADOW or not log_shadow_trade:
        return
    try:
        # Build confidence from empirical confidence system if available
        conf = min(75, abs(edge_pct) * 15)
        if HAS_EMPIRICAL and calculate_empirical_confidence:
            try:
                ec = calculate_empirical_confidence(
                    edge.game_title,
                    "YES" if edge.direction == "BUY" else "NO",
                    edge.polymarket_price,
                    days_to_close=7.0,
                )
                if not ec.get("killed"):
                    conf = min(85, ec["confidence"] * 100)
            except Exception:
                pass

        platform = "polymarket"
        signal = {
            "market_id": edge.poly_market_id or f"mlb_{game_id}_{edge.bet_team.replace(' ', '')}",
            "market": f"{edge.game_title[:180]} — {edge.bet_team} {edge.market_type.capitalize()}",
            "platform": platform,
            "side": "YES" if edge.direction == "BUY" else "NO",
            "price": edge.polymarket_price,
            "confidence": conf,
            "days_to_close": 7.0,
            "volume": 0,
            "confirmations": 1,
            "reasoning": (
                f"MLB baseball edge: Odds API {edge.odds_api_prob*100:.0f}% "
                f"vs Poly {edge.polymarket_price*100:.1f}¢ "
                f"({edge.edge_pct*100:+.1f}% edge)"
            ),
            "archetype": "sports_single_game",
            "strategy": "baseball_moneyline",
            "category": "baseball",
            "category_tier": "sports",
        }
        log_shadow_trade(signal)
    except Exception as e:
        logger.debug(f"baseball shadow log failed: {e}")

POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"
DEFAULT_MIN_EDGE = 0.05  # 5%

# Canonical Odds API team name → Polymarket title variants
MLB_TEAM_ALIASES: Dict[str, List[str]] = {
    "Arizona Diamondbacks": ["Arizona Diamondbacks", "Diamondbacks", "D-backs", "AZ"],
    "Atlanta Braves": ["Atlanta Braves", "Braves"],
    "Baltimore Orioles": ["Baltimore Orioles", "Orioles"],
    "Boston Red Sox": ["Boston Red Sox", "Red Sox"],
    "Chicago Cubs": ["Chicago Cubs", "Cubs"],
    "Chicago White Sox": ["Chicago White Sox", "White Sox"],
    "Cincinnati Reds": ["Cincinnati Reds", "Reds"],
    "Cleveland Guardians": ["Cleveland Guardians", "Guardians"],
    "Colorado Rockies": ["Colorado Rockies", "Rockies"],
    "Detroit Tigers": ["Detroit Tigers", "Tigers"],
    "Houston Astros": ["Houston Astros", "Astros"],
    "Kansas City Royals": ["Kansas City Royals", "Royals"],
    "Los Angeles Angels": ["Los Angeles Angels", "Angels"],
    "Los Angeles Dodgers": ["Los Angeles Dodgers", "Dodgers"],
    "Miami Marlins": ["Miami Marlins", "Marlins"],
    "Milwaukee Brewers": ["Milwaukee Brewers", "Brewers"],
    "Minnesota Twins": ["Minnesota Twins", "Twins"],
    "New York Mets": ["New York Mets", "Mets"],
    "New York Yankees": ["New York Yankees", "Yankees"],
    "Oakland Athletics": ["Oakland Athletics", "Athletics", "A's"],
    "Philadelphia Phillies": ["Philadelphia Phillies", "Phillies"],
    "Pittsburgh Pirates": ["Pittsburgh Pirates", "Pirates"],
    "San Diego Padres": ["San Diego Padres", "Padres"],
    "San Francisco Giants": ["San Francisco Giants", "Giants"],
    "Seattle Mariners": ["Seattle Mariners", "Mariners"],
    "St. Louis Cardinals": ["St. Louis Cardinals", "Cardinals"],
    "Tampa Bay Rays": ["Tampa Bay Rays", "Rays"],
    "Texas Rangers": ["Texas Rangers", "Rangers"],
    "Toronto Blue Jays": ["Toronto Blue Jays", "Blue Jays"],
    "Washington Nationals": ["Washington Nationals", "Nationals"],
}


@dataclass
class MLBEdge:
    game_title: str       # "Philadelphia Phillies vs. Los Angeles Dodgers"
    home_team: str
    away_team: str
    bet_team: str         # team this edge is for
    market_type: str      # "moneyline", "spread", or "total"
    odds_api_prob: float  # devigged bookmaker probability (0-1)
    american_odds: int
    polymarket_price: float  # Polymarket YES price (0-1)
    edge_pct: float       # odds_api_prob - polymarket_price (signed)
    direction: str        # "BUY" or "SELL"
    commence_time: str
    point_value: Optional[float] = None  # spread or total point, e.g. -1.5 or 8.5
    poly_market_id: Optional[str] = None
    poly_event_id: Optional[str] = None
    # Order-book executable-edge enrichment (Scanner layer; reality check)
    executable_price: Optional[float] = None  # VWAP fill price for $100, decimal
    executable_edge: Optional[float] = None    # odds_api_prob - executable_price
    book_spread: Optional[float] = None         # best_ask - best_bid
    slippage_bps: Optional[float] = None
    tradeable: bool = False                     # book ok AND executable_edge > 0
    poly_move_1h: Optional[float] = None        # Polymarket price drift ~1h (pp)
    poly_move_6h: Optional[float] = None        # Polymarket price drift ~6h (pp)


def _team_in_title(team: str, title: str) -> bool:
    """Check if a team name or any alias appears in a Polymarket event title."""
    title_lower = title.lower()
    aliases = MLB_TEAM_ALIASES.get(team, [team])
    return any(alias.lower() in title_lower for alias in aliases)


def _fetch_polymarket_baseball_sync() -> List[Dict]:
    """Synchronous fetch of Polymarket baseball events (tag_slug=baseball)."""
    try:
        resp = requests.get(
            f"{POLYMARKET_GAMMA}/events",
            params={"closed": "false", "tag_slug": "baseball", "limit": "100"},
            timeout=30,
        )
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"Polymarket baseball fetch failed: {e}")
        return []


async def get_polymarket_baseball_events() -> List[Dict]:
    """Async wrapper for Polymarket baseball event fetch."""
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as pool:
        return await loop.run_in_executor(pool, _fetch_polymarket_baseball_sync)


def _find_matching_event(
    home_team: str, away_team: str, poly_events: List[Dict]
) -> Optional[Dict]:
    """
    Find the Polymarket event for a specific game.
    Titles follow "[Team A] vs. [Team B]" — both teams must appear in the title.
    Skips non-game events (no " vs. " in title).
    """
    for event in poly_events:
        title = event.get("title", "")
        if " vs. " not in title:
            continue
        if _team_in_title(home_team, title) and _team_in_title(away_team, title):
            return event
    return None


def _extract_moneyline_prices(
    event: Dict, home_team: str, away_team: str
) -> Optional[Tuple[float, float, str]]:
    """
    Extract (home_price, away_price, market_id) from a game event's moneyline market.

    The moneyline market has question == event title.
    outcomePrices[0] = first team in title; outcomePrices[1] = second team.
    Rejects prices of 0 (stale/settled markets).
    Returns None if no valid moneyline market found.
    """
    title = event.get("title", "")
    for market in event.get("markets", []):
        if market.get("question", "") != title:
            continue

        prices_raw = market.get("outcomePrices", "[]")
        if isinstance(prices_raw, str):
            try:
                prices = json.loads(prices_raw)
            except (json.JSONDecodeError, ValueError):
                continue
        else:
            prices = prices_raw

        if len(prices) < 2:
            continue

        try:
            price0 = float(prices[0])
            price1 = float(prices[1])
        except (ValueError, TypeError):
            continue

        if price0 <= 0 or price1 <= 0:
            continue

        # Determine which price belongs to home vs away
        # Title format: "[first_team] vs. [second_team]"
        first_team_fragment = title.split(" vs. ")[0].strip()
        first_is_home = _team_in_title(home_team, first_team_fragment)

        if first_is_home:
            return price0, price1, market.get("conditionId", market.get("id", ""))
        else:
            return price1, price0, market.get("conditionId", market.get("id", ""))

    return None


def _extract_spread_prices(
    event: Dict, target_team: str, target_point: float
) -> Optional[Tuple[float, float, str]]:
    """
    Extract (favored_price, underdog_price, market_id) for a specific spread point.
    Matches Polymarket markets like "Spread: Team (-1.5)" — the team name before the
    parenthetical MUST match the Odds API team name to avoid half-market confusion.
    Returns None if no matching spread market found.
    """
    abs_point = abs(target_point)
    for market in event.get("markets", []):
        q = market.get("question", "")
        if "Spread:" not in q:
            continue
        # The question format: "Spread: Team Name (-X.5)"
        # Extract the team name from the question
        q_after_spread = q.replace("Spread:", "").strip()
        # Split on the first parenthesis to get team name
        spread_parts = q_after_spread.split("(")
        if len(spread_parts) < 2:
            continue
        spread_team_raw = spread_parts[0].strip()
        
        # Require an EXACT spread-line match. Parse the signed number from the
        # parenthetical, e.g. "(-1.5)". Substring matching (plus the old int()
        # floor of the target) wrongly paired a 1.0 line with a (-1.5) market
        # and produced cross-number false matches.
        mspr = re.search(r"\(([+-]?[0-9]+(?:\.[0-9]+)?)\)", q)
        if not mspr:
            continue
        try:
            poly_point = abs(float(mspr.group(1)))
        except (ValueError, TypeError):
            continue
        if abs(poly_point - abs_point) > 1e-9:
            continue
        
        # Only match if this spread market is for our target team
        if not _team_in_title(target_team, spread_team_raw):
            continue
        
        prices_raw = market.get("outcomePrices", "[]")
        if isinstance(prices_raw, str):
            try:
                prices = json.loads(prices_raw)
            except (json.JSONDecodeError, ValueError):
                continue
        else:
            prices = prices_raw
        if len(prices) < 2:
            continue
        try:
            price0 = float(prices[0])
            price1 = float(prices[1])
        except (ValueError, TypeError):
            continue
        if price0 <= 0 or price1 <= 0:
            continue
        
        # outcomePrices[0] = YES = the named team covers the spread
        # Odds API target_point signs: negative = favored, positive = underdog
        # Both Polymarket spreads show as "-1.5" — the YES price means
        # "the named team covers the spread" (i.e., wins by more than 1.5)
        # The NO price means the other team covers (wins by 1.5+ or the
        # named team fails to cover)
        
        # Poly YES (price0) = named team covers
        # Poly NO (price1) = named team does NOT cover (= opponent covers)
        
        # Odds API: for favored team (negative point), the probability is
        # the chance they cover the spread (= YES for the named team)
        # For underdog (positive point), same thing
        
        # We return (named_team_covers_price, named_team_fails_price, market_id)
        # and let the caller map to the right Odds API side
        return price0, price1, market.get("conditionId", market.get("id", ""))
    return None


def _extract_total_prices(
    event: Dict, target_point: float
) -> Optional[Tuple[float, float, str]]:
    """
    Extract (over_price, under_price, market_id) for a specific total point.
    Matches Polymarket markets like "Team A vs. Team B: O/U 8.5".
    """
    abs_point = abs(target_point)
    for market in event.get("markets", []):
        q = market.get("question", "")
        if "/U " not in q or "O/" not in q:
            continue
        # Require an EXACT total-line match. Polymarket questions look like
        # "Team A vs. Team B: O/U 8.5". Substring matching wrongly paired a
        # bookmaker 8.0 line with Polymarket's 8.5 market (a different bet),
        # corrupting the edge. Parse the number and compare numerically.
        mtot = re.search(r"O/U\s*([0-9]+(?:\.[0-9]+)?)", q)
        if not mtot:
            continue
        try:
            poly_point = float(mtot.group(1))
        except (ValueError, TypeError):
            continue
        if abs(poly_point - abs_point) > 1e-9:
            continue
        prices_raw = market.get("outcomePrices", "[]")
        if isinstance(prices_raw, str):
            try:
                prices = json.loads(prices_raw)
            except (json.JSONDecodeError, ValueError):
                continue
        else:
            prices = prices_raw
        if len(prices) < 2:
            continue
        try:
            price0 = float(prices[0])
            price1 = float(prices[1])
        except (ValueError, TypeError):
            continue
        if price0 <= 0 or price1 <= 0:
            continue
        # outcomePrices[0] = "YES" = Over for total markets
        return price0, price1, market.get("conditionId", market.get("id", ""))
    return None


def _devig_two_way(odds_a: int, odds_b: int) -> Tuple[float, float]:
    """
    Remove bookmaker vig from a two-outcome market.
    Returns (true_prob_a, true_prob_b) that sum to exactly 1.0.
    """
    p_a = _american_to_implied_prob(odds_a)
    p_b = _american_to_implied_prob(odds_b)
    total = p_a + p_b
    return p_a / total, p_b / total


def _best_odds_per_team(game: Dict) -> Dict[str, int]:
    """
    Find best available h2h American odds per team across all bookmakers in a game.
    "Best" = lowest implied probability (best payout for the bettor).
    """
    best: Dict[str, int] = {}
    for bookmaker in game.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market.get("key") != "h2h":
                continue
            for outcome in market.get("outcomes", []):
                name = outcome.get("name")
                price = outcome.get("price")
                if name is None or price is None:
                    continue
                price = int(price)
                existing = best.get(name)
                if existing is None or _american_to_implied_prob(price) < _american_to_implied_prob(existing):
                    best[name] = price
    return best


def _best_spreads(game: Dict) -> Dict[str, Tuple[int, float]]:
    """
    Find best available spread odds across bookmakers.
    Returns {team: (american_odds, point)}.
    Spread markets have outcomes like:
      {"name": "Team A", "price": -110, "point": 1.5}
      {"name": "Team B", "price": -110, "point": -1.5}
    """
    best: Dict[str, Tuple[int, float]] = {}
    for bookmaker in game.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market.get("key") != "spreads":
                continue
            for outcome in market.get("outcomes", []):
                name = outcome.get("name")
                price = outcome.get("price")
                point = outcome.get("point")
                if name is None or price is None or point is None:
                    continue
                price = int(price)
                point = float(point)
                existing = best.get(name)
                if existing is None or _american_to_implied_prob(price) < _american_to_implied_prob(existing[0]):
                    best[name] = (price, point)
    return best


def _best_totals(game: Dict) -> Dict[float, Tuple[int, int]]:
    """
    Find best available total (over/under) odds across bookmakers.
    Returns {point: (over_odds, under_odds)} using lowest implied prob per side.
    Total markets have outcomes like:
      {"name": "Over", "price": -105, "point": 8.0}
      {"name": "Under", "price": -115, "point": 8.0}
    """
    best: Dict[float, Dict[str, int]] = {}
    for bookmaker in game.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market.get("key") != "totals":
                continue
            over_odds, under_odds, seen_point = None, None, None
            for outcome in market.get("outcomes", []):
                name = outcome.get("name", "")
                price = outcome.get("price")
                point = outcome.get("point")
                if price is None or point is None:
                    continue
                price = int(price)
                point = float(point)
                seen_point = point
                if name.lower() == "over":
                    if over_odds is None or _american_to_implied_prob(price) < _american_to_implied_prob(over_odds):
                        over_odds = price
                elif name.lower() == "under":
                    if under_odds is None or _american_to_implied_prob(price) < _american_to_implied_prob(under_odds):
                        under_odds = price
            if seen_point is not None and over_odds is not None and under_odds is not None:
                existing = best.get(seen_point)
                if existing is None:
                    best[seen_point] = (over_odds, under_odds)
    return best


async def find_baseball_edges(min_edge: float = DEFAULT_MIN_EDGE) -> List[MLBEdge]:
    """
    Compute edges between devigged bookmaker odds and Polymarket game prices
    for moneylines, spreads, and totals.

    Returns signals where |edge| >= min_edge, sorted by |edge| desc.
    Returns [] if ODDS_API_KEY not set or no games today.
    """
    edges: List[MLBEdge] = []

    # Parallel fetch — use all-markets endpoint for spreads + totals
    odds_games, poly_events = await asyncio.gather(
        get_baseball_games_with_all_markets(),
        get_polymarket_baseball_events(),
    )

    if not odds_games:
        logger.warning("baseball_edge: no Odds API game data (key missing or no games today)")
        return edges

    game_events = [e for e in poly_events if " vs. " in e.get("title", "")]
    logger.info(
        f"baseball_edge: {len(odds_games)} Odds API games, "
        f"{len(game_events)} Polymarket game events"
    )

    for game in odds_games:
        game_id = game.get("id", "")
        home_team = game.get("home_team", "")
        away_team = game.get("away_team", "")
        commence_time = game.get("commence_time", "")

        if not home_team or not away_team:
            continue

        if _is_stale_game(commence_time):
            continue

        event = _find_matching_event(home_team, away_team, game_events)
        if not event:
            continue

        # ── MONEYLINES ──────────────────────────────────────────────
        team_odds = _best_odds_per_team(game)
        home_odds = team_odds.get(home_team)
        away_odds = team_odds.get(away_team)
        if home_odds and away_odds:
            home_true_prob, away_true_prob = _devig_two_way(home_odds, away_odds)
            prices = _extract_moneyline_prices(event, home_team, away_team)
            if prices:
                home_poly_price, away_poly_price, ml_market_id = prices
                for team, true_prob, poly_price, american_odds in [
                    (home_team, home_true_prob, home_poly_price, home_odds),
                    (away_team, away_true_prob, away_poly_price, away_odds),
                ]:
                    edge = true_prob - poly_price
                    if abs(edge) >= min_edge:
                        edges.append(MLBEdge(
                            game_title=event.get("title", ""),
                            home_team=home_team, away_team=away_team,
                            bet_team=team, market_type="moneyline",
                            odds_api_prob=true_prob, american_odds=american_odds,
                            polymarket_price=poly_price, edge_pct=edge,
                            direction="BUY" if edge > 0 else "SELL",
                            commence_time=commence_time,
                            poly_market_id=ml_market_id, poly_event_id=event.get("id"),
                        ))
                    # Track line movement
                    _update_line_movement(game_id, team, american_odds, "moneyline")
                    if abs(edge) >= 0.03:
                        await _log_baseball_shadow(MLBEdge(
                            game_title=event.get("title", ""),
                            home_team=home_team, away_team=away_team,
                            bet_team=team, market_type="moneyline",
                            odds_api_prob=true_prob, american_odds=american_odds,
                            polymarket_price=poly_price, edge_pct=edge,
                            direction="BUY" if edge > 0 else "SELL",
                            commence_time=commence_time,
                            poly_market_id=ml_market_id, poly_event_id=event.get("id"),
                        ), edge, game_id)

        # ── SPREADS ────────────────────────────────────────────────
        spreads = _best_spreads(game)
        if spreads:
            # Find the most liquid spread (smallest absolute point)
            # Odds API returns multiple spreads per game
            # For spreads: Odds API returns h2h for each spread line
            # Team A: point=1.5 (underdog, need to cover +1.5)
            # Team B: point=-1.5 (favored, need to win by >1.5)
            # Polymarket has "Spread: Team X (-1.5)" where YES = named team covers
            # We match each team's spread market independently
            
            # Find the primary spread (smallest point magnitude, usually 1.5)
            spread_items = [(team, odds, pt) for team, (odds, pt) in spreads.items() 
                          if pt is not None]
            # Group by absolute point value
            spread_groups = {}
            for team, odds, pt in spread_items:
                key = abs(int(pt))
                if key not in spread_groups:
                    spread_groups[key] = []
                spread_groups[key].append((team, odds, pt))
            
            for abs_point, group in spread_groups.items():
                if len(group) == 2:
                    (team_a, odds_a, point_a), (team_b, odds_b, point_b) = group
                    true_prob_a, true_prob_b = _devig_two_way(odds_a, odds_b)
                    spread_point = point_a  # e.g. 1.5 or -1.5
                    
                    # Get the named team's spread price from Polymarket
                    # We need the market for the favored team first
                    prices_a = _extract_spread_prices(event, team_a, point_a)
                    prices_b = _extract_spread_prices(event, team_b, point_b)
                    
                    for team, true_prob, american_odds, point, prices in [
                        (team_a, true_prob_a, odds_a, point_a, prices_a),
                        (team_b, true_prob_b, odds_b, point_b, prices_b),
                    ]:
                        if not prices:
                            continue
                        named_team_covers_price, named_team_fails_price, sp_market_id = prices
                        
                        # For this team: if they're the favored side (negative point),
                        # the Odds API line means "cover the spread" which equals
                        # Polymarket's YES (named team covers)
                        # For underdog (positive point), same logic applies
                        # Odds API prob for this team = P(they cover their spread)
                        # Polymarket YES = P(named team covers their spread)
                        # They should match — so YES price = true_prob for the named team
                        
                        poly_price = named_team_covers_price
                        edge = true_prob - poly_price
                        if abs(edge) >= min_edge:
                            edges.append(MLBEdge(
                                game_title=event.get("title", ""),
                                home_team=home_team, away_team=away_team,
                                bet_team=team, market_type="spread",
                                odds_api_prob=true_prob, american_odds=american_odds,
                                polymarket_price=poly_price, edge_pct=edge,
                                direction="BUY" if edge > 0 else "SELL",
                                commence_time=commence_time,
                                point_value=point,
                                poly_market_id=sp_market_id, poly_event_id=event.get("id"),
                            ))
                        # Track line movement
                        _update_line_movement(game_id, team, american_odds, "spread")
                        if abs(edge) >= 0.03:
                            await _log_baseball_shadow(MLBEdge(
                                game_title=event.get("title", ""),
                                home_team=home_team, away_team=away_team,
                                bet_team=team, market_type="spread",
                                odds_api_prob=true_prob, american_odds=american_odds,
                                polymarket_price=poly_price, edge_pct=edge,
                                direction="BUY" if edge > 0 else "SELL",
                                commence_time=commence_time,
                                point_value=point,
                                poly_market_id=sp_market_id, poly_event_id=event.get("id"),
                            ), edge, game_id)

        # ── TOTALS ─────────────────────────────────────────────────
        totals = _best_totals(game)
        if totals:
            for total_point, (over_odds, under_odds) in totals.items():
                true_prob_over, true_prob_under = _devig_two_way(over_odds, under_odds)
                prices = _extract_total_prices(event, total_point)
                if not prices:
                    continue
                over_poly_price, under_poly_price, tot_market_id = prices

                for label, true_prob, poly_price, american_odds in [
                    ("Over", true_prob_over, over_poly_price, over_odds),
                    ("Under", true_prob_under, under_poly_price, under_odds),
                ]:
                    edge = true_prob - poly_price
                    if abs(edge) >= min_edge:
                        edges.append(MLBEdge(
                            game_title=event.get("title", ""),
                            home_team=home_team, away_team=away_team,
                            bet_team=label, market_type="total",
                            odds_api_prob=true_prob, american_odds=american_odds,
                            polymarket_price=poly_price, edge_pct=edge,
                            direction="BUY" if edge > 0 else "SELL",
                            commence_time=commence_time,
                            point_value=total_point,
                            poly_market_id=tot_market_id, poly_event_id=event.get("id"),
                        ))
                    # Track line movement
                    _update_line_movement(game_id, label.replace(" ", ""), american_odds, "total")
                    if abs(edge) >= 0.03:
                        await _log_baseball_shadow(MLBEdge(
                            game_title=event.get("title", ""),
                            home_team=home_team, away_team=away_team,
                            bet_team=label, market_type="total",
                            odds_api_prob=true_prob, american_odds=american_odds,
                            polymarket_price=poly_price, edge_pct=edge,
                            direction="BUY" if edge > 0 else "SELL",
                            commence_time=commence_time,
                            point_value=total_point,
                            poly_market_id=tot_market_id, poly_event_id=event.get("id"),
                        ), edge, game_id)

    # --- Executable-edge enrichment: the midpoint edge above is vs Polymarket's
    # last/mid price; the price you can actually TAKE is the ask walked to size.
    # Enrich only the emitted total edges (clean Over=0/Under=1 token mapping).
    for _e in edges:
        if not _e.poly_market_id:
            continue
        _mt = _e.market_type
        if _mt == "total":
            _oi = 0 if _e.bet_team.lower() == "over" else 1
        elif _mt == "spread":
            _oi = 0  # Polymarket "Spread: Team (-x.5)" YES = named team covers
        elif _mt == "moneyline":
            # outcomePrices/tokens[0] = first team in "A vs. B" title
            _first = _e.game_title.split(" vs. ")[0] if " vs. " in _e.game_title else ""
            _oi = 0 if _team_in_title(_e.bet_team, _first) else 1
        else:
            continue
        try:
            _ex = pee.executable_edge(
                _e.odds_api_prob, _e.bet_team,
                condition_id=_e.poly_market_id,
                outcome_index=_oi,
                target_usd=100.0,
            )
        except Exception:
            _ex = {"available": False}
        if _ex.get("available"):
            _e.executable_price = _ex["executable_price"]
            _e.executable_edge = _ex["executable_edge"]
            _e.book_spread = _ex["spread"]
            _e.slippage_bps = _ex["slippage_bps"]
            _e.tradeable = _ex["tradeable"]
        # Polymarket's own recent price drift (persistent momentum, survives restart)
        try:
            _pm = pee.poly_price_move(condition_id=_e.poly_market_id, outcome_index=_oi)
        except Exception:
            _pm = {"available": False}
        if _pm.get("available"):
            _e.poly_move_1h = _pm["move_1h_pp"]
            _e.poly_move_6h = _pm["move_6h_pp"]

    edges.sort(key=lambda e: abs(e.edge_pct), reverse=True)
    return edges


async def get_baseball_edge_summary(min_edge: float = DEFAULT_MIN_EDGE) -> Dict:
    """
    MLB edge summary for `/api/baseball/edge` response.
    Mirrors get_soccer_edge_summary() shape. `min_edge` now threaded from the
    route so the dashboard (?min_edge=0.01) surfaces sub-5%% real edges.
    """
    edges = await find_baseball_edges(min_edge)

    # Build line movement report for games that have it
    games_moved = 0
    line_moves = []
    for k, v in _LINE_MOVEMENT.items():
        delta = v.get("delta_3h")
        if delta is not None and abs(delta) >= 2:
            games_moved += 1
            game_or_team = k.split("|")
            line_moves.append({
                "key": k,
                "delta_3h": delta,
                "direction": "sharp_buy" if delta > 3 else ("sharp_sell" if delta < -3 else "minor"),
                "current_odds": v["odds"],
            })

    return {
        "source": "the_odds_api_baseball",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_edges": len(edges),
        "games_tracked": len(_LINE_MOVEMENT),
        "games_with_movement": games_moved,
        "line_movements": line_moves,
        "edges": [
            {
                "game": e.game_title,
                "team": e.bet_team,
                "market_type": e.market_type,
                "point_value": e.point_value,
                "odds_api_prob": round(e.odds_api_prob * 100, 1),
                "american_odds": f"{e.american_odds:+d}",
                "polymarket_price": round(e.polymarket_price * 100, 1),
                "edge_pct": round(e.edge_pct * 100, 1),
                "direction": e.direction,
                "commence_time": e.commence_time,
                "market_id": e.poly_market_id,
                "event_id": e.poly_event_id,
                "executable_price": (round(e.executable_price * 100, 1) if e.executable_price is not None else None),
                "executable_edge": (round(e.executable_edge * 100, 1) if e.executable_edge is not None else None),
                "book_spread_pct": (round(e.book_spread * 100, 1) if e.book_spread is not None else None),
                "slippage_bps": e.slippage_bps,
                "tradeable": e.tradeable,
                "poly_move_1h": e.poly_move_1h,
                "poly_move_6h": e.poly_move_6h,
            }
            for e in edges
        ],
        "market_type_breakdown": {
            "moneyline": sum(1 for e in edges if e.market_type == "moneyline"),
            "spread": sum(1 for e in edges if e.market_type == "spread"),
            "total": sum(1 for e in edges if e.market_type == "total"),
        },
        "top_opportunities": [
            {
                "game": e.game_title,
                "team": e.bet_team,
                "edge": f"{e.edge_pct * 100:+.1f}%",
                "action": f"{e.direction} at {e.polymarket_price * 100:.0f}¢",
            }
            for e in edges[:5]
        ],
    }


if __name__ == "__main__":
    async def _test():
        summary = await get_baseball_edge_summary()
        print(f"Found {summary['total_edges']} edges")
        for e in summary["edges"][:10]:
            sign = "+" if e["edge_pct"] > 0 else ""
            print(
                f"  {e['team']} ({e['game']}): "
                f"Odds {e['odds_api_prob']}% vs Poly {e['polymarket_price']}¢ "
                f"→ {sign}{e['edge_pct']}% {e['direction']}"
            )

    asyncio.run(_test())

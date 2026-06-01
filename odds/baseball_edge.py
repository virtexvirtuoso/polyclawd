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
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests
from loguru import logger

try:
    from .the_odds_api import get_baseball_games_with_odds, _american_to_implied_prob
    from .client import devig_multiway
except ImportError:
    from the_odds_api import get_baseball_games_with_odds, _american_to_implied_prob
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


def _update_line_movement(game_id: str, team: str, best_odds: int) -> Dict:
    """Record a line movement observation and return delta info.
    
    Returns {"delta_3h": int or None, "delta_ticks": int, "direction": str}
    delta_3h = cumulative change over 3h window (None if first observation)
    """
    key = f"{game_id}|{team}"
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

        platform = "polymarket" if edge.poly_market_id and edge.poly_market_id.startswith("0x") else "polymarket"
        signal = {
            "market_id": edge.poly_market_id or f"mlb_{game_id}_{edge.bet_team.replace(' ', '')}",
            "market": f"{edge.game_title[:180]} — {edge.bet_team} Moneyline",
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
    market_type: str      # "moneyline"
    odds_api_prob: float  # devigged bookmaker probability (0-1)
    american_odds: int
    polymarket_price: float  # Polymarket YES price (0-1)
    edge_pct: float       # odds_api_prob - polymarket_price (signed)
    direction: str        # "BUY" or "SELL"
    commence_time: str
    poly_market_id: Optional[str] = None
    poly_event_id: Optional[str] = None


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
            return price0, price1, market.get("id", "")
        else:
            return price1, price0, market.get("id", "")

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


async def find_baseball_edges(min_edge: float = DEFAULT_MIN_EDGE) -> List[MLBEdge]:
    """
    Compute edges between devigged bookmaker moneylines and Polymarket game prices.

    Flow:
      1. Fetch Odds API MLB game list (home_team, away_team, bookmakers)
      2. Fetch Polymarket baseball events (tag_slug=baseball)
      3. Match each game to its Polymarket event by team name
      4. Extract moneyline prices from both sources
      5. Devig bookmaker odds, compare to Polymarket price
      6. Return signals where |edge| >= min_edge, sorted by |edge| desc

    Returns [] if ODDS_API_KEY not set or no games today.
    """
    edges: List[MLBEdge] = []

    # Parallel fetch
    odds_games, poly_events = await asyncio.gather(
        get_baseball_games_with_odds(),
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

    game_id_field = game.get("id", "")
    for game in odds_games:
        home_team = game.get("home_team", "")
        away_team = game.get("away_team", "")
        commence_time = game.get("commence_time", "")

        if not home_team or not away_team:
            continue

        # ⏰ Stale game guard: skip games starting in <30min or already started
        if _is_stale_game(commence_time):
            logger.debug(f"baseball_edge: stale game {away_team} @ {home_team} — {commence_time}")
            continue

        event = _find_matching_event(home_team, away_team, game_events)
        if not event:
            logger.debug(f"baseball_edge: no Polymarket match for {away_team} @ {home_team}")
            continue

        team_odds = _best_odds_per_team(game)
        home_odds = team_odds.get(home_team)
        away_odds = team_odds.get(away_team)
        if not home_odds or not away_odds:
            continue

        home_true_prob, away_true_prob = _devig_two_way(home_odds, away_odds)

        prices = _extract_moneyline_prices(event, home_team, away_team)
        if not prices:
            logger.debug(
                f"baseball_edge: no moneyline market for '{event.get('title')}'"
            )
            continue

        home_poly_price, away_poly_price, market_id = prices

        for team, true_prob, poly_price, american_odds in [
            (home_team, home_true_prob, home_poly_price, home_odds),
            (away_team, away_true_prob, away_poly_price, away_odds),
        ]:
            edge = true_prob - poly_price

            # 📊 Line movement tracking
            game_id = game.get("id", game_id_field)
            mov = _update_line_movement(game_id, team, american_odds)
            sharp_flag = ""
            if mov["delta_3h"] is not None and abs(mov["delta_3h"]) >= 3:
                sharp_flag = f" (line moved {mov['delta_3h']:+d} ticks = sharp {mov['direction']})"

            if abs(edge) >= min_edge:
                edges.append(MLBEdge(
                    game_title=event.get("title", f"{away_team} vs. {home_team}"),
                    home_team=home_team,
                    away_team=away_team,
                    bet_team=team,
                    market_type="moneyline",
                    odds_api_prob=true_prob,
                    american_odds=american_odds,
                    polymarket_price=poly_price,
                    edge_pct=edge,
                    direction="BUY" if edge > 0 else "SELL",
                    commence_time=commence_time,
                    poly_market_id=market_id,
                    poly_event_id=event.get("id"),
                ))

            # 💾 Log all edges >= 3% to shadow tracker (collection phase)
            if abs(edge) >= 0.03:
                try:
                    await _log_baseball_shadow(
                        MLBEdge(
                            game_title=event.get("title", f"{away_team} vs. {home_team}"),
                            home_team=home_team,
                            away_team=away_team,
                            bet_team=team,
                            market_type="moneyline",
                            odds_api_prob=true_prob,
                            american_odds=american_odds,
                            polymarket_price=poly_price,
                            edge_pct=edge,
                            direction="BUY" if edge > 0 else "SELL",
                            commence_time=commence_time,
                            poly_market_id=market_id,
                            poly_event_id=event.get("id"),
                        ),
                        edge,
                        game_id,
                    )
                except Exception:
                    pass

    edges.sort(key=lambda e: abs(e.edge_pct), reverse=True)
    return edges


async def get_baseball_edge_summary() -> Dict:
    """
    MLB edge summary for `/api/baseball/edge` response.
    Mirrors get_soccer_edge_summary() shape.
    """
    edges = await find_baseball_edges()

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
                "odds_api_prob": round(e.odds_api_prob * 100, 1),
                "american_odds": f"{e.american_odds:+d}",
                "polymarket_price": round(e.polymarket_price * 100, 1),
                "edge_pct": round(e.edge_pct * 100, 1),
                "direction": e.direction,
                "commence_time": e.commence_time,
                "market_id": e.poly_market_id,
                "event_id": e.poly_event_id,
            }
            for e in edges
        ],
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

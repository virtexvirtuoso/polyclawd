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
from config.polymarket_urls import GAMMA_API as POLYMARKET_GAMMA  # polyproxy: central URL config
    summary = await get_baseball_edge_summary()
"""

import asyncio
import json
import os
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
    from . import sports_edge_common as sec
except ImportError:  # pragma: no cover
    import poly_executable_edge as pee
    import sports_edge_common as sec

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

# Shadow logging now uses sec.log_shadow (post-enrichment, fee-adjusted).
# See end of find_baseball_edges().

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

# MLBEdge is now an alias for sec.Edge (migrated 2026-06-22, blocker B1).
# sec.Edge has backward-compat properties: .game_title, .bet_team, .odds_api_prob, .polymarket_price
MLBEdge = sec.Edge

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
    Price for "target_team covers target_point" from Polymarket runline markets.

    Polymarket lists every runline as "Spread: <team> (-N)" — ALWAYS negative,
    meaning the named team wins by MORE than N. There is no "(+N)" market. So a
    book line's sign decides which Polymarket market is the apples-to-apples bet:
      * Favorite (target_point < 0): the market named after target_team at |N|;
        the target_team outcome = P(target covers -N).
      * Underdog (target_point > 0): the market named after the OPPONENT at |N|;
        the target_team outcome there = 1 - P(opponent covers -N) = P(target +N).

    Matching on |point| alone (the old bug) paired a +1.5 underdog against the
    team's own -1.5 market — the opposite bet — inventing huge phantom edges.

    Returns (target_cover_price, opponent_cover_price, market_id) or None.
    """
    abs_point = abs(target_point)
    want_favorite = target_point < 0
    for market in event.get("markets", []):
        q = market.get("question", "")
        if "Spread:" not in q:
            continue
        parts = q.replace("Spread:", "").strip().split("(")
        if len(parts) < 2:
            continue
        named_team_raw = parts[0].strip()
        mspr = re.search(r"\(([+-]?[0-9]+(?:\.[0-9]+)?)\)", q)
        if not mspr:
            continue
        try:
            poly_point = float(mspr.group(1))
        except (ValueError, TypeError):
            continue
        # Polymarket runlines are always negative; require |poly| == |target|.
        if poly_point >= 0 or abs(abs(poly_point) - abs_point) > 1e-9:
            continue
        named_is_target = _team_in_title(target_team, named_team_raw)
        # Favorite bet -> market named after target. Underdog bet -> named after
        # the opponent (so target is the NO/cover-the-other-way side).
        if want_favorite and not named_is_target:
            continue
        if (not want_favorite) and named_is_target:
            continue

        # Polymarket convention: the named (favorite) team is outcomes[0]. So the
        # target is index 0 when it IS the named team (favorite bet), else index 1.
        prices_raw = market.get("outcomePrices", "[]")
        try:
            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        except (json.JSONDecodeError, ValueError):
            continue
        if len(prices) < 2:
            continue
        t_idx = 0 if named_is_target else 1
        try:
            target_cover = float(prices[t_idx])
            opp_cover = float(prices[1 - t_idx])
        except (ValueError, TypeError, IndexError):
            continue
        if target_cover <= 0 or opp_cover <= 0:
            continue
        return target_cover, opp_cover, market.get("conditionId", market.get("id", ""))
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

# ─── Moneyline consensus devig — dedup'd to the shared core ──────────
# Per-book devig -> weighted consensus now lives in sports_edge_common (one
# implementation across MLB / UFC / soccer). These thin wrappers preserve the
# local call sites and pick up the shared, fuller BOOK_WEIGHTS (incl. the
# williamhill + betfair_ex aliases).
def _get_mlb_weights() -> Dict[str, float]:
    """Sport-specific book weights for MLB."""
    try:
        from odds.book_weights import get_weights
        return get_weights("baseball_mlb")
    except ImportError:
        return {}  # falls back to global BOOK_WEIGHTS inside sec.*

def _consensus_devig(game: Dict) -> Dict[str, float]:
    """MLB moneyline true-probs via the shared weighted consensus (2-way)."""
    return sec.consensus_devig_2way(game, "h2h", weights=_get_mlb_weights())

def _best_odds_per_team(game: Dict) -> Dict[str, int]:
    """Raw h2h odds from the highest-weighted book with both teams (display /
    line-movement only). Delegates to the shared core."""
    return sec.consensus_best_odds(game, "h2h", weights=_get_mlb_weights())

# _best_spreads / _best_totals removed: spreads & totals now use per-book weighted
# consensus via sec.consensus_devig_spreads / sec.consensus_devig_totals (see the
# SPREADS / TOTALS blocks in find_baseball_edges). The old best-of-all helpers
# cherry-picked the most favorable line per side across books, collapsing the
# overround and manufacturing phantom edges.

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
        # True probabilities come from the weighted consensus — NOT a single book.
        consensus = _consensus_devig(game)
        home_true_prob = consensus.get(home_team)
        away_true_prob = consensus.get(away_team)
        if home_true_prob is not None and away_true_prob is not None:
            # Raw odds kept ONLY for the american_odds display field and for
            # line-movement tracking — they never drive true_prob/edge anymore.
            team_odds = _best_odds_per_team(game)
            home_odds = team_odds.get(home_team, 0)
            away_odds = team_odds.get(away_team, 0)
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
                            event_title=event.get("title", ""),
                            home_team=home_team, away_team=away_team,
                            participant=team, market_type="moneyline", market_model="2way",
                            book_prob=true_prob, american_odds=american_odds,
                            poly_price=poly_price, edge_pct=edge,
                            direction="BUY" if edge > 0 else "SELL",
                            commence_time=commence_time,
                            poly_market_id=ml_market_id, poly_event_id=event.get("id"),
                        ))
                    # Track line movement
                    _update_line_movement(game_id, team, american_odds, "moneyline")

        # ── SPREADS ────────────────────────────────────────────────
        # Per-book devig -> weighted consensus, keyed by |point|. Raw odds for
        # display only. Polymarket "Spread: Team (-x.5)" YES = named team covers.
        _mlb_w = _get_mlb_weights()
        spread_consensus = sec.consensus_devig_spreads(game, weights=_mlb_w)   # {|pt|: {team: prob}}
        spread_odds = sec.consensus_best_spread_odds(game, weights=_mlb_w)     # {|pt|: {team: (odds, signed_pt)}}
        for abs_point, team_probs in spread_consensus.items():
            odds_map = spread_odds.get(abs_point, {})
            for team, true_prob in team_probs.items():
                american_odds, point = odds_map.get(team, (0, 0.0))
                prices = _extract_spread_prices(event, team, point)
                if not prices:
                    continue
                named_team_covers_price, _named_fails_price, sp_market_id = prices
                poly_price = named_team_covers_price
                edge = true_prob - poly_price
                if abs(edge) >= min_edge:
                    edges.append(MLBEdge(
                        event_title=event.get("title", ""),
                        home_team=home_team, away_team=away_team,
                        participant=team, market_type="spread", market_model="2way",
                        book_prob=true_prob, american_odds=american_odds,
                        poly_price=poly_price, edge_pct=edge,
                        direction="BUY" if edge > 0 else "SELL",
                        commence_time=commence_time,
                        point_value=point,
                        poly_market_id=sp_market_id, poly_event_id=event.get("id"),
                    ))
                _update_line_movement(game_id, team, american_odds, "spread")

        # ── TOTALS ─────────────────────────────────────────────────
        # Per-book devig -> weighted consensus, keyed by total point.
        total_consensus = sec.consensus_devig_totals(game, weights=_mlb_w)    # {pt: {"Over":p,"Under":p}}
        total_odds = sec.consensus_best_total_odds(game, weights=_mlb_w)      # {pt: (over_odds, under_odds)}
        for total_point, ou in total_consensus.items():
            over_odds, under_odds = total_odds.get(total_point, (0, 0))
            prices = _extract_total_prices(event, total_point)
            if not prices:
                continue
            over_poly_price, under_poly_price, tot_market_id = prices
            for label, true_prob, poly_price, american_odds in [
                ("Over", ou.get("Over", 0.0), over_poly_price, over_odds),
                ("Under", ou.get("Under", 0.0), under_poly_price, under_odds),
            ]:
                edge = true_prob - poly_price
                if abs(edge) >= min_edge:
                    edges.append(MLBEdge(
                        event_title=event.get("title", ""),
                        home_team=home_team, away_team=away_team,
                        participant=label, market_type="total", market_model="2way",
                        book_prob=true_prob, american_odds=american_odds,
                        poly_price=poly_price, edge_pct=edge,
                        direction="BUY" if edge > 0 else "SELL",
                        commence_time=commence_time,
                        point_value=total_point,
                        poly_market_id=tot_market_id, poly_event_id=event.get("id"),
                    ))
                _update_line_movement(game_id, label.replace(" ", ""), american_odds, "total")

    # --- Executable-edge enrichment. Midpoint edge above is vs Polymarket's
    # last/mid price; the executable price is the ask walked to size. (P3.4)
    # Optionally use the live WS book (POLY_WS_CONSUME=1) and register tokens
    # with the WS service; both degrade gracefully to today's REST path.
    import os as _os
    _ws_consume = _os.environ.get("POLY_WS_CONSUME") == "1"
    _reg_tokens = []
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
        # resolve the side token once (reused for register + live book + edge)
        _tid = None
        try:
            _toks = pee.condition_id_to_token_ids(_e.poly_market_id)
            if _toks:
                _tid = _toks[_oi if _oi in (0, 1) else 0]
                _reg_tokens.append(_tid)
        except Exception:
            _tid = None
        _book = None
        if _ws_consume and _tid:
            try:
                from api.services import poly_ws_reader as _pwr
                _book = await _pwr.get_live_orderbook(_tid)
                if _book is not None:
                    _e.live_book = True
            except Exception:
                _book = None
        try:
            _ex = pee.executable_edge(
                _e.odds_api_prob, _e.bet_team,
                token_id=_tid, condition_id=_e.poly_market_id,
                outcome_index=_oi, target_usd=100.0, book=_book,
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
            _pm = pee.poly_price_move(token_id=_tid, condition_id=_e.poly_market_id, outcome_index=_oi)
        except Exception:
            _pm = {"available": False}
        if _pm.get("available"):
            _e.poly_move_1h = _pm["move_1h_pp"]
            _e.poly_move_6h = _pm["move_6h_pp"]

    # P3.4: hint the WS service to stream these markets (fire-and-forget, never raises)
    if _reg_tokens:
        try:
            from api.services import poly_ws_reader as _pwr
            await _pwr.register_watch(_reg_tokens)
        except Exception:
            pass

    # Post-enrichment shadow logging (uses executable price, fee-adjusted, same
    # pattern as soccer_match_edge.py / ufc_edge.py). Replaces the old pre-enrichment
    # _log_baseball_shadow which logged midpoint prices.
    _baseball_cfg = sec.SportConfig(
        name="baseball",
        odds_api_sport_keys=["baseball_mlb"],
        polymarket_tag="baseball",
        market_model="2way",
        featured_markets=["h2h"],
        shadow_strategy="baseball_{market_type}",
    )
    for _e in edges:
        # sec.log_shadow uses the strategy from cfg.shadow_strategy, but baseball
        # has per-market-type strategies. Override the cfg name per edge.
        _cfg = sec.SportConfig(
            name="baseball",
            odds_api_sport_keys=["baseball_mlb"],
            polymarket_tag="baseball",
            market_model="2way",
            featured_markets=["h2h"],
            shadow_strategy=f"baseball_{_e.market_type}",
        )
        sec.log_shadow(_e, _cfg)

        # --- Tight-filter experiment: BUY-only, edge 5-8% sweet spot ---
        # Backtest (N=20): 70% WR, +$4.55/$1 vs original 3-15% gate 52% WR.
        # Runs as a parallel shadow under strategy "baseball_{type}_tight"
        # so original system is unaffected. Compare resolution rates weekly.
        if (_e.direction == "BUY"
                and 0.05 <= _e.edge_pct <= 0.08
                and _e.tradeable
                and _e.executable_price is not None):
            _tight_cfg = sec.SportConfig(
                name="baseball",
                odds_api_sport_keys=["baseball_mlb"],
                polymarket_tag="baseball",
                market_model="2way",
                featured_markets=["h2h"],
                shadow_strategy=f"baseball_{_e.market_type}_tight",
            )
            sec.log_shadow(_e, _tight_cfg)

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
                "live_book": e.live_book,
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

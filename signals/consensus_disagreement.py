"""
Cross-sport consensus disagreement scanner — generalizes baseball_edge.py pattern.
Compares sportsbook consensus true probability vs prediction market price for all sports.
Credit-budget aware, fee-adjusted.

CE-5: Sportsbook Consensus Disagreement Index
"""

import json
import os
import re
import unicodedata
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from loguru import logger

# ─── Credit Budget Manager ──────────────────────────────────────────────
CREDIT_FILE = Path(__file__).parent.parent / "storage" / "ce5_credit_usage.json"
MAX_DAILY_CREDITS = 3000

# ─── Fee map (CE-1 pattern) ────────────────────────────────────────────
# Polymarket fee: per-category taker at entry (0% on winnings) -- see execution.fee_model
# Kalshi: quadratic fee max ~1.75¢ at P=0.5
from execution.fee_model import taker_fee_fraction

MIN_FEE_ADJUSTED_DISAGREEMENT_PP = 3.0
MIN_BOOKMAKERS = 3
CACHE_TTL_SEC = 900  # 15 min
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"

# ─── Sport configs ─────────────────────────────────────────────────────
SPORT_CONFIGS: Dict[str, dict] = {
    "baseball_mlb": {
        "odds_key": "baseball_mlb",
        "pm_tag_slug": "baseball",
        "display_name": "Baseball (MLB)",
        "model": "2way",
        "bookmakers": "pinnacle,draftkings,fanduel,betmgm,betrivers,williamhill_us,bovada",
        "estimated_credits": 2,
    },
    "basketball_nba": {
        "odds_key": "basketball_nba",
        "pm_tag_slug": "basketball",
        "display_name": "Basketball (NBA)",
        "model": "2way",
        "bookmakers": "pinnacle,draftkings,fanduel,betmgm,betrivers,williamhill_us,bovada",
        "estimated_credits": 2,
    },
    "americanfootball_nfl": {
        "odds_key": "americanfootball_nfl",
        "pm_tag_slug": None,
        "display_name": "Football (NFL)",
        "model": "2way",
        "bookmakers": "pinnacle,draftkings,fanduel,betmgm,betrivers,williamhill_us,bovada",
        "estimated_credits": 2,
    },
    "icehockey_nhl": {
        "odds_key": "icehockey_nhl",
        "pm_tag_slug": "hockey",
        "display_name": "Hockey (NHL)",
        "model": "2way",
        "bookmakers": "pinnacle,draftkings,fanduel,betmgm,betrivers,williamhill_us,bovada",
        "estimated_credits": 2,
    },
    "soccer_epl": {
        "odds_key": "soccer_epl",
        "pm_tag_slug": "soccer",
        "display_name": "Soccer (EPL)",
        "model": "3way",
        "bookmakers": "pinnacle,betfair_ex_uk,betfair_ex_eu,draftkings,fanduel,williamhill_us,betmgm",
        "estimated_credits": 2,
    },
    "mma_mixed_martial_arts": {
        "odds_key": "mma_mixed_martial_arts",
        "pm_tag_slug": "ufc",
        "display_name": "MMA (UFC)",
        "model": "2way",
        "bookmakers": "pinnacle,draftkings,fanduel,betmgm,betrivers,williamhill_us",
        "estimated_credits": 2,
    },
}

# ─── Team name aliases (expanding baseball_edge pattern) ──────────────
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

NBA_TEAM_ALIASES: Dict[str, List[str]] = {
    "Atlanta Hawks": ["Atlanta Hawks", "Hawks"],
    "Boston Celtics": ["Boston Celtics", "Celtics"],
    "Brooklyn Nets": ["Brooklyn Nets", "Nets"],
    "Charlotte Hornets": ["Charlotte Hornets", "Hornets"],
    "Chicago Bulls": ["Chicago Bulls", "Bulls"],
    "Cleveland Cavaliers": ["Cleveland Cavaliers", "Cavaliers"],
    "Dallas Mavericks": ["Dallas Mavericks", "Mavericks", "Mavs"],
    "Denver Nuggets": ["Denver Nuggets", "Nuggets"],
    "Detroit Pistons": ["Detroit Pistons", "Pistons"],
    "Golden State Warriors": ["Golden State Warriors", "Warriors", "GSW"],
    "Houston Rockets": ["Houston Rockets", "Rockets"],
    "Indiana Pacers": ["Indiana Pacers", "Pacers"],
    "Los Angeles Clippers": ["Los Angeles Clippers", "Clippers", "LAC"],
    "Los Angeles Lakers": ["Los Angeles Lakers", "Lakers", "LAL"],
    "Memphis Grizzlies": ["Memphis Grizzlies", "Grizzlies"],
    "Miami Heat": ["Miami Heat", "Heat"],
    "Milwaukee Bucks": ["Milwaukee Bucks", "Bucks"],
    "Minnesota Timberwolves": ["Minnesota Timberwolves", "Timberwolves", "Wolves"],
    "New Orleans Pelicans": ["New Orleans Pelicans", "Pelicans"],
    "New York Knicks": ["New York Knicks", "Knicks"],
    "Oklahoma City Thunder": ["Oklahoma City Thunder", "Thunder", "OKC"],
    "Orlando Magic": ["Orlando Magic", "Magic"],
    "Philadelphia 76ers": ["Philadelphia 76ers", "76ers", "Sixers"],
    "Phoenix Suns": ["Phoenix Suns", "Suns"],
    "Portland Trail Blazers": ["Portland Trail Blazers", "Trail Blazers", "Blazers"],
    "Sacramento Kings": ["Sacramento Kings", "Kings"],
    "San Antonio Spurs": ["San Antonio Spurs", "Spurs"],
    "Toronto Raptors": ["Toronto Raptors", "Raptors"],
    "Utah Jazz": ["Utah Jazz", "Jazz"],
    "Washington Wizards": ["Washington Wizards", "Wizards"],
}

NFL_TEAM_ALIASES: Dict[str, List[str]] = {
    "Arizona Cardinals": ["Arizona Cardinals", "Cardinals"],
    "Atlanta Falcons": ["Atlanta Falcons", "Falcons"],
    "Baltimore Ravens": ["Baltimore Ravens", "Ravens"],
    "Buffalo Bills": ["Buffalo Bills", "Bills"],
    "Carolina Panthers": ["Carolina Panthers", "Panthers"],
    "Chicago Bears": ["Chicago Bears", "Bears"],
    "Cincinnati Bengals": ["Cincinnati Bengals", "Bengals"],
    "Cleveland Browns": ["Cleveland Browns", "Browns"],
    "Dallas Cowboys": ["Dallas Cowboys", "Cowboys"],
    "Denver Broncos": ["Denver Broncos", "Broncos"],
    "Detroit Lions": ["Detroit Lions", "Lions"],
    "Green Bay Packers": ["Green Bay Packers", "Packers"],
    "Houston Texans": ["Houston Texans", "Texans"],
    "Indianapolis Colts": ["Indianapolis Colts", "Colts"],
    "Jacksonville Jaguars": ["Jacksonville Jaguars", "Jaguars"],
    "Kansas City Chiefs": ["Kansas City Chiefs", "Chiefs"],
    "Las Vegas Raiders": ["Las Vegas Raiders", "Raiders"],
    "Los Angeles Chargers": ["Los Angeles Chargers", "Chargers"],
    "Los Angeles Rams": ["Los Angeles Rams", "Rams"],
    "Miami Dolphins": ["Miami Dolphins", "Dolphins"],
    "Minnesota Vikings": ["Minnesota Vikings", "Vikings"],
    "New England Patriots": ["New England Patriots", "Patriots"],
    "New Orleans Saints": ["New Orleans Saints", "Saints"],
    "New York Giants": ["New York Giants", "Giants"],
    "New York Jets": ["New York Jets", "Jets"],
    "Philadelphia Eagles": ["Philadelphia Eagles", "Eagles"],
    "Pittsburgh Steelers": ["Pittsburgh Steelers", "Steelers"],
    "San Francisco 49ers": ["San Francisco 49ers", "49ers"],
    "Seattle Seahawks": ["Seattle Seahawks", "Seahawks"],
    "Tampa Bay Buccaneers": ["Tampa Bay Buccaneers", "Buccaneers", "Bucs"],
    "Tennessee Titans": ["Tennessee Titans", "Titans"],
    "Washington Commanders": ["Washington Commanders", "Commanders"],
}

NHL_TEAM_ALIASES: Dict[str, List[str]] = {
    "Anaheim Ducks": ["Anaheim Ducks", "Ducks"],
    "Boston Bruins": ["Boston Bruins", "Bruins"],
    "Buffalo Sabres": ["Buffalo Sabres", "Sabres"],
    "Calgary Flames": ["Calgary Flames", "Flames"],
    "Carolina Hurricanes": ["Carolina Hurricanes", "Hurricanes", "Canes"],
    "Chicago Blackhawks": ["Chicago Blackhawks", "Blackhawks"],
    "Colorado Avalanche": ["Colorado Avalanche", "Avalanche", "Avs"],
    "Columbus Blue Jackets": ["Columbus Blue Jackets", "Blue Jackets"],
    "Dallas Stars": ["Dallas Stars", "Stars"],
    "Detroit Red Wings": ["Detroit Red Wings", "Red Wings"],
    "Edmonton Oilers": ["Edmonton Oilers", "Oilers"],
    "Florida Panthers": ["Florida Panthers", "Panthers"],
    "Los Angeles Kings": ["Los Angeles Kings", "Kings"],
    "Minnesota Wild": ["Minnesota Wild", "Wild"],
    "Montreal Canadiens": ["Montreal Canadiens", "Canadiens", "Habs"],
    "Nashville Predators": ["Nashville Predators", "Predators", "Preds"],
    "New Jersey Devils": ["New Jersey Devils", "Devils"],
    "New York Islanders": ["New York Islanders", "Islanders"],
    "New York Rangers": ["New York Rangers", "Rangers"],
    "Ottawa Senators": ["Ottawa Senators", "Senators", "Sens"],
    "Philadelphia Flyers": ["Philadelphia Flyers", "Flyers"],
    "Pittsburgh Penguins": ["Pittsburgh Penguins", "Penguins"],
    "San Jose Sharks": ["San Jose Sharks", "Sharks"],
    "Seattle Kraken": ["Seattle Kraken", "Kraken"],
    "St. Louis Blues": ["St. Louis Blues", "Blues"],
    "Tampa Bay Lightning": ["Tampa Bay Lightning", "Lightning", "Bolts"],
    "Toronto Maple Leafs": ["Toronto Maple Leafs", "Maple Leafs", "Leafs"],
    "Utah HC": ["Utah HC", "Utah Hockey Club", "Utah"],
    "Vancouver Canucks": ["Vancouver Canucks", "Canucks"],
    "Vegas Golden Knights": ["Vegas Golden Knights", "Golden Knights", "VGK"],
    "Washington Capitals": ["Washington Capitals", "Capitals", "Caps"],
    "Winnipeg Jets": ["Winnipeg Jets", "Jets"],
}

# Soccer: reuse SOCCER_NATION_ALIASES from sports_edge_common
SOCCER_CLUB_ALIASES: Dict[str, List[str]] = {
    "Manchester City": ["Manchester City", "Man City", "Manchester City FC"],
    "Manchester United": ["Manchester United", "Man United", "Manchester Utd"],
    "Liverpool": ["Liverpool", "Liverpool FC"],
    "Arsenal": ["Arsenal", "Arsenal FC"],
    "Chelsea": ["Chelsea", "Chelsea FC"],
    "Tottenham": ["Tottenham", "Tottenham Hotspur", "Spurs"],
    "Newcastle": ["Newcastle", "Newcastle United"],
    "Aston Villa": ["Aston Villa", "Aston Villa FC"],
    "Barcelona": ["Barcelona", "FC Barcelona", "Barca"],
    "Real Madrid": ["Real Madrid", "Real Madrid CF", "Madrid"],
    "Atletico Madrid": ["Atletico Madrid", "Atletico", "Atlético Madrid"],
    "Bayern Munich": ["Bayern Munich", "Bayern München", "FC Bayern"],
    "Borussia Dortmund": ["Borussia Dortmund", "Dortmund", "BVB"],
    "Paris Saint Germain": ["Paris Saint Germain", "PSG", "Paris SG"],
    "Inter Milan": ["Inter Milan", "Inter", "Internazionale"],
    "AC Milan": ["AC Milan", "Milan"],
    "Juventus": ["Juventus", "Juventus FC", "Juve"],
    "Ajax": ["Ajax", "Ajax Amsterdam"],
}

# Map from odds_key -> team aliases
SPORT_ALIAS_MAP: Dict[str, Dict[str, List[str]]] = {
    "baseball_mlb": MLB_TEAM_ALIASES,
    "basketball_nba": NBA_TEAM_ALIASES,
    "americanfootball_nfl": NFL_TEAM_ALIASES,
    "icehockey_nhl": NHL_TEAM_ALIASES,
    "soccer_epl": SOCCER_CLUB_ALIASES,
    "mma_mixed_martial_arts": {},  # UFC fighters are usually exact name matches
}

# ─── In-memory cache ───────────────────────────────────────────────────
_sport_cache: Dict[str, dict] = {}
_credit_state: Optional[dict] = None


# ─── Helpers ───────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    """Lowercase + strip diacritics (NFKD)."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", s or "")
        if not unicodedata.combining(c)
    ).lower().strip()


def _team_in_title(team: str, title: str, aliases: Dict[str, List[str]]) -> bool:
    """Check if team or any alias appears in title text."""
    title_lower = _norm(title)
    for a in aliases.get(team, [team]):
        if _norm(a) in title_lower:
            return True
    return False


from odds.sports_edge_common import (
    american_to_implied_prob as _american_to_implied_prob,
    consensus_devig_2way as _sec_consensus_devig_2way,
    consensus_devig_3way as _sec_consensus_devig_3way,
)


def _get_api_key() -> Optional[str]:
    return os.getenv("ODDS_API_KEY") or None


def _is_stale_event(commence_time: str) -> bool:
    if not commence_time:
        return True
    try:
        gt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        return (gt - datetime.now(timezone.utc)).total_seconds() / 60 < 30
    except (ValueError, TypeError):
        return True


# ─── Credit Budget Manager ─────────────────────────────────────────────

def _ensure_credit_state():
    global _credit_state
    today = datetime.now().strftime("%Y-%m-%d")
    if _credit_state is not None and _credit_state.get("date") == today:
        return
    default = {"date": today, "credits_consumed": 0, "max_daily": MAX_DAILY_CREDITS}
    if CREDIT_FILE.exists():
        try:
            with open(CREDIT_FILE) as f:
                data = json.load(f)
            _credit_state = data if data.get("date") == today else default
        except Exception:
            _credit_state = default
    else:
        _credit_state = default


def _save_credit_state():
    global _credit_state
    if _credit_state is None:
        return
    CREDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CREDIT_FILE, "w") as f:
        json.dump(_credit_state, f, indent=2, sort_keys=True)


def sports_odds_api_credit(credits: int = 1) -> bool:
    """Check budget and consume credits. Returns True if OK, False if exhausted."""
    _ensure_credit_state()
    if _credit_state["credits_consumed"] + credits > _credit_state["max_daily"]:
        logger.warning(
            f"CE-5 credit budget exhausted: "
            f"{_credit_state['credits_consumed']}/{_credit_state['max_daily']}"
        )
        return False
    _credit_state["credits_consumed"] += credits
    _save_credit_state()
    return True


def get_credit_status() -> dict:
    _ensure_credit_state()
    return {
        "credits_consumed": _credit_state["credits_consumed"],
        "credits_remaining": _credit_state["max_daily"] - _credit_state["credits_consumed"],
        "max_daily": _credit_state["max_daily"],
        "date": _credit_state["date"],
    }


# ─── Cache management ─────────────────────────────────────────────────

def _get_cached_sport(sport_key: str) -> Optional[list]:
    """Return cached data if fresh (<CACHE_TTL_SEC old)."""
    entry = _sport_cache.get(sport_key)
    if entry and (time.time() - entry["ts"]) < CACHE_TTL_SEC:
        return entry["data"]
    return None


def _set_sport_cache(sport_key: str, data: list):
    _sport_cache[sport_key] = {"ts": time.time(), "data": data}


# ─── Odds API fetch ────────────────────────────────────────────────────

def _fetch_odds_sync(sport_key: str, api_key: str) -> list:
    """Fetch h2h odds for one sport key. Returns list of events or []."""
    params = {
        "apiKey": api_key,
        "regions": "us",
        "markets": "h2h",
        "oddsFormat": "american",
    }
    url = f"{ODDS_API_BASE}/sports/{sport_key}/odds"
    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            # Track credits from response headers
            remaining = resp.headers.get("x-requests-remaining")
            used = resp.headers.get("x-requests-used")
            if remaining:
                try:
                    from odds.rate_limiter import persist_real_remaining
                    persist_real_remaining(int(remaining), int(used) if used else None)
                except Exception:
                    pass
            data = resp.json()
            return data if isinstance(data, list) else []
        else:
            logger.warning(f"CE-5 Odds API {sport_key} returned {resp.status_code}")
            return []
    except Exception as e:
        logger.warning(f"CE-5 Odds API fetch failed for {sport_key}: {e}")
        return []


# ─── Polymarket fetch ──────────────────────────────────────────────────

def _fetch_polymarket_events_sync(tag_slug: str) -> list:
    """Fetch active events by tag_slug from Gamma API."""
    try:
        resp = requests.get(
            f"{POLYMARKET_GAMMA}/events",
            params={"closed": "false", "tag_slug": tag_slug, "limit": "100"},
            timeout=30,
        )
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"CE-5 Polymarket fetch failed for tag={tag_slug}: {e}")
        return []


def _find_poly_price(
    home_team: str,
    away_team: str,
    poly_events: list,
    aliases: Dict[str, List[str]],
) -> Optional[Tuple[float, float]]:
    """Find Polymarket (home_prob, away_prob) for a game event.
    Returns None if no matching event found."""
    for event in poly_events:
        title = event.get("title", "")
        if " vs. " not in title and " vs " not in title:
            continue
        if _team_in_title(home_team, title, aliases) and _team_in_title(away_team, title, aliases):
            # Found matching event — extract moneyline prices
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
                # Determine which price is home vs away
                # Title format: "[first_team] vs. [second_team]"
                first_fragment = title.split(" vs")[0].strip()
                first_is_home = _team_in_title(home_team, first_fragment, aliases)
                if first_is_home:
                    return (price0, price1)
                else:
                    return (price1, price0)
    return None


def _compute_consensus_2way(event: dict) -> Optional[dict]:
    """Compute weighted consensus prob for a 2-way event.
    Returns {team: true_prob} or None."""
    return _sec_consensus_devig_2way(event, "h2h") or None


def _compute_consensus_3way(event: dict) -> Optional[dict]:
    """Compute Shin-devigged weighted consensus for 3-way events."""
    return _sec_consensus_devig_3way(event, "h2h") or None


def _count_bookmakers(event: dict) -> int:
    """Count bookmakers that contributed h2h odds for this event."""
    count = 0
    for bk in event.get("bookmakers", []):
        w = {"pinnacle": 0.35, "draftkings": 0.20, "fanduel": 0.15,
             "betmgm": 0.10, "betrivers": 0.05, "williamhill_us": 0.05,
             "bovada": 0.02, "williamhill": 0.05}.get(bk.get("key", ""), 0.0)
        if w <= 0.0:
            continue
        for mk in bk.get("markets", []):
            if mk.get("key") == "h2h" and len(mk.get("outcomes", [])) >= 2:
                count += 1
                break
    return count


def _compute_fee_adjusted_disagreement(
    raw_disagreement_pp: float,
    poly_price: float,
    direction: str,
) -> Tuple[float, float]:
    """Compute round-trip fee estimate and fee-adjusted disagreement.

    Direction:
      "NO" (prediction_market > sportsbook) → buy NO at (1 - price)
      "YES" (prediction_market < sportsbook) → buy YES at price

    Polymarket charges a per-category taker fee at the entry fill and 0% on
    winnings, so a hold-to-resolution play pays only the entry leg (sports rate).
    """
    if direction == "NO":
        # Buying NO: entry cost = 1 - poly_price
        entry_price = 1.0 - poly_price
    else:
        # Buying YES: entry cost = poly_price
        entry_price = poly_price

    # Real Polymarket sports taker fee at entry: 0.03 * p * (1-p).
    round_trip_fees_pp = taker_fee_fraction(entry_price, "polymarket", "sports") * 100.0
    fee_adjusted_pp = raw_disagreement_pp - round_trip_fees_pp
    return fee_adjusted_pp, round_trip_fees_pp


# ─── Main scanner ──────────────────────────────────────────────────────

def scan_all_sports_disagreement(sports_list: Optional[list] = None) -> list:
    """Scan all specified sports for sportsbook vs prediction market disagreement.

    Args:
        sports_list: list of sport keys (e.g. ["baseball_mlb", "basketball_nba"]).
                     Defaults to all configured sports.

    Returns:
        List of dicts sorted by fee_adjusted_disagreement_pp descending.
        Each dict: {sport, event, sportsbook_consensus_pct, prediction_market_pct,
                    raw_disagreement_pp, fee_adjusted_disagreement_pp, direction,
                    source_cache, n_bookmakers, credits_consumed, signal}
    """
    api_key = _get_api_key()
    if not api_key:
        logger.warning("CE-5: ODDS_API_KEY not set — returning empty results")
        return []

    if sports_list is None:
        sports_list = list(SPORT_CONFIGS.keys())

    results: list = []
    total_credits_consumed: int = 0

    for sport_key in sports_list:
        if sport_key not in SPORT_CONFIGS:
            logger.warning(f"CE-5: unknown sport '{sport_key}' — skipping")
            continue

        cfg = SPORT_CONFIGS[sport_key]
        est_credits = cfg["estimated_credits"]

        # Check budget before any call
        credit_status = get_credit_status()
        if credit_status["credits_remaining"] < est_credits:
            logger.warning(
                f"CE-5: credit budget exhausted before {sport_key} "
                f"(remaining {credit_status['credits_remaining']})"
            )
            break  # hard stop: no more budget

        # Check cache
        cached = _get_cached_sport(sport_key)
        source_cache = "fresh"
        used_credits = 0

        if cached is not None:
            odds_events = cached
            source_cache = "reused"
            logger.info(f"CE-5: {sport_key} — cache hit (reusing)")
        else:
            # Fetch from Odds API
            if not sports_odds_api_credit(est_credits):
                logger.warning(f"CE-5: credit budget exhausted on {sport_key}")
                break
            used_credits += est_credits
            total_credits_consumed += est_credits
            odds_events = _fetch_odds_sync(cfg["odds_key"], api_key)
            if odds_events:
                _set_sport_cache(sport_key, odds_events)
                source_cache = "fresh"
            else:
                logger.warning(f"CE-5: no odds events for {sport_key}")
                continue

        # Get Polymarket events for this sport
        poly_events: list = []
        pm_tag = cfg.get("pm_tag_slug")
        if pm_tag:
            poly_events = _fetch_polymarket_events_sync(pm_tag)
            # Polymarket fetch costs nothing in Odds API credits
            logger.info(
                f"CE-5: {sport_key} — {len(odds_events)} Odds events, "
                f"{len(poly_events)} Polymarket events"
            )
        else:
            logger.info(f"CE-5: {sport_key} — no PM tag configured (NFL off-season)")

        aliases = SPORT_ALIAS_MAP.get(sport_key, {})
        model = cfg["model"]
        game_events = [
            e for e in poly_events
            if " vs" in e.get("title", "")
        ]

        for event in odds_events:
            home_team = event.get("home_team", "")
            away_team = event.get("away_team", "")
            commence_time = event.get("commence_time", "")

            if not home_team or not away_team:
                continue
            if _is_stale_event(commence_time):
                continue

            n_bk = _count_bookmakers(event)
            if n_bk < MIN_BOOKMAKERS:
                continue

            # Compute consensus probability
            if model == "3way":
                consensus = _compute_consensus_3way(event)
            else:
                consensus = _compute_consensus_2way(event)

            if not consensus or len(consensus) < 2:
                continue

            # Get Polymarket price
            if game_events and pm_tag:
                poly_prices = _find_poly_price(home_team, away_team, game_events, aliases)
                if poly_prices is None:
                    continue
                home_poly_price, away_poly_price = poly_prices

                # Compare each side
                for team, book_prob, poly_price in [
                    (home_team, consensus.get(home_team), home_poly_price),
                    (away_team, consensus.get(away_team), away_poly_price),
                ]:
                    if book_prob is None or poly_price is None:
                        continue
                    if not (0.02 <= poly_price <= 0.98):
                        continue

                    raw_pp = abs(book_prob - poly_price) * 100.0
                    direction = "NO" if poly_price > book_prob else "YES"

                    fee_adj_pp, fee_pp = _compute_fee_adjusted_disagreement(
                        raw_pp, poly_price, direction
                    )

                    signal_flag = fee_adj_pp >= MIN_FEE_ADJUSTED_DISAGREEMENT_PP

                    results.append({
                        "sport": cfg["display_name"],
                        "odds_key": sport_key,
                        "event": f"{away_team} @ {home_team}",
                        "team": team,
                        "sportsbook_consensus_pct": round(book_prob * 100, 1),
                        "prediction_market_pct": round(poly_price * 100, 1),
                        "raw_disagreement_pp": round(raw_pp, 1),
                        "round_trip_fees_pp": round(fee_pp, 2),
                        "fee_adjusted_disagreement_pp": round(fee_adj_pp, 1),
                        "direction": direction,
                        "source_cache": source_cache,
                        "n_bookmakers": n_bk,
                        "credits_consumed": used_credits,
                        "signal": signal_flag,
                        "commence_time": commence_time,
                    })
            else:
                # No Polymarket events available for this sport (e.g. NFL off-season)
                # Can optionally fall back to Kalshi here, but for now skip
                continue

    # Sort by fee-adjusted disagreement descending
    results.sort(key=lambda r: r["fee_adjusted_disagreement_pp"], reverse=True)

    # Phase 1B: persist for reconciliation with per-sport engines
    try:
        persist_ce5_results(results)
    except Exception:
        pass

    return results


def scan_sport_disagreement(sport_key: str) -> dict:
    """Scan a single sport for consensus disagreement.

    Returns dict with sport-specific results and metadata.
    """
    if sport_key not in SPORT_CONFIGS:
        return {
            "sport": sport_key,
            "error": f"Unknown sport '{sport_key}'",
            "results": [],
            "credits_consumed": 0,
        }

    results = scan_all_sports_disagreement([sport_key])
    sport_results = [r for r in results if r["odds_key"] == sport_key]
    credits_used = sum(r["credits_consumed"] for r in sport_results) if sport_results else 0

    credit_status = get_credit_status()
    return {
        "sport": SPORT_CONFIGS[sport_key]["display_name"],
        "odds_key": sport_key,
        "results": sport_results,
        "total_signals": sum(1 for r in sport_results if r["signal"]),
        "total_events_scanned": len(sport_results),
        "credits_consumed": credit_status["credits_consumed"],
        "credits_remaining": credit_status["credits_remaining"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ─── CLI test ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sport = sys.argv[1] if len(sys.argv) > 1 else None
    if sport:
        result = scan_sport_disagreement(sport)
        print(f"Sport: {result['sport']}")
        print(f"Signals (fee-adj >{MIN_FEE_ADJUSTED_DISAGREEMENT_PP}pp): {result['total_signals']}")
        print(f"Events: {result['total_events_scanned']}")
        for r in result["results"][:10]:
            print(f"  {r['event']} — {r['team']}")
            print(f"    Book: {r['sportsbook_consensus_pct']}% vs PM: {r['prediction_market_pct']}%")
            print(f"    Raw: {r['raw_disagreement_pp']}pp → Fee: {r['round_trip_fees_pp']}pp → Adj: {r['fee_adjusted_disagreement_pp']}pp")
            print(f"    Signal: {r['signal']} (n_bk={r['n_bookmakers']})")
    else:
        results = scan_all_sports_disagreement()
        signals = [r for r in results if r["signal"]]
        print(f"\nCE-5: Sportsbook Consensus Disagreement Index")
        print(f"{'='*60}")
        print(f"Total signals (fee-adj >{MIN_FEE_ADJUSTED_DISAGREEMENT_PP}pp): {len(signals)}")
        print(f"Total events scanned: {len(results)}")
        print()
        for r in signals[:20]:
            print(f"  [{r['sport']}] {r['event']} — {r['team']}")
            print(f"    Book {r['sportsbook_consensus_pct']}% vs PM {r['prediction_market_pct']}%")
            print(f"    Raw Δ: {r['raw_disagreement_pp']}pp → Adj Δ: {r['fee_adjusted_disagreement_pp']}pp ({r['direction']})")
        print()
        cs = get_credit_status()
        print(f"Credits: {cs['credits_consumed']}/{cs['max_daily']} used ({cs['credits_remaining']} remaining)")


# ─── Phase 1B: DB persistence for CE-5/per-sport reconciliation ────────────
_CE5_DB_INIT = False


def _init_ce5_cache(conn):
    global _CE5_DB_INIT
    if _CE5_DB_INIT:
        return
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ce5_signal_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scanned_at TEXT NOT NULL,
            sport TEXT NOT NULL,
            event_title TEXT NOT NULL,
            team TEXT NOT NULL,
            book_pct REAL,
            pm_pct REAL,
            fee_adj_pp REAL,
            direction TEXT,
            signal INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_ce5_event ON ce5_signal_cache(event_title, team);
    """)
    _CE5_DB_INIT = True


def persist_ce5_results(results: list) -> int:
    """Write CE-5 scan results to DB for reconciliation with per-sport engines."""
    if not results:
        return 0
    try:
        import sqlite3 as _sq
        from pathlib import Path as _P
        db = _P(__file__).parent.parent / "storage" / "shadow_trades.db"
        conn = _sq.connect(str(db), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _init_ce5_cache(conn)
        now = datetime.now(timezone.utc).isoformat()
        # Purge entries older than 24h to keep table small
        conn.execute("DELETE FROM ce5_signal_cache WHERE scanned_at < datetime('now', '-24 hours')")
        n = 0
        for r in results:
            conn.execute(
                """INSERT INTO ce5_signal_cache
                   (scanned_at, sport, event_title, team, book_pct, pm_pct,
                    fee_adj_pp, direction, signal)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (now, r.get("odds_key", ""), r.get("event", "")[:180],
                 r.get("team", "")[:80],
                 r.get("sportsbook_consensus_pct"), r.get("prediction_market_pct"),
                 r.get("fee_adjusted_disagreement_pp"), r.get("direction"),
                 1 if r.get("signal") else 0),
            )
            n += 1
        conn.commit()
        conn.close()
        return n
    except Exception as e:
        logger.debug(f"CE-5 persist failed: {e}")
        return 0


def check_ce5_agrees(event_title: str, participant: str, direction: str) -> Optional[bool]:
    """Check if CE-5 has a recent signal for this event+participant that agrees
    on direction. Returns True (agrees), False (disagrees), None (no data)."""
    try:
        import sqlite3 as _sq
        from pathlib import Path as _P
        db = _P(__file__).parent.parent / "storage" / "shadow_trades.db"
        conn = _sq.connect(str(db), timeout=5)
        conn.row_factory = _sq.Row
        _init_ce5_cache(conn)
        # Fuzzy match: event title contains participant, or team matches
        rows = conn.execute(
            """SELECT direction, fee_adj_pp, signal FROM ce5_signal_cache
               WHERE (team LIKE ? OR event_title LIKE ?)
               AND scanned_at > datetime('now', '-6 hours')
               ORDER BY scanned_at DESC LIMIT 1""",
            (f"%{participant[:20]}%", f"%{participant[:20]}%"),
        ).fetchall()
        conn.close()
        if not rows:
            return None
        r = rows[0]
        ce5_dir = r["direction"]
        # CE-5 direction is YES/NO, per-sport is BUY/SELL (BUY=YES, SELL=NO)
        norm_dir = "YES" if direction.upper() in ("YES", "BUY") else "NO"
        return ce5_dir == norm_dir
    except Exception:
        return None
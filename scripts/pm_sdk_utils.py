"""
pm_sdk_utils.py — PM US SDK moneyline BBO helpers for all sports.

ARCHITECTURE
============
PM has TWO market systems per sport:
  1. Gamma CLOB  (gamma-api.polymarket.com/events)
     → Only SOME games. Endgame MMs maintain live in-game book. Use for real-time prices.
  2. PM US SDK   (PolymarketUS().markets.bbo(slug))
     → ALL games. BUT Endgame does NOT provide live in-game pricing on SDK-only markets.
       BBO is pre-game and goes stale the moment the game starts.

Each monitor should: try Gamma CLOB first → fallback to this module.

CRITICAL: NEVER use SDK search to find moneylines.
  - MLB moneyline = market #62 of 85 per SDK event (search returns ~8)
  - Soccer = 373 markets per game (search returns subset, moneyline never appears)
  - UFC / NBA / NFL: same issue
  Always construct the slug directly and call c.markets.bbo(slug).

SLUG PATTERNS
=============
Sport     | Event slug                              | Moneyline slug
----------|------------------------------------------|-------------------------------
MLB       | (none needed)                            | aec-mlb-{away}-{home}-{date}
Soccer WC | fwc-{team1}-{team2}-{date}              | atc-fwc-{id}-{team}  (3-way)
UFC       | ufc-{f1_abbr}-{f2_abbr}-{date}         | aec-ufc-{f1_abbr}-{f2_abbr}-{date}
NBA       | nba-{away}-{home}-{date}                | aec-nba-{away}-{home}-{date}
NFL       | nfl-{away}-{home}-{date}                | aec-nfl-{away}-{home}-{date}

RETURN FORMAT
=============
All functions return: Dict[str, Tuple[str, float, bool]]
  {label: (slug, mid_price, liquid)}
  - liquid=True  → Endgame active, prices plausible (lastTradePx set + ≤15pp off Vegas)
  - liquid=False → pre-game price only; do not use for trade signals

SOCCER 3-WAY: keys are "home", "away", "draw"
UFC/MLB/NBA/NFL: keys are "home"/"away" (or "a"/"b" for UFC to match monitor convention)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Tuple

# ── MLB abbreviations ─────────────────────────────────────────────────────────
MLB_ABBR: Dict[str, str] = {
    # PM US uses "az" for Arizona (verified live 2026-07-02: aec-mlb-sf-az-… = 200, sf-ari = 404)
    "arizona diamondbacks": "az", "diamondbacks": "az", "d-backs": "az",
    "atlanta braves": "atl", "braves": "atl",
    "baltimore orioles": "bal", "orioles": "bal",
    "boston red sox": "bos", "red sox": "bos",
    "chicago cubs": "chc", "cubs": "chc",
    "chicago white sox": "cws", "white sox": "cws",
    "cincinnati reds": "cin", "reds": "cin",
    "cleveland guardians": "cle", "guardians": "cle",
    "colorado rockies": "col", "rockies": "col",
    "detroit tigers": "det", "tigers": "det",
    "houston astros": "hou", "astros": "hou",
    "kansas city royals": "kc", "royals": "kc",
    "los angeles angels": "laa", "angels": "laa",
    "los angeles dodgers": "lad", "dodgers": "lad",
    "miami marlins": "mia", "marlins": "mia",
    "milwaukee brewers": "mil", "brewers": "mil",
    "minnesota twins": "min", "twins": "min",
    "new york mets": "nym", "mets": "nym",
    "new york yankees": "nyy", "yankees": "nyy",
    # PM US uses "ath" for the Athletics (verified live 2026-07-02: aec-mlb-lad-ath-… = 200, lad-oak = 404)
    "oakland athletics": "ath", "athletics": "ath", "a's": "ath",
    "philadelphia phillies": "phi", "phillies": "phi",
    "pittsburgh pirates": "pit", "pirates": "pit",
    "san diego padres": "sd", "padres": "sd",
    "san francisco giants": "sf", "giants": "sf",
    "seattle mariners": "sea", "mariners": "sea",
    "st. louis cardinals": "stl", "cardinals": "stl",
    "tampa bay rays": "tb", "rays": "tb",
    "texas rangers": "tex", "rangers": "tex",
    "toronto blue jays": "tor", "blue jays": "tor",
    "washington nationals": "was", "nationals": "was",
}

# ── NBA abbreviations ─────────────────────────────────────────────────────────
NBA_ABBR: Dict[str, str] = {
    "atlanta hawks": "atl", "hawks": "atl",
    "boston celtics": "bos", "celtics": "bos",
    "brooklyn nets": "bkn", "nets": "bkn",
    "charlotte hornets": "cha", "hornets": "cha",
    "chicago bulls": "chi", "bulls": "chi",
    "cleveland cavaliers": "cle", "cavaliers": "cle", "cavs": "cle",
    "dallas mavericks": "dal", "mavericks": "dal", "mavs": "dal",
    "denver nuggets": "den", "nuggets": "den",
    "detroit pistons": "det", "pistons": "det",
    "golden state warriors": "gsw", "warriors": "gsw",
    "houston rockets": "hou", "rockets": "hou",
    "indiana pacers": "ind", "pacers": "ind",
    "los angeles clippers": "lac", "clippers": "lac",
    "los angeles lakers": "lal", "lakers": "lal",
    "memphis grizzlies": "mem", "grizzlies": "mem",
    "miami heat": "mia", "heat": "mia",
    "milwaukee bucks": "mil", "bucks": "mil",
    "minnesota timberwolves": "min", "timberwolves": "min", "wolves": "min",
    "new orleans pelicans": "nop", "pelicans": "nop",
    "new york knicks": "nyk", "knicks": "nyk",
    "oklahoma city thunder": "okc", "thunder": "okc",
    "orlando magic": "orl", "magic": "orl",
    "philadelphia 76ers": "phi", "76ers": "phi", "sixers": "phi",
    "phoenix suns": "phx", "suns": "phx",
    "portland trail blazers": "por", "trail blazers": "por", "blazers": "por",
    "sacramento kings": "sac", "kings": "sac",
    "san antonio spurs": "sas", "spurs": "sas",
    "toronto raptors": "tor", "raptors": "tor",
    "utah jazz": "uta", "jazz": "uta",
    "washington wizards": "was", "wizards": "was",
}

# ── NFL abbreviations ─────────────────────────────────────────────────────────
NFL_ABBR: Dict[str, str] = {
    "arizona cardinals": "ari", "cardinals": "ari",
    "atlanta falcons": "atl", "falcons": "atl",
    "baltimore ravens": "bal", "ravens": "bal",
    "buffalo bills": "buf", "bills": "buf",
    "carolina panthers": "car", "panthers": "car",
    "chicago bears": "chi", "bears": "chi",
    "cincinnati bengals": "cin", "bengals": "cin",
    "cleveland browns": "cle", "browns": "cle",
    "dallas cowboys": "dal", "cowboys": "dal",
    "denver broncos": "den", "broncos": "den",
    "detroit lions": "det", "lions": "det",
    "green bay packers": "gb", "packers": "gb",
    "houston texans": "hou", "texans": "hou",
    "indianapolis colts": "ind", "colts": "ind",
    "jacksonville jaguars": "jac", "jaguars": "jac",
    "kansas city chiefs": "kc", "chiefs": "kc",
    "las vegas raiders": "lv", "raiders": "lv",
    "los angeles chargers": "lac", "chargers": "lac",
    "los angeles rams": "lar", "rams": "lar",
    "miami dolphins": "mia", "dolphins": "mia",
    "minnesota vikings": "min", "vikings": "min",
    "new england patriots": "ne", "patriots": "ne",
    "new orleans saints": "no", "saints": "no",
    "new york giants": "nyg", "giants": "nyg",
    "new york jets": "nyj", "jets": "nyj",
    "philadelphia eagles": "phi", "eagles": "phi",
    "pittsburgh steelers": "pit", "steelers": "pit",
    "san francisco 49ers": "sf", "49ers": "sf",
    "seattle seahawks": "sea", "seahawks": "sea",
    "tampa bay buccaneers": "tb", "buccaneers": "tb", "bucs": "tb",
    "tennessee titans": "ten", "titans": "ten",
    "washington commanders": "was", "commanders": "was",
}


def _abbr(name: str, table: Dict[str, str]) -> Optional[str]:
    return table.get(name.lower().strip())


def _ufc_abbr(full_name: str) -> str:
    """Max Holloway → maxhol, Conor McGregor → conmcg"""
    parts = full_name.strip().lower().split()
    if len(parts) < 2:
        return full_name[:6].lower()
    return parts[0][:3] + parts[-1][:3]


def _bbo_to_token(
    c,
    slug: str,
    vegas_prob: Optional[float] = None,
    sdk_gap_pp: float = 15.0,
) -> Optional[Tuple[str, float, bool]]:
    """
    Call BBO on slug, return (slug, mid_price, liquid).
    liquid=False if:
      - no valid bid/ask
      - lastTradePx is None (never traded = fully dormant)
      - gap vs Vegas > sdk_gap_pp (pre-game price, Endgame not maintaining live book)
    """
    try:
        bbo = c.markets.bbo(slug)
        md = bbo.get("marketData", {})
        best_bid = float((md.get("bestBid") or {}).get("value", 0) or 0)
        best_ask = float((md.get("bestAsk") or {}).get("value", 1) or 1)
        last_trade = md.get("lastTradePx")

        if best_bid <= 0 or best_ask >= 1 or best_ask <= best_bid:
            return None  # no valid book

        mid = (best_bid + best_ask) / 2
        liquid = last_trade is not None

        if liquid and vegas_prob is not None:
            gap = abs((vegas_prob - mid) * 100)
            if gap > sdk_gap_pp:
                liquid = False

        return (slug, mid, liquid)
    except Exception:
        return None


def _date_candidates() -> list:
    """Return [today-ET, yesterday-ET] as YYYY-MM-DD strings.

    PM US slugs are dated by the ET *game date*, not UTC. After 00:00 UTC
    (20:00 ET) the UTC date runs a day ahead and every live-game slug 404s
    (root of the 2026-07-01 23:57 UTC 404 storm). A live game is dated
    today-ET or — late west-coast games past midnight ET — yesterday-ET.
    Never tomorrow: that slug is a different (pre-game) market.
    """
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
    return [now_et.strftime("%Y-%m-%d"), (now_et - timedelta(days=1)).strftime("%Y-%m-%d")]


# ── MLB ───────────────────────────────────────────────────────────────────────
def fetch_pm_sdk_mlb(
    home: str, away: str,
    pin: Optional[Dict[str, float]] = None,
) -> Dict[str, Tuple[str, float, bool]]:
    """
    Returns {"home": (slug, mid, liquid), "away": (slug, mid, liquid)}
    or {} if not found.

    Slug: aec-mlb-{away_abbr}-{home_abbr}-{YYYY-MM-DD}  (date = ET game date)
    YES outcome = away team wins.
    """
    try:
        from polymarket_us import PolymarketUS
        c = PolymarketUS()

        away_abbr = _abbr(away, MLB_ABBR)
        home_abbr = _abbr(home, MLB_ABBR)

        if away_abbr and home_abbr:
            for game_date in _date_candidates():
                slug = f"aec-mlb-{away_abbr}-{home_abbr}-{game_date}"
                away_pin = next((v for k, v in pin.items() if k.lower() in away.lower() or away.lower() in k.lower()), None) if pin else None
                tok = _bbo_to_token(c, slug, vegas_prob=away_pin)
                if tok:
                    away_p = tok[1]
                    liquid = tok[2]
                    print(f"[pm_sdk] MLB direct slug hit: {slug} away={away_p:.2f} liquid={liquid}", flush=True)
                    return {
                        "away": (slug, away_p, liquid),
                        "home": (slug, 1.0 - away_p, liquid),
                    }

        # Fallback: SDK search (less reliable — returns ~8/85 markets per event)
        from polymarket_us import PolymarketUS
        c = PolymarketUS()
        resp = c.search.query({"query": f"{away} {home} mlb"})
        events = resp if isinstance(resp, list) else resp.get("events", resp.get("results", []))
        for ev in events:
            title = ev.get("title", "").lower()
            if " vs" not in title:
                continue
            if not (away.lower() in title or any(a in title for a in MLB_ABBR.get(away.lower(), []))):
                continue
            for m in (ev.get("markets", []) or []):
                if m.get("sportsMarketType") != "baseball_team_full_game_winner":
                    continue
                slug = m.get("slug", "")
                if not slug:
                    continue
                tok = _bbo_to_token(c, slug)
                if tok:
                    parts = title.split(" vs")
                    first_is_away = any(a in parts[0] for a in [away.lower()] + MLB_ABBR.get(away.lower(), []))
                    if first_is_away:
                        return {"away": tok, "home": (slug, 1.0 - tok[1], tok[2])}
                    else:
                        return {"home": tok, "away": (slug, 1.0 - tok[1], tok[2])}
    except Exception as e:
        print(f"[pm_sdk] MLB fallback error: {e}", flush=True)
    return {}


# ── Soccer (WC 2026) ──────────────────────────────────────────────────────────
def fetch_pm_sdk_soccer(
    home: str, away: str,
    competition: str = "fwc",
    pin: Optional[Dict[str, float]] = None,
) -> Dict[str, Tuple[str, float, bool]]:
    """
    Returns {"home": ..., "away": ..., "draw": ...} or subset.

    Soccer is 3-way: each outcome is a SEPARATE market (atc- prefix).
    Event slug: {comp}-{team1}-{team2}-{date}
    Moneyline slugs: atc-{event_slug}-{team_code} and atc-{event_slug}-draw

    Cannot construct slugs from names without FIFA codes — so we search for the
    event first, extract the event slug, then derive the 3 BBO slugs from it.
    """
    try:
        from polymarket_us import PolymarketUS
        c = PolymarketUS()

        # Search for the event by team names
        resp = c.search.query({"query": f"{home} {away} {competition}"})
        events = resp if isinstance(resp, list) else resp.get("events", resp.get("results", []))

        for ev in events:
            title = ev.get("title", "").lower()
            ev_slug = ev.get("slug", "")
            if not ev_slug or " vs" not in title and "." not in title:
                continue
            # Loose match — both team names must appear in title
            home_l, away_l = home.lower(), away.lower()
            home_short = home_l.split()[-1]  # "Colombia" → "colombia"
            away_short = away_l.split()[-1]
            if not ((home_short in title or home_l in title) and
                    (away_short in title or away_l in title)):
                continue

            # Parse team codes from event slug: fwc-col-cod-2026-06-23 → col, cod
            parts = ev_slug.split("-")
            # parts[0] = competition prefix (fwc, epl, ucl...)
            # parts[-3] = year, parts[-2] = month, parts[-1] = day
            # parts[1:-3] = team codes (typically 2 codes)
            if len(parts) < 5:
                continue
            team_codes = parts[1:-3]  # e.g. ["col", "cod"]
            if len(team_codes) < 2:
                continue

            # Determine which code is home vs away from title order
            # Title: "[Team A] vs. [Team B]" — Team A is first in slug (team_codes[0])
            first_team_code = team_codes[0]
            second_team_code = team_codes[1]

            # Identify first team in title
            title_parts = (title.replace(".", "")).split(" vs ")
            first_in_title = title_parts[0].strip() if len(title_parts) >= 2 else ""
            first_is_home = home_short in first_in_title or home_l in first_in_title

            home_code = first_team_code if first_is_home else second_team_code
            away_code = second_team_code if first_is_home else first_team_code

            # Construct the 3 BBO slugs
            home_slug  = f"atc-{ev_slug}-{home_code}"
            away_slug  = f"atc-{ev_slug}-{away_code}"
            draw_slug  = f"atc-{ev_slug}-draw"

            home_pin = next((v for k, v in pin.items() if home_short in k.lower()), None) if pin else None
            away_pin = next((v for k, v in pin.items() if away_short in k.lower()), None) if pin else None

            home_tok = _bbo_to_token(c, home_slug, vegas_prob=home_pin)
            away_tok = _bbo_to_token(c, away_slug, vegas_prob=away_pin)
            draw_tok = _bbo_to_token(c, draw_slug)

            result = {}
            if home_tok:
                result["home"] = home_tok
                print(f"[pm_sdk] Soccer home BBO: {home_slug} mid={home_tok[1]:.2f} liquid={home_tok[2]}", flush=True)
            if away_tok:
                result["away"] = away_tok
                print(f"[pm_sdk] Soccer away BBO: {away_slug} mid={away_tok[1]:.2f} liquid={away_tok[2]}", flush=True)
            if draw_tok:
                result["draw"] = draw_tok
                print(f"[pm_sdk] Soccer draw BBO: {draw_slug} mid={draw_tok[1]:.2f} liquid={draw_tok[2]}", flush=True)

            if result:
                return result
    except Exception as e:
        print(f"[pm_sdk] Soccer fallback error: {e}", flush=True)
    return {}


# ── UFC ───────────────────────────────────────────────────────────────────────
def fetch_pm_sdk_ufc(
    fighter_a: str, fighter_b: str,
    pin: Optional[Dict[str, float]] = None,
) -> Dict[str, Tuple[str, float, bool]]:
    """
    Returns {"a": (slug, mid, liquid), "b": (slug, mid, liquid)} or {}.

    Slug: aec-ufc-{f1_abbr}-{f2_abbr}-{YYYY-MM-DD}
    YES = fighter_a wins (first listed).
    Fighter abbrev: first3(first_name) + first3(last_name), lowercase.
    """
    try:
        from polymarket_us import PolymarketUS
        c = PolymarketUS()

        a_abbr = _ufc_abbr(fighter_a)
        b_abbr = _ufc_abbr(fighter_b)

        # Try all upcoming dates (UFC events happen on weekends, check up to +14 days)
        now = datetime.now(timezone.utc)
        date_candidates = [(now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(15)]

        for game_date in date_candidates:
            slug = f"aec-ufc-{a_abbr}-{b_abbr}-{game_date}"
            a_pin = next((v for k, v in pin.items() if k.lower() in fighter_a.lower() or fighter_a.lower() in k.lower()), None) if pin else None
            tok = _bbo_to_token(c, slug, vegas_prob=a_pin)
            if tok:
                a_p = tok[1]
                liquid = tok[2]
                print(f"[pm_sdk] UFC direct slug hit: {slug} a={a_p:.2f} liquid={liquid}", flush=True)
                return {
                    "a": (slug, a_p, liquid),
                    "b": (slug, 1.0 - a_p, liquid),
                }

        # Reversed order fallback (some UFC slugs list fighters differently)
        for game_date in date_candidates:
            slug = f"aec-ufc-{b_abbr}-{a_abbr}-{game_date}"
            tok = _bbo_to_token(c, slug)
            if tok:
                b_p = tok[1]
                liquid = tok[2]
                print(f"[pm_sdk] UFC reversed slug hit: {slug} b={b_p:.2f} liquid={liquid}", flush=True)
                # Reversed: YES = b wins
                return {
                    "b": (slug, b_p, liquid),
                    "a": (slug, 1.0 - b_p, liquid),
                }

        # Search fallback
        resp = c.search.query({"query": f"{fighter_a} {fighter_b} ufc"})
        events = resp if isinstance(resp, list) else resp.get("events", resp.get("results", []))
        for ev in events:
            title = ev.get("title", "").lower()
            fa_l = fighter_a.lower().split()[-1]  # last name
            fb_l = fighter_b.lower().split()[-1]
            if not (fa_l in title and fb_l in title):
                continue
            for m in (ev.get("markets", []) or []):
                if m.get("sportsMarketType") != "ufc_fight_winner":
                    continue
                slug = m.get("slug", "")
                if not slug:
                    continue
                tok = _bbo_to_token(c, slug)
                if tok:
                    parts = title.split(" vs")
                    first_is_a = fa_l in parts[0]
                    if first_is_a:
                        return {"a": tok, "b": (slug, 1.0 - tok[1], tok[2])}
                    else:
                        return {"b": tok, "a": (slug, 1.0 - tok[1], tok[2])}
    except Exception as e:
        print(f"[pm_sdk] UFC fallback error: {e}", flush=True)
    return {}


# ── NBA ───────────────────────────────────────────────────────────────────────
def fetch_pm_sdk_nba(
    home: str, away: str,
    pin: Optional[Dict[str, float]] = None,
) -> Dict[str, Tuple[str, float, bool]]:
    """
    Returns {"home": ..., "away": ...} or {}.
    Slug: aec-nba-{away_abbr}-{home_abbr}-{YYYY-MM-DD}
    YES = away team wins.
    """
    try:
        from polymarket_us import PolymarketUS
        c = PolymarketUS()

        away_abbr = _abbr(away, NBA_ABBR)
        home_abbr = _abbr(home, NBA_ABBR)

        if away_abbr and home_abbr:
            for game_date in _date_candidates():
                slug = f"aec-nba-{away_abbr}-{home_abbr}-{game_date}"
                away_pin = next((v for k, v in pin.items() if away.lower() in k.lower()), None) if pin else None
                tok = _bbo_to_token(c, slug, vegas_prob=away_pin)
                if tok:
                    print(f"[pm_sdk] NBA direct slug hit: {slug} away={tok[1]:.2f} liquid={tok[2]}", flush=True)
                    return {
                        "away": tok,
                        "home": (slug, 1.0 - tok[1], tok[2]),
                    }
    except Exception as e:
        print(f"[pm_sdk] NBA fallback error: {e}", flush=True)
    return {}


# ── NFL ───────────────────────────────────────────────────────────────────────
def fetch_pm_sdk_nfl(
    home: str, away: str,
    pin: Optional[Dict[str, float]] = None,
) -> Dict[str, Tuple[str, float, bool]]:
    """
    Returns {"home": ..., "away": ...} or {}.
    Slug: aec-nfl-{away_abbr}-{home_abbr}-{YYYY-MM-DD}
    YES = away team wins.
    """
    try:
        from polymarket_us import PolymarketUS
        c = PolymarketUS()

        away_abbr = _abbr(away, NFL_ABBR)
        home_abbr = _abbr(home, NFL_ABBR)

        if away_abbr and home_abbr:
            for game_date in _date_candidates():
                slug = f"aec-nfl-{away_abbr}-{home_abbr}-{game_date}"
                away_pin = next((v for k, v in pin.items() if away.lower() in k.lower()), None) if pin else None
                tok = _bbo_to_token(c, slug, vegas_prob=away_pin)
                if tok:
                    print(f"[pm_sdk] NFL direct slug hit: {slug} away={tok[1]:.2f} liquid={tok[2]}", flush=True)
                    return {
                        "away": tok,
                        "home": (slug, 1.0 - tok[1], tok[2]),
                    }
    except Exception as e:
        print(f"[pm_sdk] NFL fallback error: {e}", flush=True)
    return {}


# ── Sports Props Scanner ──────────────────────────────────────────────────────
SOCCER_PROP_STATS = {
    "soccer_player_goals": "Goals",
    "soccer_player_assists": "Assists",
    "soccer_player_goals_plus_assists": "G+A",
    "soccer_player_shots": "Shots",
    "soccer_player_shots_on_target": "SOT",
    "soccer_player_goalkeeper_saves": "Saves",
}

UFC_PROP_STATS = {
    "ufc_method_of_victory": "Method",
    "ufc_round_of_finish": "Round",
}


def fetch_pm_sdk_sport_props(
    home: str, away: str,
    sport: str = "soccer",
    competition: str = "fwc",
    max_workers: int = 20,
    liquid_only: bool = True,
) -> List[Dict]:
    """
    Fetch all player/fight prop markets for a game from PM US SDK.

    Returns list of dicts:
      {player, stat_type, threshold, slug, mid, bid, ask, liquid, spread_pp, question}

    sport: "soccer" | "ufc"
    competition: "fwc" for WC 2026, etc.
    liquid_only: if True, only return markets where lastTradePx is set

    Player names are decoded from the market's "question" field (more reliable than slug IDs).
    """
    from concurrent.futures import ThreadPoolExecutor as _TPE
    try:
        from polymarket_us import PolymarketUS
        c = PolymarketUS()

        # Find the event
        query = f"{home} {away} {competition}" if sport == "soccer" else f"{home} {away} ufc"
        resp = c.search.query({"query": query})
        events = resp if isinstance(resp, list) else resp.get("events", resp.get("results", []))

        ev = None
        home_l, away_l = home.lower(), away.lower()
        for candidate in events:
            t = candidate.get("title", "").lower()
            home_short = home_l.split()[-1]
            away_short = away_l.split()[-1]
            if (home_short in t or home_l in t) and (away_short in t or away_l in t):
                ev = candidate
                break

        if not ev and events:
            ev = events[0]

        if not ev:
            return []

        ev_slug = ev.get("slug", "")
        markets = ev.get("markets", [])

        # Select relevant prop types by sport
        stat_map = SOCCER_PROP_STATS if sport == "soccer" else UFC_PROP_STATS

        prop_markets = []
        for m in markets:
            mt = m.get("sportsMarketType", "")
            if mt not in stat_map:
                continue
            sl = m.get("slug", "")
            question = m.get("question", "")
            prop_markets.append({
                "stat_type": stat_map[mt],
                "slug": sl,
                "question": question,
                "ev_slug": ev_slug,
            })

        if not prop_markets:
            return []

        # Parallel BBO fetch
        def _fetch(pm):
            result = dict(pm)
            result.update({"mid": None, "bid": None, "ask": None, "liquid": False,
                            "spread_pp": None, "player": "", "threshold": ""})
            try:
                bbo = c.markets.bbo(pm["slug"])
                md = bbo.get("marketData", {})
                bid = float((md.get("bestBid") or {}).get("value", 0) or 0)
                ask = float((md.get("bestAsk") or {}).get("value", 1) or 1)
                lt = md.get("lastTradePx")
                if bid > 0 and ask < 1 and ask > bid:
                    result["mid"] = (bid + ask) / 2
                    result["bid"] = bid
                    result["ask"] = ask
                    result["spread_pp"] = round((ask - bid) * 100, 1)
                    result["liquid"] = lt is not None
            except Exception:
                pass
            # Decode player name from question
            q = pm.get("question", "")
            if "Will " in q:
                player_part = q[4:]  # strip "Will "
                # Extract player name up to first action verb
                for verb in [" record", " score", " finish", " have", " get", " make"]:
                    if verb in player_part:
                        result["player"] = player_part[:player_part.index(verb)].strip()
                        break
                else:
                    result["player"] = player_part[:40]
            # Extract threshold from slug (last segment)
            sl_parts = pm["slug"].split("-")
            result["threshold"] = sl_parts[-1] if sl_parts else ""
            return result

        with _TPE(max_workers=max_workers) as ex:
            results = list(ex.map(_fetch, prop_markets))

        if liquid_only:
            results = [r for r in results if r["liquid"] and r["mid"] is not None]

        return sorted(results, key=lambda x: (x["player"], x["stat_type"], x["threshold"]))

    except Exception as e:
        print(f"[pm_sdk] Props scan error: {e}", flush=True)
        return []


def format_pm_props_report(props: List[Dict], home: str, away: str) -> str:
    """Format props list as readable report for Telegram."""
    if not props:
        return f"No liquid prop markets found for {home} vs {away}"

    lines = [f"🎯 <b>PM Props — {home} vs {away}</b>", ""]
    by_player: Dict[str, List] = {}
    for p in props:
        by_player.setdefault(p["player"], []).append(p)

    for player, plist in sorted(by_player.items()):
        lines.append(f"<b>{player}</b>")
        for p in sorted(plist, key=lambda x: (x["stat_type"], x["threshold"])):
            spread_tag = f" spread={p['spread_pp']:.0f}pp" if p["spread_pp"] else ""
            lines.append(f"  {p['stat_type']} {p['threshold']}: YES <b>{p['mid']:.0%}</b>{spread_tag}")
        lines.append("")

    return "\n".join(lines)

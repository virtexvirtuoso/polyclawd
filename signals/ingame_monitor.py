#!/usr/bin/env python3
"""
MLB In-Game Monitor — Polyclawd v1.0

Runs every 5 min via cron during MLB game hours.
For each active Polymarket MLB shadow trade:
  1. Pre-game sweep: at -45min and -15min before first pitch, re-check
     book odds and invalidate if shifted >3%.
  2. Stop-loss: close if current price dropped ≥40% from entry.
  3. Take-profit: close if current price rose ≥67% from entry.
  4. Edge re-calc: close if book_prob - poly_price ≤ 0 (edge inverted).
  5. Discord alert on every close event for manual review.

Shadow trades are never executed for real money. All closes are shadow-only.

Schema additions to shadow_trades (auto-migrated on first run):
    take_profit_price REAL
    stop_loss_price   REAL
    monitor_active    INTEGER DEFAULT 0
    last_checked_at   TEXT
    game_start_time   TEXT
    entry_book_prob   REAL
    close_reason      TEXT
"""

import json
import os
import sqlite3
import time
import urllib.request

import requests
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from loguru import logger
from config.polymarket_urls import GAMMA_API  # polyproxy: central URL config
from config.polymarket_urls import CLOB_API  # polyproxy: central URL config

# ── Load env from /etc/default/polyclawd or .env.discord when running outside systemd ──
_ENV_FILES = ["/etc/default/polyclawd", str(Path(__file__).parent.parent / ".env.discord")]
if not os.environ.get("DISCORD_WEBHOOK_URL"):
    for _ef in _ENV_FILES:
        if os.path.exists(_ef):
            try:
                with open(_ef) as _f:
                    for _line in _f:
                        _line = _line.strip()
                        if _line and not _line.startswith("#") and "=" in _line:
                            _k, _, _v = _line.partition("=")
                            os.environ.setdefault(_k.strip(), _v.strip())
                if os.environ.get("DISCORD_WEBHOOK_URL"):
                    break
            except (PermissionError, OSError):
                continue

# ── Paths ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "storage" / "shadow_trades.db"

# ── Thresholds ─────────────────────────────────────────────────────────────
STOP_LOSS_MULTIPLIER = 0.60       # close if price drops ≥40% from entry
TAKE_PROFIT_MULTIPLIER = 1.67     # close if price rises ≥67% from entry
PREGAME_ODDS_SHIFT_THRESHOLD = 0.03   # 3pp shift = invalidate edge
PREGAME_WINDOW_1_SEC = 45 * 60   # first sweep at -45min
PREGAME_WINDOW_2_SEC = 15 * 60   # second sweep at -15min
CLOB_RATE_DELAY = 2.5            # seconds between CLOB API calls
ODDS_API_RECHECK_INTERVAL = 1800  # re-fetch book odds at most every 30min per trade

# ── APIs ───────────────────────────────────────────────────────────────────

ESPN_API = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# ── MLB Archetypes to monitor ──────────────────────────────────────────────
MLB_ARCHETYPES = {"sports_single_game", "sports_winner"}

# ── Team name lookup (canonical → display) and aliases for market matching ─
MLB_TEAMS: Dict[str, str] = {
    "angels": "Los Angeles Angels", "astros": "Houston Astros",
    "athletics": "Athletics", "blue jays": "Toronto Blue Jays",
    "braves": "Atlanta Braves", "brewers": "Milwaukee Brewers",
    "cardinals": "St. Louis Cardinals", "cubs": "Chicago Cubs",
    "diamondbacks": "Arizona Diamondbacks", "dodgers": "Los Angeles Dodgers",
    "giants": "San Francisco Giants", "guardians": "Cleveland Guardians",
    "mariners": "Seattle Mariners", "marlins": "Miami Marlins",
    "mets": "New York Mets", "nationals": "Washington Nationals",
    "orioles": "Baltimore Orioles", "padres": "San Diego Padres",
    "phillies": "Philadelphia Phillies", "pirates": "Pittsburgh Pirates",
    "rangers": "Texas Rangers", "rays": "Tampa Bay Rays",
    "red sox": "Boston Red Sox", "reds": "Cincinnati Reds",
    "rockies": "Colorado Rockies", "royals": "Kansas City Royals",
    "tigers": "Detroit Tigers", "twins": "Minnesota Twins",
    "white sox": "Chicago White Sox", "yankees": "New York Yankees",
}

# ── DB helpers ─────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def migrate_schema(conn: sqlite3.Connection) -> None:
    """Add monitor columns to shadow_trades if missing."""
    new_cols = [
        ("take_profit_price", "REAL"),
        ("stop_loss_price", "REAL"),
        ("monitor_active", "INTEGER DEFAULT 0"),
        ("last_checked_at", "TEXT"),
        ("game_start_time", "TEXT"),
        ("entry_book_prob", "REAL"),
        ("close_reason", "TEXT"),
    ]
    existing = {row[1] for row in conn.execute("PRAGMA table_info(shadow_trades)")}
    for col, col_type in new_cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE shadow_trades ADD COLUMN {col} {col_type}")
            logger.info(f"migrate: added shadow_trades.{col}")
    conn.commit()

def get_active_mlb_trades(conn: sqlite3.Connection) -> List[Dict]:
    """Return unresolved Polymarket MLB shadow trades (MLB-only by team name match)."""
    archetypes_sql = ",".join(f"'{a}'" for a in MLB_ARCHETYPES)
    rows = conn.execute(f"""
        SELECT * FROM shadow_trades
        WHERE resolved = 0
          AND platform = 'polymarket'
          AND (archetype IN ({archetypes_sql}) OR monitor_active = 1)
        ORDER BY id DESC
    """).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        # Keep already-activated trades (may be from prior run)
        if d.get("monitor_active"):
            result.append(d)
            continue
        # Only activate if market title contains an MLB team name
        if _team_in_title(d.get("market", "")):
            result.append(d)
    return result

# ── CLOB price fetch ───────────────────────────────────────────────────────

def _fetch_json(url: str, timeout: int = 10) -> Optional[dict]:
    try:
        # requests, not urllib: ESPN's edge 403s Python's urllib TLS fingerprint
        r = requests.get(url, headers={"User-Agent": "Polyclawd/2.0"}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"fetch failed {url}: {e}")
        return None

# ── Gamma baseball price cache ────────────────────────────────────────────
# Gamma API ?condition_id= filter is broken (returns wrong markets).
# Instead, pre-fetch all baseball events at startup and build a
# {condition_id: (outcomePrices, clobTokenIds)} map.
_GAMMA_BASEBALL_CACHE: Dict[str, Tuple] = {}
_GAMMA_CACHE_TIME: float = 0
_GAMMA_CACHE_TTL = 10  # seconds

def _refresh_gamma_cache():
    """Fetch all baseball events and build condition_id → price map."""
    global _GAMMA_BASEBALL_CACHE, _GAMMA_CACHE_TIME
    now = time.time()
    if now - _GAMMA_CACHE_TIME < _GAMMA_CACHE_TTL:
        return
    try:
        now_utc = datetime.now(timezone.utc)
        end_min = now_utc.strftime("%Y-%m-%dT00:00:00Z")
        end_max = (now_utc + timedelta(days=2)).strftime("%Y-%m-%dT00:00:00Z")
        req = urllib.request.Request(
            f"{GAMMA_API}/events?closed=false&tag_slug=baseball&limit=100"
            f"&end_date_min={end_min}&end_date_max={end_max}",
            headers={"User-Agent": "Polyclawd/2.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            events = json.loads(resp.read().decode())
        result: Dict[str, Tuple] = {}
        for ev in events:
            for m in ev.get("markets", []):
                cond = m.get("conditionId")
                if not cond:
                    continue
                prices_raw = m.get("outcomePrices", "[]")
                if isinstance(prices_raw, str):
                    prices_raw = json.loads(prices_raw)
                ids_raw = m.get("clobTokenIds", "[]")
                if isinstance(ids_raw, str):
                    ids_raw = json.loads(ids_raw)
                result[cond] = (prices_raw, ids_raw)
        _GAMMA_BASEBALL_CACHE = result
        _GAMMA_CACHE_TIME = time.time()
        logger.debug(f"Gamma cache refreshed: {len(result)} markets")
    except Exception as e:
        logger.warning(f"Gamma cache refresh failed: {e}")

def _gamma_lookup(condition_id: str) -> Optional[Tuple]:
    """Return (outcomePrices, clobTokenIds) from cache, or None."""
    _refresh_gamma_cache()
    return _GAMMA_BASEBALL_CACHE.get(condition_id)

def get_clob_token_ids(condition_id: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (yes_token_id, no_token_id) for a Polymarket condition_id.
    Uses Gamma baseball cache (events endpoint) which returns correct data,
    unlike the broken ?condition_id= filter.
    """
    if not (isinstance(condition_id, str) and condition_id.startswith("0x")):
        return None, None
    
    # Try cache first (all baseball markets from events endpoint)
    cached = _gamma_lookup(condition_id)
    if cached:
        prices_raw, ids_raw = cached
        yes_id = None
        no_id = None
        for i, outcome in enumerate(prices_raw):
            if i < len(ids_raw):
                if i == 0:
                    yes_id = ids_raw[i]
                elif i == 1:
                    no_id = ids_raw[i]
        # If we only have one price, assume first is YES
        if yes_id is None and ids_raw:
            yes_id = ids_raw[0]
        if no_id is None and len(ids_raw) > 1:
            no_id = ids_raw[1]
        return yes_id, no_id

    # Fallback: try Gamma broken filter as last resort
    data = _fetch_json(f"{GAMMA_API}/markets?condition_id={condition_id}")
    if not data or not isinstance(data, list):
        return None, None
    market = data[0]
    ids = market.get("clobTokenIds", "[]")
    if isinstance(ids, str):
        ids = json.loads(ids)
    outcomes = market.get("outcomes", "[]")
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    yes_id = no_id = None
    for i, outcome in enumerate(outcomes):
        if i < len(ids):
            if outcome.lower() == "yes":
                yes_id = ids[i]
            elif outcome.lower() == "no":
                no_id = ids[i]
    if yes_id is None and ids:
        yes_id = ids[0]
    if no_id is None and len(ids) > 1:
        no_id = ids[1]
    return yes_id, no_id

def get_gamma_price(condition_id: str, side: str) -> Optional[float]:
    """Get true price from Gamma events cache for a baseball condition ID.
    Falls back to CLOB mid if not found in cache.
    Returns price in [0.0, 1.0] or None."""
    cached = _gamma_lookup(condition_id)
    if cached:
        prices_raw, _ = cached
        if len(prices_raw) >= 2:
            p = float(prices_raw[0])  # YES price
            if side.upper() == "NO":
                p = 1.0 - p
            logger.debug(f"get_gamma_price: {condition_id[:16]} side={side} -> {p:.4f}")
            return round(p, 4)
    return None

def get_mid_price_for_side(condition_id: str, side: str) -> Optional[float]:
    """
    Fetch current price for YES or NO side, with fallback chain:
      1. Gamma baseball cache (correct prices, always available)
      2. CLOB mid-price (if bid-ask spread is reasonable, <20¢)
      3. None if both fail
    """
    # Priority 1: Gamma cache (always correct for baseball)
    gamma_price = get_gamma_price(condition_id, side)
    if gamma_price is not None:
        return gamma_price

    # Priority 2: CLOB mid if spread is tight
    yes_token, no_token = get_clob_token_ids(condition_id)
    token_id = yes_token if side.upper() == "YES" else no_token
    if not token_id:
        token_id = yes_token
        invert = side.upper() == "NO"
    else:
        invert = False

    if not token_id:
        logger.debug(f"get_mid_price_for_side: no token_id for {condition_id}/{side}")
        return None

    data = _fetch_json(f"{CLOB_API}/book?token_id={token_id}")
    if not data:
        return None

    bids = sorted(
        [float(b["price"]) for b in data.get("bids", [])], reverse=True
    )
    asks = sorted(
        [float(a["price"]) for a in data.get("asks", [])]
    )
    best_bid = bids[0] if bids else 0.0
    best_ask = asks[0] if asks else 1.0
    spread = best_ask - best_bid

    if spread > 0.20:
        logger.debug(f"get_mid_price_for_side: CLOB spread {spread:.2f} too wide, returning None")
        return None

    mid = (best_bid + best_ask) / 2 if bids and asks else None
    if mid is None:
        return None
    return round(1.0 - mid if invert else mid, 4)

# ── ESPN game state ────────────────────────────────────────────────────────

def get_espn_games() -> List[Dict]:
    """
    Fetch live MLB scoreboard from ESPN.
    Returns list of simplified game dicts:
      {home_team, away_team, status, home_score, away_score, start_time}
    status: 'pre', 'in', 'final'
    """
    data = _fetch_json(ESPN_API)
    if not data:
        return []

    games = []
    for event in data.get("events", []):
        for comp in event.get("competitions", []):
            status_type = comp.get("status", {}).get("type", {})
            raw_status = status_type.get("name", "").lower()
            if "final" in raw_status:
                status = "final"
            elif "progress" in raw_status or "in_progress" in raw_status:
                status = "in"
            else:
                status = "pre"

            home = away = ""
            home_score = away_score = 0
            for c in comp.get("competitors", []):
                name = c.get("team", {}).get("displayName", "").lower()
                score = int(c.get("score", 0) or 0)
                if c.get("homeAway") == "home":
                    home = name
                    home_score = score
                else:
                    away = name
                    away_score = score

            games.append({
                "home_team": home,
                "away_team": away,
                "home_score": home_score,
                "away_score": away_score,
                "status": status,
                "start_time": comp.get("startDate", ""),
            })
    return games

# Non-MLB league prefixes — markets from these leagues can collide with MLB
# team aliases (e.g. "KBO: LG Twins vs. Kia Tigers" matched Detroit Tigers,
# shadow trade id=136, 2026-06-08). Never treat these as MLB.
NON_MLB_LEAGUES = ("kbo", "npb", "cpbl", "lidom", "lmb", "kbl", "ncaa")


# Non-MLB league prefixes — markets from these leagues can collide with MLB
# team aliases (e.g. "KBO: LG Twins vs. Kia Tigers" matched Detroit Tigers,
# shadow trade id=136, 2026-06-08). Never treat these as MLB.
NON_MLB_LEAGUES = ("kbo", "npb", "cpbl", "lidom", "lmb", "kbl", "ncaa")


def _team_in_title(title: str) -> Optional[str]:
    """Extract MLB team keyword from market title. Returns lowercase alias or None."""
    t = title.lower()
    if any(t.startswith(f"{lg}:") or f" {lg}:" in t or f"({lg})" in t
           for lg in NON_MLB_LEAGUES):
        return None
    for alias in sorted(MLB_TEAMS, key=len, reverse=True):  # longest first
        if alias in t:
            return alias
    return None

def match_trade_to_game(
    trade: Dict, espn_games: List[Dict]
) -> Optional[Dict]:
    """Match a shadow trade to an ESPN game by team name in market title."""
    alias = _team_in_title(trade.get("market", ""))
    if not alias:
        return None
    for game in espn_games:
        if alias in game["home_team"] or alias in game["away_team"]:
            return game
    return None

# ── Book odds helpers ──────────────────────────────────────────────────────

def _american_to_prob(odds: float) -> float:
    """Convert American odds to implied probability."""
    if odds < 0:
        return (-odds) / (-odds + 100)
    return 100 / (odds + 100)

def get_book_prob_for_trade(trade: Dict, odds_games: List[Dict]) -> Optional[float]:
    """
    Look up current book implied probability for the trade's team.
    Uses The Odds API h2h data. Returns devigged probability or None.
    """
    alias = _team_in_title(trade.get("market", ""))
    if not alias:
        return None
    canonical = MLB_TEAMS.get(alias, alias).lower()

    for game in odds_games:
        home = game.get("home_team", "").lower()
        away = game.get("away_team", "").lower()
        if alias not in home and alias not in away and canonical not in home and canonical not in away:
            continue

        # Find h2h market
        for bookmaker in game.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                outcomes = market.get("outcomes", [])
                if len(outcomes) < 2:
                    continue
                probs = [_american_to_prob(float(o.get("price", -110))) for o in outcomes]
                total = sum(probs)
                if total == 0:
                    continue
                # Devig
                devigged = [p / total for p in probs]
                # Match our team
                for i, o in enumerate(outcomes):
                    if alias in o.get("name", "").lower() or canonical in o.get("name", "").lower():
                        return round(devigged[i], 4)
    return None

# ── Trade lifecycle ────────────────────────────────────────────────────────

def activate_trade(
    conn: sqlite3.Connection,
    trade: Dict,
    game_start_iso: str,
    entry_book_prob: Optional[float],
) -> None:
    """Set monitor fields on a newly detected trade."""
    entry = float(trade.get("entry_price") or 0.5)
    tp = round(entry * TAKE_PROFIT_MULTIPLIER, 4)
    sl = round(entry * STOP_LOSS_MULTIPLIER, 4)

    conn.execute("""
        UPDATE shadow_trades
        SET monitor_active = 1,
            take_profit_price = ?,
            stop_loss_price = ?,
            game_start_time = ?,
            entry_book_prob = ?,
            last_checked_at = ?
        WHERE id = ?
    """, (
        tp, sl,
        game_start_iso,
        entry_book_prob,
        datetime.now(timezone.utc).isoformat(),
        trade["id"],
    ))
    conn.commit()
    logger.info(
        f"[ACTIVATED] id={trade['id']} market={trade.get('market','?')[:60]} "
        f"side={trade.get('side')} entry={entry:.3f} "
        f"tp={tp:.3f} sl={sl:.3f}"
    )

def close_trade(
    conn: sqlite3.Connection,
    trade: Dict,
    reason: str,
    exit_price: float,
) -> None:
    """Mark shadow trade as resolved with exit price and reason."""
    entry = float(trade.get("entry_price") or 0)
    side = trade.get("side", "YES")
    if side.upper() == "YES":
        pnl = round(exit_price - entry, 4)
    else:
        pnl = round(exit_price - entry, 4)

    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        UPDATE shadow_trades
        SET resolved = 1,
            resolved_at = ?,
            exit_price = ?,
            pnl = ?,
            close_reason = ?,
            monitor_active = 0,
            last_checked_at = ?
        WHERE id = ?
    """, (now, exit_price, pnl, reason, now, trade["id"]))
    conn.commit()
    logger.info(
        f"[CLOSED] id={trade['id']} reason={reason} "
        f"entry={entry:.3f} exit={exit_price:.3f} pnl={pnl:+.4f}"
    )

def mark_checked(conn: sqlite3.Connection, trade_id: int) -> None:
    conn.execute(
        "UPDATE shadow_trades SET last_checked_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), trade_id),
    )
    conn.commit()

# ── Decision logic ─────────────────────────────────────────────────────────

def check_pregame_sweep(
    trade: Dict,
    current_book_prob: Optional[float],
    espn_game: Optional[Dict],
) -> Optional[str]:
    """
    Return 'pregame_invalidated' if we're in a pre-game window and
    book prob has shifted >3pp from entry. Returns None otherwise.
    """
    if espn_game and espn_game.get("status") in ("in", "final"):
        return None  # game already started, no pre-game sweep

    game_start = trade.get("game_start_time")
    if not game_start:
        return None

    try:
        start_dt = datetime.fromisoformat(game_start.replace("Z", "+00:00"))
    except ValueError:
        return None

    now_utc = datetime.now(timezone.utc)
    secs_to_start = (start_dt - now_utc).total_seconds()

    in_window_1 = PREGAME_WINDOW_2_SEC <= secs_to_start <= PREGAME_WINDOW_1_SEC
    in_window_2 = 0 <= secs_to_start < PREGAME_WINDOW_2_SEC
    if not (in_window_1 or in_window_2):
        return None

    entry_book_prob = trade.get("entry_book_prob")
    if entry_book_prob is None or current_book_prob is None:
        return None

    shift = abs(current_book_prob - float(entry_book_prob))
    if shift > PREGAME_ODDS_SHIFT_THRESHOLD:
        logger.info(
            f"[PREGAME_SWEEP] id={trade['id']} "
            f"entry_book={entry_book_prob:.3f} current_book={current_book_prob:.3f} "
            f"shift={shift:.3f} → invalidating"
        )
        return "pregame_invalidated"

    return None

def check_stop_loss(trade: Dict, current_price: float) -> bool:
    sl = trade.get("stop_loss_price")
    if sl is None:
        sl = float(trade.get("entry_price", 0)) * STOP_LOSS_MULTIPLIER
    return current_price <= float(sl)

def check_take_profit(trade: Dict, current_price: float) -> bool:
    tp = trade.get("take_profit_price")
    if tp is None:
        tp = float(trade.get("entry_price", 1)) * TAKE_PROFIT_MULTIPLIER
    return current_price >= float(tp)

def check_edge_inverted(
    trade: Dict, current_price: float, current_book_prob: Optional[float]
) -> bool:
    """Return True if book_prob - poly_price <= 0 (edge gone or inverted)."""
    if current_book_prob is None:
        return False
    side = trade.get("side", "YES")
    if side.upper() == "NO":
        # For NO: edge inverted if poly NO price > book NO prob
        # book NO prob = 1 - book YES prob
        book_no_prob = 1.0 - current_book_prob
        return current_price >= book_no_prob
    return current_book_prob - current_price <= 0

# ── Discord alert ──────────────────────────────────────────────────────────

_REASON_EMOJI = {
    "stop_loss": "🛑",
    "take_profit": "✅",
    "edge_inverted": "↩️",
    "pregame_invalidated": "⚠️",
    "game_final": "🏁",
}

def send_discord_alert(trade: Dict, reason: str, exit_price: float) -> None:
    if not DISCORD_WEBHOOK_URL:
        logger.debug("Discord webhook not set, skipping alert")
        return

    entry = float(trade.get("entry_price") or 0)
    pnl = exit_price - entry
    pct = (pnl / entry * 100) if entry else 0
    emoji = _REASON_EMOJI.get(reason, "📊")
    color = 0x00D68F if pnl >= 0 else 0xF65164

    market = trade.get("market", "Unknown")[:80]
    side = trade.get("side", "?")

    embed = {
        "title": f"{emoji} Shadow Close — {reason.replace('_', ' ').title()}",
        "color": color,
        "fields": [
            {"name": "Market", "value": market, "inline": False},
            {"name": "Side", "value": side, "inline": True},
            {"name": "Entry", "value": f"{entry:.3f}", "inline": True},
            {"name": "Exit", "value": f"{exit_price:.3f}", "inline": True},
            {"name": "P&L", "value": f"{pnl:+.4f} ({pct:+.1f}%)", "inline": True},
            {"name": "Platform", "value": trade.get("platform", "polymarket"), "inline": True},
            {"name": "Shadow ID", "value": str(trade["id"]), "inline": True},
        ],
        "footer": {"text": "Polyclawd In-Game Monitor — shadow only, no real money"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    content_line = f"{emoji} **{reason.replace('_', ' ').title()}**: {market[:60]} | Entry {entry:.3f} → Exit {exit_price:.3f} ({pnl:+.4f})"

    payload = json.dumps({
        "content": content_line,
        "username": "Polyclawd Monitor",
        "avatar_url": "https://virtuosocrypto.com/polyclawd/icons/icon-192.png",
        "embeds": [embed],
    }).encode()

    try:
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "Polyclawd/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status not in (200, 204):
                logger.warning(f"Discord alert HTTP {resp.status}")
    except Exception as e:
        logger.warning(f"Discord alert failed: {e}")

# ── Main loop ──────────────────────────────────────────────────────────────

def run_monitor() -> None:
    logger.info("=== ingame_monitor start ===")

    conn = get_db()
    migrate_schema(conn)

    trades = get_active_mlb_trades(conn)
    if not trades:
        logger.info("No active MLB Polymarket shadow trades. Done.")
        return

    logger.info(f"Found {len(trades)} active MLB trades to check")

    # Fetch ESPN game state once
    espn_games = get_espn_games()
    logger.info(f"ESPN: {len(espn_games)} games fetched")

    # Fetch Odds API data once (rate-guarded)
    odds_games: List[Dict] = []
    try:
        import sys
        sys.path.insert(0, str(BASE_DIR))
        from odds.the_odds_api import get_baseball_games_with_odds
        from odds.rate_limiter import can_make_call, record_call
        import asyncio

        can, reason = can_make_call("high")
        if can:
            odds_games = asyncio.run(get_baseball_games_with_odds())
            record_call(1, "ingame_monitor")
            logger.info(f"Odds API: {len(odds_games)} MLB games")
        else:
            logger.info(f"Odds API skipped: {reason}")
    except Exception as e:
        logger.warning(f"Odds API fetch failed: {e}")

    # Process each trade
    for trade in trades:
        trade_id = trade["id"]
        market = trade.get("market", "?")[:60]
        side = trade.get("side", "YES")
        entry_price = float(trade.get("entry_price") or 0)

        if not entry_price:
            logger.debug(f"id={trade_id}: no entry_price, skipping")
            continue

        # Match to ESPN game
        espn_game = match_trade_to_game(trade, espn_games)

        # Skip final games (should be picked up by baseball_resolver)
        if espn_game and espn_game.get("status") == "final":
            logger.debug(f"id={trade_id}: game final, skipping (baseball_resolver handles)")
            mark_checked(conn, trade_id)
            continue

        # Activate if not yet active
        if not trade.get("monitor_active"):
            game_start_iso = ""
            if espn_game:
                game_start_iso = espn_game.get("start_time", "")
            entry_book_prob = get_book_prob_for_trade(trade, odds_games) if odds_games else None
            activate_trade(conn, trade, game_start_iso, entry_book_prob)
            # Re-fetch trade with updated fields
            row = conn.execute(
                "SELECT * FROM shadow_trades WHERE id = ?", (trade_id,)
            ).fetchone()
            if row:
                trade = dict(row)

        # Fetch current CLOB price (rate-limited)
        time.sleep(CLOB_RATE_DELAY)
        current_price = get_mid_price_for_side(trade["market_id"], side)
        if current_price is None:
            logger.warning(f"id={trade_id}: could not fetch CLOB price, skipping")
            mark_checked(conn, trade_id)
            continue

        logger.info(
            f"id={trade_id} {market} side={side} "
            f"entry={entry_price:.3f} current={current_price:.3f}"
        )

        # Current book prob (from this run's odds fetch)
        current_book_prob = get_book_prob_for_trade(trade, odds_games) if odds_games else None

        # ── Trigger checks (order matters: pregame → stop-loss → take-profit → edge) ──
        close_reason = None
        exit_price = current_price

        # 1. Pre-game sweep
        close_reason = check_pregame_sweep(trade, current_book_prob, espn_game)

        # 2. Stop-loss
        if close_reason is None and check_stop_loss(trade, current_price):
            close_reason = "stop_loss"

        # 3. Take-profit
        if close_reason is None and check_take_profit(trade, current_price):
            close_reason = "take_profit"

        # 4. Edge inverted
        if close_reason is None and check_edge_inverted(trade, current_price, current_book_prob):
            close_reason = "edge_inverted"

        if close_reason:
            close_trade(conn, trade, close_reason, exit_price)
            send_discord_alert(trade, close_reason, exit_price)
        else:
            mark_checked(conn, trade_id)
            logger.debug(f"id={trade_id}: no trigger, continuing to monitor")

    logger.info("=== ingame_monitor done ===")

if __name__ == "__main__":
    run_monitor()

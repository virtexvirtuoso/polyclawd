#!/usr/bin/env python3
"""
mlb_live_monitor.py — Live-game alert monitor for MLB games.

Fires 3 alert types every 5 min during active game windows:
  1. RUN TRIGGER   — ESPN score change → fresh Pinnacle odds + PM CLOB comparison
  2. LINE DRIFT    — Pinnacle ML moves > LINE_DRIFT_PP since last snapshot
  3. EDGE INVERSION — open shadow trade → close + alert if edge gone

NOTE: Does NOT handle stop-loss/take-profit — that belongs to ingame_monitor.py.
      This is alert-only, informational, same pattern as soccer_live_monitor.py.

Cron: */5 * * * * (filter to game hours in deployment)
State: storage/shadow_trades.db (auto-migrated tables)

FIELD FRESHNESS CONVENTION (2026-08-23):
  Every field in an alert is either LIVE (changes during the game) or STATIC
  (fixed pre-game). A STATIC field must NEVER be presented as live in-game
  state. Rules:
    - LIVE fields (score, on-mound pitcher, Vegas line, PM price) must come
      from a source that updates in real time (ESPN situation.pitcher, Odds
      API in-play odds, PM CLOB BBO).
    - STATIC fields (probables/starters, lineups, pre-game odds) must be
      labeled as such ("Probable starters") or overridden by a live source
      once the game is in progress.
    - When adding a new alert field, ask: "does this change during the
      game?" If yes, it must be fetched live, not from a pre-game snapshot.
  Origin: 2026-08-23 on-mound bug — alert showed pre-game starters (Rodon/
  Soriano) in the 6th inning. Fixed by reading ESPN situation.pitcher.
"""

from __future__ import annotations
from config.polymarket_urls import clob_url, gamma_url  # polyproxy: central URL config

import json
import os
import socket
import sqlite3
import sys
import time
import urllib.parse

import requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))
from odds.monitor_gate import gated_fetch_json, LIVE_BOOKS

from scripts.alert_formatter import format_grid, send_telegram
from signals.alert_dispatch import TIER_CRITICAL, TIER_DIGEST, dispatch
from signals.alert_governor import Leg, govern, purge_stale

# ── Config ────────────────────────────────────────────────────────────────────
ESPN_MLB       = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"
POLY_EVENTS    = gamma_url("/events")
ODDS_API_BASE  = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/"
CLOB_BOOK      = clob_url("/book")

ODDS_API_KEY   = os.environ.get("ODDS_API_KEY", "")
LINE_DRIFT_PP  = 8.0     # pp shift to fire drift alert (MLB swings more than soccer)
BLOWOUT_RUN_DIFF = 5     # if lead >= this, suppress drift alert (game effectively over)
BLOWOUT_LATE_INNING = 7  # in 7th+ inning, lower blowout threshold to 4 runs
BLOWOUT_LATE_DIFF = 4
WHALE_SIZE     = 30000   # CLOB book wall threshold (lower liquidity than WC)
WHALE_DEDUP_S  = 1800    # suppress re-alert for same wall within 30 min
EDGE_FLOOR     = 0.02    # close shadow trade if edge drops below 2pp
PM_GAP_PP      = 15.0    # min pp gap between PM and Vegas to flag in alerts (raised 6→15 2026-08-23: 6-15pp is PM-lag/spread noise, not a confirmed edge; real signals were 16-19pp)
PM_STALE_PP    = 35.0    # gap above this = Endgame MM not active / no live in-game liquidity
# Vegas-tier EV filter (2026-08-23, from edge_calibration): the BUY-side edge is
# regime-dependent. Buying cheap underdogs (<40% Vegas) = +15pp EV; buying
# favorites (>60%) = +9.3pp EV; the middle (40-60%) is NEGATIVE EV (-6 to -10pp).
# Only fire signals in the +EV tiers. VEGAS_UNDERDOG_MAX / VEGAS_FAVORITE_MIN are
# the Vegas devig probability bounds that define the +EV regimes.
VEGAS_UNDERDOG_MAX = 0.40   # Vegas prob below this = +EV underdog buy
VEGAS_FAVORITE_MIN = 0.60   # Vegas prob above this = +EV favorite buy


def _in_positive_ev_tier(vegas_prob: float) -> bool:
    """True if a Vegas devig prob is in a +EV regime (underdog <40% or favorite >60%).
    The 40-60% middle is negative EV per edge_calibration and is suppressed."""
    return vegas_prob < VEGAS_UNDERDOG_MAX or vegas_prob > VEGAS_FAVORITE_MIN
RUN_ALERT_COOLDOWN_MIN = 45  # per-game run-trigger TG cooldown while edge signature unchanged (92 sends/day on 2026-07-05 without it)

DB_PATH  = BASE_DIR / "storage" / "shadow_trades.db"
MC_HOST, MC_PORT = "localhost", 11211

_EXECUTOR = ThreadPoolExecutor(max_workers=6)

# MLB team name aliases — maps any form → list of equivalent forms (all lowercase)
# Covers both ESPN full names and PM short names so _nmatch works in both directions
ALIASES: Dict[str, List[str]] = {
    "athletics": ["oakland athletics", "a's", "oakland a's", "athletics"],
    "oakland athletics": ["athletics", "a's", "oakland a's"],
    "white sox": ["chicago white sox", "white sox"],
    "chicago white sox": ["white sox"],
    "red sox": ["boston red sox", "red sox"],
    "boston red sox": ["red sox"],
    "blue jays": ["toronto blue jays", "blue jays"],
    "toronto blue jays": ["blue jays"],
    "cubs": ["chicago cubs", "cubs"],
    "chicago cubs": ["cubs"],
    "yankees": ["new york yankees", "yankees"],
    "new york yankees": ["yankees"],
    "mets": ["new york mets", "mets"],
    "new york mets": ["mets"],
    "dodgers": ["los angeles dodgers", "dodgers"],
    "los angeles dodgers": ["dodgers"],
    "angels": ["los angeles angels", "angels"],
    "los angeles angels": ["angels"],
    "padres": ["san diego padres", "padres"],
    "san diego padres": ["padres"],
    "giants": ["san francisco giants", "giants"],
    "san francisco giants": ["giants"],
    "braves": ["atlanta braves", "braves"],
    "atlanta braves": ["braves"],
    "marlins": ["miami marlins", "marlins"],
    "miami marlins": ["marlins"],
    "phillies": ["philadelphia phillies", "phillies"],
    "philadelphia phillies": ["phillies"],
    "nationals": ["washington nationals", "nationals"],
    "washington nationals": ["nationals"],
    "cardinals": ["st. louis cardinals", "cardinals"],
    "st. louis cardinals": ["cardinals"],
    "brewers": ["milwaukee brewers", "brewers"],
    "milwaukee brewers": ["brewers"],
    "pirates": ["pittsburgh pirates", "pirates"],
    "pittsburgh pirates": ["pirates"],
    "reds": ["cincinnati reds", "reds"],
    "cincinnati reds": ["reds"],
    "rockies": ["colorado rockies", "rockies"],
    "colorado rockies": ["rockies"],
    "diamondbacks": ["arizona diamondbacks", "d-backs", "diamondbacks"],
    "arizona diamondbacks": ["diamondbacks", "d-backs"],
    "guardians": ["cleveland guardians", "guardians"],
    "cleveland guardians": ["guardians"],
    "tigers": ["detroit tigers", "tigers"],
    "detroit tigers": ["tigers"],
    "astros": ["houston astros", "astros"],
    "houston astros": ["astros"],
    "royals": ["kansas city royals", "royals"],
    "kansas city royals": ["royals"],
    "twins": ["minnesota twins", "twins"],
    "minnesota twins": ["twins"],
    "orioles": ["baltimore orioles", "orioles"],
    "baltimore orioles": ["orioles"],
    "rays": ["tampa bay rays", "rays"],
    "tampa bay rays": ["rays"],
    "rangers": ["texas rangers", "rangers"],
    "texas rangers": ["rangers"],
    "mariners": ["seattle mariners", "mariners"],
    "seattle mariners": ["mariners"],
}


# ── DB ────────────────────────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mlb_score_snap (
            game_id       TEXT PRIMARY KEY,
            home_team     TEXT,
            away_team     TEXT,
            home_score    INTEGER DEFAULT 0,
            away_score    INTEGER DEFAULT 0,
            home_pitcher  TEXT DEFAULT '',
            away_pitcher  TEXT DEFAULT '',
            ts            TEXT
        );
        CREATE TABLE IF NOT EXISTS mlb_line_snap (
            game_id      TEXT,
            outcome      TEXT,
            devig        REAL,
            ts           TEXT,
            game_status  TEXT DEFAULT 'pre',
            PRIMARY KEY (game_id, outcome)
        );
        CREATE TABLE IF NOT EXISTS mlb_whale_dedup (
            game_id       TEXT,
            outcome       TEXT,
            last_alert_ts TEXT,
            PRIMARY KEY (game_id, outcome)
        );
        CREATE TABLE IF NOT EXISTS mlb_run_alert_state (
            game_id        TEXT PRIMARY KEY,
            last_sent_ts   TEXT,
            last_signature TEXT
        );
        -- Append-only log of fired ODDS MOVED alerts (audit 2026-07-07: 1,270
        -- msgs/22d were Telegram-only; the PM-vs-Vegas divergence data was
        -- discarded after delivery, so the claimed edge could never be scored).
        CREATE TABLE IF NOT EXISTS mlb_odds_moved_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fired_at    TEXT NOT NULL,
            game_id     TEXT,
            home_team   TEXT,
            away_team   TEXT,
            home_score  INTEGER,
            away_score  INTEGER,
            detail      TEXT,
            outcome     TEXT,
            prev_devig  REAL,
            now_devig   REAL,
            move_pp     REAL,
            poly_price  REAL,
            gap_pp      REAL,
            trade_signal TEXT     -- BUY/SELL when PM lags Vegas by >= PM_GAP_PP (non-stale), else NULL
        );
    """)
    conn.commit()
    # Add game_status column if missing (for existing installs)
    try:
        conn.execute("ALTER TABLE mlb_line_snap ADD COLUMN game_status TEXT DEFAULT 'pre'")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists


# ── HTTP ──────────────────────────────────────────────────────────────────────
def _get(url: str, params: Optional[dict] = None, timeout: int = 12) -> Optional[object]:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    try:
        # requests, not urllib: ESPN's edge 403s Python's urllib TLS fingerprint
        # (silent outage Aug 4-16 2026; urllib3's handshake passes)
        r = requests.get(url, headers={"User-Agent": "polyclawd/1.0"}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[mlb_monitor] GET {url[:70]} → {e}", flush=True)
        return None


# ── Memcached ─────────────────────────────────────────────────────────────────
def _mc_get(key: str, timeout: float = 0.4) -> Optional[bytes]:
    try:
        s = socket.create_connection((MC_HOST, MC_PORT), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(b"get " + key.encode() + b"\r\n")
        buf = b""
        while b"END\r\n" not in buf and len(buf) < 500_000:
            chunk = s.recv(8192)
            if not chunk:
                break
            buf += chunk
        try:
            s.close()
        except Exception:
            pass
        if not buf.startswith(b"VALUE"):
            return None
        _, _, rest = buf.partition(b"\r\n")
        return rest.split(b"\r\nEND\r\n", 1)[0]
    except Exception:
        return None


def _mc_set(key: str, value: str, exptime: int = 600, timeout: float = 0.4) -> None:
    try:
        payload = value.encode()
        cmd = f"set {key} 0 {exptime} {len(payload)}\r\n".encode() + payload + b"\r\n"
        s = socket.create_connection((MC_HOST, MC_PORT), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(cmd)
        s.recv(64)
        s.close()
    except Exception:
        pass


def mc_register_tokens(token_ids: List[str]) -> None:
    if not token_ids:
        return
    raw = _mc_get("poly:ws:registered")
    cur: set = set()
    if raw:
        try:
            cur = set(json.loads(raw))
        except Exception:
            pass
    cur.update(token_ids)
    _mc_set("poly:ws:registered", json.dumps(list(cur)[:500]), exptime=600)


# ── Name matching ─────────────────────────────────────────────────────────────
def _nmatch(a: str, b: str) -> bool:
    a, b = a.lower().strip(), b.lower().strip()
    if a == b or a in b or b in a:
        return True
    for alias in ALIASES.get(a, []):
        if alias in b or b in alias:
            return True
    return False


# ── ESPN ──────────────────────────────────────────────────────────────────────
ESPN_STALE_ALERT_AFTER = 6 * 3600   # page if no successful scoreboard fetch for 6h
ESPN_ALERT_COOLDOWN    = 24 * 3600  # at most one page per day


def _espn_health(ok: bool) -> None:
    """Staleness alarm for the scoreboard feed. State lives in the DB, not
    memory — scheduler restarts must not reset the clock. Born from the
    Aug 4-16 2026 outage: ESPN 403'd urllib for 12 days and every poll
    printed a routine "No active games" with nobody paged."""
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=15)
        conn.execute("CREATE TABLE IF NOT EXISTS espn_fetch_health ("
                     "source TEXT PRIMARY KEY, last_ok_ts INTEGER, last_alert_ts INTEGER)")
        now = int(time.time())
        if ok:
            conn.execute(
                "INSERT INTO espn_fetch_health (source, last_ok_ts, last_alert_ts) "
                "VALUES ('mlb_scoreboard', ?, 0) "
                "ON CONFLICT(source) DO UPDATE SET last_ok_ts=excluded.last_ok_ts", (now,))
        else:
            row = conn.execute("SELECT last_ok_ts, last_alert_ts FROM espn_fetch_health "
                               "WHERE source='mlb_scoreboard'").fetchone()
            if row is None:
                conn.execute("INSERT INTO espn_fetch_health VALUES ('mlb_scoreboard', ?, 0)", (now,))
            elif now - row[0] > ESPN_STALE_ALERT_AFTER and now - row[1] > ESPN_ALERT_COOLDOWN:
                hours = (now - row[0]) // 3600
                dispatch("mlb_scanner_health",
                         f"🚨 MLB monitor blind: no successful ESPN scoreboard fetch for {hours}h — "
                         f"in-game alerts (direct + shadow) are NOT flowing.", TIER_CRITICAL)
                conn.execute("UPDATE espn_fetch_health SET last_alert_ts=? "
                             "WHERE source='mlb_scoreboard'", (now,))
        conn.commit()
        conn.close()
    except Exception as ex:  # noqa: BLE001 — health check must never block the poll
        print(f"[mlb_monitor] espn health check failed: {ex}", flush=True)


def fetch_espn_games() -> List[Dict]:
    data = _get(ESPN_MLB)
    _espn_health(data is not None)
    if not data:
        return []
    games = []
    for event in data.get("events", []):
        for comp in event.get("competitions", []):
            status_type = comp.get("status", {}).get("type", {})
            state = status_type.get("state", "").lower()
            completed = status_type.get("completed", False)
            raw = status_type.get("name", "").lower()

            if completed or "final" in raw or "end" in raw:
                status = "final"
            elif state == "in" or "progress" in raw or "inning" in raw or "top" in raw or "bottom" in raw or "middle" in raw:
                status = "in"
            else:
                status = "pre"

            home = away = ""
            hs = as_ = 0
            home_pitcher = away_pitcher = ""

            # Map team.id -> (homeAway, displayName) so we can attribute the
            # live on-mound pitcher (situation.pitcher) to the right side.
            team_id_to_side = {}
            for c in comp.get("competitors", []):
                tid = c.get("team", {}).get("id")
                if tid:
                    team_id_to_side[str(tid)] = c.get("homeAway")

            for c in comp.get("competitors", []):
                name = c.get("team", {}).get("displayName", "")
                score = int(c.get("score", 0) or 0)
                # Pitcher from probables list (pre-game starters)
                pitcher = ""
                for p in c.get("probables", []):
                    pitcher = p.get("athlete", {}).get("displayName", "")
                    break

                if c.get("homeAway") == "home":
                    home, hs, home_pitcher = name, score, pitcher
                else:
                    away, as_, away_pitcher = name, score, pitcher

            # In-progress: the probables list is the PRE-GAME starters and never
            # updates. Override with the LIVE on-mound pitcher from
            # situation.pitcher so the alert shows who is actually pitching now.
            if status == "in":
                sit = comp.get("situation", {}) or {}
                on_mound = (sit.get("pitcher") or {}).get("athlete") or {}
                on_mound_name = on_mound.get("displayName", "")
                on_mound_team = str((on_mound.get("team") or {}).get("id", ""))
                if on_mound_name and on_mound_team in team_id_to_side:
                    side = team_id_to_side[on_mound_team]
                    if side == "home":
                        home_pitcher = on_mound_name
                    else:
                        away_pitcher = on_mound_name

            if home and away:
                gid = f"{home.lower().replace(' ', '_')}_{away.lower().replace(' ', '_')}"
                detail = status_type.get("detail", "")
                games.append({
                    "game_id": gid,
                    "home_team": home, "away_team": away,
                    "home_score": hs, "away_score": as_,
                    "home_pitcher": home_pitcher, "away_pitcher": away_pitcher,
                    "status": status, "detail": detail,
                })
    return games


# ── Pinnacle ──────────────────────────────────────────────────────────────────
def fetch_pinnacle(home: str, away: str) -> Optional[Dict[str, float]]:
    """Returns {team_name: devigged_prob} — consensus of live-updated books.

    Uses ALL books with staleness filter (>10min behind freshest = excluded).
    Prevents pre-game snapshot bug where one book freezes during in-play.
    """
    if not ODDS_API_KEY:
        return None
    data = gated_fetch_json(ODDS_API_BASE, {
        "apiKey": ODDS_API_KEY, "bookmakers": LIVE_BOOKS,
        "markets": "h2h", "oddsFormat": "decimal",
    })
    if not data:
        return None

    for game in data:
        if not (_nmatch(game.get("home_team", ""), home) and
                _nmatch(game.get("away_team", ""), away)):
            continue

        book_probs = []
        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt["key"] != "h2h":
                    continue
                valid = [o for o in mkt["outcomes"] if o.get("price", 0) and o["price"] > 1.0]
                # COMPLETE outcome set required (2026-08-21). `len(valid) < 2`
                # let a 3-way soccer market be devigged from only 2 outcomes:
                # the pair then normalises to 1.0, inflating BOTH by the missing
                # outcome's share (~25pp on soccer) — 4x the 6pp trigger. The
                # old `total < 0.5` guard does not catch it: 2 of 3 outcomes
                # sums to ~0.78 with vig and passes.
                if len(valid) < 2 or len(valid) != len(mkt["outcomes"]):
                    continue
                raw = {o["name"]: 1.0 / o["price"] for o in valid}
                total = sum(raw.values())
                # Raw implied probabilities must sum to 1 + vig. Outside a sane
                # band the book's prices are malformed, stale or incomplete.
                if not (0.95 <= total <= 1.50):
                    continue
                devigged = {k: v / total for k, v in raw.items()}
                upd = mkt.get("last_update", bm.get("last_update", ""))
                book_probs.append((upd, devigged, bm["title"]))

        if not book_probs:
            return None

        book_probs.sort(key=lambda x: x[0], reverse=True)
        from datetime import timedelta
        try:
            fresh_dt = datetime.fromisoformat(book_probs[0][0].replace("Z", "+00:00"))
            cutoff = fresh_dt - timedelta(minutes=10)
            live_books = [
                (ts, probs, name) for ts, probs, name in book_probs
                if datetime.fromisoformat(ts.replace("Z", "+00:00")) >= cutoff
            ]
        except (ValueError, TypeError):
            live_books = book_probs

        if not live_books:
            live_books = book_probs[:1]

        all_outcomes = set()
        for _, probs, _ in live_books:
            all_outcomes.update(probs.keys())

        consensus = {}
        for outcome in all_outcomes:
            vals = [probs.get(outcome, 0) for _, probs, _ in live_books if outcome in probs]
            consensus[outcome] = sum(vals) / len(vals) if vals else 0

        total = sum(consensus.values())
        if total < 0.1:
            return None

        stale_count = len(book_probs) - len(live_books)
        if stale_count > 0:
            print(f"[fetch_pinnacle] {len(live_books)} live books, {stale_count} stale filtered", flush=True)

        return {k: v / total for k, v in consensus.items()}
    return None


# ── Polymarket ────────────────────────────────────────────────────────────────
def fetch_poly_event(home: str, away: str) -> Optional[Dict]:
    """
    Fetch today's baseball events using date-filtered endpoint.
    end_date_min/end_date_max returns ~43 today's games vs stale 2024 data from offset=200+.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data = _get(POLY_EVENTS, {
        "tag_slug": "baseball", "limit": 100,
        "end_date_min": f"{today}T00:00:00Z",
        "end_date_max": f"{today}T23:59:59Z",
    })
    if not data or not isinstance(data, list):
        return None
    for ev in data:
        t = ev.get("title", "").lower()
        if " vs" not in t:
            continue
        if _nmatch(home, t) and _nmatch(away, t):
            return ev
    return None


def fetch_pm_sdk_moneyline(home: str, away: str) -> Dict[str, Tuple]:
    """
    Fallback when Gamma API has no full-game moneyline.
    Returns {label: (slug, mid_price, liquid)} — slug used as token_id placeholder.

    Strategy 1 (primary): Direct slug construction — aec-mlb-{away_abbr}-{home_abbr}-{date}.
      Bypasses SDK search pagination (moneyline is market #62 of 85; search returns ~8 per event).
      BBO on the YES token gives the live away-wins probability; home = 1 - away.

    Strategy 2 (fallback): SDK search by team names.
      Used when abbreviations are unknown; iterates ev.markets but may miss the moneyline.
    """
    from datetime import timedelta
    try:
        from polymarket_us import PolymarketUS
        c = PolymarketUS()

        # ── Strategy 1: direct slug ──────────────────────────────────────────
        away_abbr = _team_abbr(away)
        home_abbr = _team_abbr(home)
        if away_abbr and home_abbr:
            # PM US slugs are dated by the ET *game date*, not UTC. After 00:00 UTC
            # (20:00 ET) the UTC date runs a day ahead, which 404s every live game
            # (root of the 2026-07-01 23:57 UTC 404 storm). A live game is dated
            # today-ET or — for late west-coast games past midnight ET —
            # yesterday-ET. Never tomorrow: that's a different (pre-game) market.
            from zoneinfo import ZoneInfo
            now_et = datetime.now(ZoneInfo("America/New_York"))
            today_et = now_et.strftime("%Y-%m-%d")
            yesterday_et = (now_et - timedelta(days=1)).strftime("%Y-%m-%d")
            for game_date in [today_et, yesterday_et]:
                slug = f"aec-mlb-{away_abbr}-{home_abbr}-{game_date}"
                try:
                    bbo = c.markets.bbo(slug)
                    md = bbo.get("marketData", {})
                    best_bid = float((md.get("bestBid") or {}).get("value", 0) or 0)
                    best_ask = float((md.get("bestAsk") or {}).get("value", 1) or 1)
                    last_trade = md.get("lastTradePx")
                    if best_bid > 0 and best_ask < 1 and best_ask > best_bid:
                        away_price = (best_bid + best_ask) / 2  # YES = away team wins
                        home_price = 1.0 - away_price
                        liquid = last_trade is not None
                        if not liquid:
                            print(f"[mlb_monitor] SDK slug {slug}: lastTradePx=None, stale", flush=True)
                        else:
                            print(f"[mlb_monitor] SDK direct slug hit: {slug} away={away_price:.2f}", flush=True)
                        return {
                            "away": (slug, away_price, liquid),
                            "home": (slug, home_price, liquid),
                        }
                    else:
                        print(f"[mlb_monitor] SDK slug {slug}: empty BBO (bid={best_bid:.2f} ask={best_ask:.2f})", flush=True)
                except Exception as e:
                    print(f"[mlb_monitor] SDK BBO slug {slug} failed: {e}", flush=True)

        # ── Strategy 2: SDK search fallback ─────────────────────────────────
        resp = c.search.query({"query": f"{away} {home} mlb"})
        events = resp if isinstance(resp, list) else resp.get("events", resp.get("results", []))
        for ev in events:
            title = ev.get("title", "").lower()
            if " vs" not in title:
                continue
            if not (_nmatch(home, title) and _nmatch(away, title)):
                continue
            for m in (ev.get("markets", []) or []):
                if m.get("sportsMarketType") != "baseball_team_full_game_winner":
                    continue
                slug = m.get("slug", "")
                if not slug:
                    continue
                prices = m.get("outcomePrices", [])
                if isinstance(prices, str):
                    try:
                        prices = json.loads(prices)
                    except Exception:
                        continue
                if len(prices) < 2:
                    continue
                try:
                    bbo = c.markets.bbo(slug)
                    md = bbo.get("marketData", {})
                    best_bid = float((md.get("bestBid") or {}).get("value", 0) or 0)
                    best_ask = float((md.get("bestAsk") or {}).get("value", 1) or 1)
                    last_trade = md.get("lastTradePx")
                    if best_bid > 0 and best_ask < 1 and best_ask > best_bid:
                        first_price = (best_bid + best_ask) / 2
                        liquid = last_trade is not None
                        if not liquid:
                            print(f"[mlb_monitor] SDK BBO {slug}: lastTradePx=None, stale", flush=True)
                    else:
                        first_price = float(prices[0])
                        liquid = False
                except Exception:
                    first_price = float(prices[0])
                    liquid = False
                second_price = 1.0 - first_price
                parts = title.split(" vs")
                first_team = parts[0].strip()
                if _nmatch(away, first_team):
                    return {"away": (slug, first_price, liquid), "home": (slug, second_price, liquid)}
                else:
                    return {"home": (slug, first_price, liquid), "away": (slug, second_price, liquid)}
    except Exception as e:
        print(f"[mlb_monitor] SDK moneyline fallback failed: {e}", flush=True)
    return {}


_MLB_ABBR: Dict[str, str] = {
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


def _team_abbr(name: str) -> Optional[str]:
    return _MLB_ABBR.get(name.lower().strip())


_ML_NOISE = {"spread", "o/u", "over", "under", "inning", "extra innings", "will there", "first inning", "run scored", "strikeout", "home run", "hit", "rbi"}


def extract_tokens(ev: Dict, home: str, away: str) -> Dict[str, Tuple[str, float]]:
    """
    Returns {"home": (token_id, price), "away": (token_id, price)} for the moneyline market.

    PM MLB moneyline structure:
    - Event title: "[Away] vs. [Home]" (away team listed first)
    - Moneyline market question = game title (e.g. "Cincinnati Reds vs. New York Yankees")
    - prices[0] = YES price (first-named team, usually away wins)
    - prices[1] = NO price (second-named team, usually home wins)
    - clobTokenIds = [yes_token, no_token]

    Markets with spread/OU/prop keywords are skipped.
    """
    tokens: Dict[str, Tuple[str, float]] = {}
    for m in ev.get("markets", []):
        q = m.get("question", "").lower()
        # Skip spread, over/under, and prop markets
        if any(noise in q for noise in _ML_NOISE):
            continue
        # Skip closed markets with no CLOB tokens — prices are frozen Gamma cache
        if m.get("closed", False) and not m.get("clobTokenIds"):
            continue
        prices = m.get("outcomePrices", [])
        tids = m.get("clobTokenIds", [])
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except Exception:
                continue
        if isinstance(tids, str):
            try:
                tids = json.loads(tids)
            except Exception:
                continue
        if len(prices) < 2 or len(tids) < 2:
            continue
        yes_price = float(prices[0])
        no_price  = float(prices[1])
        yes_tid   = tids[0]
        no_tid    = tids[1]

        # Both teams must appear in the question for it to be the moneyline
        if not (_nmatch(home, q) and _nmatch(away, q)):
            continue

        # Determine which team is YES (first in question) vs NO (second in question)
        # PM title format: "[Team A] vs. [Team B]" → YES = Team A
        parts = q.split(" vs")
        if len(parts) >= 2:
            first_team = parts[0].strip()
            if _nmatch(away, first_team):
                # Away is YES → home is NO
                tokens["away"] = (yes_tid, yes_price)
                tokens["home"] = (no_tid, no_price)
            else:
                # Home is YES → away is NO
                tokens["home"] = (yes_tid, yes_price)
                tokens["away"] = (no_tid, no_price)
        break  # only need the first moneyline market
    return tokens


# ── CLOB ──────────────────────────────────────────────────────────────────────
def fetch_book(token_id: str) -> Optional[Dict]:
    raw = _mc_get(f"poly:book:{token_id}")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    time.sleep(0.25)
    return _get(CLOB_BOOK, {"token_id": token_id})



def refresh_clob_prices(tokens: Dict) -> Dict:
    """Replace Gamma-cached prices with live CLOB mid. Adds liquid flag per token."""
    updated = {}
    for label, token_data in tokens.items():
        tid = token_data[0]
        gamma_price = token_data[1]
        book = fetch_book(tid)
        if not book:
            updated[label] = (tid, gamma_price, True)
            continue
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        best_bid = float(bids[0]["price"]) if bids else 0.0
        best_ask = float(asks[0]["price"]) if asks else 1.0
        if best_bid > 0 and best_ask < 1 and best_ask > best_bid:
            clob_mid = (best_bid + best_ask) / 2
            liquid = any(
                abs(float(o["price"]) - clob_mid) <= 0.15
                for o in (bids + asks)
            )
            updated[label] = (tid, clob_mid, liquid)
        else:
            updated[label] = (tid, gamma_price, False)
    return updated

def find_whale_wall(book: Dict, current_mid: Optional[float] = None,
                    proximity_pp: float = 15.0) -> Optional[Tuple[str, float, float]]:
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    if current_mid is None:
        best_bid = float(bids[0]["price"]) if bids else 0.0
        best_ask = float(asks[0]["price"]) if asks else 1.0
        current_mid = (best_bid + best_ask) / 2 if (best_bid > 0 or best_ask < 1) else None
    for side, key in [("ASK", "asks"), ("BID", "bids")]:
        for order in book.get(key, []):
            price = float(order.get("price", 0))
            sz = float(order.get("size", 0))
            if price >= 0.90 or price <= 0.10:
                continue
            if current_mid is not None and abs(price - current_mid) > (proximity_pp / 100):
                continue
            if sz >= WHALE_SIZE:
                return (side, price, sz)
    return None


# ── Alert 1: Run Trigger ──────────────────────────────────────────────────────
def check_run_trigger(conn: sqlite3.Connection, game: Dict,
                      pin: Optional[Dict], ev: Optional[Dict],
                      tokens: Dict) -> None:
    gid = game["game_id"]
    home, away = game["home_team"], game["away_team"]
    hs, as_ = game["home_score"], game["away_score"]
    detail = game.get("detail", "")
    hp = game.get("home_pitcher", "")
    ap = game.get("away_pitcher", "")

    row = conn.execute(
        "SELECT home_score, away_score, home_pitcher, away_pitcher FROM mlb_score_snap WHERE game_id=?",
        (gid,)
    ).fetchone()

    score_changed = row and (int(row["home_score"]), int(row["away_score"])) != (hs, as_)
    pitcher_changed = row and (
        (hp and row["home_pitcher"] and hp != row["home_pitcher"]) or
        (ap and row["away_pitcher"] and ap != row["away_pitcher"])
    )

    conn.execute("""
        INSERT OR REPLACE INTO mlb_score_snap
          (game_id, home_team, away_team, home_score, away_score, home_pitcher, away_pitcher, ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (gid, home, away, hs, as_, hp, ap, datetime.now(timezone.utc).isoformat()))
    conn.commit()

    if not score_changed and not pitcher_changed:
        return

    fired_ts = datetime.now(timezone.utc).strftime("%H:%M UTC")

    if score_changed and row:
        prev_hs, prev_as = int(row["home_score"]), int(row["away_score"])
        scorer = home if hs > prev_hs else away
        lines = [
            f"⚾ <b>RUN SCORED</b>  —  {fired_ts}",
            "",
            f"<b>{home} {hs} – {as_} {away}</b>  ({detail})",
            f"Run scored by: <b>{scorer}</b>",
        ]
    else:
        # Pitcher change only
        old_hp = row["home_pitcher"] if row else ""
        old_ap = row["away_pitcher"] if row else ""
        lines = [
            f"⚾ <b>PITCHING CHANGE</b>  —  {fired_ts}",
            "",
            f"<b>{home} {hs} – {as_} {away}</b>  ({detail})",
        ]
        if hp and old_hp and hp != old_hp:
            lines.append(f"  {home}: {old_hp} → <b>{hp}</b>")
        if ap and old_ap and ap != old_ap:
            lines.append(f"  {away}: {old_ap} → <b>{ap}</b>")

    if hp or ap:
        lines.append("")
        if game.get("status") == "in":
            # In-progress: only the live on-mound pitcher is accurate. The
            # other side's probable is stale (starter already out), so don't
            # fabricate a matchup.
            if hp and ap:
                lines.append(f"On the mound: <b>{hp}</b> ({home}) vs <b>{ap}</b> ({away})")
            elif hp:
                lines.append(f"On the mound: <b>{hp}</b> ({home})")
            elif ap:
                lines.append(f"On the mound: <b>{ap}</b> ({away})")
        else:
            lines.append(f"Probable starters: <b>{hp or '?'}</b> vs <b>{ap or '?'}</b>")

    if pin:
        lines.append("")
        lines.append("Vegas now thinks:")
        for name, prob in pin.items():
            tag = "(home)" if _nmatch(name, home) else "(away)"
            lines.append(f"  {name} wins: <b>{prob:.0%}</b> {tag}")

    trade_signals: List[str] = []
    signal_keys: List[str] = []
    signal_legs: List[Leg] = []
    if tokens and pin:
        lines.append("")
        lines.append("Is Polymarket keeping up?")
        for label, name in [("home", home), ("away", away)]:
            if label not in tokens:
                continue
            token_data = tokens[label]
            tid, poly_p = token_data[0], token_data[1]
            liquid = token_data[2] if len(token_data) > 2 else True
            pin_p = next((v for k, v in pin.items() if _nmatch(k, name)), None)
            if pin_p is None:
                continue
            gap = (pin_p - poly_p) * 100
            if not liquid:
                lines.append(f"  🔒 <b>{name} wins</b>: Polymarket {poly_p:.0%}  (illiquid — no live orders)")
            elif abs(gap) >= PM_STALE_PP:
                lines.append(f"  ⏸ <b>{name} wins</b>: Polymarket {poly_p:.0%}  (stale — Endgame not active, no live in-game liquidity)")
            elif abs(gap) >= PM_GAP_PP:
                direction = "cheaper" if gap > 0 else "pricier"
                action = "BUY" if gap > 0 else "SELL"
                # Vegas-tier EV filter (2026-08-23): only fire in +EV regimes.
                # The 40-60% middle is negative EV per edge_calibration.
                if not _in_positive_ev_tier(pin_p):
                    lines.append(f"  ⏸ <b>{name} wins</b>: Polymarket {poly_p:.0%}  (gap {abs(gap):.0f}pts but mid-tier {pin_p:.0%} — negative EV, suppressed)")
                else:
                    lines.append(f"  ⚠️ <b>{name} wins</b>: Polymarket {poly_p:.0%}  ← <b>{abs(gap):.0f}pts {direction} than Vegas</b>")
                    trade_signals.append(f"→ <b>{action} {name} wins YES</b> at {poly_p:.0%}  (Vegas: {pin_p:.0%})")
                    signal_keys.append(f"{action}:{name}")
                    signal_legs.append(Leg(name, abs(gap), action))
            else:
                lines.append(f"  ✅ <b>{name} wins</b>: Polymarket {poly_p:.0%}  (matches Vegas)")

    if trade_signals:
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━")
        lines.append("💰 <b>Polymarket hasn't adjusted yet:</b>")
        lines.extend(trade_signals)

    # Whale walls disabled — resting orders, not executed trades; actual trade
    # flow alerts come from sport_whale_trades.py. The old book prefetch here was
    # dead code AND passed SDK slug placeholders as CLOB token_ids (/book requires
    # numeric ERC-1155 ids → guaranteed 404). Removed 2026-07-02.
    walls_found: List[str] = []

    # Only send TG alerts when there's actionable edge (PM gap or whale wall)
    # Runs and pitcher changes are still tracked in DB snapshots above.
    # 2026-08-24: stopped dispatching no-edge events to tier-3 digest — they're
    # pure score-update noise (140+ per digest) with zero actionable content.
    # Already logged to stdout + DB snapshots; no need to spam the digest.
    if not trade_signals and not walls_found:
        event = "Pitcher change" if pitcher_changed and not score_changed else "Run"
        print(f"[mlb_monitor] {event} {home} {hs}-{as_} {away} — no edge/wall, suppressed", flush=True)
        return

    # Escalation-aware dedup (alert_governor): suppresses the same edge state,
    # fires instantly on gap widening >=5pp / direction flip / new leg. Replaces
    # the plain 45-min signature cooldown (92 sends on 2026-07-05 incident).
    # Governor state is seeded from mlb_run_alert_state on first run (C4).
    verdict = govern("mlb_run", gid, signal_legs)
    if not verdict.should_send:
        print(f"[mlb_monitor] Run alert governed ({','.join(verdict.reasons) or 'same-state'}) "
              f"{home} {hs}-{as_} {away}", flush=True)
        return

    send_telegram(verdict.decorate("\n".join(lines)))
    # Legacy state kept in sync as a rollback path (no longer gates anything).
    conn.execute(
        "INSERT OR REPLACE INTO mlb_run_alert_state (game_id, last_sent_ts, last_signature) VALUES (?, ?, ?)",
        (gid, datetime.now(timezone.utc).isoformat(), "|".join(sorted(signal_keys))),
    )
    conn.commit()
    print(f"[mlb_monitor] Run/pitcher alert ({verdict.action}): {home} {hs}-{as_} {away}", flush=True)


# ── Alert 2: Line Drift ───────────────────────────────────────────────────────
def _market_settling(tokens: Dict) -> bool:
    """True if PM market has effectively resolved (any outcome ≥98% or all ≤2%)."""
    if not tokens:
        return False
    prices = [t[1] for t in tokens.values()]
    return any(p >= 0.98 for p in prices) or all(p <= 0.02 for p in prices)


def check_line_drift(conn: sqlite3.Connection, game: Dict,
                     pin: Optional[Dict], tokens: Dict) -> None:
    if not pin:
        return
    if _market_settling(tokens):
        return
    gid = game["game_id"]
    home, away = game["home_team"], game["away_team"]
    hs, as_ = game["home_score"], game["away_score"]
    detail = game.get("detail", "")
    current_status = game.get("status", "in")
    now_ts = datetime.now(timezone.utc).isoformat()
    fired_ts = datetime.now(timezone.utc).strftime("%H:%M UTC")

    any_drift = False
    outcome_data = []

    for outcome, prob in pin.items():
        row = conn.execute(
            "SELECT devig, game_status FROM mlb_line_snap WHERE game_id=? AND outcome=?", (gid, outcome)
        ).fetchone()

        prev = float(row["devig"]) if row else prob
        prev_status = row["game_status"] if row else None
        move = (prob - prev) * 100

        # Only flag drift when BOTH the previous and current snapshot are live (in-game).
        # Pre-game → in-game transition is expected (10% pre-game vs 56% trailing in Bottom 1st
        # is normal; it's not a drift event).
        if abs(move) >= LINE_DRIFT_PP and prev_status == "in" and current_status == "in":
            any_drift = True

        label = "home" if _nmatch(outcome, home) else "away"
        tok = tokens.get(label)
        # Only use PM price when token is confirmed liquid (SDK live market).
        # Illiquid tokens (DH mismatch, SDK gap guard fired) show "—" not a stale number.
        poly_p = tok[1] if tok and (len(tok) < 3 or tok[2]) else None
        gap = (prob - poly_p) * 100 if poly_p is not None else None

        outcome_data.append({
            "name": outcome, "prev": prev, "now": prob, "move": move,
            "poly": poly_p, "gap": gap,
        })

        conn.execute("""
            INSERT OR REPLACE INTO mlb_line_snap (game_id, outcome, devig, ts, game_status)
            VALUES (?, ?, ?, ?, ?)
        """, (gid, outcome, prob, now_ts, current_status))
    conn.commit()

    # Blowout gate: suppress drift alert if game is effectively over
    # Athletics 3-9 Dodgers in Bottom 9th doesn't need an alert
    run_diff = abs(hs - as_)
    is_late = any(x in detail.lower() for x in ["7th", "8th", "9th", "10th", "11th", "12th", "13th"])
    blowout_threshold = BLOWOUT_LATE_DIFF if is_late else BLOWOUT_RUN_DIFF
    if run_diff >= blowout_threshold:
        any_drift = False

    if not any_drift:
        return

    score_line = f"{home} <b>{hs}–{as_}</b> {away}  ({detail})  <i>{fired_ts}</i>"
    trade_signals: List[str] = []
    grid_rows: List[list] = []
    comparison_lines: List[str] = []

    for d in outcome_data:
        sym = "↑" if d["move"] > 0 else "↓"
        moved = abs(d["move"]) >= LINE_DRIFT_PP
        move_str = f"{sym}{abs(d['move']):.0f}pts" if moved else f"{d['move']:+.0f}pts"
        label = f"{d['name']} wins"
        grid_rows.append([label, f"{d['prev']:.0%}", f"{d['now']:.0%}", move_str])

        if d["poly"] is not None and d["gap"] is not None:
            if abs(d["gap"]) >= PM_STALE_PP:
                comparison_lines.append(f"<b>{label}</b>: Polymarket {d['poly']:.0%}  ⏸ stale — Endgame not active")
            elif abs(d["gap"]) >= PM_GAP_PP:
                direction = "cheaper" if d["gap"] > 0 else "pricier"
                action = "BUY" if d["gap"] > 0 else "SELL"
                # Vegas-tier EV filter (2026-08-23): only fire in +EV regimes.
                if not _in_positive_ev_tier(d["now"]):
                    comparison_lines.append(f"<b>{label}</b>: Polymarket {d['poly']:.0%}  (gap {abs(d['gap']):.0f}pts but mid-tier {d['now']:.0%} — negative EV, suppressed)")
                else:
                    comparison_lines.append(f"<b>{label}</b>: Polymarket {d['poly']:.0%}  ← <b>{abs(d['gap']):.0f}pts {direction} than Vegas</b>")
                    trade_signals.append(f"→ <b>{action} {label} YES</b> at {d['poly']:.0%}  (Vegas: {d['now']:.0%})")
            else:
                comparison_lines.append(f"<b>{label}</b>: Polymarket {d['poly']:.0%}  (in line)")
        else:
            comparison_lines.append(f"<b>{label}</b>: Polymarket —")

    lines = [
        f"⚡ <b>ODDS MOVED</b> — MLB | {score_line}",
        "",
        "Vegas shifted big:",
        "",
        format_grid(["Outcome", "Prev", "Now", "Move"], grid_rows),
        "",
    ]
    lines.extend(comparison_lines)

    if trade_signals:
        lines.append("━━━━━━━━━━━━━━━━")
        lines.append("💰 <b>PM hasn't caught up yet:</b>")
        lines.extend(trade_signals)

    # Persist what we're about to send (append-only; never blocks the alert).
    try:
        for d in outcome_data:
            signal = None
            if d["poly"] is not None and d["gap"] is not None \
               and abs(d["gap"]) >= PM_GAP_PP and abs(d["gap"]) < PM_STALE_PP \
               and _in_positive_ev_tier(d["now"]):
                signal = "BUY" if d["gap"] > 0 else "SELL"
            conn.execute(
                """INSERT INTO mlb_odds_moved_log
                   (fired_at, game_id, home_team, away_team, home_score, away_score,
                    detail, outcome, prev_devig, now_devig, move_pp, poly_price,
                    gap_pp, trade_signal)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (now_ts, gid, home, away, hs, as_, detail, d["name"], d["prev"],
                 d["now"], d["move"], d["poly"], d["gap"], signal),
            )
        conn.commit()
    except Exception as ex:
        print(f"[mlb_monitor] odds_moved_log write failed: {ex}", flush=True)

    # Escalation-aware dedup (alert_governor) — drift had NO cooldown before
    # (3x Orioles/Royals sends in 10min at burst cadence, 2026-07-10). Magnitude
    # is the current devig LEVEL in pp so cumulative walks >=5pp re-fire as
    # upgrades; same-level re-reads suppress. Score deliberately NOT in state (C2).
    drift_legs = [Leg(d["name"], d["now"] * 100, "up" if d["move"] > 0 else "down")
                  for d in outcome_data if abs(d["move"]) >= LINE_DRIFT_PP]
    verdict = govern("mlb_line_drift", gid, drift_legs)
    if not verdict.should_send:
        print(f"[mlb_monitor] Line drift governed ({','.join(verdict.reasons) or 'same-state'}): {gid}", flush=True)
        return

    # LIVE (Gate 2 completed 2026-08-20): a Vegas move with no Polymarket gap
    # is not a decision — it goes to the tier-3 digest, not Telegram.
    # NOTE: the early return is load-bearing. Dropping shadow=True without it
    # would deliver the same event twice (digest + the direct send below).
    if not trade_signals:
        try:
            dispatch("odds_moved",
                     f"{home} {hs}–{as_} {away} — Vegas moved, no PM gap",
                     TIER_DIGEST)
        except Exception as ex:  # noqa: BLE001 — digest must never block
            print(f"[mlb_monitor] digest dispatch failed: {ex}", flush=True)
        print(f"[mlb_monitor] Line drift {gid} — no PM gap, digested", flush=True)
        return

    send_telegram(verdict.decorate("\n".join(lines)))
    print(f"[mlb_monitor] Line drift alert ({verdict.action}): {gid}", flush=True)


# ── Alert 3: Edge Inversion ───────────────────────────────────────────────────
def check_edge_inversion(conn: sqlite3.Connection, game: Dict,
                         tokens: Dict, pin: Optional[Dict]) -> None:
    if not pin or not tokens:
        return
    home, away = game["home_team"], game["away_team"]
    now_ts = datetime.now(timezone.utc).isoformat()

    rows = conn.execute("""
        SELECT id, market, side, entry_price, entry_book_prob
        FROM shadow_trades
        WHERE (resolved IS NULL OR resolved = 0)
          AND (close_reason IS NULL OR close_reason = '')
          AND strategy = 'mlb_match_2way'
          AND (market LIKE ? OR market LIKE ?)
    """, (f"%{home}%", f"%{away}%")).fetchall()

    for row in rows:
        ml = row["market"].lower()
        label = "home" if _nmatch(home, ml) else "away" if _nmatch(away, ml) else None
        if not label or label not in tokens:
            continue

        tok = tokens[label]
        if len(tok) >= 3 and not tok[2]:
            continue  # illiquid token (DH mismatch / SDK gap guard) — skip edge eval
        current_poly = tok[1]
        team = home if label == "home" else away
        pin_prob = next((v for k, v in pin.items() if _nmatch(k, team)), 0)
        current_edge = pin_prob - current_poly
        entry_edge = (row["entry_book_prob"] or pin_prob) - (row["entry_price"] or 0)

        if current_edge < EDGE_FLOOR:
            reason = "edge_inverted" if current_edge < 0 else "edge_below_floor"
            conn.execute("""
                UPDATE shadow_trades SET close_reason=?, last_checked_at=? WHERE id=?
            """, (reason, now_ts, row["id"]))
            conn.commit()

            if current_edge < 0:
                verdict = "❌ <b>Edge flipped</b> — Polymarket now pricier than Vegas."
                rec = "→ <b>Consider exiting.</b>"
            else:
                verdict = "⚠️ <b>Edge mostly closed</b> — PM nearly caught up."
                rec = "→ <b>Consider taking profit.</b>"

            msg = (
                f"🚨 <b>TRADE UPDATE</b>  —  <b>{team} wins</b>\n"
                f"\n"
                f"Entered at <b>{int(round((row['entry_price'] or 0)*100))}¢</b>  "
                f"(gap was <b>{int(round(entry_edge*100)):+d}pts</b>)\n"
                f"Now:  PM <b>{int(round(current_poly*100))}¢</b>  |  "
                f"Vegas <b>{int(round(pin_prob*100))}%</b>\n"
                f"\n{verdict}\n{rec}"
            )
            # Governor guards the two-process race (burst + tick both reading the
            # open trade before either commits close_reason): once per trade, ever.
            verdict = govern("mlb_edge_inversion", f"trade:{row['id']}",
                             [Leg(reason, abs(current_edge * 100), reason)])
            if not verdict.should_send:
                print(f"[mlb_monitor] Edge inversion governed: {row['id']}", flush=True)
                continue
            send_telegram(msg)
            print(f"[mlb_monitor] Edge inversion: {row['id']} {reason}", flush=True)


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    conn = get_db()
    migrate(conn)

    games = fetch_espn_games()
    active = [g for g in games if g["status"] == "in"]

    if not active:
        print("[mlb_monitor] No active games.", flush=True)
        purge_stale()  # governor housekeeping (blind spot #8) — cheap, off-slate only
        conn.close()
        return

    print(f"[mlb_monitor] {len(active)} active game(s)", flush=True)

    for game in active:
        home, away = game["home_team"], game["away_team"]
        print(f"[mlb_monitor] → {home} vs {away}  ({game['detail']})", flush=True)

        # Fetch Pinnacle + PM in parallel
        f_pin = _EXECUTOR.submit(fetch_pinnacle, home, away)
        f_ev  = _EXECUTOR.submit(fetch_poly_event, home, away)

        try:
            pin = f_pin.result(timeout=15)
        except Exception as e:
            print(f"[mlb_monitor] fetch_pinnacle failed: {e}", flush=True)
            pin = None
        try:
            ev = f_ev.result(timeout=15)
        except Exception as e:
            print(f"[mlb_monitor] fetch_poly_event failed: {e}", flush=True)
            ev = None

        tokens = extract_tokens(ev, home, away) if ev else {}
        sdk_source = False

        # Primary price source: SDK — CLOB is dead during live MLB games.
        # Gamma+CLOB path is kept as fallback only when SDK returns nothing.
        sdk_tokens = fetch_pm_sdk_moneyline(home, away)
        if sdk_tokens and any(v[2] for v in sdk_tokens.values()):
            sdk_source = True
            if tokens:
                # Gamma found the event: keep its token IDs (used for WS/whale
                # detection), but replace stale Gamma prices with live SDK prices.
                for label, (slug, price, liquid) in sdk_tokens.items():
                    if label in tokens:
                        gamma_tid = tokens[label][0]
                        tokens[label] = (gamma_tid, price, liquid)
            else:
                tokens = sdk_tokens
            print(f"[mlb_monitor] SDK live prices (primary): {home} vs {away}", flush=True)
        elif not tokens:
            # No Gamma event either — use SDK even if not fully liquid
            tokens = sdk_tokens if sdk_tokens else {}
            sdk_source = bool(tokens)
            if sdk_source:
                print(f"[mlb_monitor] SDK fallback (no Gamma): {home} vs {away}", flush=True)

        # SDK gap guard: >15pp off Vegas = SDK market not tracked by Endgame MMs, no live liquidity
        SDK_STALE_PP = 15.0
        if sdk_source and tokens and pin:
            for label, name in [("home", home), ("away", away)]:
                if label not in tokens:
                    continue
                pin_p = next((v for k, v in pin.items() if _nmatch(k, name)), None)
                if pin_p is None:
                    continue
                td = tokens[label]
                gap = abs((pin_p - td[1]) * 100)
                if gap > SDK_STALE_PP:
                    tokens[label] = (td[0], td[1], False)
                    print(f"[mlb_monitor] SDK stale guard: {name} gap={gap:.0f}pp vs Vegas, illiquid", flush=True)

        if tokens and not sdk_source:
            # SDK unavailable — fall back to CLOB refresh on Gamma token IDs
            mc_register_tokens([tokens[lbl][0] for lbl in tokens])
            tokens = refresh_clob_prices(tokens)

        check_run_trigger(conn, game, pin, ev, tokens)
        check_line_drift(conn, game, pin, tokens)
        check_edge_inversion(conn, game, tokens, pin)

    conn.close()


if __name__ == "__main__":
    main()

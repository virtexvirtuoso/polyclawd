#!/usr/bin/env python3
"""
soccer_live_monitor.py — Live-game alert monitor for WC matches.

Fires 4 alert types every 5 min during active game windows:
  1. GOAL TRIGGER    — ESPN score change → full fresh odds + CLOB re-analysis
  2. LINE DRIFT      — Pinnacle ML moves >LINE_DRIFT_PP since last snapshot
  3. CLOB WHALE WALL — single ask/bid >WHALE_SIZE in match outcome market
  4. EDGE INVERSION  — open soccer shadow trade → close + alert if edge gone

Cron: */5 * * * * (filter to game hours in deployment)
State: storage/shadow_trades.db (3 new tables auto-migrated)
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
import time
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from scripts.alert_formatter import send_telegram

# ── Config ──────────────────────────────────────────────────────────────────
POLY_EVENTS    = "https://gamma-api.polymarket.com/events"
CLOB_BOOK      = "https://clob.polymarket.com/book"
ESPN_BASE      = "https://site.api.espn.com/apis/site/v2/sports"
ODDS_API_BASE  = "https://api.the-odds-api.com/v4/sports"

# ── League configs ────────────────────────────────────────────────────────────
# espn_path: appended to ESPN_BASE/{path}/scoreboard
# odds_key:  Odds API sport key (bookmakers=pinnacle)
# pm_tag:    Gamma API tag_slug for Polymarket event lookup
# kalshi_series: Kalshi series ticker, or None to skip Kalshi leg
# active_months: skip entirely outside these months (saves credits)
LEAGUE_CONFIGS: List[Dict] = [
    {
        "name": "WC Soccer",
        "espn_path": "soccer/fifa.world/scoreboard",
        "odds_key": "soccer_fifa_world_cup",
        "pm_tag": "fifa-world-cup",
        "kalshi_series": "KXWCGAME",
        "active_months": [6, 7],
    },
    {
        "name": "MLS",
        "espn_path": "soccer/usa.1/scoreboard",
        "odds_key": "soccer_usa_mls",
        "pm_tag": "mls",
        "kalshi_series": None,
        "active_months": [3, 4, 5, 6, 7, 8, 9, 10, 11],
    },
    {
        "name": "EPL",
        "espn_path": "soccer/eng.1/scoreboard",
        "odds_key": "soccer_epl",
        "pm_tag": "soccer",
        "kalshi_series": None,
        "active_months": [8, 9, 10, 11, 12, 1, 2, 3, 4, 5],
    },
    {
        "name": "UCL",
        "espn_path": "soccer/uefa.champions/scoreboard",
        "odds_key": "soccer_uefa_champs_league",
        "pm_tag": "soccer",
        "kalshi_series": None,
        "active_months": [9, 10, 11, 12, 1, 2, 3, 4, 5, 6],
    },
    {
        "name": "LaLiga",
        "espn_path": "soccer/esp.1/scoreboard",
        "odds_key": "soccer_spain_la_liga",
        "pm_tag": "soccer",
        "kalshi_series": None,
        "active_months": [8, 9, 10, 11, 12, 1, 2, 3, 4, 5],
    },
]

ODDS_API_KEY   = os.environ.get("ODDS_API_KEY", "")
LINE_DRIFT_PP  = 5.0    # pp shift to trigger drift alert
WHALE_SIZE     = 50000  # single order size threshold (Polymarket CLOB units)
WHALE_DEDUP_S  = 1800   # suppress re-alert for same wall within 30 min
EDGE_FLOOR     = 0.02   # close shadow trade if edge drops below 2pp
PM_GAP_PP      = 6.0    # min pp gap PM vs Vegas to flag in alerts

DB_PATH  = BASE_DIR / "storage" / "shadow_trades.db"
MC_HOST, MC_PORT = "localhost", 11211

# Shared executor — module-level so it's reused across games and ticks,
# not created/destroyed per game invocation.
_EXECUTOR = ThreadPoolExecutor(max_workers=8)

# ── Team name aliases for matching ──────────────────────────────────────────
ALIASES: Dict[str, List[str]] = {
    "korea": ["south korea", "korea republic"],
    "south korea": ["korea republic", "korea"],
    "korea republic": ["south korea", "korea"],
    "usa": ["united states"],
    "united states": ["usa"],
    "ivory coast": ["cote d'ivoire"],
    "cote d'ivoire": ["ivory coast"],
}


# ── DB ───────────────────────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS soccer_score_snap (
            game_id     TEXT PRIMARY KEY,
            home_team   TEXT,
            away_team   TEXT,
            home_score  INTEGER DEFAULT 0,
            away_score  INTEGER DEFAULT 0,
            ts          TEXT
        );
        CREATE TABLE IF NOT EXISTS soccer_line_snap (
            game_id  TEXT,
            outcome  TEXT,
            devig    REAL,
            ts       TEXT,
            PRIMARY KEY (game_id, outcome)
        );
        CREATE TABLE IF NOT EXISTS soccer_whale_dedup (
            game_id       TEXT,
            outcome       TEXT,
            last_alert_ts TEXT,
            PRIMARY KEY (game_id, outcome)
        );
    """)
    conn.commit()


# ── Memcached (raw protocol, no extra deps) ──────────────────────────────────
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
    """Adds token_ids to poly:ws:registered so WS service subscribes to them.

    WS service reads this key every 60s — tokens start streaming within one cycle.
    Books (poly:book:{token_id}) then have a 15s TTL, so on subsequent 5min ticks
    the memcached read for fetch_book() will hit for active markets.
    """
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


# ── HTTP ─────────────────────────────────────────────────────────────────────
def _get(url: str, params: dict | None = None, timeout: int = 12) -> Optional[dict | list]:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"[monitor] GET {url[:70]} → {e}", flush=True)
        return None


# ── Name matching ────────────────────────────────────────────────────────────
def _nmatch(a: str, b: str) -> bool:
    a, b = a.lower().strip(), b.lower().strip()
    if a == b or a in b or b in a:
        return True
    for alias in ALIASES.get(a, []):
        if alias in b or b in alias:
            return True
    return False


# ── ESPN ─────────────────────────────────────────────────────────────────────
def fetch_espn_games(espn_path: str) -> List[Dict]:
    data = _get(f"{ESPN_BASE}/{espn_path}")
    if not data:
        return []
    games = []
    for event in data.get("events", []):
        for comp in event.get("competitions", []):
            status_type = comp.get("status", {}).get("type", {})
            raw = status_type.get("name", "").lower()
            state = status_type.get("state", "").lower()
            completed = status_type.get("completed", False)
            if completed or "final" in raw or "end" in raw:
                status = "final"
            elif state == "in" or "progress" in raw or "half" in raw or "second" in raw or "first" in raw:
                status = "in"
            else:
                status = "pre"

            home = away = ""
            hs = as_ = 0
            for c in comp.get("competitors", []):
                name = c.get("team", {}).get("displayName", "")
                score = int(c.get("score", 0) or 0)
                if c.get("homeAway") == "home":
                    home, hs = name, score
                else:
                    away, as_ = name, score

            if home and away:
                gid = f"{home.lower().replace(' ','_')}_{away.lower().replace(' ','_')}"
                detail = comp.get("status", {}).get("type", {}).get("detail", "")
                games.append({
                    "game_id": gid, "home_team": home, "away_team": away,
                    "home_score": hs, "away_score": as_,
                    "status": status, "detail": detail,
                })
    return games


# ── Pinnacle ─────────────────────────────────────────────────────────────────
def fetch_pinnacle(home: str, away: str, odds_key: str = "soccer_fifa_world_cup") -> Optional[Dict[str, float]]:
    """Returns {outcome_name: devigged_prob} from Pinnacle live h2h."""
    if not ODDS_API_KEY:
        return None
    data = _get(f"{ODDS_API_BASE}/{odds_key}/odds/", {
        "apiKey": ODDS_API_KEY, "regions": "us",
        "markets": "h2h", "oddsFormat": "american", "bookmakers": "pinnacle",
    })
    if not data:
        return None

    def imp(price: int) -> float:
        p = int(price)
        return (100 / (100 + p)) if p > 0 else (-p / (-p + 100))

    for game in data:
        if not (_nmatch(game.get("home_team", ""), home) and
                _nmatch(game.get("away_team", ""), away)):
            continue
        for bm in game.get("bookmakers", []):
            if bm["key"] != "pinnacle":
                continue
            for mkt in bm.get("markets", []):
                if mkt["key"] != "h2h":
                    continue
                outcomes = mkt["outcomes"]
                # Skip outcomes with price=0 (Pinnacle pulls lines mid-reprice after goals)
                valid = [o for o in outcomes if o.get("price", 0) != 0]
                if not valid:
                    continue
                raw = {o["name"]: imp(o["price"]) for o in valid}
                total = sum(raw.values())
                # Guard: if missing an outcome (e.g. draw line pulled), totals won't
                # sum to ~1.0 after devig — return None so caller can skip stale data
                if total < 0.1:
                    return None
                return {k: v / total for k, v in raw.items()}
    return None


# ── Polymarket ────────────────────────────────────────────────────────────────
def fetch_poly_event(home: str, away: str, pm_tag: str = "fifa-world-cup") -> Optional[Dict]:
    data = _get(POLY_EVENTS, {"tag_slug": pm_tag, "active": "true", "limit": 200})
    if not data:
        return None
    for ev in (data if isinstance(data, list) else []):
        t = ev.get("title", "").lower()
        if _nmatch(home, t) and _nmatch(away, t):
            return ev
    return None


def extract_tokens(ev: Dict, home: str, away: str) -> Dict[str, Tuple[str, float]]:
    """Returns {"home": (token_id, price), "draw": ..., "away": ...}"""
    tokens: Dict[str, Tuple[str, float]] = {}
    for m in ev.get("markets", []):
        q = m.get("question", "").lower()
        prices = m.get("outcomePrices", [])
        tids = m.get("clobTokenIds", [])
        if not prices or not tids:
            continue
        # API returns these as JSON strings or lists depending on endpoint
        if isinstance(prices, str):
            prices = json.loads(prices)
        if isinstance(tids, str):
            tids = json.loads(tids)
        yes_price = float(prices[0])
        yes_tid = tids[0]
        if "draw" in q:
            tokens["draw"] = (yes_tid, yes_price)
        elif "win" in q and _nmatch(home, q) and not _nmatch(away, q):
            tokens["home"] = (yes_tid, yes_price)
        elif "win" in q and _nmatch(away, q) and not _nmatch(home, q):
            tokens["away"] = (yes_tid, yes_price)
    return tokens


# ── CLOB ─────────────────────────────────────────────────────────────────────
def fetch_book(token_id: str) -> Optional[Dict]:
    # Try WS-published book first (~0ms). poly:book: TTL=15s, so hit rate is
    # highest during active market periods. Falls back to REST on miss.
    raw = _mc_get(f"poly:book:{token_id}")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    # REST fallback — rate limit only applies here, not on the fast path
    time.sleep(0.25)
    return _get(CLOB_BOOK, {"token_id": token_id})


def get_depth(book: Dict, levels: int = 5) -> Tuple[float, float]:
    """Returns (total_bid_size, total_ask_size) for top N levels."""
    bid_sz = sum(float(b.get("size", 0)) for b in book.get("bids", [])[:levels])
    ask_sz = sum(float(a.get("size", 0)) for a in book.get("asks", [])[:levels])
    return bid_sz, ask_sz


def find_whale_wall(book: Dict, current_mid: Optional[float] = None,
                    proximity_pp: float = 15.0) -> Optional[Tuple[str, float, float]]:
    """Returns (side, price, size) if a meaningful single order >= WHALE_SIZE.

    Filters:
    - Structural edge orders (price outside 0.10–0.90)
    - Orders more than proximity_pp away from the current mid price
      (deep resting orders from market makers, e.g. $100k bid at 14% when mid is 80%)
    """
    bids = book.get("bids", [])
    asks = book.get("asks", [])

    # Compute mid from book if not provided
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
            # Skip orders parked far from the current market price
            if current_mid is not None and abs(price - current_mid) > (proximity_pp / 100):
                continue
            if sz >= WHALE_SIZE:
                return (side, price, sz)
    return None




# ── Kalshi WC ─────────────────────────────────────────────────────────────────
_KALSHI_CODES: Dict[str, List[str]] = {
    "USA": ["united states", "usa"], "TUR": ["turkiye", "turkey"],
    "BRA": ["brazil"], "ARG": ["argentina"], "FRA": ["france"],
    "ENG": ["england"], "GER": ["germany"], "ESP": ["spain"],
    "POR": ["portugal"], "NED": ["netherlands", "holland"],
    "MAR": ["morocco"], "JPN": ["japan"], "MEX": ["mexico"],
    "KOR": ["south korea", "korea republic"], "SEN": ["senegal"],
    "GHA": ["ghana"], "NGA": ["nigeria"], "URU": ["uruguay"],
    "COL": ["colombia"], "AUS": ["australia"], "CAN": ["canada"],
    "BEL": ["belgium"], "CRO": ["croatia"], "SWI": ["switzerland"],
    "DEN": ["denmark"], "SWE": ["sweden"], "POL": ["poland"],
    "SAU": ["saudi arabia"], "IRQ": ["iraq"], "IRN": ["iran"],
    "PAR": ["paraguay"], "CPV": ["cape verde"], "DZA": ["algeria"],
    "CIV": ["ivory coast", "cote d'ivoire"], "TUN": ["tunisia"],
    "CMR": ["cameroon"], "COD": ["dr congo", "democratic republic of congo"],
    "NOR": ["norway"], "UZB": ["uzbekistan"], "ZAM": ["zambia"],
}

def _name_to_kalshi_code(name: str) -> Optional[str]:
    n = name.lower().strip()
    for code, aliases in _KALSHI_CODES.items():
        if n in aliases or any(a in n or n in a for a in aliases):
            return code
    return None


def fetch_kalshi_wc(home: str, away: str, series_ticker: str = "KXWCGAME") -> Optional[Dict[str, float]]:
    """Fetch Kalshi WC game prices → {home: mid, draw: mid, away: mid}."""
    try:
        hcode = _name_to_kalshi_code(home)
        acode = _name_to_kalshi_code(away)
        if not hcode or not acode:
            return None
        data = _get(
            "https://api.elections.kalshi.com/trade-api/v2/markets",
            {"limit": 200, "status": "open", "series_ticker": series_ticker},
        )
        if not data:
            return None
        markets = data if isinstance(data, list) else data.get("markets", [])
        result: Dict[str, float] = {}
        for m in markets:
            ticker = m.get("ticker", "")
            parts = ticker.split("-")
            if len(parts) < 3:
                continue
            event_seg = parts[1].upper()
            outcome_code = parts[2].upper()
            if hcode not in event_seg or acode not in event_seg:
                continue
            bid = float(m.get("yes_bid_dollars", 0))
            ask = float(m.get("yes_ask_dollars", 0))
            mid = (bid + ask) / 2 if (bid > 0 or ask > 0) else 0.0
            if outcome_code == hcode:
                result["home"] = mid
            elif outcome_code == acode:
                result["away"] = mid
            elif outcome_code in ("TIE", "DRAW"):
                result["draw"] = mid
        return result if len(result) >= 2 else None
    except Exception as e:
        print(f"[kalshi_wc] {e}", flush=True)
        return None

# ── Alert 1: Goal Trigger ─────────────────────────────────────────────────────
def check_goal_trigger(conn: sqlite3.Connection, game: Dict,
                       pin: Optional[Dict], ev: Optional[Dict],
                       tokens: Dict) -> None:
    gid = game["game_id"]
    home, away = game["home_team"], game["away_team"]
    hs, as_ = game["home_score"], game["away_score"]
    detail = game.get("detail", "")

    row = conn.execute(
        "SELECT home_score, away_score FROM soccer_score_snap WHERE game_id=?", (gid,)
    ).fetchone()

    score_changed = row and (int(row["home_score"]), int(row["away_score"])) != (hs, as_)

    # Upsert score snapshot
    conn.execute("""
        INSERT OR REPLACE INTO soccer_score_snap
          (game_id, home_team, away_team, home_score, away_score, ts)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (gid, home, away, hs, as_, datetime.now(timezone.utc).isoformat()))
    conn.commit()

    if not score_changed:
        return

    prev_hs, prev_as = int(row["home_score"]), int(row["away_score"])
    scorer = home if hs > prev_hs else away
    print(f"[goal_trigger] {home} {hs}-{as_} {away} (was {prev_hs}-{prev_as})", flush=True)

    fired_ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    lines = [
        f"⚽ <b>GOAL SCORED</b>  —  {fired_ts}",
        f"",
        f"<b>{home} {hs} – {as_} {away}</b>  ({detail})",
        f"Scored by: <b>{scorer}</b>",
        "",
    ]

    if pin:
        hp = pin.get(home, 0)
        ap = pin.get(away, 0)
        dp = pin.get("Draw", 0)
        lines.append("Vegas now thinks:")
        lines.append(f"  {home} wins: <b>{hp:.0%}</b>")
        lines.append(f"  Draw: <b>{dp:.0%}</b>")
        lines.append(f"  {away} wins: <b>{ap:.0%}</b>")

    trade_signals: List[str] = []
    if tokens and pin:
        lines.append("")
        lines.append("Is Polymarket keeping up?")
        for label, name in [("home", home), ("draw", "Draw"), ("away", away)]:
            if label not in tokens:
                continue
            tid, poly_p = tokens[label]
            pin_p = pin.get(name if label != "draw" else "Draw", 0)
            gap = (pin_p - poly_p) * 100
            plain = "It ends in a draw" if name == "Draw" else f"{name} wins"
            if abs(gap) >= PM_GAP_PP:
                direction = "cheaper" if gap > 0 else "pricier"
                action = "BUY" if gap > 0 else "SELL"
                lines.append(f"  ⚠️ <b>{plain}</b>: Polymarket {poly_p:.0%}  ← <b>{abs(gap):.0f}pts {direction} than Vegas</b>")
                trade_signals.append(f"→ <b>{action} {plain} YES</b> at {poly_p:.0%}  (Vegas: {pin_p:.0%})")
            else:
                lines.append(f"  ✅ <b>{plain}</b>: Polymarket {poly_p:.0%}  (matches Vegas)")

    if trade_signals:
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━")
        lines.append("💰 <b>Polymarket hasn't adjusted yet:</b>")
        lines.extend(trade_signals)

    # Whale depth check post-goal — fetch all 3 books in parallel
    if tokens:
        goal_labels = [(lbl, nm) for lbl, nm in [("home", home), ("draw", "Draw"), ("away", away)]
                       if lbl in tokens]
        book_futures = {lbl: _EXECUTOR.submit(fetch_book, tokens[lbl][0])
                        for lbl, _ in goal_labels}
        walls_found: List[str] = []
        for label, name in goal_labels:
            try:
                book = book_futures[label].result(timeout=15)
            except Exception:
                book = None
            if not book:
                continue
            wall = find_whale_wall(book)
            if wall:
                side, price, size = wall
                plain = "It ends in a draw" if name == "Draw" else f"{name} wins"
                action_desc = "selling" if side == "ASK" else "buying"
                walls_found.append(f"  🐋 Someone is {action_desc} <b>${size:,.0f}</b> at {price:.0%} on <b>{plain}</b>")
        if walls_found:
            lines.append("")
            lines.append("Big money spotted post-goal:")
            lines.extend(walls_found)

    send_telegram("\n".join(lines))
    print("[goal_trigger] Alert sent", flush=True)


# ── Alert 2: Line Drift ───────────────────────────────────────────────────────
def check_line_drift(conn: sqlite3.Connection, game: Dict,
                     pin: Optional[Dict],
                     tokens: Dict[str, Tuple[str, float]],
                     kal: Optional[Dict[str, float]] = None) -> None:
    if not pin:
        return
    gid = game["game_id"]
    home, away = game["home_team"], game["away_team"]
    hs, as_ = game["home_score"], game["away_score"]
    detail = game.get("detail", "")
    now_ts = datetime.now(timezone.utc).isoformat()
    fired_ts = datetime.now(timezone.utc).strftime("%H:%M UTC")

    # Build label → outcome name map for Poly lookup
    label_map = {"home": home, "away": away, "draw": "Draw"}
    # Reverse: pinnacle outcome name → label
    pin_to_label: Dict[str, str] = {}
    for lbl, name in label_map.items():
        for pname in pin:
            if _nmatch(pname, name) or (lbl == "draw" and pname == "Draw"):
                pin_to_label[pname] = lbl
                break

    FLAGS: Dict[str, str] = {
        "mexico": "🇲🇽", "south korea": "🇰🇷", "korea republic": "🇰🇷",
        "usa": "🇺🇸", "united states": "🇺🇸", "brazil": "🇧🇷",
        "france": "🇫🇷", "germany": "🇩🇪", "spain": "🇪🇸",
        "argentina": "🇦🇷", "england": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "portugal": "🇵🇹",
        "morocco": "🇲🇦", "japan": "🇯🇵", "netherlands": "🇳🇱",
        "australia": "🇦🇺", "canada": "🇨🇦", "turkiye": "🇹🇷", "turkey": "🇹🇷",
        "saudi arabia": "🇸🇦", "nigeria": "🇳🇬", "senegal": "🇸🇳",
        "colombia": "🇨🇴", "uruguay": "🇺🇾", "croatia": "🇭🇷",
        "switzerland": "🇨🇭", "denmark": "🇩🇰", "poland": "🇵🇱",
        "draw": "🤝",
    }
    def _flag(name: str) -> str:
        return FLAGS.get(name.lower(), "⚽")

    any_drift = False
    # Collect data for all outcomes first, then build message
    outcome_data = []

    for outcome, prob in pin.items():
        row = conn.execute(
            "SELECT devig FROM soccer_line_snap WHERE game_id=? AND outcome=?", (gid, outcome)
        ).fetchone()

        move = 0.0
        prev = prob
        if row:
            prev = float(row["devig"])
            move = (prob - prev) * 100
            if abs(move) >= LINE_DRIFT_PP:
                any_drift = True

        lbl = pin_to_label.get(outcome, "")
        poly_p = tokens[lbl][1] if lbl in tokens else None
        gap = (prob - poly_p) * 100 if poly_p is not None else None

        outcome_data.append({
            "name": outcome, "flag": _flag(outcome),
            "prev": prev, "now": prob, "move": move,
            "poly": poly_p, "gap": gap,
        })

        conn.execute("""
            INSERT OR REPLACE INTO soccer_line_snap (game_id, outcome, devig, ts)
            VALUES (?, ?, ?, ?)
        """, (gid, outcome, prob, now_ts))
    conn.commit()

    if not any_drift:
        return

    # ── Build novice-friendly message ─────────────────────────────────────
    score_line = f"{home} <b>{hs}–{as_}</b> {away}  ({detail})  <i>{fired_ts}</i>"
    lines = [
        f"⚡ <b>ODDS MOVED</b> — {score_line}",
        "",
        "Vegas bookmakers shifted big. Here's where each outcome stands:",
        "",
    ]

    trade_signals: List[str] = []

    for d in outcome_data:
        sym = "↑" if d["move"] > 0 else "↓"
        moved = abs(d["move"]) >= LINE_DRIFT_PP
        move_tag = f"  <b>{sym}{abs(d['move']):.0f}pts</b>" if moved else f"  {d['move']:+.0f}pts"

        # Plain-English outcome label — use _nmatch to handle Pinnacle/ESPN name divergence
        # e.g. Pinnacle returns "USA" but ESPN gives home="United States"
        if d["name"] == "Draw":
            label = "It ends in a draw"
        elif _nmatch(d["name"], home):
            label = f"{home} wins"
        elif _nmatch(d["name"], away):
            label = f"{away} wins"
        else:
            label = f"{d['name']} wins"  # fallback: use Pinnacle's name verbatim

        lines.append(f"{d['flag']} <b>{label}</b>")
        lines.append(f"   {d['prev']:.0%} → <b>{d['now']:.0%}</b>{move_tag}")

        if d["poly"] is not None:
            gap = d["gap"]
            if gap is not None and abs(gap) >= PM_GAP_PP:
                direction = "cheaper" if gap > 0 else "pricier"
                action = "BUY" if gap > 0 else "SELL"
                lines.append(f"   Polymarket: {d['poly']:.0%}  ← <b>{abs(gap):.0f}pts {direction} than Vegas</b>")
                trade_signals.append(
                    f"→ <b>{action} {d['name']} YES</b> on Polymarket at {d['poly']:.0%}  (Vegas says {d['now']:.0%})"
                )
            else:
                lines.append(f"   Polymarket: {d['poly']:.0%}  (in line with Vegas)")

            # Kalshi 3rd leg
            if kal is not None:
                lbl2 = pin_to_label.get(d["name"], "")
                kal_p = kal.get(lbl2)
                if kal_p and kal_p > 0:
                    kal_gap = (d["now"] - kal_p) * 100
                    if abs(kal_gap) >= PM_GAP_PP:
                        direction = "cheaper" if kal_gap > 0 else "pricier"
                        action = "BUY" if kal_gap > 0 else "SELL"
                        lines.append(f"   Kalshi:      {kal_p:.0%}  ← <b>{abs(kal_gap):.0f}pts {direction} than Vegas</b>")
                        trade_signals.append(
                            f"→ <b>{action} {d['name']} YES</b> on Kalshi at {kal_p:.0%}  (Vegas says {d['now']:.0%})"
                        )
                    else:
                        lines.append(f"   Kalshi:      {kal_p:.0%}  (in line with Vegas)")
        lines.append("")

    if trade_signals:
        lines.append("━━━━━━━━━━━━━━━━")
        lines.append("💰 <b>Polymarket hasn't caught up yet:</b>")
        lines.extend(trade_signals)

    send_telegram("\n".join(lines))
    print(f"[line_drift] Alert sent: {gid}", flush=True)


# ── Alert 3: CLOB Whale Wall ──────────────────────────────────────────────────
def check_whale_walls(conn: sqlite3.Connection, game: Dict,
                      tokens: Dict[str, Tuple[str, float]]) -> None:
    if not tokens:
        return
    gid = game["game_id"]
    home, away = game["home_team"], game["away_team"]
    detail = game.get("detail", "")
    now_s = datetime.now(timezone.utc).timestamp()
    now_ts = datetime.now(timezone.utc).isoformat()

    for label, (tid, _) in tokens.items():
        name = {"home": home, "away": away, "draw": "Draw"}.get(label, label)

        # Dedup: skip if alerted within WHALE_DEDUP_S
        row = conn.execute(
            "SELECT last_alert_ts FROM soccer_whale_dedup WHERE game_id=? AND outcome=?",
            (gid, name)
        ).fetchone()
        if row:
            try:
                last_ts = datetime.fromisoformat(row["last_alert_ts"])
                if (now_s - last_ts.timestamp()) < WHALE_DEDUP_S:
                    continue
            except Exception:
                pass

        book = fetch_book(tid)
        if not book:
            continue

        # Pass current PM mid price so we ignore deep resting orders far from market
        _, current_mid = tokens[label]
        wall = find_whale_wall(book, current_mid=current_mid)
        if not wall:
            continue

        side, price, size = wall
        plain = "It ends in a draw" if name == "Draw" else f"{name} wins"
        if side == "ASK":
            who = "selling"
            implication = f"They think <b>{plain}</b> is <i>unlikely</i> — price is unlikely to rise past {price:.0%} until this clears."
            action = f"→ If you want <b>{plain}</b>, buy below {price:.0%}"
        else:
            who = "buying"
            implication = f"They think <b>{plain}</b> is <i>likely</i> — floor is being defended at {price:.0%}."
            action = f"→ There's strong support here. Buying <b>{plain}</b> near {price:.0%} is low-risk entry."
        msg = (
            f"🐋 <b>BIG MONEY SPOTTED</b>  —  {home} vs {away}  ({detail})\n"
            f"\n"
            f"Someone is {who} <b>${size:,.0f}</b> at <b>{price:.0%}</b> on <b>{plain}</b>\n"
            f"\n"
            f"{implication}\n"
            f"\n"
            f"{action}"
        )
        send_telegram(msg)
        print(f"[whale_wall] {name} {side} {size:,.0f}@{price:.2f}", flush=True)

        conn.execute("""
            INSERT OR REPLACE INTO soccer_whale_dedup (game_id, outcome, last_alert_ts)
            VALUES (?, ?, ?)
        """, (gid, name, now_ts))
        conn.commit()


# ── Alert 4: Edge Inversion ───────────────────────────────────────────────────
def check_edge_inversion(conn: sqlite3.Connection, game: Dict,
                         tokens: Dict[str, Tuple[str, float]],
                         pin: Optional[Dict]) -> None:
    if not pin or not tokens:
        return
    home, away = game["home_team"], game["away_team"]
    now_ts = datetime.now(timezone.utc).isoformat()

    rows = conn.execute("""
        SELECT id, market, side, entry_price, entry_book_prob
        FROM shadow_trades
        WHERE (resolved IS NULL OR resolved = 0)
          AND (close_reason IS NULL OR close_reason = '')
          AND strategy = 'soccer_match_3way'
          AND market LIKE ? AND market LIKE ?
    """, (f"%{home}%", f"%{away}%")).fetchall()

    for row in rows:
        market = row["market"]
        entry_price = row["entry_price"] or 0

        # Map market text → outcome label
        label = None
        ml = market.lower()
        if "draw" in ml:
            label = "draw"
        elif _nmatch(home, ml):
            label = "home"
        elif _nmatch(away, ml):
            label = "away"

        if not label or label not in tokens:
            continue

        _, current_poly = tokens[label]
        pin_name = {"home": home, "away": away, "draw": "Draw"}[label]
        pin_prob = pin.get(pin_name, 0)
        current_edge = pin_prob - current_poly
        entry_edge = (row["entry_book_prob"] or pin_prob) - entry_price

        if current_edge < EDGE_FLOOR:
            reason = "edge_inverted" if current_edge < 0 else "edge_below_floor"
            conn.execute("""
                UPDATE shadow_trades
                SET close_reason = ?, last_checked_at = ?
                WHERE id = ?
            """, (reason, now_ts, row["id"]))
            conn.commit()

            pin_name_plain = {"home": home, "away": away, "draw": "It ends in a draw"}[label]
            plain = pin_name_plain if label == "draw" else f"{pin_name} wins"
            if current_edge < 0:
                verdict = "❌ <b>The edge has flipped</b> — Polymarket is now <i>more expensive</i> than Vegas. The original reason to buy is gone."
                rec = "→ <b>Consider exiting</b> this position. You'd be buying high now."
            else:
                verdict = "⚠️ <b>The edge has mostly closed</b> — Polymarket has nearly caught up to Vegas."
                rec = "→ <b>Consider taking profit</b>. Most of the gap has been captured."

            entry_pct = int(round(entry_price * 100))
            now_poly_pct = int(round(current_poly * 100))
            vegas_pct = int(round(pin_prob * 100))
            orig_gap = int(round(entry_edge * 100))

            msg = (
                f"🚨 <b>TRADE UPDATE</b>  —  <b>{plain}</b>\n"
                f"\n"
                f"You entered at <b>{entry_pct}¢</b>  (gap vs Vegas was <b>{orig_gap:+d}pts</b>)\n"
                f"Now:  Polymarket <b>{now_poly_pct}¢</b>  |  Vegas <b>{vegas_pct}%</b>\n"
                f"\n"
                f"{verdict}\n"
                f"{rec}"
            )
            send_telegram(msg)
            print(f"[edge_inversion] Trade {row['id']} edge gone: {current_edge:+.2%}", flush=True)


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    conn = get_db()
    migrate(conn)

    now_month = datetime.now(timezone.utc).month
    any_active = False

    for league in LEAGUE_CONFIGS:
        if now_month not in league["active_months"]:
            continue

        espn_path  = league["espn_path"]
        odds_key   = league["odds_key"]
        pm_tag     = league["pm_tag"]
        kalshi_s   = league["kalshi_series"]  # None = skip Kalshi leg
        lname      = league["name"]

        games = fetch_espn_games(espn_path)
        active = [g for g in games if g["status"] == "in"]

        if not active:
            print(f"[soccer_live_monitor] {lname}: no active games.", flush=True)
            continue

        any_active = True
        print(f"[soccer_live_monitor] {lname}: {len(active)} active game(s)", flush=True)

        for game in active:
            home, away = game["home_team"], game["away_team"]
            print(f"[soccer_live_monitor] → {home} vs {away}  ({game['detail']})", flush=True)

            # Fetch all platforms in parallel
            f_pin = _EXECUTOR.submit(fetch_pinnacle, home, away, odds_key)
            f_ev  = _EXECUTOR.submit(fetch_poly_event, home, away, pm_tag)
            f_kal = _EXECUTOR.submit(fetch_kalshi_wc, home, away, kalshi_s) if kalshi_s else None

            try:
                pin = f_pin.result(timeout=15)
            except Exception as e:
                print(f"[monitor] fetch_pinnacle failed: {e}", flush=True)
                pin = None
            try:
                ev = f_ev.result(timeout=15)
            except Exception as e:
                print(f"[monitor] fetch_poly_event failed: {e}", flush=True)
                ev = None
            try:
                kal = f_kal.result(timeout=15) if f_kal else None
            except Exception as e:
                print(f"[monitor] fetch_kalshi_wc failed: {e}", flush=True)
                kal = None

            tokens = extract_tokens(ev, home, away) if ev else {}

            if tokens:
                mc_register_tokens([tid for tid, _ in tokens.values()])

            check_goal_trigger(conn, game, pin, ev, tokens)
            check_line_drift(conn, game, pin, tokens, kal)
            check_whale_walls(conn, game, tokens)
            check_edge_inversion(conn, game, tokens, pin)

    if not any_active:
        print("[soccer_live_monitor] No active games across any league.", flush=True)

    conn.close()


if __name__ == "__main__":
    main()

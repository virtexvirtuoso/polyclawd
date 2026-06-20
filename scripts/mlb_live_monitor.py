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

# ── Config ────────────────────────────────────────────────────────────────────
ESPN_MLB       = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"
POLY_EVENTS    = "https://gamma-api.polymarket.com/events"
ODDS_API_BASE  = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/"
CLOB_BOOK      = "https://clob.polymarket.com/book"

ODDS_API_KEY   = os.environ.get("ODDS_API_KEY", "")
LINE_DRIFT_PP  = 8.0     # pp shift to fire drift alert (MLB swings more than soccer)
WHALE_SIZE     = 30000   # CLOB book wall threshold (lower liquidity than WC)
WHALE_DEDUP_S  = 1800    # suppress re-alert for same wall within 30 min
EDGE_FLOOR     = 0.02    # close shadow trade if edge drops below 2pp
PM_GAP_PP      = 6.0     # min pp gap between PM and Vegas to flag in alerts

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
            game_id  TEXT,
            outcome  TEXT,
            devig    REAL,
            ts       TEXT,
            PRIMARY KEY (game_id, outcome)
        );
        CREATE TABLE IF NOT EXISTS mlb_whale_dedup (
            game_id       TEXT,
            outcome       TEXT,
            last_alert_ts TEXT,
            PRIMARY KEY (game_id, outcome)
        );
    """)
    conn.commit()


# ── HTTP ──────────────────────────────────────────────────────────────────────
def _get(url: str, params: Optional[dict] = None, timeout: int = 12) -> Optional[object]:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
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
def fetch_espn_games() -> List[Dict]:
    data = _get(ESPN_MLB)
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

            for c in comp.get("competitors", []):
                name = c.get("team", {}).get("displayName", "")
                score = int(c.get("score", 0) or 0)
                # Pitcher from probables list
                pitcher = ""
                for p in c.get("probables", []):
                    pitcher = p.get("athlete", {}).get("displayName", "")
                    break

                if c.get("homeAway") == "home":
                    home, hs, home_pitcher = name, score, pitcher
                else:
                    away, as_, away_pitcher = name, score, pitcher

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
    """Returns {team_name: devigged_prob} — 2-way (no draw for MLB)."""
    if not ODDS_API_KEY:
        return None
    data = _get(ODDS_API_BASE, {
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
                outcomes = [o for o in mkt["outcomes"] if o.get("price", 0) != 0]
                if not outcomes:
                    continue
                raw = {o["name"]: imp(o["price"]) for o in outcomes}
                total = sum(raw.values())
                if total < 0.1:
                    return None
                return {k: v / total for k, v in raw.items()}
    return None


# ── Polymarket ────────────────────────────────────────────────────────────────
def fetch_poly_event(home: str, away: str) -> Optional[Dict]:
    """
    Paginate baseball events starting at offset 200 (offsets 0-199 are futures/specials).
    MLB game markets close at first pitch — return even closed markets for pre-game price reference.
    Filter by today's endDate to avoid matching wrong-date games with same teams.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for offset in range(200, 2100, 100):
        data = _get(POLY_EVENTS, {"tag_slug": "baseball", "limit": 100, "offset": offset})
        if not data or not isinstance(data, list):
            break
        for ev in data:
            t = ev.get("title", "").lower()
            if " vs" not in t:
                continue
            if not (_nmatch(home, t) and _nmatch(away, t)):
                continue
            # Verify endDate is today (game markets end on game day)
            for m in ev.get("markets", [])[:1]:
                end = m.get("endDate", "")
                if today in end:
                    return ev
    return None


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
        lines.append(f"On the mound: <b>{hp or '?'}</b> vs <b>{ap or '?'}</b>")

    if pin:
        lines.append("")
        lines.append("Vegas now thinks:")
        for name, prob in pin.items():
            tag = "(home)" if _nmatch(name, home) else "(away)"
            lines.append(f"  {name} wins: <b>{prob:.0%}</b> {tag}")

    trade_signals: List[str] = []
    if tokens and pin:
        lines.append("")
        lines.append("Is Polymarket keeping up?")
        for label, name in [("home", home), ("away", away)]:
            if label not in tokens:
                continue
            tid, poly_p = tokens[label]
            pin_p = next((v for k, v in pin.items() if _nmatch(k, name)), None)
            if pin_p is None:
                continue
            gap = (pin_p - poly_p) * 100
            if abs(gap) >= PM_GAP_PP:
                direction = "cheaper" if gap > 0 else "pricier"
                action = "BUY" if gap > 0 else "SELL"
                lines.append(f"  ⚠️ <b>{name} wins</b>: Polymarket {poly_p:.0%}  ← <b>{abs(gap):.0f}pts {direction} than Vegas</b>")
                trade_signals.append(f"→ <b>{action} {name} wins YES</b> at {poly_p:.0%}  (Vegas: {pin_p:.0%})")
            else:
                lines.append(f"  ✅ <b>{name} wins</b>: Polymarket {poly_p:.0%}  (matches Vegas)")

    if trade_signals:
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━")
        lines.append("💰 <b>Polymarket hasn't adjusted yet:</b>")
        lines.extend(trade_signals)

    # Post-run whale depth check — parallel book fetches
    if tokens:
        run_labels = [(lbl, nm) for lbl, nm in [("home", home), ("away", away)] if lbl in tokens]
        book_futures = {lbl: _EXECUTOR.submit(fetch_book, tokens[lbl][0]) for lbl, _ in run_labels}
        walls_found: List[str] = []
        for label, name in run_labels:
            try:
                book = book_futures[label].result(timeout=15)
            except Exception:
                book = None
            if not book:
                continue
            _, current_mid = tokens[label]
            wall = find_whale_wall(book, current_mid=current_mid)
            if wall:
                side, price, size = wall
                action_desc = "selling" if side == "ASK" else "buying"
                walls_found.append(f"  🐋 Someone is {action_desc} <b>${size:,.0f}</b> at {price:.0%} on <b>{name} wins</b>")
        if walls_found:
            lines.append("")
            lines.append("Big money spotted:")
            lines.extend(walls_found)

    send_telegram("\n".join(lines))
    print(f"[mlb_monitor] Run/pitcher alert: {home} {hs}-{as_} {away}", flush=True)


# ── Alert 2: Line Drift ───────────────────────────────────────────────────────
def check_line_drift(conn: sqlite3.Connection, game: Dict,
                     pin: Optional[Dict], tokens: Dict) -> None:
    if not pin:
        return
    gid = game["game_id"]
    home, away = game["home_team"], game["away_team"]
    hs, as_ = game["home_score"], game["away_score"]
    detail = game.get("detail", "")
    now_ts = datetime.now(timezone.utc).isoformat()
    fired_ts = datetime.now(timezone.utc).strftime("%H:%M UTC")

    any_drift = False
    outcome_data = []

    for outcome, prob in pin.items():
        row = conn.execute(
            "SELECT devig FROM mlb_line_snap WHERE game_id=? AND outcome=?", (gid, outcome)
        ).fetchone()

        prev = float(row["devig"]) if row else prob
        move = (prob - prev) * 100
        if abs(move) >= LINE_DRIFT_PP:
            any_drift = True

        label = "home" if _nmatch(outcome, home) else "away"
        poly_p = tokens[label][1] if label in tokens else None
        gap = (prob - poly_p) * 100 if poly_p is not None else None

        outcome_data.append({
            "name": outcome, "prev": prev, "now": prob, "move": move,
            "poly": poly_p, "gap": gap,
        })

        conn.execute("""
            INSERT OR REPLACE INTO mlb_line_snap (game_id, outcome, devig, ts)
            VALUES (?, ?, ?, ?)
        """, (gid, outcome, prob, now_ts))
    conn.commit()

    if not any_drift:
        return

    score_line = f"{home} <b>{hs}–{as_}</b> {away}  ({detail})  <i>{fired_ts}</i>"
    lines = [
        f"⚡ <b>ODDS MOVED</b> — MLB | {score_line}",
        "",
        "Vegas shifted big:",
        "",
    ]
    trade_signals: List[str] = []

    for d in outcome_data:
        sym = "↑" if d["move"] > 0 else "↓"
        moved = abs(d["move"]) >= LINE_DRIFT_PP
        move_tag = f"  <b>{sym}{abs(d['move']):.0f}pts</b>" if moved else f"  {d['move']:+.0f}pts"
        label = f"{d['name']} wins"

        lines.append(f"<b>{label}</b>")
        lines.append(f"   {d['prev']:.0%} → <b>{d['now']:.0%}</b>{move_tag}")

        if d["poly"] is not None and d["gap"] is not None:
            if abs(d["gap"]) >= PM_GAP_PP:
                direction = "cheaper" if d["gap"] > 0 else "pricier"
                action = "BUY" if d["gap"] > 0 else "SELL"
                lines.append(f"   Polymarket: {d['poly']:.0%}  ← <b>{abs(d['gap']):.0f}pts {direction} than Vegas</b>")
                trade_signals.append(f"→ <b>{action} {label} YES</b> at {d['poly']:.0%}  (Vegas: {d['now']:.0%})")
            else:
                lines.append(f"   Polymarket: {d['poly']:.0%}  (in line)")
        else:
            lines.append(f"   Polymarket: —")
        lines.append("")

    if trade_signals:
        lines.append("━━━━━━━━━━━━━━━━")
        lines.append("💰 <b>PM hasn't caught up yet:</b>")
        lines.extend(trade_signals)

    send_telegram("\n".join(lines))
    print(f"[mlb_monitor] Line drift alert: {gid}", flush=True)


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

        _, current_poly = tokens[label]
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

        if tokens:
            mc_register_tokens([tid for tid, _ in tokens.values()])

        check_run_trigger(conn, game, pin, ev, tokens)
        check_line_drift(conn, game, pin, tokens)
        check_edge_inversion(conn, game, tokens, pin)

    conn.close()


if __name__ == "__main__":
    main()

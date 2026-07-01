#!/usr/bin/env python3
"""
cross_sport_drift.py — Cross-sport Pinnacle line drift scanner.

Fires every 5 minutes via tick_5min. Monitors MLB, WC Soccer, MLS, and UFC
for significant Vegas line shifts. On move > threshold since last snapshot,
fires a unified drift alert with Polymarket CLOB price comparison.

No ESPN score trigger — pure Vegas line movement detection.
Credits: bookmakers=pinnacle → 1 credit per sport per tick
         4 sports × 12 ticks/hr × 16hr/day ≈ 768 credits/day
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

# ── Config ───────────────────────────────────────────────────────────────────
ODDS_API_KEY  = os.environ.get("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports"
GAMMA_API     = "https://gamma-api.polymarket.com/events"
CLOB_BOOK     = "https://clob.polymarket.com/book"

DB_PATH       = BASE_DIR / "storage" / "shadow_trades.db"
MC_HOST, MC_PORT = "localhost", 11211

# Module-level executor — shared across sports and ticks
_EXECUTOR = ThreadPoolExecutor(max_workers=8)

# ── Sport configs ─────────────────────────────────────────────────────────────
# Each config drives a full scan cycle for that sport.
# has_draw: whether the market has a 3-way (home/draw/away) outcome
# drift_pp: minimum pp shift to fire an alert
# active_months: skip entirely outside these months (saves credits)
SPORT_CONFIGS: List[Dict] = [
    {
        "name": "MLB",
        "odds_key": "baseball_mlb",
        "pm_tag": "baseball",
        "has_draw": False,
        "drift_pp": 8.0,
        "active_months": [4, 5, 6, 7, 8, 9, 10],
        "edge_floor_pp": 6.0,
    },
    {
        "name": "WC Soccer",
        "odds_key": "soccer_fifa_world_cup",
        "pm_tag": "fifa-world-cup",
        "has_draw": True,
        "drift_pp": 5.0,
        "active_months": [6, 7],
        "edge_floor_pp": 6.0,
    },
    {
        "name": "MLS",
        "odds_key": "soccer_usa_mls",
        "pm_tag": "mls",
        "has_draw": True,
        "drift_pp": 8.0,
        "active_months": [3, 4, 5, 6, 7, 8, 9, 10, 11],
        "edge_floor_pp": 6.0,
    },
    {
        "name": "UFC",
        "odds_key": "mma_mixed_martial_arts",
        "pm_tag": "ufc",
        "has_draw": False,
        "drift_pp": 10.0,
        "active_months": list(range(1, 13)),
        "edge_floor_pp": 6.0,
    },
]

# Team name aliases for cross-platform matching
ALIASES: Dict[str, List[str]] = {
    "usa": ["united states"],
    "united states": ["usa"],
    "korea": ["south korea", "korea republic"],
    "south korea": ["korea republic"],
    "korea republic": ["south korea"],
    "ivory coast": ["cote d'ivoire"],
    "cote d'ivoire": ["ivory coast"],
    "turkiye": ["turkey"],
    "turkey": ["turkiye"],
}

# Sport emoji map
SPORT_EMOJI: Dict[str, str] = {
    "MLB": "⚾",
    "WC Soccer": "⚽",
    "MLS": "⚽",
    "UFC": "🥊",
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
        CREATE TABLE IF NOT EXISTS sport_line_snap (
            sport    TEXT,
            game_id  TEXT,
            outcome  TEXT,
            devig    REAL,
            ts       TEXT,
            PRIMARY KEY (sport, game_id, outcome)
        );
        CREATE TABLE IF NOT EXISTS sport_drift_dedup (
            sport         TEXT,
            game_id       TEXT,
            outcome       TEXT,
            last_alert_ts TEXT,
            PRIMARY KEY (sport, game_id, outcome)
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
        print(f"[cross_sport_drift] GET {url[:70]} → {e}", flush=True)
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


# ── Name matching ─────────────────────────────────────────────────────────────
def _nmatch(a: str, b: str) -> bool:
    a, b = a.lower().strip(), b.lower().strip()
    if a == b or a in b or b in a:
        return True
    for alias in ALIASES.get(a, []):
        if alias in b or b in alias:
            return True
    return False


def _game_id(home: str, away: str, commence_time: str = "") -> str:
    """Include date so same matchup on consecutive days gets distinct IDs."""
    date_suffix = ""
    if commence_time:
        try:
            date_suffix = "_" + commence_time[:10]  # "2026-06-19"
        except Exception:
            pass
    h = home.lower().replace(" ", "_")
    a = away.lower().replace(" ", "_")
    return f"{h}_{a}{date_suffix}"


# ── Pinnacle odds ─────────────────────────────────────────────────────────────
def _imp(price: int) -> float:
    p = int(price)
    return (100 / (100 + p)) if p > 0 else (-p / (-p + 100))


def fetch_pinnacle_sport(odds_key: str) -> List[Dict]:
    """Fetch all Pinnacle h2h games for a sport. Returns list of game dicts.

    Each game dict:
      {"home": str, "away": str, "game_id": str,
       "outcomes": {name: devigged_prob}, "commence_time": str}
    """
    if not ODDS_API_KEY:
        return []
    data = _get(f"{ODDS_API_BASE}/{odds_key}/odds", {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "h2h",
        "oddsFormat": "american",
        "bookmakers": "pinnacle",
    })
    if not isinstance(data, list):
        return []

    games = []
    for event in data:
        home = event.get("home_team", "")
        away = event.get("away_team", "")
        if not home or not away:
            continue
        for bm in event.get("bookmakers", []):
            if bm.get("key") != "pinnacle":
                continue
            for mkt in bm.get("markets", []):
                if mkt.get("key") != "h2h":
                    continue
                outcomes = mkt.get("outcomes", [])
                valid = [o for o in outcomes if o.get("price", 0) != 0]
                if not valid:
                    continue
                raw = {o["name"]: _imp(o["price"]) for o in valid}
                total = sum(raw.values())
                if total < 0.1:
                    continue
                devigged = {k: v / total for k, v in raw.items()}
                ct = event.get("commence_time", "")
                games.append({
                    "home": home,
                    "away": away,
                    "game_id": _game_id(home, away, ct),
                    "outcomes": devigged,
                    "commence_time": ct,
                })
    return games


# ── Polymarket CLOB ───────────────────────────────────────────────────────────
def fetch_poly_mid(home: str, away: str, pm_tag: str, has_draw: bool) -> Dict[str, Optional[float]]:
    """Fetch PM mid price for each outcome. Returns {outcome_label: mid_price}."""
    data = _get(GAMMA_API, {"tag_slug": pm_tag, "active": "true", "limit": 200})
    if not data:
        return {}

    for ev in (data if isinstance(data, list) else []):
        t = ev.get("title", "").lower()
        if not (_nmatch(home, t) and _nmatch(away, t)):
            continue

        result: Dict[str, Optional[float]] = {}
        for m in ev.get("markets", []):
            q = m.get("question", "").lower()
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
            if not prices or not tids:
                continue

            yes_price = float(prices[0])
            yes_tid = tids[0]

            # Try memcached book first (low latency, populated by WS service)
            raw_book = _mc_get(f"poly:book:{yes_tid}")
            if raw_book:
                try:
                    book = json.loads(raw_book)
                    bids = book.get("bids", [])
                    asks = book.get("asks", [])
                    best_bid = float(bids[0]["price"]) if bids else 0.0
                    best_ask = float(asks[0]["price"]) if asks else 1.0
                    yes_price = (best_bid + best_ask) / 2
                except Exception:
                    pass  # fallback to gamma price

            if has_draw and "draw" in q:
                result["Draw"] = yes_price
            elif "win" in q and _nmatch(home, q) and not _nmatch(away, q):
                result[home] = yes_price
            elif "win" in q and _nmatch(away, q) and not _nmatch(home, q):
                result[away] = yes_price
            # UFC: "to win" fight (no draw)
            elif not has_draw and _nmatch(home, q):
                result[home] = yes_price
            elif not has_draw and _nmatch(away, q):
                result[away] = yes_price

        if result:
            return result

    return {}


# ── Drift detection ───────────────────────────────────────────────────────────
def check_sport_drift(conn: sqlite3.Connection, cfg: Dict, games: List[Dict]) -> None:
    """Compare current Pinnacle lines vs last snapshot. Fire alert on drift > threshold."""
    sport       = cfg["name"]
    pm_tag      = cfg["pm_tag"]
    has_draw    = cfg["has_draw"]
    drift_pp    = cfg["drift_pp"]
    edge_floor  = cfg["edge_floor_pp"]
    emoji       = SPORT_EMOJI.get(sport, "🎯")
    now_ts      = datetime.now(timezone.utc).isoformat()
    now_s       = datetime.now(timezone.utc).timestamp()

    for game in games:
        gid   = game["game_id"]
        home  = game["home"]
        away  = game["away"]
        probs = game["outcomes"]  # {outcome_name: devigged_prob}

        # Compare vs last snapshot
        any_drift = False
        outcome_data = []

        for outcome, prob in probs.items():
            row = conn.execute(
                "SELECT devig FROM sport_line_snap WHERE sport=? AND game_id=? AND outcome=?",
                (sport, gid, outcome),
            ).fetchone()

            prev = float(row["devig"]) if row else prob
            move = (prob - prev) * 100

            if abs(move) >= drift_pp:
                any_drift = True

            outcome_data.append({
                "name": outcome,
                "prev": prev,
                "now": prob,
                "move": move,
            })

            # Update snapshot
            conn.execute("""
                INSERT OR REPLACE INTO sport_line_snap (sport, game_id, outcome, devig, ts)
                VALUES (?, ?, ?, ?, ?)
            """, (sport, gid, outcome, prob, now_ts))

        conn.commit()

        if not any_drift:
            continue

        # Fetch PM mids in parallel with output already computed
        f_pm = _EXECUTOR.submit(fetch_poly_mid, home, away, pm_tag, has_draw)
        try:
            poly_mids = f_pm.result(timeout=15)
        except Exception:
            poly_mids = {}

        # Build alert
        fired_ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
        lines = [
            f"{emoji} <b>ODDS MOVED</b> — {sport} | {home} vs {away}  <i>{fired_ts}</i>",
            "",
            "Vegas shifted big on this game:",
            "",
        ]

        trade_signals: List[str] = []

        for d in outcome_data:
            sym = "↑" if d["move"] > 0 else "↓"
            moved = abs(d["move"]) >= drift_pp
            move_tag = f"  <b>{sym}{abs(d['move']):.0f}pts</b>" if moved else f"  {d['move']:+.0f}pts"

            # Plain label
            if d["name"] == "Draw":
                label = "It ends in a draw"
            elif _nmatch(d["name"], home):
                label = f"{home} wins"
            elif _nmatch(d["name"], away):
                label = f"{away} wins"
            else:
                label = f"{d['name']} wins"

            lines.append(f"<b>{label}</b>")
            lines.append(f"   {d['prev']:.0%} → <b>{d['now']:.0%}</b>{move_tag}")

            # PM comparison
            pm_p = poly_mids.get(d["name"])
            if pm_p is None:
                # Try fuzzy match
                for k, v in poly_mids.items():
                    if _nmatch(k, d["name"]) or _nmatch(d["name"], k):
                        pm_p = v
                        break

            if pm_p is not None:
                gap = (d["now"] - pm_p) * 100
                if abs(gap) >= edge_floor:
                    direction = "cheaper" if gap > 0 else "pricier"
                    action = "BUY" if gap > 0 else "SELL"
                    lines.append(f"   Polymarket: {pm_p:.0%}  ← <b>{abs(gap):.0f}pts {direction} than Vegas</b>")
                    trade_signals.append(
                        f"→ <b>{action} {label} YES</b> at {pm_p:.0%}  (Vegas: {d['now']:.0%})"
                    )
                else:
                    lines.append(f"   Polymarket: {pm_p:.0%}  (in line)")
            lines.append("")

        if trade_signals:
            lines.append("━━━━━━━━━━━━━━━━")
            lines.append("💰 <b>PM hasn't caught up yet:</b>")
            lines.extend(trade_signals)

        send_telegram("\n".join(lines))
        print(f"[cross_sport_drift] Alert: {sport} {home} vs {away}", flush=True)


# ── Main ──────────────────────────────────────────────────────────────────────
def run() -> None:
    if not ODDS_API_KEY:
        print("[cross_sport_drift] ODDS_API_KEY not set — skip", flush=True)
        return

    conn = get_db()
    migrate(conn)

    now_month = datetime.now(timezone.utc).month

    # Filter to sports active this month
    active_cfgs = [c for c in SPORT_CONFIGS if now_month in c["active_months"]]
    if not active_cfgs:
        conn.close()
        return

    print(f"[cross_sport_drift] Scanning {len(active_cfgs)} sport(s)", flush=True)

    # Fetch all Pinnacle lines in parallel
    futures = {
        cfg["name"]: _EXECUTOR.submit(fetch_pinnacle_sport, cfg["odds_key"])
        for cfg in active_cfgs
    }

    for cfg in active_cfgs:
        sport = cfg["name"]
        try:
            games = futures[sport].result(timeout=15)
        except Exception as e:
            print(f"[cross_sport_drift] {sport} fetch failed: {e}", flush=True)
            games = []

        if not games:
            print(f"[cross_sport_drift] {sport}: no games found", flush=True)
            continue

        print(f"[cross_sport_drift] {sport}: {len(games)} game(s)", flush=True)
        check_sport_drift(conn, cfg, games)

    conn.close()


if __name__ == "__main__":
    run()

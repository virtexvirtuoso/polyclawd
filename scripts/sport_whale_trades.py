#!/usr/bin/env python3
"""
sport_whale_trades.py — Multi-sport whale trade alert for active games/fights.

Ports soccer_whale_trades.py to all sports with PM CLOB liquidity:
  - WC Soccer ($5k threshold)
  - MLB ($3k)
  - UFC ($5k)
  - MLS ($2k)
  - NBA Playoffs ($3k) — active_months gated

Sources (priority order):
  1. poly:trade:{token_id} from memcached (if WS is streaming)
  2. data-api.polymarket.com/trades global feed (~30s lag)

Called by scheduler.task_sport_whale_trades() every 60s.
State: storage/shadow_trades.db (auto-migrated tables)
"""

from __future__ import annotations
from config.polymarket_urls import POLYMARKET_DATA_API as DATA_API  # polyproxy: central URL config
from config.polymarket_urls import clob_url, data_url  # polyproxy: central URL config

import json
import os
import socket
import sqlite3
import sys
import time
import urllib.parse

import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from config.polymarket_urls import GAMMA_API, CLOB_API  # polyproxy: central URL config

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))
from odds.monitor_gate import gated_fetch_json, LIVE_BOOKS

from scripts.alert_formatter import send_telegram

# Mirror scripts/smart_wallet_alert.py — suppress near-resolution follows.
# Was referenced without definition -> NameError in prod (audit 2026-07-10).
NEAR_SETTLED_HI = 0.90
NEAR_SETTLED_LO = 0.10

# ── Sport configs ─────────────────────────────────────────────────────────────
SPORT_CONFIGS = [
    {
        "name": "WC Soccer",
        "espn_path": "soccer/fifa.world/scoreboard",
        "pm_tag": "fifa-world-cup",
        "whale_usdc": 5_000,
        "active_months": [6, 7],
        "score_table": "soccer_score_snap",
        "has_draw": True,
    },
    {
        "name": "MLS",
        "espn_path": "soccer/usa.1/scoreboard",
        "pm_tag": "mls",
        "whale_usdc": 2_000,
        "active_months": [3, 4, 5, 6, 7, 8, 9, 10, 11],
        "score_table": "soccer_score_snap",
        "has_draw": True,
    },
    {
        "name": "MLB",
        "espn_path": "baseball/mlb/scoreboard",
        "pm_tag": "baseball",
        "whale_usdc": 3_000,
        "active_months": [3, 4, 5, 6, 7, 8, 9, 10],
        "score_table": "mlb_score_snap",
        "has_draw": False,
    },
    {
        "name": "UFC",
        "espn_path": "mma/ufc/scoreboard",
        "pm_tag": "ufc",
        "whale_usdc": 5_000,
        "active_months": list(range(1, 13)),  # year-round
        "score_table": "ufc_fight_snap",
        "has_draw": False,
    },
    {
        "name": "NBA",
        "espn_path": "basketball/nba/scoreboard",
        "pm_tag": "nba",
        "whale_usdc": 3_000,
        "active_months": [4, 5, 6, 10, 11, 12, 1, 2, 3],
        "score_table": None,  # NBA uses ESPN directly, no persistent snap table
        "has_draw": False,
    },
]

# ── Config ────────────────────────────────────────────────────────────────────
ESPN_BASE       = "https://site.api.espn.com/apis/site/v2/sports"

POLL_LOOKBACK_S   = 90
DATA_API_PAGE_SZ  = 500
DATA_API_MAX_PAGES = 10

MC_HOST, MC_PORT = "localhost", 11211
DB_PATH = BASE_DIR / "storage" / "shadow_trades.db"

# Team/fighter aliases for cross-platform matching
ALIASES: Dict[str, List[str]] = {
    # Soccer
    "korea": ["south korea", "korea republic"],
    "south korea": ["korea republic", "korea"],
    "usa": ["united states"],
    "united states": ["usa"],
    "ivory coast": ["cote d'ivoire"],
    # MLB
    "athletics": ["oakland athletics", "a's"],
    "white sox": ["chicago white sox"],
    "red sox": ["boston red sox"],
    "blue jays": ["toronto blue jays"],
    "yankees": ["new york yankees"],
    "mets": ["new york mets"],
    "dodgers": ["los angeles dodgers"],
    "angels": ["los angeles angels"],
    "padres": ["san diego padres"],
    "giants": ["san francisco giants"],
    "braves": ["atlanta braves"],
    "cubs": ["chicago cubs"],
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
        CREATE TABLE IF NOT EXISTS whale_token_cache (
            sport       TEXT,
            game_id     TEXT,
            label       TEXT,
            token_id    TEXT,
            outcome_name TEXT,
            cached_at   TEXT,
            PRIMARY KEY (sport, game_id, label)
        );
        CREATE TABLE IF NOT EXISTS whale_trade_seen (
            sport       TEXT,
            token_id    TEXT,
            last_tx     TEXT,
            last_ts     INTEGER,
            PRIMARY KEY (sport, token_id)
        );
    """)
    conn.commit()

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
        print(f"[sport_whale] GET {url[:70]} → {e}", flush=True)
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
    raw = _mc_get("poly:ws:registered")
    cur: set = set()
    if raw:
        try:
            cur = set(json.loads(raw))
        except Exception:
            pass
    cur.update(token_ids)
    merged = list(cur)[:500]
    _mc_set("poly:ws:registered", json.dumps(merged), exptime=600)

def mc_get_trade(token_id: str) -> Optional[Dict]:
    raw = _mc_get(f"poly:trade:{token_id}")
    if not raw:
        return None
    try:
        return json.loads(raw)
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

# ── ESPN: get active games for any sport ──────────────────────────────────────
def fetch_espn_active(espn_path: str, sport_name: str) -> List[Dict]:
    """Fetch active games/fights from ESPN scoreboard."""
    url = f"{ESPN_BASE}/{espn_path}"
    data = _get(url)
    if not data:
        return []

    games = []
    for event in data.get("events", []):
        # UFC: competitions are individual fights
        # Team sports: competitions are games
        for comp in event.get("competitions", []):
            status = comp.get("status", {})
            state = status.get("type", {}).get("state", "")
            if state != "in":
                continue

            competitors = comp.get("competitors", [])
            if len(competitors) < 2:
                continue

            # UFC uses athlete.displayName, team sports use team.displayName
            if sport_name == "UFC":
                home = competitors[0].get("athlete", {}).get("displayName", "")
                away = competitors[1].get("athlete", {}).get("displayName", "")
            else:
                home = competitors[0].get("team", {}).get("displayName", "")
                away = competitors[1].get("team", {}).get("displayName", "")

            if not home or not away:
                continue

            # Deterministic game ID
            parts = sorted([home.lower().strip(), away.lower().strip()])
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            gid = f"{parts[0]}_vs_{parts[1]}_{today}"

            # Extract game state for alert enrichment
            detail = status.get("type", {}).get("detail", "")
            scores = {}
            for c in competitors:
                if sport_name == "UFC":
                    cname = c.get("athlete", {}).get("displayName", "")
                else:
                    cname = c.get("team", {}).get("displayName", "")
                scores[cname] = c.get("score", "0")

            games.append({
                "game_id": gid,
                "home": home,
                "away": away,
                "detail": detail,
                "scores": scores,
            })

    return games

# ── Token resolution ──────────────────────────────────────────────────────────
def get_tokens_for_game(conn: sqlite3.Connection, sport: str, game_id: str,
                        home: str, away: str, pm_tag: str,
                        has_draw: bool) -> Dict[str, Tuple[str, str]]:
    """Returns {label: (token_id, outcome_name)} — uses cache, refreshes if >1h old."""
    rows = conn.execute(
        "SELECT label, token_id, outcome_name, cached_at FROM whale_token_cache "
        "WHERE sport=? AND game_id=?",
        (sport, game_id)
    ).fetchall()

    if rows:
        cached_at = rows[0]["cached_at"]
        age_s = (datetime.now(timezone.utc).timestamp() -
                 datetime.fromisoformat(cached_at).timestamp())
        if age_s < 3600 and len(rows) >= 2:
            return {r["label"]: (r["token_id"], r["outcome_name"]) for r in rows}

    # Fetch from Gamma API — date filter prevents returning stale 2024 data
    now_utc = datetime.now(timezone.utc)
    end_min = now_utc.strftime("%Y-%m-%dT00:00:00Z")
    end_max = (now_utc + timedelta(days=2)).strftime("%Y-%m-%dT00:00:00Z")
    data = _get(f"{GAMMA_API}/events", {
        "tag_slug": pm_tag, "active": "true", "limit": 100,
        "end_date_min": end_min, "end_date_max": end_max,
    })
    if not data:
        return {}

    tokens: Dict[str, Tuple[str, str]] = {}

    def _scan_events(events: list) -> bool:
        nonlocal tokens
        for ev in events:
            t = ev.get("title", "").lower()
            if not (_nmatch(home, t) and _nmatch(away, t)):
                continue
            for m in ev.get("markets", []):
                q = m.get("question", "").lower()
                tids = m.get("clobTokenIds", [])
                if isinstance(tids, str):
                    try:
                        tids = json.loads(tids)
                    except Exception:
                        continue
                if not tids:
                    continue
                yes_tid = tids[0]
                no_tid = tids[1] if len(tids) > 1 else None
                if has_draw and "draw" in q:
                    tokens["draw"] = (yes_tid, "Draw")
                    if no_tid:
                        tokens["draw_no"] = (no_tid, "Draw")
                elif "win" in q and _nmatch(home, q) and not _nmatch(away, q):
                    tokens["home"] = (yes_tid, home)
                    if no_tid:
                        tokens["home_no"] = (no_tid, home)
                elif "win" in q and _nmatch(away, q) and not _nmatch(home, q):
                    tokens["away"] = (yes_tid, away)
                    if no_tid:
                        tokens["away_no"] = (no_tid, away)
                elif "vs" in q or "vs." in q:
                    # Generic fight/game market — YES = first-named
                    # Determine who is first-named
                    h_pos = q.find(home.lower().split()[-1])
                    a_pos = q.find(away.lower().split()[-1])
                    if h_pos >= 0 and a_pos >= 0:
                        if h_pos < a_pos:
                            tokens["home"] = (yes_tid, home)
                            if no_tid:
                                tokens["away"] = (no_tid, away)
                        else:
                            tokens["away"] = (yes_tid, away)
                            if no_tid:
                                tokens["home"] = (no_tid, home)
            if tokens:
                return True
        return False

    found = _scan_events(data if isinstance(data, list) else [])

    # Paginate if not found (UFC especially has many pages)
    if not found:
        for offset in [100, 200]:
            data2 = _get(f"{GAMMA_API}/events", {
                "tag_slug": pm_tag, "active": "true", "limit": 100, "offset": offset,
                "end_date_min": end_min, "end_date_max": end_max,
            })
            if not data2 or not isinstance(data2, list):
                break
            if _scan_events(data2):
                break

    if tokens:
        now_ts = datetime.now(timezone.utc).isoformat()
        conn.execute("DELETE FROM whale_token_cache WHERE sport=? AND game_id=?",
                      (sport, game_id))
        for label, (tid, name) in tokens.items():
            conn.execute("""
                INSERT OR REPLACE INTO whale_token_cache
                  (sport, game_id, label, token_id, outcome_name, cached_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (sport, game_id, label, tid, name, now_ts))
        conn.commit()

    return tokens

# ── Trade sources ─────────────────────────────────────────────────────────────
def get_live_ws_trade(token_id: str) -> Optional[Dict]:
    t = mc_get_trade(token_id)
    if not t:
        return None
    return {
        "tx": str(t.get("ts", "")),
        "price": float(t.get("price", 0) or 0),
        "size": float(t.get("size", 0) or 0),
        "side": str(t.get("side", "BUY")).upper(),
        "timestamp": int(float(t.get("ts", 0) or 0) / 1000),
        "source": "ws",
    }

def get_global_trades(watched_tokens: set, since_ts: int = 0) -> List[Dict]:
    if since_ts == 0:
        since_ts = int(time.time()) - POLL_LOOKBACK_S

    out = []
    for page in range(DATA_API_MAX_PAGES):
        offset = page * DATA_API_PAGE_SZ
        data = _get(f"{DATA_API}/trades", {"limit": DATA_API_PAGE_SZ, "offset": offset})
        if not data or not isinstance(data, list):
            break

        oldest_ts = int(time.time())
        for t in data:
            ts = int(t.get("timestamp", 0) or 0)
            oldest_ts = min(oldest_ts, ts)
            if ts < since_ts:
                continue
            asset = t.get("asset", "")
            if asset not in watched_tokens:
                continue
            size = float(t.get("size", 0) or 0)
            price = float(t.get("price", 0) or 0)
            out.append({
                "tx": t.get("transactionHash", ""),
                "token_id": asset,
                "price": price,
                "size_usdc": size * price,
                "side": str(t.get("side", "BUY")).upper(),
                "timestamp": ts,
                "source": "data-api",
            })

        if oldest_ts < since_ts:
            break

    return out

# ── Alert ─────────────────────────────────────────────────────────────────────
def _get_vegas_consensus(sport: str, home: str, away: str, outcome_label: str) -> Optional[float]:
    """Fetch live Vegas consensus probability for the outcome."""
    try:
        sport_key_map = {
            "WC Soccer": "soccer_fifa_world_cup",
            "MLS": "soccer_usa_mls",
            "MLB": "baseball_mlb",
            "UFC": "mma_mixed_martial_arts",
            "NBA": "basketball_nba",
        }
        sport_key = sport_key_map.get(sport)
        if not sport_key:
            return None
        api_key = os.environ.get("ODDS_API_KEY", "")
        if not api_key:
            return None
        data = gated_fetch_json(
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds",
            {"apiKey": api_key, "bookmakers": LIVE_BOOKS,
             "markets": "h2h", "oddsFormat": "decimal"},
        )
        if not data:
            return None
        # Find the matching event
        h_l, a_l = home.lower().strip(), away.lower().strip()
        for ev in data:
            eh = ev.get("home_team", "").lower().strip()
            ea = ev.get("away_team", "").lower().strip()
            if not (_nmatch(h_l, eh) or _nmatch(a_l, ea)):
                continue
            # Consensus from all books
            probs_by_outcome: Dict[str, List[float]] = {}
            for bm in ev.get("bookmakers", []):
                for mkt in bm.get("markets", []):
                    if mkt.get("key") != "h2h":
                        continue
                    for oc in mkt.get("outcomes", []):
                        name = oc.get("name", "")
                        dec = oc.get("price", 0)
                        if dec > 1:
                            probs_by_outcome.setdefault(name, []).append(1.0 / dec)
            # Match outcome_label to book outcome name
            for name, prob_list in probs_by_outcome.items():
                n_l = name.lower()
                if outcome_label == "Draw" and n_l == "draw":
                    return sum(prob_list) / len(prob_list)
                if _nmatch(outcome_label.lower().replace(" wins", ""), n_l):
                    return sum(prob_list) / len(prob_list)
            break
        return None
    except Exception:
        return None

def _get_net_flow(conn: sqlite3.Connection, sport: str, token_id: str,
                  window_s: int = 900) -> Tuple[float, float, int]:
    """Returns (buy_usd, sell_usd, trade_count) in last window_s seconds."""
    try:
        cutoff = int(time.time()) - window_s
        rows = conn.execute("""
            SELECT last_tx FROM whale_trade_seen
            WHERE sport=? AND token_id=? AND last_ts > ?
        """, (sport, token_id, cutoff)).fetchall()
        # We don't store individual trade amounts in whale_trade_seen,
        # so use the data-api for recent trades on this token
        url = data_url(f"/trades?asset_id={token_id}&limit=50")
        data = _get(url)
        if not data:
            return (0.0, 0.0, 0)
        buy_usd, sell_usd, count = 0.0, 0.0, 0
        cutoff_ts = time.time() - window_s
        for t in data:
            ts_str = t.get("match_time", t.get("timestamp", ""))
            try:
                from datetime import datetime as _dt
                ts = _dt.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if ts < cutoff_ts:
                break
            price = float(t.get("price", 0))
            size = float(t.get("size", 0))
            usd = price * size
            if usd < 100:  # skip dust
                continue
            side = str(t.get("side", "")).upper()
            if side == "BUY":
                buy_usd += usd
            else:
                sell_usd += usd
            count += 1
        return (buy_usd, sell_usd, count)
    except Exception:
        return (0.0, 0.0, 0)

def _get_wallet_intel(maker_addr: str) -> Optional[str]:
    """Check if wallet is known or new. Returns a short description."""
    if not maker_addr:
        return None
    try:
        url = data_url(f"/activity?user={maker_addr}&limit=5")
        data = _get(url)
        if not data:
            return None
        if len(data) <= 1:
            return "🆕 First trade on PM"
        # Check age of earliest trade
        oldest = data[-1]
        ts_str = oldest.get("timestamp", oldest.get("match_time", ""))
        try:
            from datetime import datetime as _dt
            oldest_ts = _dt.fromisoformat(ts_str.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - oldest_ts).days
            if age_days < 1:
                return "🆕 New wallet (< 1 day old)"
            elif age_days < 7:
                return f"📅 Wallet is {age_days}d old"
            else:
                return f"📅 Active wallet ({age_days}d old)"
        except Exception:
            return None
    except Exception:
        return None

def _get_price_after(token_id: str) -> Optional[float]:
    """Get current mid price from CLOB book."""
    try:
        url = clob_url(f"/book?token_id={token_id}")
        data = _get(url)
        if not data:
            return None
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        best_bid = float(bids[0]["price"]) if bids else 0
        best_ask = float(asks[0]["price"]) if asks else 1
        if best_bid > 0 or best_ask < 1:
            return (best_bid + best_ask) / 2
        return None
    except Exception:
        return None

def _fire_alert(sport: str, home: str, away: str, outcome: str,
                direction: str, size_usdc: float, price: float,
                lag: str, conn: Optional[sqlite3.Connection] = None,
                token_id: str = "", game_meta: Optional[Dict] = None,
                maker_addr: str = "", is_no_token: bool = False) -> None:
    lag_note = "" if lag == "live" else "  <i>(~30s delay)</i>"
    sport_emoji = {"WC Soccer": "⚽", "MLS": "⚽", "MLB": "⚾",
                   "UFC": "🥊", "NBA": "🏀"}.get(sport, "🎯")

    # ── Core line ──
    lines = [
        f"🐋 <b>WHALE TRADE</b> {sport_emoji} {sport}  —  {home} vs {away}{lag_note}",
        "",
    ]

    # ── 1. Game state context ──
    if game_meta:
        detail = game_meta.get("detail", "")
        scores = game_meta.get("scores", {})
        if scores:
            score_str = " – ".join(f"{k} {v}" for k, v in scores.items())
            lines.append(f"📊 <b>{score_str}</b>  ({detail})")
        elif detail:
            lines.append(f"📊 {detail}")

    # ── 2. The trade ──
    if is_no_token and direction == "buying NO on":
        yes_eq = 1.0 - price
        lines.append(f"💵 <b>BUY NO</b> on <b>{outcome}</b> — ${size_usdc:,.0f} @ ~{price*100:.0f}¢  (YES ~{yes_eq*100:.0f}¢)")
    else:
        lines.append(f"💵 <b>{direction.upper()} ${size_usdc:,.0f}</b> on <b>{outcome}</b> at {price:.0%}")

    # ── 3. Vegas comparison ──
    vegas_prob = _get_vegas_consensus(sport, home, away, outcome)
    if vegas_prob is not None:
        diff_pp = (price - vegas_prob) * 100
        if diff_pp > 2:
            lines.append(f"📈 Vegas says {vegas_prob:.0%} — whale paying {abs(diff_pp):.0f}pp <b>premium</b>")
        elif diff_pp < -2:
            lines.append(f"📉 Vegas says {vegas_prob:.0%} — whale getting {abs(diff_pp):.0f}pp <b>discount</b>")
        else:
            lines.append(f"⚖️ Vegas agrees at {vegas_prob:.0%}")

    # ── 4. Net flow (15 min) ──
    if conn and token_id:
        buy_usd, sell_usd, n_trades = _get_net_flow(conn, sport, token_id)
        if n_trades > 0:
            net = buy_usd - sell_usd
            flow_dir = "BUY" if net > 0 else "SELL"
            lines.append(
                f"🔄 Net flow 15min: <b>${abs(net):,.0f} {flow_dir}</b> "
                f"({n_trades} trades — ${buy_usd:,.0f} buy / ${sell_usd:,.0f} sell)"
            )

    # ── 5. Price impact ──
    if token_id:
        price_now = _get_price_after(token_id)
        if price_now is not None:
            impact_pp = (price_now - price) * 100
            if abs(impact_pp) >= 0.5:
                arrow = "↑" if impact_pp > 0 else "↓"
                lines.append(f"💥 Price impact: {price:.0%} → {price_now:.0%} ({arrow}{abs(impact_pp):.1f}pp)")

    # ── 6. Wallet intel ──
    wallet_info = _get_wallet_intel(maker_addr)
    if wallet_info:
        lines.append(wallet_info)

    send_telegram("\n".join(lines))
    print(f"[sport_whale] {sport}: {outcome} {direction} ${size_usdc:,.0f}@{price:.0%} ({lag})",
          flush=True)

# ── Main ──────────────────────────────────────────────────────────────────────
def run() -> None:
    conn = get_db()
    migrate(conn)

    now_month = datetime.now(timezone.utc).month
    total_alerted = 0

    for sport_cfg in SPORT_CONFIGS:
        sport = sport_cfg["name"]
        if now_month not in sport_cfg["active_months"]:
            continue

        # Get active games from ESPN
        active_games = fetch_espn_active(sport_cfg["espn_path"], sport)
        if not active_games:
            continue

        print(f"[sport_whale] {sport}: {len(active_games)} active game(s)", flush=True)

        whale_threshold = sport_cfg["whale_usdc"]
        has_draw = sport_cfg["has_draw"]
        pm_tag = sport_cfg["pm_tag"]

        # Collect all tokens across all active games
        all_tokens: Dict[str, Tuple[str, str, str]] = {}  # token_id → (game_id, label, outcome_name)
        game_meta: Dict[str, Dict] = {}

        for game in active_games:
            gid = game["game_id"]
            home, away = game["home"], game["away"]
            tokens = get_tokens_for_game(conn, sport, gid, home, away, pm_tag, has_draw)
            game_meta[gid] = {"home": home, "away": away, "tokens": tokens}
            for label, (tid, name) in tokens.items():
                all_tokens[tid] = (gid, label, name)

        if not all_tokens:
            continue

        mc_register_tokens(list(all_tokens.keys()))
        now_ts = int(time.time())
        alerted: List[str] = []

        # ── Path 1: WS memcached trades ──────────────────────────────────────
        ws_hits = 0
        for tid, (gid, label, outcome_name) in all_tokens.items():
            trade = get_live_ws_trade(tid)
            if not trade:
                continue
            ws_hits += 1
            size_usdc = trade["price"] * trade["size"]
            if size_usdc < whale_threshold:
                continue

            dedup_key = f"ws:{tid}:{trade['tx']}"
            row = conn.execute(
                "SELECT last_tx FROM whale_trade_seen WHERE sport=? AND token_id=?",
                (sport, tid)
            ).fetchone()
            if row and row["last_tx"] == dedup_key:
                continue

            meta = game_meta[gid]
            home, away = meta["home"], meta["away"]
            is_no_token = label.endswith("_no")
            base_label = label[:-3] if is_no_token else label
            yes_price = (1.0 - trade["price"]) if is_no_token else trade["price"]
            # Near-settled gate on YES-equivalent price
            if yes_price >= NEAR_SETTLED_HI or yes_price <= NEAR_SETTLED_LO:
                print(f"[sport_whale] SKIP near-settled: {outcome_name} YES~={yes_price:.0%}", flush=True)
                continue
            plain = "Draw" if base_label == "draw" else f"{outcome_name} wins"
            if is_no_token:
                direction = "selling YES on" if trade["side"] == "SELL" else "buying NO on"
            else:
                direction = "buying" if trade["side"] == "BUY" else "selling"
            maker = trade.get("maker_address", trade.get("maker", ""))
            _fire_alert(sport, home, away, plain, direction, size_usdc, trade["price"], "live",
                        conn=conn, token_id=tid, game_meta=meta, maker_addr=maker,
                        is_no_token=is_no_token)

            conn.execute("""
                INSERT OR REPLACE INTO whale_trade_seen (sport, token_id, last_tx, last_ts)
                VALUES (?, ?, ?, ?)
            """, (sport, tid, dedup_key, now_ts))
            conn.commit()
            alerted.append(tid)

        # ── Path 2: data-api global feed ─────────────────────────────────────
        watched_set = set(all_tokens.keys())
        last_ts_rows = conn.execute(
            f"SELECT MIN(last_ts) FROM whale_trade_seen "
            f"WHERE sport=? AND token_id IN ({','.join('?' * len(watched_set))})",
            [sport] + list(watched_set)
        ).fetchone()
        min_last_ts = int(last_ts_rows[0] or 0) if last_ts_rows else 0
        global_trades = get_global_trades(watched_set, since_ts=min_last_ts)
        global_trades.sort(key=lambda t: t["timestamp"])

        for trade in global_trades:
            tid = trade["token_id"]
            if tid in alerted:
                continue
            if trade["size_usdc"] < whale_threshold:
                continue

            tx = trade["tx"]
            row = conn.execute(
                "SELECT last_tx FROM whale_trade_seen WHERE sport=? AND token_id=?",
                (sport, tid)
            ).fetchone()
            if row and row["last_tx"] == tx:
                continue

            gid, label, outcome_name = all_tokens[tid]
            meta = game_meta[gid]
            home, away = meta["home"], meta["away"]
            is_no_token = label.endswith("_no")
            base_label = label[:-3] if is_no_token else label
            yes_price = (1.0 - trade["price"]) if is_no_token else trade["price"]
            if yes_price >= NEAR_SETTLED_HI or yes_price <= NEAR_SETTLED_LO:
                print(f"[sport_whale] SKIP near-settled: {outcome_name} YES~={yes_price:.0%}", flush=True)
                continue
            plain = "Draw" if base_label == "draw" else f"{outcome_name} wins"
            if is_no_token:
                direction = "selling YES on" if trade["side"] == "SELL" else "buying NO on"
            else:
                direction = "buying" if trade["side"] == "BUY" else "selling"
            maker = trade.get("maker_address", trade.get("maker", ""))
            _fire_alert(sport, home, away, plain, direction, trade["size_usdc"],
                        trade["price"], "30s-delay",
                        conn=conn, token_id=tid, game_meta=meta, maker_addr=maker,
                        is_no_token=is_no_token)

            conn.execute("""
                INSERT OR REPLACE INTO whale_trade_seen (sport, token_id, last_tx, last_ts)
                VALUES (?, ?, ?, ?)
            """, (sport, tid, tx, now_ts))
            conn.commit()
            alerted.append(tid)

        total_alerted += len(alerted)
        print(f"[sport_whale] {sport}: WS={ws_hits}/{len(all_tokens)}, alerted={len(alerted)}",
              flush=True)

    if total_alerted == 0:
        print("[sport_whale] No whale trades across any sport.", flush=True)

    conn.close()

if __name__ == "__main__":
    run()

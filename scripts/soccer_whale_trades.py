#!/usr/bin/env python3
"""
soccer_whale_trades.py — Live taker-flow whale alert for active WC soccer games.

Called by scheduler.task_soccer_whale_trades() every 60s.

Sources (in priority order):
  1. poly:trade:{token_id} from memcached (if WS is streaming the token)
  2. data-api.polymarket.com/trades global feed (no auth, ~30s lag)

Alert fires when a single matched trade >= TRADE_WHALE_USDC on an active
soccer game outcome. Dedup by transaction hash stored in sqlite.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from scripts.alert_formatter import send_telegram
from scripts.whale_thresholds import TRADE_FLOOR

# ── Config ───────────────────────────────────────────────────────────────────
TRADE_WHALE_USDC  = TRADE_FLOOR["soccer"]  # 0k — data-driven floor (2026-06-19 study)
NEAR_SETTLED_HI   = 0.90   # suppress alerts when YES >= 90% (near-resolved)
NEAR_SETTLED_LO   = 0.10   # suppress alerts when YES <= 10% (near-resolved)
DATA_API          = "https://data-api.polymarket.com"
GAMMA_API         = "https://gamma-api.polymarket.com"
# data-api global feed: 500 trades ≈ 9s at current PM volume.
# We paginate until we've covered POLL_LOOKBACK_S seconds of trades (≥ our 60s tick).
POLL_LOOKBACK_S   = 90      # cover 1.5x the tick interval to avoid edge gaps
DATA_API_PAGE_SZ  = 500     # max page size
DATA_API_MAX_PAGES = 10     # hard cap: 10 pages × 500 = 5000 trades ≈ 90s coverage
MC_HOST, MC_PORT  = "localhost", 11211
DB_PATH = BASE_DIR / "storage" / "shadow_trades.db"

# ── DB ────────────────────────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS soccer_token_cache (
            game_id     TEXT,
            label       TEXT,
            token_id    TEXT,
            outcome_name TEXT,
            cached_at   TEXT,
            PRIMARY KEY (game_id, label)
        );
        CREATE TABLE IF NOT EXISTS soccer_trade_seen (
            token_id    TEXT PRIMARY KEY,
            last_tx     TEXT,
            last_ts     INTEGER
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
        print(f"[soccer_whale_trades] GET {url[:70]} → {e}", flush=True)
        return None


# ── Memcached (sync raw protocol) ────────────────────────────────────────────
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


def mc_get_trade(token_id: str) -> Optional[Dict]:
    """Read last trade from WS memcached store."""
    raw = _mc_get(f"poly:trade:{token_id}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def mc_register_tokens(token_ids: List[str]) -> None:
    """Register tokens with the WS service (adds to poly:ws:registered)."""
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
    merged = list(cur)[:500]
    _mc_set("poly:ws:registered", json.dumps(merged), exptime=600)


# ── Token resolution ──────────────────────────────────────────────────────────
def _nmatch(a: str, b: str) -> bool:
    a, b = a.lower().strip(), b.lower().strip()
    if a == b or a in b or b in a:
        return True
    aliases = {
        "usa": ["united states"], "united states": ["usa"],
        "korea": ["south korea", "korea republic"],
        "south korea": ["korea republic"], "korea republic": ["south korea"],
    }
    for alias in aliases.get(a, []):
        if alias in b or b in alias:
            return True
    return False


def get_tokens_for_game(conn: sqlite3.Connection, game_id: str,
                        home: str, away: str) -> Dict[str, Tuple[str, str]]:
    """Returns {label: (token_id, outcome_name)} — uses cache, refreshes if >1h old."""
    rows = conn.execute(
        "SELECT label, token_id, outcome_name, cached_at FROM soccer_token_cache WHERE game_id=?",
        (game_id,)
    ).fetchall()

    if rows:
        # Check freshness — if cache is <1h old and has all 3 labels, use it
        cached_at = rows[0]["cached_at"]
        age_s = (datetime.now(timezone.utc).timestamp() -
                 datetime.fromisoformat(cached_at).timestamp())
        if age_s < 3600 and len(rows) >= 2:
            return {r["label"]: (r["token_id"], r["outcome_name"]) for r in rows}

    # Fetch from Gamma
    data = _get(f"{GAMMA_API}/events", {"tag_slug": "fifa-world-cup", "active": "true", "limit": 100})
    if not data:
        return {}

    tokens: Dict[str, Tuple[str, str]] = {}
    for ev in (data if isinstance(data, list) else []):
        t = ev.get("title", "").lower()
        if not (_nmatch(home, t) and _nmatch(away, t)):
            continue
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
            if not tids:
                continue
            yes_tid = tids[0]
            no_tid = tids[1] if len(tids) > 1 else None
            if "draw" in q:
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
        if tokens:
            break

    if tokens:
        now_ts = datetime.now(timezone.utc).isoformat()
        conn.execute("DELETE FROM soccer_token_cache WHERE game_id=?", (game_id,))
        for label, (tid, name) in tokens.items():
            conn.execute("""
                INSERT OR REPLACE INTO soccer_token_cache
                  (game_id, label, token_id, outcome_name, cached_at)
                VALUES (?, ?, ?, ?, ?)
            """, (game_id, label, tid, name, now_ts))
        conn.commit()

    return tokens


# ── Trade fetching ────────────────────────────────────────────────────────────
def get_live_ws_trade(token_id: str) -> Optional[Dict]:
    """Read most recent trade from WS memcached — None if not streaming."""
    t = mc_get_trade(token_id)
    if not t:
        return None
    return {
        "tx": str(t.get("ts", "")),  # use ts as dedup key
        "price": float(t.get("price", 0) or 0),
        "size": float(t.get("size", 0) or 0),
        "side": str(t.get("side", "BUY")).upper(),
        "timestamp": int(float(t.get("ts", 0) or 0) / 1000),  # ms → s
        "source": "ws",
    }


def get_global_trades(watched_tokens: set, since_ts: int = 0) -> List[Dict]:
    """Poll data-api global trade feed, filter to our watched tokens.

    Paginates (via offset) until either:
    - a trade older than `since_ts` is reached (no more relevant history), or
    - DATA_API_MAX_PAGES pages fetched (hard cap)

    `since_ts` defaults to (now - POLL_LOOKBACK_S) when 0.
    """
    if since_ts == 0:
        since_ts = int(time.time()) - POLL_LOOKBACK_S

    out = []
    for page in range(DATA_API_MAX_PAGES):
        offset = page * DATA_API_PAGE_SZ
        data = _get(f"{DATA_API}/trades", {"limit": DATA_API_PAGE_SZ, "offset": offset})
        if not data or not isinstance(data, list):
            break

        oldest_ts_this_page = int(time.time())
        for t in data:
            ts = int(t.get("timestamp", 0) or 0)
            oldest_ts_this_page = min(oldest_ts_this_page, ts)
            if ts < since_ts:
                continue  # too old — but keep scanning page for out-of-order entries
            asset = t.get("asset", "")
            if asset not in watched_tokens:
                continue
            size = float(t.get("size", 0) or 0)
            price = float(t.get("price", 0) or 0)
            out.append({
                "tx": t.get("transactionHash", ""),
                "token_id": asset,
                "price": price,
                "size_usdc": size * price,  # size in shares, multiply by price for USDC
                "side": str(t.get("side", "BUY")).upper(),
                "timestamp": ts,
                "outcome": t.get("outcome", ""),
                "wallet": t.get("proxyWallet", ""),
                "source": "data-api",
            })

        # Stop paginating once the oldest trade on this page is older than our window
        if oldest_ts_this_page < since_ts:
            break

    return out


# ── Main logic ────────────────────────────────────────────────────────────────
def run() -> None:
    conn = get_db()
    migrate(conn)

    # Get active games (updated within last 15 min)
    cutoff_ts = (datetime.now(timezone.utc).timestamp() - 900)
    cutoff_iso = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).isoformat()
    active_games = conn.execute(
        "SELECT game_id, home_team, away_team FROM soccer_score_snap WHERE ts > ?",
        (cutoff_iso,)
    ).fetchall()

    if not active_games:
        conn.close()
        return

    print(f"[soccer_whale_trades] {len(active_games)} active game(s)", flush=True)

    # Collect all token IDs across all active games
    all_tokens: Dict[str, Tuple[str, str, str]] = {}  # token_id → (game_id, label, outcome_name)
    game_meta: Dict[str, Dict] = {}

    for game in active_games:
        gid = game["game_id"]
        home, away = game["home_team"], game["away_team"]
        tokens = get_tokens_for_game(conn, gid, home, away)
        game_meta[gid] = {"home": home, "away": away, "tokens": tokens}
        for label, (tid, name) in tokens.items():
            all_tokens[tid] = (gid, label, name)

    if not all_tokens:
        conn.close()
        return

    # Register tokens with WS service so it subscribes to them
    mc_register_tokens(list(all_tokens.keys()))

    now_ts = int(time.time())
    alerted: List[str] = []

    # ── Path 1: WS memcached trades (low latency) ─────────────────────────────
    ws_hits = 0
    for tid, (gid, label, outcome_name) in all_tokens.items():
        trade = get_live_ws_trade(tid)
        if not trade:
            continue
        ws_hits += 1
        size_usdc = trade["price"] * trade["size"]
        if size_usdc < TRADE_WHALE_USDC:
            continue

        dedup_key = f"ws:{tid}:{trade['tx']}"
        row = conn.execute(
            "SELECT last_tx FROM soccer_trade_seen WHERE token_id=?", (tid,)
        ).fetchone()
        if row and row["last_tx"] == dedup_key:
            continue  # already alerted

        meta = game_meta[gid]
        home, away = meta["home"], meta["away"]
        is_no_token = label.endswith("_no")
        base_label = label[:-3] if is_no_token else label
        # YES-equivalent price for near-settled check
        yes_price = (1.0 - trade["price"]) if is_no_token else trade["price"]
        if yes_price >= NEAR_SETTLED_HI or yes_price <= NEAR_SETTLED_LO:
            print(f"[soccer_whale_trades] SKIP near-settled: {outcome_name} YES~={yes_price:.0%}", flush=True)
            continue
        plain = "Draw" if base_label == "draw" else f"{outcome_name} wins"
        if is_no_token:
            direction = "selling NO on" if trade["side"] == "SELL" else "buying NO on"
            price_note = f"~{trade['price']*100:.0f}¢  (YES ~{yes_price*100:.0f}¢)"
        else:
            is_sell = trade["side"] == "SELL"
            direction = "selling YES on" if is_sell else "buying YES on"
            price_note = f"~{yes_price*100:.0f}¢" if not is_sell else f"~{yes_price*100:.0f}¢ (NO ~{(1-yes_price)*100:.0f}¢)"
        _fire_alert(home, away, plain, direction, size_usdc, trade["price"], price_note, "live", meta)

        conn.execute("""
            INSERT OR REPLACE INTO soccer_trade_seen (token_id, last_tx, last_ts)
            VALUES (?, ?, ?)
        """, (tid, dedup_key, now_ts))
        conn.commit()
        alerted.append(tid)

    print(f"[soccer_whale_trades] WS tokens live: {ws_hits}/{len(all_tokens)}", flush=True)

    # ── Path 2: data-api global feed (fallback, ~30s lag) ─────────────────────
    watched_set = set(all_tokens.keys())
    # Use oldest last_ts across watched tokens as the lower bound — avoids re-scanning
    # old trades on every tick. Falls back to POLL_LOOKBACK_S if no prior state.
    last_ts_rows = conn.execute(
        f"SELECT MIN(last_ts) FROM soccer_trade_seen WHERE token_id IN ({','.join('?'*len(watched_set))})",
        list(watched_set)
    ).fetchone()
    min_last_ts = int(last_ts_rows[0] or 0) if last_ts_rows else 0
    global_trades = get_global_trades(watched_set, since_ts=min_last_ts)

    # Sort ascending by timestamp so we process oldest first
    global_trades.sort(key=lambda t: t["timestamp"])

    for trade in global_trades:
        tid = trade["token_id"]
        if tid in alerted:
            continue  # WS already got this one
        if trade["size_usdc"] < TRADE_WHALE_USDC:
            continue

        tx = trade["tx"]
        row = conn.execute(
            "SELECT last_tx FROM soccer_trade_seen WHERE token_id=?", (tid,)
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
            print(f"[soccer_whale_trades] SKIP near-settled: {outcome_name} YES~={yes_price:.0%}", flush=True)
            continue
        plain = "Draw" if base_label == "draw" else f"{outcome_name} wins"
        if is_no_token:
            direction = "selling NO on" if trade["side"] == "SELL" else "buying NO on"
            price_note = f"~{trade['price']*100:.0f}¢  (YES ~{yes_price*100:.0f}¢)"
        else:
            is_sell = trade["side"] == "SELL"
            direction = "selling YES on" if is_sell else "buying YES on"
            price_note = f"~{yes_price*100:.0f}¢" if not is_sell else f"~{yes_price*100:.0f}¢ (NO ~{(1-yes_price)*100:.0f}¢)"
        _fire_alert(home, away, plain, direction, trade["size_usdc"], trade["price"], price_note, "30s-delay", meta)

        conn.execute("""
            INSERT OR REPLACE INTO soccer_trade_seen (token_id, last_tx, last_ts)
            VALUES (?, ?, ?)
        """, (tid, tx, now_ts))
        conn.commit()
        alerted.append(tid)

    print(f"[soccer_whale_trades] Alerted {len(alerted)} whale trade(s)", flush=True)
    conn.close()


def _fire_alert(home: str, away: str, outcome: str, direction: str,
                size_usdc: float, price: float, price_note: str,
                lag: str, game_meta: Optional[Dict] = None) -> None:
    lag_note = "" if lag == "live" else "  <i>(~30s delay)</i>"
    lines = [f"🐋 <b>WHALE TRADE</b> ⚽  —  {home} vs {away}{lag_note}", ""]

    # Score + minute from game_meta
    if game_meta:
        detail = game_meta.get("detail", "")
        scores = game_meta.get("scores", {})
        if scores:
            score_str = " – ".join(f"{k} {v}" for k, v in scores.items())
            lines.append(f"📊 <b>{score_str}</b>  ({detail})")
        elif detail:
            lines.append(f"📊 {detail}")

    lines.append(f"💵 <b>{direction} {outcome}</b> — ${size_usdc:,.0f} @ {price_note}")
    send_telegram("\n".join(lines))
    print(f"[soccer_whale_trades] Alert: {outcome} {direction} ${size_usdc:,.0f}@{price:.0%} ({lag})", flush=True)


if __name__ == "__main__":
    run()

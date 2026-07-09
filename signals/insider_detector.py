#!/usr/bin/env python3
"""
Fresh Wallet Insider Detection System — Phase 1 MVP

Polls Polymarket trades for large bets, scores wallets on composite signal:
  - Wallet age (30%): How new is the wallet?
  - Bet size (25%): Absolute + relative to market liquidity
  - Event specificity (20%): Military/regulatory > sports/weather
  - Concentration (15%): Single bet vs diversified
  - Timing (10%): Close to resolution = higher signal

Alert thresholds:
  ≥ 80: CRITICAL → Telegram + Discord
  60-79: HIGH → Discord
  40-59: MODERATE → Log only

Uses:
  - data-api.polymarket.com/trades (no auth)
  - data-api.polymarket.com/activity (wallet first trade)
  - gamma-api.polymarket.com/markets (market metadata)
"""

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
from loguru import logger

# ── Config ──────────────────────────────────────────────────────────
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

MIN_TRADE_SIZE = 5_000        # Only analyze trades ≥ $5K
SCAN_LIMIT = 200              # Trades per poll
POLL_INTERVAL = 60            # Seconds between polls

# Wallet age cache TTL
WALLET_CACHE_TTL = 86400      # 24h — wallet age doesn't change fast

# Known market makers / high-volume bots to exclude
KNOWN_MM_ADDRESSES = {
    # Add known MM proxy wallets here as we discover them
}

DB_PATH = Path(__file__).parent.parent / "storage" / "shadow_trades.db"

# ── Scoring Weights ─────────────────────────────────────────────────
WEIGHTS = {
    "wallet_age":        0.30,
    "bet_size":          0.25,
    "event_specificity": 0.20,
    "concentration":     0.15,
    "timing":            0.10,
}

# ── Specificity Keywords ────────────────────────────────────────────
HIGH_SPECIFICITY = {
    100: ["strike", "strikes", "bomb", "military", "invasion", "war", "attack",
          "troops", "forces entering", "ground offensive", "airstrike"],
    85:  ["regulatory", "indicted", "arrested", "sanctions", "banned", "lawsuit",
          "sec ", "fda ", "approval", "ruling"],
    75:  ["iran", "israel", "ukraine", "russia", "china", "taiwan", "nato",
          "khamenei", "regime", "coup", "assassination", "strait of hormuz"],
    65:  ["earnings", "merger", "acquisition", "ipo", "fdv", "token launch",
          "bankruptcy", "default"],
    30:  ["election", "nominee", "president", "governor", "senator", "democrat",
          "republican", "midterm"],
    10:  ["win ", "winner", "game", "match", "championship", "nfl", "nba",
          "soccer", "cricket", "f1 "],
}

LOW_SPECIFICITY = ["weather", "temperature", "up or down", "5m", "15m", "30m",
                   "1h", "updown"]


# ── DB Setup ────────────────────────────────────────────────────────

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _init_tables(conn)
    return conn


def _init_tables(conn: sqlite3.Connection):
    conn.execute("""CREATE TABLE IF NOT EXISTS insider_wallets (
        address TEXT PRIMARY KEY,
        first_seen TEXT,
        first_trade_ts REAL,
        insider_score REAL,
        total_bets INTEGER DEFAULT 0,
        total_volume REAL DEFAULT 0,
        wins INTEGER DEFAULT 0,
        losses INTEGER DEFAULT 0,
        win_rate REAL,
        total_pnl REAL DEFAULT 0,
        last_active TEXT,
        notes TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS insider_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        wallet_address TEXT,
        detected_at TEXT,
        market_id TEXT,
        market_title TEXT,
        event_slug TEXT,
        platform TEXT DEFAULT 'polymarket',
        side TEXT,
        outcome TEXT,
        size_usd REAL,
        entry_price REAL,
        insider_score REAL,
        wallet_age_hours REAL,
        event_specificity_score REAL,
        bet_size_score REAL,
        concentration_score REAL,
        timing_score REAL,
        resolved INTEGER DEFAULT 0,
        resolution_outcome TEXT,
        exit_price REAL,
        pnl REAL,
        tx_hash TEXT
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_insider_trades_wallet
        ON insider_trades(wallet_address)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_insider_trades_score
        ON insider_trades(insider_score DESC)""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_insider_trades_resolved
        ON insider_trades(resolved)""")
    conn.commit()


# ── API Helpers ─────────────────────────────────────────────────────

_http = None

def _client() -> httpx.Client:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.Client(timeout=15, headers={"User-Agent": "Polyclawd/2.0"})
    return _http


def fetch_recent_trades(limit: int = SCAN_LIMIT) -> List[Dict]:
    """Fetch most recent trades from Polymarket."""
    try:
        r = _client().get(f"{DATA_API}/trades", params={"limit": limit})
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.warning("Trade fetch failed: {}", e)
    return []


def get_wallet_first_trade(address: str) -> Optional[float]:
    """Get timestamp of wallet's first trade on Polymarket.
    
    Returns Unix timestamp or None.
    """
    # Check cache first
    conn = _get_db()
    row = conn.execute(
        "SELECT first_trade_ts FROM insider_wallets WHERE address=?", (address,)
    ).fetchone()
    if row and row["first_trade_ts"]:
        conn.close()
        return row["first_trade_ts"]
    conn.close()

    # Fetch from API — oldest trade
    try:
        r = _client().get(f"{DATA_API}/activity", params={
            "user": address, "limit": 1, "sortBy": "timestamp", "sortOrder": "asc"
        })
        if r.status_code == 200:
            data = r.json()
            if data and len(data) > 0:
                ts = data[0].get("timestamp", 0)
                # Cache it
                _cache_wallet(address, ts)
                return ts
    except Exception as e:
        logger.debug("Wallet age lookup failed for {}: {}", address[:12], e)
    return None


def get_wallet_positions(address: str) -> List[Dict]:
    """Get wallet's current positions to calculate concentration."""
    try:
        r = _client().get(f"{DATA_API}/activity", params={
            "user": address, "limit": 50, "sortBy": "timestamp", "sortOrder": "desc"
        })
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.debug("Position fetch failed for {}: {}", address[:12], e)
    return []


def _cache_wallet(address: str, first_trade_ts: float):
    """Cache wallet first trade timestamp."""
    conn = _get_db()
    conn.execute("""INSERT INTO insider_wallets (address, first_seen, first_trade_ts, last_active)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(address) DO UPDATE SET
            first_trade_ts = COALESCE(insider_wallets.first_trade_ts, excluded.first_trade_ts),
            last_active = excluded.last_active""",
        (address, datetime.now(timezone.utc).isoformat(), first_trade_ts,
         datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()


# ── Scoring Functions ───────────────────────────────────────────────

def score_wallet_age(first_trade_ts: Optional[float]) -> float:
    """Score 0-100 based on how new the wallet is."""
    if first_trade_ts is None:
        return 50  # Unknown — moderate score
    
    age_hours = (time.time() - first_trade_ts) / 3600
    
    if age_hours < 1:
        return 100
    elif age_hours < 6:
        return 85
    elif age_hours < 24:
        return 70
    elif age_hours < 72:  # 3 days
        return 50
    elif age_hours < 168:  # 7 days
        return 30
    elif age_hours < 720:  # 30 days
        return 15
    else:
        return 0


def score_bet_size(size_usd: float, market_volume: float = 0) -> float:
    """Score 0-100 based on bet size."""
    if size_usd >= 500_000:
        score = 100
    elif size_usd >= 100_000:
        score = 85
    elif size_usd >= 50_000:
        score = 70
    elif size_usd >= 20_000:
        score = 55
    elif size_usd >= 10_000:
        score = 40
    elif size_usd >= 5_000:
        score = 25
    else:
        return 0
    
    # Liquidity impact bonus: if bet is >10% of market volume
    if market_volume > 0 and size_usd / market_volume > 0.10:
        score = min(100, score + 20)
    
    return score


def score_event_specificity(title: str) -> float:
    """Score 0-100 based on event type. Military/regulatory = high."""
    title_lower = title.lower()
    
    # Skip low-specificity markets entirely
    for kw in LOW_SPECIFICITY:
        if kw in title_lower:
            return 0
    
    # Check from highest to lowest
    for score_val, keywords in sorted(HIGH_SPECIFICITY.items(), reverse=True):
        for kw in keywords:
            if kw in title_lower:
                return score_val
    
    return 40  # Unknown category — moderate


def score_concentration(recent_trades: List[Dict], current_trade: Dict) -> float:
    """Score 0-100 based on how concentrated the wallet's activity is."""
    if not recent_trades or len(recent_trades) <= 1:
        return 100  # Single trade = maximum concentration
    
    # Count unique markets
    unique_markets = set()
    unique_events = set()
    for t in recent_trades:
        unique_markets.add(t.get("conditionId", t.get("market_id", "")))
        unique_events.add(t.get("eventSlug", ""))
    
    n_markets = len(unique_markets)
    n_events = len(unique_events)
    
    if n_markets <= 1:
        return 100
    elif n_markets <= 3 and n_events <= 2:
        return 80
    elif n_markets <= 5 and n_events <= 3:
        return 40
    else:
        return 10


def score_timing(title: str, event_slug: str = "") -> float:
    """Score 0-100 based on how close to resolution.
    
    Heuristic: extract dates from title, compare to now.
    """
    title_lower = title.lower()
    now = datetime.now(timezone.utc)
    
    # 5-minute markets
    if re.search(r'\d+:\d+[ap]m.*\d+:\d+[ap]m', title_lower):
        return 100  # Resolves in minutes
    
    # "on March 17" / "by March 17" patterns
    months = {
        'january': 1, 'february': 2, 'march': 3, 'april': 4,
        'may': 5, 'june': 6, 'july': 7, 'august': 8,
        'september': 9, 'october': 10, 'november': 11, 'december': 12
    }
    
    for month_name, month_num in months.items():
        match = re.search(rf'{month_name}\s+(\d{{1,2}})', title_lower)
        if match:
            day = int(match.group(1))
            try:
                target = datetime(now.year, month_num, day, tzinfo=timezone.utc)
                if target < now:
                    target = datetime(now.year + 1, month_num, day, tzinfo=timezone.utc)
                hours_until = (target - now).total_seconds() / 3600
                
                if hours_until < 6:
                    return 100
                elif hours_until < 24:
                    return 75
                elif hours_until < 72:
                    return 50
                elif hours_until < 168:
                    return 30
                else:
                    return 10
            except ValueError:
                pass
    
    return 30  # Unknown resolution time


def calculate_insider_score(trade: Dict, wallet_first_ts: Optional[float],
                            recent_activity: List[Dict]) -> Dict:
    """Calculate composite insider score for a trade."""
    title = trade.get("title", "")
    size = trade.get("size", 0)
    
    age_score = score_wallet_age(wallet_first_ts)
    size_score = score_bet_size(size)
    specificity_score = score_event_specificity(title)
    concentration = score_concentration(recent_activity, trade)
    timing = score_timing(title, trade.get("eventSlug", ""))
    
    composite = (
        WEIGHTS["wallet_age"] * age_score +
        WEIGHTS["bet_size"] * size_score +
        WEIGHTS["event_specificity"] * specificity_score +
        WEIGHTS["concentration"] * concentration +
        WEIGHTS["timing"] * timing
    )
    
    wallet_age_hours = (time.time() - wallet_first_ts) / 3600 if wallet_first_ts else None
    
    return {
        "insider_score": round(composite, 1),
        "wallet_age_score": age_score,
        "wallet_age_hours": round(wallet_age_hours, 1) if wallet_age_hours else None,
        "bet_size_score": size_score,
        "event_specificity_score": specificity_score,
        "concentration_score": concentration,
        "timing_score": timing,
    }


# ── Main Scanner ────────────────────────────────────────────────────

# Track already-processed tx hashes to avoid re-alerting
# File-backed so dedup survives scheduler restarts (in-memory set would reset
# and re-fire every trade in the scan window on every restart).
_SEEN_TXS_FILE = Path("/tmp/insider_seen_txs.json")
_seen_txs_max = 10_000


def _load_seen_txs() -> set:
    try:
        with open(_SEEN_TXS_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_seen_txs(seen: set) -> None:
    try:
        # Keep only the most recent N to bound file size
        items = list(seen)[-_seen_txs_max:]
        with open(_SEEN_TXS_FILE, "w") as f:
            json.dump(items, f)
    except Exception:
        pass


def scan_for_insiders() -> List[Dict]:
    """Main scan: fetch trades, filter large ones, score wallets.
    
    Returns list of scored insider candidates above threshold (40+).
    """
    _seen_txs = _load_seen_txs()

    trades = fetch_recent_trades(SCAN_LIMIT)
    if not trades:
        return []
    
    # Filter: size >= threshold, not seen before, not known MM
    large_trades = []
    max_size = max((t.get("size", 0) for t in trades), default=0)
    for t in trades:
        tx = t.get("transactionHash", "")
        size = t.get("size", 0)
        wallet = t.get("proxyWallet", "")
        
        if size < MIN_TRADE_SIZE:
            continue
        if tx in _seen_txs:
            continue
        if wallet in KNOWN_MM_ADDRESSES:
            continue
        
        large_trades.append(t)
        _seen_txs.add(tx)
    
    _save_seen_txs(_seen_txs)
    
    if not large_trades:
        logger.debug("Insider scan: 0 trades ≥${:,} (max=${:,.0f}, {} total)", MIN_TRADE_SIZE, max_size, len(trades))
        return []
    
    logger.info("Insider scan: {} large trades (≥${:,}) out of {} total",
                len(large_trades), MIN_TRADE_SIZE, len(trades))
    
    results = []
    for trade in large_trades:
        wallet = trade["proxyWallet"]
        
        # Get wallet age
        first_ts = get_wallet_first_trade(wallet)
        
        # Get recent activity for concentration scoring
        recent = get_wallet_positions(wallet)
        
        # Score
        scores = calculate_insider_score(trade, first_ts, recent)
        
        if scores["insider_score"] < 40:
            continue
        
        result = {
            "wallet": wallet,
            "trade": trade,
            "scores": scores,
            "size_usd": trade["size"],
            "title": trade.get("title", ""),
            "side": trade.get("side", ""),
            "outcome": trade.get("outcome", ""),
            "price": trade.get("price", 0),
            "event_slug": trade.get("eventSlug", ""),
            "tx_hash": trade.get("transactionHash", ""),
        }
        results.append(result)
        
        # Store in DB
        _store_insider_trade(result)
        
        level = ("CRITICAL" if scores["insider_score"] >= 80 else
                 "HIGH" if scores["insider_score"] >= 60 else "MODERATE")
        logger.info("🔍 INSIDER {} [{}]: ${:,.0f} on '{}' | wallet_age={}h | score={}",
                    level, wallet[:12], trade["size"], trade.get("title", "")[:50],
                    scores.get("wallet_age_hours", "?"), scores["insider_score"])
    
    return results


def _store_insider_trade(result: Dict):
    """Store detected insider trade in DB."""
    conn = _get_db()
    scores = result["scores"]
    trade = result["trade"]
    
    # Update wallet record
    conn.execute("""INSERT INTO insider_wallets (address, first_seen, insider_score, total_bets, total_volume, last_active)
        VALUES (?, ?, ?, 1, ?, ?)
        ON CONFLICT(address) DO UPDATE SET
            insider_score = MAX(insider_wallets.insider_score, excluded.insider_score),
            total_bets = insider_wallets.total_bets + 1,
            total_volume = insider_wallets.total_volume + excluded.total_volume,
            last_active = excluded.last_active""",
        (result["wallet"], datetime.now(timezone.utc).isoformat(),
         scores["insider_score"], result["size_usd"],
         datetime.now(timezone.utc).isoformat()))
    
    # Store trade
    conn.execute("""INSERT INTO insider_trades
        (wallet_address, detected_at, market_id, market_title, event_slug, side, outcome,
         size_usd, entry_price, insider_score, wallet_age_hours,
         event_specificity_score, bet_size_score, concentration_score, timing_score, tx_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (result["wallet"], datetime.now(timezone.utc).isoformat(),
         trade.get("conditionId", ""), result["title"][:200], result["event_slug"],
         result["side"], result["outcome"], result["size_usd"], result["price"],
         scores["insider_score"], scores.get("wallet_age_hours"),
         scores["event_specificity_score"], scores["bet_size_score"],
         scores["concentration_score"], scores["timing_score"],
         result["tx_hash"]))
    
    conn.commit()
    conn.close()


# ── Alert Functions ─────────────────────────────────────────────────

def send_alerts(results: List[Dict]):
    """Send alerts for detected insider activity."""
    from scripts.alert_formatter import format_alert, send_telegram

    for i, r in enumerate(results, 1):
        score = r["scores"]["insider_score"]
        # Only alert on high conviction (80+). Log 60-79 for calibration.
        if score < 80:
            continue

        s = r["scores"]
        wallet = r["wallet"]
        age_str = f"{s['wallet_age_hours']:.0f}h" if s.get("wallet_age_hours") is not None else "unknown"
        wallet_short = wallet[:8] + "..." + wallet[-4:] if len(wallet) > 16 else wallet

        slug = r.get("event_slug", "")
        url = f"https://polymarket.com/event/{slug}" if slug else ""

        # Skip decided markets (price at 0¢ or 100¢ = already resolved)
        price_raw = r.get("price", 0)
        if price_raw <= 0.02 or price_raw >= 0.98:
            logger.info("SKIP decided market: '{}' price={:.2f}", r["title"][:50], price_raw)
            continue

        # Map exchange terminology to prediction market language
        side = "YES" if r["side"] == "BUY" else "NO" if r["side"] == "SELL" else r["side"]
        price = int(price_raw * 100)
        size = r["size_usd"]

        msg = format_alert(
            alert_type="insider",
            rank=i,
            emoji="🚨",
            title=r["title"][:80],
            direction=side,
            price_cents=price,
            action=f"🐋 New wallet bought ${size:,.0f} {side} · Wallet {age_str} old",
            signal_score=f"Suspicion {score:.0f}/100 · Wallet {wallet_short}",
            close_info="",
            data_line=f"Wallet {age_str} old · ${size:,.0f} bet · {s['event_specificity_score']:.0f}% specific · {s['concentration_score']:.0f}% concentrated · {s['timing_score']:.0f} timing",
            links=[f"<a href='{url}'>Polymarket</a>"] if url else None,
        )

        send_telegram(msg)


# ── Resolution Tracking ────────────────────────────────────────────

def resolve_insider_trades():
    """Check if flagged insider trades have resolved and update outcomes."""
    conn = _get_db()
    unresolved = conn.execute(
        "SELECT * FROM insider_trades WHERE resolved=0"
    ).fetchall()
    
    if not unresolved:
        conn.close()
        return
    
    resolved_count = 0
    for trade in unresolved:
        market_id = trade["market_id"]
        if not market_id:
            continue
        
        try:
            # Check if market has resolved via CLOB API
            # (market_id is a hex condition_id; Gamma /markets/{id} expects numeric IDs)
            r = _client().get(f"https://clob.polymarket.com/markets/{market_id}")
            if r.status_code != 200:
                # Fallback: try Gamma with condition_id query param
                r = _client().get(f"{GAMMA_API}/markets", params={"condition_id": market_id})
                if r.status_code != 200:
                    continue
                markets = r.json()
                if not markets:
                    continue
                market = markets[0] if isinstance(markets, list) else markets
            else:
                market = r.json()
            
            # Handle both CLOB ("closed") and Gamma ("resolved") response formats
            is_resolved = market.get("resolved") or market.get("closed")
            if not is_resolved:
                continue
            
            # Determine outcome -- CLOB uses tokens[].winner, Gamma uses "outcome"
            winning_outcome = market.get("outcome", "")
            if not winning_outcome:
                tokens = market.get("tokens", [])
                for tok in tokens:
                    if tok.get("winner"):
                        winning_outcome = tok.get("outcome", "")
                        break
            insider_side = trade["outcome"]  # The outcome they bet on
            
            won = (insider_side.lower() == winning_outcome.lower())
            exit_price = 1.0 if won else 0.0
            entry = trade["entry_price"] or 0.5
            pnl = (exit_price - entry) * trade["size_usd"] / entry if entry > 0 else 0
            
            conn.execute("""UPDATE insider_trades 
                SET resolved=1, resolution_outcome=?, exit_price=?, pnl=?
                WHERE id=?""",
                ("won" if won else "lost", exit_price, round(pnl, 2), trade["id"]))
            
            # Update wallet stats
            if won:
                conn.execute("""UPDATE insider_wallets 
                    SET wins = wins + 1, total_pnl = total_pnl + ?
                    WHERE address=?""", (pnl, trade["wallet_address"]))
            else:
                conn.execute("""UPDATE insider_wallets 
                    SET losses = losses + 1, total_pnl = total_pnl + ?
                    WHERE address=?""", (pnl, trade["wallet_address"]))
            
            # Update win rate
            conn.execute("""UPDATE insider_wallets 
                SET win_rate = CAST(wins AS REAL) / NULLIF(wins + losses, 0)
                WHERE address=?""", (trade["wallet_address"],))
            
            resolved_count += 1
            logger.info("Insider trade resolved: {} {} on '{}' → {} (${:+,.0f})",
                        trade["wallet_address"][:12],
                        "WON" if won else "LOST",
                        trade["market_title"][:40],
                        winning_outcome, pnl)
            
        except Exception as e:
            logger.debug("Insider resolution failed for {}: {}", market_id[:20], e)
    
    conn.commit()
    conn.close()
    
    if resolved_count:
        logger.info("Insider resolution: {} trades resolved", resolved_count)


# ── API Endpoints Data ──────────────────────────────────────────────

def get_recent_insiders(limit: int = 20, min_score: float = 40) -> List[Dict]:
    """Get recent insider detections for API endpoint."""
    conn = _get_db()
    rows = conn.execute("""
        SELECT t.*, w.total_bets as wallet_total_bets, w.win_rate as wallet_wr,
               w.total_volume as wallet_total_volume
        FROM insider_trades t
        LEFT JOIN insider_wallets w ON t.wallet_address = w.address
        WHERE t.insider_score >= ?
        ORDER BY t.detected_at DESC
        LIMIT ?
    """, (min_score, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_insider_leaderboard(min_bets: int = 2) -> List[Dict]:
    """Get top insider wallets by score and win rate."""
    conn = _get_db()
    rows = conn.execute("""
        SELECT * FROM insider_wallets
        WHERE total_bets >= ?
        ORDER BY insider_score DESC
        LIMIT 20
    """, (min_bets,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── CLI ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "scan":
        results = scan_for_insiders()
        if results:
            for r in results:
                s = r["scores"]
                print(f"[{s['insider_score']:.0f}] ${r['size_usd']:,.0f} {r['side']} @ {int(r['price']*100)}¢ — {r['title'][:60]}")
                print("---")
            send_alerts(results)
        else:
            print("No insider activity detected")
    elif len(sys.argv) > 1 and sys.argv[1] == "resolve":
        resolve_insider_trades()
    elif len(sys.argv) > 1 and sys.argv[1] == "recent":
        for r in get_recent_insiders():
            print(f"  [{r['insider_score']:.0f}] ${r['size_usd']:>10,.0f}  {r['wallet_address'][:12]}  {r['market_title'][:50]}")
    else:
        print("Usage: insider_detector.py [scan|resolve|recent]")

#!/usr/bin/env python3
"""
Paper Portfolio Manager — Kelly-fractional sizing with SQLite tracking.
"""

import sqlite3
try:
    from empirical_confidence import calculate_empirical_confidence
    HAS_EMPIRICAL = True
except ImportError:
    HAS_EMPIRICAL = False
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "storage" / "shadow_trades.db"

STARTING_BANKROLL = 500.0
KELLY_FRACTION = 1 / 8
MAX_CONCURRENT = 10
MIN_CONFIDENCE = 0.50
MIN_EDGE = 0.15  # 15% minimum edge — was 5%, letting noise through
MIN_PRICE = 0.05  # Price floor — reject garbage contracts below 5 cents
MAX_PRICE = 0.95  # Price ceiling — reject near-certain markets (no edge)
MIN_BET = 5.0
MAX_BET = 35.0  # Raised: higher conviction NO-only strategy


def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _init_tables(conn)
    return conn


def _init_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS paper_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opened_at TEXT NOT NULL,
            market_id TEXT NOT NULL,
            market_title TEXT,
            platform TEXT DEFAULT 'kalshi',
            side TEXT NOT NULL,
            entry_price REAL NOT NULL,
            bet_size REAL NOT NULL,
            potential_payout REAL,
            confidence REAL,
            edge_pct REAL,
            status TEXT DEFAULT 'open',
            closed_at TEXT,
            exit_price REAL,
            pnl REAL,
            close_reason TEXT
        );
        CREATE TABLE IF NOT EXISTS paper_portfolio_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            bankroll REAL NOT NULL,
            total_pnl REAL DEFAULT 0,
            total_trades INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            win_rate REAL DEFAULT 0,
            max_drawdown REAL DEFAULT 0,
            peak_bankroll REAL NOT NULL,
            current_drawdown_pct REAL DEFAULT 0,
            sharpe_estimate REAL DEFAULT 0
        );
    """)
    conn.commit()


def _get_bankroll(conn) -> float:
    row = conn.execute("SELECT bankroll FROM paper_portfolio_state ORDER BY id DESC LIMIT 1").fetchone()
    return row["bankroll"] if row else STARTING_BANKROLL


def _get_peak(conn) -> float:
    row = conn.execute("SELECT peak_bankroll FROM paper_portfolio_state ORDER BY id DESC LIMIT 1").fetchone()
    return row["peak_bankroll"] if row else STARTING_BANKROLL


def _count_open(conn) -> int:
    row = conn.execute("SELECT COUNT(*) as c FROM paper_positions WHERE status='open'").fetchone()
    return row["c"]


def _save_state(conn, bankroll, pnl_change=0):
    prev = conn.execute("SELECT * FROM paper_portfolio_state ORDER BY id DESC LIMIT 1").fetchone()
    total_pnl = (prev["total_pnl"] if prev else 0) + pnl_change
    total_trades = prev["total_trades"] if prev else 0
    wins = prev["wins"] if prev else 0
    losses = prev["losses"] if prev else 0
    
    if pnl_change > 0:
        wins += 1
        total_trades += 1
    elif pnl_change < 0:
        losses += 1
        total_trades += 1
    
    win_rate = wins / total_trades if total_trades > 0 else 0
    peak = max(bankroll, prev["peak_bankroll"] if prev else STARTING_BANKROLL)
    drawdown = (peak - bankroll) / peak if peak > 0 else 0
    max_dd = max(drawdown, prev["max_drawdown"] if prev else 0)
    
    # Simple Sharpe: mean pnl / std pnl from closed trades
    closed = conn.execute("SELECT pnl FROM paper_positions WHERE status IN ('won','lost')").fetchall()
    if len(closed) >= 2:
        pnls = [r["pnl"] for r in closed]
        mean_pnl = sum(pnls) / len(pnls)
        var = sum((p - mean_pnl)**2 for p in pnls) / len(pnls)
        std = math.sqrt(var) if var > 0 else 1
        sharpe = (mean_pnl / std) * math.sqrt(252) if std > 0 else 0
    else:
        sharpe = 0
    
    conn.execute("""INSERT INTO paper_portfolio_state 
        (timestamp, bankroll, total_pnl, total_trades, wins, losses, win_rate, max_drawdown, peak_bankroll, current_drawdown_pct, sharpe_estimate)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now(timezone.utc).isoformat(), bankroll, total_pnl, total_trades, wins, losses, win_rate, max_dd, peak, drawdown, sharpe))
    conn.commit()


def evaluate_signal(signal: dict) -> dict:
    """Check if signal meets criteria, calculate bet size."""
    confidence = signal.get("confidence", 0)
    if isinstance(confidence, str):
        confidence = float(confidence.replace("%", "")) / 100
    if confidence > 1:
        confidence = confidence / 100
    
    market_price = signal.get("entry_price") or signal.get("price") or signal.get("market_price", 0.5)
    if isinstance(market_price, str):
        market_price = float(market_price)
    if market_price > 1:
        market_price = market_price / 100
    
    side = (signal.get("side") or signal.get("direction") or "YES").upper()
    
    # Price floor/ceiling filter — reject garbage and near-certain contracts
    # "Namibia wins World Cup" at 0.1¢ = garbage, don't buy it
    effective_price = market_price if side == "YES" else (1 - market_price)
    if effective_price < MIN_PRICE:
        return {"eligible": False, "reason": f"Price {effective_price:.1%} below floor {MIN_PRICE:.0%} — garbage contract", "edge": 0, "kelly_pct": 0, "bet_size": 0}
    if effective_price > MAX_PRICE:
        return {"eligible": False, "reason": f"Price {effective_price:.1%} above ceiling {MAX_PRICE:.0%} — no edge", "edge": 0, "kelly_pct": 0, "bet_size": 0}
    
    # ─── Phase 1: Empirical Confidence Override ─────────────
    empirical_result = None
    if HAS_EMPIRICAL:
        try:
            market_title = signal.get("market") or signal.get("market_title") or signal.get("title", "")
            empirical_result = calculate_empirical_confidence(market_title, side or "YES", market_price)
            if empirical_result["killed"]:
                return {"eligible": False, "reason": f"Kill rule: {empirical_result['kill_reason']}", "edge": 0, "kelly_pct": 0, "bet_size": 0, "empirical": empirical_result}
            confidence = empirical_result["confidence"]
        except Exception:
            pass  # Fallback to old confidence

    if side == "YES":
        edge = confidence - market_price
        odds = (1 / market_price) - 1 if market_price > 0 else 0
    else:
        edge = confidence - (1 - market_price)
        odds = (1 / (1 - market_price)) - 1 if market_price < 1 else 0
    
    if confidence < MIN_CONFIDENCE:
        return {"eligible": False, "reason": f"Confidence {confidence:.0%} < {MIN_CONFIDENCE:.0%}", "edge": edge, "kelly_pct": 0, "bet_size": 0}
    if edge < MIN_EDGE:
        return {"eligible": False, "reason": f"Edge {edge:.1%} < {MIN_EDGE:.0%}", "edge": edge, "kelly_pct": 0, "bet_size": 0}
    
    kelly_pct = edge / odds if odds > 0 else 0
    
    conn = _get_db()
    bankroll = _get_bankroll(conn)
    open_count = _count_open(conn)
    conn.close()
    
    if open_count >= MAX_CONCURRENT:
        return {"eligible": False, "reason": f"Max {MAX_CONCURRENT} concurrent positions", "edge": edge, "kelly_pct": kelly_pct, "bet_size": 0}
    
    bet_size = bankroll * kelly_pct * KELLY_FRACTION
    bet_size = max(MIN_BET, min(MAX_BET, bet_size))
    
    if bet_size > bankroll:
        return {"eligible": False, "reason": f"Insufficient bankroll ${bankroll:.2f}", "edge": edge, "kelly_pct": kelly_pct, "bet_size": 0}
    
    return {"eligible": True, "bet_size": round(bet_size, 2), "edge": round(edge, 4), "kelly_pct": round(kelly_pct, 4), "reason": "Criteria met", "empirical": empirical_result}


def open_position(signal: dict) -> dict:
    """Open a paper position if criteria met."""
    eval_result = evaluate_signal(signal)
    if not eval_result["eligible"]:
        return {"opened": False, **eval_result}
    
    market_id = signal.get("market_id") or signal.get("ticker") or signal.get("id", "unknown")
    side = (signal.get("side") or signal.get("direction") or "YES").upper()
    market_price = signal.get("entry_price") or signal.get("price") or signal.get("market_price", 0.5)
    if isinstance(market_price, str):
        market_price = float(market_price)
    if market_price > 1:
        market_price = market_price / 100
    
    # Price floor/ceiling filter — reject garbage and near-certain contracts
    effective_price = market_price if side == "YES" else (1 - market_price)
    if effective_price < MIN_PRICE:
        return {"eligible": False, "reason": f"Price {effective_price:.1%} below floor {MIN_PRICE:.0%}", "edge": 0, "kelly_pct": 0, "bet_size": 0}
    if effective_price > MAX_PRICE:
        return {"eligible": False, "reason": f"Price {effective_price:.1%} above ceiling {MAX_PRICE:.0%}", "edge": 0, "kelly_pct": 0, "bet_size": 0}
    
    confidence = signal.get("confidence", 0)
    if isinstance(confidence, str):
        confidence = float(confidence.replace("%", "")) / 100
    if confidence > 1:
        confidence = confidence / 100
    
    bet_size = eval_result["bet_size"]
    
    if side == "YES":
        potential_payout = bet_size * (1 / market_price - 1)
    else:
        potential_payout = bet_size * (1 / (1 - market_price) - 1)
    
    conn = _get_db()
    
    # Check not already tracking this market
    existing = conn.execute("SELECT id FROM paper_positions WHERE market_id=? AND status='open'", (market_id,)).fetchone()
    if existing:
        conn.close()
        return {"opened": False, "reason": "Already tracking this market"}
    
    conn.execute("""INSERT INTO paper_positions 
        (opened_at, market_id, market_title, platform, side, entry_price, bet_size, potential_payout, confidence, edge_pct, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
        (datetime.now(timezone.utc).isoformat(), market_id,
         (signal.get("market") or signal.get("market_title") or signal.get("title", ""))[:120],
         signal.get("platform", "kalshi"), side, market_price, bet_size,
         round(potential_payout, 2), confidence, eval_result["edge"]))
    conn.commit()
    conn.close()
    
    return {"opened": True, "market_id": market_id, "side": side, "bet_size": bet_size, "edge": eval_result["edge"], "potential_payout": round(potential_payout, 2)}


def close_position(market_id: str, outcome: str, exit_price: float = None) -> dict:
    """Close a position. outcome: 'won' or 'lost'."""
    conn = _get_db()
    pos = conn.execute("SELECT * FROM paper_positions WHERE market_id=? AND status='open'", (market_id,)).fetchone()
    if not pos:
        conn.close()
        return {"closed": False, "reason": "No open position for this market"}
    
    bet_size = pos["bet_size"]
    entry_price = pos["entry_price"]
    side = pos["side"]
    
    if outcome == "won":
        if side == "YES":
            pnl = bet_size * (1 / entry_price - 1)
        else:
            pnl = bet_size * (1 / (1 - entry_price) - 1)
    else:
        pnl = -bet_size
    
    bankroll = _get_bankroll(conn) + pnl
    
    conn.execute("""UPDATE paper_positions SET status=?, closed_at=?, exit_price=?, pnl=?, close_reason=?
        WHERE id=?""",
        (outcome, datetime.now(timezone.utc).isoformat(), exit_price or (1.0 if outcome == "won" else 0.0),
         round(pnl, 2), outcome, pos["id"]))
    conn.commit()
    
    _save_state(conn, bankroll, pnl)
    conn.close()
    
    return {"closed": True, "market_id": market_id, "pnl": round(pnl, 2), "new_bankroll": round(bankroll, 2)}


def get_portfolio_status() -> dict:
    conn = _get_db()
    bankroll = _get_bankroll(conn)
    peak = _get_peak(conn)
    
    open_positions = [dict(r) for r in conn.execute("SELECT * FROM paper_positions WHERE status='open' ORDER BY opened_at DESC").fetchall()]
    
    state = conn.execute("SELECT * FROM paper_portfolio_state ORDER BY id DESC LIMIT 1").fetchone()
    
    closed_count = conn.execute("SELECT COUNT(*) as c FROM paper_positions WHERE status IN ('won','lost')").fetchone()["c"]
    won_count = conn.execute("SELECT COUNT(*) as c FROM paper_positions WHERE status='won'").fetchone()["c"]
    
    conn.close()
    
    drawdown_pct = (peak - bankroll) / peak * 100 if peak > 0 else 0
    
    return {
        "bankroll": round(bankroll, 2),
        "starting_bankroll": STARTING_BANKROLL,
        "total_pnl": round(bankroll - STARTING_BANKROLL, 2),
        "total_pnl_pct": round((bankroll - STARTING_BANKROLL) / STARTING_BANKROLL * 100, 2),
        "open_positions": len(open_positions),
        "positions": open_positions,
        "total_trades": closed_count,
        "wins": won_count,
        "losses": closed_count - won_count,
        "win_rate": round(won_count / closed_count * 100, 1) if closed_count > 0 else 0,
        "peak_bankroll": round(peak, 2),
        "current_drawdown_pct": round(drawdown_pct, 2),
        "max_drawdown": round(state["max_drawdown"] * 100, 2) if state else 0,
        "sharpe_estimate": round(state["sharpe_estimate"], 2) if state else 0,
    }


def get_positions(status: str = "all") -> dict:
    conn = _get_db()
    if status == "open":
        rows = conn.execute("SELECT * FROM paper_positions WHERE status='open' ORDER BY opened_at DESC").fetchall()
    elif status == "closed":
        rows = conn.execute("SELECT * FROM paper_positions WHERE status IN ('won','lost','expired') ORDER BY closed_at DESC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM paper_positions ORDER BY opened_at DESC").fetchall()
    conn.close()
    return {"positions": [dict(r) for r in rows], "count": len(rows)}


def get_position_history(limit: int = 50) -> list:
    conn = _get_db()
    rows = conn.execute("SELECT * FROM paper_positions WHERE status IN ('won','lost','expired') ORDER BY closed_at DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def process_signals(signals: list) -> dict:
    """Process a batch of signals, open positions for eligible ones."""
    results = []
    opened = 0
    skipped = 0
    
    for sig in (signals or []):
        eval_result = evaluate_signal(sig)
        market_id = sig.get("market_id") or sig.get("ticker") or sig.get("id", "unknown")
        market_title = (sig.get("market") or sig.get("market_title") or sig.get("title", ""))[:80]
        
        entry = {
            "market_id": market_id,
            "market": market_title,
            "eligible": eval_result["eligible"],
            "reason": eval_result["reason"],
            "edge": eval_result.get("edge", 0),
            "bet_size": eval_result.get("bet_size", 0),
        }
        
        if eval_result["eligible"]:
            result = open_position(sig)
            if result.get("opened"):
                opened += 1
                entry["action"] = "opened"
            else:
                skipped += 1
                entry["action"] = "skipped"
                entry["reason"] = result.get("reason", eval_result["reason"])
        else:
            skipped += 1
            entry["action"] = "skipped"
        
        results.append(entry)
    
    status = get_portfolio_status()
    
    return {
        "processed": len(signals or []),
        "opened": opened,
        "skipped": skipped,
        "signals": results,
        "portfolio": {
            "bankroll": status["bankroll"],
            "open_positions": status["open_positions"],
            "total_pnl": status["total_pnl"],
        }
    }


def resolve_open_positions() -> dict:
    """Auto-resolve expired paper positions using Polymarket/Kalshi APIs.
    
    Called by watchdog every 5 minutes.
    """
    import json
    import urllib.request
    
    GAMMA_API = "https://gamma-api.polymarket.com"
    KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
    
    def _fetch(url, timeout=10):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except:
            return None
    
    conn = _get_db()
    open_positions = conn.execute("SELECT * FROM paper_positions WHERE status='open'").fetchall()
    
    if not open_positions:
        conn.close()
        return {"resolved": 0, "note": "No open positions"}
    
    resolved = 0
    total_pnl = 0
    
    for pos in open_positions:
        market_id = pos["market_id"]
        platform = pos["platform"] or "kalshi"
        outcome = None
        
        if platform == "polymarket" or market_id.startswith("0x"):
            data = _fetch(f"{GAMMA_API}/markets?condition_id={market_id}")
            if data and isinstance(data, list) and len(data) > 0:
                m = data[0]
                if m.get("closed") or m.get("resolved"):
                    outcome_raw = m.get("outcome")
                    if outcome_raw:
                        outcome = outcome_raw.upper()
                    else:
                        prices = m.get("outcomePrices")
                        if prices:
                            try:
                                pl = json.loads(prices) if isinstance(prices, str) else prices
                                if float(pl[0]) > 0.9: outcome = "YES"
                                elif float(pl[1]) > 0.9: outcome = "NO"
                            except: pass
        else:
            data = _fetch(f"{KALSHI_API}/markets/{market_id}")
            if data:
                market = data.get("market", data)
                result = market.get("result", "")
                if result:
                    outcome = result.upper()
        
        if not outcome:
            continue
        
        side = pos["side"]
        entry_price = pos["entry_price"]
        bet_size = pos["bet_size"]
        won = (outcome == side)
        
        if won:
            if side == "YES":
                pnl = bet_size * (1 / entry_price - 1)
            else:
                pnl = bet_size * (1 / (1 - entry_price) - 1)
            status = "won"
        else:
            pnl = -bet_size
            status = "lost"
        
        conn.execute("""UPDATE paper_positions SET status=?, closed_at=?, exit_price=?, pnl=?, close_reason=?
            WHERE id=?""",
            (status, datetime.now(timezone.utc).isoformat(),
             1.0 if won else 0.0, round(pnl, 2), f"auto-resolved: {outcome}", pos["id"]))
        
        resolved += 1
        total_pnl += pnl
    
    if resolved > 0:
        conn.commit()
        bankroll = _get_bankroll(conn) + total_pnl
        _save_state(conn, bankroll, total_pnl)
    
    conn.close()
    return {"resolved": resolved, "total_pnl": round(total_pnl, 2)}

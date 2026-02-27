#!/usr/bin/env python3
"""
Paper Portfolio Manager — Kelly-fractional sizing with SQLite tracking.
Falls back to JSON state files (~/.openclaw/paper-trading/) when SQLite is empty.
"""

import json
import sqlite3
try:
    from empirical_confidence import calculate_empirical_confidence
    HAS_EMPIRICAL = True
except ImportError:
    HAS_EMPIRICAL = False
try:
    from api.services.source_health import get_last_success_timestamp as _get_source_ts
    HAS_SOURCE_HEALTH = True
except ImportError:
    HAS_SOURCE_HEALTH = False
try:
    from volume_spike_detector import detect_spike as _detect_volume_spike
    HAS_VOLUME_SPIKE = True
except ImportError:
    HAS_VOLUME_SPIKE = False
try:
    from time_decay_optimizer import get_time_decay_modifier
    HAS_TIME_DECAY = True
except ImportError:
    HAS_TIME_DECAY = False
try:
    from price_momentum_filter import check_entry as _check_momentum
    HAS_MOMENTUM = True
except ImportError:
    HAS_MOMENTUM = False
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "storage" / "shadow_trades.db"
JSON_DIR = Path.home() / ".openclaw" / "paper-trading"

# ─── Correlation Cap ────────────────────────────────────────
# Archetypes that move together are grouped. Max N open positions per group.
CORRELATION_GROUPS = {
    "price_above": "crypto", "price_range": "crypto",
    "daily_updown": "crypto", "intraday_updown": "crypto",
    "directional": "crypto",
    "sports_single_game": "sports", "sports_winner": "sports",
    "game_total": "sports",
    "election": "geopolitical", "geopolitical": "geopolitical",
    "deadline_binary": "politics",
    "financial_price": "finance",
    "entertainment": "culture", "ai_model": "culture",
    "social_count": "culture", "weather": "culture",
    "parlay": "other", "other": "other",
}
MAX_PER_GROUP = 3


def _check_correlation_cap(archetype: str, conn) -> Optional[str]:
    """Return block reason if correlation group is full, else None."""
    group = CORRELATION_GROUPS.get(archetype, "other")
    sibling_archetypes = [a for a, g in CORRELATION_GROUPS.items() if g == group]
    placeholders = ",".join("?" * len(sibling_archetypes))
    row = conn.execute(
        f"SELECT COUNT(*) as c FROM paper_positions WHERE status='open' AND archetype IN ({placeholders})",
        sibling_archetypes
    ).fetchone()
    count = row["c"]
    logger.debug(
        "Correlation cap check: archetype=%s group=%s open=%d/%d siblings=%s",
        archetype, group, count, MAX_PER_GROUP, sibling_archetypes
    )
    if count >= MAX_PER_GROUP:
        logger.info(
            "BLOCKED by correlation cap: archetype=%s group=%s open=%d/%d",
            archetype, group, count, MAX_PER_GROUP
        )
        return f"Correlation cap: {group} {count}/{MAX_PER_GROUP}"
    return None


def get_correlation_status() -> dict:
    """Return current open position counts per correlation group for debugging."""
    conn = _get_db()
    rows = conn.execute(
        "SELECT archetype, COUNT(*) as c FROM paper_positions WHERE status='open' GROUP BY archetype"
    ).fetchall()
    conn.close()

    groups: Dict[str, dict] = {}
    for row in rows:
        arch = row["archetype"] or "other"
        group = CORRELATION_GROUPS.get(arch, "other")
        if group not in groups:
            groups[group] = {"count": 0, "max": MAX_PER_GROUP, "archetypes": {}}
        groups[group]["count"] += row["c"]
        groups[group]["archetypes"][arch] = row["c"]

    # Include empty groups for completeness
    all_groups = set(CORRELATION_GROUPS.values())
    for g in all_groups:
        if g not in groups:
            groups[g] = {"count": 0, "max": MAX_PER_GROUP, "archetypes": {}}

    for g in groups.values():
        g["full"] = g["count"] >= g["max"]

    return groups


def _get_dynamic_kelly(conn) -> Dict[str, Any]:
    """Calculate dynamic Kelly fraction based on rolling performance.

    Returns:
        {"fraction": float, "rolling_wr": float, "rolling_trades": int,
         "drawdown_pct": float, "status": str, "reason": str}
    """
    # Rolling win rate from last N closed trades
    rows = conn.execute(
        "SELECT status FROM paper_positions WHERE status IN ('won','lost') ORDER BY closed_at DESC LIMIT ?",
        (KELLY_ROLLING_WINDOW,)
    ).fetchall()

    rolling_trades = len(rows)
    wins = sum(1 for r in rows if r["status"] == "won")
    rolling_wr = wins / rolling_trades if rolling_trades > 0 else 0.5

    # Current drawdown
    bankroll = _get_bankroll(conn)
    peak = _get_peak(conn)
    drawdown_pct = (peak - bankroll) / peak if peak > 0 else 0

    # Decision logic
    if drawdown_pct >= DRAWDOWN_PAUSE_PCT:
        fraction = 0
        status = "paused"
        reason = f"Drawdown {drawdown_pct:.1%} >= {DRAWDOWN_PAUSE_PCT:.0%} — trading paused"
        logger.warning("🛑 KELLY PAUSED: drawdown=%.1f%% (threshold %.0f%%)", drawdown_pct * 100, DRAWDOWN_PAUSE_PCT * 100)
    elif rolling_trades < BOOTSTRAP_TRADES:
        # Bootstrap mode: seed WR assumption until enough data
        fraction = KELLY_FRACTION_BOOTSTRAP
        rolling_wr = BOOTSTRAP_WR  # Override with Becker-validated WR
        status = "bootstrap"
        reason = f"Bootstrap mode: {rolling_trades}/{BOOTSTRAP_TRADES} trades — seeded {BOOTSTRAP_WR:.0%} WR, 1/8 Kelly"
        logger.info("🚀 KELLY BOOTSTRAP: trades=%d/%d fraction=1/8 seeded_wr=%.0f%%", rolling_trades, BOOTSTRAP_TRADES, BOOTSTRAP_WR * 100)
    elif rolling_wr < KELLY_MIN_WR:
        fraction = KELLY_FRACTION_COLD
        status = "cold"
        reason = f"WR {rolling_wr:.0%} < {KELLY_MIN_WR:.0%} over {rolling_trades} trades — half size"
        logger.info("❄️ KELLY COLD: wr=%.0f%% trades=%d fraction=1/%d", rolling_wr * 100, rolling_trades, int(1/fraction))
    else:
        fraction = KELLY_FRACTION
        status = "normal"
        reason = f"WR {rolling_wr:.0%} over {rolling_trades} trades — full size"
        logger.debug("Kelly normal: wr=%.0f%% trades=%d", rolling_wr * 100, rolling_trades)

    return {
        "fraction": fraction,
        "rolling_wr": round(rolling_wr, 3),
        "rolling_trades": rolling_trades,
        "drawdown_pct": round(drawdown_pct, 4),
        "status": status,
        "reason": reason,
    }


def get_kelly_status() -> Dict[str, Any]:
    """Return current Kelly status for dashboard/API."""
    conn = _get_db()
    result = _get_dynamic_kelly(conn)
    conn.close()
    return result


STARTING_BANKROLL = 10000.0
KELLY_FRACTION = 1 / 6        # Becker-validated: 79% NO WR on high-conviction filters supports 1/6
KELLY_FRACTION_COLD = 1 / 12  # Half size when win rate drops
KELLY_ROLLING_WINDOW = 20     # Trades to evaluate rolling WR
KELLY_MIN_WR = 0.55           # Below this → downshift to KELLY_FRACTION_COLD
DRAWDOWN_PAUSE_PCT = 0.15     # 15% drawdown → pause trading
BOOTSTRAP_TRADES = 20         # Minimum trades before trusting rolling stats
BOOTSTRAP_WR = 0.57           # Seeded WR during bootstrap (Becker-validated)
KELLY_FRACTION_BOOTSTRAP = 1 / 8  # Between cold (1/12) and normal (1/6)
MAX_CONCURRENT = 10
MIN_CONFIDENCE = 0.50
MIN_EDGE = 0.12  # 12% minimum edge — lowered from 15% (Becker validates edges as low as 12%)
MIN_PRICE = 0.05  # Price floor — reject garbage contracts below 5 cents
MAX_PRICE = 0.95  # Price ceiling — reject near-certain markets (no edge)
MIN_BET = 100.0  # Bootstrap: meaningful minimum bet size
MAX_BET = 1000.0  # Scaled for $10K bankroll

# Archetype filters — data-driven from resolved trades
ARCHETYPE_BLOCKLIST = {"price_above", "sports_winner"}  # 0% WR, -100% ROI across 7 trades
ARCHETYPE_BOOST = {"sports_single_game": 1.3, "social_count": 1.3}  # Proven +180% blended ROI
MIN_NO_IMPLIED_PROB = 0.35  # Minimum implied NO probability (1 - entry_price for NO bets)
MIN_EXPIRY_HOURS = 72  # Minimum 3 days to resolution for crypto/price markets


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
    # Migration: add archetype column if missing
    try:
        conn.execute("ALTER TABLE paper_positions ADD COLUMN archetype TEXT DEFAULT 'other'")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists
    # Migration: add strategy column if missing
    try:
        conn.execute("ALTER TABLE paper_positions ADD COLUMN strategy TEXT DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists
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
    
    # ─── Source Staleness Check ─────────────────────────────
    if HAS_SOURCE_HEALTH:
        import time as _time
        platform = (signal.get("platform") or "kalshi").lower()
        source_map = {"kalshi": "kalshi", "polymarket": "polymarket_gamma", "manifold": "manifold"}
        primary_source = source_map.get(platform, platform)
        ts = _get_source_ts(primary_source)
        if ts:
            age = _time.time() - ts
            if age > 3600:
                logger.debug("Staleness reject: %s data is %.0fs old", primary_source, age)
                return {"eligible": False, "reason": f"Stale data: {primary_source} is {age:.0f}s old (>3600s)", "edge": 0, "kelly_pct": 0, "bet_size": 0}
    
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

    # Archetype blocklist — proven unprofitable archetypes
    try:
        from mispriced_category_signal import classify_archetype
    except ImportError:
        classify_archetype = lambda s: s.get("archetype", "unknown")
    archetype = classify_archetype(signal)
    if archetype in ARCHETYPE_BLOCKLIST:
        logger.info("🚫 BLOCKED archetype=%s market=%s (0%% WR, -100%% ROI)", archetype, signal.get("market", "")[:40])
        return {"eligible": False, "reason": f"Blocked archetype: {archetype} (0% historical WR)", "edge": edge, "kelly_pct": 0, "bet_size": 0}

    # Minimum NO implied probability — reject if market is too efficient
    if side == "NO" and effective_price < MIN_NO_IMPLIED_PROB:
        logger.info("🚫 BLOCKED low NO prob=%.0f%% market=%s", effective_price*100, signal.get("market", "")[:40])
        return {"eligible": False, "reason": f"NO implied prob {effective_price:.0%} < {MIN_NO_IMPLIED_PROB:.0%} — market too efficient", "edge": edge, "kelly_pct": 0, "bet_size": 0}
    
    kelly_pct = edge / odds if odds > 0 else 0
    
    conn = _get_db()
    bankroll = _get_bankroll(conn)
    open_count = _count_open(conn)
    
    if open_count >= MAX_CONCURRENT:
        conn.close()
        return {"eligible": False, "reason": f"Max {MAX_CONCURRENT} concurrent positions", "edge": edge, "kelly_pct": kelly_pct, "bet_size": 0}
    
    # Dynamic Kelly — adjusts fraction based on rolling performance
    kelly_data = _get_dynamic_kelly(conn)
    conn.close()
    
    if kelly_data["status"] == "paused":
        return {"eligible": False, "reason": kelly_data["reason"], "edge": edge, "kelly_pct": kelly_pct, "bet_size": 0, "kelly": kelly_data}
    
    effective_kelly = kelly_data["fraction"]
    bet_size = bankroll * kelly_pct * effective_kelly
    
    # Becker time decay: duration × volume modifier (replaces simple duration boost)
    days_to_close = signal.get("days_to_close", 7)
    volume = signal.get("volume", 0)
    if isinstance(volume, str):
        try:
            volume = float(volume)
        except (ValueError, TypeError):
            volume = 0
    time_decay_data = None
    if HAS_TIME_DECAY:
        time_decay_data = get_time_decay_modifier(days_to_close, volume, side)
        bet_size *= time_decay_data["multiplier"]
        logger.debug("Time decay applied: mult=%.3f no_wr=%.1f%% dur=%s vol=%s",
                      time_decay_data["multiplier"], time_decay_data["no_wr"] * 100,
                      time_decay_data["duration"], time_decay_data["volume_bucket"])
    else:
        # Fallback: old simple duration modifier
        if days_to_close >= 28:
            bet_size *= 1.15
        elif days_to_close >= 7:
            bet_size *= 1.10
        elif days_to_close < 1:
            bet_size *= 0.85
    
    # Volume spike boost: retail FOMO = YES overpriced = best NO entry
    volume_spike_data = None
    if HAS_VOLUME_SPIKE and side == "NO":
        market_id = signal.get("market_id") or signal.get("ticker") or signal.get("id", "")
        volume = signal.get("volume", 0)
        if isinstance(volume, str):
            try:
                volume = int(float(volume))
            except (ValueError, TypeError):
                volume = 0
        if market_id and volume > 0:
            volume_spike_data = _detect_volume_spike(market_id, volume)
            if volume_spike_data.get("spike"):
                if volume_spike_data["level"] == "mega":
                    bet_size *= 1.20  # 10x+ volume = extreme FOMO, 20% boost
                    logger.info("Volume MEGA spike boost: market=%s ratio=%.1fx bet_size=%.2f", market_id[:30], volume_spike_data["ratio"], bet_size)
                else:
                    bet_size *= 1.10  # 3x+ volume = FOMO, 10% boost
                    logger.info("Volume spike boost: market=%s ratio=%.1fx bet_size=%.2f", market_id[:30], volume_spike_data["ratio"], bet_size)

    # Archetype boost — proven profitable archetypes get larger size
    if archetype in ARCHETYPE_BOOST:
        boost = ARCHETYPE_BOOST[archetype]
        bet_size *= boost
        logger.info("🎯 Archetype boost: %s x%.1f bet_size=%.2f", archetype, boost, bet_size)

    bet_size = max(MIN_BET, min(MAX_BET, bet_size))
    
    if bet_size > bankroll:
        return {"eligible": False, "reason": f"Insufficient bankroll ${bankroll:.2f}", "edge": edge, "kelly_pct": kelly_pct, "bet_size": 0}
    
    return {"eligible": True, "bet_size": round(bet_size, 2), "edge": round(edge, 4), "kelly_pct": round(kelly_pct, 4), "reason": "Criteria met", "empirical": empirical_result, "volume_spike": volume_spike_data, "time_decay": time_decay_data, "kelly": kelly_data}


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

    # Classify archetype for breakdown tracking
    market_title = (signal.get("market") or signal.get("market_title") or signal.get("title", ""))[:120]
    archetype = "other"
    try:
        from mispriced_category_signal import classify_archetype
        archetype = classify_archetype(market_title)
    except Exception:
        pass

    # Strategy field (e.g., "price_to_strike", "no_fade")
    strategy = signal.get("strategy", "")

    # Cross-strategy agreement: if price_to_strike and NO fade agree, boost; if disagree, halve
    if strategy == "price_to_strike":
        # Check if NO fade also has a signal on this market
        try:
            _cross_side = signal.get("cross_strategy_side")
            if _cross_side:
                if _cross_side == side:
                    bet_size *= 1.2  # Agreement boost
                    logger.info("Cross-strategy AGREE boost 1.2x: %s %s", market_id[:30], side)
                else:
                    bet_size *= 0.5  # Disagreement halve
                    logger.info("Cross-strategy DISAGREE halve: %s %s vs %s", market_id[:30], side, _cross_side)
                bet_size = max(MIN_BET, min(MAX_BET, bet_size))
        except Exception:
            pass

    conn = _get_db()

    # Check not already tracking this market
    existing = conn.execute("SELECT id FROM paper_positions WHERE market_id=? AND status='open'", (market_id,)).fetchone()
    if existing:
        conn.close()
        return {"opened": False, "reason": "Already tracking this market"}

    # Price momentum filter — only bet NO when YES is rising or flat
    momentum_data = None
    if HAS_MOMENTUM and side == "NO":
        mom_result = _check_momentum(market_id, market_price, side)
        momentum_data = mom_result.get("momentum_data")
        if not mom_result["allow"]:
            logger.info("Position blocked by momentum: market=%s reason=%s", market_id, mom_result.get("reason"))
            conn.close()
            return {"opened": False, "reason": mom_result.get("reason", "Momentum filter"), "archetype": archetype, "edge": eval_result["edge"], "momentum": momentum_data}
        if mom_result["multiplier"] > 1.0:
            bet_size *= mom_result["multiplier"]
            bet_size = min(MAX_BET, bet_size)
            logger.info("Momentum boost: market=%s mult=%.2f new_bet=%.2f", market_id[:30], mom_result["multiplier"], bet_size)

    # Correlation cap — max positions per correlated group
    cap_reason = _check_correlation_cap(archetype, conn)
    if cap_reason:
        logger.info("Position blocked: market=%s archetype=%s reason=%s", market_id, archetype, cap_reason)
        conn.close()
        return {"opened": False, "reason": cap_reason, "archetype": archetype, "edge": eval_result["edge"]}

    conn.execute("""INSERT INTO paper_positions
        (opened_at, market_id, market_title, platform, side, entry_price, bet_size, potential_payout, confidence, edge_pct, status, archetype, strategy)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
        (datetime.now(timezone.utc).isoformat(), market_id, market_title,
         signal.get("platform", "kalshi"), side, market_price, bet_size,
         round(potential_payout, 2), confidence, eval_result["edge"], archetype, strategy))
    conn.commit()
    conn.close()

    return {"opened": True, "market_id": market_id, "side": side, "bet_size": bet_size, "edge": eval_result["edge"], "potential_payout": round(potential_payout, 2), "archetype": archetype}


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


def _load_json(filename, default=None):
    """Read a JSON file from the legacy paper-trading directory."""
    path = JSON_DIR / filename
    if not path.exists():
        return default if default is not None else {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return default if default is not None else {}


def _convert_json_position(pos: dict) -> dict:
    """Map legacy JSON position fields to the portfolio format the frontend expects."""
    market_id = pos.get("market_id") or pos.get("id", "unknown")
    entry_price = pos.get("entry_price", 0.5)
    side = (pos.get("side") or "YES").upper()
    cost_basis = pos.get("cost_basis") or pos.get("amount", 0)

    # Compute potential payout from shares
    shares = pos.get("shares", 0)
    if side == "YES":
        potential_payout = max(0, shares - cost_basis)
    else:
        potential_payout = max(0, shares - cost_basis)

    # Detect platform from market_id format
    platform = pos.get("platform", "")
    if not platform:
        platform = "polymarket" if str(market_id).startswith("0x") or str(market_id).startswith("pos_") else "kalshi"

    confidence = pos.get("entry_confidence", pos.get("confidence", 0))
    if isinstance(confidence, (int, float)) and confidence > 1:
        confidence = confidence / 100

    return {
        "id": market_id,
        "market_id": market_id,
        "market_title": pos.get("market") or pos.get("market_question") or pos.get("market_title", ""),
        "platform": platform,
        "side": side,
        "entry_price": entry_price,
        "bet_size": cost_basis,
        "potential_payout": round(potential_payout, 2),
        "confidence": confidence,
        "edge_pct": pos.get("entry_ev") or pos.get("edge_pct", 0),
        "status": pos.get("status", "open"),
        "opened_at": pos.get("opened_at", ""),
        "closed_at": pos.get("resolved_at"),
        "pnl": pos.get("pnl"),
    }


def _get_status_from_json() -> dict:
    """Build portfolio status from legacy JSON state files."""
    balance_data = _load_json("balance.json", {"usdc": STARTING_BANKROLL})
    bankroll = balance_data.get("usdc", STARTING_BANKROLL)

    raw_positions = _load_json("positions.json", [])
    if not isinstance(raw_positions, list):
        raw_positions = []
    positions = [_convert_json_position(p) for p in raw_positions]
    open_positions = [p for p in positions if p.get("status") == "open"]

    # Count resolved trades from trades.json
    raw_trades = _load_json("trades.json", [])
    if not isinstance(raw_trades, list):
        raw_trades = []
    resolved = [t for t in raw_trades if t.get("type") in ("SELL", "RESOLVE")]
    wins = sum(1 for t in resolved if (t.get("pnl") or 0) > 0)
    losses = len(resolved) - wins

    total_pnl = bankroll - STARTING_BANKROLL
    peak = max(bankroll, STARTING_BANKROLL)

    # Risk exposure from open positions
    capital_at_risk = sum(p.get("bet_size", 0) for p in open_positions)
    max_loss = -capital_at_risk
    max_gain = sum(p.get("potential_payout", 0) for p in open_positions)

    return {
        "bankroll": round(bankroll, 2),
        "starting_bankroll": STARTING_BANKROLL,
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / STARTING_BANKROLL * 100, 2) if STARTING_BANKROLL else 0,
        "open_positions": len(open_positions),
        "max_positions": MAX_CONCURRENT,
        "positions": open_positions,
        "total_trades": len(resolved),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(resolved) * 100, 1) if resolved else 0,
        "peak_bankroll": round(peak, 2),
        "current_drawdown_pct": round((peak - bankroll) / peak * 100, 2) if peak > 0 else 0,
        "max_drawdown": 0,
        "sharpe_estimate": 0,
        "capital_at_risk": round(capital_at_risk, 2),
        "max_loss": round(max_loss, 2),
        "max_gain": round(max_gain, 2),
        "source": "json",
    }


def _get_positions_from_json(status: str = "all") -> dict:
    """Read positions from legacy JSON files."""
    raw_positions = _load_json("positions.json", [])
    if not isinstance(raw_positions, list):
        raw_positions = []
    positions = [_convert_json_position(p) for p in raw_positions]

    if status == "open":
        positions = [p for p in positions if p.get("status") == "open"]
    elif status == "closed":
        positions = [p for p in positions if p.get("status") in ("won", "lost", "expired")]

    return {"positions": positions, "count": len(positions)}


def get_portfolio_status() -> dict:
    conn = _get_db()
    total_rows = conn.execute("SELECT COUNT(*) as c FROM paper_positions").fetchone()["c"]
    state = conn.execute("SELECT * FROM paper_portfolio_state ORDER BY id DESC LIMIT 1").fetchone()

    # Fall back to JSON if SQLite has no data
    if total_rows == 0 and state is None:
        conn.close()
        return _get_status_from_json()

    bankroll = _get_bankroll(conn)
    peak = _get_peak(conn)

    open_positions = [dict(r) for r in conn.execute("SELECT * FROM paper_positions WHERE status='open' ORDER BY opened_at DESC").fetchall()]

    closed_count = conn.execute("SELECT COUNT(*) as c FROM paper_positions WHERE status IN ('won','lost')").fetchone()["c"]
    won_count = conn.execute("SELECT COUNT(*) as c FROM paper_positions WHERE status='won'").fetchone()["c"]

    conn.close()

    drawdown_pct = (peak - bankroll) / peak * 100 if peak > 0 else 0

    # Risk exposure from open positions
    capital_at_risk = sum(p.get("bet_size", 0) for p in open_positions)
    max_loss = -capital_at_risk
    max_gain = sum(p.get("potential_payout", 0) for p in open_positions)

    return {
        "bankroll": round(bankroll, 2),
        "starting_bankroll": STARTING_BANKROLL,
        "total_pnl": round(bankroll - STARTING_BANKROLL, 2),
        "total_pnl_pct": round((bankroll - STARTING_BANKROLL) / STARTING_BANKROLL * 100, 2),
        "open_positions": len(open_positions),
        "max_positions": MAX_CONCURRENT,
        "positions": open_positions,
        "total_trades": closed_count,
        "wins": won_count,
        "losses": closed_count - won_count,
        "win_rate": round(won_count / closed_count * 100, 1) if closed_count > 0 else 0,
        "peak_bankroll": round(peak, 2),
        "current_drawdown_pct": round(drawdown_pct, 2),
        "max_drawdown": round(state["max_drawdown"] * 100, 2) if state else 0,
        "sharpe_estimate": round(state["sharpe_estimate"], 2) if state else 0,
        "capital_at_risk": round(capital_at_risk, 2),
        "max_loss": round(max_loss, 2),
        "max_gain": round(max_gain, 2),
    }


def get_positions(status: str = "all") -> dict:
    conn = _get_db()
    total = conn.execute("SELECT COUNT(*) as c FROM paper_positions").fetchone()["c"]
    if total == 0:
        conn.close()
        return _get_positions_from_json(status)
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


def get_live_positions() -> dict:
    """Get open positions enriched with current market prices and unrealized P&L.

    Fetches live prices from Polymarket CLOB / Kalshi APIs.
    """
    import urllib.request

    CLOB_API = "https://clob.polymarket.com"
    KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

    def _fetch(url, timeout=8):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception:
            return None

    conn = _get_db()
    rows = conn.execute("SELECT * FROM paper_positions WHERE status='open' ORDER BY opened_at DESC").fetchall()
    conn.close()

    positions = []
    total_unrealized = 0.0

    for pos in rows:
        p = dict(pos)
        market_id = p["market_id"]
        platform = p.get("platform") or "kalshi"
        side = p["side"]
        entry_price = p["entry_price"]
        bet_size = p["bet_size"]
        current_price = None

        # Fetch current YES price
        if platform == "polymarket" or market_id.startswith("0x"):
            data = _fetch(f"{CLOB_API}/markets/{market_id}")
            if data:
                tokens = data.get("tokens", [])
                if tokens:
                    # First token is YES side
                    current_price = float(tokens[0].get("price", 0))
                    p["market_slug"] = data.get("market_slug", "")
        else:
            data = _fetch(f"{KALSHI_API}/markets/{market_id}")
            if data:
                market = data.get("market", data)
                current_price = market.get("last_price")
                if current_price and current_price > 1:
                    current_price = current_price / 100

        if current_price is not None:
            if side == "YES":
                # Bought YES at entry_price, now worth current_price
                unrealized = bet_size * (current_price / entry_price - 1)
            else:
                # Bought NO at (1-entry_price), now worth (1-current_price)
                no_entry = 1 - entry_price
                no_current = 1 - current_price
                unrealized = bet_size * (no_current / no_entry - 1) if no_entry > 0 else 0
            p["current_price"] = round(current_price, 4)
            p["unrealized_pnl"] = round(unrealized, 2)
            total_unrealized += unrealized
        else:
            p["current_price"] = None
            p["unrealized_pnl"] = None

        # Hold time in days
        if p.get("opened_at"):
            try:
                opened = datetime.fromisoformat(p["opened_at"].replace("Z", "+00:00"))
                hold_days = (datetime.now(timezone.utc) - opened).days
                p["hold_days"] = hold_days
                p["stale"] = False  # Staleness based on price data freshness, not hold time
            except Exception:
                p["hold_days"] = 0
                p["stale"] = False
        else:
            p["hold_days"] = 0
            p["stale"] = False

        positions.append(p)

    return {
        "positions": positions,
        "count": len(positions),
        "total_unrealized_pnl": round(total_unrealized, 2),
    }


def get_archetype_breakdown() -> dict:
    """Compute win rate and P&L breakdown by archetype from closed trades."""
    conn = _get_db()
    closed = conn.execute(
        "SELECT archetype, status, pnl, bet_size, opened_at, closed_at FROM paper_positions WHERE status IN ('won','lost')"
    ).fetchall()

    # Backfill archetype for pre-migration positions (NULL, empty, or default 'other')
    nulls = conn.execute(
        "SELECT id, market_title FROM paper_positions WHERE archetype IS NULL OR archetype = '' OR archetype = 'other'"
    ).fetchall()
    if nulls:
        try:
            from mispriced_category_signal import classify_archetype
            for row in nulls:
                arch = classify_archetype(row["market_title"])
                conn.execute("UPDATE paper_positions SET archetype=? WHERE id=?", (arch, row["id"]))
            conn.commit()
            # Re-fetch
            closed = conn.execute(
                "SELECT archetype, status, pnl, bet_size, opened_at, closed_at FROM paper_positions WHERE status IN ('won','lost')"
            ).fetchall()
        except Exception:
            pass

    conn.close()

    buckets = {}
    total_hold_days = 0
    total_closed = 0

    for row in closed:
        arch = row["archetype"] or "other"
        if arch not in buckets:
            buckets[arch] = {"wins": 0, "losses": 0, "pnl": 0.0, "bet_total": 0.0, "hold_days": []}

        b = buckets[arch]
        if row["status"] == "won":
            b["wins"] += 1
        else:
            b["losses"] += 1
        b["pnl"] += row["pnl"] or 0
        b["bet_total"] += row["bet_size"] or 0

        # Hold time
        if row["opened_at"] and row["closed_at"]:
            try:
                opened = datetime.fromisoformat(row["opened_at"].replace("Z", "+00:00"))
                closed_dt = datetime.fromisoformat(row["closed_at"].replace("Z", "+00:00"))
                days = (closed_dt - opened).total_seconds() / 86400
                b["hold_days"].append(days)
                total_hold_days += days
                total_closed += 1
            except Exception:
                pass

    breakdown = []
    for arch, b in sorted(buckets.items(), key=lambda x: x[1]["wins"] + x[1]["losses"], reverse=True):
        total = b["wins"] + b["losses"]
        avg_hold = sum(b["hold_days"]) / len(b["hold_days"]) if b["hold_days"] else 0
        breakdown.append({
            "archetype": arch,
            "trades": total,
            "wins": b["wins"],
            "losses": b["losses"],
            "win_rate": round(b["wins"] / total * 100, 1) if total > 0 else 0,
            "pnl": round(b["pnl"], 2),
            "roi": round(b["pnl"] / b["bet_total"] * 100, 1) if b["bet_total"] > 0 else 0,
            "avg_hold_days": round(avg_hold, 1),
        })

    avg_hold_all = round(total_hold_days / total_closed, 1) if total_closed > 0 else 0

    return {
        "breakdown": breakdown,
        "total_closed": total_closed,
        "avg_hold_days": avg_hold_all,
    }


def get_archetype_cumulative_pnl() -> dict:
    """Return per-archetype cumulative P&L series for sparkline charts.

    Each archetype gets a list of {date, cumulative_pnl} points ordered by close date.
    """
    conn = _get_db()
    rows = conn.execute(
        "SELECT archetype, pnl, closed_at FROM paper_positions WHERE status IN ('won','lost') ORDER BY closed_at ASC"
    ).fetchall()
    conn.close()

    series = {}
    for row in rows:
        arch = row["archetype"] or "other"
        if arch not in series:
            series[arch] = {"points": [], "running": 0.0}
        s = series[arch]
        s["running"] += row["pnl"] or 0
        dt = row["closed_at"]
        label = ""
        if dt:
            try:
                d = datetime.fromisoformat(dt.replace("Z", "+00:00"))
                label = f"{d.month}/{d.day}"
            except Exception:
                label = dt[:10]
        s["points"].append({"date": label, "pnl": round(s["running"], 2)})

    return {
        arch: s["points"] for arch, s in series.items()
    }


def close_position_by_id(position_id: int, outcome: str) -> dict:
    """Manually close a position by its DB row id. outcome: 'won' or 'lost'."""
    conn = _get_db()
    pos = conn.execute("SELECT * FROM paper_positions WHERE id=? AND status='open'", (position_id,)).fetchone()
    if not pos:
        conn.close()
        return {"closed": False, "reason": f"No open position with id={position_id}"}

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
        (outcome, datetime.now(timezone.utc).isoformat(),
         1.0 if outcome == "won" else 0.0, round(pnl, 2), f"manual: {outcome}", pos["id"]))
    conn.commit()
    _save_state(conn, bankroll, pnl)
    conn.close()

    return {
        "closed": True,
        "id": position_id,
        "market": (pos["market_title"] or "")[:60],
        "outcome": outcome,
        "pnl": round(pnl, 2),
        "new_bankroll": round(bankroll, 2),
    }


def get_resolve_log(limit: int = 20) -> list:
    """Return the last N auto-resolved positions with timestamps."""
    conn = _get_db()
    rows = conn.execute(
        "SELECT id, market_title, side, status, pnl, closed_at, close_reason "
        "FROM paper_positions "
        "WHERE close_reason LIKE 'auto-resolved%' OR close_reason LIKE 'manual%' "
        "ORDER BY closed_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def resolve_open_positions() -> dict:
    """Auto-resolve expired paper positions using Polymarket CLOB / Kalshi APIs.

    Called by watchdog every 5 minutes.
    """
    import urllib.request

    CLOB_API = "https://clob.polymarket.com"
    KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

    def _fetch(url, timeout=10):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception:
            return None

    def _resolve_polymarket(market_id: str, side: str):
        """Resolve a Polymarket position via CLOB API + Gamma API fallback.

        Returns outcome as 'YES' or 'NO', or None if not yet resolved.
        Handles binary (Yes/No tokens) and named-outcome markets (team names).
        """
        # Primary: CLOB API
        data = _fetch(f"{CLOB_API}/markets/{market_id}")
        if data and data.get("closed"):
            tokens = data.get("tokens", [])
            if tokens:
                winner_token = None
                for t in tokens:
                    if t.get("winner") is True:
                        winner_token = t
                        break

                if winner_token is not None:
                    winner_name = (winner_token.get("outcome") or "").strip()
                    if winner_name.upper() in ("YES", "NO"):
                        return winner_name.upper()
                    if len(tokens) >= 2:
                        first_won = tokens[0].get("winner") is True
                        return "YES" if first_won else "NO"

        # Fallback: Gamma API (sometimes resolves before CLOB updates)
        gamma_data = _fetch(f"https://gamma-api.polymarket.com/markets?condition_id={market_id}")
        if gamma_data and isinstance(gamma_data, list) and gamma_data:
            gm = gamma_data[0]
            if gm.get("closed") or gm.get("resolved"):
                outcome_prices = gm.get("outcomePrices", "")
                try:
                    if isinstance(outcome_prices, str):
                        import json as _json
                        outcome_prices = _json.loads(outcome_prices)
                    if isinstance(outcome_prices, list) and len(outcome_prices) >= 2:
                        yes_p = float(outcome_prices[0])
                        no_p = float(outcome_prices[1])
                        # Resolved markets have prices at 0 or 1
                        if yes_p > 0.99:
                            return "YES"
                        elif no_p > 0.99:
                            return "NO"
                except Exception:
                    pass

        # Check if market expired (end_date passed) — force-resolve via price
        if data and not data.get("closed"):
            tokens = data.get("tokens", [])
            if tokens and len(tokens) >= 2:
                yes_p = float(tokens[0].get("price", 0.5))
                # If market is >24h past end date and price is extreme, resolve
                end_date = data.get("end_date_iso") or ""
                if end_date:
                    try:
                        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                        hours_past = (datetime.now(timezone.utc) - end_dt).total_seconds() / 3600
                        if hours_past > 24:
                            if yes_p > 0.95:
                                logger.info(f"Force-resolve {market_id[:16]}... YES (price={yes_p:.2f}, {hours_past:.0f}h past expiry)")
                                return "YES"
                            elif yes_p < 0.05:
                                logger.info(f"Force-resolve {market_id[:16]}... NO (price={yes_p:.2f}, {hours_past:.0f}h past expiry)")
                                return "NO"
                    except Exception:
                        pass

        return None

    conn = _get_db()
    open_positions = conn.execute("SELECT * FROM paper_positions WHERE status='open'").fetchall()

    if not open_positions:
        conn.close()
        return {"resolved": 0, "note": "No open positions"}

    resolved = 0
    total_pnl = 0
    details = []

    for pos in open_positions:
        market_id = pos["market_id"]
        platform = pos["platform"] or "kalshi"
        side = pos["side"]
        outcome = None

        if platform == "polymarket" or market_id.startswith("0x"):
            outcome = _resolve_polymarket(market_id, side)
        else:
            data = _fetch(f"{KALSHI_API}/markets/{market_id}")
            if data:
                market = data.get("market", data)
                result = market.get("result", "")
                if result:
                    outcome = result.upper()

        if not outcome:
            continue

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
        details.append({"market": (pos["market_title"] or "")[:60], "outcome": outcome, "side": side, "won": won, "pnl": round(pnl, 2)})
        logger.info(f"Resolved: {pos['market_title'][:50]} → {outcome} ({'WON' if won else 'LOST'} ${pnl:+.2f})")

    if resolved > 0:
        conn.commit()
        bankroll = _get_bankroll(conn) + total_pnl
        _save_state(conn, bankroll, total_pnl)
    
    conn.close()
    return {"resolved": resolved, "total_pnl": round(total_pnl, 2), "details": details}

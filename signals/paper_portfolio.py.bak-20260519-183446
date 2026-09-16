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
from loguru import logger
import logging

# ── META-LABELING (López de Prado inspired, Mar 13 2026) ────────────
# Predicts P(profit | signal features) using trained logistic regression.
# See vault: MARKET_AGREEMENT_SYSTEM.md, HOW_EDGE_WORKS.md
_meta_model = None
_meta_model_loaded = False

def _load_meta_model():
    global _meta_model, _meta_model_loaded
    if _meta_model_loaded:
        return _meta_model
    _meta_model_loaded = True
    try:
        import pickle
        model_path = Path(__file__).parent.parent / "storage" / "meta_model.pkl" if "Path" in dir() else None
        if model_path is None:
            from pathlib import Path as _P
            model_path = _P(__file__).parent.parent / "storage" / "meta_model.pkl"
        if model_path.exists():
            with open(str(model_path), "rb") as f:
                _meta_model = pickle.load(f)
            logger.info("Meta-model loaded ({} features)", len(_meta_model.get("feature_names", [])))
    except Exception as e:
        logger.warning("Meta-model load failed: {}", e)
    return _meta_model

def meta_label_score(side, entry_price, confidence, edge_pct, archetype):
    """Return P(profit) from meta-labeling model. None if model not available."""
    import numpy as np
    model = _load_meta_model()
    if model is None:
        return None
    
    ARCHETYPES = ["weather", "entertainment", "geopolitical", "election", "price_above",
                  "sports_winner", "sports_single_game", "social_count", "deadline_binary",
                  "ai_model", "other"]
    
    arch = (archetype or "other").lower()
    arch_idx = next((i for i, a in enumerate(ARCHETYPES) if a in arch), len(ARCHETYPES) - 1)
    
    eff_price = entry_price if side == "YES" else (1 - entry_price)
    disagreement = abs(confidence - entry_price) if side == "YES" else abs(confidence - (1 - entry_price))
    potential_return = (1 / eff_price - 1) if eff_price > 0 else 0
    side_num = 1.0 if side == "YES" else 0.0
    
    features = np.array([eff_price, edge_pct or 0, confidence, disagreement,
                         potential_return, side_num, arch_idx / len(ARCHETYPES)])
    
    mean = np.array(model["mean"])
    std = np.array(model["std"])
    weights = np.array(model["weights"])
    bias = model["bias"]
    
    x_norm = (features - mean) / std
    z = float(x_norm @ weights + bias)
    prob = 1 / (1 + np.exp(-max(-500, min(500, z))))
    return round(prob, 3)

try:
    from api.activity_feed import emit_event
    HAS_ACTIVITY_FEED = True
except ImportError:
    HAS_ACTIVITY_FEED = False
import math
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional


BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "storage" / "shadow_trades.db"
JSON_DIR = Path.home() / ".openclaw" / "paper-trading"

# ─── Correlation Cap ────────────────────────────────────────
# Archetypes that move together are grouped. Max N open positions per group.
CORRELATION_GROUPS = {
    "price_above": "crypto", "price_range": "crypto", "crypto_price": "crypto",
    "daily_updown": "crypto", "intraday_updown": "crypto",
    "directional": "crypto",
    "sports_single_game": "sports", "sports_winner": "sports",
    "game_total": "sports",
    "election": "geopolitical", "geopolitical": "geopolitical",
    "deadline_binary": "politics",
    "financial_price": "finance",
    "entertainment": "entertainment", "ai_model": "culture",
    "social_count": "social_count", "weather": "weather",
    "parlay": "other", "other": "other",
}

# ── Entity-level concentration (Mar 16 audit) ──────────────────────
# Iran cluster had 15 correlated bets that all lost together.
# This maps title keywords → entity groups with separate caps.
ENTITY_GROUPS = {
    "iran": 2,    # Max 2 Iran-related positions
    "israel": 2,  # Max 2 Israel-related positions  
    "trump": 3,   # Max 3 Trump-related positions
    "bitcoin": 3, # Max 3 BTC positions
    "ethereum": 3,# Max 3 ETH positions
}

def _check_entity_concentration(title: str, conn) -> Optional[str]:
    """Block if too many positions on the same named entity."""
    title_lower = (title or "").lower()
    for entity, cap in ENTITY_GROUPS.items():
        if entity in title_lower:
            rows = conn.execute(
                "SELECT market_title FROM paper_positions WHERE status='open'"
            ).fetchall()
            count = sum(1 for r in rows if entity in (r["market_title"] or "").lower())
            if count >= cap:
                return f"Entity concentration: \'{entity}\' has {count}/{cap} open positions"
    return None
MAX_PER_GROUP = 10  # Entity guards handle concentration now, group cap stays loose


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



MAX_PER_CITY_DAY = 2   # Weather: max positions per city per day
MAX_PER_CITY = 3       # Weather: max TOTAL positions per city (across all dates)
MAX_PER_EVENT = 2      # Non-weather: max positions per underlying event


def _extract_weather_city_date(title: str):
    """Extract (city, date_str) from weather market title, or (None, None)."""
    if not title:
        return None, None
    t = title.lower()
    city_m = re.search(r'temperature in ([a-z\s]+?)\s+be\b', t)
    date_m = re.search(r'on ((?:january|february|march|april|may|june|july|august|september|october|november|december) \d+)', t)
    city = city_m.group(1).strip() if city_m else None
    date_str = date_m.group(1).strip() if date_m else None
    return city, date_str


def _check_event_concentration(title: str, archetype: str, conn):
    """Block if too many positions on the same underlying event.
    
    Weather: max MAX_PER_CITY_DAY per city+day combo.
    Non-weather: max MAX_PER_EVENT positions sharing >=60% title words.
    """
    if archetype == 'weather':
        city, date_str = _extract_weather_city_date(title)
        if city:
            rows = conn.execute(
                "SELECT market_title FROM paper_positions WHERE status='open' AND archetype='weather'"
            ).fetchall()
            city_day_count = 0
            city_total_count = 0
            for row in rows:
                c, d = _extract_weather_city_date(row["market_title"])
                if c == city:
                    city_total_count += 1
                    if d == date_str:
                        city_day_count += 1
            # Check per-city total first (prevents 4x Dallas across different dates)
            if city_total_count >= MAX_PER_CITY:
                return f"City concentration: {city} has {city_total_count}/{MAX_PER_CITY} open positions"
            # Then check per city+day
            if date_str and city_day_count >= MAX_PER_CITY_DAY:
                return f"Event concentration: {city}/{date_str} {city_day_count}/{MAX_PER_CITY_DAY}"
        return None
    
    # Non-weather: title similarity check
    if not title:
        return None
    def _title_words(t):
        t = re.sub(r'[\d\.\$%,]', '', t.lower())
        t = re.sub(r'\b(by|on|in|the|will|be|of|to|a|an|or|and|from|for)\b', '', t)
        return set(w for w in t.split() if len(w) > 2)
    
    new_words = _title_words(title)
    if not new_words:
        return None
    
    rows = conn.execute(
        "SELECT market_title FROM paper_positions WHERE status='open'"
    ).fetchall()
    similar_count = 0
    for row in rows:
        existing_words = _title_words(row["market_title"] or "")
        if not existing_words:
            continue
        overlap = len(new_words & existing_words) / max(len(new_words | existing_words), 1)
        if overlap >= 0.60:
            similar_count += 1
    if similar_count >= MAX_PER_EVENT:
        return f"Event concentration: {similar_count}/{MAX_PER_EVENT} similar titles"
    return None


def _get_dynamic_kelly(conn, confidence: float = 0.60) -> Dict[str, Any]:
    """Calculate dynamic Kelly fraction based on rolling performance + signal confidence.

    Layer 1 (rolling WR): System-level safety floor — are we broken? pause/reduce.
    Layer 2 (confidence):  Per-signal conviction — THIS signal is strong, lean in.

    Returns:
        {"fraction": float, "rolling_wr": float, "rolling_trades": int,
         "drawdown_pct": float, "status": str, "reason": str,
         "conviction_mult": float, "confidence_tier": str}
    """
    # Rolling win rate from last N closed trades
    rows = conn.execute(
        "SELECT status FROM paper_positions WHERE status IN ('won','lost','stopped') ORDER BY closed_at DESC LIMIT ?",
        (KELLY_ROLLING_WINDOW,)
    ).fetchall()

    rolling_trades = len(rows)
    wins = sum(1 for r in rows if r["status"] == "won")
    rolling_wr = wins / rolling_trades if rolling_trades > 0 else 0.5

    # Current drawdown
    bankroll = _get_bankroll(conn)
    peak = _get_peak(conn)
    drawdown_pct = (peak - bankroll) / peak if peak > 0 else 0

    # ── Layer 1: Rolling WR sets the safety floor ─────────────────
    if drawdown_pct >= DRAWDOWN_PAUSE_PCT:
        fraction = 0
        status = "paused"
        reason = f"Drawdown {drawdown_pct:.1%} >= {DRAWDOWN_PAUSE_PCT:.0%} — trading paused"
        logger.warning("🛑 KELLY PAUSED: drawdown={}%% (threshold {}%%)", drawdown_pct * 100, DRAWDOWN_PAUSE_PCT * 100)
    elif rolling_trades < BOOTSTRAP_TRADES:
        # Bootstrap mode: seed WR assumption until enough data
        fraction = KELLY_FRACTION_BOOTSTRAP
        rolling_wr = BOOTSTRAP_WR  # Override with Becker-validated WR
        status = "bootstrap"
        reason = f"Bootstrap mode: {rolling_trades}/{BOOTSTRAP_TRADES} trades — seeded {BOOTSTRAP_WR:.0%} WR, 1/8 Kelly"
        logger.info("🚀 KELLY BOOTSTRAP: trades={}/{} fraction=1/8 seeded_wr={}%%", rolling_trades, BOOTSTRAP_TRADES, BOOTSTRAP_WR * 100)
    elif rolling_wr < KELLY_MIN_WR:
        fraction = KELLY_FRACTION_COLD
        status = "cold"
        reason = f"WR {rolling_wr:.0%} < {KELLY_MIN_WR:.0%} over {rolling_trades} trades — half size"
        logger.debug("❄️ KELLY COLD: wr={}%% trades={} fraction=1/{}", rolling_wr * 100, rolling_trades, int(1/fraction))
    else:
        fraction = KELLY_FRACTION
        status = "normal"
        reason = f"WR {rolling_wr:.0%} over {rolling_trades} trades — full size"
        logger.debug("Kelly normal: wr={}%% trades={}", rolling_wr * 100, rolling_trades)

    # ── Layer 2: Confidence-scaled conviction ─────────────────────
    # Scale Kelly fraction based on per-signal confidence.
    # High-conviction signals get more capital, weak signals get less.
    # Never exceeds quarter-Kelly (well below overbetting threshold).
    if confidence >= 0.75:
        conviction_mult = 1.50
        confidence_tier = "high"
    elif confidence >= 0.60:
        conviction_mult = 1.00
        confidence_tier = "normal"
    else:
        conviction_mult = 0.75
        confidence_tier = "weak"

    base_fraction = fraction
    fraction *= conviction_mult

    if fraction > 0 and conviction_mult != 1.0:
        logger.info("Conviction scaling: conf={:.0%} tier={} base=1/{:.0f} -> 1/{:.0f}",
                     confidence, confidence_tier, 1/base_fraction if base_fraction > 0 else 0,
                     1/fraction if fraction > 0 else 0)

    return {
        "fraction": fraction,
        "rolling_wr": round(rolling_wr, 3),
        "rolling_trades": rolling_trades,
        "drawdown_pct": round(drawdown_pct, 4),
        "status": status,
        "reason": reason,
        "conviction_mult": conviction_mult,
        "confidence_tier": confidence_tier,
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
DRAWDOWN_PAUSE_PCT = 0.30     # 30% drawdown → pause trading (raised from 15% — paper mode needs data)
BOOTSTRAP_TRADES = 20         # Minimum trades before trusting rolling stats
BOOTSTRAP_WR = 0.57           # Seeded WR during bootstrap (Becker-validated)
KELLY_FRACTION_BOOTSTRAP = 1 / 8  # Between cold (1/12) and normal (1/6)
MAX_CONCURRENT = 999  # uncapped for paper testing
MIN_CONFIDENCE = 0.50
MIN_EDGE = 0.12  # 12% minimum edge for non-weather
# Per-archetype edge floors — weather needs 20% (backtest: <15% edge is noise)
ARCHETYPE_MIN_EDGE = {
    "weather": 0.20,
}
MIN_PRICE = 0.05  # Price floor — reject garbage contracts below 5 cents
MAX_PRICE = 0.95  # Price ceiling — reject near-certain markets (no edge)
MIN_BET = 100.0  # Bootstrap: meaningful minimum bet size
MAX_BET = 1000.0  # Scaled for $10K bankroll
# Per-strategy max bet overrides (0W/4L strategies get capped)
STRATEGY_MAX_BET = {
    'MispricedCategoryWhale': 200.0,  # Was losing $100-543/trade. Cap until WR improves.
}
# Per-archetype min bet — weather gets 00 floor based on 4,877-bracket synthetic backtest
# (36K P&L at 00/30% stop vs 8K at 00/30% stop, zero losing weeks)
# Daily loss circuit breaker — stop all new bets if daily realized+unrealized losses exceed cap
DAILY_LOSS_CAP = -2000.0  # Stop betting if daily P&L < -$2K

ARCHETYPE_MIN_BET = {
    'weather': 500.0,
}
MAX_RESOLUTION_DAYS = 14  # Reject markets resolving >14 days out — capital drag

# Archetype filters — data-driven from resolved trades
ARCHETYPE_BLOCKLIST = {"sports_winner", "deadline_binary", "election", "social_count"}  # 0% WR. price_above removed — now has crypto_price_signal with VPS data
ARCHETYPE_BOOST = {"sports_single_game": 1.3, "social_count": 1.3}  # Proven +180% blended ROI
MIN_NO_IMPLIED_PROB = 0.35  # Minimum implied NO probability (1 - entry_price for NO bets)
MIN_EXPIRY_HOURS = 72  # Minimum 3 days to resolution for crypto/price markets

# Weather entry timing window — only bet 3-24h before resolution
# RMSE drops ~60% from 72h to 12h. Late entry = sharper signals, fewer false stops.
WEATHER_MAX_HOURS_BEFORE = 24   # Don't bet more than 24h before event
WEATHER_MIN_HOURS_BEFORE = 3    # Don't bet less than 3h before (need time for stop to work)


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
        CREATE TABLE IF NOT EXISTS equity_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            realized_bankroll REAL NOT NULL,
            unrealized_pnl REAL DEFAULT 0,
            total_equity REAL NOT NULL,
            open_positions INTEGER DEFAULT 0,
            peak_equity REAL NOT NULL,
            source TEXT DEFAULT 'snapshot'
        );
        CREATE INDEX IF NOT EXISTS idx_equity_snapshots_ts ON equity_snapshots(ts);
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
    # Migration: add columns incrementally
    for col, typedef in [
        ("market_slug", "TEXT DEFAULT ''"),
        ("closing_line", "REAL"),
        ("kelly_fraction", "REAL"),
        ("conviction_mult", "REAL"),
        ("resolution_price", "REAL"),
        ("entry_forecast_json", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE paper_positions ADD COLUMN {col} {typedef}")
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
    closed = conn.execute("SELECT pnl FROM paper_positions WHERE status IN ('won','lost','stopped')").fetchall()
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
    # ── DAILY LOSS CAP ─────────────────────────────────────────────
    # Circuit breaker: stop all new bets if daily P&L is too negative
    try:
        conn_check = _get_db()
        from datetime import datetime, timezone, timedelta
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0).isoformat()
        daily_closed = conn_check.execute(
            "SELECT COALESCE(SUM(pnl), 0) as daily_pnl FROM paper_positions WHERE closed_at >= ? AND status IN ('won','lost','stopped')",
            (today_start,)
        ).fetchone()
        daily_pnl = daily_closed["daily_pnl"] if daily_closed else 0
        conn_check.close()
        if daily_pnl <= DAILY_LOSS_CAP:
            return {"eligible": False, "reason": f"Daily loss cap hit: ${daily_pnl:+,.0f} <= ${DAILY_LOSS_CAP:,.0f}", "edge": 0, "kelly_pct": 0, "bet_size": 0}
    except Exception as e:
        pass  # Don't block betting if check fails

    # ── HEALTH GATE (per archetype, state-based) ───────────────────
    # State-based circuit breaker. Replaces the static daily-entry cap
    # (volume isn't a clean predictor of bad days — Apr 18: 30 trades,
    # +$2,970 vs Apr 28: 12 trades, -$2,171). Refuses entries only when
    # the archetype's measured performance / model state is degraded.
    # See services/health_gates.py and the vault Framework note.
    sig_arch_for_health = (signal.get("archetype") or "").lower()
    if sig_arch_for_health == "weather":
        try:
            from services.health_gates import weather_health_check
            h = weather_health_check()
            if h["state"] == "RED":
                return {"eligible": False,
                        "reason": f"Weather health RED — {'; '.join(h['reasons'])} | resume: {h['resume_when']}",
                        "edge": 0, "kelly_pct": 0, "bet_size": 0}
        except Exception:
            pass  # Don't block betting if health check fails
    
    confidence = signal.get("confidence", 0)
    if isinstance(confidence, str):
        confidence = float(confidence.replace("%", "")) / 100
    if confidence > 1:
        confidence = confidence / 100
    
    # ── CONFIDENCE CAP (Mar 16 audit) ──────────────────────────────
    # System was outputting 92%+ confidence on wrong-side bets.
    # No prediction market signal should claim >85% — that's overfit.
    MAX_CONFIDENCE = 0.85
    if confidence > MAX_CONFIDENCE:
        logger.debug("Confidence capped: {:.0%} → {:.0%}", confidence, MAX_CONFIDENCE)
        confidence = MAX_CONFIDENCE
    
    market_price = signal.get("entry_price") or signal.get("price") or signal.get("market_price", 0.5)
    if isinstance(market_price, str):
        market_price = float(market_price)
    if market_price > 1:
        market_price = market_price / 100
    
    side = (signal.get("side") or signal.get("direction") or "YES").upper()
    market_title = signal.get("market") or signal.get("market_title") or signal.get("title", "")
    
    # Price floor/ceiling filter — reject garbage and near-certain contracts
    # "Namibia wins World Cup" at 0.1¢ = garbage, don't buy it
    effective_price = market_price if side == "YES" else (1 - market_price)
    # Weather YES bets at low prices are legitimate longshots (e.g. 3¢ temperature bucket)
    early_archetype = signal.get("archetype") or ""
    # Derive archetype once for all uses in this function
    try:
        from mispriced_category_signal import classify_archetype
    except ImportError:
        classify_archetype = lambda s: s.get("archetype", "unknown")
    archetype = early_archetype or signal.get("archetype") or classify_archetype(signal.get("market_title") or signal.get("title") or "")
    price_floor = 0.01 if early_archetype == "weather" else MIN_PRICE
    if effective_price < price_floor:
        return {"eligible": False, "reason": f"Price {effective_price:.1%} below floor {price_floor:.0%} — garbage contract", "edge": 0, "kelly_pct": 0, "bet_size": 0}
    if effective_price > MAX_PRICE:
        return {"eligible": False, "reason": f"Price {effective_price:.1%} above ceiling {MAX_PRICE:.0%} — no edge", "edge": 0, "kelly_pct": 0, "bet_size": 0}

    # ── tweet_count_mc near-modal cap (2026-04-28) ─────────────────────
    # Diagnostic on n=11 trades found a clean cleavage: NO bets with
    # YES-market-price ≥ 0.40 went 1W/3L for −$103, while YES-market-price
    # < 0.40 went 5W/2L for +$33. The MC fades the consensus reliably on
    # extreme buckets (cheap-to-fade) but breaks down near the modal
    # bucket where the market is correctly priced. Cap until forecast
    # persistence + recalibration ship.
    # Threshold expressed as effective_price (what we pay per contract):
    # we want to be paying ≥ 60¢ (i.e. fading a market that already
    # thinks the outcome is unlikely). Read `strategy` directly from the
    # signal because the locally-scoped `strategy` variable is assigned
    # later in this function.
    if signal.get("strategy") == "tweet_count_mc" and effective_price < 0.60:
        return {"eligible": False,
                "reason": (f"tweet_count_mc near-modal cap: effective_price "
                           f"{effective_price:.0%} < 60% (market_price "
                           f"{market_price:.0%}, side {side}) — historical "
                           f"calibration error in this regime, blocked"),
                "edge": 0, "kelly_pct": 0, "bet_size": 0}

    # ─── Source Staleness Check ─────────────────────────────
    # Skip for weather/tweet signals — they fetch their own fresh data,
    # not dependent on Gamma scanner freshness.
    strategy = signal.get("strategy", "")
    self_sourced = strategy in ("tweet_count_mc", "weather_ensemble", "weather")
    if HAS_SOURCE_HEALTH and not self_sourced:
        import time as _time
        platform = (signal.get("platform") or "kalshi").lower()
        source_map = {"kalshi": "kalshi", "polymarket": "polymarket_gamma", "manifold": "manifold"}
        primary_source = source_map.get(platform, platform)
        ts = _get_source_ts(primary_source)
        if ts:
            age = _time.time() - ts
            if age > 86400:  # 24h — only block if source truly dead, not just stale cache
                logger.debug("Staleness reject: {} data is {}s old", primary_source, age)
                return {"eligible": False, "reason": f"Stale data: {primary_source} is {age:.0f}s old (>24h)", "edge": 0, "kelly_pct": 0, "bet_size": 0}
            elif age > 3600:
                logger.info("Staleness warning: {} data is {}s old — proceeding anyway", primary_source, age)
    
    # ─── Phase 1: Empirical Confidence Override ─────────────
    empirical_result = None
    if HAS_EMPIRICAL:
        try:
            market_title = signal.get("market") or signal.get("market_title") or signal.get("title", "")
            empirical_result = calculate_empirical_confidence(market_title, side or "YES", market_price, override_archetype=early_archetype or None)
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
    
    # ── MARKET AGREEMENT GATE (MCP-inspired, Mar 13 2026) ──────────────
    # Block trades where model and market strongly disagree.
    # Rationale: large disagreement = model error, not alpha.
    # See vault: MARKET_AGREEMENT_SYSTEM.md
    #
    # Weather bracket markets (2°F windows) structurally produce high
    # disagreement because ensemble confidence on a narrow bracket is
    # 50-75% while market prices span 6-94%. A flat 20% gate blocks
    # the entire archetype. Weather gets 50% threshold, NO-side only,
    # with source agreement requirement from the ensemble.
    DEFAULT_MAX_DISAGREEMENT = 0.20
    WEATHER_MAX_DISAGREEMENT = 0.50
    is_weather = early_archetype == "weather" or strategy in ("weather", "weather_ensemble")
    max_disagreement = WEATHER_MAX_DISAGREEMENT if is_weather else DEFAULT_MAX_DISAGREEMENT
    if side == "YES":
        disagreement = abs(confidence - market_price)
    else:
        disagreement = abs(confidence - (1 - market_price))
    if disagreement > max_disagreement:
        logger.info("MARKET GATE: Blocking {} — disagreement {:.0%} (conf={:.0%}, mkt={:.0%}, side={}, threshold={:.0%})",
                    market_title[:50] if market_title else "?", disagreement, confidence,
                    market_price, side, max_disagreement)
        return {"eligible": False, "reason": f"Market disagreement {disagreement:.0%} > {max_disagreement:.0%}", "edge": edge, "kelly_pct": 0, "bet_size": 0}
    # Weather extra guard: only allow NO side (YES has 0% WR historically)
    if is_weather and side == "YES":
        logger.info("MARKET GATE: Blocking weather YES — historical 0%% WR, NO-only archetype")
        return {"eligible": False, "reason": "Weather YES blocked (0% historical WR)", "edge": edge, "kelly_pct": 0, "bet_size": 0}
    # Weather extra guard: require source agreement from ensemble (>40%)
    if is_weather:
        source_agreement = signal.get("source_agreement", signal.get("agreement", 1.0))
        if source_agreement < 0.40:
            logger.info("MARKET GATE: Blocking weather — source agreement %.0f%% < 40%%", source_agreement * 100)
            return {"eligible": False, "reason": f"Weather source agreement {source_agreement:.0%} < 40%", "edge": edge, "kelly_pct": 0, "bet_size": 0}

    # ── META-LABELING GATE ──────────────────────────────────────────────
    meta_score = meta_label_score(side, market_price, confidence, edge, early_archetype or "other")
    # Exempt new strategies from meta gate until they have 20+ trades of their own data
    META_EXEMPT_STRATEGIES = {"crypto_price", "cross_platform_edge"}
    if meta_score is not None and meta_score < 0.40 and strategy not in META_EXEMPT_STRATEGIES:
        logger.info("META GATE: Blocking {} — P(profit)={:.0%} (arch={}, side={}, disagree={:.0%})",
                    market_title[:40] if market_title else "?", meta_score, early_archetype or "other", side, disagreement)
        return {"eligible": False, "reason": f"Meta-label P(profit)={meta_score:.0%} < 40%", 
                "edge": edge, "kelly_pct": 0, "bet_size": 0, "meta_score": meta_score}

    # Cross-platform edge strategy: relaxed thresholds (the cross-platform spread IS the signal)
    if strategy == "cross_platform_edge":
        xplat_min_conf = 0.10   # Allow low-probability events if spread is real
        xplat_min_edge = 0.05   # 5% cross-platform spread is meaningful
        if confidence < xplat_min_conf:
            return {"eligible": False, "reason": f"X-plat confidence {confidence:.0%} < {xplat_min_conf:.0%}", "edge": edge, "kelly_pct": 0, "bet_size": 0}
        if edge < xplat_min_edge:
            return {"eligible": False, "reason": f"X-plat edge {edge:.1%} < {xplat_min_edge:.0%}", "edge": edge, "kelly_pct": 0, "bet_size": 0}
    else:
        if confidence < MIN_CONFIDENCE:
            return {"eligible": False, "reason": f"Confidence {confidence:.0%} < {MIN_CONFIDENCE:.0%}", "edge": edge, "kelly_pct": 0, "bet_size": 0}
        effective_min_edge = ARCHETYPE_MIN_EDGE.get(archetype, MIN_EDGE)
        if edge < effective_min_edge:
            return {"eligible": False, "reason": f"Edge {edge:.1%} < {effective_min_edge:.0%} (archetype={archetype})", "edge": edge, "kelly_pct": 0, "bet_size": 0}

    # Archetype blocklist — proven unprofitable archetypes
    if archetype in ARCHETYPE_BLOCKLIST:
        logger.info("🚫 BLOCKED archetype={} market={} (0%% WR, -100%% ROI)", archetype, signal.get("market", "")[:40])
        return {"eligible": False, "reason": f"Blocked archetype: {archetype} (0% historical WR)", "edge": edge, "kelly_pct": 0, "bet_size": 0}

    # ─── Resolution Horizon Gate ──────────────────────────────
    # Reject markets that resolve too far in the future (capital drag)
    from datetime import datetime, timezone, timedelta
    end_date_str = signal.get("end_date") or signal.get("resolves_at") or signal.get("resolution_date") or ""
    days_out = None
    if end_date_str:
        try:
            if isinstance(end_date_str, str):
                end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            else:
                end_dt = end_date_str
            days_out = (end_dt - datetime.now(timezone.utc)).days
        except Exception as e:
            logger.debug("Could not parse end_date %r: {}", end_date_str, e)
    
    # Fallback: use days_to_close if no end_date was parsed
    if days_out is None:
        dtc = signal.get("days_to_close")
        if dtc is not None:
            try:
                days_out = int(float(dtc))
            except (ValueError, TypeError):
                pass
    
    if days_out is not None and days_out > MAX_RESOLUTION_DAYS:
        logger.info("BLOCKED horizon={}d (>{}d) market={}", days_out, MAX_RESOLUTION_DAYS, signal.get("market", "")[:40])
        return {"eligible": False, "reason": f"Resolution too far: {days_out}d (max {MAX_RESOLUTION_DAYS}d)", "edge": edge, "kelly_pct": 0, "bet_size": 0}

    # ─── Weather Entry Timing Window ─────────────────────────────
    # Only bet on weather 3-24h before resolution. Late entry = sharper forecast.
    if archetype == "weather" and end_date_str:
        try:
            if isinstance(end_date_str, str):
                _end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            else:
                _end_dt = end_date_str
            hours_to_close = (_end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
            if hours_to_close > WEATHER_MAX_HOURS_BEFORE:
                logger.debug("Weather timing: {}h to close > {}h max, skipping {}",
                             f"{hours_to_close:.1f}", WEATHER_MAX_HOURS_BEFORE, market_title[:40])
                return {"eligible": False, "reason": f"Weather too early: {hours_to_close:.0f}h to close (max {WEATHER_MAX_HOURS_BEFORE}h)", "edge": edge, "kelly_pct": 0, "bet_size": 0}
            if hours_to_close < WEATHER_MIN_HOURS_BEFORE:
                logger.debug("Weather timing: {}h to close < {}h min, skipping {}",
                             f"{hours_to_close:.1f}", WEATHER_MIN_HOURS_BEFORE, market_title[:40])
                return {"eligible": False, "reason": f"Weather too late: {hours_to_close:.0f}h to close (min {WEATHER_MIN_HOURS_BEFORE}h)", "edge": edge, "kelly_pct": 0, "bet_size": 0}
            logger.info("Weather timing OK: {:.1f}h to close for {}", hours_to_close, market_title[:40])
        except Exception as e:
            logger.debug("Weather timing check failed: {}", e)

    # Also check market title for year-scale bets (fallback heuristic)
    import re
    title = (signal.get("market") or signal.get("market_title") or signal.get("title") or "").lower()
    far_patterns = [r"before 20[2-3]\d", r"by 20[2-3]\d", r"in 20[2-3]\d", r"end of 20[2-3]\d"]
    for pat in far_patterns:
        m = re.search(pat, title)
        if m:
            try:
                year = int(re.search(r"20[2-3]\d", m.group()).group())
                if year > datetime.now().year:
                    logger.info("🚫 BLOCKED long-dated title pattern '{}' market={}", m.group(), title[:40])
                    return {"eligible": False, "reason": f"Long-dated market ({m.group()})", "edge": edge, "kelly_pct": 0, "bet_size": 0}
            except Exception:
                pass

    # Minimum NO implied probability — reject if market is too efficient
    if side == "NO" and effective_price < MIN_NO_IMPLIED_PROB:
        logger.info("🚫 BLOCKED low NO prob={}%% market={}", effective_price*100, signal.get("market", "")[:40])
        return {"eligible": False, "reason": f"NO implied prob {effective_price:.0%} < {MIN_NO_IMPLIED_PROB:.0%} — market too efficient", "edge": edge, "kelly_pct": 0, "bet_size": 0}

    # ── PRICE SANITY CHECK (Mar 16 audit) ──────────────────────────────
    # If market is >80% likely to happen (YES price > 0.80), don't bet NO.
    # We were betting NO on "BTC above $64K" at 0.57 entry (43% NO implied) with 92% confidence — and losing.
    # High YES prices mean the market has strong conviction; fading it is usually wrong.
    if side == "NO" and market_price > 0.80:
        logger.info("🚫 SANITY: Blocking NO on high-prob market (YES={:.0%}) market={}", market_price, title[:40])
        return {"eligible": False, "reason": f"Sanity: NO on {market_price:.0%} YES market — too risky to fade", "edge": edge, "kelly_pct": 0, "bet_size": 0}
    
    kelly_pct = edge / odds if odds > 0 else 0
    
    conn = _get_db()
    bankroll = _get_bankroll(conn)
    open_count = _count_open(conn)
    
    if open_count >= MAX_CONCURRENT:
        conn.close()
        return {"eligible": False, "reason": f"Max {MAX_CONCURRENT} concurrent positions", "edge": edge, "kelly_pct": kelly_pct, "bet_size": 0}
    
    # Dynamic Kelly — adjusts fraction based on rolling performance + signal confidence
    kelly_data = _get_dynamic_kelly(conn, confidence=confidence)
    conn.close()
    
    if kelly_data["status"] == "paused":
        return {"eligible": False, "reason": kelly_data["reason"], "edge": edge, "kelly_pct": kelly_pct, "bet_size": 0, "kelly": kelly_data, "meta_score": meta_score if "meta_score" in dir() else meta_label_score(side, market_price, confidence, edge, archetype)}
    
    effective_kelly = kelly_data["fraction"]

    # CV Kelly haircut — uncertainty-adjusted sizing
    # Only applies AFTER bootstrap phase (need real data, not seeded WR)
    if kelly_data["status"] not in ("bootstrap", "paused"):
        try:
            from signals.cv_kelly import calculate_cv_kelly_haircut
            cv_result = calculate_cv_kelly_haircut(effective_kelly)
            if cv_result["n_resolved"] >= 15:
                effective_kelly = cv_result["kelly_adjusted"]
                logger.info("📐 CV Kelly: haircut={}%% kelly={}→{} (n={}, cv={})",
                            cv_result["cv_haircut"] * 100, kelly_data["fraction"],
                            effective_kelly, cv_result["n_resolved"], cv_result["cv_edge"])
        except Exception as e:
            logger.debug("CV Kelly skipped: {}", e)
    else:
        logger.debug("CV Kelly deferred: still in {} mode", kelly_data["status"])

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
        logger.debug("Time decay applied: mult={} no_wr={}%% dur={} vol={}",
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
                    logger.info("Volume MEGA spike boost: market={} ratio={}x bet_size={}", market_id[:30], volume_spike_data["ratio"], bet_size)
                else:
                    bet_size *= 1.10  # 3x+ volume = FOMO, 10% boost
                    logger.info("Volume spike boost: market={} ratio={}x bet_size={}", market_id[:30], volume_spike_data["ratio"], bet_size)

    # Score velocity — crypto markets get multiplier from Virtuoso confluence score trend
    score_velocity_data = None
    if archetype in ("crypto", "price_above", "price_range", "daily_updown", "intraday_updown", "directional"):
        try:
            from signals.alpha_score_tracker import score_velocity_modifier
            # Try to match signal to a symbol (e.g. "BTC" in title → BTCUSDT)
            title_upper = (signal.get("market_title") or signal.get("title") or "").upper()
            symbol = None
            for sym in ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "DOT", "LINK", "MATIC"]:
                if sym in title_upper:
                    symbol = sym + "USDT"
                    break
            if symbol:
                sv = score_velocity_modifier(symbol)
                score_velocity_data = sv
                if sv["multiplier"] != 1.0:
                    bet_size *= sv["multiplier"]
                    logger.info("📈 Score velocity: {} delta={} mult={} bet_size={}",
                                symbol, sv.get("delta") or 0, sv["multiplier"], bet_size)
        except Exception as e:
            logger.debug("Score velocity skipped: {}", e)

    # Archetype boost — proven profitable archetypes get larger size
    if archetype in ARCHETYPE_BOOST:
        boost = ARCHETYPE_BOOST[archetype]
        bet_size *= boost
        logger.info("🎯 Archetype boost: {} x{} bet_size={}", archetype, boost, bet_size)

    # All archetypes use standard sizing (paper mode — liquidity irrelevant)
    strategy_name = signal.get("strategy", "")
    strategy_max = STRATEGY_MAX_BET.get(strategy_name, MAX_BET)
    # Apply archetype minimum bet (weather gets $100 floor from backtest)
    archetype_min = ARCHETYPE_MIN_BET.get(archetype, MIN_BET)
    bet_size = max(archetype_min, min(strategy_max, bet_size))

    # ── z-cushion gating (weather only) ─────────────────────────────
    # Reject or cap positions where the forecast mean is too close to the bracket edge.
    # z_cushion = distance_to_edge / forecast_std — low z = coin flip territory.
    if archetype == "weather":
        weather_detail = signal.get("weather_detail") or {}
        w_mean = weather_detail.get("forecast_mean") or weather_detail.get("mean")
        w_std = weather_detail.get("forecast_std") or weather_detail.get("std")
        w_comparison = weather_detail.get("comparison")
        w_threshold = weather_detail.get("threshold")
        w_threshold_high = weather_detail.get("threshold_high")
        if w_mean is not None and w_std and w_std > 0 and w_threshold is not None:
            # Distance to nearest bracket edge
            if w_comparison in ("between", "exact") and w_threshold_high is not None:
                dist = min(abs(w_mean - w_threshold), abs(w_mean - w_threshold_high))
            else:
                dist = abs(w_mean - w_threshold)
            z_cushion = dist / w_std
            if z_cushion < 0.5:
                return {"eligible": False, "reason": f"z-cushion {z_cushion:.2f} < 0.50 — coin flip zone (mean={w_mean:.1f}, edge={w_threshold}, std={w_std:.1f})", "edge": edge, "kelly_pct": kelly_pct, "bet_size": 0}
            elif z_cushion < 1.0:
                bet_size = min(bet_size, 100.0)
                logger.info("z-cushion cap: z=%.2f < 1.0, capping bet to $100", z_cushion)
            elif z_cushion < 1.5:
                bet_size = bet_size * 0.5
                logger.info("z-cushion half-Kelly: z=%.2f < 1.5, halving bet to $%.0f", z_cushion, bet_size)
            # else: z >= 1.5, normal Kelly

    # Liquidity cap — when the engine pre-walks the Polymarket book, it sets
    # liquidity_cap_usd to the actual fillable depth at the configured slip cap.
    # Shrink the bet to that cap (but never below the archetype floor).
    liq_cap = signal.get("liquidity_cap_usd")
    if liq_cap is not None:
        try:
            liq_cap = float(liq_cap)
            if liq_cap > 0 and bet_size > liq_cap:
                capped = max(archetype_min, min(bet_size, liq_cap))
                logger.info(
                    "💧 Liquidity cap: market={} bet ${:.0f} → ${:.0f} (book depth ${:.0f})",
                    (signal.get("market_id") or "?")[:30], bet_size, capped, liq_cap,
                )
                bet_size = capped
        except (TypeError, ValueError):
            pass

    if bet_size > bankroll:
        return {"eligible": False, "reason": f"Insufficient bankroll ${bankroll:.2f}", "edge": edge, "kelly_pct": kelly_pct, "bet_size": 0}
    
    return {"eligible": True, "bet_size": round(bet_size, 2), "edge": round(edge, 4), "kelly_pct": round(kelly_pct, 4), "reason": "Criteria met", "empirical": empirical_result, "volume_spike": volume_spike_data, "time_decay": time_decay_data, "score_velocity": score_velocity_data, "kelly": kelly_data}


def open_position(signal: dict) -> dict:
    """Open a paper position if criteria met."""
    eval_result = evaluate_signal(signal)
    if not eval_result["eligible"]:
        return {"opened": False, **eval_result}

    market_id = signal.get("market_id") or signal.get("ticker") or signal.get("id", "unknown")
    side = (signal.get("side") or signal.get("direction") or "YES").upper()
    market_title = signal.get("market") or signal.get("market_title") or signal.get("title", "")
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
    archetype = signal.get("archetype") or "other"
    if archetype == "other":
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
                    logger.info("Cross-strategy AGREE boost 1.2x: {} {}", market_id[:30], side)
                else:
                    bet_size *= 0.5  # Disagreement halve
                    logger.info("Cross-strategy DISAGREE halve: {} {} vs {}", market_id[:30], side, _cross_side)
                bet_size = max(MIN_BET, min(MAX_BET, bet_size))
        except Exception:
            pass

    conn = _get_db()

    # Check not already tracking this market
    existing = conn.execute("SELECT id FROM paper_positions WHERE market_id=? AND status='open'", (market_id,)).fetchone()
    if existing:
        conn.close()
        return {"opened": False, "reason": "Already tracking this market"}

    # Stop-loss re-entry gate: if we got stopped out of this market,
    # only re-enter if current edge is STRONGER than original entry edge.
    # The market told us something -- do not repeat bad trades on weak signal.
    prev_stopped = conn.execute(
        "SELECT edge_pct, side, entry_price, pnl FROM paper_positions "
        "WHERE market_id=? AND status='stopped' ORDER BY closed_at DESC LIMIT 1",
        (market_id,)
    ).fetchone()
    if prev_stopped:
        prev_edge = prev_stopped["edge_pct"] or 0
        curr_edge = edge_pct or 0
        if curr_edge <= prev_edge:
            logger.info(
                "Re-entry BLOCKED: {} stopped with edge {:.1f}pp, new edge {:.1f}pp not stronger",
                market_id[:30], prev_edge, curr_edge
            )
            conn.close()
            return {"opened": False, "reason": f"Stopped previously (edge {prev_edge:.1f}pp), new edge {curr_edge:.1f}pp not stronger"}
        logger.info(
            "Re-entry ALLOWED: {} stopped with edge {:.1f}pp, new edge {:.1f}pp is stronger",
            market_id[:30], prev_edge, curr_edge
        )

    # Price momentum filter — only bet NO when YES is rising or flat
    momentum_data = None
    if HAS_MOMENTUM and side == "NO":
        mom_result = _check_momentum(market_id, market_price, side)
        momentum_data = mom_result.get("momentum_data")
        if not mom_result["allow"]:
            logger.info("Position blocked by momentum: market={} reason={}", market_id, mom_result.get("reason"))
            conn.close()
            return {"opened": False, "reason": mom_result.get("reason", "Momentum filter"), "archetype": archetype, "edge": eval_result["edge"], "momentum": momentum_data}
        if mom_result["multiplier"] > 1.0:
            bet_size *= mom_result["multiplier"]
            bet_size = min(MAX_BET, bet_size)
            logger.info("Momentum boost: market={} mult={} new_bet={}", market_id[:30], mom_result["multiplier"], bet_size)

    # Correlation cap — max positions per correlated group
    cap_reason = _check_correlation_cap(archetype, conn)
    if cap_reason:
        logger.info("Position blocked: market={} archetype={} reason={}", market_id, archetype, cap_reason)
        conn.close()
        return {"opened": False, "reason": cap_reason, "archetype": archetype, "edge": eval_result["edge"]}

    # Event concentration — max positions per underlying event
    event_reason = _check_event_concentration(market_title, archetype, conn)
    if event_reason:
        logger.info("Position blocked: market={} reason={}", market_id[:40], event_reason)
        conn.close()
        return {"opened": False, "reason": event_reason, "archetype": archetype, "edge": eval_result["edge"]}

    # Entity concentration guard (Mar 16 audit)
    entity_reason = _check_entity_concentration(market_title, conn)
    if entity_reason:
        logger.info("Position blocked: market={} reason={}", market_id[:40], entity_reason)
        conn.close()
        return {"opened": False, "reason": entity_reason, "archetype": archetype, "edge": eval_result["edge"]}

    slug = signal.get("event_slug") or signal.get("slug") or ""
    kelly_meta = eval_result.get("kelly", {})
    # Persist entry forecast snapshot for forecast-drift stops + calibration.
    # Per-archetype payload schema; the calibration tracker reads these to
    # recover the model's actual P(NO|YES) at entry, which is essential for
    # a non-conflated Brier score (vs the static `confidence` tier value).
    entry_forecast_json = None
    if archetype == "weather":
        weather_detail = signal.get("weather_detail")
        if weather_detail:
            try:
                entry_forecast_json = json.dumps(weather_detail)
            except (TypeError, ValueError):
                pass
    elif archetype == "social_count":
        # tweet_count_scanner already produced the MC outputs — capture
        # them so the calibration tracker has a real model probability.
        detail = {
            "type": "tweet_count_mc",
            "bracket":         signal.get("bracket"),
            "handle":          signal.get("handle"),
            "mc_yes_prob":     signal.get("mc_yes_prob"),
            "mc_no_prob":      signal.get("mc_no_prob"),
            "posts_so_far":    signal.get("posts_so_far"),
            "days_elapsed":    signal.get("days_elapsed"),
            "window_days":     signal.get("window_days"),
            "projected_total": signal.get("projected_total"),
            "daily_mean":      signal.get("daily_mean"),
            "daily_stdev":     signal.get("daily_stdev"),
            "days_to_close":   signal.get("days_to_close"),
        }
        detail = {k: v for k, v in detail.items() if v is not None}
        if any(k in detail for k in ("mc_yes_prob", "mc_no_prob")):
            try:
                entry_forecast_json = json.dumps(detail)
            except (TypeError, ValueError):
                pass
    conn.execute("""INSERT INTO paper_positions
        (opened_at, market_id, market_title, platform, side, entry_price, bet_size, potential_payout, confidence, edge_pct, status, archetype, strategy, market_slug, kelly_fraction, conviction_mult, entry_forecast_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)""",
        (datetime.now(timezone.utc).isoformat(), market_id, market_title,
         signal.get("platform", "kalshi"), side, market_price, bet_size,
         round(potential_payout, 2), confidence, eval_result["edge"], archetype, strategy, slug,
         kelly_meta.get("fraction"), kelly_meta.get("conviction_mult"), entry_forecast_json))
    conn.commit()
    conn.close()
    
    # Emit activity event
    if HAS_ACTIVITY_FEED:
        try:
            emit_event(
                "trade", "info",
                f"Paper position opened: {side} {market_title[:40]}",
                f"{side} @ {market_price:.2f} | ${bet_size:.0f} bet | {eval_result['edge']*100:.1f}% edge"
            )
        except Exception:
            pass

    # Discord alert
    try:
        from signals.discord_alerts import alert_position_opened
        mkt_url = f"https://polymarket.com/event/{slug}" if slug else ""
        alert_position_opened(market_title, side, market_price, bet_size, strategy,
                              eval_result.get("edge", 0) * 100, market_url=mkt_url,
                              confidence=confidence, archetype=archetype,
                              potential_payout=round(potential_payout, 2))
    except Exception as e:
        logger.debug("Discord alert failed: {}", e)

    return {"opened": True, "market_id": market_id, "side": side, "bet_size": bet_size, "edge": eval_result["edge"], "potential_payout": round(potential_payout, 2), "archetype": archetype}


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


# --- Portfolio status cache (15s TTL) ---
_status_cache = {"data": None, "ts": 0}
_STATUS_CACHE_TTL = 15  # seconds


def get_portfolio_status() -> dict:
    import time as _time
    now = _time.time()
    if _status_cache["data"] is not None and (now - _status_cache["ts"]) < _STATUS_CACHE_TTL:
        return _status_cache["data"]
    result = _get_portfolio_status_uncached()
    _status_cache["data"] = result
    _status_cache["ts"] = _time.time()
    return result


def _get_portfolio_status_uncached() -> dict:
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

    closed_count = conn.execute("SELECT COUNT(*) as c FROM paper_positions WHERE status IN ('won','lost','stopped')").fetchone()["c"]
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
    rows = conn.execute("SELECT * FROM paper_positions WHERE status IN ('won','lost','expired','stopped') ORDER BY closed_at DESC LIMIT ?", (limit,)).fetchall()
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


# --- Live positions cache (30s TTL) ---
_live_cache = {"data": None, "ts": 0}
_LIVE_CACHE_TTL = 30  # seconds


def get_live_positions() -> dict:
    """Get open positions enriched with current market prices and unrealized P&L.

    Uses 30s server-side cache + parallel price fetches for speed.
    """
    import time as _time
    now = _time.time()
    if _live_cache["data"] is not None and (now - _live_cache["ts"]) < _LIVE_CACHE_TTL:
        return _live_cache["data"]

    result = _fetch_live_positions()
    _live_cache["data"] = result
    _live_cache["ts"] = _time.time()
    return result


def _fetch_live_positions() -> dict:
    """Actual live position fetch with parallel price lookups."""
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    CLOB_API = "https://clob.polymarket.com"
    KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

    def _fetch(url, timeout=6):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception:
            return None

    conn = _get_db()
    rows = conn.execute("SELECT * FROM paper_positions WHERE status='open' ORDER BY opened_at DESC").fetchall()
    conn.close()

    if not rows:
        return {"positions": [], "count": 0, "total_unrealized_pnl": 0.0}

    # Parallel price fetch for all positions
    def _fetch_price(pos_dict):
        market_id = pos_dict["market_id"]
        platform = pos_dict.get("platform") or "kalshi"
        if platform == "polymarket" or market_id.startswith("0x"):
            data = _fetch(f"{CLOB_API}/markets/{market_id}")
            if data:
                tokens = data.get("tokens", [])
                if tokens:
                    return float(tokens[0].get("price", 0)), data.get("market_slug", "")
            return None, ""
        else:
            data = _fetch(f"{KALSHI_API}/markets/{market_id}")
            if data:
                market = data.get("market", data)
                cp = market.get("last_price")
                if cp and cp > 1:
                    cp = cp / 100
                return cp, ""
            return None, ""

    pos_dicts = [dict(r) for r in rows]

    # Fetch all prices in parallel (max 8 workers)
    with ThreadPoolExecutor(max_workers=8) as pool:
        price_results = list(pool.map(_fetch_price, pos_dicts))

    positions = []
    total_unrealized = 0.0

    for p, (current_price, slug) in zip(pos_dicts, price_results):
        side = p["side"]
        entry_price = p["entry_price"]
        bet_size = p["bet_size"]

        if slug:
            p["market_slug"] = slug

        if current_price is not None:
            if side == "YES":
                unrealized = bet_size * (current_price / entry_price - 1)
            else:
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
                p["stale"] = False
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


# ─────────────────────────────────────────────────────────────────────────────
# Equity snapshots — periodic time-series capture of realized + unrealized
# ─────────────────────────────────────────────────────────────────────────────

def snapshot_equity(include_live: bool = True) -> dict:
    """Capture a single equity snapshot (realized bankroll + live unrealized).

    Safe to call frequently — uses the live-positions 30s cache. If live fetch
    fails, falls back to realized-only so the snapshot still writes.
    """
    conn = _get_db()
    try:
        bankroll = _get_bankroll(conn)
        prev_peak = _get_peak(conn)
        open_ct = _count_open(conn)

        unrealized = 0.0
        if include_live and open_ct > 0:
            try:
                live = get_live_positions()
                unrealized = float(live.get("total_unrealized_pnl") or 0)
            except Exception as e:
                logger.debug(f"snapshot_equity: live fetch failed, using realized-only: {e}")
                unrealized = 0.0

        total_equity = bankroll + unrealized
        peak_equity = max(prev_peak, total_equity)

        ts = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO equity_snapshots
               (ts, realized_bankroll, unrealized_pnl, total_equity, open_positions, peak_equity, source)
               VALUES (?, ?, ?, ?, ?, ?, 'snapshot')""",
            (ts, round(bankroll, 2), round(unrealized, 2), round(total_equity, 2), open_ct, round(peak_equity, 2))
        )
        conn.commit()
        return {
            "ts": ts,
            "realized": round(bankroll, 2),
            "unrealized": round(unrealized, 2),
            "equity": round(total_equity, 2),
            "open_positions": open_ct,
        }
    finally:
        conn.close()


def backfill_equity_snapshots() -> dict:
    """One-time reconstruction of equity snapshots from closed trade history.

    Idempotent: only runs if the equity_snapshots table has no backfilled rows.
    Walks closed trades in chronological order and writes one snapshot per
    close (realized-only — no unrealized data for the past).
    """
    conn = _get_db()
    try:
        existing = conn.execute(
            "SELECT COUNT(*) AS c FROM equity_snapshots WHERE source='backfill'"
        ).fetchone()
        if existing and existing["c"] > 0:
            return {"status": "already_backfilled", "count": existing["c"]}

        trades = conn.execute(
            """SELECT closed_at, pnl FROM paper_positions
               WHERE status IN ('won','lost','stopped','expired')
                 AND closed_at IS NOT NULL
               ORDER BY closed_at ASC"""
        ).fetchall()

        if not trades:
            return {"status": "no_trades", "count": 0}

        # Seed snapshot at the time of the first trade with the starting bankroll.
        first_ts = trades[0]["closed_at"]
        bankroll = STARTING_BANKROLL
        peak = STARTING_BANKROLL
        rows = [(first_ts, round(bankroll, 2), 0.0, round(bankroll, 2), 0, round(peak, 2), "backfill")]

        for t in trades:
            bankroll += (t["pnl"] or 0)
            peak = max(peak, bankroll)
            rows.append((
                t["closed_at"],
                round(bankroll, 2),
                0.0,
                round(bankroll, 2),
                0,
                round(peak, 2),
                "backfill",
            ))

        conn.executemany(
            """INSERT INTO equity_snapshots
               (ts, realized_bankroll, unrealized_pnl, total_equity, open_positions, peak_equity, source)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        return {"status": "backfilled", "count": len(rows)}
    finally:
        conn.close()


def get_equity_series(hours: int | None = None, limit: int = 5000) -> list:
    """Return equity snapshots ordered oldest→newest.

    Args:
        hours: If set, only return snapshots from the last N hours.
        limit: Max rows to return (default 5000 — plenty for a year at 5min cadence).
    """
    conn = _get_db()
    try:
        # Select the most-recent N rows (DESC + LIMIT), then re-sort ASC for chart.
        # The earlier ORDER BY ts ASC LIMIT N silently truncated the tail once
        # row count exceeded `limit`, freezing the chart at the oldest N snapshots.
        if hours:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            rows = conn.execute(
                """SELECT ts, realized_bankroll, unrealized_pnl, total_equity, open_positions, peak_equity, source
                   FROM (
                       SELECT ts, realized_bankroll, unrealized_pnl, total_equity, open_positions, peak_equity, source
                       FROM equity_snapshots WHERE ts >= ? ORDER BY ts DESC LIMIT ?
                   ) ORDER BY ts ASC""",
                (cutoff, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT ts, realized_bankroll, unrealized_pnl, total_equity, open_positions, peak_equity, source
                   FROM (
                       SELECT ts, realized_bankroll, unrealized_pnl, total_equity, open_positions, peak_equity, source
                       FROM equity_snapshots ORDER BY ts DESC LIMIT ?
                   ) ORDER BY ts ASC""",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_archetype_breakdown() -> dict:
    """Compute win rate and P&L breakdown by archetype from closed trades."""
    conn = _get_db()
    closed = conn.execute(
        "SELECT archetype, status, pnl, bet_size, opened_at, closed_at FROM paper_positions WHERE status IN ('won','lost','stopped')"
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
                "SELECT archetype, status, pnl, bet_size, opened_at, closed_at FROM paper_positions WHERE status IN ('won','lost','stopped')"
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
        "SELECT archetype, pnl, closed_at FROM paper_positions WHERE status IN ('won','lost','stopped') ORDER BY closed_at ASC"
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

    # Calibration outcome log — manual closes feed the outcome metric only.
    # (No auto-resolved prefix → helper skips the model metric, correctly.)
    try:
        from signals.resolution_logger import log_position_close
        log_position_close(pos, won=(outcome == "won"), pnl=pnl,
                           close_reason=f"manual: {outcome}")
    except Exception as e:
        logger.warning("Manual-close resolution log failed: %s", e)

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
        "SELECT id, market_title, side, status, pnl, closed_at, close_reason, strategy "
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

        Returns (outcome, closing_line) tuple.
        outcome: 'YES' or 'NO', or None if not yet resolved.
        closing_line: YES token price at resolution time (for CLV tracking).
        """
        # Primary: CLOB API
        _closing_line = None  # YES token price for CLV
        data = _fetch(f"{CLOB_API}/markets/{market_id}")
        if data and data.get("closed"):
            tokens = data.get("tokens", [])
            if tokens:
                # Capture closing line for CLV tracking
                if len(tokens) >= 2:
                    _closing_line = float(tokens[0].get("price", 0.5))
                winner_token = None
                for t in tokens:
                    if t.get("winner") is True:
                        winner_token = t
                        break

                if winner_token is not None:
                    winner_name = (winner_token.get("outcome") or "").strip()
                    if winner_name.upper() in ("YES", "NO"):
                        return (winner_name.upper(), _closing_line)
                    if len(tokens) >= 2:
                        first_won = tokens[0].get("winner") is True
                        return ("YES" if first_won else "NO", _closing_line)

                # Closed but no winner flag — resolve via final token prices
                # (CLOB sometimes sets closed=True before setting winner on tokens)
                if len(tokens) >= 2:
                    yes_p = float(tokens[0].get("price", 0.5))
                    if yes_p > 0.95:
                        logger.info(f"Resolve {market_id[:16]}... YES (closed, price={yes_p:.2f}, no winner flag)")
                        return ("YES", _closing_line)
                    elif yes_p < 0.05:
                        logger.info(f"Resolve {market_id[:16]}... NO (closed, price={yes_p:.2f}, no winner flag)")
                        return ("NO", _closing_line)

        # Gamma API fallback REMOVED — condition_id search returns wrong markets
        # (e.g. different bracket/city for weather markets). CLOB is primary,
        # force-resolve below is the backstop. See 2026-02-28 session notes.

        # Check if market expired (end_date passed) — graduated force-resolve
        # More time past expiry = more lenient price threshold (both signals add certainty)
        # 0-6h:   don't force (market may still be settling)
        # 6-12h:  price must be >0.99 / <0.01 (near-certain)
        # 12-24h: price must be >0.975 / <0.025 (very likely)
        # 24h+:   price must be >0.95 / <0.05 (standard)
        if data and not data.get("closed"):
            tokens = data.get("tokens", [])
            if tokens and len(tokens) >= 2:
                yes_p = float(tokens[0].get("price", 0.5))
                _closing_line = yes_p
                end_date = data.get("end_date_iso") or ""
                if end_date:
                    try:
                        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                        hours_past = (datetime.now(timezone.utc) - end_dt).total_seconds() / 3600
                        # Graduated thresholds: tighter price requirement when less time has passed
                        if hours_past >= 24:
                            threshold = 0.95
                        elif hours_past >= 12:
                            threshold = 0.975
                        elif hours_past >= 6:
                            threshold = 0.99
                        else:
                            threshold = None  # Don't force-resolve in first 6h
                        if threshold is not None:
                            if yes_p > threshold:
                                logger.info(f"Force-resolve {market_id[:16]}... YES (price={yes_p:.3f}, {hours_past:.0f}h past expiry, threshold={threshold})")
                                return ("YES", _closing_line)
                            elif yes_p < (1 - threshold):
                                logger.info(f"Force-resolve {market_id[:16]}... NO (price={yes_p:.3f}, {hours_past:.0f}h past expiry, threshold={1-threshold})")
                                return ("NO", _closing_line)
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
        closing_line = None

        if platform == "polymarket" or market_id.startswith("0x"):
            _resolve_result = _resolve_polymarket(market_id, side)
            outcome = _resolve_result[0] if _resolve_result else None
            closing_line = _resolve_result[1] if _resolve_result else None
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

        resolution = 1.0 if won else 0.0
        conn.execute("""UPDATE paper_positions SET status=?, closed_at=?, exit_price=?, pnl=?, close_reason=?, closing_line=?, resolution_price=?
            WHERE id=?""",
            (status, datetime.now(timezone.utc).isoformat(),
             resolution, round(pnl, 2), f"auto-resolved: {outcome}", closing_line, resolution, pos["id"]))

        # Calibration logs — auto-resolved closes feed BOTH the outcome metric
        # (every close) and the model metric (auto-resolved only). The helper
        # routes by the "auto-resolved:" prefix on close_reason.
        try:
            from signals.resolution_logger import log_position_close
            log_position_close(pos, won=won, pnl=pnl,
                               close_reason=f"auto-resolved: {outcome}",
                               closing_line=closing_line)
        except Exception as e:
            logger.warning("Auto-resolution log failed: {}", e)

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

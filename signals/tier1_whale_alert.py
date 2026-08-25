#!/usr/bin/env python3
"""
Tier-1 Whale Alert — conviction-tier signal on top of whale tracking.

Rare (≤2x/wk) conviction alert meaning "this over everything else."
Logs every sized alert's characteristics + outcome to learn dumb-vs-informed empirically.
Maintains a Tier-1 scorecard at realistic fills toward the N=30 graduation bar.

Governing skill: 08-AI/Skills/Trading-Finance/whale-signal-graduation-gate/SKILL.md
Build spec: 04-Trading/Research/Prediction-Markets/Tier1-Alert-Build-Spec-2026-07-10.md
"""

import html
import json
import logging
import math
import os
import sqlite3
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Any
from config.polymarket_urls import clob_url  # polyproxy: central URL config

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
STORAGE_DIR = PROJECT_ROOT / "storage"
DB_PATH = STORAGE_DIR / "shadow_trades.db"

# ── Telegram config ──────────────────────────────────────────────────────
FERNANDO_TELEGRAM_CHAT_ID = "468298295"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# ── Tier-1 thresholds ────────────────────────────────────────────────────
# Rarity budget: Tier-1 must fire ≤ ~2x/week on average
# Track rolling 7-day count; if we'd exceed budget, suppress
TIER1_WEEKLY_BUDGET = 2
TIER1_ROLLING_DAYS = 7

# Size filter: print must be large relative to book depth
# A print is "sized" if it moves the book by at least this fraction
MIN_BOOK_IMPACT_RATIO = 0.15  # 15% of book depth

# Minimum absolute size to consider (avoid noise in thin markets)
MIN_ABSOLUTE_SIZE_USD = 500

# Later-larger rule: if both sides show whale flow, direction follows
# the later, larger, higher-conviction print
LATER_LARGER_WINDOW_HOURS = 24

# Scorecard constants
GRADUATION_BAR = 30  # N=30 resolved Tier-1 calls with positive realistic-fill CLV/P&L
SLIPPAGE_BPS = 50    # Realistic fill: assume 50bps slippage (conservative)

# ── State file ───────────────────────────────────────────────────────────
# Tracks rolling Tier-1 count, last alert timestamps, etc.
STATE_FILE = STORAGE_DIR / "tier1_state.json"


# ============================================================================
# Database Setup
# ============================================================================

def _get_db() -> sqlite3.Connection:
    """Get SQLite connection with WAL mode."""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _init_tables(conn)
    return conn


def _init_tables(conn: sqlite3.Connection):
    """Create Tier-1 tracking tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tier1_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fired_at TEXT NOT NULL,
            alert_type TEXT NOT NULL,  -- 'tier1' or 'sized'
            market_id TEXT,
            market_title TEXT,
            platform TEXT DEFAULT 'kalshi',
            side TEXT,
            flagged_price REAL,
            book_bid_depth REAL,
            book_ask_depth REAL,
            print_size_usd REAL,
            book_impact_ratio REAL,
            imbalance_ratio REAL,
            whale_count INTEGER,
            whale_consensus TEXT,
            later_larger_applied INTEGER DEFAULT 0,
            later_larger_side TEXT,
            reasoning TEXT,
            scorecard_number INTEGER,  -- which Tier-1 # this is (1-30)
            resolved INTEGER DEFAULT 0,
            resolved_outcome TEXT,
            realistic_fill_pnl REAL,
            realistic_fill_clv REAL,
            resolved_at TEXT
        );

        CREATE TABLE IF NOT EXISTS tier1_scorecard (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id INTEGER,
            tier1_number INTEGER NOT NULL,
            fired_at TEXT NOT NULL,
            market_title TEXT,
            side TEXT,
            flagged_price REAL,
            realistic_fill_price REAL,
            resolved_outcome TEXT,
            resolution_price REAL,
            pnl REAL,
            clv_bps REAL,
            resolved_at TEXT,
            FOREIGN KEY (alert_id) REFERENCES tier1_alerts(id)
        );

        CREATE INDEX IF NOT EXISTS idx_tier1_alerts_fired ON tier1_alerts(fired_at);
        CREATE INDEX IF NOT EXISTS idx_tier1_alerts_type ON tier1_alerts(alert_type);
        CREATE INDEX IF NOT EXISTS idx_tier1_scorecard_number ON tier1_scorecard(tier1_number);
    """)
    conn.commit()


# ============================================================================
# State Management (rolling rarity budget)
# ============================================================================

def _load_state() -> dict:
    """Load Tier-1 state from disk."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            pass
    return {
        "tier1_fire_timestamps": [],  # ISO timestamps of Tier-1 fires
        "last_sized_alert_ts": None,
        "weekly_count": 0,
        "weekly_count_updated": None,  # ISO date of last count update
    }


def _save_state(state: dict):
    """Save Tier-1 state to disk."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _get_rolling_tier1_count() -> int:
    """Count Tier-1 alerts in the rolling window (last 7 days)."""
    state = _load_state()
    now = time.time()
    cutoff = now - (TIER1_ROLLING_DAYS * 86400)
    # Prune old timestamps
    recent = [ts for ts in state.get("tier1_fire_timestamps", [])
              if isinstance(ts, (int, float)) and ts > cutoff]
    state["tier1_fire_timestamps"] = recent
    _save_state(state)
    return len(recent)


def _record_tier1_fire():
    """Record a Tier-1 fire timestamp for rarity budget tracking."""
    state = _load_state()
    now = time.time()
    cutoff = now - (TIER1_ROLLING_DAYS * 86400)
    # Prune old
    recent = [ts for ts in state.get("tier1_fire_timestamps", [])
              if isinstance(ts, (int, float)) and ts > cutoff]
    recent.append(now)
    state["tier1_fire_timestamps"] = recent
    _save_state(state)


# ============================================================================
# Tier-1 Evaluation Logic
# ============================================================================

def _calculate_book_impact(print_size_usd: float, bid_depth: float, ask_depth: float) -> float:
    """Calculate how much a print moves the book as a ratio of total depth."""
    total_depth = bid_depth + ask_depth
    if total_depth <= 0:
        return 0
    return print_size_usd / total_depth


def _evaluate_later_larger(whale_alerts: List[Dict], market_id: str) -> Optional[Dict]:
    """
    Apply the later-larger rule: if both sides show whale flow in the same market
    within the window, follow the later, larger, higher-conviction print.

    Returns dict with {'side': str, 'reasoning': str} or None if no conflict.
    """
    # Filter alerts for this market within the time window
    now = time.time()
    window_start = now - (LATER_LARGER_WINDOW_HOURS * 3600)
    
    market_alerts = [a for a in whale_alerts
                     if a.get("market_id") == market_id
                     and a.get("ts", 0) >= window_start]
    
    if len(market_alerts) < 2:
        return None
    
    # Check if both sides are represented
    sides = set()
    for a in market_alerts:
        side = a.get("side") or a.get("signal_side") or ""
        if side:
            sides.add(side)
    
    if len(sides) < 2:
        return None  # Only one side, no conflict
    
    # Both sides present — find the later, larger, higher-conviction print
    # Sort by timestamp descending (most recent first)
    sorted_alerts = sorted(market_alerts, key=lambda a: a.get("ts", 0), reverse=True)
    
    latest = sorted_alerts[0]
    latest_side = latest.get("side") or latest.get("signal_side", "")
    latest_size = latest.get("print_size_usd", 0) or latest.get("flow_dollars", 0) or 0
    latest_ts = latest.get("ts", 0)
    
    # Check if the latest is also the largest
    max_size = max(a.get("print_size_usd", 0) or a.get("flow_dollars", 0) or 0
                   for a in sorted_alerts)
    
    is_largest = latest_size >= max_size * 0.8  # Within 80% of max
    
    if is_largest:
        return {
            "side": latest_side,
            "reasoning": (
                f"Later-larger rule: latest print ({latest_side}, "
                f"${latest_size:,.0f}) is both most recent and largest. "
                f"Following {latest_side}."
            ),
        }
    
    # Latest isn't largest — check if the largest is recent enough
    largest = max(sorted_alerts, key=lambda a: a.get("print_size_usd", 0) or a.get("flow_dollars", 0) or 0)
    largest_side = largest.get("side") or largest.get("signal_side", "")
    largest_size = largest.get("print_size_usd", 0) or largest.get("flow_dollars", 0) or 0
    largest_ts = largest.get("ts", 0)
    
    # If largest is within last 6 hours, follow it
    if (now - largest_ts) < 21600:  # 6 hours
        return {
            "side": largest_side,
            "reasoning": (
                f"Later-larger rule: largest print ({largest_side}, "
                f"${largest_size:,.0f}) is within 6h window. "
                f"Following {largest_side}."
            ),
        }
    
    # Default to latest
    return {
        "side": latest_side,
        "reasoning": (
            f"Later-larger rule: following latest print ({latest_side}, "
            f"${latest_size:,.0f}) as most recent signal."
        ),
    }


def _check_rarity_budget() -> bool:
    """
    Check if we can fire a Tier-1 alert within the rarity budget.
    Returns True if allowed, False if budget is exhausted.
    """
    rolling_count = _get_rolling_tier1_count()
    if rolling_count >= TIER1_WEEKLY_BUDGET:
        logger.info(
            "TIER-1 RARITY BUDGET EXCEEDED: %d in last %d days (max %d)",
            rolling_count, TIER1_ROLLING_DAYS, TIER1_WEEKLY_BUDGET
        )
        return False
    return True


def _get_realistic_fill_price(flagged_price: float, side: str, bid_depth: float, ask_depth: float) -> float:
    """
    Calculate realistic fill price with slippage.
    Uses the existing liquidity cap logic: assume 50bps slippage on the effective price.
    """
    if side == "YES":
        # Buying YES: pay ask side, add slippage
        effective = flagged_price
        slippage = effective * (SLIPPAGE_BPS / 10000)
        return min(1.0, effective + slippage)
    else:
        # Buying NO: pay (1 - ask), add slippage
        effective = 1 - flagged_price
        slippage = effective * (SLIPPAGE_BPS / 10000)
        return min(1.0, effective + slippage)


# ============================================================================
# Core Evaluation Pipeline
# ============================================================================

def evaluate_whale_alert(alert: Dict, recent_alerts: List[Dict] = None) -> Dict:
    """
    Evaluate a single whale alert for Tier-1 potential.

    Returns:
        {
            "is_tier1": bool,
            "is_sized": bool,
            "tier1_number": int or None,
            "reasoning": str,
            "side": str,
            "flagged_price": float,
            "realistic_fill_price": float,
            "book_impact_ratio": float,
            "later_larger": dict or None,
        }
    """
    result = {
        "is_tier1": False,
        "is_sized": False,
        "tier1_number": None,
        "reasoning": "",
        "side": alert.get("side") or alert.get("signal_side", "YES"),
        "flagged_price": alert.get("best_bid", 0) or alert.get("best_ask", 0) or 0,
        "realistic_fill_price": 0,
        "book_impact_ratio": 0,
        "later_larger": None,
    }

    # ── Step 1: Size filter ──────────────────────────────────────────
    # Extract print size from alert
    print_size = (
        alert.get("print_size_usd") or
        alert.get("flow_dollars") or
        0
    )
    
    bid_depth = alert.get("bid_depth", 0) or 0
    ask_depth = alert.get("ask_depth", 0) or 0
    
    # If no explicit print size, estimate from book jump
    if print_size <= 0:
        # Use the level_jump values from reasons
        reasons = alert.get("reasons", "")
        if "level_jump_bid" in reasons:
            # Extract the jump amount
            import re
            m = re.search(r'level_jump_bid_(\d+)', reasons)
            if m:
                print_size = float(m.group(1))
        elif "level_jump_ask" in reasons:
            import re
            m = re.search(r'level_jump_ask_(\d+)', reasons)
            if m:
                print_size = float(m.group(1))
    
    if print_size <= 0:
        result["reasoning"] = "No measurable print size"
        return result
    
    # No book price → can't score a fill or grade CLV; skip rather than
    # inventing a 0.5 flagged price.
    if not result["flagged_price"]:
        result["reasoning"] = "No book price data; skipping evaluation"
        return result
    
    # Check absolute minimum
    if print_size < MIN_ABSOLUTE_SIZE_USD:
        result["reasoning"] = f"Print size ${print_size:,.0f} below minimum ${MIN_ABSOLUTE_SIZE_USD:,.0f}"
        return result
    
    # Calculate book impact
    book_impact = _calculate_book_impact(print_size, bid_depth, ask_depth)
    result["book_impact_ratio"] = book_impact
    
    if book_impact < MIN_BOOK_IMPACT_RATIO:
        result["reasoning"] = (
            f"Book impact {book_impact:.1%} below threshold {MIN_BOOK_IMPACT_RATIO:.0%} "
            f"(print ${print_size:,.0f} in ${bid_depth+ask_depth:,.0f} depth)"
        )
        return result
    
    result["is_sized"] = True
    
    # ── Step 2: Later-larger rule ────────────────────────────────────
    market_id = alert.get("market_id") or alert.get("id", "")
    if recent_alerts and market_id:
        later_larger = _evaluate_later_larger(recent_alerts, market_id)
        if later_larger:
            result["later_larger"] = later_larger
            result["side"] = later_larger["side"]
    
    # ── Step 3: Check rarity budget ───────────────────────────────────
    if not _check_rarity_budget():
        result["reasoning"] = (
            f"Sized alert (${print_size:,.0f}, impact {book_impact:.1%}) "
            f"but rarity budget exhausted"
        )
        return result
    
    # ── Step 4: Tier-1 conviction check ───────────────────────────────
    # Additional conviction criteria:
    # - Print is in a deep market (actor accepted real slippage)
    # - Imbalance ratio is significant
    # - Multiple whales on same side
    
    imbalance_ratio = alert.get("imbalance_ratio", 0) or 0
    whale_count = alert.get("whale_count", 0) or 0
    severity = alert.get("severity", "LOW")
    
    conviction_score = 0
    
    # Factor 1: Book impact (higher = more conviction)
    if book_impact >= 0.50:
        conviction_score += 3
    elif book_impact >= 0.30:
        conviction_score += 2
    elif book_impact >= 0.15:
        conviction_score += 1
    
    # Factor 2: Imbalance ratio
    if imbalance_ratio >= 5.0:
        conviction_score += 2
    elif imbalance_ratio >= 3.0:
        conviction_score += 1
    
    # Factor 3: Severity from existing pipeline
    if severity == "CRITICAL":
        conviction_score += 2
    elif severity == "HIGH":
        conviction_score += 1
    
    # Factor 4: Multiple whales
    if whale_count >= 3:
        conviction_score += 2
    elif whale_count >= 2:
        conviction_score += 1
    
    # Factor 5: Later-larger confirmation
    if result["later_larger"]:
        conviction_score += 1
    
    # Tier-1 threshold: need at least 4 conviction points
    if conviction_score < 4:
        result["reasoning"] = (
            f"Sized alert (${print_size:,.0f}, impact {book_impact:.1%}) "
            f"but conviction score {conviction_score}/10 below threshold 4"
        )
        return result
    
    # ── It's a Tier-1 alert! ─────────────────────────────────────────
    result["is_tier1"] = True
    result["realistic_fill_price"] = _get_realistic_fill_price(
        result["flagged_price"], result["side"], bid_depth, ask_depth
    )
    
    # Get the next scorecard number
    conn = _get_db()
    row = conn.execute("SELECT COUNT(*) as c FROM tier1_scorecard").fetchone()
    next_number = (row["c"] if row else 0) + 1
    conn.close()
    
    result["tier1_number"] = next_number
    
    # Build reasoning
    parts = [
        f"TIER-1 #{next_number}/{GRADUATION_BAR}",
        f"Conviction score: {conviction_score}/10",
        f"Print: ${print_size:,.0f} (impact {book_impact:.1%})",
    ]
    if imbalance_ratio > 0:
        parts.append(f"Imbalance: {imbalance_ratio:.1f}:1")
    if whale_count > 0:
        parts.append(f"Whales: {whale_count}")
    if result["later_larger"]:
        parts.append(f"Later-larger: {result['later_larger']['side']}")
    
    result["reasoning"] = " · ".join(parts)
    
    return result


# ============================================================================
# Logging
# ============================================================================

def log_sized_alert(alert: Dict, evaluation: Dict):
    """Log every sized alert (not just Tier-1) for dumb-vs-informed learning."""
    conn = _get_db()
    
    conn.execute("""
        INSERT INTO tier1_alerts
            (fired_at, alert_type, market_id, market_title, platform,
             side, flagged_price, book_bid_depth, book_ask_depth,
             print_size_usd, book_impact_ratio, imbalance_ratio,
             whale_count, whale_consensus, later_larger_applied,
             later_larger_side, reasoning, scorecard_number)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now(timezone.utc).isoformat(),
        "tier1" if evaluation["is_tier1"] else "sized",
        alert.get("market_id") or alert.get("id", ""),
        alert.get("title") or alert.get("question") or alert.get("market", ""),
        alert.get("platform", "kalshi"),
        evaluation["side"],
        evaluation["flagged_price"],
        alert.get("bid_depth", 0),
        alert.get("ask_depth", 0),
        alert.get("print_size_usd", 0) or alert.get("flow_dollars", 0) or 0,
        evaluation["book_impact_ratio"],
        alert.get("imbalance_ratio", 0),
        alert.get("whale_count", 0),
        alert.get("consensus", ""),
        1 if evaluation["later_larger"] else 0,
        evaluation["later_larger"]["side"] if evaluation["later_larger"] else None,
        evaluation["reasoning"],
        evaluation["tier1_number"],
    ))
    conn.commit()
    conn.close()
    
    logger.info(
        "Logged %s alert: %s — %s",
        "TIER-1" if evaluation["is_tier1"] else "sized",
        alert.get("market_id", "?")[:20],
        evaluation["reasoning"][:80],
    )


def log_tier1_scorecard_entry(alert: Dict, evaluation: Dict):
    """Log a Tier-1 scorecard entry (only for Tier-1 alerts)."""
    if not evaluation["is_tier1"]:
        return
    
    conn = _get_db()
    
    # Get the alert ID we just inserted
    row = conn.execute(
        "SELECT id FROM tier1_alerts WHERE scorecard_number = ? ORDER BY id DESC LIMIT 1",
        (evaluation["tier1_number"],)
    ).fetchone()
    alert_id = row["id"] if row else None
    
    conn.execute("""
        INSERT INTO tier1_scorecard
            (alert_id, tier1_number, fired_at, market_title, side,
             flagged_price, realistic_fill_price, resolved_outcome,
             resolution_price, pnl, clv_bps, resolved_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        alert_id,
        evaluation["tier1_number"],
        datetime.now(timezone.utc).isoformat(),
        alert.get("title") or alert.get("question") or alert.get("market", ""),
        evaluation["side"],
        evaluation["flagged_price"],
        evaluation["realistic_fill_price"],
        None,  # Not resolved yet
        None,
        None,  # P&L not known yet
        None,  # CLV not known yet
        None,
    ))
    conn.commit()
    conn.close()
    
    logger.info("Scorecard entry #%d logged", evaluation["tier1_number"])


# ============================================================================
# Resolution Tracking
# ============================================================================

def _fetch_json(url: str, timeout: int = 15) -> Optional[dict]:
    """Fetch JSON from a URL. Returns None on any failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.debug("fetch_json failed for %s: %s", url[:80], e)
        return None


def _get_closing_line(market_id: str, platform: str) -> Optional[float]:
    """
    Last traded YES price BEFORE resolution (the closing line).

    Returns None if unavailable — callers must record NULL CLV, never a
    fake 0 or outcome-derived value (0/1 makes "CLV" collapse into win/lose).
    """
    try:
        if platform == "polymarket" or (market_id or "").startswith("0x"):
            data = _fetch_json(clob_url(f"/markets/{market_id}"))
            tokens = (data or {}).get("tokens") or []
            if not tokens:
                return None
            token_id = tokens[0].get("token_id")
            if not token_id:
                return None
            hist = _fetch_json(
                clob_url(f"/prices-history?market={token_id}&interval=1w&fidelity=60")
            )
            pts = (hist or {}).get("history") or []
            if not pts:
                return None
            # Take the last point at or before market end; fall back to last point.
            end_iso = (data or {}).get("end_date_iso") or ""
            cutoff = None
            if end_iso:
                try:
                    cutoff = datetime.fromisoformat(
                        end_iso.replace("Z", "+00:00")
                    ).timestamp()
                except ValueError:
                    cutoff = None
            eligible = [p for p in pts if cutoff is None or p.get("t", 0) <= cutoff]
            if not eligible:
                eligible = pts
            price = eligible[-1].get("p")
            return float(price) if price is not None else None
        else:
            # Kalshi: last_price is the final traded price (in cents).
            data = _fetch_json(
                f"https://api.elections.kalshi.com/trade-api/v2/markets/{market_id}"
            )
            market = (data or {}).get("market", data or {})
            lp = market.get("last_price")
            return float(lp) / 100.0 if lp is not None else None
    except Exception as e:
        logger.debug("closing line fetch failed for %s: %s", (market_id or "")[:20], e)
        return None


def resolve_tier1_alerts():
    """
    Check all unresolved Tier-1 alerts against market resolution.
    Updates scorecard with realistic-fill P&L and CLV.
    """
    conn = _get_db()
    
    unresolved = conn.execute("""
        SELECT a.*, s.id as scorecard_id, s.tier1_number, s.realistic_fill_price
        FROM tier1_alerts a
        JOIN tier1_scorecard s ON a.scorecard_number = s.tier1_number
        WHERE a.alert_type = 'tier1'
          AND a.resolved = 0
          AND s.resolved_at IS NULL
    """).fetchall()
    
    if not unresolved:
        conn.close()
        return {"resolved": 0}
    
    resolved_count = 0
    now = datetime.now(timezone.utc).isoformat()
    
    for alert in unresolved:
        market_id = alert["market_id"]
        platform = alert["platform"] or "kalshi"
        side = alert["side"]
        flagged_price = alert["flagged_price"]
        realistic_fill_price = alert["realistic_fill_price"]
        
        # Try to resolve via existing resolution pipeline
        outcome = None
        resolution_price = None
        
        if platform == "polymarket" or (market_id and market_id.startswith("0x")):
            # Use existing resolution logic from paper_portfolio
            try:
                from signals.paper_portfolio import _resolve_polymarket
                resolve_result = _resolve_polymarket(market_id, side)
                if resolve_result:
                    outcome = resolve_result[0]
                    resolution_price = resolve_result[1]
            except Exception as e:
                logger.debug("Polymarket resolution failed for %s: %s", market_id[:20], e)
        else:
            # Kalshi resolution
            try:
                KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
                import urllib.request, json
                req = urllib.request.Request(
                    f"{KALSHI_API}/markets/{market_id}",
                    headers={"User-Agent": "Polyclawd/1.0"}
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
                market = data.get("market", data)
                result = market.get("result", "")
                if result:
                    outcome = result.upper()
                    resolution_price = 1.0 if outcome == "YES" else 0.0
            except Exception as e:
                logger.debug("Kalshi resolution failed for %s: %s", market_id[:20], e)
        
        if not outcome:
            continue
        
        # Calculate realistic-fill P&L
        won = (outcome == side)
        if won:
            if side == "YES":
                pnl = 100 * (1 / realistic_fill_price - 1)  # Per $100 notional
            else:
                pnl = 100 * (1 / (1 - realistic_fill_price) - 1)
        else:
            pnl = -100  # Lose full $100 notional
        
        # CLV (Closing Line Value): last pre-resolution traded price vs fill.
        # NOT the resolution outcome (0/1) — that would collapse CLV into win/lose.
        # NULL when no closing line is retrievable; NULL rows are excluded from
        # the graduation bar's CLV counts rather than polluting them with fakes.
        closing_yes = _get_closing_line(market_id, platform)
        if closing_yes is not None:
            if side == "YES":
                clv_bps = (closing_yes - realistic_fill_price) * 10000
            else:
                clv_bps = (realistic_fill_price - closing_yes) * 10000
        else:
            clv_bps = None
            logger.warning(
                "Tier-1 #%d resolved with NULL CLV (no closing line for %s)",
                alert["tier1_number"], (market_id or "")[:24],
            )
        
        clv_rounded = round(clv_bps, 1) if clv_bps is not None else None
        
        # Update tier1_alerts
        conn.execute("""
            UPDATE tier1_alerts
            SET resolved = 1, resolved_outcome = ?, realistic_fill_pnl = ?,
                realistic_fill_clv = ?, resolved_at = ?
            WHERE id = ?
        """, (outcome, round(pnl, 2), clv_rounded, now, alert["id"]))
        
        # Update scorecard
        conn.execute("""
            UPDATE tier1_scorecard
            SET resolved_outcome = ?, resolution_price = ?,
                pnl = ?, clv_bps = ?, resolved_at = ?
            WHERE id = ?
        """, (outcome, resolution_price, round(pnl, 2), clv_rounded, now, alert["scorecard_id"]))
        
        resolved_count += 1
        logger.info(
            "Tier-1 #%d resolved: %s → %s (P&L: $%.2f, CLV: %s)",
            alert["tier1_number"], side, outcome, pnl,
            f"{clv_bps:+.1f}bps" if clv_bps is not None else "NULL",
        )
    
    conn.commit()
    conn.close()
    
    return {"resolved": resolved_count}


# ============================================================================
# Scorecard Status
# ============================================================================

def get_scorecard_status() -> Dict:
    """
    Get current Tier-1 scorecard status.
    
    Returns:
        {
            "total_tier1": int,
            "resolved": int,
            "positive_clv": int,
            "negative_clv": int,
            "running_clv_bps": float,
            "running_pnl": float,
            "graduation_progress": str,  # e.g. "5/30"
            "on_track": bool,
            "recent_alerts": [...],
        }
    """
    conn = _get_db()
    
    total = conn.execute(
        "SELECT COUNT(*) as c FROM tier1_scorecard"
    ).fetchone()["c"]
    
    resolved = conn.execute(
        "SELECT COUNT(*) as c FROM tier1_scorecard WHERE resolved_at IS NOT NULL"
    ).fetchone()["c"]
    
    positive_clv = conn.execute(
        "SELECT COUNT(*) as c FROM tier1_scorecard WHERE clv_bps > 0"
    ).fetchone()["c"]
    
    negative_clv = conn.execute(
        "SELECT COUNT(*) as c FROM tier1_scorecard WHERE clv_bps < 0"
    ).fetchone()["c"]
    
    running_clv = conn.execute(
        "SELECT COALESCE(SUM(clv_bps), 0) as s FROM tier1_scorecard WHERE clv_bps IS NOT NULL"
    ).fetchone()["s"]
    
    running_pnl = conn.execute(
        "SELECT COALESCE(SUM(pnl), 0) as s FROM tier1_scorecard WHERE pnl IS NOT NULL"
    ).fetchone()["s"]
    
    # Recent alerts (last 10)
    recent = conn.execute("""
        SELECT tier1_number, fired_at, market_title, side, flagged_price,
               resolved_outcome, pnl, clv_bps
        FROM tier1_scorecard
        ORDER BY id DESC LIMIT 10
    """).fetchall()
    
    conn.close()
    
    on_track = (positive_clv > negative_clv) if resolved > 0 else None
    
    return {
        "total_tier1": total,
        "resolved": resolved,
        "positive_clv": positive_clv,
        "negative_clv": negative_clv,
        "running_clv_bps": round(running_clv, 1),
        "running_pnl": round(running_pnl, 2),
        "graduation_progress": f"{resolved}/{GRADUATION_BAR}",
        "on_track": on_track,
        "recent_alerts": [dict(r) for r in recent],
    }


# ============================================================================
# Telegram Delivery
# ============================================================================

def _send_telegram(message: str) -> bool:
    """Send a Telegram message to Fernando via Bot API.

    Direct Bot API only — the openclaw CLI does not exist on the VPS where
    the scheduler runs. Failures are logged at ERROR so silent alert loss
    (the 2026-07-11 incident) is visible in the journal.
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set — Tier-1 alert NOT delivered")
        return False
    
    fields = {
        "chat_id": FERNANDO_TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    data = urllib.parse.urlencode(fields).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            return result.get("ok", False)
    except Exception as e:
        logger.error("Telegram Bot API send failed: %s", e)
        return False


def _format_tier1_alert(alert: Dict, evaluation: Dict) -> str:
    """Format a Tier-1 alert message for Telegram."""
    scorecard = get_scorecard_status()
    # HTML-escape: this message is sent with parse_mode=HTML and this sender has
    # no plain-text fallback — a raw < in a market title (weather brackets like
    # "<85°") 400s at Telegram and DROPS the Tier-1 alert outright.
    market_title = html.escape(
        str(alert.get("title") or alert.get("question") or alert.get("market", "Unknown market")),
        quote=False)
    side = evaluation["side"]
    price = evaluation["flagged_price"]
    print_size = alert.get("print_size_usd", 0) or alert.get("flow_dollars", 0) or 0
    impact = evaluation["book_impact_ratio"]
    imbalance = alert.get("imbalance_ratio", 0)
    whale_count = alert.get("whale_count", 0)
    
    lines = [
        "🐋 <b>TIER-1 WHALE ALERT</b> 🐋",
        "",
        f"<b>#{evaluation['tier1_number']}/{GRADUATION_BAR}</b> · {market_title[:100]}",
        "",
        f"<b>{side}</b> @ {price:.0%}",
        f"Print: <b>${print_size:,.0f}</b> (book impact {impact:.1%})",
    ]
    
    if imbalance:
        lines.append(f"Imbalance: {imbalance:.1f}:1")
    if whale_count:
        lines.append(f"Whales: {whale_count}")
    if evaluation["later_larger"]:
        lines.append(f"Later-larger: {evaluation['later_larger']['side']}")
    
    lines.append("")
    lines.append(f"📊 <b>Scorecard</b>: {scorecard['graduation_progress']} resolved")
    if scorecard["resolved"] > 0:
        lines.append(f"Running CLV: {scorecard['running_clv_bps']:+.1f}bps")
        lines.append(f"Running P&L: ${scorecard['running_pnl']:+.2f}")
        lines.append(f"Positive CLV: {scorecard['positive_clv']}/{scorecard['resolved']}")
    
    lines.append("")
    lines.append("<i>No real-money execution. Graduation bar: N=30 with positive CLV.</i>")
    
    return "\n".join(lines)


def _format_sized_alert_log(alert: Dict, evaluation: Dict) -> str:
    """Format a sized (non-Tier-1) alert for internal logging."""
    market_title = alert.get("title") or alert.get("question") or alert.get("market", "Unknown")
    print_size = alert.get("print_size_usd", 0) or alert.get("flow_dollars", 0) or 0
    impact = evaluation["book_impact_ratio"]
    
    return (
        f"[SIZED] {market_title[:60]} | "
        f"${print_size:,.0f} print | "
        f"impact {impact:.1%} | "
        f"side {evaluation['side']} | "
        f"{evaluation['reasoning'][:60]}"
    )


# ============================================================================
# Main Entry Point
# ============================================================================

def process_whale_alerts(alerts: List[Dict]) -> Dict:
    """
    Process a batch of whale alerts and fire Tier-1 alerts as needed.
    
    Args:
        alerts: List of whale alert dicts from the whale pipeline
                (polyclawd__polyclawd_whale_alerts format)
    
    Returns:
        {
            "alerts_processed": int,
            "sized_alerts": int,
            "tier1_fired": int,
            "tier1_details": [...],
            "scorecard": {...},
        }
    """
    if not alerts:
        return {"alerts_processed": 0, "sized_alerts": 0, "tier1_fired": 0, "tier1_details": []}
    
    # First, resolve any pending Tier-1 alerts
    resolve_result = resolve_tier1_alerts()
    if resolve_result["resolved"] > 0:
        logger.info("Resolved %d Tier-1 alerts", resolve_result["resolved"])
    
    sized_count = 0
    tier1_count = 0
    tier1_details = []
    
    for alert in alerts:
        evaluation = evaluate_whale_alert(alert, alerts)
        
        if not evaluation["is_sized"]:
            continue
        
        sized_count += 1
        
        # Log every sized alert
        log_sized_alert(alert, evaluation)
        logger.info(_format_sized_alert_log(alert, evaluation))
        
        if evaluation["is_tier1"]:
            tier1_count += 1
            tier1_details.append({
                "market_id": alert.get("market_id", ""),
                "market_title": alert.get("title") or alert.get("question", ""),
                "side": evaluation["side"],
                "price": evaluation["flagged_price"],
                "tier1_number": evaluation["tier1_number"],
                "reasoning": evaluation["reasoning"],
            })
            
            # Log scorecard entry
            log_tier1_scorecard_entry(alert, evaluation)
            
            # Record for rarity budget
            _record_tier1_fire()
            
            # Send Telegram alert
            message = _format_tier1_alert(alert, evaluation)
            sent = _send_telegram(message)
            logger.info(
                "TIER-1 #%d fired: %s %s @ %.0f%% — Telegram sent: %s",
                evaluation["tier1_number"],
                evaluation["side"],
                (alert.get("title") or alert.get("question", ""))[:40],
                evaluation["flagged_price"] * 100,
                sent,
            )
    
    scorecard = get_scorecard_status()
    
    return {
        "alerts_processed": len(alerts),
        "sized_alerts": sized_count,
        "tier1_fired": tier1_count,
        "tier1_details": tier1_details,
        "scorecard": scorecard,
    }


def get_tier1_status() -> Dict:
    """
    Get current Tier-1 system status (for API/dashboard).
    """
    scorecard = get_scorecard_status()
    state = _load_state()
    
    return {
        "scorecard": scorecard,
        "rarity_budget": {
            "weekly_budget": TIER1_WEEKLY_BUDGET,
            "rolling_7d_count": _get_rolling_tier1_count(),
            "budget_remaining": max(0, TIER1_WEEKLY_BUDGET - _get_rolling_tier1_count()),
        },
        "config": {
            "min_book_impact_ratio": MIN_BOOK_IMPACT_RATIO,
            "min_absolute_size_usd": MIN_ABSOLUTE_SIZE_USD,
            "slippage_bps": SLIPPAGE_BPS,
            "graduation_bar": GRADUATION_BAR,
            "later_larger_window_hours": LATER_LARGER_WINDOW_HOURS,
        },
        "last_sized_alert_ts": state.get("last_sized_alert_ts"),
    }


# ============================================================================
# CLI
# ============================================================================

def main():
    """CLI entry point for testing."""
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    
    if cmd == "status":
        status = get_tier1_status()
        print(json.dumps(status, indent=2, default=str))
    
    elif cmd == "resolve":
        result = resolve_tier1_alerts()
        print(json.dumps(result, indent=2))
    
    elif cmd == "scorecard":
        card = get_scorecard_status()
        print(json.dumps(card, indent=2, default=str))
    
    elif cmd == "test-alert":
        # Send a test Tier-1 alert to Telegram
        test_alert = {
            "market_id": "test_market_123",
            "title": "Test: Will BTC reach $150K by Dec 2026?",
            "platform": "kalshi",
            "side": "YES",
            "best_bid": 0.35,
            "best_ask": 0.40,
            "bid_depth": 50000,
            "ask_depth": 50000,
            "print_size_usd": 15000,
            "flow_dollars": 15000,
            "imbalance_ratio": 4.5,
            "whale_count": 3,
            "severity": "HIGH",
            "consensus": "STRONG YES",
        }
        evaluation = evaluate_whale_alert(test_alert)
        print("Evaluation:", json.dumps(evaluation, indent=2, default=str))
        
        if evaluation["is_tier1"]:
            log_tier1_scorecard_entry(test_alert, evaluation)
            _record_tier1_fire()
            msg = _format_tier1_alert(test_alert, evaluation)
            print("\n--- Telegram Message ---")
            print(msg)
            sent = _send_telegram(msg)
            print(f"\nSent: {sent}")
        else:
            print("Not Tier-1:", evaluation["reasoning"])
    
    else:
        print(f"Usage: {sys.argv[0]} [status|resolve|scorecard|test-alert]")


if __name__ == "__main__":
    main()

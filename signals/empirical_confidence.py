"""Empirical Confidence Engine — Phase 1 of Confidence Redesign.

Replaces the old signal-quality-based confidence with actual win probability
estimated from resolved trades. Confidence now means "probability we win this trade."

Data-driven. Self-improving. Honest.
"""

import sqlite3
import re
import logging
from typing import Dict, Tuple, Optional
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "storage" / "shadow_trades.db"

# ─── Archetype Classification ────────────────────────────────────────

_INTRADAY_RE = re.compile(
    r'\d+[:\d]*\s*(am|pm)|'
    r'\b(5m|15m|30m|1h|4h)\b|'
    r'(am|pm)\s*(to|-|–)\s*(am|pm)',
    re.IGNORECASE
)

# Import full archetype classifier (14 archetypes) from mispriced_category_signal
try:
    from mispriced_category_signal import classify_archetype
except ImportError:
    # Fallback if import fails
    def classify_archetype(title: str) -> str:
        """Minimal fallback classifier."""
        if not title: return "other"
        t = title.lower()
        if 'up or down' in t: return 'daily_updown'
        if 'above' in t or 'below' in t: return 'price_above'
        if 'between' in t or 'range' in t: return 'price_range'
        return 'other'



def price_zone(price: float) -> str:
    """Classify entry price into zone."""
    if price < 0.30:
        return 'garbage'
    elif price < 0.45:
        return 'cheap'
    elif price < 0.55:
        return 'mid_low'
    elif price < 0.65:
        return 'mid'
    elif price < 0.75:
        return 'sweet'
    elif price < 0.85:
        return 'premium'
    else:
        return 'expensive'


# ─── Price Zone Modifiers (from empirical data, 51 trades) ───────────

PRICE_ZONE_MODIFIERS = {
    'garbage':   0.25,   # <30¢ → 20% WR
    'cheap':     0.55,   # 30-45¢ → 43% WR
    'mid_low':   0.85,   # 45-55¢ → limited data, neutral
    'mid':       1.00,   # 55-65¢ → 64% WR (reference zone)
    'sweet':     1.15,   # 65-75¢ → best risk/reward
    'premium':   1.10,   # 75-85¢ → 80% WR, strong
    'expensive': 0.75,   # 85-100¢ → 50% WR, overpaying
}

# ─── Becker Priors (408K Polymarket markets, fallback when <10 resolved) ──
BECKER_NO_WIN_RATES = {
    'daily_updown':      0.517,  # n=36,759
    'intraday_updown':   0.517,  # same as daily (coin flip)
    'price_above':       0.528,  # n=4,044
    'price_range':       0.886,  # n=2,917 — strongest edge
    'ai_model':          0.774,  # n=1,800
    'geopolitical':      0.696,  # n=4,685 (election proxy)
    'election':          0.696,  # n=4,685
    'sports_winner':     0.781,  # n=7,538
    'sports_single_game':0.781,  # same as sports
    'entertainment':     0.600,  # n=1,201 (estimated)
    'deadline_binary':   0.600,  # generic fallback
    'social_count':      0.600,  # generic fallback
    'weather':           0.600,  # n=8,575 (limited resolution data)
    'directional':       0.600,  # generic fallback
    'other':             0.593,  # overall base rate
}

# ─── Duration Modifier (Becker: longer markets = more NO edge) ──────
DURATION_MODIFIERS = {
    'daily':    0.85,   # 0-1d: 51.7% NO (weak)
    'short':    0.95,   # 2-3d: 55.4% NO
    'weekly':   1.10,   # 4-7d: 65.7% NO (sweet spot)
    'biweekly': 1.05,   # 8-14d: 60.7% NO
    'monthly':  1.10,   # 15-30d: 66.3% NO
    'quarterly':1.15,   # 31-90d: 76.5% NO (strongest)
    'long':     1.10,   # >90d: 66.9% NO
}

def classify_duration(days_to_close: float) -> str:
    """Classify market duration for Becker modifier."""
    if days_to_close <= 1:
        return 'daily'
    elif days_to_close <= 3:
        return 'short'
    elif days_to_close <= 7:
        return 'weekly'
    elif days_to_close <= 14:
        return 'biweekly'
    elif days_to_close <= 30:
        return 'monthly'
    elif days_to_close <= 90:
        return 'quarterly'
    else:
        return 'long'



# ─── Kill Rules ──────────────────────────────────────────────────────

def check_kill_rules(title: str, entry_price: float, side: str) -> Tuple[bool, str]:
    """Hard reject losing combos. Returns (killed, reason)."""
    archetype = classify_archetype(title)
    # entry_price here is always the YES market price (0-1)
    price_cents = int(entry_price * 100)

    # K3: Anything below 30¢
    if price_cents < 30:
        return True, f"K3: entry {price_cents}¢ < 30¢ floor (20% WR historically)"

    # K1: Intraday up/down — any side (coin flip minus fees)
    if archetype == 'intraday_updown':
        return True, "K1: intraday up/down (53% WR, no edge after fees)"

    # K4: Price range — only kill YES side (Becker: NO wins 89%, n=2,447)
    if archetype == 'price_range' and side == 'YES':
        return True, "K4: price_range YES side (11% WR per Becker 408K study)"
    # price_range NO passes through — 89% WR

    # K5: Directional dip/crash longshots
    if archetype == 'directional':
        return True, "K5: directional dip/crash bet (0% WR)"

    # K2: price_above + cheap YES
    if archetype == 'price_above' and side == 'YES' and price_cents < 45:
        return True, "K2: price_above cheap YES (20% WR)"

    # K6: Unknown archetype
    if archetype == 'other':
        return True, "K6: unclassified archetype — don't trade unknowns"

    return False, ""


# ─── Empirical WR Lookup ─────────────────────────────────────────────

def _load_resolved_trades() -> list:
    """Load all resolved trades from DB for WR calculation."""
    try:
        db = sqlite3.connect(str(DB_PATH))
        db.row_factory = sqlite3.Row

        trades = []

        # Shadow trades
        for t in db.execute("SELECT market, side, entry_price, outcome, platform FROM shadow_trades WHERE resolved=1").fetchall():
            trades.append({
                "title": t["market"] or "",
                "side": t["side"] or "?",
                "price": t["entry_price"] or 0,
                "won": t["side"] == t["outcome"],
                "platform": t["platform"] or "unknown",
            })

        # Paper trades
        for t in db.execute("SELECT market_title, side, entry_price, status, platform FROM paper_positions WHERE status IN ('won','lost')").fetchall():
            trades.append({
                "title": t["market_title"] or "",
                "side": t["side"],
                "price": t["entry_price"],
                "won": t["status"] == "won",
                "platform": t["platform"] or "unknown",
            })

        db.close()
        return trades
    except Exception as e:
        logger.error(f"Failed to load resolved trades: {e}")
        return []


def _compute_wr_table(trades: list) -> Dict[str, Dict]:
    """Build WR lookup table keyed by (archetype, side)."""
    table = {}
    for t in trades:
        arch = classify_archetype(t["title"])
        key = f"{arch}|{t['side']}"
        if key not in table:
            table[key] = {"wins": 0, "total": 0}
        table[key]["total"] += 1
        if t["won"]:
            table[key]["wins"] += 1

    # Add WR
    for key, stats in table.items():
        stats["wr"] = stats["wins"] / stats["total"] if stats["total"] > 0 else 0.5

    return table


def bayesian_smooth(prior_wr: float, bucket_wr: float, n: int, prior_weight: int = 5) -> float:
    """Bayesian smoothing with conjugate beta prior.
    
    prior_weight controls how much we trust the prior vs data.
    As n grows, bucket data dominates.
    """
    if n == 0:
        return prior_wr
    return (prior_wr * prior_weight + bucket_wr * n) / (prior_weight + n)


# ─── Main Confidence Calculator ─────────────────────────────────────

# Cache for WR table (refreshed each call — DB is small)
_wr_cache = None
_wr_cache_count = 0

def calculate_empirical_confidence(
    title: str,
    side: str,
    entry_price: float,
    force_refresh: bool = False,
    days_to_close: float = 7.0,
) -> Dict:
    """Calculate honest win probability from empirical data.
    
    Returns:
        {
            "confidence": float (0-1),  # Our estimated P(win)
            "edge": float,              # confidence - cost_basis (honest)
            "archetype": str,
            "price_zone": str,
            "base_wr": float,           # Raw archetype|side WR
            "smoothed_wr": float,       # After Bayesian smoothing
            "zone_modifier": float,
            "sample_size": int,
            "killed": bool,
            "kill_reason": str,
            "breakdown": dict,
        }
    """
    global _wr_cache, _wr_cache_count

    archetype = classify_archetype(title)
    zone = price_zone(entry_price)
    zone_mod = PRICE_ZONE_MODIFIERS.get(zone, 1.0)

    # Kill rule check
    killed, kill_reason = check_kill_rules(title, entry_price, side)
    if killed:
        return {
            "confidence": 0.0,
            "edge": -1.0,
            "archetype": archetype,
            "price_zone": zone,
            "base_wr": 0.0,
            "smoothed_wr": 0.0,
            "zone_modifier": zone_mod,
            "duration_modifier": 1.0,
            "duration_bucket": "unknown",
            "sample_size": 0,
            "killed": True,
            "kill_reason": kill_reason,
            "breakdown": {},
        }

    # Load WR table
    trades = _load_resolved_trades()
    total_resolved = len(trades)
    wr_table = _compute_wr_table(trades)

    # Determine prior weight based on total sample size
    if total_resolved < 30:
        prior_weight = 10  # Conservative
    elif total_resolved < 100:
        prior_weight = 5   # Balanced
    elif total_resolved < 300:
        prior_weight = 3   # Data-driven
    else:
        prior_weight = 1   # Empirical

    # Look up archetype|side WR
    key = f"{archetype}|{side}"
    bucket = wr_table.get(key, {"wins": 0, "total": 0, "wr": 0.5})
    base_wr = bucket["wr"]
    n = bucket["total"]

    # Also check the more specific archetype|side|zone bucket
    zone_key = f"{archetype}|{side}|{zone}"
    zone_trades = [t for t in trades 
                   if classify_archetype(t["title"]) == archetype 
                   and t["side"] == side 
                   and price_zone(t["price"]) == zone]
    zone_n = len(zone_trades)
    zone_wr = sum(t["won"] for t in zone_trades) / zone_n if zone_n > 0 else base_wr

    # Archetype-level prior (all sides combined)
    arch_trades = [t for t in trades if classify_archetype(t["title"]) == archetype]
    arch_wr = sum(t["won"] for t in arch_trades) / len(arch_trades) if arch_trades else 0.50

    # Overall system prior
    overall_wr = sum(t["won"] for t in trades) / len(trades) if trades else 0.50

    # Two-level Bayesian smoothing:
    # 1. Smooth archetype|side bucket toward archetype prior
    side_smoothed = bayesian_smooth(arch_wr, base_wr, n, prior_weight)
    # 2. If we have zone-level data (n>=2), smooth zone toward side level
    if zone_n >= 2:
        smoothed = bayesian_smooth(side_smoothed, zone_wr, zone_n, max(2, prior_weight // 2))
    else:
        smoothed = side_smoothed

    # Apply price zone modifier
    # Apply duration modifier (Becker: weekly/monthly NO markets much stronger)
    # days_to_close passed as parameter (default 7.0)  # default to weekly
    dur_bucket = classify_duration(days_to_close)
    dur_mod = DURATION_MODIFIERS.get(dur_bucket, 1.0)
    
    confidence = smoothed * zone_mod * dur_mod

    # Cap at 92% — nothing is certain
    confidence = min(0.92, max(0.08, confidence))

    # Calculate honest edge
    if side == "YES":
        cost_basis = entry_price
    else:
        cost_basis = 1.0 - entry_price

    edge = confidence - cost_basis

    return {
        "confidence": round(confidence, 4),
        "edge": round(edge, 4),
        "archetype": archetype,
        "price_zone": zone,
        "base_wr": round(base_wr, 4),
        "smoothed_wr": round(smoothed, 4),
        "zone_modifier": zone_mod,
        "duration_modifier": dur_mod,
        "duration_bucket": dur_bucket,
        "sample_size": n,
        "total_resolved": total_resolved,
        "prior_weight": prior_weight,
        "killed": False,
        "kill_reason": "",
        "breakdown": {
            "archetype_wr": round(arch_wr, 4),
            "overall_wr": round(overall_wr, 4),
            "bucket_key": key,
            "bucket_n": n,
            "bucket_wins": bucket["wins"],
            "zone_key": zone_key,
            "zone_n": zone_n,
            "zone_wr": round(zone_wr, 4),
            "side_smoothed": round(side_smoothed, 4),
        },
    }


# ─── Calibration Audit ───────────────────────────────────────────────

def calibration_audit() -> Dict:
    """Check if predicted confidence matches actual win rates.
    
    Perfect calibration: 70% confidence trades win 70% of the time.
    """
    trades = _load_resolved_trades()
    if not trades:
        return {"error": "No resolved trades", "buckets": []}

    # Calculate empirical confidence for each historical trade
    results = []
    for t in trades:
        ec = calculate_empirical_confidence(t["title"], t["side"], t["price"])
        if not ec["killed"]:
            results.append({
                "confidence": ec["confidence"],
                "won": t["won"],
            })

    # Bin by confidence decile
    buckets = []
    for lo_pct in range(30, 95, 10):
        lo = lo_pct / 100
        hi = (lo_pct + 10) / 100
        bucket = [r for r in results if lo <= r["confidence"] < hi]
        if bucket:
            predicted = (lo + hi) / 2
            actual = sum(r["won"] for r in bucket) / len(bucket)
            miscalibration = actual - predicted
            buckets.append({
                "range": f"{lo_pct}-{lo_pct+10}%",
                "predicted_wr": round(predicted * 100, 1),
                "actual_wr": round(actual * 100, 1),
                "miscalibration": round(miscalibration * 100, 1),
                "n": len(bucket),
                "calibrated": abs(miscalibration) < 0.10,
            })

    overall_error = sum(abs(b["miscalibration"]) for b in buckets) / len(buckets) if buckets else 0
    return {
        "total_trades": len(results),
        "avg_calibration_error": round(overall_error, 1),
        "calibrated": overall_error < 10,
        "buckets": buckets,
    }

"""Empirical Confidence Engine — Phase 1 of Confidence Redesign.

Replaces the old signal-quality-based confidence with actual win probability
estimated from resolved trades. Confidence now means "probability we win this trade."

Data-driven. Self-improving. Honest.
"""

import sqlite3
import re
import time
from typing import Dict, Tuple, Optional
from loguru import logger
from pathlib import Path


DB_PATH = Path(__file__).parent.parent / "storage" / "shadow_trades.db"

# ─── Archetype Classification ────────────────────────────────────────

_INTRADAY_RE = re.compile(
    r"\d+[:\d]*\s*(am|pm)|"
    r"\b(5m|15m|30m|1h|4h)\b|"
    r"(am|pm)\s*(to|-|–)\s*(am|pm)",
    re.IGNORECASE,
)

# Import full archetype classifier (14 archetypes) from mispriced_category_signal
try:
    from signals.mispriced_category_signal import classify_archetype
except ImportError:
    try:
        from mispriced_category_signal import classify_archetype
    except ImportError:
        # Fallback if both imports fail
        def classify_archetype(title: str) -> str:
            """Minimal fallback classifier."""
            if not title:
                return "other"
            t = title.lower()
            if "up or down" in t:
                return "daily_updown"
            if "above" in t or "below" in t:
                return "price_above"
            if "between" in t or "range" in t:
                return "price_range"
            return "other"


def price_zone(price: float) -> str:
    """Classify entry price into zone."""
    if price < 0.30:
        return "garbage"
    elif price < 0.45:
        return "cheap"
    elif price < 0.55:
        return "mid_low"
    elif price < 0.65:
        return "mid"
    elif price < 0.75:
        return "sweet"
    elif price < 0.85:
        return "premium"
    else:
        return "expensive"


# ─── Price Zone Modifiers (NO-side, from 10K+ Kalshi trades backtest) ──
# Adjusts P(win) relative to mid zone. Higher YES price = cheaper NO = lower WR.
# Kelly sizing already captures the payoff asymmetry through cost basis.

PRICE_ZONE_MODIFIERS = {
    "garbage": 0.55,  # <15¢ YES — K3 kills these anyway
    "cheap": 0.75,  # 30-45¢ YES — extrapolated, no direct data
    "mid_low": 0.90,  # 45-55¢ YES — extrapolated, near mid
    "mid": 1.00,  # 55-65¢ YES → 45.5% NO WR (reference, n=4,401)
    "sweet": 0.83,  # 65-75¢ YES → 37.7% NO WR (n=2,812)
    "premium": 0.66,  # 75-85¢ YES → 30.1% NO WR (n=1,958)
    "expensive": 0.51,  # 85-92¢ YES → 23.3% NO WR (n=1,363)
}

# ─── Empirical NO Win Rates (159K markets: 110K Kalshi + 49K Polymarket) ───
BECKER_NO_WIN_RATES = {
    "daily_updown": 0.463,  # n=887 (Poly only)
    "intraday_updown": 0.504,  # n=15,570 (Poly only — coin flip confirmed)
    "price_above": 0.593,  # n=3,763 (Kalshi 166 + Poly 3,597)
    "price_range": 0.566,  # n=31,982 (Kalshi only — was 0.886 Becker)
    "ai_model": 0.741,  # n=54 (Kalshi 45 + Poly 9)
    "geopolitical": 0.686,  # n=315 (Kalshi 2 + Poly 313)
    "election": 0.639,  # n=794 (Kalshi 557 + Poly 237)
    "sports_winner": 0.567,  # n=27,642 (Kalshi 25,208 + Poly 2,434)
    "sports_single_game": 0.560,  # n=6,309 (Kalshi 1,850 + Poly 4,459)
    "entertainment": 0.711,  # n=114 (Kalshi 68 + Poly 46)
    "deadline_binary": 0.694,  # n=9,062 (Kalshi 5,704 + Poly 3,358)
    "social_count": 0.941,  # n=1,773 (Poly only — NO almost always wins)
    "weather": 0.853,  # n=68 (Kalshi 6 + Poly 62)
    "directional": 0.697,  # n=390 (Poly only)
    "other": 0.638,  # n=36,480 (Kalshi 20,828 @ 66.8% + Poly 15,652 @ 55.8%)
    "parlay": 0.937,  # n=4,251 Kalshi (93.7% NO WR — multi-leg bets almost always fail)
    "financial_price": 0.646,  # n=18,356 Kalshi + 21 Poly (64.6% NO WR)
    "game_total": 0.521,  # n=10,999 Kalshi (52.1% NO WR — near coin flip after fees)
}

# ─── Duration Modifier (empirical: 97K tradeable markets, blended Kalshi+Poly) ──
# Baseline: 61.4% NO WR across all tradeable durations.
# Modifier = bucket NO WR / baseline. Platform gap <9pp on all buckets.
DURATION_MODIFIERS = {
    "daily": 0.94,  # 0-1d: 57.8% NO (n=51,425)
    "short": 1.00,  # 2-3d: 61.7% NO (n=18,030) — baseline
    "weekly": 1.15,  # 4-7d: 70.8% NO (n=13,502) — sweet spot confirmed
    "biweekly": 1.06,  # 8-14d: 65.2% NO (n=10,471)
    "monthly": 1.08,  # 15-30d: 66.1% NO (n=3,614)
    "quarterly": 1.15,  # 31-90d: no data in <=30d filter, keep Becker
    "long": 1.10,  # >90d: no data in <=30d filter, keep Becker
}


def classify_duration(days_to_close: float) -> str:
    """Classify market duration for Becker modifier."""
    if days_to_close <= 1:
        return "daily"
    elif days_to_close <= 3:
        return "short"
    elif days_to_close <= 7:
        return "weekly"
    elif days_to_close <= 14:
        return "biweekly"
    elif days_to_close <= 30:
        return "monthly"
    elif days_to_close <= 90:
        return "quarterly"
    else:
        return "long"


# ─── Calibration Adjustment (per-archetype, from 48 paper + 33 shadow trades) ─
# Blanket calibration was too aggressive — killed profitable weather 63-73% bin.
# Per-archetype approach: cap overconfident bins, preserve what works.
# Updated 2026-03-08. Revisit when sample > 200.

# Archetype confidence caps — max confidence the system will assign.
# Based on actual WR by archetype+confidence bin.
# ── Archetype Confidence Caps (recalibrated Mar 16 from actual performance) ──
# Becker priors were 50-60pp too optimistic vs our actual WR.
# Caps now anchored to actual shadow+paper WR with conservative bias.
# Only weather and social_count have proven profitable.
ARCHETYPE_CONFIDENCE_CAPS = {
    "weather": 0.70,  # Proven: 55.6% WR in sweet spot. Keep.
    "social_count": 0.75,  # 50% WR (n=2), Becker 94.1%. Trust Becker partially.
    "geopolitical": 0.30,  # 12.5% actual WR (n=8). Becker 68.6% is wildly wrong for us.
    "election": 0.25,  # 0% actual WR (n=6). Near-kill.
    "deadline_binary": 0.25,  # 9.1% actual WR (n=11). Becker 69.4% is fantasy.
    "price_above": 0.30,  # 9.1% actual WR (n=11). Becker 59.3% broken.
    "sports_winner": 0.25,  # 0% actual WR (n=8). Already blocked, but defense in depth.
    "sports_single_game": 0.45,  # 100% WR (n=1) — tiny sample. Conservative.
    "sports_tournament": 0.50,  # New archetype (2026-06-20). No shadow data. Moderate cap — 30d shadow eval.
    "entertainment": 0.30,  # 0% actual WR (n=2). Becker 71.1% not realized.
    "ai_model": 0.30,  # 0% actual WR (n=1). Conservative.
    "financial_price": 0.40,  # No data. Conservative.
    "parlay": 0.30,  # Parlays are traps.
    "other": 0.30,  # 0% actual WR (n=8). Was 50%, way too generous.
}


# High-confidence penalty: if raw conf > cap, apply diminishing returns
# instead of hard clamp — preserves signal ordering


_champion_cache = None
_champion_cache_at = 0.0
_CHAMPION_TTL = 300.0  # seconds; promotions propagate within 5 min without restart

def _active_champion():
    """Indirection so tests can monkeypatch; loads champion lazily with a 5-min TTL cache."""
    global _champion_cache, _champion_cache_at
    now = time.monotonic()
    if _champion_cache is None or (now - _champion_cache_at) > _CHAMPION_TTL:
        from signals.calibration_champion import load_champion
        _champion_cache = load_champion()
        _champion_cache_at = now
    return _champion_cache


def apply_calibration(raw_confidence: float, archetype: str = "other") -> float:
    """Apply per-archetype soft cap, then the learned champion calibration map.

    Champion identity/missing -> behavior is exactly the legacy soft-cap (§9C)."""
    from signals.calibration_champion import apply_champion
    cap = ARCHETYPE_CONFIDENCE_CAPS.get(archetype, 0.60)
    if raw_confidence <= cap:
        capped = raw_confidence
    else:
        excess = raw_confidence - cap
        capped = cap + excess * 0.15
    return apply_champion(capped, archetype, champion=_active_champion())


# ─── Kill Rules ──────────────────────────────────────────────────────


def check_kill_rules(title: str, entry_price: float, side: str, signal_archetype: str = None) -> Tuple[bool, str]:
    """Hard reject losing combos. Returns (killed, reason)."""
    archetype = signal_archetype or classify_archetype(title)
    # entry_price here is always the YES market price (0-1)
    price_cents = int(entry_price * 100)

    # K3: Anything below 15¢ — exempt weather, social_count, sports_tournament
    if price_cents < 15 and archetype not in ("weather", "social_count", "sports_tournament"):
        return True, f"K3: entry {price_cents}¢ < 15¢ floor"

    # K1: Intraday up/down — any side (coin flip minus fees)
    if archetype == "intraday_updown":
        return True, "K1: intraday up/down (50% NO WR, n=15,570 — no edge after fees)"

    # K4: Price range — only kill YES side (57% NO WR, n=31,982)
    if archetype == "price_range" and side == "YES":
        return True, "K4: price_range YES side (43% WR, n=31,982)"
    # price_range NO passes through — 57% NO WR

    # K5: Directional dip/crash longshots
    if archetype == "directional":
        return True, "K5: directional dip/crash bet (70% NO WR but low n=390, unreliable)"

    # K2: price_above + cheap YES
    if archetype == "price_above" and side == "YES" and price_cents < 45:
        return True, "K2: price_above cheap YES (20% WR)"

    # K6: Unknown archetype
    if archetype == "other":
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
        for t in db.execute(
            "SELECT market, side, entry_price, outcome, platform FROM shadow_trades WHERE resolved=1"
        ).fetchall():
            trades.append(
                {
                    "title": t["market"] or "",
                    "side": t["side"] or "?",
                    "price": t["entry_price"] or 0,
                    "won": t["side"] == t["outcome"],
                    "platform": t["platform"] or "unknown",
                }
            )

        # Paper trades
        for t in db.execute(
            "SELECT market_title, side, entry_price, status, platform FROM paper_positions WHERE status IN ('won','lost')"
        ).fetchall():
            trades.append(
                {
                    "title": t["market_title"] or "",
                    "side": t["side"],
                    "price": t["entry_price"],
                    "won": t["status"] == "won",
                    "platform": t["platform"] or "unknown",
                }
            )

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
    override_archetype: str = None,
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

    archetype = override_archetype or classify_archetype(title)
    zone = price_zone(entry_price)
    zone_mod = PRICE_ZONE_MODIFIERS.get(zone, 1.0)

    # Kill rule check
    killed, kill_reason = check_kill_rules(title, entry_price, side, signal_archetype=archetype)
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
        prior_weight = 5  # Balanced
    elif total_resolved < 300:
        prior_weight = 3  # Data-driven
    else:
        prior_weight = 1  # Empirical

    # Look up archetype|side WR
    key = f"{archetype}|{side}"
    bucket = wr_table.get(key, {"wins": 0, "total": 0, "wr": 0.5})
    base_wr = bucket["wr"]
    n = bucket["total"]

    # Also check the more specific archetype|side|zone bucket
    zone_key = f"{archetype}|{side}|{zone}"
    zone_trades = [
        t
        for t in trades
        if classify_archetype(t["title"]) == archetype and t["side"] == side and price_zone(t["price"]) == zone
    ]
    zone_n = len(zone_trades)
    zone_wr = sum(t["won"] for t in zone_trades) / zone_n if zone_n > 0 else base_wr

    # Archetype-level prior (all sides combined)
    # Fall back to empirical priors from 159K resolved markets when no local data
    becker_prior = BECKER_NO_WIN_RATES.get(archetype, 0.593)
    arch_trades = [t for t in trades if classify_archetype(t["title"]) == archetype]
    arch_wr = sum(t["won"] for t in arch_trades) / len(arch_trades) if arch_trades else becker_prior

    # Overall system prior
    overall_wr = sum(t["won"] for t in trades) / len(trades) if trades else 0.593

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

    # Apply per-archetype calibration cap (soft ceiling on overconfidence)
    raw_confidence = confidence
    confidence = apply_calibration(confidence, archetype=archetype)

    # Cap at 85% — nothing is certain (tightened from 92% on Mar 16 audit)
    confidence = min(0.85, max(0.08, confidence))

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
            "raw_confidence": round(raw_confidence, 4),
            "calibration_applied": round(raw_confidence - confidence, 4) if raw_confidence != confidence else 0,
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


def calibrated_confidence_oos(title: str, side: str, price: float, train_trades: list) -> float:
    """OOS-safe confidence in [0,100]. Fit ONLY on train_trades (data strictly
    earlier than the trade being scored). Shrinks to the bot's own realized rate."""
    archetype = classify_archetype(title)
    wr_table = _compute_wr_table(train_trades)

    key = f"{archetype}|{side}"
    bucket = wr_table.get(key, {"wins": 0, "total": 0, "wr": 0.5})
    base_wr, n = bucket["wr"], bucket["total"]

    arch_trades = [t for t in train_trades if classify_archetype(t["title"]) == archetype]
    own_arch_n = len(arch_trades)
    own_arch_wr = (sum(int(t["won"]) for t in arch_trades) / own_arch_n) if own_arch_n else None

    # Prefer the bot's OWN realized archetype rate; borrow Becker only when starved (<10)
    if own_arch_n >= 10:
        prior = own_arch_wr
    else:
        prior = BECKER_NO_WIN_RATES.get(archetype, 0.55)

    # Shrink the bucket toward the prior; weight prior heavily when n is small
    prior_weight = 10 if n < 30 else (5 if n < 100 else 3)
    wr = bayesian_smooth(prior, base_wr, n, prior_weight=prior_weight)
    return round(max(0.0, min(1.0, wr)) * 100, 1)


# ─── Calibration Audit ───────────────────────────────────────────────


def calibration_audit() -> Dict:
    """Check if predicted confidence matches actual win rates — measured OOS.

    Splits resolved trades chronologically (70/30 positional cut, since DB trade
    dicts lack resolved_at). Scores the held-out test slice with
    calibrated_confidence_oos(train) — zero in-sample look-ahead.
    Falls back to in-sample when the test slice has fewer than 8 trades.

    API contract (keys preserved for dashboard consumers):
      total_trades          -- number of test-slice trades scored
      avg_calibration_error -- ECE * 100, rounded to 1 dp (legacy scale)
      calibrated            -- bool, True when ECE <= 0.10
      buckets               -- list of per-confidence-decile dicts (legacy shape)

    New additive keys (won't break existing callers):
      oos_ece, n_train, n_test, mode
    """
    from signals.calibration_core import expected_calibration_error

    all_trades = _load_resolved_trades()
    if not all_trades:
        return {"error": "No resolved trades", "buckets": []}

    # Positional 70/30 split (DB rows arrive in chronological insertion order)
    cut = int(len(all_trades) * 0.7)
    train = all_trades[:cut]
    test = all_trades[cut:]
    mode = "oos"

    if len(test) < 8:
        train = all_trades
        test = all_trades
        mode = "in_sample_fallback"

    results = []
    for t in test:
        if mode == "oos":
            conf = calibrated_confidence_oos(t["title"], t["side"], t["price"], train) / 100.0
        else:
            ec = calculate_empirical_confidence(t["title"], t["side"], t["price"])
            if ec["killed"]:
                continue
            conf = ec["confidence"]
        results.append({"confidence": conf, "won": int(t["won"])})

    if not results:
        return {"total_trades": 0, "avg_calibration_error": 0.0, "calibrated": False, "buckets": []}

    preds = [r["confidence"] for r in results]
    outcomes = [r["won"] for r in results]
    oos_ece = expected_calibration_error(preds, outcomes)

    # Per-decile buckets (legacy shape preserved)
    buckets = []
    for lo_pct in range(0, 100, 10):
        lo = lo_pct / 100
        hi = (lo_pct + 10) / 100
        bucket = [r for r in results if lo <= r["confidence"] < hi]
        if bucket:
            predicted = sum(r["confidence"] for r in bucket) / len(bucket)
            actual = sum(r["won"] for r in bucket) / len(bucket)
            miscalibration = actual - predicted
            buckets.append(
                {
                    "range": f"{lo_pct}-{lo_pct + 10}%",
                    "predicted_wr": round(predicted * 100, 1),
                    "actual_wr": round(actual * 100, 1),
                    "miscalibration": round(miscalibration * 100, 1),
                    "n": len(bucket),
                    "calibrated": abs(miscalibration) < 0.10,
                }
            )

    avg_cal_error = round(oos_ece * 100, 1)
    return {
        # Legacy keys (API contract)
        "total_trades": len(results),
        "avg_calibration_error": avg_cal_error,
        "calibrated": oos_ece <= 0.10,
        "buckets": buckets,
        # New additive keys
        "oos_ece": oos_ece,
        "n_train": len(train),
        "n_test": len(test),
        "mode": mode,
    }

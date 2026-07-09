"""Volume-Synchronized Probability of Informed Trading for Polymarket CLOB data.
Adapted from Easley, Lopez de Prado, O'Hara (2012).
Implements the Andersen-Bondarenko validation critique.

Definitions:
  VPIN = mean(|V_buy - V_sell| / V_bar) for N equal-volume bars.
  High VPIN (>0.7) suggests elevated informed trading probability.

Validation gate (Andersen & Bondarenko 2014):
  VPIN is mechanically correlated with volume. If backtested directional
  accuracy for VPIN > 0.7 events is ≤50%, VPIN is downgraded to informational
  metric only — never used as a trade signal.

Architecture:
  - compute_vpin(): Core computation for one token_id
  - scan_top_markets_vpin(): Scans top liquidity markets
  - backtest_vpin_accuracy(): Validates VPIN predictive power
  - Ensurestorage in vpin_snapshots.db for backtesting
"""

import json
from collections import Counter
import logging
import math
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from db import connect as db_connect  # noqa: E402

logger = logging.getLogger("vpin")

# Constants
GAMMA_API = "https://gamma-api.polymarket.com"
VPIN_DB_PATH = PROJECT_ROOT / "storage" / "vpin_snapshots.db"
DEFAULT_N_BARS = 50
DEFAULT_MIN_TRADES = 200
VPIN_HIGH_THRESHOLD = 0.7
VPIN_LOW_THRESHOLD = 0.4
MIN_SNAPSHOTS_FOR_BACKTEST = 100
BACKTEST_MIN_ACCURACY = 55.0  # % — Andersen-Bondarenko gate


# ─── DB Schema ──────────────────────────────────────────────────────────

def _ensure_db():
    """Create vpin_snapshots.db schema if not present."""
    conn = db_connect(str(VPIN_DB_PATH))
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vpin_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                slug TEXT NOT NULL,
                token_id TEXT,
                vpin REAL NOT NULL,
                buy_pct REAL,
                n_trades INTEGER,
                price_at_snap REAL,
                price_1h_later REAL,
                direction_match INTEGER
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_vpin_ts
            ON vpin_snapshots(ts)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_vpin_slug
            ON vpin_snapshots(slug)
        """)
        conn.commit()
    finally:
        conn.close()


def _save_vpin_snapshot(
    slug: str,
    token_id: Optional[str],
    vpin: float,
    buy_pct: float,
    n_trades: int,
    price_at_snap: float,
):
    """Store a VPIN snapshot for later backtesting."""
    _ensure_db()
    conn = db_connect(str(VPIN_DB_PATH))
    try:
        now = time.time()
        conn.execute(
            """INSERT INTO vpin_snapshots (ts, slug, token_id, vpin, buy_pct,
               n_trades, price_at_snap)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (now, slug, token_id, vpin, buy_pct, n_trades, price_at_snap),
        )
        conn.commit()
    finally:
        conn.close()


def _load_vpin_snapshots(limit: int = 500) -> list:
    """Load stored VPIN snapshots for backtesting."""
    _ensure_db()
    conn = db_connect(str(VPIN_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM vpin_snapshots ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _update_price_1h_later(slug: str, ts: float, price_1h: float):
    """Backfill price_1h_later and direction_match for a snapshot."""
    _ensure_db()
    conn = db_connect(str(VPIN_DB_PATH))
    try:
        # Find the snapshot closest to ts+1h for this slug
        target_ts = ts + 3600
        row = conn.execute(
            """SELECT id, price_at_snap FROM vpin_snapshots
               WHERE slug = ? AND ts >= ? AND price_1h_later IS NULL
               ORDER BY ts ASC LIMIT 1""",
            (slug, target_ts),
        ).fetchone()
        if row:
            price_old = row[1] or 0.5
            direction_match = 1 if (price_1h > price_old) == (price_1h > price_old) else -1  # corrected below
            # Correct direction_match: does the move direction match VPIN flow?
            # We'll compute this in backtest_vpin_accuracy instead.
            conn.execute(
                "UPDATE vpin_snapshots SET price_1h_later = ? WHERE id = ?",
                (price_1h, row[0]),
            )
            conn.commit()
    finally:
        conn.close()


# ─── Trade Fetching ─────────────────────────────────────────────────────

def _get_recent_trades(token_id: str, limit: int = 500, condition_id: str = "") -> list:
    """Fetch recent trades using the CLOB client.
    Falls back to our direct /trades implementation if available."""
    from odds.polymarket_clob import get_recent_trades as _clob_trades
    return _clob_trades(token_id, limit=limit, condition_id=condition_id)


def _fetch_market_price(slug: str) -> Optional[float]:
    """Fetch current Yes price for a market slug from Gamma API."""
    try:
        url = f"{GAMMA_API}/markets?slug={slug}"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        if not data:
            return None
        prices = data[0].get("outcomePrices", "[]")
        if isinstance(prices, str):
            prices = json.loads(prices)
        if isinstance(prices, list) and len(prices) > 0:
            return float(prices[0])
        return None
    except Exception as e:
        logger.debug("Price fetch error for %s: %s", slug, e)
        return None


def _fetch_liquid_markets(limit: int = 30) -> list:
    """Fetch top liquid active markets from Gamma API."""
    try:
        url = f"{GAMMA_API}/markets?limit={limit}&active=true&closed=false&_sort=liquidityNum&_order=desc"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.error("Failed to fetch liquid markets: %s", e)
        return []


# ─── VPIN Core Computation ──────────────────────────────────────────────

def compute_vpin(token_id: str, n_bars: int = DEFAULT_N_BARS, condition_id: str = "") -> dict:
    """
    Compute VPIN for a single Polymarket CLOB token.

    Algorithm:
      1. Fetch recent trades via authenticated CLOB /trades endpoint.
      2. Sort chronologically.
      3. Partition into N equal-volume bars.
      4. Within each bar, classify cumulative buy/sell volume.
      5. VPIN = mean(|V_buy - V_sell| / V_bar) across all bars.
      6. Classify: high (>0.7), medium (0.4-0.7), low (<0.4).

    Args:
        token_id: CLOB token ID
        n_bars: Number of equal-volume buckets (default 50)

    Returns:
        Dict with keys: vpin, buy_pct, n_trades, bar_size, n_bars,
        vpin_class, buy_volume, sell_volume, total_volume
        Returns error dict on failure.
    """
    trades = _get_recent_trades(token_id, limit=2000, condition_id=condition_id)
    if not trades or len(trades) < DEFAULT_MIN_TRADES:
        return {
            "error": f"Insufficient trades: got {len(trades) if trades else 0}, need {DEFAULT_MIN_TRADES}",
            "vpin": None, "n_trades": len(trades) if trades else 0,
        }

    # Sort by timestamp ascending
    trades.sort(key=lambda t: t.get("timestamp", 0))

    total_volume = sum(t["size"] for t in trades)
    bar_size = total_volume / n_bars

    # Bucket trades into equal-volume bars
    bars = []
    current_bar = {"buy": 0.0, "sell": 0.0}
    current_vol = 0.0

    for t in trades:
        side = t.get("side", "BUY").upper()
        size = t["size"]
        if size <= 0:
            continue

        # If adding this trade would exceed bar_size, split proportionally
        remaining = bar_size - current_vol

        if size <= remaining:
            # Trade fits entirely in current bar
            if side == "BUY":
                current_bar["buy"] += size
            else:
                current_bar["sell"] += size
            current_vol += size
        else:
            # Partial fill to complete current bar
            if side == "BUY":
                current_bar["buy"] += remaining
            else:
                current_bar["sell"] += remaining
            current_vol += remaining

            # Finalize this bar
            bars.append(current_bar)

            # Remaining trade size starts the next bar
            leftover = size - remaining
            current_bar = {"buy": 0.0, "sell": 0.0}
            current_vol = 0.0

            # Distribute leftover across new bars if it spans multiple buckets
            while leftover > bar_size:
                if side == "BUY":
                    current_bar["buy"] = bar_size
                else:
                    current_bar["sell"] = bar_size
                bars.append(current_bar)
                leftover -= bar_size
                current_bar = {"buy": 0.0, "sell": 0.0}
                current_vol = 0.0

            if leftover > 0:
                if side == "BUY":
                    current_bar["buy"] = leftover
                else:
                    current_bar["sell"] = leftover
                current_vol = leftover

    # Push the last partial bar if it's meaningful (>10% of bar_size)
    if current_vol > bar_size * 0.1:
        bars.append(current_bar)

    if len(bars) < 3:
        return {
            "error": f"Too few bars constructed ({len(bars)})",
            "vpin": None, "n_trades": len(trades),
        }

    # Compute VPIN: mean imbalance across all bars
    imbalances = []
    total_buy = 0.0
    total_sell = 0.0

    for bar in bars:
        v_buy = bar["buy"]
        v_sell = bar["sell"]
        bar_vol = v_buy + v_sell
        total_buy += v_buy
        total_sell += v_sell

        if bar_vol > 0:
            imbalance = abs(v_buy - v_sell) / bar_vol
            imbalances.append(imbalance)

    vpin = sum(imbalances) / len(imbalances) if imbalances else 0.0

    # Buy percentage
    total_vol_actual = total_buy + total_sell
    buy_pct = (total_buy / total_vol_actual * 100) if total_vol_actual > 0 else 50.0

    # Classification
    if vpin >= VPIN_HIGH_THRESHOLD:
        vpin_class = "high"
    elif vpin >= VPIN_LOW_THRESHOLD:
        vpin_class = "medium"
    else:
        vpin_class = "low"

    return {
        "vpin": round(vpin, 4),
        "buy_pct": round(buy_pct, 1),
        "n_trades": len(trades),
        "bar_size": round(bar_size, 4),
        "n_bars": len(bars),
        "vpin_class": vpin_class,
        "buy_volume": round(total_buy, 2),
        "sell_volume": round(total_sell, 2),
        "total_volume": round(total_vol_actual, 2),
    }


# ─── Market Token Resolution ────────────────────────────────────────────

def _resolve_token_id(market: dict) -> Optional[str]:
    """Extract the YES CLOB token ID from a market dict."""
    clob_token_ids = market.get("clobTokenIds", "[]")
    if isinstance(clob_token_ids, str):
        try:
            clob_token_ids = json.loads(clob_token_ids)
        except (json.JSONDecodeError, TypeError):
            clob_token_ids = []
    if isinstance(clob_token_ids, list) and len(clob_token_ids) > 0:
        return clob_token_ids[0]
    return None


# ─── Top-Market Scan ────────────────────────────────────────────────────

def scan_top_markets_vpin(top_n: int = 20) -> list:
    """
    Scan top-N liquid markets for VPIN.

    Fetches top markets by liquidity from Gamma, computes VPIN for each,
    ranks by VPIN score, and stores snapshots.

    Handles auth failure gracefully (logs warning, skips market).

    Args:
        top_n: Number of markets to scan (default 20)

    Returns:
        List of dicts ranked by VPIN (highest first).
        Each dict: {slug, question, vpin, buy_pct, n_trades,
                     vpin_class, token_id, price}
    """
    markets = _fetch_liquid_markets(limit=top_n + 10)  # Fetch extra to account for misses
    if not markets:
        logger.warning("No liquid markets returned from Gamma")
        return []

    results = []
    snapshot_rows = []
    for m in markets:
        slug = m.get("slug", "")
        question = m.get("question", "")[:80]
        if not slug:
            continue

        token_id = _resolve_token_id(m)
        if not token_id:
            logger.debug("Skipping %s: no token ID", slug)
            continue

        result = compute_vpin(token_id, condition_id=m.get("conditionId", ""))
        if result.get("error"):
            logger.debug("VPIN skip %s: %s", slug, result["error"])
            continue

        price = _fetch_market_price(slug)

        entry = {
            "slug": slug,
            "question": question,
            "token_id": token_id,
            "vpin": result["vpin"],
            "buy_pct": result["buy_pct"],
            "n_trades": result["n_trades"],
            "bar_size": result["bar_size"],
            "n_bars": result["n_bars"],
            "vpin_class": result["vpin_class"],
            "buy_volume": result["buy_volume"],
            "sell_volume": result["sell_volume"],
            "total_volume": result["total_volume"],
            "price": price,
        }
        results.append(entry)

        # Snapshot deferred until after the sanity gate below
        snapshot_rows.append(dict(
            slug=slug,
            token_id=token_id,
            vpin=result["vpin"],
            buy_pct=result["buy_pct"],
            n_trades=result["n_trades"],
            price_at_snap=price or 0.5,
        ))

    # Sanity gate: identical VPIN across most markets = dead input filter
    # (2026-07-08: token_id passed as data-api `market` param was silently
    # ignored, stamping one global-tape VPIN on all 30 markets)
    if len(results) >= 5:
        vpin_counts = Counter(r["vpin"] for r in results)
        top_vpin, top_count = vpin_counts.most_common(1)[0]
        if top_count > len(results) / 2:
            logger.error(
                "VPIN sanity gate: %d/%d markets share identical VPIN=%.4f — "
                "input filter likely dead; skipping snapshot writes",
                top_count, len(results), top_vpin,
            )
            snapshot_rows = []

    for row in snapshot_rows:
        _save_vpin_snapshot(**row)

    # Rank by VPIN descending
    results.sort(key=lambda x: x["vpin"], reverse=True)

    logger.info(
        "VPIN scan: %d/%d markets computed (top VPIN=%.4f %s)",
        len(results), len(markets),
        results[0]["vpin"] if results else 0,
        results[0]["slug"] if results else "",
    )

    return results


# ─── Per-Slug VPIN ──────────────────────────────────────────────────────

def vpin_for_slug(slug: str) -> dict:
    """
    Compute VPIN for a single market slug.

    Resolves slug → token_id → trades → VPIN.

    Args:
        slug: Polymarket event slug

    Returns:
        Dict with VPIN computation result and market info.
        Includes slug, question, price in addition to VPIN fields.
    """
    # Fetch market info
    try:
        url = f"{GAMMA_API}/markets?slug={slug}"
        req = urllib.request.Request(url, headers={"User-Agent": "Polyclawd/2.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            markets = json.loads(resp.read().decode())
    except Exception as e:
        return {"error": f"Failed to fetch market: {e}", "slug": slug}

    if not markets:
        return {"error": f"Market not found: {slug}", "slug": slug}

    m = markets[0]
    token_id = _resolve_token_id(m)
    if not token_id:
        return {"error": f"No token ID for {slug}", "slug": slug}

    result = compute_vpin(token_id, condition_id=m.get("conditionId", ""))
    price = _fetch_market_price(slug)

    result["slug"] = slug
    result["question"] = m.get("question", "")[:80]
    result["token_id"] = token_id
    result["price"] = price

    return result


# ─── Backtest: Andersen-Bondarenko Validation ───────────────────────────

def backtest_vpin_accuracy(vpin_snapshots: Optional[list] = None) -> dict:
    """
    Validate VPIN predictive power: for VPIN > 0.7 events, does price
    move in the direction indicated by VPIN flow direction within 1 hour?

    Implements the Andersen & Bondarenko (2014) critique: VPIN is
    mechanically correlated with volume. If directional accuracy ≤ 50%,
    VPIN is informational only.

    Direction match logic:
      - buy_pct > 55% (net buying pressure) + VPIN > 0.7 → expect YES price ↑
      - buy_pct < 45% (net selling pressure) + VPIN > 0.7 → expect YES price ↓
      - We check subsequent price move direction against expectation.

    Args:
        vpin_snapshots: List of stored snapshots. If None, loads from DB.

    Returns:
        Dict with accuracy stats:
          - total_events: number of VPIN > 0.7 events with 1h price data
          - correct: count where direction matched
          - accuracy: percentage
          - pass_gate: True if accuracy > BACKTEST_MIN_ACCURACY
          - medium_events: same for VPIN 0.4-0.7
          - verdict: "SIGNAL" | "INFORMATIONAL_ONLY" | "INSUFFICIENT_DATA"
    """
    if vpin_snapshots is None:
        snapshots = _load_vpin_snapshots(limit=2000)
    else:
        snapshots = vpin_snapshots

    # Filter to snapshots that have 1h price data
    high_events = []
    medium_events = []

    for s in snapshots:
        vpin = s.get("vpin", 0)
        buy_pct = s.get("buy_pct", 50)
        price_at = s.get("price_at_snap")
        price_1h = s.get("price_1h_later")

        if price_at is None or price_1h is None:
            continue

        # Determine expected direction from VPIN flow
        # buy_pct > 55% + high VPIN → informed buying → expect price ↑
        # buy_pct < 45% + high VPIN → informed selling → expect price ↓
        price_move = price_1h - price_at
        if buy_pct > 55:
            expected_up = True
        elif buy_pct < 45:
            expected_up = False
        else:
            # Neutral buy/split — skip direction test
            continue

        direction_match = 1 if (price_move > 0) == expected_up else 0

        if vpin >= VPIN_HIGH_THRESHOLD:
            high_events.append(direction_match)
        elif vpin >= VPIN_LOW_THRESHOLD:
            medium_events.append(direction_match)

    results = {}

    # High VPIN events (>0.7)
    n_high = len(high_events)
    if n_high >= 5:
        correct_high = sum(high_events)
        acc_high = correct_high / n_high * 100
        results["high_vpin"] = {
            "total": n_high,
            "correct": correct_high,
            "accuracy": round(acc_high, 1),
        }
    else:
        results["high_vpin"] = {"total": n_high, "note": "Insufficient data (need 5+)", "accuracy": None}

    # Medium VPIN events (0.4-0.7)
    n_med = len(medium_events)
    if n_med >= 5:
        correct_med = sum(medium_events)
        acc_med = correct_med / n_med * 100
        results["medium_vpin"] = {
            "total": n_med,
            "correct": correct_med,
            "accuracy": round(acc_med, 1),
        }
    else:
        results["medium_vpin"] = {"total": n_med, "note": "Insufficient data (need 5+)", "accuracy": None}

    # Overall assessment
    total_events = n_high + n_med
    total_snapshots = len(snapshots)

    high_acc = results.get("high_vpin", {}).get("accuracy")

    if high_acc is not None and n_high >= 5:
        if high_acc > BACKTEST_MIN_ACCURACY:
            verdict = "SIGNAL"
        elif high_acc <= 50:
            verdict = "INFORMATIONAL_ONLY"
        else:
            verdict = "SIGNAL" if high_acc >= BACKTEST_MIN_ACCURACY else "INFORMATIONAL_ONLY"
    elif total_snapshots >= MIN_SNAPSHOTS_FOR_BACKTEST:
        # Enough snapshots collected but no high-VPIN events resolved yet
        # This means VPIN is never reaching >0.7
        verdict = "INSUFFICIENT_HIGH_EVENTS"
    else:
        verdict = "INSUFFICIENT_DATA"

    results["verdict"] = verdict
    results["total_snapshots"] = total_snapshots
    results["total_events_with_price"] = total_events
    results["backtest_min_accuracy"] = BACKTEST_MIN_ACCURACY
    results["gate_passed"] = verdict == "SIGNAL"

    # Summary text
    if verdict == "SIGNAL":
        results["summary"] = (
            f"VPIN validated as trade signal: {high_acc:.1f}% accuracy on {n_high} high-VPIN events"
        )
    elif verdict == "INFORMATIONAL_ONLY":
        results["summary"] = (
            f"VPIN accuracy {high_acc:.1f}% — below {BACKTEST_MIN_ACCURACY}% gate. "
            f"Demoted to informational metric per Andersen-Bondarenko critique."
        )
    elif verdict == "INSUFFICIENT_HIGH_EVENTS":
        results["summary"] = (
            f"{total_snapshots} snapshots collected but no high-VPIN (>0.7) resolved events. "
            f"VPIN values are consistently below threshold."
        )
    else:
        results["summary"] = (
            f"Collecting data: {total_snapshots}/{MIN_SNAPSHOTS_FOR_BACKTEST} snapshots. "
            f"Need {max(0, 5 - n_high)} more high-VPIN events with 1h price data."
        )

    return results


# ─── Standalone Runner ──────────────────────────────────────────────────

def run_scan(verbose: bool = False) -> dict:
    """Run a full VPIN scan and return results."""
    results = scan_top_markets_vpin(top_n=20)
    accuracy = backtest_vpin_accuracy()
    return {
        "scan_results": results,
        "accuracy": accuracy,
        "scan_time": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import pprint

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    print("=" * 60)
    print("VPIN Scan Runner")
    print("=" * 60)

    # Check env
    if not os.environ.get("POLYMARKET_CLOB_API_KEY"):
        print("WARNING: POLYMARKET_CLOB_API_KEY not set — trades fetch will fail")
    if not os.environ.get("POLYMARKET_CLOB_SECRET"):
        print("WARNING: POLYMARKET_CLOB_SECRET not set — trades fetch will fail")

    result = run_scan(verbose=True)
    print("\nScan results:")
    for r in result.get("scan_results", [])[:5]:
        print(f"  {r['slug']:40s} VPIN={r['vpin']:.4f}  class={r['vpin_class']}  "
              f"buy={r['buy_pct']:.1f}%  trades={r['n_trades']}")

    print("\nBacktest accuracy:")
    acc = result.get("accuracy", {})
    print(f"  Verdict: {acc.get('verdict', 'N/A')}")
    print(f"  Snapshots: {acc.get('total_snapshots', 0)}")
    high = acc.get("high_vpin", {})
    if high.get("accuracy"):
        print(f"  High VPIN: {high['accuracy']}% ({high['correct']}/{high['total']})")
    med = acc.get("medium_vpin", {})
    if med.get("accuracy"):
        print(f"  Medium VPIN: {med['accuracy']}% ({med['correct']}/{med['total']})")
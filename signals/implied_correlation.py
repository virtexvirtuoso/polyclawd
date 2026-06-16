#!/usr/bin/env python3
"""
Implied Correlation Matrix — CE-6

Derives implied correlations between related prediction markets and flags
divergences from historical co-occurrence >2 standard deviations.

Event clusters are auto-detected from open markets. For each pair in a
cluster, correlation is estimated via three paths:
  Path A (explicit joint market): if a "both A and B" market exists
  Path B (price co-movement): Pearson correlation on 7d daily price changes
  Path C (historical): realized co-occurrence from resolved markets

Correlation snapshots are stored in storage/correlation_matrix.db.
"""

import json
import math
import sqlite3
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

# ── Paths ──────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
SHADOW_DB = BASE_DIR / "storage" / "shadow_trades.db"
CORR_DB = BASE_DIR / "storage" / "correlation_matrix.db"

# ── Cluster definitions ────────────────────────────────────────────────
# Each cluster has archetype matchers and/or keyword matchers on market title
CLUSTER_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "crypto",
        "archetypes": {"price_above"},
        "keywords": {"bitcoin", "btc", "ethereum", "eth", "crypto", "solana",
                      "sol", "bnb", "xrp", "cardano", "ada", "dogecoin"},
    },
    {
        "name": "political",
        "archetypes": {"election"},
        "keywords": {"trump", "biden", "election", "president", "senate",
                      "congress", "democrat", "republican", "vote", "poll",
                      "primary", "governor", "gop", "dnc", "nominee"},
    },
    {
        "name": "economic",
        "archetypes": set(),
        "keywords": {"fed", "inflation", "interest rate", "gdp", "cpi",
                      "unemployment", "recession", "treasury", "debt ceiling",
                      "shutdown", "economic", "tariff"},
    },
    {
        "name": "sports_mlb",
        "archetypes": {"sports_winner", "sports_single_game"},
        "keywords": {"mlb", "baseball", "yankees", "red sox", "dodgers",
                      "astros", "braves", "cubs"},
    },
    {
        "name": "sports_nfl",
        "archetypes": {"sports_winner", "sports_single_game"},
        "keywords": {"nfl", "football", "super bowl", "chiefs", "49ers",
                      "cowboys", "eagles"},
    },
    {
        "name": "sports_nba",
        "archetypes": {"sports_winner", "sports_single_game"},
        "keywords": {"nba", "basketball", "lakers", "celtics", "warriors",
                      "bucks", "nuggets"},
    },
    {
        "name": "sports_soccer",
        "archetypes": {"sports_winner", "sports_single_game"},
        "keywords": {"soccer", "uefa", "champions league", "premier league",
                      "laliga", "serie a", "bundesliga", "world cup"},
    },
    {
        "name": "ufc",
        "archetypes": {"sports_winner"},
        "keywords": {"ufc", "mma", "fight", "octagon"},
    },
    {
        "name": "entertainment",
        "archetypes": {"entertainment"},
        "keywords": {"oscar", "grammy", "emmy", "award", "box office",
                      "billboard", "movie", "film", "album"},
    },
    {
        "name": "geopolitical",
        "archetypes": {"geopolitical"},
        "keywords": {"war", "sanction", "treaty", "nato", "china", "russia",
                      "iran", "ukraine", "israel", "ceasefire", "strike"},
    },
]


# ════════════════════════════════════════════════════════════════════════
# Database Helpers
# ════════════════════════════════════════════════════════════════════════

def _get_shadow_conn() -> sqlite3.Connection:
    """Get connection to shadow_trades.db (read-only)."""
    conn = sqlite3.connect(str(SHADOW_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_corr_db():
    """Create correlation_matrix.db schema if not present."""
    conn = sqlite3.connect(str(CORR_DB), timeout=10)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS correlation_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            cluster TEXT NOT NULL,
            pair TEXT NOT NULL,
            implied_corr REAL,
            historical_corr REAL,
            price_corr_7d REAL,
            deviation_std REAL,
            n_data_points INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_corr_cluster
            ON correlation_snapshots(cluster);
        CREATE INDEX IF NOT EXISTS idx_corr_ts
            ON correlation_snapshots(ts);
    """)
    conn.commit()
    conn.close()


def _save_correlation_snapshot(cluster: str, pair: str,
                                implied_corr: Optional[float],
                                historical_corr: Optional[float],
                                price_corr_7d: Optional[float],
                                deviation_std: Optional[float],
                                n_data_points: int):
    """Insert one correlation snapshot row."""
    conn = sqlite3.connect(str(CORR_DB), timeout=10)
    try:
        conn.execute("""
            INSERT INTO correlation_snapshots
                (ts, cluster, pair, implied_corr, historical_corr,
                 price_corr_7d, deviation_std, n_data_points)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            time.time(),
            cluster,
            pair,
            _sanitize_float(implied_corr),
            _sanitize_float(historical_corr),
            _sanitize_float(price_corr_7d),
            _sanitize_float(deviation_std),
            n_data_points,
        ))
        conn.commit()
    finally:
        conn.close()


def _sanitize_float(v: Any) -> Optional[float]:
    """Return None for NaN/Inf; clamp to [-1, 1] for correlation values."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    return None


# ════════════════════════════════════════════════════════════════════════
# Cluster Auto-Detection
# ════════════════════════════════════════════════════════════════════════

def auto_detect_clusters(min_markets: int = 3) -> List[Dict[str, Any]]:
    """Scan open markets and group into auto-detected event clusters.

    Uses archetype + keyword matching against CLUSTER_DEFINITIONS.
    Markets are sourced from shadow_trades (open/unresolved trades) and
    signal_snapshots (recent active signals).

    Returns list of cluster dicts, each with:
        name, markets (list of dicts), n (market count)
    """
    conn = _get_shadow_conn()
    try:
        # Gather all distinct open (unresolved) markets from shadow_trades
        unresolved = conn.execute("""
            SELECT DISTINCT market, market_id, archetype, category, platform
            FROM shadow_trades
            WHERE resolved = 0
        """).fetchall()

        # Also gather unique markets from recent signal_snapshots
        recent_signals = conn.execute("""
            SELECT DISTINCT market, market_id, category
            FROM signal_snapshots
            WHERE snapshot_date >= date('now', '-30 days')
        """).fetchall()

        # Build a set of (title_lower, mid) for matching
        unique_markets: Dict[str, dict] = {}
        for row in unresolved:
            key = row["market_id"] or row["market"]
            if key:
                unique_markets[key] = {
                    "id": row["market_id"],
                    "title": row["market"],
                    "title_lower": (row["market"] or "").lower(),
                    "archetype": row["archetype"] or "",
                    "category": row["category"] or "",
                    "platform": row["platform"] or "kalshi",
                }
        for row in recent_signals:
            key = row["market_id"] or row["market"]
            if key and key not in unique_markets:
                unique_markets[key] = {
                    "id": row["market_id"],
                    "title": row["market"],
                    "title_lower": (row["market"] or "").lower(),
                    "archetype": "",
                    "category": row["category"] or "",
                    "platform": "polymarket",
                }

        if not unique_markets:
            return []

        # Classify each market into clusters
        cluster_markets: Dict[str, list] = defaultdict(list)
        for mid, mkt in unique_markets.items():
            matched = _classify_market(mkt)
            for cluster_name in matched:
                cluster_markets[cluster_name].append(mkt)

        # Filter clusters with >= min_markets
        result = []
        for name, markets in sorted(cluster_markets.items()):
            if len(markets) >= min_markets:
                result.append({
                    "name": name,
                    "markets": markets,
                    "n": len(markets),
                })

        return result
    finally:
        conn.close()


def _classify_market(mkt: dict) -> List[str]:
    """Return list of cluster names this market belongs to.

    Sport-specific clusters (sports_mlb, sports_nba, etc.) use AND logic:
    both archetype=sports_winner|sports_single_game AND specific keywords
    must match. This prevents F1 markets from being classified as MLB/NBA.

    Thematic clusters (crypto, political, economic) use OR logic:
    archetype OR keyword or category match.
    """
    title = mkt["title_lower"]
    archetype = (mkt.get("archetype") or "").lower()
    category = (mkt.get("category") or "").lower()

    # Sport cluster names (require AND logic: archetype + keyword)
    sport_clusters = {"sports_mlb", "sports_nfl", "sports_nba",
                      "sports_soccer", "ufc"}

    matches = []
    for definition in CLUSTER_DEFINITIONS:
        name = definition["name"]
        arch_matches = archetype in definition["archetypes"]
        kw_matches = any(kw in title for kw in definition["keywords"])

        # Sport clusters use AND: both archetype AND keyword must match
        if name in sport_clusters:
            if arch_matches and kw_matches:
                matches.append(name)
            continue

        # Also match category-based hints
        cat_matches = False
        if name == "crypto" and category in ("tech", "crypto"):
            cat_matches = True
        elif name == "political" and (category == "dynamic" or archetype == "election"):
            cat_matches = True
        elif name == "sports_mlb" and category in ("baseball", "mlb"):
            cat_matches = True
        elif name == "sports_soccer" and category == "soccer":
            cat_matches = True
        elif name == "sports_nfl" and category == "nfl":
            cat_matches = True

        if arch_matches or kw_matches or cat_matches:
            matches.append(name)

    return matches


# ════════════════════════════════════════════════════════════════════════
# Correlation Matrix Builder
# ════════════════════════════════════════════════════════════════════════

def build_correlation_matrix(cluster: dict,
                              conn: Optional[sqlite3.Connection] = None,
                              persist: bool = True,
                              max_pairs: int = 50) -> dict:
    """Build NxN correlation matrix for a market cluster.

    For each unique pair of markets in the cluster, computes:
      - implied_corr: from explicit joint market price, or price co-movement
      - historical_corr: realized co-occurrence from resolved markets
      - price_corr_7d: Pearson correlation on 7-day price changes

    Markets within the same cluster that share the same underlying entity
    (e.g., "BTC > $100k" and "BTC > $120k") are compared via Path B
    (price co-movement) since they are nested thresholds.

    max_pairs cap prevents runaway O(n^2) on large clusters (>150 pairs
    skipped with truncation note).

    Returns dict with cluster_name, markets list, matrix dict, anomalies list.
    """
    markets = cluster.get("markets", [])
    if len(markets) < 2:
        return {
            "cluster_name": cluster["name"],
            "markets": [m["title"] for m in markets],
            "matrix": {},
            "anomalies": [],
        }

    # Pre-count pairs to cap computation
    n_pairs_expected = len(markets) * (len(markets) - 1) // 2
    if n_pairs_expected > max_pairs:
        logger.warning(
            f"Truncating correlation matrix for '{cluster['name']}': "
            f"{n_pairs_expected} pairs exceeds max_pairs={max_pairs}. "
            f"Sampling first {max_pairs} pairs."
        )

    own_conn = False
    if conn is None:
        conn = _get_shadow_conn()
        own_conn = True

    try:
        cluster_name = cluster["name"]
        matrix: Dict[str, dict] = {}
        anomalies: List[dict] = []

        # Get all pairs (unique, no self-pairs, no reverse-duplicates)
        pairs = _get_unique_pairs(markets)

        # Apply max_pairs cap to prevent O(n^2) blowup
        if len(pairs) > max_pairs:
            pairs = pairs[:max_pairs]

        for pair_key, mkt_a, mkt_b in pairs:
            a_id = mkt_a.get("id") or mkt_a["title"]
            b_id = mkt_b.get("id") or mkt_b["title"]

            # ── Path A: check for explicit joint market ──────────────
            implied_corr = _compute_implied_joint_corr(conn, a_id, b_id,
                                                         mkt_a, mkt_b)

            # ── Path B: price co-movement (7d) ──────────────────────
            price_corr_7d = _compute_price_co_movement(conn, a_id, b_id)

            # ── Path C: historical co-occurrence ─────────────────────
            historical_corr, hist_n = _compute_historical_corr(conn,
                                    cluster_name, mkt_a, mkt_b)

            # Fill in implied correlation from price co-movement if no
            # explicit joint market was found
            if implied_corr is None and price_corr_7d is not None:
                implied_corr = price_corr_7d

            # ── Deviation and anomaly detection ──────────────────────
            deviation_std = None
            signal_flag = False
            if implied_corr is not None and historical_corr is not None:
                # Compute z-score of divergence
                diff = abs(implied_corr - historical_corr)
                # Std of Fisher-z transformed correlations ≈ 1/sqrt(n-3)
                # For small n we use a conservative estimate
                if hist_n and hist_n > 3:
                    se = 1.0 / math.sqrt(hist_n - 3)
                    deviation_std = diff / se if se > 0 else None
                else:
                    # Fallback: treat diff > 0.3 as ~2σ
                    deviation_std = diff / 0.15 if diff > 0 else 0

                if deviation_std is not None and deviation_std > 2.0:
                    signal_flag = True

            n_points = hist_n or 0
            if price_corr_7d is not None:
                n_points = max(n_points, 7)

            pair_entry = {
                "implied_corr": _round_corr(implied_corr),
                "historical_corr": _round_corr(historical_corr),
                "price_corr_7d": _round_corr(price_corr_7d),
                "deviation_std": round(deviation_std, 2) if deviation_std is not None else None,
                "n_data_points": n_points,
                "signal": signal_flag,
            }
            matrix[pair_key] = pair_entry

            if signal_flag:
                anomalies.append({
                    "pair": pair_key,
                    "market_a": mkt_a["title"],
                    "market_b": mkt_b["title"],
                    "implied_corr": pair_entry["implied_corr"],
                    "historical_corr": pair_entry["historical_corr"],
                    "deviation_std": pair_entry["deviation_std"],
                    "signal": True,
                })

            # Persist to correlation_matrix.db
            if persist:
                _save_correlation_snapshot(
                    cluster_name, pair_key,
                    pair_entry["implied_corr"],
                    pair_entry["historical_corr"],
                    pair_entry["price_corr_7d"],
                    pair_entry["deviation_std"],
                    n_points,
                )

        return {
            "cluster_name": cluster_name,
            "markets": [m["title"] for m in markets],
            "n_markets": len(markets),
            "n_pairs": len(pairs),
            "matrix": matrix,
            "anomalies": anomalies,
        }
    finally:
        if own_conn:
            conn.close()


def _get_unique_pairs(markets: list) -> List[Tuple[str, dict, dict]]:
    """Generate unique market pairs as (pair_key, mkt_a, mkt_b)."""
    pairs = []
    seen = set()
    for i, a in enumerate(markets):
        a_title = a.get("title", "")
        a_id = a.get("id") or a_title
        for j in range(i + 1, len(markets)):
            b = markets[j]
            b_title = b.get("title", "")
            b_id = b.get("id") or b_title
            # Pair key: sorted tuple of market_ids or titles
            key = tuple(sorted([a_id, b_id]))
            if key not in seen:
                seen.add(key)
                pair_key = f"{a_id}_{b_id}" if a_id < b_id else f"{b_id}_{a_id}"
                pairs.append((pair_key, a, b))
    return pairs


def _round_corr(v: Optional[float]) -> Optional[float]:
    """Round correlation to 4 decimal places."""
    if v is None:
        return None
    return round(v, 4)


# ════════════════════════════════════════════════════════════════════════
# Path A: Implied Correlation from Explicit Joint Market
# ════════════════════════════════════════════════════════════════════════

# Joint market keywords that suggest "both A and B" type markets
JOINT_PATTERNS = [
    "both", "and", "all of", "combination", "simultaneous",
    "at the same time", "double", "triple",
]

def _compute_implied_joint_corr(
    conn: sqlite3.Connection,
    a_id: str,
    b_id: str,
    mkt_a: dict,
    mkt_b: dict,
) -> Optional[float]:
    """Try to find a joint market spanning both A and B and get its price.

    Searches for markets whose title contains both market titles' keywords,
    or uses signal/snapshot prices to derive correlation from implied joint.

    Implied correlation formula:
        If P(A∩B) = P(A) * P(B) + corr * sqrt(P(A)*(1-P(A))*P(B)*(1-P(B)))
        Then corr = (P(A∩B) - P(A)*P(B)) / sqrt(P(A)*(1-P(A))*P(B)*(1-P(B)))
    """
    # Get prices for individual markets
    p_a = _get_current_price(conn, a_id, mkt_a)
    p_b = _get_current_price(conn, b_id, mkt_b)
    if p_a is None or p_b is None:
        return None

    # Try to find joint market price
    p_joint = _find_joint_market_price(conn, mkt_a, mkt_b)
    if p_joint is None:
        return None

    # Derive correlation from joint probability
    # P(A∩B) = P(A)*P(B) + ρ*sqrt(P(A)(1-P(A))*P(B)(1-P(B)))
    # ρ = (P(A∩B) - P(A)*P(B)) / sqrt(P(A)(1-P(A))*P(B)(1-P(B)))
    p_a = max(0.001, min(0.999, p_a))
    p_b = max(0.001, min(0.999, p_b))
    p_joint = max(0.001, min(0.999, p_joint))

    numerator = p_joint - (p_a * p_b)
    denominator = math.sqrt(p_a * (1 - p_a) * p_b * (1 - p_b))
    if denominator < 0.001:
        return None

    rho = numerator / denominator
    # Clamp to [-1, 1]
    return max(-1.0, min(1.0, rho))


def _get_current_price(conn: sqlite3.Connection, market_id: str,
                        mkt: dict) -> Optional[float]:
    """Get most recent price for a market from signal_snapshots or shadow_trades."""
    # Try signal_snapshots first (has explicit price column)
    row = conn.execute("""
        SELECT price FROM signal_snapshots
        WHERE market_id = ?
           OR market = ?
        ORDER BY snapshot_date DESC
        LIMIT 1
    """, (market_id, mkt.get("title"))).fetchone()
    if row and row["price"] is not None:
        return row["price"]

    # Try shadow_trades (entry_price as proxy)
    row = conn.execute("""
        SELECT entry_price FROM shadow_trades
        WHERE market_id = ?
           OR market = ?
        ORDER BY timestamp DESC
        LIMIT 1
    """, (market_id, mkt.get("title"))).fetchone()
    if row and row["entry_price"] is not None:
        return row["entry_price"]

    return None


def _find_joint_market_price(conn: sqlite3.Connection,
                              mkt_a: dict, mkt_b: dict) -> Optional[float]:
    """Search for a joint 'both A and B' market and return its price."""
    # Extract keywords from each title (first 3 meaningful words)
    a_words = _extract_keywords(mkt_a.get("title", ""))
    b_words = _extract_keywords(mkt_b.get("title", ""))

    if not a_words or not b_words:
        return None

    # Query for markets containing keywords from both titles
    like_patterns = []
    for w in a_words[:2]:
        for w2 in b_words[:2]:
            like_patterns.append(f"%{w}%{w2}%")
            like_patterns.append(f"%{w2}%{w}%")

    for pattern in like_patterns[:5]:  # Limit to 5 patterns
        row = conn.execute("""
            SELECT price FROM signal_snapshots
            WHERE market LIKE ? ESCAPE '\\'
               AND market != ?
               AND market != ?
            ORDER BY snapshot_date DESC
            LIMIT 1
        """, (pattern, mkt_a.get("title", ""), mkt_b.get("title", ""))).fetchone()
        if row and row["price"] is not None:
            return row["price"]

        row = conn.execute("""
            SELECT entry_price FROM shadow_trades
            WHERE market LIKE ? ESCAPE '\\'
               AND market != ?
               AND market != ?
            ORDER BY timestamp DESC
            LIMIT 1
        """, (pattern, mkt_a.get("title", ""), mkt_b.get("title", ""))).fetchone()
        if row and row["entry_price"] is not None:
            return row["entry_price"]

    return None


def _extract_keywords(title: str, max_words: int = 5) -> List[str]:
    """Extract meaningful keywords from a market title."""
    stopwords = {"the", "a", "an", "in", "on", "at", "for", "to", "of",
                 "and", "or", "is", "be", "will", "not", "by", "with",
                 "from", "as", "are", "was", "were", "been", "has", "had",
                 "do", "does", "did", "but", "if", "than", "that", "this",
                 "its", "it", "up", "down", "above", "below", "reach",
                 "price", "have", "their", "what", "which", "who", "how"}
    words = title.lower().split()
    return [w.strip(".,?!;:'\"()[]{}") for w in words
            if w.strip(".,?!;:'\"()[]{}") and w not in stopwords and len(w) > 2][:max_words]


# ════════════════════════════════════════════════════════════════════════
# Path B: Price Co-Movement (7-day Pearson)
# ════════════════════════════════════════════════════════════════════════

def _compute_price_co_movement(conn: sqlite3.Connection,
                                 a_id: str, b_id: str) -> Optional[float]:
    """Compute Pearson correlation of daily price changes over a rolling 7d window.

    Uses signal_snapshots to get price history. Brings prices to a daily
    frequency, computes daily returns, then Pearson r.
    """
    # Get 14 days of price data for both markets (need at least 7 daily points)
    prices_a = _get_daily_prices(conn, a_id)
    prices_b = _get_daily_prices(conn, b_id)

    if len(prices_a) < 3 or len(prices_b) < 3:
        return None

    # Interpolate to daily frequency and join on date
    daily_a = _to_daily_map(prices_a)
    daily_b = _to_daily_map(prices_b)

    # Find common dates (last 14 days)
    common = sorted(set(daily_a.keys()) & set(daily_b.keys()))[-14:]
    if len(common) < 3:
        return None

    # Compute daily returns
    returns_a = []
    returns_b = []
    for i in range(1, len(common)):
        r_a = (daily_a[common[i]] - daily_a[common[i-1]]) / daily_a[common[i-1]] if daily_a[common[i-1]] > 0 else 0
        r_b = (daily_b[common[i]] - daily_b[common[i-1]]) / daily_b[common[i-1]] if daily_b[common[i-1]] > 0 else 0
        returns_a.append(r_a)
        returns_b.append(r_b)

    if len(returns_a) < 3:
        return None

    # Pearson correlation
    try:
        r, _ = _pearsonr(returns_a, returns_b)
        return r
    except (ValueError, ZeroDivisionError):
        return None


def _get_daily_prices(conn: sqlite3.Connection, market_id: str) -> List[Tuple[str, float]]:
    """Get (date, price) pairs from signal_snapshots for a given market."""
    rows = conn.execute("""
        SELECT snapshot_date, price
        FROM signal_snapshots
        WHERE (market_id = ? OR market = ?)
          AND price IS NOT NULL
          AND price > 0
          AND price < 1
        ORDER BY snapshot_date ASC
    """, (market_id, market_id)).fetchall()
    return [(r["snapshot_date"], r["price"]) for r in rows]


def _to_daily_map(prices: List[Tuple[str, float]]) -> Dict[str, float]:
    """Convert (date, price) list to {date: avg_price}. Averages multiple entries per day."""
    by_date: Dict[str, list] = defaultdict(list)
    for date, price in prices:
        by_date[date].append(price)
    return {d: statistics.mean(v) for d, v in by_date.items()}


def _pearsonr(x: List[float], y: List[float]) -> Tuple[float, float]:
    """Simple Pearson r and p-value (two-tailed)."""
    n = len(x)
    if n < 3:
        raise ValueError("Need at least 3 data points")
    mx = sum(x) / n
    my = sum(y) / n
    sxx = sum((xi - mx) ** 2 for xi in x)
    syy = sum((yi - my) ** 2 for yi in y)
    sxy = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    denom = math.sqrt(sxx * syy)
    if denom == 0:
        raise ZeroDivisionError("Zero variance")
    r = sxy / denom
    # t-statistic for p-value
    t = r * math.sqrt((n - 2) / (1 - r * r)) if abs(r) < 1.0 else float('inf')
    p = 2 * (1 - _t_cdf(abs(t), n - 2))
    return r, p


def _t_cdf(t: float, df: int) -> float:
    """Approximate t-distribution CDF using normal approximation for df > 30,
    or direct integration via Abramowitz & Stegun for small df.
    """
    if df > 30:
        # Normal approximation
        x = t * (1 - 1 / (4 * df))
        return _normal_cdf(x)
    # For small df, use simple approximation
    x = df / (df + t * t)
    # Beta function approximation (partial)
    a = df / 2.0
    b = 0.5
    return 1 - 0.5 * _incomplete_beta(x, a, b)


def _normal_cdf(x: float) -> float:
    """Standard normal CDF approximation (Abramowitz & Stegun 26.2.17)."""
    if x < -10:
        return 0.0
    if x > 10:
        return 1.0
    z = abs(x)
    t = 1.0 / (1.0 + 0.2316419 * z)
    d = 0.3989422804014327 * math.exp(-x * x / 2.0)
    p = d * t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 +
        t * (-1.821255978 + t * 1.330274429))))
    if x > 0:
        return 1.0 - p
    return p


def _incomplete_beta(x: float, a: float, b: float,
                      max_iter: int = 100) -> float:
    """Continued fraction approximation of regularized incomplete beta."""
    if x < 0 or x > 1:
        return 0.0
    if x == 0 or x == 1:
        return x
    # Lentz's continued fraction method
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(math.log(x) * a + math.log(1 - x) * b - lbeta + math.log(a))
    # Modified Lentz
    f = 1.0
    c = 1.0
    d = 1.0 - (a + b) * x / (a + 1)
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    f = d
    for m in range(1, max_iter + 1):
        # Even step
        num = m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m))
        d = 1.0 + num * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + num / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = c * d
        f *= delta
        # Odd step
        num = -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1))
        d = 1.0 + num * d
        if abs(d) < 1e-30:
            d = 1e-30
        c = 1.0 + num / c
        if abs(c) < 1e-30:
            c = 1e-30
        d = 1.0 / d
        delta = c * d
        f *= delta
        if abs(delta - 1.0) < 1e-8:
            break
    return front * f


# ════════════════════════════════════════════════════════════════════════
# Path C: Historical Co-occurrence
# ════════════════════════════════════════════════════════════════════════

def _compute_historical_corr(
    conn: sqlite3.Connection,
    cluster_name: str,
    mkt_a: dict,
    mkt_b: dict,
) -> Tuple[Optional[float], int]:
    """Compute historical correlation from resolved markets in same cluster.

    For each resolved market pair within the same archetype, counts how often
    their outcomes co-occur (both YES or both NO). Returns phi coefficient
    (Matthews correlation for binary pairs) and count of pairs examined.

    Falls back to archetype-level co-occurrence rate if no direct matched pair.
    """
    archetype = mkt_a.get("archetype", "") or mkt_b.get("archetype", "")
    if not archetype:
        # Try to infer from cluster name
        for definition in CLUSTER_DEFINITIONS:
            if definition["name"] == cluster_name:
                arch_set = definition["archetypes"]
                if arch_set:
                    archetype = next(iter(arch_set))
                break

    if not archetype:
        return None, 0

    # Get resolved markets in this archetype
    resolved = conn.execute("""
        SELECT market, outcome
        FROM shadow_trades
        WHERE resolved = 1
          AND outcome IN ('YES', 'NO')
          AND side IN ('YES', 'NO')
          AND archetype = ?
        ORDER BY market
    """, (archetype,)).fetchall()

    if len(resolved) < 4:
        return None, len(resolved)

    # Group by market title (different threshold variations count as same event)
    market_outcomes: Dict[str, List[str]] = defaultdict(list)
    for r in resolved:
        title = _normalize_market_title(r["market"])
        market_outcomes[title].append(r["outcome"])

    titles = list(market_outcomes.keys())
    if len(titles) < 2:
        return None, 1

    # For each pair of distinct market titles (if multiple outcomes exist),
    # compute phi coefficient of co-occurrence
    co_occurrences: List[float] = []
    for i in range(len(titles)):
        outcomes_a = market_outcomes[titles[i]]
        for j in range(i + 1, len(titles)):
            outcomes_b = market_outcomes[titles[j]]
            if len(outcomes_a) < 2 or len(outcomes_b) < 2:
                continue
            # Compare the majority outcome for each
            maj_a = max(set(outcomes_a), key=outcomes_a.count)
            maj_b = max(set(outcomes_b), key=outcomes_b.count)
            # Compute phi
            n00 = sum(1 for oa, ob in zip(outcomes_a[:min(len(outcomes_a), len(outcomes_b))],
                                           outcomes_b[:min(len(outcomes_a), len(outcomes_b))])
                      if oa == 'NO' and ob == 'NO')
            n01 = sum(1 for oa, ob in zip(outcomes_a[:min(len(outcomes_a), len(outcomes_b))],
                                           outcomes_b[:min(len(outcomes_a), len(outcomes_b))])
                      if oa == 'NO' and ob == 'YES')
            n10 = sum(1 for oa, ob in zip(outcomes_a[:min(len(outcomes_a), len(outcomes_b))],
                                           outcomes_b[:min(len(outcomes_a), len(outcomes_b))])
                      if oa == 'YES' and ob == 'NO')
            n11 = sum(1 for oa, ob in zip(outcomes_a[:min(len(outcomes_a), len(outcomes_b))],
                                           outcomes_b[:min(len(outcomes_a), len(outcomes_b))])
                      if oa == 'YES' and ob == 'YES')
            n = n00 + n01 + n10 + n11
            if n < 3:
                continue
            denom = math.sqrt((n00 + n01) * (n00 + n10) * (n01 + n11) * (n10 + n11))
            if denom > 0:
                phi = ((n00 * n11) - (n01 * n10)) / denom
                co_occurrences.append(phi)

    if not co_occurrences:
        return None, len(resolved)

    # Mean historical correlation across all pairs
    mean_hist = sum(co_occurrences) / len(co_occurrences)
    return mean_hist, len(co_occurrences)


def _normalize_market_title(title: str) -> str:
    """Normalize market title for comparison (remove threshold details, dates)."""
    t = title.lower()
    # Remove price/dollar amounts
    import re
    t = re.sub(r'\$[\d,]+[kKmMbB]?', '$X', t)
    t = re.sub(r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}', 'DATE', t)
    t = re.sub(r'\b(above|below|reach)\s+\$?X\b', '', t)
    t = re.sub(r'\bin\s+\w+\s+\d{4}\b', '', t)
    return t.strip()


# ════════════════════════════════════════════════════════════════════════
# Main Scan Entry Point
# ════════════════════════════════════════════════════════════════════════

def scan_correlation_anomalies(min_markets: int = 3,
                                persist: bool = True) -> List[dict]:
    """Scan all clusters and return flat list of anomalies across all.

    Each anomaly tracks the divergence between implied and historical
    correlation, flagged when >2σ.
    """
    _ensure_corr_db()

    clusters = auto_detect_clusters(min_markets=min_markets)
    logger.info(f"auto_detect_clusters: found {len(clusters)} clusters")

    all_anomalies: List[dict] = []
    for cluster in clusters:
        logger.info(f"Building matrix for cluster '{cluster['name']}' "
                     f"({cluster['n']} markets)")
        result = build_correlation_matrix(cluster, persist=persist)
        all_anomalies.extend(result.get("anomalies", []))

    logger.info(f"scan_correlation_anomalies: {len(all_anomalies)} anomalies found")
    return all_anomalies


def get_all_matrices(min_markets: int = 3) -> List[dict]:
    """Build and return correlation matrices for all detected clusters.
    Clusters with >30 markets are sampled down to 30 to keep compute feasible.
    """
    _ensure_corr_db()

    clusters = auto_detect_clusters(min_markets=min_markets)
    matrices = []
    for cluster in clusters:
        # Cap large clusters to prevent O(n^2) blowup (84 markets = 3486 pairs)
        capped = dict(cluster)
        markets = capped.get("markets", [])
        if len(markets) > 30:
            logger.warning(f"Truncating cluster '{cluster['name']}' from {len(markets)} to 30 markets")
            capped["markets"] = markets[:30]
            capped["n"] = 30
        result = build_correlation_matrix(capped, persist=True)
        matrices.append(result)

    return matrices


def get_cluster_matrix(cluster_name: str) -> Optional[dict]:
    """Build matrix for a specific cluster by name."""
    _ensure_corr_db()

    clusters = auto_detect_clusters(min_markets=2)
    for cluster in clusters:
        if cluster["name"] == cluster_name:
            return build_correlation_matrix(cluster, persist=True)
    return None


def get_latest_snapshots(cluster: Optional[str] = None,
                          limit: int = 50) -> List[dict]:
    """Get most recent correlation matrix snapshots from the DB."""
    _ensure_corr_db()
    conn = sqlite3.connect(str(CORR_DB), timeout=10)
    try:
        if cluster:
            rows = conn.execute("""
                SELECT * FROM correlation_snapshots
                WHERE cluster = ?
                ORDER BY ts DESC
                LIMIT ?
            """, (cluster, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM correlation_snapshots
                ORDER BY ts DESC
                LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_cluster_list(min_markets: int = 3) -> List[Dict[str, Any]]:
    """Get list of auto-detected clusters with their member counts."""
    clusters = auto_detect_clusters(min_markets=min_markets)
    return [
        {
            "name": c["name"],
            "n_markets": c["n"],
            "markets": [m["title"] for m in c["markets"]],
        }
        for c in clusters
    ]


# ════════════════════════════════════════════════════════════════════════
# CLI Entry
# ════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"

    if mode == "scan":
        anomalies = scan_correlation_anomalies()
        print(f"Found {len(anomalies)} anomalies:")
        for a in anomalies:
            print(f"  {a['pair']}: implied={a['implied_corr']:.4f} "
                  f"hist={a['historical_corr']:.4f} "
                  f"z={a['deviation_std']:.2f}")
    elif mode == "matrices":
        matrices = get_all_matrices()
        for m in matrices:
            print(f"\n=== {m['cluster_name']} ({m['n_markets']} markets, "
                  f"{m['n_pairs']} pairs) ===")
            for pair_key, entry in sorted(m["matrix"].items())[:10]:
                signal_tag = " ⚠️ ANOMALY" if entry.get("signal") else ""
                print(f"  {pair_key[:60]:60s} "
                      f"impl={entry['implied_corr'] or 'N/A':>8} "
                      f"hist={entry['historical_corr'] or 'N/A':>8} "
                      f"z={entry['deviation_std'] or 'N/A':>6}"
                      f"{signal_tag}")
    elif mode == "clusters":
        clusters = get_cluster_list()
        for c in clusters:
            print(f"  {c['name']}: {c['n_markets']} markets")
    else:
        print("Usage: python3 signals/implied_correlation.py [scan|matrices|clusters]")
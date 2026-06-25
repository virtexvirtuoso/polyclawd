"""
whale_thresholds.py — Per-market whale threshold config.

Based on comprehensive CLOB liquidity study (2026-06-19) sampling real
Polymarket orderbooks within 15pp of mid (to exclude AMM boundary orders).

CLOB BOOK WALL thresholds (single order on the book):
  - Set at ~p99 of observed human-placed orders
  - Below p99 = normal market-making, not a signal
  - Above p99 = abnormally large position, alert-worthy

MATCHED TRADE thresholds (executed trades):
  - Lower than book-wall because matched trades are rarer

Usage:
    from scripts.whale_thresholds import WHALE_FLOOR, TRADE_FLOOR

    # sport is a lowercase key: "mlb", "soccer", "nfl", "nba", "ufc",
    #   "politics", "crypto", "weather", "entertainment", "economy"
    sz_threshold = WHALE_FLOOR.get(sport, WHALE_FLOOR["default"])
"""

# Per-category CLOB book-wall thresholds (single order >= this → whale alert)
# Source: 2026-06-19 CLOB liquidity study, p99 of within-15pp-of-mid orders
WHALE_FLOOR = {
    "soccer":        500_000,   # WC/Champions League — deep books, huge orders normal
    "nba":           150_000,   # NBA — moderate liquidity
    "mlb":           100_000,   # MLB moneyline — p99 = $443k, floor at $100k
    "nfl":           100_000,   # NFL — similar profile to MLB
    "politics":       75_000,   # Elections — median $2k but occasional big whales
    "entertainment":  10_000,   # Entertainment — smaller markets
    "crypto":         10_000,   # Crypto prediction markets (not DeFi)
    "ufc":             2_000,   # UFC — very thin liquidity
    "weather":        25_000,   # Weather markets
    "economy":        50_000,   # Economy/macro markets
    "default":        50_000,   # Fallback for unknown categories
}

# Per-category matched trade thresholds (executed trade >= this → whale trade alert)
TRADE_FLOOR = {
    "soccer":        20_000,
    "nba":           10_000,
    "mlb":            5_000,
    "nfl":            5_000,
    "politics":       5_000,
    "entertainment":  1_000,
    "crypto":         1_000,
    "ufc":              500,
    "weather":        2_000,
    "economy":        5_000,
    "default":        5_000,
}

# Dedup window (seconds) — suppress re-alert for same wall within this window
WHALE_DEDUP_S = 1800   # 30 min

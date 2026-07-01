#!/usr/bin/env python3
"""
Volatility Spread — Implied Vol (Alpaca OPRA) vs Realized Vol (yfinance).

Computes the IV/RV ratio for each tracked ticker as a confidence overlay:
  IV/RV > 1.5 → options expensive → reduce BUY confidence
  IV/RV < 0.8 → options cheap → maintain/increase confidence

Stores RV in a JSON cache (1-hour TTL) to avoid hammering yfinance.
Designed to be called from options_implied.py during each scan cycle.
"""

import json
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

# Cache
BASE_DIR = Path(__file__).parent.parent
CACHE_FILE = BASE_DIR / "storage" / "vol_cache.json"
CACHE_TTL = 3600  # 1 hour

# ATR calculation window
RV_WINDOW = 30  # trading days for realized vol (≈1.5 calendar months)

# Thresholds
IV_RV_EXPENSIVE = 1.5   # IV >> RV → options expensive
IV_RV_CHEAP = 0.8       # IV < RV → options cheap


def _load_cache() -> Dict:
    """Load RV cache from JSON file. Returns empty dict on failure."""
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def _save_cache(cache: Dict):
    """Save RV cache to JSON file."""
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def fetch_realized_vol(ticker: str, window: int = RV_WINDOW) -> Optional[float]:
    """Fetch 30-day rolling realized volatility from yfinance.

    Computes: daily log returns → std dev → annualized (× sqrt(252)).

    Returns annualized vol as a decimal (e.g., 0.35 = 35% vol).
    Returns None if yfinance fails or data is stale (>24h since last close).
    """
    now = datetime.now(timezone.utc)

    # Check cache first
    cache = _load_cache()
    cached = cache.get(ticker)
    if cached:
        cache_age = now.timestamp() - cached.get("timestamp", 0)
        if cache_age < CACHE_TTL:
            return cached.get("rv")

    # Fetch from yfinance
    try:
        import yfinance as yf

        stock = yf.Ticker(ticker)
        hist = stock.history(period=f"{window + 20}d")  # extra buffer for weekends
    except Exception as e:
        logger.debug(f"yfinance failed for {ticker}: {e}")
        return None

    if hist.empty or "Close" not in hist.columns:
        return None

    closes = hist["Close"].dropna().values
    if len(closes) < window:
        logger.debug(f"yfinance {ticker}: only {len(closes)}/30 days of price data")
        return None

    # Check staleness: last close should be within 24h for market hours
    last_idx = hist.index[-1]
    if isinstance(last_idx, datetime):
        last_dt = last_idx.replace(tzinfo=timezone.utc) if last_idx.tzinfo is None else last_idx
        hours_ago = (now - last_dt).total_seconds() / 3600
        if hours_ago > 48:  # weekend allowance
            logger.debug(f"yfinance {ticker}: stale ({hours_ago:.0f}h ago)")
            return None

    # Use the last `window` days
    recent = closes[-window:]
    log_returns = []
    for i in range(1, len(recent)):
        if recent[i] > 0 and recent[i - 1] > 0:
            log_returns.append(math.log(recent[i] / recent[i - 1]))

    if len(log_returns) < 10:
        return None

    rv = statistics.stdev(log_returns) * math.sqrt(252)

    # Cache the result
    cache[ticker] = {
        "rv": round(rv, 6),
        "timestamp": now.timestamp(),
        "n": len(log_returns),
        "last_close": float(recent[-1]),
    }
    _save_cache(cache)

    return rv


def get_iv_rv_ratio(ticker: str, current_iv: float) -> Optional[float]:
    """Get IV/RV ratio for a ticker. Returns None if RV unavailable.

    Args:
        ticker: Stock ticker (e.g., "NVDA")
        current_iv: Current implied vol from Alpaca (decimal, e.g., 0.45)

    Returns:
        IV/RV ratio as float, or None if RV couldn't be computed.
    """
    if not current_iv or current_iv <= 0:
        return None

    rv = fetch_realized_vol(ticker)
    if not rv or rv <= 0:
        return None

    return round(current_iv / rv, 2)


def get_rv_history(ticker: str, lookback: int = 90) -> List[Dict]:
    """Get daily RV history for charting.

    Returns list of {date, rv} dicts for the last `lookback` trading days.
    """
    try:
        import yfinance as yf

        stock = yf.Ticker(ticker)
        hist = stock.history(period=f"{lookback + 30}d")
    except Exception:
        return []

    if hist.empty or "Close" not in hist.columns:
        return []

    closes = hist["Close"].dropna().values
    if len(closes) < 30:
        return []

    # Compute rolling 30-day RV for each day
    results = []
    for i in range(RV_WINDOW, len(closes)):
        window_closes = closes[i - RV_WINDOW : i]
        log_rets = []
        for j in range(1, len(window_closes)):
            if window_closes[j] > 0 and window_closes[j - 1] > 0:
                log_rets.append(math.log(window_closes[j] / window_closes[j - 1]))
        if len(log_rets) < 10:
            continue
        rv = statistics.stdev(log_rets) * math.sqrt(252)
        idx = hist.index[i]
        date_str = str(idx.date()) if hasattr(idx, "date") else str(idx)[:10]
        results.append({"date": date_str, "rv": round(rv, 4)})

    return results


def get_iv_rv_status() -> Dict[str, Dict]:
    """Get IV/RV ratio for all tracked tickers. Used by dashboard endpoint.

    Returns {ticker: {iv_rv_ratio, rv, n_data_points}}.
    """
    from signals.options_implied import discover_active_tickers

    tickers = discover_active_tickers()
    results = {}

    # Query latest IV for each ticker from options_implied DB
    db_path = __import__("os").environ.get(
        "OPTIONS_DB",
        str(Path.home() / "polyclawd-data" / "options_implied.db"),
    )

    try:
        import sqlite3

        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        for tk in tickers:
            # Get latest IV for this ticker
            # Use ATM IV — pick strike closest to underlying with reasonable IV
            row = conn.execute(
                "SELECT iv, strike, underlying FROM options_implied WHERE ticker=? AND iv IS NOT NULL AND iv < 2.0 AND iv > 0.05 AND date=(SELECT MAX(date) FROM options_implied WHERE ticker=?) ORDER BY ABS(strike - underlying) ASC LIMIT 1",
                (tk, tk),
            ).fetchone()
            current_iv = row["iv"] if row else None
            rv = fetch_realized_vol(tk)
            ratio = get_iv_rv_ratio(tk, current_iv) if current_iv else None
            results[tk] = {
                "iv": round(current_iv, 4) if current_iv else None,
                "rv": round(rv, 4) if rv else None,
                "iv_rv_ratio": min(ratio, 5.0) if ratio else None,
                "rv_n": _load_cache().get(tk, {}).get("n", 0) if rv else 0,
            }
        conn.close()
    except Exception as e:
        logger.warning(f"get_iv_rv_status failed: {e}")

    return results


# ─── CLI ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import logging as builtin_logging

    builtin_logging.basicConfig(level=builtin_logging.INFO, format="%(message)s")

    import sys

    ticker = sys.argv[1] if len(sys.argv) > 1 else "NVDA"

    rv = fetch_realized_vol(ticker)
    print(f"{ticker}: RV={rv:.1%}" if rv else f"{ticker}: RV unavailable")

    iv = float(sys.argv[2]) if len(sys.argv) > 2 else 0.45
    ratio = get_iv_rv_ratio(ticker, iv)
    print(
        f"{ticker}: IV={iv:.1%}, IV/RV={ratio}"
        if ratio
        else f"{ticker}: IV/RV unavailable"
    )
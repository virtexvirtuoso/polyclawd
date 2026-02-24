#!/usr/bin/env python3
"""
Price-to-Strike Probability Calculator

Computes volatility-adjusted probabilities for crypto strike markets
(e.g., "Will BTC be above $75,000 on March 15?") and identifies
mispriced markets where our model probability diverges from market price.

Uses Student-t distribution (df=4) for fat tails + momentum overlay.
"""

import json
import logging
import math
import re
import sqlite3
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "storage" / "shadow_trades.db"

# Asset name → trading symbol mapping
ASSET_MAP = {
    "bitcoin": "BTCUSDT", "btc": "BTCUSDT",
    "ethereum": "ETHUSDT", "eth": "ETHUSDT", "ether": "ETHUSDT",
    "solana": "SOLUSDT", "sol": "SOLUSDT",
    "dogecoin": "DOGEUSDT", "doge": "DOGEUSDT",
    "xrp": "XRPUSDT", "ripple": "XRPUSDT",
    "cardano": "ADAUSDT", "ada": "ADAUSDT",
    "avalanche": "AVAXUSDT", "avax": "AVAXUSDT",
    "polygon": "MATICUSDT", "matic": "MATICUSDT",
    "chainlink": "LINKUSDT", "link": "LINKUSDT",
    "litecoin": "LTCUSDT", "ltc": "LTCUSDT",
    "polkadot": "DOTUSDT", "dot": "DOTUSDT",
    "sui": "SUIUSDT",
    "pepe": "PEPEUSDT",
}

# Minimum edge to generate a signal
MIN_EDGE = 0.10
# Min/max days for market eligibility
MIN_DAYS = 1  # skip same-day
MAX_DAYS = 90
# Momentum adjustment coefficient
MOMENTUM_COEFF = 0.3
# Student-t degrees of freedom
T_DF = 4
# Min snapshots for vol calculation
MIN_SNAPSHOTS = 20

# Gamma API for fetching markets
GAMMA_API = "https://gamma-api.polymarket.com"


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _student_t_cdf(x: float, df: int = 4) -> float:
    """Student-t CDF using scipy if available, else regularized incomplete beta approximation."""
    try:
        from scipy.stats import t
        return float(t.cdf(x, df))
    except ImportError:
        pass
    # Pure-python fallback using the regularized incomplete beta function
    # For df=4: closed-form via standard integral
    # Use normal approximation adjusted for df (Cornish-Fisher)
    # This is approximate but usable
    import math
    # Abramowitz & Stegun approximation via normal CDF with correction
    v = df
    g1 = (x * (1 + 1 / (4 * v))) / math.sqrt(1 + x * x / (2 * v))
    # Normal CDF approximation
    return _normal_cdf(g1)


def _normal_cdf(x: float) -> float:
    """Standard normal CDF approximation."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


class StrikeProbabilityCalculator:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or str(DB_PATH)

    def _get_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def parse_strike_market(self, market_title: str, market_metadata: dict = None) -> Optional[Dict[str, Any]]:
        """Extract asset, strike price, direction, and expiry from market title.

        Returns dict with keys: asset, symbol, strike, direction, expiry_date
        or None if parsing fails.
        """
        if not market_title:
            return None

        title_lower = market_title.lower().strip()
        logger.debug("Parsing strike market: %s", market_title[:80])

        # Pattern: "Will the price of {asset} be above/below ${strike} on/by {date}?"
        # Also: "Will {asset} be above/below ${strike}..."
        patterns = [
            r'(?:price\s+of\s+)?(\w+)\s+(?:be\s+)?(?:at\s+or\s+)?(above|below|over|under|higher|lower)\s+\$?([\d,]+(?:\.\d+)?)',
            r'(\w+)\s+(?:price\s+)?(?:be\s+)?(?:at\s+or\s+)?(above|below|over|under|higher|lower)\s+than?\s+\$?([\d,]+(?:\.\d+)?)',
        ]

        asset_name = None
        direction = None
        strike = None

        for pattern in patterns:
            match = re.search(pattern, title_lower)
            if match:
                asset_name = match.group(1).strip()
                dir_word = match.group(2).strip()
                strike_str = match.group(3).replace(",", "")
                try:
                    strike = float(strike_str)
                except ValueError:
                    continue

                direction = "above" if dir_word in ("above", "over", "higher") else "below"
                break

        if asset_name is None or strike is None:
            logger.debug("Failed to parse strike from: %s", market_title[:80])
            return None

        # Map asset name to symbol
        symbol = ASSET_MAP.get(asset_name)
        if not symbol:
            logger.debug("Unknown asset: %s", asset_name)
            return None

        # Extract expiry date
        expiry_date = None

        # Try metadata first
        if market_metadata:
            for key in ("end_date_iso", "end_date", "expiry", "expiration", "endDate"):
                val = market_metadata.get(key)
                if val:
                    try:
                        if isinstance(val, str):
                            expiry_date = datetime.fromisoformat(val.replace("Z", "+00:00"))
                        break
                    except (ValueError, TypeError):
                        continue

        # Parse date from title
        if not expiry_date:
            # Match patterns like "March 15", "March 15, 2026", "Mar 15", "3/15", "2026-03-15"
            date_patterns = [
                (r'(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})', '%B %d %Y'),
                (r'(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s|$|\?)', None),  # month day without year
                (r'(\d{1,2})/(\d{1,2})/(\d{2,4})', None),
                (r'(\d{4})-(\d{2})-(\d{2})', '%Y-%m-%d'),
            ]

            # Month name pattern with optional year
            month_match = re.search(
                r'(?:on|by|before)\s+(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})?',
                market_title, re.IGNORECASE
            )
            if month_match:
                month_str = month_match.group(1)
                day_str = month_match.group(2)
                year_str = month_match.group(3)
                now = datetime.now(timezone.utc)
                year = int(year_str) if year_str else now.year

                month_names = {
                    'january': 1, 'february': 2, 'march': 3, 'april': 4,
                    'may': 5, 'june': 6, 'july': 7, 'august': 8,
                    'september': 9, 'october': 10, 'november': 11, 'december': 12,
                    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4,
                    'jun': 6, 'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
                }
                month_num = month_names.get(month_str.lower())
                if month_num:
                    try:
                        expiry_date = datetime(year, month_num, int(day_str), 23, 59, 59, tzinfo=timezone.utc)
                        # If date is in the past and no year specified, try next year
                        if not year_str and expiry_date < now:
                            expiry_date = expiry_date.replace(year=now.year + 1)
                    except ValueError:
                        pass

            # ISO date pattern
            if not expiry_date:
                iso_match = re.search(r'(\d{4})-(\d{2})-(\d{2})', market_title)
                if iso_match:
                    try:
                        expiry_date = datetime(
                            int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)),
                            23, 59, 59, tzinfo=timezone.utc
                        )
                    except ValueError:
                        pass

        if not expiry_date:
            logger.debug("Could not extract expiry date from: %s", market_title[:80])
            return None

        result = {
            "asset": asset_name,
            "symbol": symbol,
            "strike": strike,
            "direction": direction,
            "expiry_date": expiry_date,
        }
        logger.debug("Parsed strike market: %s", result)
        return result

    def get_realized_vol(self, symbol: str, window_hours: int = 168) -> Optional[float]:
        """Compute annualized daily volatility from price_snapshots.

        Returns daily vol as a decimal (e.g., 0.021 = 2.1%/day), or None if insufficient data.
        """
        conn = self._get_db()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()

        rows = conn.execute(
            """SELECT timestamp, price FROM price_snapshots
               WHERE symbol = ? AND timestamp >= ? AND price > 0
               ORDER BY timestamp ASC""",
            (symbol, cutoff)
        ).fetchall()
        conn.close()

        if len(rows) < MIN_SNAPSHOTS:
            logger.debug("Insufficient snapshots for vol: %s has %d (need %d)", symbol, len(rows), MIN_SNAPSHOTS)
            return None

        # Compute log returns
        prices = [r["price"] for r in rows]
        log_returns = []
        for i in range(1, len(prices)):
            if prices[i - 1] > 0 and prices[i] > 0:
                log_returns.append(math.log(prices[i] / prices[i - 1]))

        if len(log_returns) < 2:
            return None

        # Compute std of returns
        mean_ret = sum(log_returns) / len(log_returns)
        variance = sum((r - mean_ret) ** 2 for r in log_returns) / (len(log_returns) - 1)
        std_ret = math.sqrt(variance)

        # Estimate snapshots per day from actual timestamps
        try:
            first_ts = datetime.fromisoformat(rows[0]["timestamp"].replace("Z", "+00:00"))
            last_ts = datetime.fromisoformat(rows[-1]["timestamp"].replace("Z", "+00:00"))
            total_hours = max((last_ts - first_ts).total_seconds() / 3600, 1)
            snapshots_per_day = (len(rows) / total_hours) * 24
        except Exception:
            snapshots_per_day = 144  # default: every 10 min

        # Annualize to daily vol
        daily_vol = std_ret * math.sqrt(snapshots_per_day)

        logger.debug("Realized vol for %s: %.4f daily (%.1f%%) from %d snapshots, %.1f/day",
                      symbol, daily_vol, daily_vol * 100, len(rows), snapshots_per_day)
        return daily_vol

    def get_momentum(self, symbol: str, window_hours: int = 6) -> Optional[float]:
        """Compute normalized momentum score via linear regression on recent snapshots.

        Returns score in [-1, +1], or None if insufficient data.
        """
        conn = self._get_db()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()

        rows = conn.execute(
            """SELECT timestamp, price FROM price_snapshots
               WHERE symbol = ? AND timestamp >= ? AND price > 0
               ORDER BY timestamp ASC""",
            (symbol, cutoff)
        ).fetchall()
        conn.close()

        if len(rows) < 3:
            logger.debug("Insufficient snapshots for momentum: %s has %d", symbol, len(rows))
            return None

        # Linear regression: price vs time index
        prices = [r["price"] for r in rows]
        n = len(prices)
        x_vals = list(range(n))
        x_mean = (n - 1) / 2
        y_mean = sum(prices) / n

        numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_vals, prices))
        denominator = sum((x - x_mean) ** 2 for x in x_vals)

        if denominator == 0:
            return 0.0

        slope = numerator / denominator  # price change per snapshot

        # Normalize: slope / (daily_vol * current_price)
        current_price = prices[-1]
        daily_vol = self.get_realized_vol(symbol)

        if not daily_vol or daily_vol == 0 or current_price == 0:
            return 0.0

        # slope is per-snapshot; convert to per-day equivalent
        try:
            first_ts = datetime.fromisoformat(rows[0]["timestamp"].replace("Z", "+00:00"))
            last_ts = datetime.fromisoformat(rows[-1]["timestamp"].replace("Z", "+00:00"))
            hours_span = max((last_ts - first_ts).total_seconds() / 3600, 0.1)
            snapshots_per_day = (n / hours_span) * 24
        except Exception:
            snapshots_per_day = 144

        slope_per_day = slope * snapshots_per_day
        momentum = slope_per_day / (daily_vol * current_price)

        # Clamp to [-1, +1]
        momentum = max(-1.0, min(1.0, momentum))

        logger.debug("Momentum for %s: %.3f (slope=%.2f/snap, vol=%.4f, price=%.0f)",
                      symbol, momentum, slope, daily_vol, current_price)
        return round(momentum, 4)

    def calculate_probability(self, symbol: str, strike: float, direction: str,
                              days_left: float) -> Optional[Dict[str, Any]]:
        """Calculate probability of asset reaching strike price.

        Returns dict with prob, vol, momentum, z_score, current_price or None on failure.
        """
        conn = self._get_db()
        row = conn.execute(
            "SELECT price FROM price_snapshots WHERE symbol = ? AND price > 0 ORDER BY timestamp DESC LIMIT 1",
            (symbol,)
        ).fetchone()
        conn.close()

        if not row:
            logger.debug("No current price for %s", symbol)
            return None

        current_price = row["price"]
        daily_vol = self.get_realized_vol(symbol)

        if daily_vol is None or daily_vol <= 0:
            logger.debug("No vol available for %s", symbol)
            return None

        if days_left <= 0:
            logger.debug("Non-positive days_left: %.2f", days_left)
            return None

        distance_pct = (strike - current_price) / current_price
        vol_over_period = daily_vol * math.sqrt(days_left)

        if vol_over_period <= 0:
            return None

        z_score = distance_pct / vol_over_period

        # Student-t CDF for fat tails
        if direction == "above":
            base_prob = 1 - _student_t_cdf(z_score, T_DF)
        else:
            base_prob = _student_t_cdf(z_score, T_DF)

        # Momentum overlay
        momentum = self.get_momentum(symbol)
        if momentum is not None:
            if direction == "above":
                adjusted_prob = base_prob * (1 + MOMENTUM_COEFF * momentum)
            else:
                adjusted_prob = base_prob * (1 - MOMENTUM_COEFF * momentum)
        else:
            adjusted_prob = base_prob
            momentum = 0.0

        # Clamp
        final_prob = max(0.01, min(0.99, adjusted_prob))

        result = {
            "probability": round(final_prob, 4),
            "base_probability": round(base_prob, 4),
            "current_price": current_price,
            "daily_vol": round(daily_vol, 6),
            "daily_vol_pct": round(daily_vol * 100, 2),
            "momentum": round(momentum, 4),
            "z_score": round(z_score, 4),
            "distance_pct": round(distance_pct * 100, 2),
            "days_left": round(days_left, 2),
        }
        logger.debug("Probability calc: %s strike=%.0f dir=%s → %.1f%% (z=%.2f, vol=%.1f%%/day, mom=%.2f)",
                      symbol, strike, direction, final_prob * 100, z_score, daily_vol * 100, momentum)
        return result

    def score_market(self, market: dict) -> Optional[Dict[str, Any]]:
        """Score a single market: parse, calculate prob, compute edge.

        market dict should have: title (or question), yes_price (or outcomePrices), end_date_iso, etc.
        Returns scored dict or None if not applicable.
        """
        title = market.get("title") or market.get("question") or market.get("market_title", "")
        if not title:
            return None

        parsed = self.parse_strike_market(title, market)
        if not parsed:
            return None

        # Calculate days left
        now = datetime.now(timezone.utc)
        expiry = parsed["expiry_date"]
        days_left = (expiry - now).total_seconds() / 86400

        if days_left < MIN_DAYS:
            logger.debug("Skipping same-day market: %s (%.1f days)", title[:50], days_left)
            return None
        if days_left > MAX_DAYS:
            logger.debug("Skipping long-dated market: %s (%.0f days)", title[:50], days_left)
            return None

        # Get market-implied probability
        yes_price = None
        no_price = None

        # Try various field names
        for key in ("yes_price", "outcomePrices", "bestBid", "lastTradePrice"):
            val = market.get(key)
            if val is not None:
                if isinstance(val, str):
                    try:
                        val = json.loads(val)
                    except (json.JSONDecodeError, TypeError):
                        try:
                            val = float(val)
                        except (ValueError, TypeError):
                            continue
                if isinstance(val, list) and len(val) >= 2:
                    yes_price = float(val[0])
                    no_price = float(val[1])
                elif isinstance(val, (int, float)):
                    yes_price = float(val)
                    no_price = 1 - yes_price
                break

        if yes_price is None:
            # Try tokens array (Polymarket CLOB format)
            tokens = market.get("tokens", [])
            if tokens and len(tokens) >= 2:
                yes_price = float(tokens[0].get("price", 0))
                no_price = float(tokens[1].get("price", 0))

        if yes_price is None or yes_price <= 0:
            logger.debug("No market price found for: %s", title[:50])
            return None

        # Calculate our probability
        prob_result = self.calculate_probability(
            parsed["symbol"], parsed["strike"], parsed["direction"], days_left
        )
        if not prob_result:
            return None

        our_prob = prob_result["probability"]
        market_implied = yes_price

        # Determine signal direction and edge
        yes_edge = our_prob - market_implied
        no_edge = (1 - our_prob) - (no_price if no_price else (1 - market_implied))

        if yes_edge >= no_edge and yes_edge >= MIN_EDGE:
            signal = "YES"
            edge = yes_edge
        elif no_edge > yes_edge and no_edge >= MIN_EDGE:
            signal = "NO"
            edge = no_edge
        else:
            signal = "SKIP"
            edge = max(yes_edge, no_edge)

        result = {
            "market_id": market.get("id") or market.get("condition_id") or market.get("market_id", ""),
            "market_title": title[:200],
            "asset": parsed["asset"],
            "symbol": parsed["symbol"],
            "strike": parsed["strike"],
            "direction": parsed["direction"],
            "expiry_date": expiry.isoformat(),
            "days_left": round(days_left, 1),
            "our_prob": round(our_prob, 4),
            "market_prob": round(market_implied, 4),
            "edge": round(edge, 4),
            "edge_pct": round(edge * 100, 1),
            "signal": signal,
            "strategy": "price_to_strike",
            "current_price": prob_result["current_price"],
            "daily_vol_pct": prob_result["daily_vol_pct"],
            "momentum": prob_result["momentum"],
            "z_score": prob_result["z_score"],
            "distance_pct": prob_result["distance_pct"],
        }

        if signal != "SKIP":
            logger.info("🎯 Strike signal: %s %s | edge=%.1f%% | our=%.1f%% vs market=%.1f%% | %s $%.0f %s in %.0fd",
                        signal, title[:50], edge * 100, our_prob * 100, market_implied * 100,
                        parsed["asset"], parsed["strike"], parsed["direction"], days_left)

        return result

    def scan_all_strikes(self, markets: List[dict] = None) -> List[Dict[str, Any]]:
        """Scan all crypto strike markets and return scored results sorted by edge.

        If markets not provided, fetches from Polymarket Gamma API.
        """
        if markets is None:
            markets = self._fetch_crypto_markets()

        logger.info("Scanning %d markets for strike probability signals", len(markets))

        results = []
        for market in markets:
            try:
                scored = self.score_market(market)
                if scored and scored["signal"] != "SKIP":
                    results.append(scored)
            except Exception as e:
                logger.debug("Error scoring market: %s — %s", 
                           (market.get("title") or market.get("question", ""))[:40], e)

        # Sort by edge descending
        results.sort(key=lambda x: x["edge"], reverse=True)

        logger.info("Strike scan complete: %d signals from %d markets", len(results), len(markets))
        return results

    def _fetch_crypto_markets(self) -> List[dict]:
        """Fetch active crypto price markets from Polymarket Gamma API."""
        markets = []

        # Search for crypto price markets
        queries = ["bitcoin price", "ethereum price", "btc above", "eth above",
                    "btc below", "eth below", "solana price", "sol above"]

        seen_ids = set()
        for query in queries:
            try:
                url = f"{GAMMA_API}/markets?tag=crypto&active=true&closed=false&limit=50"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode())

                if isinstance(data, list):
                    for m in data:
                        mid = m.get("id") or m.get("condition_id", "")
                        if mid and mid not in seen_ids:
                            seen_ids.add(mid)
                            markets.append(m)
            except Exception as e:
                logger.debug("Failed to fetch markets for query '%s': %s", query, e)

        # Also try the events endpoint for crypto
        try:
            url = f"{GAMMA_API}/events?tag=crypto&active=true&closed=false&limit=50"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                events = json.loads(resp.read().decode())

            if isinstance(events, list):
                for event in events:
                    for m in event.get("markets", []):
                        mid = m.get("id") or m.get("condition_id", "")
                        if mid and mid not in seen_ids:
                            seen_ids.add(mid)
                            markets.append(m)
        except Exception as e:
            logger.debug("Failed to fetch crypto events: %s", e)

        logger.debug("Fetched %d crypto markets from Gamma API", len(markets))
        return markets


# Module-level convenience functions
_calculator = None

def get_calculator() -> StrikeProbabilityCalculator:
    global _calculator
    if _calculator is None:
        _calculator = StrikeProbabilityCalculator()
    return _calculator


def scan_strikes(markets: List[dict] = None) -> List[Dict[str, Any]]:
    """Module-level convenience: scan all strike markets."""
    return get_calculator().scan_all_strikes(markets)


def score_market(market: dict) -> Optional[Dict[str, Any]]:
    """Module-level convenience: score a single market."""
    return get_calculator().score_market(market)

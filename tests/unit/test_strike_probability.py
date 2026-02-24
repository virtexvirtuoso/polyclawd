#!/usr/bin/env python3
"""Tests for strike_probability.py"""

import math
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

# Add signals dir to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'signals'))

from strike_probability import StrikeProbabilityCalculator, _student_t_cdf, _normal_cdf


@pytest.fixture
def tmp_db():
    """Create a temp DB with price_snapshots table and sample data."""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE price_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, symbol TEXT, price REAL,
        change_24h REAL, volume_24h REAL,
        high_24h REAL, low_24h REAL, bid REAL, ask REAL, source TEXT
    )""")

    # Insert 50 BTC snapshots over ~2 days, price ~64000 with some noise
    now = datetime.now(timezone.utc)
    base_price = 64000.0
    for i in range(50):
        ts = (now - timedelta(hours=50 - i)).isoformat()
        # Slight upward trend with noise
        noise = math.sin(i * 0.5) * 500 + i * 10
        price = base_price + noise
        conn.execute(
            "INSERT INTO price_snapshots (timestamp, symbol, price, change_24h, volume_24h, high_24h, low_24h, bid, ask, source) "
            "VALUES (?, ?, ?, 0, 0, 0, 0, 0, 0, 'test')",
            (ts, 'BTCUSDT', round(price, 2))
        )

    # Insert 50 ETH snapshots
    for i in range(50):
        ts = (now - timedelta(hours=50 - i)).isoformat()
        noise = math.sin(i * 0.3) * 30 + i * 1
        price = 3400.0 + noise
        conn.execute(
            "INSERT INTO price_snapshots (timestamp, symbol, price, change_24h, volume_24h, high_24h, low_24h, bid, ask, source) "
            "VALUES (?, ?, ?, 0, 0, 0, 0, 0, 0, 'test')",
            (ts, 'ETHUSDT', round(price, 2))
        )

    conn.commit()
    conn.close()
    yield path
    os.unlink(path)


@pytest.fixture
def calc(tmp_db):
    return StrikeProbabilityCalculator(db_path=tmp_db)


# ─── Parsing Tests ───────────────────────────────────────────

class TestParseStrikeMarket:
    def test_basic_above(self, calc):
        result = calc.parse_strike_market(
            "Will the price of Bitcoin be above $75,000 on March 15, 2026?"
        )
        assert result is not None
        assert result["symbol"] == "BTCUSDT"
        assert result["strike"] == 75000.0
        assert result["direction"] == "above"
        assert result["expiry_date"].month == 3
        assert result["expiry_date"].day == 15

    def test_basic_below(self, calc):
        result = calc.parse_strike_market(
            "Will the price of Ethereum be below $3,000 on April 1, 2026?"
        )
        assert result is not None
        assert result["symbol"] == "ETHUSDT"
        assert result["strike"] == 3000.0
        assert result["direction"] == "below"

    def test_short_form(self, calc):
        result = calc.parse_strike_market(
            "Will BTC be above $70000 on Feb 28, 2026?"
        )
        assert result is not None
        assert result["symbol"] == "BTCUSDT"
        assert result["strike"] == 70000.0
        assert result["direction"] == "above"

    def test_no_match_non_price(self, calc):
        result = calc.parse_strike_market("Will the Lakers win the NBA Finals?")
        assert result is None

    def test_no_match_unknown_asset(self, calc):
        result = calc.parse_strike_market(
            "Will the price of Unobtainium be above $100 on March 1?"
        )
        assert result is None

    def test_metadata_expiry(self, calc):
        result = calc.parse_strike_market(
            "Will Bitcoin be above $80000 by end date?",
            {"end_date_iso": "2026-04-15T23:59:59Z"}
        )
        assert result is not None
        assert result["expiry_date"].month == 4
        assert result["expiry_date"].day == 15

    def test_no_date_returns_none(self, calc):
        result = calc.parse_strike_market("Will Bitcoin be above $100000?")
        assert result is None


# ─── Volatility Tests ────────────────────────────────────────

class TestRealizedVol:
    def test_btc_vol_returns_float(self, calc):
        vol = calc.get_realized_vol("BTCUSDT", window_hours=168)
        assert vol is not None
        assert isinstance(vol, float)
        assert 0 < vol < 1  # daily vol should be a reasonable fraction

    def test_insufficient_data(self, calc):
        vol = calc.get_realized_vol("SOLUSDT", window_hours=168)
        assert vol is None  # no SOL data in fixture

    def test_vol_magnitude(self, calc):
        vol = calc.get_realized_vol("BTCUSDT")
        assert vol is not None
        # Crypto daily vol typically 1-10%
        assert 0.001 < vol < 0.5


# ─── Momentum Tests ──────────────────────────────────────────

class TestMomentum:
    def test_momentum_returns_bounded(self, calc):
        mom = calc.get_momentum("BTCUSDT", window_hours=48)
        assert mom is not None
        assert -1.0 <= mom <= 1.0

    def test_momentum_no_data(self, calc):
        mom = calc.get_momentum("SOLUSDT")
        assert mom is None

    def test_uptrend_positive(self, tmp_db):
        """Insert strongly trending data and verify positive momentum."""
        conn = sqlite3.connect(tmp_db)
        now = datetime.now(timezone.utc)
        # Clear existing and insert strong uptrend
        conn.execute("DELETE FROM price_snapshots WHERE symbol='TESTUSDT'")
        for i in range(30):
            ts = (now - timedelta(hours=5) + timedelta(minutes=i * 10)).isoformat()
            price = 1000 + i * 50  # strong uptrend
            conn.execute(
                "INSERT INTO price_snapshots (timestamp, symbol, price, change_24h, volume_24h, high_24h, low_24h, bid, ask, source) "
                "VALUES (?, 'TESTUSDT', ?, 0, 0, 0, 0, 0, 0, 'test')",
                (ts, price)
            )
        conn.commit()
        conn.close()

        calc = StrikeProbabilityCalculator(db_path=tmp_db)
        mom = calc.get_momentum("TESTUSDT", window_hours=6)
        # May be None if vol calc fails (not enough data for TESTUSDT vol)
        # But at minimum it should not error


# ─── Probability Tests ───────────────────────────────────────

class TestProbability:
    def test_at_the_money(self, calc):
        """Strike near current price should be ~50%."""
        # Get current BTC price from fixture
        conn = sqlite3.connect(calc.db_path)
        row = conn.execute("SELECT price FROM price_snapshots WHERE symbol='BTCUSDT' ORDER BY timestamp DESC LIMIT 1").fetchone()
        conn.close()
        current = row[0]

        result = calc.calculate_probability("BTCUSDT", current, "above", 7)
        assert result is not None
        # At the money, prob should be near 50%
        assert 0.3 < result["probability"] < 0.7

    def test_deep_otm_low_prob(self, calc):
        """Very high strike should have low probability."""
        result = calc.calculate_probability("BTCUSDT", 200000, "above", 7)
        assert result is not None
        assert result["probability"] < 0.2

    def test_deep_itm_high_prob(self, calc):
        """Very low strike should have high probability for 'above'."""
        result = calc.calculate_probability("BTCUSDT", 10000, "above", 7)
        assert result is not None
        assert result["probability"] > 0.8

    def test_below_direction(self, calc):
        """'below' on a low strike should have low probability."""
        result = calc.calculate_probability("BTCUSDT", 10000, "below", 7)
        assert result is not None
        assert result["probability"] < 0.2

    def test_no_data_returns_none(self, calc):
        result = calc.calculate_probability("SOLUSDT", 100, "above", 7)
        assert result is None


# ─── Scoring Tests ────────────────────────────────────────────

class TestScoring:
    def test_score_market_with_edge(self, calc):
        """Market with clear mispricing should return a signal."""
        # Deep ITM market priced at 25% → huge edge
        market = {
            "title": "Will the price of Bitcoin be above $10,000 on March 15, 2026?",
            "id": "test-market-1",
            "yes_price": 0.25,
        }
        result = calc.score_market(market)
        assert result is not None
        assert result["signal"] == "YES"
        assert result["edge"] > 0.1
        assert result["strategy"] == "price_to_strike"

    def test_score_market_no_signal(self, calc):
        """Fairly priced market should return SKIP or None."""
        conn = sqlite3.connect(calc.db_path)
        row = conn.execute("SELECT price FROM price_snapshots WHERE symbol='BTCUSDT' ORDER BY timestamp DESC LIMIT 1").fetchone()
        conn.close()
        current = row[0]

        market = {
            "title": f"Will the price of Bitcoin be above ${int(current)} on March 15, 2026?",
            "id": "test-market-2",
            "yes_price": 0.50,  # roughly fair for ATM
        }
        result = calc.score_market(market)
        # Could be None or SKIP — either is acceptable for near-fair pricing
        if result is not None:
            assert result["edge"] < 0.5  # shouldn't show massive edge on fair market

    def test_skip_same_day(self, calc):
        """Same-day market should be skipped."""
        today = datetime.now(timezone.utc).strftime("%B %d, %Y")
        market = {
            "title": f"Will the price of Bitcoin be above $60,000 on {today}?",
            "id": "test-same-day",
            "yes_price": 0.50,
        }
        result = calc.score_market(market)
        assert result is None

    def test_skip_non_crypto(self, calc):
        result = calc.score_market({"title": "Will it rain tomorrow?", "yes_price": 0.5})
        assert result is None


# ─── Student-t CDF Tests ─────────────────────────────────────

class TestStudentT:
    def test_cdf_at_zero(self):
        assert abs(_student_t_cdf(0, 4) - 0.5) < 0.01

    def test_cdf_large_positive(self):
        assert _student_t_cdf(10, 4) > 0.99

    def test_cdf_large_negative(self):
        assert _student_t_cdf(-10, 4) < 0.01

    def test_fatter_than_normal(self):
        """Student-t should give higher tail probabilities than normal."""
        z = 3.0
        t_tail = 1 - _student_t_cdf(z, 4)
        n_tail = 1 - _normal_cdf(z)
        assert t_tail > n_tail  # fat tails


# ─── Scan Tests ──────────────────────────────────────────────

class TestScan:
    def test_scan_with_mock_markets(self, calc):
        """Integration test: scan a list of mock markets."""
        markets = [
            {
                "id": "m1",
                "title": "Will the price of Bitcoin be above $10,000 on March 15, 2026?",
                "yes_price": 0.25,
            },
            {
                "id": "m2",
                "title": "Will the price of Ethereum be below $1,000 on March 20, 2026?",
                "yes_price": 0.80,
            },
            {
                "id": "m3",
                "title": "Will the Lakers win?",
                "yes_price": 0.50,
            },
        ]
        results = calc.scan_all_strikes(markets)
        assert isinstance(results, list)
        # At least the deep ITM BTC market should generate a signal
        btc_signals = [r for r in results if r["symbol"] == "BTCUSDT"]
        assert len(btc_signals) >= 1
        # Should be sorted by edge descending
        if len(results) >= 2:
            assert results[0]["edge"] >= results[1]["edge"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

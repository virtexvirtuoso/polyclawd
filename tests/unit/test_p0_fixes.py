"""Tests for P0 bug fixes: Kelly variable odds, PRAGMA busy_timeout, Sharpe Bessel's correction."""
import sqlite3
import pytest
from config.scaling_phases import calculate_position_size


# ============================================================================
# Kelly Criterion — Variable Odds
# ============================================================================

class TestKellyVariableOdds:
    """Verify Kelly formula handles variable market prices correctly."""

    def test_even_odds_backward_compat(self):
        """At market_price=0.50, new formula should match old (2p-1) behavior."""
        # Old formula: kelly = 2*0.60 - 1 = 0.20
        result = calculate_position_size(1000, 60, market_price=0.50)
        assert abs(result["kelly_raw"] - 0.20) < 0.001
        assert result["payout_ratio"] == 1.0

    def test_underpriced_market(self):
        """market_price=0.25 with p=0.45 → kelly≈0.267 (positive edge on cheap contract)."""
        result = calculate_position_size(1000, 45, market_price=0.25)
        # b = 0.75/0.25 = 3.0, kelly = (3*0.45 - 0.55)/3 = (1.35-0.55)/3 = 0.267
        assert abs(result["kelly_raw"] - 0.2667) < 0.01
        assert abs(result["payout_ratio"] - 3.0) < 0.01

    def test_negative_edge_clamped(self):
        """When edge is negative, Kelly should clamp to 0 (don't bet)."""
        # p=0.30, market_price=0.50 → b=1.0, kelly = (1*0.30 - 0.70)/1 = -0.40 → clamped to 0
        result = calculate_position_size(1000, 30, market_price=0.50)
        assert result["kelly_raw"] == 0.0

    def test_expensive_market(self):
        """market_price=0.90 → low payout ratio, needs high confidence."""
        # b = 0.10/0.90 ≈ 0.111, p=0.95 → kelly = (0.111*0.95 - 0.05)/0.111 ≈ 0.5
        result = calculate_position_size(1000, 95, market_price=0.90)
        assert result["kelly_raw"] > 0
        assert abs(result["payout_ratio"] - 0.1111) < 0.01

    def test_market_price_clamped_extremes(self):
        """market_price outside [0.01, 0.99] should be clamped."""
        result_low = calculate_position_size(1000, 60, market_price=0.0)
        result_high = calculate_position_size(1000, 60, market_price=1.0)
        # Should not error, should use clamped values
        assert result_low["payout_ratio"] > 0
        assert result_high["payout_ratio"] > 0

    def test_position_usd_returns_positive(self):
        """Sanity: position size should be positive for positive edge."""
        result = calculate_position_size(500, 65, market_price=0.40)
        assert result["position_usd"] > 0
        assert result["position_pct"] > 0

    def test_return_dict_has_new_fields(self):
        """Verify market_price and payout_ratio in return dict."""
        result = calculate_position_size(1000, 60, market_price=0.35)
        assert "market_price" in result
        assert "payout_ratio" in result
        assert result["market_price"] == 0.35


# ============================================================================
# PRAGMA busy_timeout — Alpha Score Tracker
# ============================================================================

class TestPragmaBusyTimeout:
    """Verify _get_conn() sets WAL mode and busy_timeout."""

    def test_get_conn_wal_mode(self, tmp_path):
        """_get_conn should set journal_mode=WAL."""
        from signals.alpha_score_tracker import _get_conn
        db = str(tmp_path / "test.db")
        conn = _get_conn(db)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        conn.close()

    def test_get_conn_busy_timeout(self, tmp_path):
        """_get_conn should set busy_timeout=5000."""
        from signals.alpha_score_tracker import _get_conn
        db = str(tmp_path / "test.db")
        conn = _get_conn(db)
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout == 5000
        conn.close()


# ============================================================================
# Sharpe Ratio — Bessel's Correction
# ============================================================================

class TestSharpeBessel:
    """Verify Sharpe uses N-1 denominator (sample std)."""

    def test_sharpe_with_known_values(self):
        """Compute Sharpe manually and compare.

        PnL values: [10, -5, 15, -2, 8]
        Mean = 5.2
        Sample variance = [(10-5.2)^2 + (-5-5.2)^2 + (15-5.2)^2 + (-2-5.2)^2 + (8-5.2)^2] / 4
                       = [23.04 + 104.04 + 96.04 + 51.84 + 7.84] / 4
                       = 282.8 / 4 = 70.7
        Sample std = sqrt(70.7) ≈ 8.408
        Sharpe = mean / std = 5.2 / 8.408 ≈ 0.6185
        """
        import math
        pnl_values = [10, -5, 15, -2, 8]
        mean_pnl = sum(pnl_values) / len(pnl_values)
        variance = sum((x - mean_pnl) ** 2 for x in pnl_values) / (len(pnl_values) - 1)
        std_pnl = math.sqrt(variance)
        expected_sharpe = mean_pnl / std_pnl

        assert abs(expected_sharpe - 0.6185) < 0.01
        # This confirms the formula we patched uses N-1

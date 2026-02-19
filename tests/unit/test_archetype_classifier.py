"""Unit tests for archetype classifier and kill rules."""
import pytest

from signals.mispriced_category_signal import classify_archetype, _check_kill_rules


# ============================================================================
# classify_archetype() tests
# ============================================================================

class TestClassifyArchetype:
    """Test market title → archetype classification."""

    @pytest.mark.parametrize("title,expected", [
        # daily_updown
        ("Bitcoin Up or Down on February 14?", "daily_updown"),
        ("Bitcoin Up or Down on February 17?", "daily_updown"),
        ("Ethereum Up or Down on February 20?", "daily_updown"),
        ("Will Bitcoin go Up or Down today?", "daily_updown"),
        ("SOL Up or Down on March 1?", "daily_updown"),

        # intraday_updown — time patterns
        ("Bitcoin Up or Down - February 14, 2:00PM-6:00PM ET", "intraday_updown"),
        ("Bitcoin Up or Down - February 17, 8:00AM-12:00PM ET", "intraday_updown"),
        ("Ethereum Up or Down - Feb 20, 10:00AM ET", "intraday_updown"),
        ("BTC Up or Down 4h", "intraday_updown"),
        ("ETH Up or Down 15m", "intraday_updown"),
        ("Bitcoin Up or Down 1h candle", "intraday_updown"),
        ("SOL Up or Down - March 1, 3:00PM to 5:00PM", "intraday_updown"),
        ("BTC Up or Down AM to PM session", "intraday_updown"),

        # price_above
        ("Will BTC be above $68,000 on February 17?", "price_above"),
        ("Will Bitcoin be above $100,000?", "price_above"),
        ("Ethereum above $4,000 by March?", "price_above"),
        ("Will SOL reach $200?", "price_above"),
        ("BTC below $50,000 on Friday?", "price_above"),
        ("Will gold exceed $2,500?", "price_above"),
        ("Bitcoin over $95,000 tomorrow?", "price_above"),
        ("ETH under $3,000 by end of week?", "price_above"),

        # price_range
        ("Bitcoin price range on Feb 18?", "price_range"),
        ("BTC price between $65,000 and $70,000?", "price_range"),
        ("Ethereum price on March 1?", "price_range"),
        ("Bitcoin price at close today?", "price_range"),

        # directional
        ("Will Bitcoin dip to $60,000?", "directional"),
        ("Will BTC crash to $40,000?", "directional"),
        ("Will Ethereum fall to $2,000?", "directional"),
        ("Will SOL drop to $100?", "directional"),
        ("Will BTC plunge to $50,000?", "directional"),

        # other — unclassifiable
        ("Will the new iPhone have a USB-C port?", "other"),
        ("Who will win Best Picture at the Oscars?", "other"),
        ("Will it rain in NYC tomorrow?", "other"),
        ("Taylor Swift album release date?", "other"),
        ("", "other"),
    ])
    def test_classify_archetype(self, title, expected):
        assert classify_archetype(title) == expected

    def test_none_title(self):
        assert classify_archetype(None) == "other"

    def test_case_insensitivity(self):
        assert classify_archetype("BITCOIN UP OR DOWN ON FEBRUARY 14?") == "daily_updown"
        assert classify_archetype("bitcoin up or down - feb 14, 2:00pm ET") == "intraday_updown"


# ============================================================================
# _check_kill_rules() tests
# ============================================================================

class TestKillRules:
    """Test kill rule logic."""

    def test_k3_cheap_entry(self):
        """K3: Any trade below 30c should be killed."""
        killed, reason, arch = _check_kill_rules("Bitcoin Up or Down on Feb 14?", 25)
        assert killed
        assert "K3" in reason
        assert "30c" in reason

    def test_k3_at_boundary(self):
        """K3: Exactly 30c should NOT be killed by K3."""
        killed, reason, arch = _check_kill_rules("Bitcoin Up or Down on Feb 14?", 30)
        assert not killed  # 30c is not < 30

    def test_k1_intraday(self):
        """K1: Intraday up/down should be killed."""
        killed, reason, arch = _check_kill_rules(
            "Bitcoin Up or Down - Feb 14, 2:00PM-6:00PM ET", 65
        )
        assert killed
        assert "K1" in reason
        assert arch == "intraday_updown"

    def test_k4_price_range(self):
        """K4: Price range markets should be killed."""
        killed, reason, arch = _check_kill_rules("Bitcoin price range on Feb 18?", 55)
        assert killed
        assert "K4" in reason
        assert arch == "price_range"

    def test_k5_directional(self):
        """K5: Directional dip/crash should be killed."""
        killed, reason, arch = _check_kill_rules("Will Bitcoin dip to $60,000?", 70)
        assert killed
        assert "K5" in reason
        assert arch == "directional"

    def test_k2_price_above_cheap(self):
        """K2: price_above with cheap entry (<45c) should be killed."""
        killed, reason, arch = _check_kill_rules("Will BTC be above $68,000?", 35)
        assert killed
        assert "K2" in reason

    def test_k2_price_above_not_cheap(self):
        """K2: price_above with normal entry (>=45c) should NOT be killed."""
        killed, reason, arch = _check_kill_rules("Will BTC be above $68,000?", 65)
        assert not killed
        assert arch == "price_above"

    def test_k6_unknown_archetype(self):
        """K6: Unknown archetype should be killed."""
        killed, reason, arch = _check_kill_rules("Who will win the Super Bowl?", 55)
        assert killed
        assert "K6" in reason
        assert arch == "other"

    def test_daily_updown_passes(self):
        """daily_updown at normal price should pass all kill rules."""
        killed, reason, arch = _check_kill_rules("Bitcoin Up or Down on Feb 14?", 65)
        assert not killed
        assert reason == ""
        assert arch == "daily_updown"

    def test_price_above_normal_passes(self):
        """price_above at normal price should pass all kill rules."""
        killed, reason, arch = _check_kill_rules("Will BTC be above $68,000?", 70)
        assert not killed
        assert arch == "price_above"

    def test_k3_takes_priority_over_k1(self):
        """K3 (price) fires before K1 (archetype) since it's checked first."""
        killed, reason, arch = _check_kill_rules(
            "Bitcoin Up or Down - Feb 14, 2:00PM ET", 20
        )
        assert killed
        assert "K3" in reason  # K3 fires first, not K1

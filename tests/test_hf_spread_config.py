"""Task 4.1 — HF spread scan noise kill: sub-1h sends off, 1h+ threshold 0.15,
>$10K liquidity gate on 1h+ intramarket alerts. Scanning/data stay intact."""
import services.hf_spread_scanner as hf


def test_sub_hour_timeframes_never_alert():
    assert hf.TF_CONFIG["5m"]["alert"] is False
    assert hf.TF_CONFIG["15m"]["alert"] is False


def test_1h_plus_still_alert():
    assert hf.TF_CONFIG["1h"]["alert"] is True
    assert hf.TF_CONFIG["4h"]["alert"] is True


def test_1h_plus_intramarket_threshold_raised_to_015():
    assert hf.TF_CONFIG["1h"]["intramarket"] == 0.15
    assert hf.TF_CONFIG["4h"]["intramarket"] == 0.15


def test_1h_plus_liquidity_gate_10k():
    assert hf.TF_CONFIG["1h"]["min_liquidity"] == 10_000
    assert hf.TF_CONFIG["4h"]["min_liquidity"] == 10_000


def test_alert_allowed_blocks_sub_hour_sends():
    a5 = {"type": "intramarket", "asset": "BTC", "duration": "5m",
          "direction": "UP", "liquidity": 50_000.0}
    a15 = {"type": "cross_asset", "group": "high_beta", "duration": "15m"}
    ts = {"type": "term_spread", "asset": "ETH", "short_tf": "15m",
          "long_tf": "1h", "duration": "15m"}
    assert hf._alert_allowed(a5) is False
    assert hf._alert_allowed(a15) is False
    assert hf._alert_allowed(ts) is False


def test_alert_allowed_1h_intramarket_liquidity_gate():
    lo = {"type": "intramarket", "asset": "BTC", "duration": "1h",
          "direction": "UP", "liquidity": 5_000.0}
    hi = {"type": "intramarket", "asset": "BTC", "duration": "1h",
          "direction": "UP", "liquidity": 20_000.0}
    missing = {"type": "intramarket", "asset": "BTC", "duration": "1h",
               "direction": "UP"}  # no liquidity data -> fail closed
    assert hf._alert_allowed(lo) is False
    assert hf._alert_allowed(hi) is True
    assert hf._alert_allowed(missing) is False


def test_alert_allowed_1h_non_intramarket_passes():
    assert hf._alert_allowed(
        {"type": "cross_asset", "group": "high_beta", "duration": "1h"}) is True
    assert hf._alert_allowed(
        {"type": "term_spread", "asset": "SOL", "short_tf": "1h",
         "long_tf": "4h", "duration": "1h"}) is True

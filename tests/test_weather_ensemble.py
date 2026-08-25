"""Tests for weather_ensemble module."""
import json
import pytest
from unittest.mock import patch, MagicMock
from signals.weather_ensemble import (
    _c_to_f,
    _norm_cdf,
    _t_cdf,
    _fetch_open_meteo_ensemble,
    get_ensemble_forecast,
    prob_below,
    prob_above,
    prob_in_range,
    ensemble_fair_value,
    source_health,
    _cache,
    _cache_ts,
    _resolve_city,
)


class TestUnitConversions:
    def test_c_to_f_freezing(self):
        assert _c_to_f(0) == 32.0

    def test_c_to_f_boiling(self):
        assert _c_to_f(100) == 212.0

    def test_c_to_f_body_temp(self):
        assert abs(_c_to_f(37) - 98.6) < 0.1


class TestNormCDF:
    def test_mean(self):
        assert abs(_norm_cdf(0) - 0.5) < 0.001

    def test_negative_extreme(self):
        assert _norm_cdf(-10) == 0.0

    def test_positive_extreme(self):
        assert _norm_cdf(10) == 1.0

    def test_one_sigma(self):
        assert abs(_norm_cdf(1) - 0.8413) < 0.05  # Approximation tolerance

    def test_two_sigma(self):
        assert abs(_norm_cdf(2) - 0.9772) < 0.03

    def test_symmetry(self):
        assert abs(_norm_cdf(1) + _norm_cdf(-1) - 1.0) < 0.001


class TestTCDF:
    def test_mean(self):
        assert abs(_t_cdf(0, df=4) - 0.5) < 0.001

    def test_fatter_tails(self):
        # t-distribution should have more mass in tails than normal
        normal_tail = 1 - _norm_cdf(2)
        t_tail = 1 - _t_cdf(2, df=4)
        assert t_tail > normal_tail

    def test_converges_to_normal(self):
        # High df should be close to normal
        assert abs(_t_cdf(1, df=100) - _norm_cdf(1)) < 0.02


class TestResolveCity:
    def test_exact(self):
        assert _resolve_city("miami") is not None

    def test_case_insensitive(self):
        assert _resolve_city("Miami") is not None

    def test_fuzzy(self):
        assert _resolve_city("buenos") is not None

    def test_unknown(self):
        assert _resolve_city("atlantis") is None


# Mock Open-Meteo ensemble response
MOCK_ENSEMBLE_RESPONSE = {
    "daily": {
        "time": ["2026-02-28"],
        "temperature_2m_max_icon_seamless_eps": [30.0],
        "temperature_2m_max_member01_icon_seamless_eps": [29.5],
        "temperature_2m_max_member02_icon_seamless_eps": [30.5],
        "temperature_2m_max_member03_icon_seamless_eps": [28.0],
        "temperature_2m_max_member04_icon_seamless_eps": [31.0],
        "temperature_2m_max_ncep_gefs_seamless": [28.5],
        "temperature_2m_max_member01_ncep_gefs_seamless": [27.5],
        "temperature_2m_max_member02_ncep_gefs_seamless": [29.0],
        "temperature_2m_max_gem_global_ensemble": [26.0],
        "temperature_2m_max_member01_gem_global_ensemble": [25.5],
        "temperature_2m_min_icon_seamless_eps": [20.0],
        "temperature_2m_min_ncep_gefs_seamless": [21.0],
        "temperature_2m_min_gem_global_ensemble": [19.5],
    }
}


class TestFetchOpenMeteoEnsemble:
    @patch("signals.weather_ensemble._fetch_json")
    def test_parses_members(self, mock_fetch):
        mock_fetch.return_value = MOCK_ENSEMBLE_RESPONSE
        result = _fetch_open_meteo_ensemble(25.76, -80.19, "2026-02-28")
        assert result is not None
        assert result["n_members"] == 10  # 5 icon + 3 gefs + 2 gem
        assert result["high_std_f"] > 0
        assert len(result["raw_highs_f"]) == 10

    @patch("signals.weather_ensemble._fetch_json")
    def test_null_values_skipped(self, mock_fetch):
        resp = {"daily": {
            "time": ["2026-02-28"],
            "temperature_2m_max_ecmwf_ifs04": [None],
            "temperature_2m_max_icon_seamless_eps": [30.0],
        }}
        mock_fetch.return_value = resp
        result = _fetch_open_meteo_ensemble(25.76, -80.19, "2026-02-28")
        assert result is not None
        assert result["n_members"] == 1

    @patch("signals.weather_ensemble._fetch_json")
    def test_empty_response(self, mock_fetch):
        mock_fetch.return_value = None
        # Should try fallback
        result = _fetch_open_meteo_ensemble(25.76, -80.19, "2026-02-28")
        # Fallback also uses _fetch_json which returns None
        assert result is None


class TestGetEnsembleForecast:
    @patch("signals.weather_ensemble._fetch_open_meteo_ensemble")
    def test_aggregation(self, mock_om):
        _cache.clear()
        _cache_ts.clear()
        mock_om.return_value = {
            "source": "open_meteo_ensemble",
            "high_f": 82.0,
            "high_std_f": 2.5,
            "low_f": 68.0,
            "n_members": 50,
            "models": ["icon_seamless", "gfs_seamless"],
            "raw_highs_f": [80, 81, 82, 83, 84],
            "p10_f": 80.0,
            "p90_f": 84.0,
        }
        result = get_ensemble_forecast("miami", "2026-02-28")
        assert result is not None
        assert result["ensemble"]["high_mean_f"] == 82.0
        assert result["ensemble"]["n_sources"] == 1
        assert result["ensemble"]["n_models"] == 50

    def test_unknown_city(self):
        result = get_ensemble_forecast("atlantis", "2026-02-28")
        assert result is None

    @patch("signals.weather_ensemble._fetch_open_meteo_ensemble")
    def test_caching(self, mock_om):
        _cache.clear()
        _cache_ts.clear()
        mock_om.return_value = {
            "source": "test", "high_f": 80.0, "high_std_f": 2.0,
            "low_f": 65.0, "n_members": 5, "models": ["test"],
            "raw_highs_f": [80], "p10_f": 78.0, "p90_f": 82.0,
        }
        get_ensemble_forecast("miami", "2026-02-28")
        get_ensemble_forecast("miami", "2026-02-28")
        assert mock_om.call_count == 1  # Second call uses cache


class TestProbabilities:
    @patch("signals.weather_ensemble.get_ensemble_forecast")
    def test_prob_below_at_mean(self, mock_forecast):
        mock_forecast.return_value = {
            "ensemble": {
                "high_mean_f": 80.0, "high_std_f": 2.0,
                "n_sources": 3, "source_agreement": 0.9,
            }
        }
        result = prob_below("miami", "2026-02-28", 80.0)
        assert abs(result["probability"] - 0.5) < 0.01

    @patch("signals.weather_ensemble.get_ensemble_forecast")
    def test_prob_below_far_below(self, mock_forecast):
        mock_forecast.return_value = {
            "ensemble": {
                "high_mean_f": 80.0, "high_std_f": 2.0,
                "n_sources": 3, "source_agreement": 0.9,
            }
        }
        result = prob_below("miami", "2026-02-28", 70.0)
        assert result["probability"] < 0.01

    @patch("signals.weather_ensemble.get_ensemble_forecast")
    def test_prob_above(self, mock_forecast):
        mock_forecast.return_value = {
            "ensemble": {
                "high_mean_f": 80.0, "high_std_f": 2.0,
                "n_sources": 3, "source_agreement": 0.9,
            }
        }
        result = prob_above("miami", "2026-02-28", 80.0)
        assert abs(result["probability"] - 0.5) < 0.01

    @patch("signals.weather_ensemble.get_ensemble_forecast")
    def test_prob_in_range(self, mock_forecast):
        mock_forecast.return_value = {
            "ensemble": {
                "high_mean_f": 80.0, "high_std_f": 2.0,
                "n_sources": 3, "source_agreement": 0.9,
            }
        }
        result = prob_in_range("miami", "2026-02-28", 78.0, 82.0)
        # Within 1 std on each side, should be ~68%
        assert 0.5 < result["probability"] < 0.85

    @patch("signals.weather_ensemble.get_ensemble_forecast")
    def test_prob_narrow_range(self, mock_forecast):
        mock_forecast.return_value = {
            "ensemble": {
                "high_mean_f": 80.0, "high_std_f": 3.0,
                "n_sources": 3, "source_agreement": 0.9,
            }
        }
        # Narrow 1°F range far from mean
        result = prob_in_range("miami", "2026-02-28", 62.0, 63.0)
        assert result["probability"] < 0.01


class TestEnsembleFairValue:
    @patch("signals.weather_ensemble.get_ensemble_forecast")
    def test_above(self, mock_forecast):
        mock_forecast.return_value = {
            "ensemble": {
                "high_mean_f": 80.0, "high_std_f": 2.0,
                "n_sources": 3, "source_agreement": 0.9,
            }
        }
        result = ensemble_fair_value("miami", "2026-02-28", "above", 75.0)
        assert result is not None
        assert result["fair_value"] > 0.9  # Very likely above 75 if mean is 80

    @patch("signals.weather_ensemble.get_ensemble_forecast")
    def test_below(self, mock_forecast):
        mock_forecast.return_value = {
            "ensemble": {
                "high_mean_f": 80.0, "high_std_f": 2.0,
                "n_sources": 3, "source_agreement": 0.9,
            }
        }
        result = ensemble_fair_value("miami", "2026-02-28", "below", 85.0)
        assert result is not None
        assert result["fair_value"] > 0.9

    @patch("signals.weather_ensemble.get_ensemble_forecast")
    def test_between(self, mock_forecast):
        mock_forecast.return_value = {
            "ensemble": {
                "high_mean_f": 80.0, "high_std_f": 2.0,
                "n_sources": 3, "source_agreement": 0.9,
            }
        }
        result = ensemble_fair_value("miami", "2026-02-28", "between", 79.0, 81.0)
        assert result is not None
        assert 0.2 < result["fair_value"] < 0.5


class TestSourceHealth:
    def test_returns_all_sources(self):
        h = source_health()
        assert "open_meteo_ensemble" in h
        assert "pirate_weather" in h
        assert "tomorrow_io" in h
        assert "weatherapi" in h
        assert h["open_meteo_ensemble"]["configured"] is True

"""Tests for cross-platform election comparison."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from signals.cross_platform_elections import (
    cross_platform_divergence, _extract_search_terms, enrich_with_cross_platform,
)


def test_strong_divergence():
    result = cross_platform_divergence(0.60, 0.45)
    assert result["strength"] == "strong"
    assert result["confidence_multiplier"] == 1.3
    assert result["abs_divergence"] == 0.15


def test_moderate_divergence():
    result = cross_platform_divergence(0.55, 0.48)
    assert result["strength"] == "moderate"
    assert result["confidence_multiplier"] == 1.15


def test_no_divergence():
    result = cross_platform_divergence(0.50, 0.48)
    assert result["strength"] == "none"
    assert result["confidence_multiplier"] == 1.0


def test_missing_prices():
    result = cross_platform_divergence(None, 0.5)
    assert result["confidence_multiplier"] == 1.0
    assert result["strength"] == "none"


def test_extract_search_terms():
    terms = _extract_search_terms("Will Orbán win Hungary 2026?")
    assert len(terms) >= 1
    assert any("Hungary" in t or "Orbán" in t for t in terms)


def test_enrich_skips_non_election():
    signals = [{"title": "BTC price", "price": 0.5, "election_signal": False}]
    result = enrich_with_cross_platform(signals)
    assert "cross_platform" not in result[0]


def test_divergence_negative():
    """When Manifold is higher than Polymarket."""
    result = cross_platform_divergence(0.40, 0.55)
    assert result["divergence"] < 0
    assert result["strength"] == "strong"
    # When poly < manifold, multiplier is lower
    assert result["confidence_multiplier"] == 1.15

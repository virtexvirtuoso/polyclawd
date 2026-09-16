"""Tests for election polling scraper."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from signals.election_polls import (
    _detect_country, _detect_year, _parse_percentage, _recency_weight,
    _parse_polling_html, is_incumbent_favored, poll_vs_market,
    scan_election_markets, NO_DEMOCRATIC_TRANSITION,
)
from datetime import datetime, timedelta


def test_detect_country():
    assert _detect_country("Will Orbán win Hungary 2026?") == "hungary"
    assert _detect_country("Brazil presidential election") == "brazil"
    assert _detect_country("Maduro Venezuela") == "venezuela"
    assert _detect_country("Random market") is None


def test_detect_year():
    assert _detect_year("Hungary 2026 election") == 2026
    assert _detect_year("No year here") == 2026  # default


def test_parse_percentage():
    assert _parse_percentage("47.1%") == 47.1
    assert _parse_percentage("33") == 33.0
    assert _parse_percentage("") is None
    assert _parse_percentage("N/A") is None


def test_recency_weight():
    assert _recency_weight(datetime.now()) == 1.0
    assert _recency_weight(datetime.now() - timedelta(days=45)) == 0.7
    assert _recency_weight(datetime.now() - timedelta(days=120)) == 0.4
    assert _recency_weight(None) == 0.4


def test_incumbency_detection():
    assert is_incumbent_favored("Will Orbán win Hungary election?") is True
    assert is_incumbent_favored("Lula Brazil president 2026") is True
    assert is_incumbent_favored("Maduro Venezuela") is True
    assert is_incumbent_favored("Random market about sports") is False


def test_venezuela_special_case():
    result = poll_vs_market("Will Maduro lose Venezuela?", 0.15)
    assert result["confidence_multiplier"] == 1.2
    assert "no democratic transition" in result["polling_data"]["note"].lower()


MOCK_HTML = """
<html><body>
<table class="wikitable">
<tr><th>Pollster</th><th>Date</th><th>Fidesz</th><th>TISZA</th><th>DK</th></tr>
<tr><td>Medián</td><td>15 February 2026</td><td>35.0</td><td>42.0</td><td>8.0</td></tr>
<tr><td>Závecz</td><td>10 January 2026</td><td>33.0</td><td>40.0</td><td>9.0</td></tr>
</table>
</body></html>
"""


def test_parse_polling_html():
    result = _parse_polling_html(MOCK_HTML, "hungary", 2026)
    assert result["error"] is None
    assert result["poll_count"] >= 2
    assert len(result["polls"]) >= 2
    # Should have party data
    assert result["latest_polls"]  # not empty


def test_poll_vs_market_no_country():
    result = poll_vs_market("Random sports market", 0.5)
    assert result["confidence_multiplier"] == 1.0
    assert "Could not detect" in result["reasoning"]


def test_scan_election_markets():
    signals = [
        {"title": "Will Orbán win Hungary 2026 election?", "price": 0.65},
        {"title": "Bitcoin to 100k?", "price": 0.3},
    ]
    enriched = scan_election_markets(signals)
    assert enriched[0]["election_signal"] is True
    assert "polling_data" in enriched[0]
    assert enriched[1]["election_signal"] is False


def test_scan_non_election():
    signals = [{"title": "Will it rain tomorrow?", "price": 0.5}]
    enriched = scan_election_markets(signals)
    assert enriched[0]["election_signal"] is False
    assert "polling_data" not in enriched[0]

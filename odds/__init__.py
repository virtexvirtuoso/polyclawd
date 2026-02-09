"""
Odds comparison module for Polyclawd
- Vegas odds scraping (VegasInsider, The Odds API)
- Soccer futures edge detection
- Smart entity-based market matching
- PolyRouter unified API (7 platforms)
- Polymarket CLOB (orderbook + price history)
- Sophisticated edge math (Shin method, sharp book weighting)
"""

from .soccer_edge import find_soccer_edges, get_soccer_edge_summary
from .vegas_scraper import get_vegas_odds_with_fallback, VegasOdds, get_all_vegas_futures
from .smart_matcher import create_signature, signatures_match, match_markets
from .edge_math import (
    american_to_implied,
    implied_to_american,
    basic_no_vig,
    shin_no_vig,
    get_consensus_true_prob,
    calculate_edge,
    apply_edge_filters,
    combined_decision_score,
    EdgeFilter,
    SHARP_BOOKS,
    SOFT_BOOKS,
)
from .correlation import (
    scan_correlation_arb,
    detect_constraint_violations,
    group_markets_by_entity,
    extract_entities,
    MarketPair,
)
from . import polyrouter
from . import polymarket_clob
from . import correlation

__all__ = [
    "find_soccer_edges",
    "get_soccer_edge_summary", 
    "get_vegas_odds_with_fallback",
    "get_all_vegas_futures",
    "VegasOdds",
    "create_signature",
    "signatures_match",
    "match_markets",
    "polyrouter",
    "polymarket_clob",
    "correlation",
    # Edge math exports
    "american_to_implied",
    "implied_to_american",
    "basic_no_vig",
    "shin_no_vig",
    "get_consensus_true_prob",
    "calculate_edge",
    "apply_edge_filters",
    "combined_decision_score",
    "EdgeFilter",
    "SHARP_BOOKS",
    "SOFT_BOOKS",
    # Correlation exports
    "scan_correlation_arb",
    "detect_constraint_violations",
    "group_markets_by_entity",
    "extract_entities",
    "MarketPair",
]

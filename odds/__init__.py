"""
Odds comparison module for Polyclawd
- Vegas odds scraping (VegasInsider, The Odds API)
- Soccer futures edge detection
- Smart entity-based market matching
- PolyRouter unified API (7 platforms)
"""

from .soccer_edge import find_soccer_edges, get_soccer_edge_summary
from .vegas_scraper import get_vegas_odds_with_fallback, VegasOdds
from .smart_matcher import create_signature, signatures_match, match_markets
from . import polyrouter

__all__ = [
    "find_soccer_edges",
    "get_soccer_edge_summary", 
    "get_vegas_odds_with_fallback",
    "VegasOdds",
    "create_signature",
    "signatures_match",
    "match_markets",
    "polyrouter",
]

"""
Odds comparison module for Polyclawd
- Vegas odds scraping (VegasInsider, The Odds API)
- Soccer futures edge detection
"""

from .soccer_edge import find_soccer_edges, get_soccer_edge_summary
from .vegas_scraper import get_vegas_odds_with_fallback, VegasOdds

__all__ = [
    "find_soccer_edges",
    "get_soccer_edge_summary", 
    "get_vegas_odds_with_fallback",
    "VegasOdds"
]

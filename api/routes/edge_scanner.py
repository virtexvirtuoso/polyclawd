"""
Cross-Platform Edge Scanner Routes
"""

from fastapi import APIRouter, Query
from api.services.cross_platform_edge import scan_edges

router = APIRouter(prefix="/api/edge", tags=["Edge Scanner"])


@router.get("/scan")
async def get_cross_platform_edges(
    refresh: bool = Query(False, description="Force refresh, bypass 6h cache")
):
    """
    Scan all platforms for probability discrepancies.
    
    Compares:
    - Polymarket (prediction market)
    - Kalshi (regulated exchange)
    - Metaculus (crowd forecasts)
    
    Returns edges where platforms disagree by >5%.
    Results are cached for 6 hours. Use ?refresh=true to force fresh data.
    """
    return await scan_edges(force_refresh=refresh)


@router.get("/topics")
async def get_tracked_topics():
    """List all topics being tracked for cross-platform comparison."""
    from api.services.cross_platform_edge import TOPIC_KEYWORDS
    return {
        "topics": list(TOPIC_KEYWORDS.keys()),
        "keywords": TOPIC_KEYWORDS
    }

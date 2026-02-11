"""
Cross-Platform Edge Scanner Routes

Includes:
- /scan - Cross-platform edge detection
- /calculate - Sophisticated edge calculation with Shin method
- /topics - Tracked topic keywords
"""

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from api.services.cross_platform_edge import scan_edges

router = APIRouter(prefix="/api/edge", tags=["Edge Scanner"])


# ============================================================================
# Request/Response Models
# ============================================================================

class BookmakerOdds(BaseModel):
    """Bookmaker odds for a single book."""
    book: str
    fav_odds: Optional[int] = None
    dog_odds: Optional[int] = None
    home_odds: Optional[int] = None
    away_odds: Optional[int] = None
    yes_odds: Optional[int] = None
    no_odds: Optional[int] = None


class EdgeCalculateRequest(BaseModel):
    """Request body for edge calculation."""
    bookmaker_odds: List[BookmakerOdds]
    market_price: float  # Current market price (0-1 or 0-100)
    outcome: str = "yes"  # Which outcome to analyze: yes/no/home/away
    confidence: Optional[float] = None  # Optional confidence score (0-100)
    volume: Optional[float] = None  # Market volume in USD
    hours_to_resolution: Optional[float] = None


# ============================================================================
# Existing Routes
# ============================================================================

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


# ============================================================================
# New Edge Calculation Routes
# ============================================================================

@router.post("/calculate")
async def calculate_edge(request: EdgeCalculateRequest):
    """
    Calculate edge using sophisticated methods.
    
    Features:
    - **Shin method**: Better vig removal for heavy favorites (-300 or worse)
    - **Sharp book prioritization**: Pinnacle, Circa > DraftKings, FanDuel
    - **Kelly sizing**: Full, half, and quarter Kelly recommendations
    - **Edge filters**: Time decay, volume, confidence thresholds
    
    Request body:
    ```json
    {
        "bookmaker_odds": [
            {"book": "pinnacle", "yes_odds": -250, "no_odds": +200},
            {"book": "draftkings", "yes_odds": -280, "no_odds": +220}
        ],
        "market_price": 0.72,
        "outcome": "yes",
        "confidence": 65,
        "volume": 250000,
        "hours_to_resolution": 48
    }
    ```
    
    Returns edge analysis with Kelly sizing and filter results.
    """
    try:
        from odds.edge_math import (
            get_consensus_true_prob,
            calculate_edge,
            apply_edge_filters,
            combined_decision_score,
            EdgeFilter,
            SHARP_BOOKS,
            SOFT_BOOKS
        )
        
        # Convert Pydantic models to dicts
        odds_list = [o.model_dump() for o in request.bookmaker_odds]
        
        # Get consensus true probability from bookmaker odds
        true_prob = get_consensus_true_prob(odds_list, request.outcome)
        
        if true_prob is None:
            raise HTTPException(
                status_code=400,
                detail="Could not calculate true probability from provided odds"
            )
        
        # Normalize market price (accept both 0-1 and 0-100)
        market_price = request.market_price
        if market_price > 1:
            market_price = market_price / 100
        
        # Calculate edge and Kelly sizing
        edge_result = calculate_edge(true_prob, market_price)
        
        # Identify which books were used
        sharp_books_used = []
        soft_books_used = []
        for o in odds_list:
            book = o.get('book', '').lower()
            if book in SHARP_BOOKS:
                sharp_books_used.append(book)
            elif book in SOFT_BOOKS:
                soft_books_used.append(book)
        
        result = {
            "true_probability": {
                "value": round(true_prob * 100, 2),
                "source": "sharp_consensus" if sharp_books_used else "soft_average",
                "sharp_books_used": sharp_books_used,
                "soft_books_used": soft_books_used
            },
            "edge": edge_result,
        }
        
        # Apply filters if we have the required data
        if request.confidence is not None:
            confidence = request.confidence
            volume = request.volume or 0
            hours = request.hours_to_resolution or 0
            
            filter_result = apply_edge_filters(
                edge_result["edge_pct"],
                confidence,
                volume,
                hours,
                EdgeFilter()
            )
            result["filters"] = filter_result
            
            # Add combined decision score
            decision = combined_decision_score(edge_result["edge_pct"], confidence)
            result["decision"] = decision
        
        return result
        
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"Edge math module not available: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Edge calculation failed: {e}")


@router.get("/calculate/example")
async def get_edge_example():
    """
    Show an example edge calculation with Shin method.
    
    Demonstrates the difference between basic and Shin vig removal
    for a heavy favorite line.
    """
    from odds.edge_math import (
        american_to_implied,
        basic_no_vig,
        shin_no_vig,
        calculate_edge,
        SHARP_BOOKS
    )
    
    # Example: Heavy favorite at -350 / +280
    fav_odds = -350
    dog_odds = 280
    market_price = 0.78  # Polymarket showing 78% YES
    
    # Convert to implied probabilities
    p_fav = american_to_implied(fav_odds)
    p_dog = american_to_implied(dog_odds)
    
    # Compare methods
    basic_fav, basic_dog = basic_no_vig(p_fav, p_dog)
    shin_fav, shin_dog = shin_no_vig(p_fav, p_dog)
    
    return {
        "example_line": {
            "favorite_odds": fav_odds,
            "underdog_odds": dog_odds,
            "implied_fav": round(p_fav * 100, 2),
            "implied_dog": round(p_dog * 100, 2),
            "total_implied": round((p_fav + p_dog) * 100, 2),
            "vig_pct": round((p_fav + p_dog - 1) * 100, 2)
        },
        "vig_removal_comparison": {
            "basic_method": {
                "true_fav": round(basic_fav * 100, 2),
                "true_dog": round(basic_dog * 100, 2),
                "note": "Distributes vig evenly - less accurate for unbalanced lines"
            },
            "shin_method": {
                "true_fav": round(shin_fav * 100, 2),
                "true_dog": round(shin_dog * 100, 2),
                "note": "Accounts for informed bettor bias - better for heavy favorites"
            }
        },
        "edge_vs_market": {
            "market_price": round(market_price * 100, 2),
            "shin_true_prob": round(shin_fav * 100, 2),
            "edge_pct": round((shin_fav - market_price) * 100, 2),
            "edge_direction": "YES" if shin_fav > market_price else "NO"
        },
        "kelly_sizing": calculate_edge(shin_fav, market_price),
        "sharp_books": SHARP_BOOKS,
        "note": "Shin method is preferred for lines with >75% implied probability"
    }


@router.get("/sharp-books")
async def get_sharp_books():
    """Get the list of sharp and soft books used for prioritization."""
    from odds.edge_math import SHARP_BOOKS, SOFT_BOOKS
    
    return {
        "sharp_books": {
            "list": SHARP_BOOKS,
            "description": "Low vig (~2-3%), efficient markets. Pinnacle is the gold standard.",
            "priority": "HIGH - Use these for true probability estimation"
        },
        "soft_books": {
            "list": SOFT_BOOKS,
            "description": "Higher vig (~5-8%), recreational markets. Good for finding +EV bets.",
            "priority": "MEDIUM - Use as fallback when no sharp lines available"
        },
        "methodology": (
            "Sharp books are prioritized because they have lower margins and attract "
            "professional bettors, making their lines more accurate. When calculating "
            "true probability, we use sharp book consensus first, then fall back to "
            "averaging soft book lines."
        )
    }

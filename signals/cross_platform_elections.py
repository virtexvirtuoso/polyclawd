"""Cross-platform election price comparison (Polymarket vs Manifold Markets).

Compares election market prices across platforms to detect divergences
and provide confidence signals for the paper trading system.
"""

from loguru import logger
import logging
from typing import Optional

import httpx


MANIFOLD_API = "https://api.manifold.markets/v0"


def get_manifold_price(search_term: str) -> dict:
    """Search Manifold Markets for a matching market and return its probability.
    
    Args:
        search_term: Keywords to search for (e.g., "Hungary election 2026")
        
    Returns:
        dict with: found, question, probability, url, error
    """
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                f"{MANIFOLD_API}/search-markets",
                params={"term": search_term, "limit": 5},
            )
            resp.raise_for_status()
            markets = resp.json()
    except Exception as e:
        logger.error("Manifold API error for '{}': {}", search_term, e)
        return {
            "found": False,
            "question": None,
            "probability": None,
            "url": None,
            "error": str(e),
        }
    
    if not markets:
        return {
            "found": False,
            "question": None,
            "probability": None,
            "url": None,
            "error": "No matching markets found",
        }
    
    # Use the first (best) match
    best = markets[0]
    prob = best.get("probability")
    
    return {
        "found": True,
        "question": best.get("question", ""),
        "probability": round(prob, 4) if prob is not None else None,
        "url": best.get("url", ""),
        "id": best.get("id", ""),
        "error": None,
    }


def cross_platform_divergence(poly_price: float, manifold_price: float) -> dict:
    """Calculate divergence between Polymarket and Manifold prices.
    
    Args:
        poly_price: Polymarket YES price (0-1)
        manifold_price: Manifold probability (0-1)
        
    Returns:
        dict with: divergence, abs_divergence, strength, confidence_multiplier, reasoning
    """
    if poly_price is None or manifold_price is None:
        return {
            "divergence": 0,
            "abs_divergence": 0,
            "strength": "none",
            "confidence_multiplier": 1.0,
            "reasoning": "Missing price data",
        }
    
    divergence = poly_price - manifold_price
    abs_div = abs(divergence)
    
    if abs_div > 0.10:
        strength = "strong"
        # Both platforms agree the YES price is similar range → strong signal
        # If Polymarket YES is higher than Manifold → Polymarket overpriced → good for NO
        multiplier = 1.3 if divergence > 0 else 1.15
        reasoning = f"Strong divergence: Poly {poly_price:.1%} vs Manifold {manifold_price:.1%} (Δ{divergence:+.1%})"
    elif abs_div > 0.05:
        strength = "moderate"
        multiplier = 1.15 if divergence > 0 else 1.05
        reasoning = f"Moderate divergence: Poly {poly_price:.1%} vs Manifold {manifold_price:.1%} (Δ{divergence:+.1%})"
    else:
        strength = "none"
        multiplier = 1.0
        reasoning = f"Markets agree: Poly {poly_price:.1%} vs Manifold {manifold_price:.1%} (Δ{divergence:+.1%})"
    
    return {
        "divergence": round(divergence, 4),
        "abs_divergence": round(abs_div, 4),
        "strength": strength,
        "confidence_multiplier": multiplier,
        "reasoning": reasoning,
    }


def _extract_search_terms(market_title: str) -> list[str]:
    """Extract search terms from a market title for Manifold lookup."""
    terms = []
    title_lower = market_title.lower()
    
    # Try the full title first
    terms.append(market_title)
    
    # Try key phrases
    country_keywords = {
        "hungary": ["Hungary election", "Orbán", "TISZA Hungary"],
        "brazil": ["Brazil election", "Lula president", "Bolsonaro 2026"],
        "venezuela": ["Venezuela Maduro", "Venezuela election"],
    }
    
    for country, keywords in country_keywords.items():
        if country in title_lower or any(k.lower() in title_lower for k in keywords):
            terms.extend(keywords)
            break
    
    return terms[:3]  # Max 3 searches


def enrich_with_cross_platform(signals: list) -> list:
    """Add Manifold Markets comparison to election signals.
    
    Expects signals to already have election_signal=True from scan_election_markets.
    """
    for signal in signals:
        if not signal.get("election_signal"):
            continue
        
        title = signal.get("title", "") or signal.get("question", "") or ""
        poly_price = signal.get("price", signal.get("yes_price"))
        
        if isinstance(poly_price, str):
            try:
                poly_price = float(poly_price)
            except (ValueError, TypeError):
                poly_price = None
        
        if not title or poly_price is None:
            continue
        
        # Search Manifold
        search_terms = _extract_search_terms(title)
        manifold_result = None
        
        for term in search_terms:
            result = get_manifold_price(term)
            if result["found"]:
                manifold_result = result
                break
        
        if manifold_result and manifold_result["probability"] is not None:
            divergence = cross_platform_divergence(poly_price, manifold_result["probability"])
            signal["cross_platform"] = {
                "manifold": manifold_result,
                "divergence": divergence,
            }
            logger.info("Cross-platform check: {} → Manifold={} div={} mult={}",
                       title[:50],
                       f"{manifold_result['probability']:.1%}" if manifold_result['probability'] else "N/A",
                       divergence["strength"],
                       divergence["confidence_multiplier"])
        else:
            signal["cross_platform"] = {
                "manifold": manifold_result or {"found": False, "error": "No search terms matched"},
                "divergence": {"confidence_multiplier": 1.0, "strength": "none", "reasoning": "No Manifold market found"},
            }
    
    return signals

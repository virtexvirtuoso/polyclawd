"""
Sophisticated edge calculation with Shin method and sharp book weighting.

Key features:
- Sharp book prioritization (Pinnacle, Circa, etc.)
- Shin method for unbalanced lines (heavy favorites)
- Kelly sizing recommendations
- Edge filters with time decay
"""
import math
from typing import Tuple, List, Optional
from dataclasses import dataclass

# Sharp books to prioritize (low vig, ~2-3%)
SHARP_BOOKS = ['pinnacle', 'pinnaclesports', 'circa', 'betcris', 'bookmaker']
SOFT_BOOKS = ['draftkings', 'fanduel', 'betmgm', 'caesars', 'pointsbet', 'barstool']


def american_to_implied(odds: int) -> float:
    """Convert American odds to implied probability.
    
    Examples:
        -200 → 0.667 (66.7%)
        +150 → 0.400 (40.0%)
    """
    if odds < 0:
        return abs(odds) / (abs(odds) + 100)
    return 100 / (odds + 100)


def implied_to_american(prob: float) -> int:
    """Convert implied probability back to American odds.
    
    Examples:
        0.667 → -200
        0.400 → +150
    """
    if prob <= 0 or prob >= 1:
        return 0
    if prob > 0.5:
        return int(-100 * prob / (1 - prob))
    return int(100 * (1 - prob) / prob)


def basic_no_vig(p_a: float, p_b: float) -> Tuple[float, float]:
    """Basic vig removal - assumes even distribution of vig.
    
    Works well for balanced lines (close to 50/50).
    """
    total = p_a + p_b
    if total == 0:
        return 0.5, 0.5
    return p_a / total, p_b / total


def shin_no_vig(p_fav: float, p_dog: float) -> Tuple[float, float]:
    """
    Shin method for unbalanced lines.
    
    Better than basic no-vig for heavy favorites (-300 or worse).
    The Shin model assumes bookmakers set odds to protect against 
    informed bettors (insiders), which creates asymmetric vig distribution.
    
    Returns true probabilities (favorite, underdog).
    
    Reference: Shin (1991, 1992, 1993) papers on bookmaker behavior.
    """
    Z = p_fav + p_dog  # overround / total implied probability
    
    if Z <= 1.0:  # No vig case
        return p_fav, p_dog
    
    # Shin factor calculation
    # For a two-outcome market, we solve for s (informed bettor fraction)
    # using: sum(sqrt(p_i^2 + s*(1-s)) - s) = 1
    try:
        # Quadratic approximation for 2-outcome markets
        discriminant = p_fav**2 + p_dog**2 - (Z**2 - 2*Z + 2)
        if discriminant < 0:
            discriminant = 0
        s = (Z - math.sqrt(max(0, 2 - (1 - p_fav)**2 - (1 - p_dog)**2))) / (2 * Z - 2)
        s = max(0, min(s, 0.5))  # Clamp to valid range [0, 0.5]
    except (ValueError, ZeroDivisionError):
        # Fallback to simple approximation
        s = (Z - 1) / 2
    
    # Calculate true probabilities using Shin formula
    denom = 1 - 2 * s
    if abs(denom) < 0.001:  # Avoid division by near-zero
        return basic_no_vig(p_fav, p_dog)
    
    true_fav = (p_fav - s) / denom
    true_dog = (p_dog - s) / denom
    
    # Ensure valid probabilities and normalize
    true_fav = max(0, min(1, true_fav))
    true_dog = max(0, min(1, true_dog))
    
    total = true_fav + true_dog
    if total > 0:
        true_fav /= total
        true_dog /= total
    
    return true_fav, true_dog


def get_consensus_true_prob(bookmaker_odds: List[dict], outcome: str) -> Optional[float]:
    """
    Get consensus true probability from multiple bookmakers.
    Prioritizes sharp books (Pinnacle, Circa, etc.).
    
    Args:
        bookmaker_odds: List of dicts with structure:
            [{"book": "pinnacle", "fav_odds": -300, "dog_odds": +250}, ...]
            Alternative keys: "home_odds"/"away_odds" or "yes_odds"/"no_odds"
        outcome: Which outcome to get probability for:
            'yes', 'home', 'favorite' → first outcome
            'no', 'away', 'underdog' → second outcome
    
    Returns:
        Consensus true probability (0-1), or None if no valid data.
    """
    sharp_probs = []
    soft_probs = []
    
    for book_data in bookmaker_odds:
        book = book_data.get('book', '').lower()
        
        # Support multiple key formats
        fav = (book_data.get('fav_odds') or 
               book_data.get('home_odds') or 
               book_data.get('yes_odds'))
        dog = (book_data.get('dog_odds') or 
               book_data.get('away_odds') or 
               book_data.get('no_odds'))
        
        if not fav or not dog:
            continue
        
        try:
            p_fav = american_to_implied(int(fav))
            p_dog = american_to_implied(int(dog))
        except (ValueError, TypeError):
            continue
        
        # Use Shin for heavy favorites (>75% implied)
        if p_fav > 0.75 or p_dog > 0.75:
            true_fav, true_dog = shin_no_vig(p_fav, p_dog)
        else:
            true_fav, true_dog = basic_no_vig(p_fav, p_dog)
        
        # Select appropriate probability based on outcome
        is_first_outcome = outcome.lower() in ['yes', 'home', 'favorite', 'over', 'fav']
        prob = true_fav if is_first_outcome else true_dog
        
        # Categorize by book type
        if book in SHARP_BOOKS:
            sharp_probs.append(prob)
        elif book in SOFT_BOOKS or book:  # Any named book goes to soft
            soft_probs.append(prob)
    
    # Prioritize sharp book consensus
    if sharp_probs:
        return sum(sharp_probs) / len(sharp_probs)
    elif soft_probs:
        return sum(soft_probs) / len(soft_probs)
    return None


def calculate_edge(true_prob: float, market_price: float) -> dict:
    """
    Calculate edge and Kelly sizing for a betting opportunity.
    
    Args:
        true_prob: Estimated true probability (0-1)
        market_price: Current market price (0-1)
    
    Returns:
        Dict with:
        - true_prob: True probability (%)
        - market_price: Market price (%)
        - edge_pct: Edge percentage (positive = YES, negative = NO)
        - edge_direction: "YES" or "NO"
        - kelly_full: Full Kelly fraction (%)
        - kelly_half: Half Kelly fraction (%) - recommended
        - kelly_quarter: Quarter Kelly fraction (%) - conservative
        - ev_per_dollar: Expected value per dollar bet (cents)
    """
    edge = true_prob - market_price
    
    # Kelly fractions for each side
    if edge > 0:  # Bet YES - we think true prob > market price
        # Kelly = edge / (1 - market_price)
        # This is the fraction of bankroll to bet on YES
        kelly_yes = edge / (1 - market_price) if market_price < 1 else 0
        kelly_no = 0
    else:  # Bet NO - we think true prob < market price
        # For NO bets, we're betting on (1 - true_prob) at price (1 - market_price)
        # Kelly = |edge| / market_price
        kelly_yes = 0
        kelly_no = abs(edge) / market_price if market_price > 0 else 0
    
    kelly_raw = max(kelly_yes, kelly_no)
    
    return {
        "true_prob": round(true_prob * 100, 2),
        "market_price": round(market_price * 100, 2),
        "edge_pct": round(edge * 100, 2),
        "edge_direction": "YES" if edge > 0 else "NO",
        "kelly_full": round(kelly_raw * 100, 2),
        "kelly_half": round(kelly_raw * 50, 2),
        "kelly_quarter": round(kelly_raw * 25, 2),
        "ev_per_dollar": round(abs(edge) * 100, 2)
    }


@dataclass
class EdgeFilter:
    """Configuration for edge quality filters."""
    min_edge_pct: float = 2.0       # Minimum raw edge (%)
    min_volume: float = 100000      # Minimum market volume ($)
    min_confidence: float = 40      # Minimum confidence score (0-100)
    min_adjusted_edge: float = 3.0  # Minimum edge × confidence/100 threshold
    edge_time_decay: bool = True    # Apply higher thresholds far from resolution


def apply_edge_filters(
    edge_pct: float,
    confidence: float,
    volume: float,
    hours_to_resolution: float,
    filters: EdgeFilter = None
) -> dict:
    """
    Apply quality filters to edge signals.
    
    The "adjusted edge" metric combines raw edge with confidence:
        adjusted_edge = |edge_pct| × (confidence / 100)
    
    This ensures we only bet when BOTH edge AND confidence are sufficient.
    
    Args:
        edge_pct: Raw edge percentage (can be negative)
        confidence: Signal confidence (0-100)
        volume: Market volume in USD
        hours_to_resolution: Hours until market resolves
        filters: EdgeFilter configuration (uses defaults if None)
    
    Returns:
        Dict with filter results and reasoning.
    """
    if filters is None:
        filters = EdgeFilter()
    
    # Time-based edge threshold adjustment
    # Markets far from resolution need higher edges (more uncertainty)
    if filters.edge_time_decay and hours_to_resolution and hours_to_resolution > 0:
        if hours_to_resolution > 168:  # >1 week
            min_edge = filters.min_edge_pct * 1.5
            time_note = ">1 week out, requiring 1.5x edge"
        elif hours_to_resolution > 72:  # >3 days
            min_edge = filters.min_edge_pct * 1.3
            time_note = ">3 days out, requiring 1.3x edge"
        elif hours_to_resolution > 24:  # >1 day
            min_edge = filters.min_edge_pct * 1.2
            time_note = ">1 day out, requiring 1.2x edge"
        else:
            min_edge = filters.min_edge_pct
            time_note = "<24h, using base edge"
    else:
        min_edge = filters.min_edge_pct
        time_note = "No time decay"
    
    # Combined quality metric
    adjusted_edge = abs(edge_pct) * (confidence / 100)
    
    # Check all filters
    passes = (
        abs(edge_pct) >= min_edge and
        confidence >= filters.min_confidence and
        volume >= filters.min_volume and
        adjusted_edge >= filters.min_adjusted_edge
    )
    
    return {
        "passes_filter": passes,
        "min_edge_required": round(min_edge, 2),
        "adjusted_edge": round(adjusted_edge, 2),
        "time_note": time_note,
        "reasons": [] if passes else _get_filter_reasons(
            edge_pct, confidence, volume, min_edge, adjusted_edge, filters
        )
    }


def _get_filter_reasons(edge, conf, vol, min_edge, adj_edge, filters):
    """Generate human-readable filter rejection reasons."""
    reasons = []
    if abs(edge) < min_edge:
        reasons.append(f"Edge {abs(edge):.1f}% < {min_edge:.1f}% minimum")
    if conf < filters.min_confidence:
        reasons.append(f"Confidence {conf:.0f} < {filters.min_confidence} minimum")
    if vol < filters.min_volume:
        reasons.append(f"Volume ${vol:,.0f} < ${filters.min_volume:,.0f} minimum")
    if adj_edge < filters.min_adjusted_edge:
        reasons.append(f"Adjusted edge {adj_edge:.1f}% < {filters.min_adjusted_edge}% threshold")
    return reasons


def combined_decision_score(edge_pct: float, confidence: float) -> dict:
    """
    Combined edge + confidence decision metric.
    
    The adjusted edge ensures we only bet when BOTH metrics are strong:
        adjusted_edge = |edge_pct| × (confidence / 100)
    
    Decision thresholds:
        - > 5.0: STRONG - High conviction bet
        - > 3.0: MODERATE - Standard bet
        - ≤ 3.0: WEAK - Skip or reduce size
    
    Args:
        edge_pct: Edge percentage (positive = YES, negative = NO)
        confidence: Confidence score (0-100)
    
    Returns:
        Dict with decision metrics.
    """
    adjusted_edge = abs(edge_pct) * (confidence / 100)
    
    if adjusted_edge > 5.0:
        strength = "strong"
        should_bet = True
    elif adjusted_edge > 3.0:
        strength = "moderate"
        should_bet = True
    else:
        strength = "weak"
        should_bet = False
    
    return {
        "adjusted_edge": round(adjusted_edge, 2),
        "should_bet": should_bet,
        "bet_direction": "YES" if edge_pct > 0 else "NO",
        "strength": strength,
        "kelly_multiplier": min(1.0, adjusted_edge / 5.0)  # Scale Kelly by strength
    }


# ============================================================================
# Utility Functions
# ============================================================================

def calculate_vig(p_a: float, p_b: float) -> float:
    """Calculate bookmaker vig/margin from implied probabilities."""
    return (p_a + p_b - 1) * 100


def estimate_sharp_line(market_odds: List[dict]) -> Optional[dict]:
    """
    Estimate the "true" sharp line from available bookmaker odds.
    
    Returns the most likely true probability based on sharp book consensus,
    with fallback to soft book average.
    """
    yes_prob = get_consensus_true_prob(market_odds, "yes")
    no_prob = get_consensus_true_prob(market_odds, "no")
    
    if yes_prob is None:
        return None
    
    # If we only got one side, calculate the other
    if no_prob is None:
        no_prob = 1 - yes_prob
    
    return {
        "true_yes": round(yes_prob * 100, 2),
        "true_no": round(no_prob * 100, 2),
        "american_yes": implied_to_american(yes_prob),
        "american_no": implied_to_american(no_prob)
    }

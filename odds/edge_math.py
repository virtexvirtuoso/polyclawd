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
SHARP_BOOKS = ["pinnacle", "pinnaclesports", "circa", "betcris", "bookmaker"]
SOFT_BOOKS = ["draftkings", "fanduel", "betmgm", "caesars", "pointsbet", "barstool"]


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
    Shin (1992) devig for 2-outcome markets. Solves the proper Shin equation

        sqrt(z^2 + 4(1-z)*p_fav^2/s) + sqrt(z^2 + 4(1-z)*p_dog^2/s) = 2

    for the informed-bettor fraction z in [0, 0.999] via Newton's method,
    then returns true probabilities (favorite, underdog) summing to 1.

    Matches sports_edge_common.devig_shin for n=2. Better than basic no-vig
    for heavy favorites because it removes the vig asymmetrically.

    Returns true probabilities (favorite, underdog).

    Reference: Shin (1991, 1992, 1993). Verified 2026-08-23.
    """
    s = p_fav + p_dog  # overround
    if s <= 1.0:  # No vig
        return p_fav, p_dog

    # Newton solve for z. For n=2: f(z) = r_f + r_d - 2 = 0
    # where r_i = sqrt(z^2 + 4(1-z)*p_i^2/s)
    # f'(z) = (2z - 4*p_fav^2/s)/(2*r_f) + (2z - 4*p_dog^2/s)/(2*r_d)
    z = 0.0
    for _ in range(60):
        r_f = math.sqrt(z * z + 4.0 * (1.0 - z) * p_fav * p_fav / s)
        r_d = math.sqrt(z * z + 4.0 * (1.0 - z) * p_dog * p_dog / s)
        f = r_f + r_d - 2.0
        if r_f > 0 and r_d > 0:
            df = ((2.0 * z - 4.0 * p_fav * p_fav / s) / (2.0 * r_f)
                  + (2.0 * z - 4.0 * p_dog * p_dog / s) / (2.0 * r_d))
        else:
            df = -1.0
        if abs(df) < 1e-12:
            break
        z_new = z - f / df
        z = max(0.0, min(0.999, z_new))
        if abs(f) < 1e-10:
            break

    # True probabilities: pi_i = (sqrt(z^2 + 4(1-z)*p_i^2/s) - z) / (2*(1-z))
    denom = 2.0 * (1.0 - z)
    if abs(denom) < 1e-6:
        return basic_no_vig(p_fav, p_dog)

    r_f = math.sqrt(z * z + 4.0 * (1.0 - z) * p_fav * p_fav / s)
    r_d = math.sqrt(z * z + 4.0 * (1.0 - z) * p_dog * p_dog / s)
    true_fav = (r_f - z) / denom
    true_dog = (r_d - z) / denom

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
        book = book_data.get("book", "").lower()

        # Support multiple key formats
        fav = book_data.get("fav_odds") or book_data.get("home_odds") or book_data.get("yes_odds")
        dog = book_data.get("dog_odds") or book_data.get("away_odds") or book_data.get("no_odds")

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
        is_first_outcome = outcome.lower() in ["yes", "home", "favorite", "over", "fav"]
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
        "ev_per_dollar": round(abs(edge) * 100, 2),
    }


@dataclass
class EdgeFilter:
    """Configuration for edge quality filters."""

    min_edge_pct: float = 2.0  # Minimum raw edge (%)
    min_volume: float = 100000  # Minimum market volume ($)
    min_confidence: float = 40  # Minimum confidence score (0-100)
    min_adjusted_edge: float = 3.0  # Minimum edge × confidence/100 threshold
    edge_time_decay: bool = True  # Apply higher thresholds far from resolution


def apply_edge_filters(
    edge_pct: float, confidence: float, volume: float, hours_to_resolution: float, filters: EdgeFilter = None
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
        abs(edge_pct) >= min_edge
        and confidence >= filters.min_confidence
        and volume >= filters.min_volume
        and adjusted_edge >= filters.min_adjusted_edge
    )

    return {
        "passes_filter": passes,
        "min_edge_required": round(min_edge, 2),
        "adjusted_edge": round(adjusted_edge, 2),
        "time_note": time_note,
        "reasons": []
        if passes
        else _get_filter_reasons(edge_pct, confidence, volume, min_edge, adjusted_edge, filters),
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
        "kelly_multiplier": min(1.0, adjusted_edge / 5.0),  # Scale Kelly by strength
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
        "american_no": implied_to_american(no_prob),
    }


# ============================================================================
# Cross-Platform Arb Fee Adjustments
# ============================================================================

# Cross-platform fee math is delegated to the single source of truth in
# execution.fee_model (verified 2026 Polymarket per-category + Kalshi schedules).
# fee_model is pure math with no I/O, so importing it here introduces no coupling
# to the execution layer's side effects and no import cycle.
from execution.fee_model import taker_fee_fraction


def net_arb_edge(
    buy_price: float,
    sell_price: float,
    buy_platform: str,
    sell_platform: str,
    estimated_slippage: float = 0.005,
    category: str = "politics",
) -> dict:
    """
    Compute fee-adjusted arbitrage edge for cross-platform CLOB arb.

    Both Polymarket and Kalshi are order-book exchanges with no embedded vig in
    mid-prices (unlike sportsbook odds). Each leg is a real taker fill, so each
    incurs its own taker fee at its own fill price — there is NO "fee on profit"
    (that was the obsolete pre-2026 model). Fees are symmetric around p=0.5 and
    shrink to ~0 at the extremes:
      - Polymarket: TAKER_RATE[category] * p * (1-p); world-events = 0
      - Kalshi: 0.07 * p * (1-p) (general schedule)

    Args:
        buy_price: Price paid on the buy side (fraction of 1.0, e.g. 0.70)
        sell_price: Price received on the sell side (fraction of 1.0, e.g. 0.75)
        buy_platform: "polymarket" or "kalshi"
        sell_platform: "polymarket" or "kalshi"
        estimated_slippage: Fraction of contract value lost to spread-crossing (default 0.005 = 0.5pp)
        category: Polymarket fee category for the Polymarket leg(s) (default "politics",
                  since cross-arb pairs are politics/macro/tech). Ignored for Kalshi legs.

    Returns:
        dict with keys:
        - gross_return: raw return fraction (e.g. 0.0714 for 7.14%)
        - net_return: return after fees & slippage
        - buy_fee: taker fee fraction on the buy leg (at buy_price)
        - sell_fee: taker fee fraction on the sell/close leg (at sell_price)
        - slippage: slippage fraction deducted
        - net_edge_pp: net edge in percentage points (e.g. 4.2 for 4.2pp)
    """
    # Gross return
    gross_return = (sell_price / buy_price) - 1.0 if buy_price > 0 else 0.0

    # Each leg is a taker fill at its own price (real 2026 fee schedules).
    buy_fee = taker_fee_fraction(buy_price, buy_platform, category)
    sell_fee = taker_fee_fraction(sell_price, sell_platform, category)

    # Slippage deduction
    slippage = estimated_slippage

    # Net return after all costs
    net_return = gross_return - buy_fee - sell_fee - slippage

    # Net edge in percentage points
    net_edge_pp = net_return * 100.0

    return {
        "gross_return": round(gross_return, 6),
        "net_return": round(net_return, 6),
        "buy_fee": round(buy_fee, 6),
        "sell_fee": round(sell_fee, 6),
        "slippage": round(slippage, 6),
        "net_edge_pp": round(net_edge_pp, 2),
    }

"""Fee correctness for the consensus-disagreement (devig vs sportsbook) signal.

The old model charged a flat 2% Polymarket "winner fee" on the entry price
(POLY_WINNER_FEE = 0.02) — a settlement fee that no longer exists (Polymarket is
0% on winnings). The real cost of entering is the per-category taker fee at the
fill price. These are sports markets, so category = "sports" (rate 0.03):
    fee_fraction = 0.03 * p * (1 - p)
"""

from execution.fee_model import taker_fee_fraction
from signals.consensus_disagreement import _compute_fee_adjusted_disagreement


def test_fee_is_real_sports_taker_not_flat_winner_fee():
    # YES, poly_price 0.50 -> entry 0.50
    # real: 0.03 * 0.5 * 0.5 = 0.0075 -> 0.75pp   (old flat: 0.5*0.02 -> 1.0pp)
    _, fee_pp = _compute_fee_adjusted_disagreement(5.0, 0.50, "YES")
    assert fee_pp == round(taker_fee_fraction(0.50, "polymarket", "sports") * 100.0, 2)
    assert fee_pp == 0.75


def test_fee_adjusted_subtracts_real_fee():
    fee_adj, fee_pp = _compute_fee_adjusted_disagreement(5.0, 0.50, "YES")
    assert abs(fee_adj - (5.0 - fee_pp)) < 1e-9


def test_no_direction_uses_one_minus_price():
    # NO, poly_price 0.80 -> entry 0.20 -> 0.03 * 0.2 * 0.8 = 0.0048 -> 0.48pp
    _, fee_pp = _compute_fee_adjusted_disagreement(5.0, 0.80, "NO")
    assert fee_pp == round(taker_fee_fraction(0.20, "polymarket", "sports") * 100.0, 2)
    assert fee_pp == 0.48


def test_fee_shrinks_at_price_extremes():
    # near-certain favorite costs far less to enter than a coin-flip
    _, fee_mid = _compute_fee_adjusted_disagreement(5.0, 0.50, "YES")
    _, fee_edge = _compute_fee_adjusted_disagreement(5.0, 0.95, "YES")
    assert fee_edge < fee_mid

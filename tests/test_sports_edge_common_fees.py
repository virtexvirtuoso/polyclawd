"""Fee correctness for the devig executable-edge net-of-fee gate (strategy #1).

`fee_adjusted_edge` must net the executable edge against the REAL Polymarket
sports taker fee at the fill price (0.03 * p * (1-p), 0% on winnings), not the
obsolete flat 2%-on-winnings (POLY_WINNER_FEE * executable_price).
"""

from types import SimpleNamespace

from execution.fee_model import taker_fee_fraction
from odds.sports_edge_common import fee_adjusted_edge


def test_fee_adjusted_edge_uses_real_sports_taker_fee():
    edge = SimpleNamespace(executable_edge=0.05, executable_price=0.55)
    # real: 0.05 - 0.03*0.55*0.45 = 0.042575  (old flat: 0.05 - 0.02*0.55 = 0.039)
    expected = 0.05 - taker_fee_fraction(0.55, "polymarket", "sports")
    assert fee_adjusted_edge(edge) == expected
    assert fee_adjusted_edge(edge) > 0.039  # strictly better than the old over-charge


def test_fee_adjusted_edge_none_when_not_enriched():
    assert fee_adjusted_edge(SimpleNamespace(executable_edge=None, executable_price=None)) is None

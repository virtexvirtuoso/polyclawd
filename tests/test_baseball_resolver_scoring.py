"""Pure win/pnl scorer for baseball shadow resolution.

Resolves each trade against ITS OWN Polymarket market: win = the trade's chosen
outcome (bet_team, or Over/Under; inverted for SELL) matches the market's winning
token. Replaces the old bug where `side` (BUY/SELL) was compared to the game's
first-team-win flag, scrambling win/loss (YES 97% / NO 14%, both-teams-win).

Run: venv/bin/python -m pytest tests/test_baseball_resolver_scoring.py -v --noconftest
"""

from signals.baseball_resolver import score_baseball_trade as score


# market text format: "<title> — <bet_label> <Moneyline|Spread|Total>"
# side: YES = BUY, NO = SELL.  Returns (is_correct, pnl).

def test_moneyline_buy_second_team_wins():
    # The case the OLD resolver got WRONG: bet the second-listed team, it wins.
    ic, pnl = score("Athletics vs. New York Yankees — New York Yankees Moneyline",
                    "YES", 0.45, "New York Yankees")
    assert ic == 1 and pnl > 0


def test_moneyline_buy_team_loses():
    ic, pnl = score("Athletics vs. New York Yankees — New York Yankees Moneyline",
                    "YES", 0.45, "Athletics")
    assert ic == 0 and pnl < 0


def test_moneyline_sell_team_loses_is_a_win():
    # SELL the team (side NO); the OTHER team wins -> bet wins.
    ic, pnl = score("Athletics vs. New York Yankees — New York Yankees Moneyline",
                    "NO", 0.45, "Athletics")
    assert ic == 1 and pnl > 0


def test_spread_favorite_covers():
    # Detroit -1.5 BUY, Tigers covered (they are the winning token).
    ic, pnl = score("Detroit Tigers vs. Tampa Bay Rays — Detroit Tigers Spread",
                    "YES", 0.40, "Detroit Tigers")
    assert ic == 1 and pnl > 0


def test_spread_favorite_fails_to_cover():
    # Angels -1.5 BUY, but Rockies (+1.5) is the winning token -> loss.
    ic, pnl = score("Colorado Rockies vs. Los Angeles Angels — Los Angeles Angels Spread",
                    "YES", 0.55, "Colorado Rockies")
    assert ic == 0 and pnl < 0


def test_spread_underdog_covers():
    # Underdog Rockies +1.5 BUY (its own market_id is the Angels -1.5 market, whose
    # winning token is 'Colorado Rockies' when the dog covers).
    ic, pnl = score("Colorado Rockies vs. Los Angeles Angels — Colorado Rockies Spread",
                    "YES", 0.50, "Colorado Rockies")
    assert ic == 1 and pnl > 0


def test_total_over_hits():
    ic, pnl = score("Miami Marlins vs. Washington Nationals — Over Total",
                    "YES", 0.53, "Over")
    assert ic == 1 and pnl > 0


def test_total_under_loses_when_over_hits():
    ic, pnl = score("Miami Marlins vs. Washington Nationals — Under Total",
                    "YES", 0.47, "Over")
    assert ic == 0 and pnl < 0


def test_both_teams_cannot_both_win():
    # The old bug let two opposite-team YES bets both 'win'. Here they must split.
    a = score("San Francisco Giants vs. Milwaukee Brewers — San Francisco Giants Moneyline",
              "YES", 0.50, "Milwaukee Brewers")
    b = score("San Francisco Giants vs. Milwaukee Brewers — Milwaukee Brewers Moneyline",
              "YES", 0.50, "Milwaukee Brewers")
    assert a[0] == 0 and b[0] == 1     # only the Brewers bet wins


def test_pnl_sign_tracks_win():
    win = score("A vs. B — B Moneyline", "YES", 0.30, "B")
    loss = score("A vs. B — B Moneyline", "YES", 0.30, "A")
    assert win[1] > 0 and loss[1] < 0

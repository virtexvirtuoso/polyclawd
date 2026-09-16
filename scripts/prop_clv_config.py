#!/usr/bin/env python3
"""
prop_clv_config.py — per-sport configuration for the generalized prop CLV harness.

Spec: 02-Projects/Polyclawd/Development/Prop-CLV-Generalized-Spec.md

Each PropConfig declares which markets to snapshot, their outcome SHAPE
(per-market, since one sport mixes shapes), the sharp/soft book sets, and the
control unit for the within-event paired gate ("event" = one game/match;
"card" = one UFC card, because a fight's two h2h sides are zero-sum and cannot
control each other).
"""

from __future__ import annotations

from dataclasses import dataclass

# Soft books we'd actually bet (sport-agnostic — US/UK retail books).
SOFT_BOOKS = frozenset(
    {"draftkings", "fanduel", "betrivers", "betmgm", "caesars", "onexbet", "skybet", "williamhill_us"}
)

# Betfair-weighted sharp consensus (the B12 fix that the goalscorer logger uses).
SOCCER_SHARP = (("betfair_ex_uk", 0.40), ("betfair_ex_eu", 0.40), ("pinnacle", 0.15), ("williamhill", 0.05))
# Pinnacle leads game lines (h2h/totals) for MLB/UFC; Betfair backs it up.
GAMELINE_SHARP = (("pinnacle", 0.70), ("betfair_ex_eu", 0.15), ("betfair_ex_uk", 0.15))

# Market-specific fair-value haircuts (de-bias yes-only consensus). 1.0 = none.
# goalscorer carries a ~+1.2pp positive bias (spec §3.2 OQ2).
HAIRCUTS = {"player_goal_scorer_anytime": 0.958}


@dataclass(frozen=True)
class PropConfig:
    name: str  # "soccer_wc" | "mlb" | "ufc" ...
    sport_key: str  # the-odds-api sport key
    markets: tuple  # tuple of (market_key, shape) — shape per market
    sharp_books: tuple  # tuple of (book_key, weight)
    soft_books: frozenset = SOFT_BOOKS
    control_unit: str = "event"  # "event" | "card"
    min_edge: float = 5.0  # edge_pct threshold to "flag" (select) a bet

    @property
    def market_keys(self) -> list:
        return [m for m, _ in self.markets]

    def shape_of(self, market_key: str) -> str:
        for m, s in self.markets:
            if m == market_key:
                return s
        return "yes_no"


CONFIGS = {
    # Soccer — World Cup goalscorer (reproduces the existing scorer logger exactly).
    "soccer_wc": PropConfig(
        name="soccer_wc",
        sport_key="soccer_fifa_world_cup",
        markets=(("player_goal_scorer_anytime", "yes_no"),),
        sharp_books=SOCCER_SHARP,
        control_unit="event",
    ),
    # Soccer — club leagues (the near-free generalization test).
    "soccer_epl": PropConfig(
        name="soccer_epl",
        sport_key="soccer_epl",
        # shots_on_target + assists REMOVED 2026-08-22: live probe found NO sharp
        # book on either market on any EPL or La Liga fixture, and the collected
        # data confirms it — both were 100% n_sharp<=1 (29,039 and 24,271 rows).
        # shots_on_target alone produced 18,181 of the 22,958 flagged EPL bets at
        # an apparent +7.15% edge with nothing anchoring it. Goalscorer is kept:
        # it does get Pinnacle/Betfair on some fixtures (56.9% anchorless, so
        # ~20.7k rows carry a real consensus and survive the MIN_SHARP gate).
        markets=(("player_goal_scorer_anytime", "yes_no"),),
        sharp_books=SOCCER_SHARP,
        control_unit="event",
    ),
    # Baseball — MLB. pitcher_strikeouts ONLY: batter props are soft-only (no
    # Pinnacle/Betfair anchor), so CLV-vs-sharp doesn't apply to them. Pitcher Ks
    # have a real sharp anchor. NOTE keys are pitcher_*, NOT player_* (422 trap).
    "mlb": PropConfig(
        name="mlb",
        sport_key="baseball_mlb",
        markets=(("pitcher_strikeouts", "over_under"),),
        sharp_books=GAMELINE_SHARP,
        control_unit="event",
    ),
    # Soccer — La Liga (added 2026-08-22, replacing MLS in the alerting roster).
    # GOALSCORER ONLY, deliberately. Live probe 2026-08-22 across both leagues'
    # nearest fixtures: player_shots_on_target and player_assists returned NO
    # sharp book on ANY fixture, in EPL or La Liga. Without a Pinnacle/Betfair
    # anchor a CLV-vs-sharp edge is undefined — the same reason MLB excludes
    # batter props. Sharp coverage on goalscorer is sparse and per-fixture in
    # both leagues (EPL Everton: pinnacle; La Liga Valencia: betfair_ex_uk), so
    # La Liga is not a weaker cousin here — it matches EPL's real coverage.
    "soccer_laliga": PropConfig(
        name="soccer_laliga",
        sport_key="soccer_spain_la_liga",
        markets=(("player_goal_scorer_anytime", "yes_no"),),
        sharp_books=SOCCER_SHARP,
        control_unit="event",
    ),
    # Soccer — UEFA Champions League. Highest volume-per-event of any soccer
    # competition on Polymarket ($2.51M/event, $474K/24h, $6.43M liquidity —
    # second only to EPL on 24h flow and first on depth). Idle until the league
    # phase is scheduled: the Odds API listed 0 UCL events on 2026-08-22.
    "soccer_ucl": PropConfig(
        name="soccer_ucl",
        sport_key="soccer_uefa_champs_league",
        markets=(("player_goal_scorer_anytime", "yes_no"),),
        sharp_books=SOCCER_SHARP,
        control_unit="event",
    ),
    # NFL — GAME LINES, not player props. Live probe 2026-08-22 on the first
    # fixture: h2h/spreads/totals all carry Pinnacle (h2h also both Betfair
    # exchanges), while player_pass_yds / player_rush_yds / player_receptions /
    # player_anytime_td / player_pass_tds ALL returned "no sharp books". US
    # player props are soft-only, exactly as the odds-api reference warns, so
    # they are excluded rather than logged without an anchor.
    # control_unit="card" groups by commence DATE: a game's two h2h sides are
    # zero-sum and cannot control each other, but other games on the same slate
    # can — the same reasoning as the UFC card below.
    "nfl": PropConfig(
        name="nfl",
        sport_key="americanfootball_nfl",
        markets=(("h2h", "two_way"), ("totals", "over_under")),
        sharp_books=GAMELINE_SHARP,
        control_unit="card",
    ),
    # UFC/MMA — no player props exist; h2h moneyline (two_way) + rounds totals.
    # Control unit = card: a fight's two h2h sides are zero-sum, so the opposite
    # side is NOT a valid control; other fights on the card are.
    "ufc": PropConfig(
        name="ufc",
        sport_key="mma_mixed_martial_arts",
        markets=(("h2h", "two_way"), ("totals", "over_under")),
        sharp_books=GAMELINE_SHARP,
        control_unit="card",
    ),
}

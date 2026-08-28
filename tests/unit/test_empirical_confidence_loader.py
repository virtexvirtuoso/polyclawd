"""Unit tests for _load_resolved_trades in signals.empirical_confidence.

This is the second consumer of shadow_trades (the archetype win-rate table
feeding confidence scoring); signals.source_win_rates is the first. Both read
the same table, so both must apply the same win predicate and the same row
filter.

Fixture vocabulary is seeded from production's ACTUAL distinct values
(SELECT DISTINCT on storage/shadow_trades.db, 2026-08-26) -- a fixture with a
fictional schema is exactly what let the market-frame bug below survive:

  shadow_trades.side     : 'YES' | 'NO' | 'PASS'
  shadow_trades.outcome  : 'YES' | 'NO' | '' | 'VOID'   (NULL also present)
  shadow_trades.resolved : 0 | 1
  shadow_trades.pnl      : REAL, NULL possible

Two independent defects are covered:

1. Row filter. Selecting on `resolved = 1` alone pulls in NULL/''-outcome,
   VOID, PASS-side and NULL-pnl rows. Each is then scored as a LOSS,
   depressing every archetype bucket. On production 2026-08-26 that was 35 of
   367 resolved rows.

2. Win predicate. `side == outcome` is WRONG. `outcome` is the market-frame
   resolution ('YES' = first-listed outcome won), while the sports resolvers
   score a trade by name match against the picked team/total and write pnl
   from that verdict. On production 2026-08-26 `side == outcome` disagreed
   with sign(pnl) on 113 of 332 scoreable rows -- all baseball_moneyline /
   baseball_spread / baseball_total -- and agreed on all 92 non-sports rows.
   The win is sign(pnl), matching signals/source_win_rates.py.
"""

import sqlite3

import pytest

import signals.empirical_confidence as ec


SHADOW_DDL = """
CREATE TABLE shadow_trades (
    market TEXT, side TEXT, entry_price REAL, outcome TEXT,
    pnl REAL, platform TEXT, resolved INTEGER
)
"""
PAPER_DDL = """
CREATE TABLE paper_positions (
    market_title TEXT, side TEXT, entry_price REAL, status TEXT, platform TEXT
)
"""

# (market, side, entry_price, outcome, pnl, platform, resolved)
SHADOW_ROWS = [
    # --- scoreable, side == outcome AGREES with sign(pnl) (non-sports shape) --
    ("Mkt A", "YES", 0.40, "YES", 3.10, "kalshi", 1),  # win
    ("Mkt B", "NO", 0.30, "NO", 2.50, "kalshi", 1),  # win (NO side, NO result)
    ("Mkt C", "YES", 0.50, "NO", -5.00, "kalshi", 1),  # loss
    # --- scoreable, market-frame MISMATCH: side == outcome DISAGREES with pnl -
    # Deliberately unbalanced (2 pnl-wins vs 1 pnl-loss) so the aggregate win
    # count differs between the two predicates and a revert to side == outcome
    # cannot pass by symmetry.
    ("Yankees vs Red Sox", "YES", 0.55, "NO", 4.20, "kalshi", 1),  # win by pnl
    ("Astros vs Angels", "YES", 0.60, "NO", 1.75, "kalshi", 1),  # win by pnl
    ("Dodgers vs Giants", "NO", 0.45, "NO", -6.00, "kalshi", 1),  # loss by pnl
    # --- NOT scoreable: must be dropped, never counted as losses -------------
    ("Mkt D", "YES", 0.40, None, 0.0, "kalshi", 1),  # outcome never written
    ("Mkt E", "YES", 0.40, "", 0.0, "kalshi", 1),  # outcome empty string
    ("Mkt F", "YES", 0.40, "VOID", 0.0, "kalshi", 1),  # market voided
    ("Mkt G", "PASS", 0.40, "YES", 1.0, "kalshi", 1),  # non-directional
    ("Mkt H", "YES", 0.40, "YES", None, "kalshi", 1),  # pnl never written
    # --- not resolved yet ----------------------------------------------------
    ("Mkt I", "YES", 0.40, "YES", 2.0, "kalshi", 0),
]

SCOREABLE_TITLES = {
    "Mkt A",
    "Mkt B",
    "Mkt C",
    "Yankees vs Red Sox",
    "Astros vs Angels",
    "Dodgers vs Giants",
}


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "shadow_trades.db"
    conn = sqlite3.connect(path)
    conn.execute(SHADOW_DDL)
    conn.execute(PAPER_DDL)
    conn.executemany("INSERT INTO shadow_trades VALUES (?,?,?,?,?,?,?)", SHADOW_ROWS)
    conn.executemany(
        "INSERT INTO paper_positions VALUES (?,?,?,?,?)",
        [
            ("Mkt J", "YES", 0.45, "won", "polymarket"),
            ("Mkt K", "NO", 0.55, "lost", "polymarket"),
            ("Mkt L", "YES", 0.45, "stopped", "polymarket"),  # excluded
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(ec, "DB_PATH", path)
    return path


def _shadow(trades):
    return [t for t in trades if t["platform"] == "kalshi"]


def _by_title(trades):
    return {t["title"]: t for t in trades}


# ─── Row filter ──────────────────────────────────────────────────────


def test_unscoreable_shadow_rows_are_dropped_not_counted_as_losses(db):
    """NULL / '' / VOID outcome, PASS side and NULL pnl must not appear."""
    shadow = _shadow(ec._load_resolved_trades())
    assert len(shadow) == 6
    assert {t["title"] for t in shadow} == SCOREABLE_TITLES


def test_null_pnl_row_is_dropped(db):
    """A resolved YES/YES row with no pnl cannot be scored -- drop it."""
    assert "Mkt H" not in {t["title"] for t in ec._load_resolved_trades()}


def test_unresolved_rows_excluded(db):
    assert "Mkt I" not in {t["title"] for t in ec._load_resolved_trades()}


def test_paper_arm_unaffected(db):
    """The paper_positions arm keeps its own 'won'/'lost' vocabulary."""
    paper = [t for t in ec._load_resolved_trades() if t["platform"] == "polymarket"]
    assert len(paper) == 2  # 'stopped' excluded
    assert sum(t["won"] for t in paper) == 1


# ─── Win predicate: sign(pnl), not side == outcome ───────────────────


def test_no_side_win_is_scored_as_a_win(db):
    """A NO-side trade resolving NO with positive pnl is a win."""
    assert _by_title(_shadow(ec._load_resolved_trades()))["Mkt B"]["won"] is True


def test_market_frame_mismatch_positive_pnl_is_a_win(db):
    """MUTATION GUARD: side='YES', outcome='NO', pnl=+4.20.

    side == outcome says LOSS; the resolver that wrote the pnl says WIN.
    Reverting the predicate to side == outcome flips this to False.
    """
    won = _by_title(_shadow(ec._load_resolved_trades()))["Yankees vs Red Sox"]["won"]
    assert won is True, "market-frame row with positive pnl must score as a WIN"


def test_market_frame_mismatch_negative_pnl_is_a_loss(db):
    """MUTATION GUARD: side='NO', outcome='NO', pnl=-6.00.

    side == outcome says WIN; the resolver that wrote the pnl says LOSS.
    Reverting the predicate to side == outcome flips this to True.
    """
    won = _by_title(_shadow(ec._load_resolved_trades()))["Dodgers vs Giants"]["won"]
    assert won is False, "market-frame row with negative pnl must score as a LOSS"


def test_shadow_win_count_and_rate_use_pnl_sign(db):
    """MUTATION GUARD (aggregate): 4 of 6 scoreable rows have pnl > 0.

    Under the old side == outcome predicate the same 6 rows give 3 wins
    (Mkt A, Mkt B, Dodgers vs Giants), so this assertion fails on revert.
    """
    shadow = _shadow(ec._load_resolved_trades())
    assert sum(t["won"] for t in shadow) == 4
    assert sum(t["won"] for t in shadow) / len(shadow) == pytest.approx(4 / 6)


def test_wr_table_bucket_uses_pnl_sign(db):
    """The predicate must survive into _compute_wr_table, which is what
    calculate_empirical_confidence -> _get_dynamic_kelly actually sizes on."""
    shadow = _shadow(ec._load_resolved_trades())
    mismatch = [t for t in shadow if t["title"] in ("Yankees vs Red Sox", "Astros vs Angels")]
    table = ec._compute_wr_table(mismatch)
    assert sum(b["wins"] for b in table.values()) == 2
    assert sum(b["total"] for b in table.values()) == 2
    assert all(b["wr"] == 1.0 for b in table.values())


def test_zero_pnl_is_a_loss(db):
    """pnl exactly 0 is a LOSS, matching source_win_rates.py's
    `CASE WHEN pnl > 0 THEN 'won' ELSE 'lost'`. 0 such rows on prod today."""
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO shadow_trades VALUES (?,?,?,?,?,?,?)",
        ("Mkt Scratch", "YES", 0.50, "YES", 0.0, "kalshi", 1),
    )
    conn.commit()
    conn.close()
    scratch = _by_title(_shadow(ec._load_resolved_trades()))["Mkt Scratch"]
    assert scratch["won"] is False

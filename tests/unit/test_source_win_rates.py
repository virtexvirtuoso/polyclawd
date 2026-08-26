"""Unit tests for the per-source empirical win-rate learning loop.

Covers signals.source_win_rates and the blended get_source_win_rate in
api.routes.signals. Pattern from tests/unit/test_signals.py: tmp sqlite DB
plus monkeypatched module paths.
"""

import json
import sqlite3

import pytest

import signals.source_win_rates as swr
from api.routes.signals import N_FLOOR, get_source_win_rate


# ============================================================================
# Fixtures
# ============================================================================

SEEDS = {
    "mapped_source": {"wins": 5, "losses": 5, "total": 10},
    "news_google": {"wins": 3, "losses": 7, "total": 10},
    "small_sample": {"wins": 4, "losses": 6, "total": 10},
    "hot_source": {"wins": 1, "losses": 1, "total": 2},
    "cold_source": {"wins": 1, "losses": 1, "total": 2},
}


def _make_db(path, paper_rows=(), shadow_rows=()):
    """Create a minimal shadow_trades.db with both tables.

    The two tables use DIFFERENT outcome vocabularies and these fixtures must
    mirror the production values exactly (SELECT DISTINCT on prod, 2026-08-26):

      paper_positions.status : 'won' | 'lost' | 'stopped' | 'open'
      shadow_trades.side     : 'YES' | 'NO' | 'PASS'
      shadow_trades.outcome  : 'YES' | 'NO' | 'VOID' | NULL

    A shadow trade is a win when side == outcome; there is no 'won'/'lost'
    string anywhere in that table. Seeding invented values here previously hid
    a query that matched 0 of 334 resolved shadow trades in production.
    """
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE paper_positions (strategy TEXT, status TEXT)")
    conn.execute("CREATE TABLE shadow_trades (strategy TEXT, side TEXT, outcome TEXT, resolved INTEGER)")
    conn.executemany("INSERT INTO paper_positions VALUES (?, ?)", paper_rows)
    conn.executemany("INSERT INTO shadow_trades VALUES (?, ?, ?, ?)", shadow_rows)
    conn.commit()
    conn.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Wire tmp DB + tmp seed file into both modules; reset the 60s cache."""
    db_path = tmp_path / "shadow_trades.db"
    _make_db(
        db_path,
        paper_rows=[
            # mapped_strategy: 6 won, 4 lost, 3 stopped (stopped must not count)
            *[("mapped_strategy", "won")] * 6,
            *[("mapped_strategy", "lost")] * 4,
            *[("mapped_strategy", "stopped")] * 3,
            # tiny_strategy: 2 won, 1 lost -> total 3 < N_FLOOR
            *[("tiny_strategy", "won")] * 2,
            ("tiny_strategy", "lost"),
            # hot_strategy: 20/20 won -> clamps high
            *[("hot_strategy", "won")] * 20,
            # cold_strategy: 0/20 won -> clamps low
            *[("cold_strategy", "lost")] * 20,
        ],
        shadow_rows=[
            # (strategy, side, outcome, resolved) — a win is side == outcome
            ("mapped_strategy", "YES", "YES", 1),  # win
            ("mapped_strategy", "NO", "YES", 1),  # loss
            # must NOT count:
            ("mapped_strategy", "YES", "YES", 0),  # not resolved yet
            ("mapped_strategy", "YES", None, 1),  # resolved, outcome never written
            ("mapped_strategy", "YES", "VOID", 1),  # market voided
            ("mapped_strategy", "PASS", "YES", 1),  # non-directional row
            # a NO-side trade that lands NO is a win, not a loss
            ("no_side_strategy", "NO", "NO", 1),
            ("no_side_strategy", "NO", "YES", 1),
        ],
    )
    monkeypatch.setattr(swr, "DB_PATH", db_path)
    monkeypatch.setattr(swr, "_cache", {"ts": 0.0, "counts": {}})
    monkeypatch.setattr(
        swr,
        "SOURCE_TO_STRATEGY",
        {
            "mapped_source": ["mapped_strategy"],
            "small_sample": ["tiny_strategy"],
            "hot_source": ["hot_strategy"],
            "cold_source": ["cold_strategy"],
            # one source, two strategy labels — the real shape of
            # mispriced_category (MispricedCategoryWhale + legacy_*)
            "renamed_source": ["mapped_strategy", "no_side_strategy"],
        },
    )
    seeds_file = tmp_path / "source_outcomes.json"
    seeds_file.write_text(json.dumps(SEEDS))
    monkeypatch.setattr("api.routes.signals.SOURCE_OUTCOMES_FILE", seeds_file)
    return db_path


# ============================================================================
# Tests
# ============================================================================


class TestSourceWinRates:
    def test_blend_math(self, env):
        """(a) Blended rate = (prior_wins + wins) / (prior_total + total)."""
        # mapped_strategy: paper 6W/4L + shadow resolved 1W/1L = 7 wins / 12 total
        assert swr.lookup("mapped_source") == (7, 12)
        # prior 5/10 -> (5 + 7) / (10 + 12) = 12/22
        expected = (5 + 7) / (10 + 12)
        assert get_source_win_rate("mapped_source") == pytest.approx(expected)

    def test_stopped_excluded(self, env):
        """(b) 'stopped' positions never count toward wins or total."""
        wins, total = swr.lookup("mapped_source")
        assert total == 12  # 15 paper rows exist but 3 stopped are excluded
        counts = swr.empirical_source_counts()
        assert counts["mapped_strategy"] == (7, 12)

    def test_unresolvable_shadow_rows_excluded(self, env):
        """(b2) Unresolved, NULL-outcome, VOID and PASS-side rows never count.

        6 mapped_strategy shadow rows exist; only the 2 resolved YES/NO ones
        are scoreable. Counting the other 4 would silently score them as
        losses (the failure mode in signals/empirical_confidence.py).
        """
        paper_only = (6, 10)  # 6W/4L, stopped excluded
        wins, total = swr.lookup("mapped_source")
        assert (wins - paper_only[0], total - paper_only[1]) == (1, 2)

    def test_shadow_win_is_side_equals_outcome(self, env):
        """(b3) A NO-side trade resolving NO is a win, not a loss.

        Regression guard: the query must compare side to outcome, not look for
        a literal 'won' string that shadow_trades has never stored.
        """
        assert swr.lookup("no_side_strategy") == (1, 2)

    def test_multi_strategy_source_sums_all_labels(self, env):
        """(b4) A source mapped to several strategy labels sums all of them.

        A renamed scanner leaves its history split across labels; counting
        only the current name scores the source off a fraction of its record.
        """
        assert swr.lookup("renamed_source") == (7 + 1, 12 + 2)

    def test_unmapped_source_returns_seed(self, env):
        """(c) Source with no strategy mapping keeps its seed rate exactly."""
        assert swr.lookup("news_google") == (0, 0)
        assert get_source_win_rate("news_google") == pytest.approx(3 / 10)

    def test_below_n_floor_returns_seed(self, env):
        """(d) Fewer than N_FLOOR resolved trades -> prior wins."""
        wins, total = swr.lookup("small_sample")
        assert total == 3 and total < N_FLOOR
        assert get_source_win_rate("small_sample") == pytest.approx(4 / 10)

    def test_clamp(self, env):
        """(e) Result clamps to [0.20, 0.80]."""
        # hot: (1 + 20) / (2 + 20) = 0.954... -> 0.80
        assert get_source_win_rate("hot_source") == pytest.approx(0.80)
        # cold: (1 + 0) / (2 + 20) = 0.045... -> 0.20
        assert get_source_win_rate("cold_source") == pytest.approx(0.20)

    def test_unreadable_db_falls_back_to_prior(self, env, tmp_path, monkeypatch):
        """(f) Missing/unreadable DB -> empty counts -> seed rates served."""
        monkeypatch.setattr(swr, "DB_PATH", tmp_path / "does_not_exist.db")
        monkeypatch.setattr(swr, "_cache", {"ts": 0.0, "counts": {}})
        assert swr.empirical_source_counts() == {}
        assert swr.lookup("mapped_source") == (0, 0)
        assert get_source_win_rate("mapped_source") == pytest.approx(5 / 10)

    def test_exact_strategy_name_match(self, env):
        """A source named exactly like a DB strategy needs no mapping."""
        assert swr.lookup("hot_strategy") == (20, 20)

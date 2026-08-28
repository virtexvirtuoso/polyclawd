"""Unit tests for the per-source empirical win-rate learning loop.

Covers signals.source_win_rates and the blended get_source_win_rate in
api.routes.signals. Pattern from tests/unit/test_signals.py: tmp sqlite DB
plus monkeypatched module paths.
"""

import json
import logging
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
      shadow_trades.side     : 'YES' | 'NO' | 'PASS' | ''
      shadow_trades.outcome  : 'YES' | 'NO' | 'VOID' | ''   (plus NULL-safety)
      shadow_trades.pnl      : signed settlement written by the resolver;
                               its sign is the resolver's own win verdict

    There is no 'won'/'lost' string anywhere in shadow_trades. Seeding
    invented values here previously hid a query that matched 0 of 334
    resolved shadow trades in production. Nor is side == outcome a win:
    `outcome` is the market-frame resolution ('YES' = first-listed token
    won), which contradicts the resolver's pnl on 113 of 332 production
    baseball rows.
    """
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE paper_positions (strategy TEXT, status TEXT)")
    conn.execute(
        "CREATE TABLE shadow_trades "
        "(strategy TEXT, side TEXT, outcome TEXT, resolved INTEGER, pnl REAL)"
    )
    conn.executemany("INSERT INTO paper_positions VALUES (?, ?)", paper_rows)
    conn.executemany("INSERT INTO shadow_trades VALUES (?, ?, ?, ?, ?)", shadow_rows)
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
            # (strategy, side, outcome, resolved, pnl) — win iff pnl > 0
            ("mapped_strategy", "YES", "YES", 1, 0.47),  # win
            ("mapped_strategy", "NO", "YES", 1, -0.55),  # loss
            # must NOT count:
            ("mapped_strategy", "YES", "YES", 0, None),  # not resolved yet
            ("mapped_strategy", "YES", None, 1, None),  # outcome never written
            ("mapped_strategy", "YES", "", 1, None),  # outcome = '' (33 rows in prod)
            ("mapped_strategy", "YES", "VOID", 1, 0.0),  # market voided
            ("mapped_strategy", "PASS", "YES", 1, None),  # non-directional row
            ("mapped_strategy", "", "YES", 1, None),  # side never written ('' in prod)
            ("mapped_strategy", "YES", "NO", 1, None),  # scoreable frame, pnl missing
            # a NO-side trade that lands NO settles positive: win, not loss
            ("no_side_strategy", "NO", "NO", 1, 0.405),
            ("no_side_strategy", "NO", "YES", 1, -0.9255),
            # sports rows (baseball_* shape): outcome is the MARKET-frame
            # resolution, so side != outcome can still be a winning trade.
            # Every row below has side != outcome, and wins do NOT equal
            # losses, so pnl > 0 and side == outcome yield DIFFERENT
            # aggregates: (2, 3) vs (0, 3). A balanced 1-win/1-loss pair
            # let a side == outcome mutation survive by cancellation.
            ("market_frame_strategy", "YES", "NO", 1, 0.575),  # win (id 86 shape)
            ("market_frame_strategy", "YES", "NO", 1, 0.310),  # win, same shape
            ("market_frame_strategy", "NO", "YES", 1, -0.620),  # loss
        ],
    )
    monkeypatch.setattr(swr, "DB_PATH", db_path)
    monkeypatch.setattr(swr, "_cache", {"ts": 0.0, "counts": None})
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
        """(b2) Unresolved, ''/NULL-outcome, VOID, PASS/''-side and NULL-pnl
        rows never count.

        9 mapped_strategy shadow rows exist; only the 2 resolved YES/NO ones
        with pnl written are scoreable. Counting the other 7 would silently
        score them as losses (the failure mode in
        signals/empirical_confidence.py).
        """
        paper_only = (6, 10)  # 6W/4L, stopped excluded
        wins, total = swr.lookup("mapped_source")
        assert (wins - paper_only[0], total - paper_only[1]) == (1, 2)

    def test_shadow_win_is_pnl_sign(self, env):
        """(b3) A NO-side trade resolving NO settles positive: a win.

        Regression guard: the query must derive win/loss from the resolver's
        signed pnl, not look for a literal 'won' string that shadow_trades
        has never stored.
        """
        assert swr.lookup("no_side_strategy") == (1, 2)

    def test_market_frame_outcome_not_used_as_win(self, env):
        """(b3b) side == outcome is NOT the win predicate.

        Production baseball resolvers write `outcome` as the market-frame
        resolution and score the trade by name-matching, so a side != outcome
        row with positive pnl is a WIN (113 of 332 prod rows disagree between
        the two predicates). All three fixture rows have side != outcome:
        the correct predicate (pnl > 0) counts (2, 3), while side == outcome
        counts (0, 3) — the fixture is deliberately unbalanced so that
        mutating the query back to side == outcome fails this assertion
        instead of cancelling out.
        """
        assert swr.lookup("market_frame_strategy") == (2, 3)

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
        monkeypatch.setattr(swr, "_cache", {"ts": 0.0, "counts": None})
        assert swr.empirical_source_counts() == {}
        assert swr.lookup("mapped_source") == (0, 0)
        assert get_source_win_rate("mapped_source") == pytest.approx(5 / 10)

    def test_exact_strategy_name_match(self, env):
        """A source named exactly like a DB strategy needs no mapping."""
        assert swr.lookup("hot_strategy") == (20, 20)


class TestShadowZeroHitAlarm:
    """The shadow arm going structurally dead must WARN, not sit quiet.

    Fleet rule (2026-08-25): a filter that can never match is
    indistinguishable from no data unless it alarms. The original query
    filtered shadow_trades for 'won'/'lost' and scored 0/334 silently.
    """

    def test_alarm_fires_when_shadow_arm_dead(self, tmp_path, monkeypatch, caplog):
        """Resolved shadow rows exist, none scoreable -> one WARNING."""
        db_path = tmp_path / "dead_shadow.db"
        _make_db(
            db_path,
            paper_rows=[("some_strategy", "won")],
            shadow_rows=[
                # all resolved, none pass the outcome/side/pnl filter — the
                # exact production shape while the arm was dead
                ("s1", "YES", "", 1, None),
                ("s1", "", "", 1, None),
                ("s1", "NO", "VOID", 1, 0.0),
            ],
        )
        monkeypatch.setattr(swr, "DB_PATH", db_path)
        monkeypatch.setattr(swr, "_cache", {"ts": 0.0, "counts": None})
        with caplog.at_level(logging.WARNING, logger="signals.source_win_rates"):
            counts = swr.empirical_source_counts()
        # paper arm still works; shadow arm contributes nothing
        assert counts == {"some_strategy": (1, 1)}
        alarms = [r for r in caplog.records if "shadow arm matched 0 of 3" in r.message]
        assert len(alarms) == 1
        assert alarms[0].levelno == logging.WARNING

    def test_alarm_rate_limited_to_cache_refresh(self, tmp_path, monkeypatch, caplog):
        """Repeated lookups within the TTL log the WARNING only once."""
        db_path = tmp_path / "dead_shadow2.db"
        _make_db(db_path, shadow_rows=[("s1", "YES", "", 1, None)])
        monkeypatch.setattr(swr, "DB_PATH", db_path)
        monkeypatch.setattr(swr, "_cache", {"ts": 0.0, "counts": None})
        with caplog.at_level(logging.WARNING, logger="signals.source_win_rates"):
            for _ in range(5):
                swr.empirical_source_counts()
        assert len([r for r in caplog.records if "shadow arm" in r.message]) == 1

    def test_no_alarm_when_shadow_arm_alive(self, env, caplog):
        """Scoreable resolved shadow rows present -> silence."""
        with caplog.at_level(logging.WARNING, logger="signals.source_win_rates"):
            swr.empirical_source_counts()
        assert not [r for r in caplog.records if "shadow arm" in r.message]

    def test_no_alarm_when_no_resolved_shadow_rows(self, tmp_path, monkeypatch, caplog):
        """Zero resolved shadow rows is a legitimate state, not a dead filter."""
        db_path = tmp_path / "no_resolved.db"
        _make_db(
            db_path,
            paper_rows=[("some_strategy", "won")],
            shadow_rows=[("s1", "YES", "", 0, None)],  # unresolved only
        )
        monkeypatch.setattr(swr, "DB_PATH", db_path)
        monkeypatch.setattr(swr, "_cache", {"ts": 0.0, "counts": None})
        with caplog.at_level(logging.WARNING, logger="signals.source_win_rates"):
            swr.empirical_source_counts()
        assert not [r for r in caplog.records if "shadow arm" in r.message]


class TestEmptyResultCaching:
    def test_empty_result_is_cached_for_ttl(self, tmp_path, monkeypatch):
        """A legitimately-empty {} is cached: no SQLite reopen within the TTL.

        Regression guard for `if _cache["counts"]`, which treated {} as
        'nothing cached' and reopened the DB on every request while empty.
        """
        db_path = tmp_path / "empty.db"
        _make_db(db_path)  # both tables exist, zero rows
        monkeypatch.setattr(swr, "DB_PATH", db_path)
        monkeypatch.setattr(swr, "_cache", {"ts": 0.0, "counts": None})
        assert swr.empirical_source_counts() == {}
        assert swr._cache["counts"] == {}  # cached, not discarded

        def _boom(*args, **kwargs):
            raise AssertionError("DB reopened within TTL for a cached empty result")

        monkeypatch.setattr(swr.sqlite3, "connect", _boom)
        assert swr.empirical_source_counts() == {}  # served from cache

    def test_error_result_is_not_cached(self, tmp_path, monkeypatch):
        """A failure {} is NOT cached, so the next call retries the DB."""
        monkeypatch.setattr(swr, "DB_PATH", tmp_path / "does_not_exist.db")
        monkeypatch.setattr(swr, "_cache", {"ts": 0.0, "counts": None})
        assert swr.empirical_source_counts() == {}
        assert swr._cache["counts"] is None  # untouched -> next call retries

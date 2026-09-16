"""Regression tests for scripts/entry_reasoning_check.py classification.

Written after an independent review REJECTED the first version. That version
classified a position as "legacy" using a LEXICOGRAPHIC string compare:

    opened_at >= "2026-08-22T01:40:00"

`live_positions.opened_at` is TEXT with mixed formats (production holds both
"2026-06-27 01:14:00" and ISO-T). At index 10, ' ' (0x20) < 'T' (0x54), so any
same-day space-separated timestamp — and NULL, "", date-only, epoch-as-text —
sorted BELOW the cutoff and was silently reported as an expected legacy gap.
The monitor then printed "OK — invariant holds" over real violations: a false
all-clear, the exact failure it exists to prevent.

Boundary is now LEGACY_MAX_POSITION_ID (ids are AUTOINCREMENT and monotonic),
which is immune to timestamp format entirely. These tests pin that.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.entry_reasoning_check import LEGACY_MAX_POSITION_ID, query  # noqa: E402

DDL = """
CREATE TABLE live_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, opened_at TEXT, market_id TEXT,
    market_slug TEXT, market_title TEXT, token_id TEXT, side TEXT,
    entry_price REAL, shares REAL, cost_usd REAL, status TEXT,
    closed_at TEXT, exit_price REAL, pnl REAL, close_reason TEXT,
    fee_paid_total REAL DEFAULT 0, archetype TEXT DEFAULT 'weather');
CREATE TABLE live_entry_reasoning (
    id INTEGER PRIMARY KEY AUTOINCREMENT, position_id INTEGER NOT NULL,
    ts TEXT, trigger_source TEXT, wallet_address TEXT, wallet_win_rate REAL,
    wallet_net_pnl REAL, edge_pct REAL, confidence REAL, reasoning TEXT,
    raw_json TEXT);
"""

# Every one of these was misrouted to "legacy" by the string compare.
MISROUTED = [
    ("2026-08-22 23:59:00", "same-day, SPACE separator, 22h after cutoff"),
    (None, "NULL opened_at"),
    ("", "empty opened_at"),
    ("2026-08-22", "date-only"),
    ("1756000000", "epoch-as-text"),
]


def _db(tmp_path, rows):
    """rows: list of (id, opened_at, has_reasoning)."""
    p = str(tmp_path / "t.db")
    c = sqlite3.connect(p)
    c.executescript(DDL)
    for pid, opened, has_reason in rows:
        c.execute(
            "INSERT INTO live_positions (id, opened_at, market_title, status, archetype) "
            "VALUES (?,?,?,?,?)", (pid, opened, f"pos{pid}", "open", "smart_wallet"))
        if has_reason:
            c.execute("INSERT INTO live_entry_reasoning (position_id, trigger_source) "
                      "VALUES (?,?)", (pid, "smart_wallet"))
    c.commit()
    c.close()
    return p


@pytest.mark.parametrize("opened_at,label", MISROUTED)
def test_post_boundary_without_reasoning_is_a_violation(tmp_path, opened_at, label):
    """Fail CLOSED: past the id boundary with no reasoning row => violation."""
    pid = LEGACY_MAX_POSITION_ID + 1
    db = _db(tmp_path, [(pid, opened_at, False)])
    violations, legacy, covered = query(db)
    assert [v["id"] for v in violations] == [pid], f"{label} was NOT flagged"
    assert legacy == [], f"{label} was wrongly classified as legacy"
    assert covered == 0


@pytest.mark.parametrize("opened_at,label", MISROUTED)
def test_post_boundary_with_reasoning_is_covered(tmp_path, opened_at, label):
    """A reasoning row clears it regardless of timestamp format."""
    pid = LEGACY_MAX_POSITION_ID + 1
    db = _db(tmp_path, [(pid, opened_at, True)])
    violations, legacy, covered = query(db)
    assert violations == [] and legacy == [] and covered == 1


def test_pre_boundary_without_reasoning_is_legacy_not_violation(tmp_path):
    """Genuinely old rows stay legacy — including the space-format one in prod."""
    db = _db(tmp_path, [(1, "2026-06-27 01:14:00", False),
                        (LEGACY_MAX_POSITION_ID, "2026-08-21T19:58:15+00:00", False)])
    violations, legacy, covered = query(db)
    assert violations == []
    assert sorted(x["id"] for x in legacy) == [1, LEGACY_MAX_POSITION_ID]


def test_boundary_is_exclusive_at_the_max_legacy_id(tmp_path):
    db = _db(tmp_path, [(LEGACY_MAX_POSITION_ID, "x", False),
                        (LEGACY_MAX_POSITION_ID + 1, "x", False)])
    violations, legacy, _ = query(db)
    assert [v["id"] for v in violations] == [LEGACY_MAX_POSITION_ID + 1]
    assert [l["id"] for l in legacy] == [LEGACY_MAX_POSITION_ID]

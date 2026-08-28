"""resolve_trades must not starve rows behind an unresolvable head.

The queue was `ORDER BY timestamp ASC LIMIT 15`, so every run pulled the same
15 oldest unresolved rows. Rows that can never resolve — an empty market_id, a
closed market with winner=False on both tokens, or a long-dated market still
open months later — stayed at the head forever and every newer row was
starved. On production 2026-08-28 that left 42 immediately-resolvable rows
(34 MispricedCategoryWhale, 6 twc_resolution_edge, 2 manifold_lead) stuck
behind a head of 15 that could never clear.

The fix orders by least-recently-attempted and stamps the whole batch up
front, so an unresolvable row goes to the back of the queue after its turn.
"""

import sqlite3

import pytest

import signals.shadow_tracker as st


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A DB with more unresolved rows than one batch, none resolvable."""
    path = tmp_path / "shadow_trades.db"
    monkeypatch.setattr(st, "DB_PATH", path)
    monkeypatch.setattr(st, "STORAGE_DIR", tmp_path)
    # Never resolve anything: this test is about queue fairness, not outcomes.
    monkeypatch.setattr(st, "_check_polymarket_resolution", lambda cid: None)
    monkeypatch.setattr(st, "_fetch_json", lambda *a, **k: None)

    conn = st.get_db()          # creates schema + runs the migrations
    for i in range(40):
        conn.execute(
            "INSERT INTO shadow_trades "
            "(timestamp, market_id, market, platform, side, entry_price, resolved) "
            "VALUES (?,?,?,?,?,?,0)",
            (f"2026-01-{i + 1:02d}T00:00:00+00:00", f"0x{i:064x}",
             f"Market {i}", "polymarket", "YES", 0.5),
        )
    conn.commit()
    conn.close()
    return path


def _attempted(path):
    conn = sqlite3.connect(path)
    ids = {r[0] for r in conn.execute(
        "SELECT id FROM shadow_trades WHERE last_resolve_attempt IS NOT NULL"
    )}
    conn.close()
    return ids


def test_migration_adds_the_column(db):
    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(shadow_trades)")}
    conn.close()
    assert "last_resolve_attempt" in cols


def test_second_run_reaches_different_rows(db):
    """The core guarantee: an unresolvable head must not block the tail."""
    st.resolve_trades(batch_size=10, delay=0)
    first = _attempted(db)
    assert len(first) == 10, f"first batch stamped {len(first)} rows"

    st.resolve_trades(batch_size=10, delay=0)
    second = _attempted(db)
    assert len(second) == 20, (
        "second run re-processed the same rows — the queue is still "
        f"head-of-line blocked (stamped {len(second)}, expected 20)"
    )


def test_every_row_gets_a_turn(db):
    """Four batches of 10 must cover all 40 rows exactly once."""
    for _ in range(4):
        st.resolve_trades(batch_size=10, delay=0)
    assert len(_attempted(db)) == 40


def test_old_ordering_would_fail_this(db):
    """Positive control that the fixture can distinguish the two orderings.

    Under `ORDER BY timestamp ASC LIMIT 10` the first ten rows are returned
    every time, so the attempted set would stay at 10 and
    test_second_run_reaches_different_rows would fail. Assert the fixture
    really does hold more rows than a single batch.
    """
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM shadow_trades WHERE resolved=0").fetchone()[0]
    conn.close()
    assert n > 10, "fixture must exceed one batch or the test proves nothing"

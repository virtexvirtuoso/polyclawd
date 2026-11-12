"""Task 1.11 (#58): one stopped market must not abort a process_signals batch.

open_position's stop-loss re-entry gate used an undefined name `edge_pct`, so a
signal on a market we were previously stopped out of raised NameError and took
the whole process_signals batch down with it. Real process_signals and
open_position run here. This file stubs only evaluate_signal (heavy,
network-adjacent scoring), HAS_ACTIVITY_FEED and HAS_MOMENTUM; the
Discord/Telegram protection comes from the autouse `_notifier_capture` fixture
in tests/conftest.py, not from anything in this file. Storage is redirected to
tmp_path.

The gate under test: a signal on a market with a `stopped` row is rejected when
its edge is <= the stored edge of the most recent stopped row (NULL counts as
0), and allowed when strictly greater.
"""
from datetime import datetime, timezone

import pytest
from loguru import logger

from signals import paper_portfolio as pp

STOPPED_ID = "KXSTOPPED-26NOV11"
PREV_EDGE = 0.20  # stored edge_pct of the earlier, stopped position (fraction)


def _signal(market_id, title, edge):
    return {
        "market_id": market_id,
        "market_title": title,
        "side": "YES",
        "price": 0.40,
        "confidence": 0.60,
        "archetype": "other",
        "platform": "kalshi",
        "_edge": edge,
    }


@pytest.fixture()
def paper_db(tmp_path, monkeypatch):
    """Isolated paper DB seeded with one stopped position, no live routing."""
    monkeypatch.setattr(pp, "DB_PATH", tmp_path / "shadow_trades.db")
    monkeypatch.setattr(pp, "HAS_ACTIVITY_FEED", False)
    monkeypatch.setattr(pp, "HAS_MOMENTUM", False)
    monkeypatch.setitem(pp._status_cache, "data", None)
    monkeypatch.setitem(pp._status_cache, "ts", 0)

    def fake_eval(sig):
        return {"eligible": True, "reason": "Criteria met", "edge": sig["_edge"],
                "bet_size": 10.0, "kelly_pct": 0.05, "kelly": {}}

    monkeypatch.setattr(pp, "evaluate_signal", fake_eval)

    conn = pp._get_db()
    conn.execute(
        """INSERT INTO paper_positions
        (opened_at, closed_at, market_id, market_title, platform, side,
         entry_price, bet_size, edge_pct, status, pnl, archetype)
        VALUES (?, ?, ?, 'Stopped market', 'kalshi', 'YES', 0.40, 10.0, ?,
                'stopped', -5.0, 'other')""",
        (datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(),
         STOPPED_ID, PREV_EDGE))
    conn.commit()
    conn.close()
    return pp.DB_PATH


def _seed_stopped(market_id, edge_pct, closed_at):
    conn = pp._get_db()
    try:
        conn.execute(
            """INSERT INTO paper_positions
            (opened_at, closed_at, market_id, market_title, platform, side,
             entry_price, bet_size, edge_pct, status, pnl, archetype)
            VALUES (?, ?, ?, 'Stopped market', 'kalshi', 'YES', 0.40, 10.0, ?,
                    'stopped', -5.0, 'other')""",
            (closed_at, closed_at, market_id, edge_pct))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def log_lines():
    """Capture loguru messages (pytest's caplog does not see loguru)."""
    lines = []
    sink_id = logger.add(lambda m: lines.append(m.record["message"]), level="INFO")
    yield lines
    logger.remove(sink_id)


def _open_ids(db_path):
    # _get_db() reads pp.DB_PATH, which paper_db has pointed at db_path.
    assert pp.DB_PATH == db_path
    conn = pp._get_db()
    try:
        return {r["market_id"] for r in conn.execute(
            "SELECT market_id FROM paper_positions WHERE status='open'")}
    finally:
        conn.close()


def test_stopped_market_does_not_abort_batch(paper_db):
    batch = [
        _signal("KXFIRST-26NOV11", "First market alpha", 0.15),
        _signal(STOPPED_ID, "Stopped market", 0.10),  # weaker than PREV_EDGE
        _signal("KXTHIRD-26NOV11", "Third market gamma", 0.15),
    ]
    out = pp.process_signals(batch)

    assert out["processed"] == 3
    assert out["opened"] == 2
    assert out["skipped"] == 1
    actions = {e["market_id"]: e["action"] for e in out["signals"]}
    assert actions["KXFIRST-26NOV11"] == "opened"
    assert actions[STOPPED_ID] == "skipped"
    assert actions["KXTHIRD-26NOV11"] == "opened"
    assert _open_ids(paper_db) == {"KXFIRST-26NOV11", "KXTHIRD-26NOV11"}


def test_stopped_market_rejected_with_stop_reason_not_raise(paper_db):
    res = pp.open_position(_signal(STOPPED_ID, "Stopped market", 0.10))
    assert res["opened"] is False
    assert res["reason"].startswith("Stopped previously")
    assert "not stronger" in res["reason"]
    assert _open_ids(paper_db) == set()


def test_stronger_edge_reenters_and_logs_allowed(paper_db, log_lines):
    res = pp.open_position(_signal(STOPPED_ID, "Stopped market", 0.30))
    assert res["opened"] is True
    assert _open_ids(paper_db) == {STOPPED_ID}
    assert any(
        l == "Re-entry ALLOWED: {} stopped with edge {:.1f}pp, new edge {:.1f}pp is stronger"
        .format(STOPPED_ID, PREV_EDGE, 0.30)
        for l in log_lines), log_lines


@pytest.mark.parametrize("edge, allowed", [
    (0.20, False),    # equal to stored: gate uses <=, so rejected
    (0.2001, True),   # smallest step above (scorer rounds edges to 4 dp)
])
def test_reentry_boundary_is_strict(paper_db, edge, allowed):
    res = pp.open_position(_signal(STOPPED_ID, "Stopped market", edge))
    assert res["opened"] is allowed
    assert (_open_ids(paper_db) == {STOPPED_ID}) is allowed
    if not allowed:
        assert "not stronger" in res["reason"]


@pytest.mark.parametrize("edge, allowed", [
    (0.05, True),   # positive edge beats a NULL stored edge (counts as 0)
    (0.0, False),   # zero does not (0 <= 0)
])
def test_null_stored_edge_counts_as_zero(paper_db, edge, allowed):
    # In real use evaluate_signal marks a zero/negative edge ineligible, so
    # edge 0.0 never reaches this gate; the stubbed case pins the `or 0` anyway.
    market = "KXNULLEDGE-26NOV11"
    _seed_stopped(market, None, "2026-11-01T00:00:00+00:00")
    res = pp.open_position(_signal(market, "Null edge market", edge))
    assert res["opened"] is allowed
    assert (market in _open_ids(paper_db)) is allowed


def test_most_recent_stopped_row_is_the_reference(paper_db):
    # The query orders by closed_at DESC LIMIT 1, so the later row wins.
    # Earlier stop had a high edge (0.50), the later one a low edge (0.10);
    # a 0.30 signal beats the later row only. Insert order is chronological, so a
    # query with no ORDER BY (rowid order) would pick the earlier row and fail.
    market = "KXTWOSTOPS-26NOV11"
    _seed_stopped(market, 0.50, "2026-11-01T00:00:00+00:00")  # earlier
    _seed_stopped(market, 0.10, "2026-11-05T00:00:00+00:00")  # later
    res = pp.open_position(_signal(market, "Two stops market", 0.30))
    assert res["opened"] is True

"""Task 1.11 (#58): one stopped market must not abort a process_signals batch.

open_position's stop-loss re-entry gate used an undefined name `edge_pct`, so a
signal on a market we were previously stopped out of raised NameError and took
the whole process_signals batch down with it. Real process_signals and
open_position run here; only evaluate_signal (heavy, network-adjacent scoring)
and the notifier side channels are stubbed. Storage is redirected to tmp_path.
"""
from datetime import datetime, timezone

import pytest

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


def _open_ids(db_path):
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

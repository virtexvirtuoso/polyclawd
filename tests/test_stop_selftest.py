"""Task 2.2 — synthetic stop selftest (scripts/stop_selftest.py), REDESIGNED.

Local (--local) mode: alert_openclaw is monkeypatched inside the script so
nothing is really sent. Verifies:
* the synthetic 'selftest-<uuid4>' position closes via the UNIVERSAL STOP
  and a 🛑 [SELFTEST] alert goes through the Telegram sender;
* _fetch_price patching is prefix-scoped — other rows delegate to the real
  function;
* ALL paper-accounting side effects are restored (position row gone,
  paper_portfolio_state back to snapshot), while a REAL close that
  interleaves during the run is replayed so its P&L survives;
* module state (fetch fn, sender) is restored afterwards.
"""

import sqlite3
from datetime import datetime, timezone

import pytest

import scripts.openclaw_alerts as oa
import services.stop_evaluator as se
from tests.test_stop_thresholds import SCHEMA, insert_pos

from scripts.stop_selftest import SELFTEST_TITLE, run_selftest


@pytest.fixture
def selftest_db(tmp_path):
    db = tmp_path / "shadow_trades.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO paper_portfolio_state (timestamp, bankroll, peak_bankroll)"
        " VALUES (?, 1000.0, 1000.0)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()
    conn.close()
    return db


def _state_rows(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM paper_portfolio_state ORDER BY id")]
    conn.close()
    return rows


def test_local_selftest_passes_and_cleans_up(selftest_db, monkeypatch):
    # An unrelated open position: proves prefix-scoped delegation.
    insert_pos(selftest_db, market_id="KXOTHER-1",
               market_title="Other market on July 20?")
    delegated = []

    def fake_real_fetch(pos):
        delegated.append(pos["market_id"])
        return (pos["id"], None)  # no price -> row skipped, stays open

    monkeypatch.setattr(se, "_fetch_price", fake_real_fetch)
    fetch_before = se._fetch_price
    sender_before = oa.alert_openclaw

    report = run_selftest(local=True, db_path=selftest_db)

    assert report["ok"] is True, report["notes"]
    assert any("🛑" in m and SELFTEST_TITLE in m for m in report["alerts"])

    conn = sqlite3.connect(selftest_db)
    leftovers = conn.execute(
        "SELECT COUNT(*) FROM paper_positions WHERE market_id LIKE 'selftest-%'"
        " OR market_title LIKE '[SELFTEST]%'").fetchone()[0]
    other_status = conn.execute(
        "SELECT status FROM paper_positions WHERE market_id='KXOTHER-1'"
    ).fetchone()[0]
    conn.close()
    assert leftovers == 0
    assert other_status == "open"

    # paper accounting restored: only the seed state row remains
    assert len(_state_rows(selftest_db)) == 1
    # prefix-scoped patch delegated the non-selftest row to the real fn
    assert delegated == ["KXOTHER-1"]
    # module state restored
    assert se._fetch_price is fetch_before
    assert oa.alert_openclaw is sender_before


def test_interleaved_real_close_survives_restore(selftest_db, monkeypatch):
    # The unrelated position ALSO breaches the universal stop during the run.
    insert_pos(selftest_db, market_id="KXOTHER-2",
               market_title="Other market on July 20?")
    monkeypatch.setattr(se, "_fetch_price", lambda pos: (pos["id"], 0.27))

    report = run_selftest(local=True, db_path=selftest_db)

    assert report["ok"] is True, report["notes"]

    conn = sqlite3.connect(selftest_db)
    other = conn.execute(
        "SELECT status, pnl FROM paper_positions WHERE market_id='KXOTHER-2'"
    ).fetchone()
    leftovers = conn.execute(
        "SELECT COUNT(*) FROM paper_positions WHERE market_id LIKE 'selftest-%'"
    ).fetchone()[0]
    conn.close()
    assert leftovers == 0
    assert other[0] == "stopped"  # the real close stands

    rows = _state_rows(selftest_db)
    # seed row + exactly one replayed row for the real close — the
    # selftest's own bankroll mutation is gone
    assert len(rows) == 2
    assert rows[-1]["bankroll"] == pytest.approx(1000.0 + other[1])
    assert rows[-1]["losses"] == 1
    assert rows[-1]["total_trades"] == 1

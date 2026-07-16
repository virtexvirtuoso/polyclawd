"""Task 5.3 Step 1 — shadow-mode dispatch wiring for the 4 low-risk pipelines.
Direct sends stay authoritative; dispatch(tier=2, shadow=True) runs alongside
with an entity+state dedup_key (F3: never just the pipeline name)."""
import sqlite3

import pytest

import scripts.pm_leaderboard_scraper as lb
import scripts.whale_resolution_tracker as wrt
import signals.alert_dispatch as ad


@pytest.fixture
def wires(monkeypatch, tmp_path):
    sends, dispatches = [], []
    monkeypatch.setattr(lb, "send_telegram", lambda msg, **kw: sends.append(msg) or True)
    monkeypatch.setattr(
        "scripts.openclaw_alerts.alert_openclaw",
        lambda msg, **kw: sends.append(msg) or True)
    monkeypatch.setattr(
        ad, "dispatch",
        lambda pipeline, message, tier, **kw: dispatches.append((pipeline, tier, kw)) or True)
    # isolate the /tmp dedup caches
    monkeypatch.setattr(lb, "_RANK_RISER_DEDUP_FILE", tmp_path / "riser.json")
    monkeypatch.setattr(lb, "_GRAD_DEDUP_FILE", tmp_path / "grad.json")
    return sends, dispatches


def test_rising_wallets_shadow_dispatch(wires):
    sends, dispatches = wires
    lb.alert_rank_risers([{
        "wallet": "0xabc123", "name": "climber", "category": "sports",
        "seed_rank": 30, "current_rank": 5, "pnl": 12345.0,
    }])
    assert len(sends) == 1
    assert len(dispatches) == 1
    pipeline, tier, kw = dispatches[0]
    assert pipeline == "rising_wallets" and tier == 2
    assert kw.get("shadow") is True
    assert "0xabc123" in kw.get("dedup_key", "") and "5" in kw["dedup_key"]


def test_leaderboard_wallets_shadow_dispatch(wires):
    sends, dispatches = wires
    lb.alert_new_discoveries([{
        "wallet": "0xdef456", "name": "bigfish", "pnl": 20_000.0, "volume": 250_000.0,
    }])
    assert len(sends) == 1
    assert len(dispatches) == 1
    pipeline, tier, kw = dispatches[0]
    assert pipeline == "leaderboard_wallets" and tier == 2
    assert kw.get("shadow") is True
    assert "0xdef456" in kw.get("dedup_key", "")


def test_graduation_shadow_dispatch(wires, monkeypatch, tmp_path):
    sends, dispatches = wires
    db = tmp_path / "meta.db"

    def _conn():
        c = sqlite3.connect(str(db))
        c.row_factory = sqlite3.Row
        return c

    conn = _conn()
    conn.execute("""
        CREATE TABLE pm_wallets (
            wallet TEXT PRIMARY KEY, name TEXT, closed_positions INTEGER,
            win_rate REAL, net_pnl REAL, smart INTEGER DEFAULT 0,
            skill_n INTEGER, skill_ret REAL, skill_p REAL)
    """)
    conn.execute(
        "INSERT INTO pm_wallets VALUES ('0xwhale789','ace',40,0.70,90000.0,0,0,0.0,1.0)")
    conn.commit()
    monkeypatch.setattr(lb, "get_db", _conn)
    n = lb.alert_graduations(conn)
    conn.close()
    assert n == 1
    assert len(sends) == 1
    assert len(dispatches) == 1
    pipeline, tier, kw = dispatches[0]
    assert pipeline == "graduation" and tier == 2
    assert kw.get("shadow") is True
    assert "0xwhale789" in kw.get("dedup_key", "")


def test_whale_resolutions_shadow_dispatch(wires):
    sends, dispatches = wires
    wrt._send_summary("🐳 resolution summary text")
    assert len(sends) == 1
    assert len(dispatches) == 1
    pipeline, tier, kw = dispatches[0]
    assert pipeline == "whale_resolutions" and tier == 2
    assert kw.get("shadow") is True
    assert kw.get("dedup_key")  # entity-state digest, non-empty

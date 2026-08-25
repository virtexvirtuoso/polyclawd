# Tests for whale_outcomes (alert outcome labeling) and whale_wallets
# (PM smart-money ledger). All offline — network functions are not exercised.
import json
import sqlite3
import time

import pytest

from signals.whale_outcomes import (
    _correct,
    backfill,
    direction_from_alert,
    get_meta_db,
    ingest_new_alerts,
)
from signals.whale_wallets import (
    get_meta_db as get_wallet_db,
    get_smart_wallets,
    is_smart,
    queue_wallet_seen,
)
from signals.whale_scanner import get_db as get_alerts_db


@pytest.fixture
def meta(tmp_path):
    conn = get_meta_db(tmp_path / "meta.db")
    yield conn
    conn.close()


# ── Direction inference ─────────────────────────────────────────────────────

def test_direction_kalshi_flow_beats_wall():
    assert direction_from_alert("kalshi", "vol_spike_900,taker_YES_88%,level_jump_ask_500", {}) == 1
    assert direction_from_alert("kalshi", "taker_NO_100%", {}) == -1

def test_direction_kalshi_wall_only():
    assert direction_from_alert("kalshi", "level_jump_bid_1200", {}) == 1
    assert direction_from_alert("kalshi", "level_jump_ask_1200", {}) == -1
    assert direction_from_alert("kalshi", "imbalance_flip_6.0x", {}) is None

def test_direction_pm_outcome_mapping():
    payload = {"flow_desc": "BUY Indiana Fever $1,234 (95%)",
               "_outcomes": ["Indiana Fever", "Chicago Sky"]}
    assert direction_from_alert("polymarket", "", payload) == 1
    payload["flow_desc"] = "SELL Indiana Fever $500 (80%)"
    assert direction_from_alert("polymarket", "", payload) == -1
    payload["flow_desc"] = "BUY Chicago Sky $500 (80%)"
    assert direction_from_alert("polymarket", "", payload) == -1
    assert direction_from_alert("polymarket", "", {"flow_desc": "BUY X"}) is None


# ── Correctness math ────────────────────────────────────────────────────────

def test_correct_directional():
    assert _correct(1, 0.40, 0.48) == 1     # long, price up
    assert _correct(1, 0.40, 0.31) == 0     # long, price down
    assert _correct(-1, 0.40, 0.31) == 1    # short, price down
    assert _correct(1, 0.40, 0.401) is None  # sub-epsilon = no-move
    assert _correct(None, 0.40, 0.50) is None
    assert _correct(1, None, 0.50) is None


# ── Ingest + backfill plumbing ──────────────────────────────────────────────

def _insert_alert(conn, ts, platform, market, severity, score, reasons, payload):
    conn.execute(
        "INSERT INTO whale_alerts (ts, platform, market, severity, score, reasons, payload)"
        " VALUES (?,?,?,?,?,?,?)",
        (ts, platform, market, severity, score, reasons, json.dumps(payload)))
    conn.commit()

def test_ingest_captures_price_and_direction(meta, tmp_path):
    alerts = get_alerts_db(tmp_path / "alerts.db")
    _insert_alert(alerts, time.time() - 7200, "kalshi", "KXT-1", "HIGH", 6,
                  "vol_spike_900,taker_YES_88%",
                  {"best_bid": 0.40, "best_ask": 0.44})
    _insert_alert(alerts, time.time() - 7200, "polymarket", "slug-1", "LOW", 3,
                  "vol$_spike_800", {"current_price": 0.62, "condition_id": "0xabc"})
    n = ingest_new_alerts(meta, tmp_path / "alerts.db")
    assert n == 2
    r = meta.execute("SELECT * FROM whale_outcomes WHERE market='KXT-1'").fetchone()
    assert r["direction"] == 1
    assert r["price_at_alert"] == pytest.approx(0.42)
    r2 = meta.execute("SELECT * FROM whale_outcomes WHERE market='slug-1'").fetchone()
    assert r2["direction"] is None          # PM direction resolves at backfill
    assert r2["price_at_alert"] == 0.62
    assert r2["condition_id"] == "0xabc"
    # idempotent: second ingest adds nothing
    assert ingest_new_alerts(meta, tmp_path / "alerts.db") == 0
    alerts.close()

def test_backfill_scores_kalshi_resolution(meta, tmp_path, monkeypatch):
    alerts = get_alerts_db(tmp_path / "alerts.db")
    _insert_alert(alerts, time.time() - 8 * 3600, "kalshi", "KXT-2", "CRITICAL", 9,
                  "taker_YES_95%", {"best_bid": 0.40, "best_ask": 0.44})
    alerts.close()
    ingest_new_alerts(meta, tmp_path / "alerts.db")

    import signals.whale_outcomes as wo
    monkeypatch.setattr(wo, "kalshi_lookup",
                        lambda tickers: {"KXT-2": {"mid": 0.55, "result": "yes"}})
    stats = backfill(meta)
    assert stats["filled"] == 1 and stats["resolved"] == 1
    r = meta.execute("SELECT * FROM whale_outcomes WHERE market='KXT-2'").fetchone()
    assert r["price_1h"] == 0.55
    assert r["correct_1h"] == 1             # long from 0.42 -> 0.55
    assert r["correct_res"] == 1            # resolved yes, whale was long
    assert r["done"] == 1


# ── Wallet ledger ───────────────────────────────────────────────────────────

def test_is_smart_criteria():
    """Non-skill path: WR floor AND net floor, both from the module constants.

    Refreshed 2026-08-21 -- this test had gone stale against two deliberate
    tightenings (WR 0.55 -> 0.62 on 2026-06-25, net $1k -> $100k on
    2026-06-20) and had been failing ever since. Assert against the constants
    rather than hardcoded numbers so the next tightening cannot silently
    desync it again.
    """
    from signals.whale_wallets import SMART_MIN_CLOSED, SMART_MIN_NET, SMART_MIN_WIN_RATE

    def stats(closed, wr, net):
        return {"closed": closed, "wins": round(closed * wr),
                "realized": net, "net": net}

    # Clears both floors comfortably.
    assert is_smart(stats(40, 0.70, SMART_MIN_NET * 1.5))
    # Too few closed positions to judge.
    assert not is_smart(stats(SMART_MIN_CLOSED - 1, 0.90, SMART_MIN_NET * 1.5))
    # Win rate below the floor, however profitable.
    assert not is_smart(stats(40, SMART_MIN_WIN_RATE - 0.05, SMART_MIN_NET * 9))
    # Net below the floor, however high the win rate.
    assert not is_smart(stats(40, 0.70, SMART_MIN_NET - 1))
    # Realized profit hiding unrealized wreckage must NOT qualify.
    assert not is_smart({"closed": 40, "wins": 28, "realized": 5000.0, "net": -1000.0})


def test_compute_stats_counts_zombie_losses():
    """Positions held to worthless resolution never get realizedPnl set —
    they must count as losses or longshot sprayers read as 100% winners
    (investigation 2026-06-12: pd.unique '100%/202' with 3,047 zombies)."""
    from signals.whale_wallets import compute_stats
    rows = [
        # 2 realized wins
        {"realizedPnl": 100.0, "size": 0, "currentValue": 0, "initialValue": 50, "cashPnl": 0},
        {"realizedPnl": 40.0, "size": 0, "currentValue": 0, "initialValue": 50, "cashPnl": 0},
        # 1 realized loss (sold at a loss)
        {"realizedPnl": -30.0, "size": 10, "currentValue": 0, "initialValue": 30, "cashPnl": 0},
        # 3 zombies: held, worthless, no realized pnl -> count as losses
        {"realizedPnl": 0.0, "size": 100, "currentValue": 0.0, "initialValue": 48.0, "cashPnl": -48.0},
        {"realizedPnl": 0.0, "size": 100, "currentValue": 0.1, "initialValue": 48.0, "cashPnl": -47.9},
        {"realizedPnl": 0.0, "size": 100, "currentValue": 0.0, "initialValue": 48.0, "cashPnl": -48.0},
        # 1 healthy open position (not a zombie, counts toward net via cashPnl)
        {"realizedPnl": 0.0, "size": 100, "currentValue": 60.0, "initialValue": 48.0, "cashPnl": 12.0},
        # dust holding below $1 basis: ignored as zombie
        {"realizedPnl": 0.0, "size": 1, "currentValue": 0.0, "initialValue": 0.5, "cashPnl": -0.5},
    ]
    s = compute_stats(rows)
    assert s["zombies"] == 3
    assert s["closed"] == 3 + 3            # realized events + zombies
    assert s["wins"] == 2
    assert s["realized"] == 110.0
    assert s["net"] == 110.0 + (-48.0 - 47.9 - 48.0 + 12.0 - 0.5)
    assert s["wins"] / s["closed"] < 0.5   # the honest win rate

def test_queue_and_smart_lookup(tmp_path):
    conn = get_wallet_db(tmp_path / "w.db")
    queue_wallet_seen(conn, "0xabc", "Trader", 500.0)
    queue_wallet_seen(conn, "0xabc", "Trader", 300.0)   # accumulates
    queue_wallet_seen(conn, "0xtiny", "Small", 5.0)     # below floor, ignored
    conn.commit()
    rows = conn.execute("SELECT * FROM pm_wallet_seen").fetchall()
    assert len(rows) == 1
    assert rows[0]["dollars"] == 800.0

    conn.execute("INSERT INTO pm_wallets (wallet, name, closed_positions, wins,"
                 " win_rate, realized_pnl, smart, refreshed)"
                 " VALUES ('0xwin','Winner',40,28,0.7,2000,1,?)", (time.time(),))
    conn.commit()
    smart = get_smart_wallets(conn)
    assert "0xwin" in smart and smart["0xwin"]["win_rate"] == 0.7
    assert "0xabc" not in smart
    conn.close()

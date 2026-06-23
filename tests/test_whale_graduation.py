"""Regression: smart-wallet graduation must not be demoted by the flow-activity
check. pm_wallet_seen is one-row-per-wallet, so the old COUNT(*)<5 test was
unsatisfiable and demoted every wallet on promotion (roster stuck empty,
diagnosed 2026-06-23)."""
import time

import pytest

from signals import whale_wallets as ww


@pytest.fixture()
def meta(tmp_path):
    return ww.get_meta_db(tmp_path / "meta.db")


def _seed_smart(conn, wallet, last_seen):
    now = time.time()
    conn.execute(
        "INSERT INTO pm_wallets (wallet, name, first_seen, last_seen, "
        " closed_positions, wins, win_rate, realized_pnl, net_pnl, zombies, "
        " smart, refreshed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (wallet, "RN1", now, now, 2970, 1791, 0.603, 2_000_505, 1_582_337, 0,
         1, now))
    conn.execute(
        "INSERT INTO pm_wallet_seen (wallet, name, dollars, last_seen) "
        "VALUES (?,?,?,?)", (wallet, "RN1", 5000.0, last_seen))
    conn.commit()


def test_recently_active_smart_wallet_survives(meta, monkeypatch):
    # strong stats, sliding-WR healthy, seen in flow today
    monkeypatch.setattr(ww, "fetch_wallet_stats",
                        lambda w: {"closed": 2970, "wins": 1791,
                                   "realized": 2_000_505, "net": 1_582_337,
                                   "zombies": 0})
    _seed_smart(meta, "0xRN1", last_seen=time.time())
    res = ww.demote_stale_wallets(meta)
    assert res["demoted"] == 0
    assert meta.execute(
        "SELECT smart FROM pm_wallets WHERE wallet='0xRN1'").fetchone()[0] == 1


def test_flow_stale_30d_is_demoted(meta, monkeypatch):
    monkeypatch.setattr(ww, "fetch_wallet_stats",
                        lambda w: {"closed": 2970, "wins": 1791,
                                   "realized": 2_000_505, "net": 1_582_337,
                                   "zombies": 0})
    _seed_smart(meta, "0xRN1", last_seen=time.time() - 31 * 86400)
    res = ww.demote_stale_wallets(meta)
    assert res["demoted"] == 1
    assert meta.execute(
        "SELECT smart FROM pm_wallets WHERE wallet='0xRN1'").fetchone()[0] == 0

"""Rule 0: live trades must carry an allowlisted strategy category."""

import sqlite3
import pytest

from execution import live_db, live_config
from execution.risk_governor import RiskGovernor


@pytest.fixture
def gov(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYCLAWD_LIVE_STRATEGY_ALLOWLIST", "smart_wallet,baseball_total,soccer_match_3way")
    conn = live_db.connect(path=tmp_path / "t.db")
    g = RiskGovernor(conn, mode="LIVE")
    g.set_bankroll(100.0)
    return g


def test_allowlisted_strategy_passes_rule0(gov):
    d = gov.check({"size_usd": 5.0, "market_id": "m1", "category": "baseball_total"})
    assert "strategy_allowlist" not in d.reason


def test_unlisted_strategy_rejected(gov):
    d = gov.check({"size_usd": 5.0, "market_id": "m1", "category": "price_above"})
    assert d.allowed is False
    assert "strategy_allowlist" in d.reason


def test_missing_strategy_rejected_fail_closed(gov):
    d = gov.check({"size_usd": 5.0, "market_id": "m1"})
    assert d.allowed is False
    assert "strategy_allowlist" in d.reason


def test_empty_allowlist_env_blocks_everything(gov, monkeypatch):
    monkeypatch.setenv("POLYCLAWD_LIVE_STRATEGY_ALLOWLIST", "")
    d = gov.check({"size_usd": 5.0, "market_id": "m1", "category": "baseball_total"})
    assert d.allowed is False

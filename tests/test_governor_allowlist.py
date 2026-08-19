"""Rule 0: live trades must carry an allowlisted strategy category."""

import pytest

from execution import live_db, live_config
from execution.risk_governor import Decision, RiskGovernor


@pytest.fixture
def gov(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYCLAWD_LIVE_STRATEGY_ALLOWLIST", "smart_wallet,baseball_total,soccer_match_3way")
    conn = live_db.connect(path=tmp_path / "t.db")
    g = RiskGovernor(conn, mode="LIVE")
    g.set_bankroll(100.0)
    return g


def test_allowlisted_strategy_passes_rule0(gov):
    d = gov.check({"size_usd": 5.0, "market_id": "m1", "category": "baseball_total"})
    assert d.allowed is True
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


def test_maker_path_is_gated_by_entry_check(tmp_path, monkeypatch):
    """The entry-gate governor.check() inside execute_intent must block BEFORE
    any vendor order is posted (Task 3: maker legs previously bypassed ALL
    risk caps). This must FAIL if that entry gate is ever deleted — a blocking
    governor stub is used, and the test proves NO order was posted."""
    import execution.live_executor as le

    class _BlockingGov:
        def check(self, intent):
            return Decision(
                False,
                "strategy_allowlist: category price_above not in "
                "['baseball_total', 'smart_wallet', 'soccer_match_3way']",
            )

    posted = []

    def _fake_post_maker(**kw):
        posted.append(kw)
        return {"orderID": "SHOULD-NEVER-POST"}

    monkeypatch.setattr(le.clob_client, "post_maker", _fake_post_maker)
    # Defensive only: if the entry gate is ever deleted, these keep the test
    # failing FAST (via the posted == [] assertion below) rather than hanging
    # on the real maker-wait poll loop.
    monkeypatch.setattr(le.live_config, "maker_wait_secs", lambda: 0)
    monkeypatch.setattr(le, "_wait_for_maker_fill", lambda oid, timeout: True)

    # The idempotency check (Step 0, before the gate) queries live_open_orders
    # via conn — give it a real, initialised in-memory-on-disk DB rather than
    # None so that pre-gate code path works exactly as in production.
    conn = live_db.connect(path=tmp_path / "gate.db")

    res = le.execute_intent(
        conn,
        _BlockingGov(),
        token_id="tok-1",
        side="BUY",
        fair_price=0.5,
        size_usd=5.0,
        tick_size=0.01,
        neg_risk=False,
        net_edge_taker=0.10,
        client_order_ref="gate-test-ref",
        category="price_above",
    )

    assert res["action"] == "dropped"
    assert "governor:" in res["reason"]
    assert posted == []  # load-bearing: proves no vendor order was posted


def test_per_trade_cap_is_min_of_env_and_bankroll_fraction(gov, monkeypatch):
    monkeypatch.setenv("POLYCLAWD_WEATHER_PER_TRADE_CAP", "15.0")
    monkeypatch.setenv("POLYCLAWD_PER_TRADE_FRAC", "0.10")
    # gov fixture bankroll = 100 → frac cap = $10 < env $15 → effective cap $10
    d = gov.check({"size_usd": 12.0, "market_id": "m1", "category": "baseball_total"})
    assert d.allowed is False
    assert "per_trade_cap" in d.reason
    d = gov.check({"size_usd": 9.0, "market_id": "m1", "category": "baseball_total"})
    assert "per_trade_cap" not in d.reason
    assert d.allowed is True


def test_per_trade_cap_env_arm_binds_when_frac_is_looser(gov, monkeypatch):
    monkeypatch.setenv("POLYCLAWD_WEATHER_PER_TRADE_CAP", "15.0")
    monkeypatch.setenv("POLYCLAWD_PER_TRADE_FRAC", "0.50")
    # gov fixture bankroll = 100 → frac cap = $50 > env $15 → effective cap $15 (flat arm binds)
    d = gov.check({"size_usd": 16.0, "market_id": "m1", "category": "baseball_total"})
    assert d.allowed is False
    assert "per_trade_cap" in d.reason
    d = gov.check({"size_usd": 15.0, "market_id": "m1", "category": "baseball_total"})
    assert "per_trade_cap" not in d.reason
    assert d.allowed is True


def test_per_trade_cap_frac_arm_boundary_exactly_allowed(gov, monkeypatch):
    monkeypatch.setenv("POLYCLAWD_WEATHER_PER_TRADE_CAP", "100.0")
    monkeypatch.setenv("POLYCLAWD_PER_TRADE_FRAC", "0.10")
    # gov fixture bankroll = 100 → frac cap = $10.00 exactly; strict > means $10.00 is allowed
    d = gov.check({"size_usd": 10.0, "market_id": "m1", "category": "baseball_total"})
    assert d.allowed is True
    assert "per_trade_cap" not in d.reason

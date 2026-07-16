"""execute_intent must thread market_title through to record_real_fill.

Hex-ID fix groundwork (Alert Overhaul plan, Task 3.4): live_positions rows were
born with market_title='' because the three record_real_fill call sites in
live_executor never passed a title. All vendor/DB calls are monkeypatched —
no network, no real DB writes.
"""
import execution.live_executor as le

TITLE = "Will it rain in NYC on July 20?"


class _Gov:
    def record_fill(self, **kw):
        pass


def _stub_maker_path(monkeypatch, captured):
    monkeypatch.setattr(le.live_db, "get_open_order_by_ref", lambda conn, ref: None)
    monkeypatch.setattr(le.live_db, "record_open_order", lambda conn, **kw: None)
    monkeypatch.setattr(le.live_db, "update_open_order_status", lambda conn, oid, st: None)
    monkeypatch.setattr(le, "_maker_slice_depth", lambda tid: 100.0)
    monkeypatch.setattr(le.clob_client, "post_maker", lambda **kw: {"orderID": "oid-1"})
    monkeypatch.setattr(le.live_config, "maker_wait_secs", lambda: 0)
    monkeypatch.setattr(le, "_wait_for_maker_fill", lambda oid, timeout: True)
    # Single slice fully matched: 10 USD @ 0.5 = 20 shares.
    monkeypatch.setattr(
        le, "_poll_until_settled",
        lambda oid, label="": {"size_matched": 20.0, "status": "MATCHED"},
    )

    def fake_rrf(conn, **kw):
        captured.update(kw)
        return 1

    monkeypatch.setattr(le.live_position_tracker, "record_real_fill", fake_rrf)


def test_maker_fill_passes_market_title(monkeypatch):
    captured = {}
    _stub_maker_path(monkeypatch, captured)
    res = le.execute_intent(
        None, _Gov(),
        token_id="7132104567925221259462638553270691275033272857194",
        side="BUY", fair_price=0.5, size_usd=10.0, tick_size=0.01,
        neg_risk=False, net_edge_taker=0.10, client_order_ref="t-ref-1",
        market_title=TITLE,
    )
    assert res["action"] == "maker_filled"
    assert captured["market_title"] == TITLE


def test_market_title_defaults_empty_for_legacy_callers(monkeypatch):
    captured = {}
    _stub_maker_path(monkeypatch, captured)
    res = le.execute_intent(
        None, _Gov(),
        token_id="7132104567925221259462638553270691275033272857194",
        side="BUY", fair_price=0.5, size_usd=10.0, tick_size=0.01,
        neg_risk=False, net_edge_taker=0.10, client_order_ref="t-ref-2",
    )
    assert res["action"] == "maker_filled"
    assert captured["market_title"] == ""

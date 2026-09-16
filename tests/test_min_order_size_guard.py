"""Vendor minimum-order-size guard (Polymarket rejects orders below
``min_order_size`` SHARES, read per market from GET /book).

Two independent failure modes are covered:

  1. Whole-order pre-flight — at a $3.45 canary size any market priced above
     0.690 buys fewer than 5 shares and is rejected by the vendor. Nothing in
     the codebase checked this, so 100% of such orders were posted and bounced.
  2. Per-slice ladder — ``_maker_slice_depth`` splits an order across top-of-book
     USD depth. On a thin book an otherwise-valid order fragments into sub-minimum
     slices, and the exchange validates EACH slice independently.

All vendor/DB calls are monkeypatched — no network, no real DB writes.
"""

import pytest

import execution.live_executor as le
from execution.risk_governor import Decision

TOKEN = "7132104567925221259462638553270691275033272857194"
MIN_SHARES = 5.0


class _Gov:
    def __init__(self):
        self.fills = []

    def check(self, intent):
        return Decision(True, "ok")

    def record_fill(self, **kw):
        self.fills.append(kw)


def _stub(monkeypatch, *, slice_depth=1000.0, min_shares=MIN_SHARES, posted=None):
    """Stub every side effect. `posted` collects the per-slice post_maker kwargs."""
    monkeypatch.setattr(le, "_min_order_shares", lambda tid: min_shares)
    monkeypatch.setattr(le, "_maker_slice_depth", lambda tid: slice_depth)
    monkeypatch.setattr(le.live_db, "get_open_order_by_ref", lambda conn, ref: None)
    monkeypatch.setattr(le.live_db, "record_open_order", lambda conn, **kw: None)
    monkeypatch.setattr(le.live_db, "update_open_order_status", lambda conn, oid, st: None)
    monkeypatch.setattr(le.live_config, "maker_wait_secs", lambda: 0)
    monkeypatch.setattr(le, "_wait_for_maker_fill", lambda oid, timeout: True)
    monkeypatch.setattr(le.live_position_tracker, "record_real_fill", lambda conn, **kw: 1)

    calls = posted if posted is not None else []
    counter = {"n": 0}

    def fake_post(**kw):
        counter["n"] += 1
        oid = f"oid-{counter['n']}"
        calls.append(dict(kw, order_id=oid))
        return {"orderID": oid}

    monkeypatch.setattr(le.clob_client, "post_maker", fake_post)

    # Every slice fills exactly what was requested (looked up lazily — `calls`
    # is still empty at stub time).
    def fake_poll(oid, label=""):
        by_oid = {c["order_id"]: c["size"] for c in calls}
        return {"size_matched": by_oid.get(oid, 0.0), "status": "MATCHED"}

    monkeypatch.setattr(le, "_poll_until_settled", fake_poll)
    return calls


def _run(gov, *, price, size_usd, ref):
    return le.execute_intent(
        None,
        gov,
        token_id=TOKEN,
        side="BUY",
        fair_price=price,
        size_usd=size_usd,
        tick_size=0.01,
        neg_risk=False,
        net_edge_taker=0.10,
        client_order_ref=ref,
    )


# ---------------------------------------------------------------------------
# 1. Whole-order pre-flight
# ---------------------------------------------------------------------------


def test_order_below_vendor_minimum_is_skipped_and_posts_nothing(monkeypatch):
    """$3.45 @ 0.75 = 4.6 shares < 5 → skipped, and NO order reaches the vendor."""
    posted = _stub(monkeypatch)
    res = _run(_Gov(), price=0.75, size_usd=3.45, ref="min-1")

    assert res["action"] == "skipped_min_size"
    assert res["min_shares"] == MIN_SHARES
    assert res["intended_shares"] == pytest.approx(4.6)
    assert "below vendor minimum" in res["reason"]
    assert posted == [], "an order below the vendor minimum must never be posted"


def test_order_above_vendor_minimum_is_allowed(monkeypatch):
    """$3.45 @ 0.50 = 6.9 shares > 5 → normal maker path, one slice posted."""
    posted = _stub(monkeypatch)
    res = _run(_Gov(), price=0.50, size_usd=3.45, ref="min-2")

    assert res["action"] == "maker_filled"
    assert len(posted) == 1
    assert posted[0]["size"] == pytest.approx(6.9)


def test_boundary_exactly_min_shares_is_allowed(monkeypatch):
    """EXACTLY 5.0 shares is legal — only strictly-below is rejected."""
    posted = _stub(monkeypatch)
    res = _run(_Gov(), price=0.50, size_usd=2.50, ref="min-3")  # 2.50/0.50 == 5.0

    assert res["action"] == "maker_filled"
    assert len(posted) == 1
    assert posted[0]["size"] == pytest.approx(5.0)


def test_boundary_one_tick_below_min_shares_is_skipped(monkeypatch):
    """4.99 shares → skipped. Guards the epsilon from swallowing real breaches."""
    posted = _stub(monkeypatch)
    res = _run(_Gov(), price=0.50, size_usd=2.495, ref="min-4")  # 4.99 shares

    assert res["action"] == "skipped_min_size"
    assert posted == []


def test_per_market_minimum_is_read_not_hardcoded(monkeypatch):
    """A market whose vendor minimum is 15 shares must reject a 10-share order
    that would pass a hardcoded 5."""
    posted = _stub(monkeypatch, min_shares=15.0)
    res = _run(_Gov(), price=0.50, size_usd=5.0, ref="min-5")  # 10 shares

    assert res["action"] == "skipped_min_size"
    assert res["min_shares"] == 15.0
    assert posted == []


# ---------------------------------------------------------------------------
# 2. Per-slice ladder
# ---------------------------------------------------------------------------


def test_ladder_never_emits_a_slice_below_the_minimum(monkeypatch):
    """Thin book (slice depth $1.00) + $10 @ 0.50: naive laddering would post ten
    $1.00 slices = 2 shares each, ALL rejected. Every emitted slice must clear 5."""
    posted = _stub(monkeypatch, slice_depth=1.0)
    res = _run(_Gov(), price=0.50, size_usd=10.0, ref="lad-1")

    assert res["action"] == "maker_filled"
    assert posted, "the order itself is valid and must still be placed"
    for call in posted:
        assert call["size"] >= MIN_SHARES - 1e-9, f"slice below vendor minimum: {call['size']}"
    assert sum(c["size"] for c in posted) == pytest.approx(20.0)  # 10 USD / 0.50


def test_ladder_tail_is_merged_not_emitted(monkeypatch):
    """$10 @ 0.90 with min 5 shares → min rung $4.50. Chunks would be
    4.50/4.50/1.00; the $1.00 tail (1.11 sh) must be MERGED, not posted."""
    chunks = le._ladder_slices(10.0, 1.0, 0.90, MIN_SHARES)

    assert len(chunks) == 2
    assert chunks == pytest.approx([4.5, 5.5])
    assert sum(chunks) == pytest.approx(10.0)
    for c in chunks:
        assert c / 0.90 >= MIN_SHARES - 1e-9


def test_ladder_preserves_notional_and_respects_wide_book():
    """A book deeper than the minimum keeps depth-driven laddering."""
    chunks = le._ladder_slices(100.0, 30.0, 0.50, MIN_SHARES)  # min rung = $2.50

    assert chunks == pytest.approx([30.0, 30.0, 30.0, 10.0])
    assert sum(chunks) == pytest.approx(100.0)
    for c in chunks:
        assert c / 0.50 >= MIN_SHARES - 1e-9


def test_ladder_single_slice_when_order_equals_minimum():
    """An order exactly at the minimum is one rung, never fragmented."""
    chunks = le._ladder_slices(2.50, 0.10, 0.50, MIN_SHARES)

    assert chunks == pytest.approx([2.50])


# ---------------------------------------------------------------------------
# 3. Taker remainder
# ---------------------------------------------------------------------------


def test_taker_remainder_below_minimum_is_dropped(monkeypatch):
    """Maker partially fills, leaving 2 shares. The FAK would be rejected — drop."""
    posted = _stub(monkeypatch)
    # Requested 20 shares ($10 @ 0.50); only 18 fill, remainder = 2 sh < 5.
    monkeypatch.setattr(le, "_poll_until_settled", lambda oid, label="": {"size_matched": 18.0, "status": "LIVE"})
    monkeypatch.setattr(le.clob_client, "cancel", lambda oid: {"canceled": [oid]})

    crossed = []
    monkeypatch.setattr(le.clob_client, "cross_taker", lambda **kw: crossed.append(kw) or {})

    res = _run(_Gov(), price=0.50, size_usd=10.0, ref="tak-1")

    assert crossed == [], "must not post a FAK below the vendor minimum"
    # A partial maker fill still reports maker_filled; the taker-skip reason is
    # attached separately by _finish_dropped_or_partial.
    assert res["action"] == "maker_filled"
    assert res["shares"] == pytest.approx(18.0)
    assert "below vendor minimum" in res["taker_skipped_reason"]


# ---------------------------------------------------------------------------
# 4. Lookup fallback
# ---------------------------------------------------------------------------


def test_min_order_shares_falls_back_to_vendor_default_on_lookup_failure(monkeypatch):
    """A vendor outage must not crash execution — fall back to the documented 5."""

    def boom(token_id):
        raise RuntimeError("clob unreachable")

    monkeypatch.setattr(le.clob_client, "get_min_order_size", boom)
    assert le._min_order_shares(TOKEN) == pytest.approx(le.clob_client.DEFAULT_MIN_ORDER_SHARES)

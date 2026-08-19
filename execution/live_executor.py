"""Phase F — Hybrid maker→taker live executor.

The core money-routing logic for live weather trading. Always tries a
post-only maker leg first (0% fee). If the maker order does not fill within
the configured window, it falls back to a taker cross *only* when:
  - net-of-fee taker edge >= min_taker_edge(), AND
  - the order book can execute the size at acceptable quality
    (depth >= 1.5x, slippage <= 200bps, spread <= 5c), AND
  - the risk governor allows it.
Otherwise the resting maker order is cancelled and the intent is dropped.

Design for testability
-----------------------
Every side-effecting dependency is reached through an INJECTED module-level
function or the passed-in ``governor`` / ``conn`` so tests can substitute
fakes without any network, sleeping, or real orders:

  * order placement / fill polling     → execution.clob_client (set_client)
  * risk gate                          → the passed governor object
  * persistence                        → execution.live_db / live_position_tracker
  * the maker wait/poll                → _wait_for_maker_fill (override in tests)
  * maker slice depth for laddering    → _maker_slice_depth (monkeypatch in tests)
  * taker pre-trade safety             → can_execute (uses size_to_book)

No module-level state. execute_intent() is the only public entry point.

Deployed-cap contract (I1) — IMPORTANT FOR PHASE G
--------------------------------------------------
On every recorded fill this executor calls ``governor.record_fill(usd=...)``,
which BUMPS the governor's ``deployed_usd`` running total. That total backs the
60% max-deployed cap (``max_deployed_frac``). The executor NEVER decrements it —
it only ever opens exposure. Therefore Phase G (position exit) MUST call
``governor.record_close(market_id=..., usd=...)`` when a position is closed to
RELEASE the deployed_usd it consumed. If exits do not call record_close, the
deployed total grows monotonically, the 60% cap over-counts, and the governor
will eventually (and wrongly) reject all new trades. No behaviour change is made
here — this is the wiring contract Phase G must honour.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from loguru import logger

from execution import clob_client, fee_model, live_config, live_db, live_position_tracker


# ---------------------------------------------------------------------------
# Pre-trade safety — taker can_execute check via the order book
# ---------------------------------------------------------------------------

# Quality gates for crossing taker (reused from the Phase C/D order-book checks).
_MIN_DEPTH_MULT = 1.5  # book must hold >= 1.5x the requested USD
_MAX_SLIPPAGE_BPS = 200.0  # <= 200 bps (2%) VWAP slippage vs top of book
_MAX_SPREAD = 0.05  # <= 5 cents spread


def can_execute(token_id: str, side: str, size_usd: float) -> bool:
    """Return True iff the book can absorb a taker cross of *size_usd* at
    acceptable quality (depth >= 1.5x, slippage <= 200bps, spread <= 5c).

    Implemented via odds.polymarket_clob.size_to_book, which walks the live
    book. Imported lazily so tests can monkeypatch it on the module. Any
    exception or a non-tradeable book → False (fail safe: never cross blind).
    """
    try:
        from odds.polymarket_clob import size_to_book
    except Exception:  # pragma: no cover - import guard
        return False

    try:
        est = size_to_book(
            token_id=token_id,
            side=side,
            target_usd=size_usd * _MIN_DEPTH_MULT,
            max_slip_bps=_MAX_SLIPPAGE_BPS,
            max_spread=_MAX_SPREAD,
        )
    except Exception as exc:
        logger.warning("can_execute: size_to_book raised (treating as non-tradeable): {}", exc)
        return False

    if est is None or not getattr(est, "ok", False):
        return False
    # Depth: the book must hold at least 1.5x the size we actually want to cross.
    if getattr(est, "actual_usd", 0.0) < size_usd * _MIN_DEPTH_MULT:
        return False
    if getattr(est, "slippage_bps", 0.0) > _MAX_SLIPPAGE_BPS:
        return False
    if getattr(est, "spread", 0.0) > _MAX_SPREAD:
        return False
    return True


# ---------------------------------------------------------------------------
# Maker slice depth — laddering helper
# ---------------------------------------------------------------------------


def _maker_slice_depth(token_id: str) -> float:
    """Return the USD depth of a single maker slice for *token_id*.

    Real implementation reads top-of-book depth from the live order book so a
    large order is laddered across price levels rather than parked entirely at
    one level. Tests monkeypatch this to a small fixed number to force
    laddering deterministically.

    Falls back to per_trade_cap() (i.e. "one slice, no laddering") if the book
    can't be read — never raises.
    """
    try:
        from odds.polymarket_clob import get_orderbook

        book = get_orderbook(token_id)
        if book is not None and getattr(book, "asks", None):
            top = book.asks[0]
            depth = float(top.price) * float(top.size)
            if depth > 0:
                return depth
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("_maker_slice_depth: book read failed, no laddering: {}", exc)
    return live_config.per_trade_cap()


# ---------------------------------------------------------------------------
# Maker fill polling — MOCKABLE (tests override to avoid real sleeping)
# ---------------------------------------------------------------------------


def _wait_for_maker_fill(order_id: str, timeout: float) -> bool:
    """Poll the CLOB for up to *timeout* seconds; return True once filled.

    Real implementation polls clob_client.get_order(order_id) every few
    seconds and checks clob_client.order_is_filled. Tests OVERRIDE this whole
    function (monkeypatch the module attribute) so they never sleep the real
    maker_wait_secs window.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    poll_interval = 5.0
    while time.monotonic() < deadline:
        try:
            status = clob_client.get_order(order_id)
            if clob_client.order_is_filled(status):
                return True
        except Exception as exc:
            logger.warning("_wait_for_maker_fill: get_order failed (retrying): {}", exc)
        time.sleep(poll_interval)
    # Final check at the deadline.
    try:
        status = clob_client.get_order(order_id)
        return clob_client.order_is_filled(status)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _shares_for(side: str, price: float, size_usd: float) -> float:
    """Convert a USD notional into share count at *price* (price is in 0..1)."""
    if price <= 0:
        return 0.0
    return size_usd / price


def _order_id_of(resp: object) -> str:
    """Extract the order ID from a vendor post response dict."""
    if isinstance(resp, dict):
        return str(resp.get("orderID") or resp.get("id") or resp.get("order_id") or "")
    return ""


def _coerce_float(val: object) -> float:
    """Best-effort float coercion (vendor sends sizes/prices as strings)."""
    if val is None:
        return 0.0
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _matched_shares_of(order_status: object) -> float:
    """Read the ACTUAL matched shares from a get_order dict.

    Tolerates snake_case (size_matched / matched_size) and camelCase
    (sizeMatched). Returns 0.0 if no recognised field is present.
    """
    if not isinstance(order_status, dict):
        return 0.0
    for key in ("size_matched", "sizeMatched", "matched_size", "matchedSize"):
        if order_status.get(key) is not None:
            return _coerce_float(order_status.get(key))
    return 0.0


def _status_label(order_status: object) -> str:
    """Map a get_order dict to a coarse live_open_orders status string.

    "filled" when fully matched, "cancelled"/"expired" for terminal non-fill
    states, else "live" (still resting / partially filled).
    """
    if not isinstance(order_status, dict):
        return "live"
    raw = str(order_status.get("status", "")).strip().upper()
    if raw in ("MATCHED", "FILLED"):
        return "filled"
    if raw in ("CANCELED", "CANCELLED", "EXPIRED"):
        return "cancelled"
    return "live"


# Map a category to a fee — exposed so callers/tests can reach fee_model via the
# module (live_executor.fee_model) without a second import.
__all__ = ["execute_intent", "execute_exit", "can_execute", "fee_model"]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def execute_intent(
    conn,
    governor,
    *,
    token_id: str,
    side: str,
    fair_price: float,
    size_usd: float,
    tick_size: float,
    neg_risk: bool,
    net_edge_taker: float,
    client_order_ref: str,
    category: str = "weather",
    market_title: str = "",
) -> dict:
    """Route a single trade intent through the hybrid maker→taker executor.

    Sequence:
      0. Idempotency: if client_order_ref already exists in live_open_orders,
         return action "skipped_duplicate" WITHOUT posting anything.
      1. Maker-first: post a post-only maker leg (laddered across slices if the
         size exceeds a single slice's depth), then wait up to maker_wait_secs.
      2. On maker fill → record fill (fee=0, liquidity="maker"), tell governor,
         return action "maker_filled".
      3. On no maker fill → cancel the resting maker order(s), then consider a
         taker cross. Cross only if net_edge_taker >= min_taker_edge() AND
         can_execute() AND governor.check() allows. On taker fill → record
         (real leg_fee, liquidity="taker"), tell governor, action "taker_filled".
      4. Otherwise drop: action "dropped" (resting maker already cancelled).

    Returns a result dict. Always contains keys: action, client_order_ref.
    action ∈ {"maker_filled", "taker_filled", "dropped", "skipped_duplicate"}.
    """
    result: dict = {
        "action": None,
        "client_order_ref": client_order_ref,
        "token_id": token_id,
        "side": side,
    }

    # ── Step 0: idempotency guard (BEFORE any vendor post) ──────────────────
    existing = live_db.get_open_order_by_ref(conn, client_order_ref)
    if existing is not None:
        result["action"] = "skipped_duplicate"
        result["reason"] = "duplicate client_order_ref"
        result["order_id"] = existing.get("order_id")
        logger.info("execute_intent: skipping duplicate ref={}", client_order_ref)
        return result

    # ── Step 0b: governor entry gate (BEFORE any vendor post) ───────────────
    # Covers the maker path too — previously governor.check() was only called
    # on the taker leg (line ~493), so maker legs bypassed ALL risk caps.
    entry_decision = governor.check(
        {"size_usd": size_usd, "market_id": token_id, "token_id": token_id,
         "category": category}
    )
    if not entry_decision.allowed:
        result["action"] = "dropped"
        result["reason"] = f"governor: {entry_decision.reason}"
        return result

    # ── Step 1: maker-first, laddered across slices ─────────────────────────
    slice_depth = _maker_slice_depth(token_id)
    if slice_depth is None or slice_depth <= 0:
        slice_depth = size_usd  # one slice

    # Build per-slice USD chunks.
    slices: list[float] = []
    remaining = size_usd
    while remaining > 1e-9:
        chunk = min(slice_depth, remaining)
        slices.append(chunk)
        remaining -= chunk
    if not slices:
        slices = [size_usd]

    # Per-slice bookkeeping: requested shares per order_id so that after polling
    # we can map matched-vs-requested per slice and derive the unfilled remainder.
    maker_order_ids: list[str] = []
    slice_requested_shares: dict[str, float] = {}
    maker_price = fair_price  # rest at fair (passive); post_only rejects if it would cross
    for chunk_usd in slices:
        chunk_shares = _shares_for(side, maker_price, chunk_usd)
        resp = clob_client.post_maker(
            token_id=token_id,
            side=side,
            price=maker_price,
            size=chunk_shares,
            tick_size=tick_size,
            neg_risk=neg_risk,
        )
        oid = _order_id_of(resp)
        maker_order_ids.append(oid)
        slice_requested_shares[oid] = chunk_shares
        # Persist for restart-safety + idempotency. The FIRST slice carries the
        # client_order_ref (UNIQUE) — subsequent slices store the ref too but
        # would collide, so only the first row owns it; the others are recorded
        # under the order_id without a ref to avoid an IntegrityError.
        try:
            live_db.record_open_order(
                conn,
                client_order_ref=client_order_ref if not maker_order_ids[:-1] else None,
                order_id=oid,
                token_id=token_id,
                side=side,
                price=maker_price,
                size=chunk_shares,
                status="live",
                ts=_utcnow(),
            )
        except Exception as exc:
            # A racing duplicate ref is the only expected failure; treat as dup.
            logger.warning("execute_intent: record_open_order failed ({}); treating as duplicate", exc)
            result["action"] = "skipped_duplicate"
            result["reason"] = "duplicate client_order_ref (race)"
            return result

    primary_oid = maker_order_ids[0] if maker_order_ids else ""

    # Total requested shares across all slices — basis for the unfilled remainder.
    total_requested_shares = sum(slice_requested_shares.values()) or _shares_for(
        side, maker_price, size_usd
    )

    # ── Step 2: wait, then POLL EVERY SLICE for its ACTUAL fill (C1) ─────────
    # _wait_for_maker_fill only signals the wait window elapsed (or an early fill
    # was seen). The AUTHORITATIVE fill numbers come from get_order on EACH slice
    # (reading size_matched / original_size) — we never assume the requested size
    # filled, and we sum the ACTUAL matched shares across all ladder slices.
    timeout = float(live_config.maker_wait_secs())
    _wait_for_maker_fill(primary_oid, timeout)

    maker_filled_shares = 0.0
    for oid in maker_order_ids:
        if not oid:
            continue
        # Use retry poll: CLOB may return size_matched=0 briefly after fill.
        status = _poll_until_settled(oid, label="C1-post-wait")
        matched = _matched_shares_of(status)
        if matched > 0:
            maker_filled_shares += matched
        # Mark each slice's open-orders status from its REAL get_order status.
        live_db.update_open_order_status(conn, oid, _status_label(status))

    # Record the ACTUAL maker fill (if any) at the maker price, fee 0.
    if maker_filled_shares > 1e-9:
        maker_usd = maker_filled_shares * maker_price
        live_position_tracker.record_real_fill(
            conn,
            order_id=primary_oid,
            market_id=token_id,
            market_slug=result.get("market_slug", "") or token_id,
            side=side,
            liquidity="maker",
            price=maker_price,
            shares=maker_filled_shares,
            usd=maker_usd,
            fee_paid=0.0,
            fair_price=fair_price,
            token_id=token_id,
            market_title=market_title,
        )
        # Tell the governor the ACTUAL usd deployed, not the full size_usd.
        governor.record_fill(market_id=token_id, usd=maker_usd, liquidity="maker")
        logger.info(
            "execute_intent: MAKER filled ref={} {:.4f} sh ${:.2f}",
            client_order_ref,
            maker_filled_shares,
            maker_usd,
        )

    # Unfilled remainder (shares) is the only candidate for the taker leg.
    remainder_shares = max(0.0, total_requested_shares - maker_filled_shares)

    # Maker fully filled → done, no taker.
    if remainder_shares <= 1e-9:
        result.update(
            action="maker_filled",
            order_id=primary_oid,
            price=maker_price,
            shares=maker_filled_shares,
            usd=maker_filled_shares * maker_price,
            fee_paid=0.0,
            liquidity="maker",
        )
        return result

    # ── Step 3: cancel each STILL-RESTING slice, then re-poll to confirm (C2) ─
    # A slice can fill in the race between the wait deadline and cancel landing.
    # For each resting slice: cancel → re-poll get_order ONCE. If it filled in the
    # window (size_matched grew), record the extra portion as a maker fill and
    # REDUCE the taker remainder — never cross taker for size that already filled.
    # If cancel raises ClobError, still re-poll to learn the true status and act
    # on it (confirmed-filled → maker; confirmed-cancelled → eligible for taker).
    for oid in maker_order_ids:
        if not oid:
            continue
        # Snapshot matched count BEFORE cancel (use retry poll for same reason).
        already_matched = _matched_shares_of(_poll_until_settled(oid, label="C2-pre-cancel"))
        try:
            clob_client.cancel(oid)
        except Exception as exc:
            logger.warning("execute_intent: cancel({}) raised; re-polling to confirm: {}", oid, exc)
        # Re-poll with retry: cancel and fill can race; CLOB may transiently
        # report size_matched=0 even when the fill landed before the cancel.
        status = _poll_until_settled(oid, label="C2-post-cancel")
        post_cancel_matched = _matched_shares_of(status)
        late_fill = post_cancel_matched - already_matched
        if late_fill > 1e-9:
            late_usd = late_fill * maker_price
            live_position_tracker.record_real_fill(
                conn,
                order_id=oid,
                market_id=token_id,
                market_slug=result.get("market_slug", "") or token_id,
                side=side,
                liquidity="maker",
                price=maker_price,
                shares=late_fill,
                usd=late_usd,
                fee_paid=0.0,
                fair_price=fair_price,
                token_id=token_id,
                market_title=market_title,
            )
            governor.record_fill(market_id=token_id, usd=late_usd, liquidity="maker")
            maker_filled_shares += late_fill
            remainder_shares = max(0.0, remainder_shares - late_fill)
            logger.info(
                "execute_intent: cancel-window MAKER fill ref={} oid={} {:.4f} sh",
                client_order_ref,
                oid,
                late_fill,
            )
        live_db.update_open_order_status(conn, oid, _status_label(status))

    # After cancel-confirm, the taker only crosses the genuinely-unfilled size.
    if remainder_shares <= 1e-9:
        result.update(
            action="maker_filled",
            order_id=primary_oid,
            price=maker_price,
            shares=maker_filled_shares,
            usd=maker_filled_shares * maker_price,
            fee_paid=0.0,
            liquidity="maker",
        )
        return result

    # The taker candidate is the unfilled remainder, valued at fair_price.
    remainder_usd = remainder_shares * fair_price

    # ── Taker gating: edge, then depth, then governor ───────────────────────
    min_edge = live_config.min_taker_edge()
    if net_edge_taker < min_edge:
        return _finish_dropped_or_partial(
            result,
            maker_filled_shares,
            maker_price,
            primary_oid,
            reason=f"taker edge {net_edge_taker:.4f} < min_taker_edge {min_edge:.4f}",
            client_order_ref=client_order_ref,
        )

    if not can_execute(token_id, side, remainder_usd):
        return _finish_dropped_or_partial(
            result,
            maker_filled_shares,
            maker_price,
            primary_oid,
            reason="can_execute: book too thin / wide / slippery",
            client_order_ref=client_order_ref,
        )

    decision = governor.check(
        {
            "size_usd": remainder_usd,
            "market_id": token_id,
            "token_id": token_id,
            "category": category,
        }
    )
    if not decision.allowed:
        return _finish_dropped_or_partial(
            result,
            maker_filled_shares,
            maker_price,
            primary_oid,
            reason=f"governor: {decision.reason}",
            client_order_ref=client_order_ref,
        )

    # ── Step 3b: cross taker (FAK) for the REMAINDER only. ───────────────────
    # BUY amount is USD; SELL amount is shares.
    taker_amount = remainder_usd if side.upper() == "BUY" else remainder_shares
    resp = clob_client.cross_taker(
        token_id=token_id,
        side=side,
        amount=taker_amount,
        tick_size=tick_size,
        neg_risk=neg_risk,
    )
    taker_oid = _order_id_of(resp)

    # ── C3: read the ACTUAL matched size + avg fill price from the FAK response.
    # FAK response field names to be confirmed at Phase J canary via the logged
    # raw response below. If NEITHER a matched-size nor an avg-price field is
    # recognised, _parse_taker_fill flags a fallback and we log the WHOLE raw
    # response at WARNING (so the canary confirms the real field names) rather
    # than silently assuming the full size_usd filled at fair_price.
    taker_shares, fill_price, used_fallback = _parse_taker_fill(
        resp, side=side, remainder_shares=remainder_shares, fair_price=fair_price
    )
    if used_fallback:
        logger.warning(
            "execute_intent: TAKER FAK response had no recognised matched-size/avg-price "
            "field — FALLING BACK to remainder@fair. RAW RESPONSE (confirm field names at "
            "Phase J canary): {}",
            resp,
        )

    fee_paid = fee_model.leg_fee(taker_shares, fill_price, category, maker=False)
    taker_usd = taker_shares * fill_price

    live_db.record_open_order(
        conn,
        order_id=taker_oid,
        token_id=token_id,
        side=side,
        price=fill_price,
        size=taker_shares,
        status="filled",
        ts=_utcnow(),
    )
    live_position_tracker.record_real_fill(
        conn,
        order_id=taker_oid,
        market_id=token_id,
        market_slug=result.get("market_slug", "") or token_id,
        side=side,
        liquidity="taker",
        price=fill_price,
        shares=taker_shares,
        usd=taker_usd,
        fee_paid=fee_paid,
        fair_price=fair_price,
        token_id=token_id,
        market_title=market_title,
    )
    governor.record_fill(market_id=token_id, usd=taker_usd, liquidity="taker")

    result.update(
        action="taker_filled",
        order_id=taker_oid,
        price=fill_price,
        shares=taker_shares,
        usd=taker_usd,
        fee_paid=fee_paid,
        liquidity="taker",
        maker_shares=maker_filled_shares,
    )
    logger.info(
        "execute_intent: TAKER filled ref={} {:.4f} sh ${:.2f} fee=${:.4f}",
        client_order_ref,
        taker_shares,
        taker_usd,
        fee_paid,
    )
    return result


def _safe_get_order(order_id: str) -> dict | None:
    """get_order wrapper that never raises — returns None on any error."""
    if not order_id:
        return None
    try:
        return clob_client.get_order(order_id)
    except Exception as exc:
        logger.warning("execute_intent: get_order({}) failed: {}", order_id, exc)
        return None


# Retry constants for fill-confirmation polls.
# The CLOB can report status=MATCHED before size_matched is non-zero —
# we retry briefly to avoid treating a confirmed fill as zero.
_FILL_CONFIRM_RETRIES = 3
_FILL_CONFIRM_SLEEP = 1.5  # seconds between retries


def _poll_until_settled(order_id: str, *, label: str = "") -> dict | None:
    """Poll get_order until size_matched > 0 or a terminal state is reached.

    Addresses the CLOB race where status=MATCHED is returned before
    size_matched is updated.  Retries up to _FILL_CONFIRM_RETRIES times
    with _FILL_CONFIRM_SLEEP between each attempt.

    Terminal early-exit conditions (no further retries):
      - size_matched > 0  (fill confirmed)
      - status in {CANCELED, CANCELLED, EXPIRED}  (no fill possible)

    Falls through and returns the last status dict (or None) when retries
    are exhausted without confirmation.
    """
    status = _safe_get_order(order_id)
    for attempt in range(_FILL_CONFIRM_RETRIES):
        if status is None:
            break
        matched = _matched_shares_of(status)
        label_str = f"[{label}] " if label else ""
        raw = str(status.get("status", "")).strip().upper()
        if matched > 0:
            break  # fill confirmed
        if raw in ("CANCELED", "CANCELLED", "EXPIRED"):
            break  # definitely not filled
        # Status may be MATCHED/LIVE with size_matched still 0 — retry
        logger.debug(
            "{}poll_until_settled: oid={} attempt={}/{} status={} matched=0 — retrying",
            label_str, order_id, attempt + 1, _FILL_CONFIRM_RETRIES, raw,
        )
        time.sleep(_FILL_CONFIRM_SLEEP)
        status = _safe_get_order(order_id)
    return status


def _parse_taker_fill(
    resp: object, *, side: str, remainder_shares: float, fair_price: float
) -> tuple[float, float, bool]:
    """Extract (matched_shares, avg_fill_price, used_fallback) from a FAK response.

    FAK response field names to be confirmed at Phase J canary via the logged
    raw response. We try the documented/probable fields first; if NEITHER a
    matched-size nor an avg-price field is found, we conservatively fall back to
    (remainder_shares, fair_price) and signal used_fallback=True so the caller
    logs the raw response at WARNING. We never record MORE than the remainder we
    asked to cross.
    """
    if not isinstance(resp, dict):
        return remainder_shares, fair_price, True

    fill_price = None
    for key in ("avg_price", "average_price", "match_price", "price", "avgPrice"):
        if resp.get(key) not in (None, ""):
            candidate = _coerce_float(resp.get(key))
            if candidate > 0:
                fill_price = candidate
                break

    matched = None
    for key in (
        "matched_size",
        "size_matched",
        "matchedSize",
        "sizeMatched",
        "filled_size",
        "makerAmountFilled",
        "takerAmountFilled",
    ):
        if resp.get(key) not in (None, ""):
            matched = _coerce_float(resp.get(key))
            break

    if matched is None and fill_price is None:
        # No recognised fields at all — conservative fallback + canary log.
        return remainder_shares, fair_price, True

    if fill_price is None or fill_price <= 0:
        fill_price = fair_price
    if matched is None:
        # Learned a price but not a size — assume the full remainder filled at
        # the reported price (still better than assuming size_usd@fair).
        matched = remainder_shares

    matched = min(matched, remainder_shares)
    return matched, fill_price, False


def _finish_dropped_or_partial(
    result: dict,
    maker_filled_shares: float,
    maker_price: float,
    primary_oid: str,
    *,
    reason: str,
    client_order_ref: str,
) -> dict:
    """Terminal helper when the taker leg is NOT crossed.

    If a maker portion already filled, the trade is a real (partial) maker fill —
    report it as "maker_filled" with the taker-skip reason attached. If nothing
    filled at all, it's a clean "dropped".
    """
    if maker_filled_shares > 1e-9:
        result.update(
            action="maker_filled",
            order_id=primary_oid,
            price=maker_price,
            shares=maker_filled_shares,
            usd=maker_filled_shares * maker_price,
            fee_paid=0.0,
            liquidity="maker",
            taker_skipped_reason=reason,
        )
        logger.info(
            "execute_intent: partial MAKER fill, taker skipped ref={} ({})",
            client_order_ref,
            reason,
        )
    else:
        result.update(action="dropped", reason=reason)
        logger.info("execute_intent: DROPPED ref={} ({})", client_order_ref, reason)
    return result


# ---------------------------------------------------------------------------
# Phase G — Exit wiring: execute_exit
# ---------------------------------------------------------------------------


def execute_exit(
    conn,
    governor,
    *,
    position_row: dict,
    mark_price: float,
    tick_size: float,
    hard_cap_frac: float,
    reason: str,
    category: str = "weather",
) -> dict:
    """Maker-preferred SELL to close a live position.

    Sequence
    --------
    1. Post a post-only maker SELL at mark_price (earns 0 fee).
    2. Wait (_wait_for_maker_fill, mockable) → poll get_order for ACTUAL filled shares.
       Record the filled portion via live_position_tracker.close_position + governor.record_close.
    3. For the unfilled remainder:
       a. Cancel the resting maker.
       b. Re-poll to confirm (avoids double-exposure; same C2 pattern as entry).
       c. If the position's adverse move has hit hard_cap_frac (genuine hard stop):
          cross taker (FAK SELL) for the remainder → record taker close.
       d. Otherwise: hold remainder open to resolution (fee-free). Action = "held_remainder".
          Do NOT force a taker cross for noise-stops.

    Returns
    -------
    dict with keys:
        action      : "maker_closed" | "taker_closed" | "partial_closed" | "held_remainder"
        shares_sold : total shares actually sold (maker + taker)
        exit_price  : weighted average exit price
        fee_paid    : total fee paid (0 for pure-maker close)
        pnl         : combined realised pnl
        reason      : the trigger reason passed in

    Notes (I2/M3)
    -------------
    mark_price is the orderbook MID at the time of exit routing.  A post-only
    SELL at mid may rest above the best bid and not fill on thin books — by
    design.  The unfilled non-hard-stop remainder then holds to resolution
    (action = "held_remainder"), fee-free.  A genuine hard-cap stop crosses
    taker to guarantee the exit regardless of book depth.

    slippage_vs_fair on each SELL fill in live_fills is recorded as
    (exit_price - entry_price) — this is the per-share PnL contribution, NOT
    exit-time fill-quality slippage vs the live mid.  Behaviour is intentional:
    it enables recompute_equity to sum realized PnL directly from fill rows.
    """
    token_id = str(position_row.get("token_id") or "")
    market_id = str(position_row.get("market_id") or "")
    pos_shares = float(position_row.get("shares") or 0.0)
    entry_price = float(position_row.get("entry_price") or 0.0)
    cost_usd = float(position_row.get("cost_usd") or 0.0)
    position_id = int(position_row.get("id") or 0) or None

    if pos_shares <= 0:
        return {
            "action": "held_remainder",
            "shares_sold": 0.0,
            "exit_price": mark_price,
            "fee_paid": 0.0,
            "pnl": 0.0,
            "reason": reason,
        }

    # ── Step 1: post maker SELL at mark_price ────────────────────────────────
    resp = clob_client.post_maker(
        token_id=token_id,
        side="SELL",
        price=mark_price,
        size=pos_shares,
        tick_size=tick_size,
        neg_risk=bool(position_row.get("neg_risk", False)),
    )
    maker_oid = _order_id_of(resp)

    # ── Step 2: wait, then poll for ACTUAL filled shares ─────────────────────
    timeout = float(live_config.maker_wait_secs())
    _wait_for_maker_fill(maker_oid, timeout)

    status_after_wait = _poll_until_settled(maker_oid, label="exit-C1-post-wait")
    maker_filled_shares = _matched_shares_of(status_after_wait)

    maker_pnl = 0.0
    maker_usd_released = 0.0

    if maker_filled_shares > 1e-9:
        # Proportional cost basis released by the maker fill.
        frac = maker_filled_shares / pos_shares
        maker_cost_released = cost_usd * frac

        close_res = live_position_tracker.close_position(
            conn,
            position_id=position_id,
            market_id=market_id if position_id is None else None,
            exit_price=mark_price,
            shares_sold=maker_filled_shares,
            fee_paid=0.0,
            reason=f"maker_exit: {reason}",
            liquidity="maker",
            order_id=maker_oid,
        )
        maker_pnl = close_res["pnl"]
        maker_usd_released = close_res["usd_released"]

        governor.record_close(market_id=market_id, usd=maker_usd_released)

        logger.info(
            "execute_exit: MAKER filled {:.4f} sh @ {:.4f} pnl={:.4f} reason={}",
            maker_filled_shares, mark_price, maker_pnl, reason,
        )

    remainder_shares = max(0.0, pos_shares - maker_filled_shares)

    # Fully filled by maker — done.
    if remainder_shares <= 1e-9:
        return {
            "action": "maker_closed",
            "shares_sold": maker_filled_shares,
            "exit_price": mark_price,
            "fee_paid": 0.0,
            "pnl": maker_pnl,
            "reason": reason,
        }

    # ── Step 3: cancel resting maker, re-poll to confirm (C2) ────────────────
    already_matched_pre_cancel = _matched_shares_of(_poll_until_settled(maker_oid, label="exit-C2-pre-cancel"))
    try:
        clob_client.cancel(maker_oid)
    except Exception as exc:
        logger.warning("execute_exit: cancel({}) raised; re-polling: {}", maker_oid, exc)

    status_post_cancel = _poll_until_settled(maker_oid, label="exit-C2-post-cancel")
    post_cancel_matched = _matched_shares_of(status_post_cancel)
    cancel_window_fill = post_cancel_matched - already_matched_pre_cancel

    total_maker_shares = maker_filled_shares
    total_maker_pnl = maker_pnl
    total_maker_released = maker_usd_released

    if cancel_window_fill > 1e-9:
        # Race fill in the cancel window — record it, reduce remainder.
        frac_cw = cancel_window_fill / pos_shares
        cw_cost_released = cost_usd * frac_cw
        cw_close = live_position_tracker.close_position(
            conn,
            position_id=position_id,
            market_id=market_id if position_id is None else None,
            exit_price=mark_price,
            shares_sold=cancel_window_fill,
            fee_paid=0.0,
            reason=f"maker_cancel_window: {reason}",
            liquidity="maker",
            order_id=maker_oid,
        )
        total_maker_shares += cancel_window_fill
        total_maker_pnl += cw_close["pnl"]
        total_maker_released += cw_cost_released
        governor.record_close(market_id=market_id, usd=cw_cost_released)
        remainder_shares = max(0.0, remainder_shares - cancel_window_fill)

    if remainder_shares <= 1e-9:
        return {
            "action": "maker_closed",
            "shares_sold": total_maker_shares,
            "exit_price": mark_price,
            "fee_paid": 0.0,
            "pnl": total_maker_pnl,
            "reason": reason,
        }

    # ── Step 3c: hard-cap gate ────────────────────────────────────────────────
    # Only cross taker for a GENUINE hard stop — when the adverse move has already
    # consumed hard_cap_frac of the cost basis.  Noise-stops are held to resolution.
    current_loss_frac = 0.0
    if cost_usd > 0:
        adverse_move = entry_price - mark_price  # positive = adverse for a BUY position
        loss_usd = adverse_move * pos_shares if adverse_move > 0 else 0.0
        current_loss_frac = loss_usd / cost_usd

    is_hard_stop = current_loss_frac >= hard_cap_frac

    if not is_hard_stop:
        # Hold remainder to resolution — fee-free.
        logger.info(
            "execute_exit: held_remainder {:.4f} sh (loss_frac={:.2%} < hard_cap={:.2%}) reason={}",
            remainder_shares, current_loss_frac, hard_cap_frac, reason,
        )
        return {
            "action": "held_remainder",
            "shares_sold": total_maker_shares,
            "exit_price": mark_price,
            "fee_paid": 0.0,
            "pnl": total_maker_pnl,
            "reason": reason,
        }

    # ── Step 3d: cross taker (FAK SELL) for the remainder ───────────────────
    taker_resp = clob_client.cross_taker(
        token_id=token_id,
        side="SELL",
        amount=remainder_shares,   # SELL amount = shares
        tick_size=tick_size,
        neg_risk=bool(position_row.get("neg_risk", False)),
    )
    taker_shares, taker_price, _fallback = _parse_taker_fill(
        taker_resp, side="SELL", remainder_shares=remainder_shares, fair_price=mark_price
    )
    taker_fee = fee_model.leg_fee(taker_shares, taker_price, category, maker=False)
    taker_oid = _order_id_of(taker_resp)

    taker_close = live_position_tracker.close_position(
        conn,
        position_id=position_id,
        market_id=market_id if position_id is None else None,
        exit_price=taker_price,
        shares_sold=taker_shares,
        fee_paid=taker_fee,
        reason=f"taker_hard_cap: {reason}",
        liquidity="taker",
        order_id=taker_oid,
    )
    taker_pnl = taker_close["pnl"]
    taker_released = taker_close["usd_released"]
    governor.record_close(market_id=market_id, usd=taker_released)

    total_shares = total_maker_shares + taker_shares
    total_fee = taker_fee
    total_pnl = total_maker_pnl + taker_pnl
    # Weighted avg exit price
    if total_shares > 0:
        avg_price = (
            total_maker_shares * mark_price + taker_shares * taker_price
        ) / total_shares
    else:
        avg_price = taker_price

    action = "taker_closed" if total_maker_shares <= 1e-9 else "partial_closed"

    logger.info(
        "execute_exit: TAKER hard-cap {:.4f} sh @ {:.4f} fee={:.4f} reason={}",
        taker_shares, taker_price, taker_fee, reason,
    )

    return {
        "action": action,
        "shares_sold": total_shares,
        "exit_price": avg_price,
        "fee_paid": total_fee,
        "pnl": total_pnl,
        "reason": reason,
    }

"""
Thin wrapper around the Polymarket polymarket-client SDK (polymarket-client>=0.1.0b9).

This is the ONLY module in the Polyclawd codebase that imports polymarket-client.
All other execution modules go through this wrapper so that:
  (a) tests can inject a fake client without touching the real network, and
  (b) SDK API changes are contained to one file.

Decision on record (Phase C, updated Phase F+): live execution goes through the
polymarket-client SecureClient (sync) with RelayerApiKey auth against the
Deposit Wallet (POLY_PROXY / deposit-wallet flow). This replaced py-clob-client
(0.34.6) because py-clob-client could not see the pUSD balance in the deposit
wallet system introduced by Polymarket in 2026.

--- ACCOUNT SETUP HISTORY ---

The Polymarket account uses a "deposit wallet" flow (wallet_type=DEPOSIT_WALLET):
  EOA signing key:   0xa22D31A495d70185C6DeaEaDE31C7C126f3c20f8
    (derived from BOT_EOA_PRIVATE_KEY)
  Deposit wallet:    0xa495c42d60521eE28e1dA237C0baB560D5095777
    (POLYMARKET_DEPOSIT_WALLET — discovered by SecureClient.create() on 2026-06-25)
  Relayer API key:   POLYMARKET_RELAYER_API_KEY (single key, no secret)
  Relayer address:   0xa22D31A495d70185C6DeaEaDE31C7C126f3c20f8 (same as EOA)

Trading approvals were set on 2026-06-25 via approve_erc20 for pUSD to:
  standard_exchange (0xE111...): MAX allowance
  neg_risk_exchange (0xe222...): MAX allowance
  neg_risk_adapter  (0xd91E...): MAX allowance

--- REAL VENDOR API (polymarket-client==0.1.0b9) ---

Import:    from polymarket import SecureClient, RelayerApiKey, AcceptedOrder, RejectedOrder

SecureClient.create(
    private_key: str,           # EOA private key for signing
    wallet: str,                # Deposit wallet address (not EOA!)
    api_key: RelayerApiKey(...) # RelayerApiKey(key=..., address=EOA_address)
)

--- Order placement ---

Maker (post-only GTC):
    result = client.place_limit_order(
        token_id=token_id,
        price=price,        # float in (0, 1)
        size=size,          # shares (conditional tokens)
        side="BUY"|"SELL",
        post_only=True,     # fee-avoidance mechanism: reject if would cross
    )
    Returns AcceptedOrder (ok=True, order_id, status, making_amount, taking_amount)
         or RejectedOrder (ok=False, code, message)
    code "post_only_would_cross" → ClobError (order not placed, not filled)

Taker (FAK):
    result = client.place_market_order(
        token_id=token_id,
        side="BUY"|"SELL",
        amount=usd_amount,  # BUY: USD to spend
        shares=num_shares,  # SELL: shares to sell (use 'shares' kwarg, not 'amount')
        order_type="FAK",   # fill-and-kill
    )
    Same return type as limit. "fak_not_filled" is a normal outcome (not an error).

Cancel:
    result = client.cancel_order(order_id=order_id)
    Returns CancelOrdersResponse(canceled=[...], not_canceled={...})

Order status:
    result = client.get_order(order_id=order_id)
    Returns OpenOrder with fields: id, status, original_size, size_matched, ...
    status ∈ {"LIVE", "MATCHED", "DELAYED", "CANCELED", ...} (uppercase in this wrapper)

Balance:
    bal = client.get_balance_allowance(asset_type="COLLATERAL")
    bal.balance is an integer in base units (pUSD has 6 decimals → divide by 1e6)

--- Tick size ---

get_tick_size() queries the CLOB REST API directly
(GET https://clob.polymarket.com/tick-size?token_id=...) — no auth required.
The SDK handles tick size internally when placing orders, but we expose
get_tick_size() for callers that need it before building execute_intent parameters.

--- Side constants ---

Side strings are plain "BUY" / "SELL".
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import requests as _requests

from execution import live_config

if TYPE_CHECKING:
    from polymarket import SecureClient as _SecureClientType


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class ClobError(Exception):
    """Raised when a CLOB call returns a business rejection or unexpected error.

    Distinct from HTTP-level errors (which the SDK raises as RequestRejectedError).
    Plain Exception subclass so callers can catch it cleanly."""


# ---------------------------------------------------------------------------
# Vendor client — injectable for tests, lazy-initialised for production
# ---------------------------------------------------------------------------

_client: "_SecureClientType | None" = None


def set_client(c: "_SecureClientType | None") -> None:
    """Replace the module-level vendor client.  Pass None to reset.
    Used by tests to inject a fake without hitting the network."""
    global _client
    _client = c


def _get_client() -> "_SecureClientType":
    """Return the vendor client, creating it lazily on first real call.

    Lazy init means PAPER-mode code can import this module without credentials.
    """
    global _client
    if _client is not None:
        return _client

    from polymarket import SecureClient, RelayerApiKey

    pk = live_config.bot_eoa_private_key()
    deposit_wallet = os.environ.get("POLYMARKET_DEPOSIT_WALLET") or None
    relayer_key = os.environ.get("POLYMARKET_RELAYER_API_KEY") or None
    relayer_address = os.environ.get("POLYMARKET_RELAYER_ADDRESS") or None

    if not pk:
        raise ClobError("BOT_EOA_PRIVATE_KEY not set — cannot initialise CLOB client")

    kwargs: dict = {"private_key": pk}
    if deposit_wallet:
        kwargs["wallet"] = deposit_wallet
    # If deposit_wallet is not set, SDK discovers it from the EOA automatically.

    if relayer_key and relayer_address:
        kwargs["api_key"] = RelayerApiKey(key=relayer_key, address=relayer_address)

    _client = SecureClient.create(**kwargs)
    return _client


# ---------------------------------------------------------------------------
# Response conversion helpers
# ---------------------------------------------------------------------------


def _accepted_to_dict(order) -> dict:
    """Convert AcceptedOrder to a dict with keys the executor expects.

    order_id is exposed as both "orderID" (py-clob-client compat) and "id".
    status is uppercased for consistency with order_is_filled() checks.
    """
    from decimal import Decimal

    def _to_float(v) -> float:
        if v is None:
            return 0.0
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    return {
        "orderID": str(order.order_id),
        "id": str(order.order_id),
        "status": str(order.status).upper(),
        "making_amount": _to_float(order.making_amount),
        "taking_amount": _to_float(order.taking_amount),
    }


def _open_order_to_dict(order) -> dict:
    """Convert OpenOrder to a dict matching the fields order_is_filled() reads.

    Both snake_case and camelCase keys are populated to be robust against
    executor field-name lookups.
    """
    def _to_str(v) -> str:
        if v is None:
            return "0"
        return str(v)

    status_raw = str(order.status or "").strip().upper()

    return {
        "id": str(order.id),
        "orderID": str(order.id),
        "status": status_raw,
        "original_size": _to_str(order.original_size),
        "originalSize": _to_str(order.original_size),
        "size_matched": _to_str(order.size_matched),
        "sizeMatched": _to_str(order.size_matched),
        "price": _to_str(order.price),
    }


# ---------------------------------------------------------------------------
# Public API — the only surface the rest of Polyclawd touches
# ---------------------------------------------------------------------------


def get_tick_size(token_id: str) -> float:
    """Return the minimum tick size for token_id as a float.

    Queries the CLOB REST API (no auth required). Returns one of:
    0.1, 0.01, 0.001, 0.0001.
    """
    resp = _requests.get(
        "https://clob.polymarket.com/tick-size",
        params={"token_id": token_id},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    raw = data.get("minimum_tick_size") or data.get("tick_size") or "0.01"
    return float(raw)


def post_maker(
    token_id: str,
    side: str,
    price: float,
    size: float,
    tick_size: float,  # accepted but handled internally by SDK
    neg_risk: bool = False,  # accepted but handled internally by SDK
) -> dict:
    """Place a post-only GTC limit (maker) order.

    tick_size and neg_risk are accepted for API compatibility with existing
    callers but are NOT forwarded to the SDK — the SDK determines them from
    on-chain market data automatically.

    Args:
        token_id:  CLOB token ID for the outcome leg being traded.
        side:      "BUY" or "SELL".
        price:     Limit price in USDC (0 < price < 1).
        size:      Number of shares (conditional tokens).
        tick_size: Market minimum tick — accepted but unused (SDK handles internally).
        neg_risk:  Neg-risk flag — accepted but unused (SDK handles internally).

    Returns:
        Dict with keys "orderID", "id", "status" from AcceptedOrder.

    Raises:
        ClobError: If the order is rejected by the exchange (including
                   post_only_would_cross — fee avoidance mechanism).
    """
    from polymarket import RejectedOrder

    c = _get_client()
    result = c.place_limit_order(
        token_id=token_id,
        price=price,
        size=size,
        side=side,
        post_only=True,
    )
    if isinstance(result, RejectedOrder):
        raise ClobError(
            f"post_maker rejected ({result.code}): {result.message}"
        )
    return _accepted_to_dict(result)


def cross_taker(
    token_id: str,
    side: str,
    amount: float,
    tick_size: float,  # accepted but handled internally by SDK
    neg_risk: bool = False,  # accepted but handled internally by SDK
) -> dict:
    """Place a marketable FAK (fill-and-kill) taker order.

    tick_size and neg_risk are accepted for API compatibility but NOT forwarded
    to the SDK — handled internally.

    'amount' semantics (matching py-clob-client MarketOrderArgs):
      BUY:  amount = USD to spend
      SELL: amount = shares to sell

    Returns:
        Dict containing "orderID", "status", and fill-size fields:
          size_matched / sizeMatched  = shares actually matched
          making_amount               = raw making-amount from exchange
          taking_amount               = raw taking-amount from exchange
        If the FAK did not fill (fak_not_filled), returns with status "UNMATCHED"
        and size_matched="0" — this is NOT an error; the executor handles it.

    Raises:
        ClobError: For hard rejections (invalid order, insufficient balance, etc.).
    """
    from polymarket import RejectedOrder

    c = _get_client()
    if side.upper() == "BUY":
        result = c.place_market_order(
            token_id=token_id,
            side="BUY",
            amount=amount,
            order_type="FAK",
        )
    else:
        result = c.place_market_order(
            token_id=token_id,
            side="SELL",
            shares=amount,
            order_type="FAK",
        )

    if isinstance(result, RejectedOrder):
        if result.code in ("fak_not_filled", "unmatched"):
            # Normal FAK outcome (no liquidity to fill). Not an error.
            return {
                "orderID": "",
                "id": "",
                "status": "UNMATCHED",
                "size_matched": "0",
                "sizeMatched": "0",
                "making_amount": 0.0,
                "taking_amount": 0.0,
            }
        raise ClobError(
            f"cross_taker rejected ({result.code}): {result.message}"
        )

    d = _accepted_to_dict(result)
    # Expose size_matched for _parse_taker_fill() in live_executor.
    #
    # SDK semantics (confirmed from polymarket SDK source place.py):
    #   BUY order  → maker_amount = USDC offered (collateral approved) → making_amount = USDC paid
    #                taker_amount = conditional tokens requested       → taking_amount = shares received
    #   SELL order → maker_amount = conditional tokens offered         → making_amount = shares sold
    #                taker_amount = USDC requested                     → taking_amount = USDC received
    #
    # Both cases: matched = shares, avg_price = USDC/shares (base-unit ratio cancels: both 6-decimal).
    import logging as _logging
    _logging.getLogger(__name__).info(
        "cross_taker raw amounts: side=%s making_amount=%s taking_amount=%s",
        side, d["making_amount"], d["taking_amount"],
    )
    if side.upper() == "BUY":
        matched = d["taking_amount"]   # shares received
        avg_price = (d["making_amount"] / matched) if matched > 1e-9 else 0.0  # USDC/shares
    else:
        matched = d["making_amount"]   # shares sold
        avg_price = (d["taking_amount"] / matched) if matched > 1e-9 else 0.0  # USDC/shares

    d["size_matched"] = str(matched)
    d["sizeMatched"] = str(matched)
    if avg_price > 0:
        d["avg_price"] = avg_price

    return d


def cancel(order_id: str) -> dict:
    """Cancel a resting order by its CLOB order ID.

    Returns:
        Dict with "cancelled" bool and "order_id".
    Raises:
        ClobError: If the SDK raises unexpectedly (NOT raised for already-cancelled orders).
    """
    result = _get_client().cancel_order(order_id=order_id)
    success = order_id in (result.canceled or [])
    return {"cancelled": success, "order_id": order_id}


def get_order(order_id: str) -> dict:
    """Fetch the current state of a resting order by its CLOB order ID.

    Returns a dict with at minimum:
        id / orderID    order identifier (same value, both keys present)
        status          "LIVE" | "MATCHED" | "CANCELED" | "DELAYED" | ...  (uppercase)
        original_size   requested size (string)
        size_matched    filled size so far (string)
        price           limit price (string)

    Raises:
        ClobError: If the SDK raises RequestRejectedError or any unexpected error.
    """
    try:
        result = _get_client().get_order(order_id=order_id)
    except Exception as exc:
        raise ClobError(f"get_order({order_id}) failed: {exc}") from exc
    return _open_order_to_dict(result)


def order_is_filled(order_status: dict) -> bool:
    """Return True iff *order_status* (from get_order) is fully filled.

    Detection strategy (same as before):
      1. If status == "MATCHED" | "FILLED" → True.
      2. If status is another known terminal/active state → False.
      3. Fall back to size_matched >= original_size comparison.
    """
    if not isinstance(order_status, dict):
        return False

    status = str(order_status.get("status", "")).strip().upper()
    if status in ("MATCHED", "FILLED"):
        return True
    if status in ("LIVE", "OPEN", "DELAYED", "CANCELED", "CANCELLED", "EXPIRED", "UNMATCHED"):
        return False

    def _f(key: str) -> float:
        val = order_status.get(key)
        if val is None:
            return 0.0
        try:
            return float(val)
        except (TypeError, ValueError):
            return 0.0

    original = _f("original_size") or _f("originalSize") or _f("size")
    matched = _f("size_matched") or _f("sizeMatched") or _f("matched_size")
    return original > 0 and matched >= original - 1e-9


def get_balance() -> float:
    """Return the pUSD collateral balance available for trading.

    Queries get_balance_allowance(asset_type='COLLATERAL'). The balance field
    is an integer in base units (6 decimals for pUSD).

    Returns:
        Available balance in USD-equivalent float.
    """
    bal = _get_client().get_balance_allowance(asset_type="COLLATERAL")
    return float(bal.balance) / 1_000_000

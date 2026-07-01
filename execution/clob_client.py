"""
Thin wrapper around the Polymarket py-clob-client vendor library.

This is the ONLY module in the Polyclawd codebase that imports
py_clob_client.  All other execution modules go through this wrapper so
that (a) tests can inject a fake vendor client without touching the real
network, and (b) vendor API changes are contained to one file.

Decision on record (Phase C): live weather goes DIRECT CLOB self-custody
(NOT the Simmer taker-only path) to enable maker-first / 5%-fee avoidance.

--- REAL VENDOR API (py-clob-client==0.34.6, introspected 2026-06-02) ---

Package:   py-clob-client==0.34.6
Import:    from py_clob_client.client import ClobClient
           from py_clob_client.clob_types import (
               ApiCreds, OrderArgs, MarketOrderArgs,
               PartialCreateOrderOptions, OrderType,
               BalanceAllowanceParams, AssetType,
           )

ClobClient.__init__(
    host,
    chain_id: int = None,
    key: str = None,           # EOA private key
    creds: ApiCreds = None,
    signature_type: int = None,
    funder: str = None,
    builder_config = None,
    tick_size_ttl: float = 300.0,
)

ClobClient.set_api_creds(creds: ApiCreds)   -- set CLOB REST creds post-init
ApiCreds(api_key, api_secret, api_passphrase)

--- Order creation (DEVIATIONS FROM PLAN) ---

Plan assumed:  create_and_post_order(OrderArgs, options) → everything in one.
Real maker path we use:
    order = client.create_order(OrderArgs, PartialCreateOrderOptions)
    result = client.post_order(order, orderType=OrderType.GTC, post_only=True)
  (create_and_post_order exists but calls post_order with default GTC, no
  post_only flag — kept separate so we can explicitly pass post_only=True.)

  CRITICAL: post_only=True is the fee-avoidance mechanism.  The vendor
  accepts post_only as a direct kwarg to post_order() — confirmed by:
    inspect.signature(ClobClient.post_order)
    → (self, order, orderType: OrderType = 'GTC', post_only: bool = False)
  Internally it sets "postOnly": true in the JSON body via order_to_json().
  If post_only=False (the default) and the order crosses the book, it fills
  as a TAKER at 5% fee.  post_only=True causes the exchange to REJECT the
  order rather than let it cross — so we never accidentally pay taker fees.

Real taker (FAK) path:
    order = client.create_market_order(MarketOrderArgs, PartialCreateOrderOptions)
    result = client.post_order(order, orderType=OrderType.FAK)
  FAK orders are intentional taker fills; post_only is left False (default).

OrderArgs(token_id, price, size, side, fee_rate_bps=0, nonce=0,
          expiration=0, taker=ZERO_ADDRESS)

MarketOrderArgs(token_id, amount, side, price=0, fee_rate_bps=0, nonce=0,
                taker=ZERO_ADDRESS, order_type=OrderType.FOK)

PartialCreateOrderOptions(tick_size: str|None, neg_risk: bool|None)
    tick_size must be passed as a string Literal matching one of:
    "0.1", "0.01", "0.001", "0.0001"

OrderType: GTC | FAK | FOK | GTD   (string enum)

--- Balance ---

client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
Returns dict with keys "balance" and "allowance" (both string USD amounts).

--- Side constants ---

Side strings are plain "BUY" / "SELL" — not imported constants.
(py_clob_client.order_builder.constants has BUY/SELL but they equal those strings.)

--- get_tick_size ---

Returns a str (Literal "0.1" | "0.01" | "0.001" | "0.0001").
Our wrapper converts to float for callers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from execution import live_config
from py_clob_client.clob_types import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    MarketOrderArgs,
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
)

if TYPE_CHECKING:
    from py_clob_client.client import ClobClient as _ClobClientType


# ---------------------------------------------------------------------------
# Response validation
# ---------------------------------------------------------------------------

# Vendor source (py_clob_client/http_helpers/helpers.py) raises PolyApiException
# on non-200 HTTP status codes, so HTTP errors are already covered.  However,
# the CLOB REST API can return HTTP 200 with a business-rejection body such as
#   {"error": "...", "errorCode": ...}  or  {"success": false, ...}
# We detect these and raise ClobError so callers get a clear, actionable message
# rather than silently treating a rejected order as a success.


class ClobError(Exception):
    """Raised when a CLOB call returns HTTP 200 but signals a business rejection
    (e.g. a post-only order that would cross, or insufficient balance).

    Distinct from the vendor's PolyApiException (raised on non-200 HTTP). Plain
    Exception subclass so it can carry a simple string message — PolyApiException
    expects a response object in its constructor and cannot."""


def _check_response(result: object, method: str) -> None:
    """Raise ClobError if *result* looks like a business-rejection response.

    The vendor raises PolyApiException for non-200 HTTP status.  This guard
    catches HTTP-200 rejections whose bodies contain error-ish keys but no
    order ID — e.g. {"error": "post_only order would cross", "errorCode": 4}.

    Checked keys (vendor-sourced): "error", "errorCode", "errorMsg", "success".
    A result with none of these keys (normal success dict) passes through.
    """
    if not isinstance(result, dict):
        return
    # Success path: dict has an order-id-like key — vendor always returns
    # "orderID" or "id" on true success.  Skip further checks.
    if result.get("orderID") or result.get("id"):
        return
    # Explicit failure signals.
    if result.get("success") is False:
        raise ClobError(f"{method} rejected by CLOB API: {result}")
    for key in ("error", "errorCode", "errorMsg"):
        if result.get(key):
            raise ClobError(f"{method} rejected by CLOB API: {result}")


# ---------------------------------------------------------------------------
# Vendor client — injectable for tests, lazy-initialised for production
# ---------------------------------------------------------------------------

_client: "_ClobClientType | None" = None


def set_client(c: "_ClobClientType | None") -> None:
    """Replace the module-level vendor client.  Pass None to reset.
    Used by tests to inject a fake without hitting the network."""
    global _client
    _client = c


def _get_client() -> "_ClobClientType":
    """Return the vendor client, creating it lazily on first real call.

    Lazy init means PAPER-mode code can import this module without any
    credentials configured in the environment.
    """
    global _client
    if _client is not None:
        return _client

    from py_clob_client.client import ClobClient

    host = "https://clob.polymarket.com"
    chain_id = 137  # Polygon mainnet

    key = live_config.bot_eoa_private_key()
    sig_type = live_config.signature_type()

    c = ClobClient(host=host, chain_id=chain_id, key=key, signature_type=sig_type)

    api_key = live_config.clob_api_key()
    api_secret = live_config.clob_api_secret()
    api_passphrase = live_config.clob_api_passphrase()
    if api_key and api_secret and api_passphrase:
        c.set_api_creds(
            ApiCreds(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=api_passphrase,
            )
        )

    _client = c
    return _client


# ---------------------------------------------------------------------------
# Public API — the only surface the rest of Polyclawd touches
# ---------------------------------------------------------------------------


def get_tick_size(token_id: str) -> float:
    """Return the minimum tick size for token_id as a float.

    The vendor returns a string Literal ("0.1", "0.01", "0.001", "0.0001");
    we convert to float for arithmetic convenience.
    """
    raw: str = _get_client().get_tick_size(token_id)
    return float(raw)


def post_maker(
    token_id: str,
    side: str,
    price: float,
    size: float,
    tick_size: float,
    neg_risk: bool = False,
) -> dict:
    """Place a post-only GTC limit (maker) order.

    Uses create_order + post_order(GTC) so we control the OrderType.
    'size' is in terms of the conditional token shares.

    Args:
        token_id:  CLOB token ID for the outcome leg being traded.
        side:      "BUY" or "SELL".
        price:     Limit price in USDC (0 < price < 1).
        size:      Number of shares (conditional tokens).
        tick_size: Market minimum tick (float, e.g. 0.01).
        neg_risk:  Set True for negatively-correlated outcome tokens.

    Returns:
        Vendor response dict (includes "orderID", "status", etc.).
    """
    c = _get_client()
    order_args = OrderArgs(
        token_id=token_id,
        side=side,
        price=price,
        size=size,
    )
    options = PartialCreateOrderOptions(
        tick_size=str(tick_size),
        neg_risk=neg_risk,
    )
    signed_order = c.create_order(order_args, options)
    # post_only=True is the fee-avoidance mechanism: if this limit would cross
    # the book the exchange REJECTS it rather than filling it as a taker (5%
    # weather fee). Without this flag a crossing GTC fills as taker silently.
    result = c.post_order(signed_order, orderType=OrderType.GTC, post_only=True)
    _check_response(result, "post_maker")
    return result


def cross_taker(
    token_id: str,
    side: str,
    amount: float,
    tick_size: float,
    neg_risk: bool = False,
) -> dict:
    """Place a marketable FAK (fill-and-kill) taker order.

    Uses create_market_order + post_order(FAK).
    'amount' is USD for BUY orders, shares for SELL orders
    (matching MarketOrderArgs.amount semantics in the vendor library).

    Args:
        token_id:  CLOB token ID for the outcome leg.
        side:      "BUY" or "SELL".
        amount:    USD to spend (BUY) or shares to sell (SELL).
        tick_size: Market minimum tick (float, e.g. 0.01).
        neg_risk:  Set True for negatively-correlated outcome tokens.

    Returns:
        Vendor response dict.
    """
    c = _get_client()
    order_args = MarketOrderArgs(
        token_id=token_id,
        side=side,
        amount=amount,
    )
    options = PartialCreateOrderOptions(
        tick_size=str(tick_size),
        neg_risk=neg_risk,
    )
    signed_order = c.create_market_order(order_args, options)
    # Taker fill: post_only stays False (FAK is intentionally marketable).
    result = c.post_order(signed_order, orderType=OrderType.FAK)
    _check_response(result, "cross_taker")
    return result


def cancel(order_id: str) -> dict:
    """Cancel a resting order by its CLOB order ID.

    Returns:
        Vendor response dict.
    """
    result = _get_client().cancel(order_id)
    _check_response(result, "cancel")
    return result


def get_order(order_id: str) -> dict:
    """Fetch the current state of a resting order by its CLOB order ID.

    Delegates to the vendor's ``ClobClient.get_order(order_id)`` (Level-2 auth),
    which returns the order object as a dict. Typical vendor fields:
        id / order_id     order identifier
        status            "LIVE" | "MATCHED" | "CANCELED" | ... (vendor casing)
        original_size     requested size (string)
        size_matched      filled size so far (string)
        price             limit price (string)

    The raw vendor dict is returned unchanged so callers (and
    ``order_is_filled``) can inspect whatever fields are present. Wrapped in
    ``_check_response`` so an HTTP-200 business-rejection body raises ClobError
    rather than being mistaken for a live order.
    """
    result = _get_client().get_order(order_id)
    _check_response(result, "get_order")
    return result


def order_is_filled(order_status: dict) -> bool:
    """Return True iff *order_status* (a vendor get_order dict) is fully filled.

    Detection strategy, in order:
      1. If a ``status`` field is present, treat the vendor's terminal-filled
         states ("MATCHED" / "FILLED") as filled and explicit non-filled
         terminal/active states ("LIVE" / "OPEN" / "CANCELED" / "CANCELLED")
         as not-filled — case-insensitively.
      2. Otherwise (or for ambiguous status), compare matched vs original size:
         filled iff size_matched >= original_size and original_size > 0.

    Be conservative: anything we can't positively confirm as filled returns
    False, so the executor never records a phantom fill.
    """
    if not isinstance(order_status, dict):
        return False

    status = str(order_status.get("status", "")).strip().upper()
    if status in ("MATCHED", "FILLED"):
        return True
    if status in ("LIVE", "OPEN", "DELAYED", "CANCELED", "CANCELLED", "EXPIRED", "UNMATCHED"):
        return False

    # Fall back to size comparison (works regardless of status casing / absence).
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
    """Return the USDC collateral balance available for trading.

    Calls get_balance_allowance with AssetType.COLLATERAL and extracts
    the "balance" field (vendor returns it as a numeric string).

    Returns:
        Available USDC balance as a float.
    """
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    result = _get_client().get_balance_allowance(params)
    return float(result["balance"])

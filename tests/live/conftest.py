"""Harness for the live-money code (plan task 1.0c).

* ``live_db``       a temporary SQLite database built by ``execution.live_db.connect``
                    (production DDL), with POLYCLAWD_LIVE_DB pointing at it.
* ``fake_clob``     a stand-in for the SDK client, injected through
                    ``execution.clob_client.set_client`` and removed on teardown.
* ``shapes``        builders for every exchange answer, each made from the SDK's own
                    response classes (never a dict, never a Mock).
* ``fixed_tick_and_min_size``  patches the two wrapper functions that query REST.

Nothing here needs credentials, network or the production database; an autouse
fixture makes any real HTTP or socket connect fail the test.
"""

from __future__ import annotations

import inspect
import socket
import urllib.request
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import sqlite3

import pytest
import requests

from polymarket import (
    AcceptedOrder,
    BalanceAllowance,
    CancelOrdersResponse,
    OpenOrder,
    Page,
    Paginator,
    RejectedOrder,
    SecureClient,
)

from execution import clob_client, live_config, live_db as live_db_module

# Exchange messages that the SDK maps to a code (polymarket/models/clob/order_response.py).
_MSG_POST_ONLY_CROSS = "invalid post-only order: order crosses book"
_MSG_FAK_NOT_FILLED = (
    "no orders found to match with FAK order. "
    "FAK orders are partially filled or killed if no match is found."
)

TOKEN_ID = "71321045679252212594626385532706912750332728571942532289631379312455583992563"
CONDITION_ID = "0x" + "ab" * 32


# ---------------------------------------------------------------------------
# Shapes: every answer is an SDK class
# ---------------------------------------------------------------------------


class Shapes:
    """Exchange answers built from SDK classes. Field values are plausible, not
    recorded (see fixtures/README.md); the classes are the SDK's own, so a field
    the SDK does not have cannot be scripted."""

    TOKEN_ID = TOKEN_ID
    CONDITION_ID = CONDITION_ID

    # -- order placement (place_limit_order / place_market_order) ------------

    @staticmethod
    def accepted_post(order_id: str = "0xorder-live-1", status: str = "live",
                      making: str = "0", taking: str = "0") -> AcceptedOrder:
        return AcceptedOrder(
            order_id=order_id,
            status=status,
            making_amount=Decimal(making),
            taking_amount=Decimal(taking),
            trade_ids=(),
            transactions_hashes=(),
        )

    @staticmethod
    def rejected_would_cross() -> RejectedOrder:
        return RejectedOrder(code="post_only_would_cross", message=_MSG_POST_ONLY_CROSS)

    @staticmethod
    def rejected_bad_tick() -> RejectedOrder:
        # The SDK has no tick-size code, so the exchange text maps to "unknown".
        # The message text is a guess (not recorded): see fixtures/README.md.
        return RejectedOrder(
            code="unknown",
            message="invalid price: breaks minimum tick size rules",
        )

    @staticmethod
    def rejected_not_enough_balance() -> RejectedOrder:
        return RejectedOrder(
            code="not_enough_balance",
            message="not enough balance / allowance: the balance is not enough",
        )

    @staticmethod
    def fak_filled_buy(order_id: str = "0xorder-fak-1", usd: str = "4.2",
                       shares: str = "10") -> AcceptedOrder:
        """BUY: making_amount = USD paid, taking_amount = shares received."""
        return AcceptedOrder(
            order_id=order_id,
            status="matched",
            making_amount=Decimal(usd),
            taking_amount=Decimal(shares),
            trade_ids=("trade-1",),
            transactions_hashes=(),
        )

    @staticmethod
    def fak_filled_sell(order_id: str = "0xorder-fak-2", shares: str = "10",
                        usd: str = "4.0") -> AcceptedOrder:
        """SELL: making_amount = shares sold, taking_amount = USD received."""
        return AcceptedOrder(
            order_id=order_id,
            status="matched",
            making_amount=Decimal(shares),
            taking_amount=Decimal(usd),
            trade_ids=("trade-2",),
            transactions_hashes=(),
        )

    @staticmethod
    def fak_unfilled() -> RejectedOrder:
        return RejectedOrder(code="fak_not_filled", message=_MSG_FAK_NOT_FILLED)

    # -- cancel_order ---------------------------------------------------------

    @staticmethod
    def cancel_confirmed(order_id: str = "0xorder-live-1") -> CancelOrdersResponse:
        return CancelOrdersResponse(canceled=(order_id,), not_canceled={})

    @staticmethod
    def cancel_not_canceled(order_id: str = "0xorder-live-1",
                            reason: str = "order can't be found - already canceled or matched"
                            ) -> CancelOrdersResponse:
        return CancelOrdersResponse(canceled=(), not_canceled={order_id: reason})

    # -- get_order / list_open_orders (OpenOrder) -----------------------------

    @staticmethod
    def open_order(order_id: str = "0xorder-live-1", status: str = "LIVE",
                   side: str = "BUY", price: str = "0.42", original_size: str = "10",
                   size_matched: str = "0") -> OpenOrder:
        return OpenOrder(
            id=order_id,
            market=CONDITION_ID,
            asset_id=TOKEN_ID,
            owner="00000000-0000-0000-0000-000000000000",
            maker_address="0x" + "22" * 20,
            side=side,
            price=Decimal(price),
            original_size=Decimal(original_size),
            size_matched=Decimal(size_matched),
            outcome="Yes",
            order_type="GTC",
            status=status,
            created_at=1_762_800_000,
        )

    @classmethod
    def order_live(cls, order_id: str = "0xorder-live-1") -> OpenOrder:
        return cls.open_order(order_id, "LIVE", size_matched="0")

    @classmethod
    def order_matched(cls, order_id: str = "0xorder-live-1") -> OpenOrder:
        return cls.open_order(order_id, "MATCHED", size_matched="10")

    @classmethod
    def order_cancelled(cls, order_id: str = "0xorder-live-1") -> OpenOrder:
        # The wrapper's docstring spells the status "CANCELED"; whether the
        # exchange spells it that way for get_order is NOT verified.
        return cls.open_order(order_id, "CANCELED", size_matched="0")

    @staticmethod
    def empty_page() -> Page:
        return Page(items=(), has_more=False)

    @classmethod
    def open_orders_pages(cls) -> tuple:
        """Two pages: orders A and B, then C. Cursors chain page 1 -> page 2."""
        p1 = Page(
            items=(cls.open_order("0xorder-A"), cls.open_order("0xorder-B", price="0.50")),
            has_more=True,
            next_cursor="cursor-2",
        )
        p2 = Page(items=(cls.open_order("0xorder-C", side="SELL", price="0.60"),), has_more=False)
        return (p1, p2)

    # -- get_balance_allowance ------------------------------------------------

    @staticmethod
    def balance(usd: str = "12.5") -> BalanceAllowance:
        base_units = int(Decimal(usd) * 1_000_000)  # pUSD has 6 decimals
        return BalanceAllowance(balance=base_units, allowances={"0x" + "e1" * 20: 2**255})


@pytest.fixture
def shapes() -> type[Shapes]:
    return Shapes


# ---------------------------------------------------------------------------
# FakeClob
# ---------------------------------------------------------------------------


class FakeClobUnscripted(AssertionError):
    """A client method was called with no answer queued for it."""


class FakeClob:
    """Stand-in for ``polymarket.SecureClient``, below ``execution.clob_client``.

    Only the six methods the code uses exist; any other attribute raises
    AttributeError. Each call is checked against the real SDK method's signature
    (so a keyword the SDK does not take fails), recorded in ``calls`` as
    ``(method, args, kwargs)``, and answered from a per-method FIFO queue filled
    with ``script``. Nothing queued -> FakeClobUnscripted (no default answers).
    A queued exception instance is raised instead of returned.
    ``list_open_orders`` takes a sequence of Pages and returns a real Paginator.
    """

    Unscripted = FakeClobUnscripted

    METHODS = (
        "place_limit_order",
        "place_market_order",
        "cancel_order",
        "get_order",
        "get_balance_allowance",
        "list_open_orders",
    )

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self._queues: dict[str, deque] = {m: deque() for m in self.METHODS}

    # -- scripting and inspection --------------------------------------------

    def script(self, method: str, *answers) -> "FakeClob":
        if method not in self._queues:
            raise KeyError(f"FakeClob has no method {method!r}")
        self._queues[method].extend(answers)
        return self

    def calls_to(self, method: str) -> list[tuple[tuple, dict]]:
        return [(a, k) for m, a, k in self.calls if m == method]

    def assert_no_calls(self) -> None:
        assert self.calls == [], f"client was called: {self.calls}"

    def assert_all_consumed(self) -> None:
        left = {m: len(q) for m, q in self._queues.items() if q}
        assert not left, f"scripted answers never used: {left}"

    # -- the client surface ---------------------------------------------------

    def _next(self, method: str, args: tuple, kwargs: dict):
        self.calls.append((method, args, kwargs))
        # same argument check the real method would do (TypeError on a wrong keyword)
        inspect.signature(getattr(SecureClient, method)).bind(object(), *args, **kwargs)
        queue = self._queues[method]
        if not queue:
            raise FakeClobUnscripted(
                f"FakeClob.{method} called with nothing scripted "
                f"(args={args}, kwargs={kwargs}); call fake_clob.script({method!r}, ...) first"
            )
        answer = queue.popleft()
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def place_limit_order(self, *args, **kwargs):
        return self._next("place_limit_order", args, kwargs)

    def place_market_order(self, *args, **kwargs):
        return self._next("place_market_order", args, kwargs)

    def cancel_order(self, *args, **kwargs):
        return self._next("cancel_order", args, kwargs)

    def get_order(self, *args, **kwargs):
        return self._next("get_order", args, kwargs)

    def get_balance_allowance(self, *args, **kwargs):
        return self._next("get_balance_allowance", args, kwargs)

    def list_open_orders(self, *args, **kwargs) -> Paginator:
        pages = self._next("list_open_orders", args, kwargs)
        if isinstance(pages, Page):
            pages = (pages,)
        pages = tuple(pages)
        fetched: list[str | None] = []

        def fetch(cursor: str | None) -> Page:
            # a Paginator must ask for exactly the cursor the previous page gave
            expected = None if not fetched else pages[len(fetched) - 1].next_cursor
            assert cursor == expected, f"paginator asked cursor {cursor!r}, expected {expected!r}"
            assert len(fetched) < len(pages), "paginator asked for more pages than scripted"
            fetched.append(cursor)
            return pages[len(fetched) - 1]

        return Paginator(fetch)


@pytest.fixture
def fake_clob():
    """A FakeClob injected as the SDK client; ``set_client(None)`` on teardown."""
    fake = FakeClob()
    clob_client.set_client(fake)
    try:
        yield fake
    finally:
        clob_client.set_client(None)


@pytest.fixture
def armed(monkeypatch):
    """The three components of ``live_config.live_armed()`` set to True: mode LIVE,
    POLYCLAWD_LIVE_STOPS_ENFORCE=1 and LIVE_ORDER_PATH_READY True. For tests that
    legitimately drive the live order path against ``fake_clob``; every other test
    runs unarmed and ``post_maker`` / ``cross_taker`` refuse."""
    monkeypatch.setenv("POLYCLAWD_MODE", "LIVE")
    monkeypatch.setenv("POLYCLAWD_LIVE_STOPS_ENFORCE", "1")
    monkeypatch.setattr(live_config, "LIVE_ORDER_PATH_READY", True, raising=False)
    assert live_config.live_armed() is True


def _assert_no_client_injected():
    """A client left in ``execution.clob_client`` by an earlier test would be
    used by this one: fail at setup, in any test order."""
    leaked = clob_client._client
    assert leaked is None, (
        f"execution.clob_client has a client injected before this test ({leaked!r}): "
        "an earlier test leaked it (inject through the fake_clob fixture, or call "
        "set_client(None) in a finally)"
    )


@pytest.fixture(autouse=True)
def _no_client_leaked_from_an_earlier_test():
    _assert_no_client_injected()
    yield


@pytest.fixture
def fixed_tick_and_min_size(monkeypatch):
    """get_tick_size / get_min_order_size query REST themselves, not the client:
    pin them to fixed values (0.01 and 5 shares)."""
    monkeypatch.setattr(clob_client, "get_tick_size", lambda token_id: 0.01)
    monkeypatch.setattr(clob_client, "get_min_order_size", lambda token_id: 5.0)


# ---------------------------------------------------------------------------
# live_db
# ---------------------------------------------------------------------------


@dataclass
class LiveDb:
    path: Path
    conn: sqlite3.Connection


@pytest.fixture
def live_db(tmp_path, monkeypatch):
    """Temporary live DB: created through ``execution.live_db.connect(path)`` (which
    applies the production DDL), POLYCLAWD_LIVE_DB set to it so that argument-less
    ``connect()`` calls in the code under test land here too. Yields ``LiveDb``
    with the file ``path`` and an open ``conn`` (closed on teardown)."""
    path = tmp_path / "live" / "shadow_trades.db"
    conn = live_db_module.connect(path)
    monkeypatch.setenv("POLYCLAWD_LIVE_DB", str(path))
    try:
        yield LiveDb(path=path, conn=conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# No network, no ambient egress guard
# ---------------------------------------------------------------------------


def _fail_on_network_attempts(attempts, opted_in):
    """Fail if the network guard fired during a test that did not ask for it.
    The guard raises AssertionError, but code under test may wrap its call in
    `except Exception` and swallow it, so the record is what fails the test.
    `opted_in` is True for tests that provoke the guard on purpose (they request
    the ``network_attempts`` fixture)."""
    if opted_in or not attempts:
        return
    what = "; ".join(f"{label} {args}" for label, args in attempts)
    pytest.fail(
        f"test attempted network access in tests/live ({what}). Code under test may "
        "have swallowed the guard's AssertionError. Stub the call, or request the "
        "`network_attempts` fixture if the attempt is intended.",
        pytrace=False,
    )


@pytest.fixture(autouse=True)
def _no_network_no_ambient_guard(request, monkeypatch):
    """tests/live must not touch the network: HTTP and socket connects fail loudly
    and are recorded; a test that did not request ``network_attempts`` fails at
    teardown if any was recorded (even one the code under test swallowed).
    The egress guard is inactive unless POLYCLAWD_EGRESS_REQUIRE_SRC is set, so
    remove any ambient value; tests that want the guard set it themselves."""
    monkeypatch.delenv("POLYCLAWD_EGRESS_REQUIRE_SRC", raising=False)
    monkeypatch.delenv("POLYCLAWD_EGRESS_HOSTS", raising=False)
    attempts: list = []

    def _guard(label):
        def _boom(*a, **k):
            attempts.append((label, a[:2]))
            raise AssertionError(f"network access attempted in tests/live: {a[:2]}")
        return _boom

    monkeypatch.setattr(requests.sessions.Session, "request", _guard("requests"))
    monkeypatch.setattr(urllib.request, "urlopen", _guard("urlopen"))
    monkeypatch.setattr(socket.socket, "connect", _guard("socket.connect"))
    monkeypatch.setattr(socket, "getaddrinfo", _guard("getaddrinfo"))
    yield attempts
    _fail_on_network_attempts(attempts, "network_attempts" in request.fixturenames)


@pytest.fixture
def network_attempts(_no_network_no_ambient_guard) -> list:
    """The network attempts recorded in this test (label, first two arguments).
    Requesting it marks the test as one that provokes the guard on purpose."""
    return _no_network_no_ambient_guard

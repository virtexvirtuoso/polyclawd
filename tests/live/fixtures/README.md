# tests/live/fixtures: exchange response shapes

`tests/live/conftest.py` (`Shapes`, `FakeClob`) scripts the SDK client with the
SDK's own response classes (`polymarket-client` 0.14.0), never dicts or mocks.
Because the fake sits below `execution/clob_client.py`, the wrapper's conversion
code runs in every test. Field values are plausible, not recorded.

| Shape | SDK class | Builder | Status |
|---|---|---|---|
| Accepted post (resting, `live`) | `AcceptedOrder` | `accepted_post` | built from SDK classes |
| Rejected post, would cross | `RejectedOrder` (`post_only_would_cross`) | `rejected_would_cross` | built from SDK classes |
| Rejected post, bad tick | `RejectedOrder` (`unknown`; message text is a guess) | `rejected_bad_tick` | built from SDK classes |
| Rejected, not enough balance | `RejectedOrder` (`not_enough_balance`) | `rejected_not_enough_balance` | built from SDK classes |
| `get_order` live | `OpenOrder` (`LIVE`) | `order_live` | built from SDK classes |
| `get_order` matched | `OpenOrder` (`MATCHED`) | `order_matched` | built from SDK classes |
| `get_order` cancelled | `OpenOrder` (`CANCELED`; spelling unverified) | `order_cancelled` | built from SDK classes |
| Cancel confirmed | `CancelOrdersResponse` | `cancel_confirmed` | built from SDK classes |
| Cancel `not_canceled` | `CancelOrdersResponse` | `cancel_not_canceled` | built from SDK classes |
| FAK filled (BUY and SELL) | `AcceptedOrder` (`matched`) | `fak_filled_buy`, `fak_filled_sell` | built from SDK classes |
| FAK unfilled | `RejectedOrder` (`fak_not_filled`) | `fak_unfilled` | built from SDK classes |
| Open orders, empty page | `Paginator` of `Page` | `empty_page` | built from SDK classes |
| Open orders, two pages | `Paginator` of `Page` of `OpenOrder` | `open_orders_pages` | built from SDK classes |
| Balance | `BalanceAllowance` | `balance` | built from SDK classes |

Only two shapes can be recorded without trading: the open-orders page, which is
the **empty** page because the account has no open orders as of 2026-11-11, and
`get_order` on finished orders, if the exchange still returns them. A separate
step will record them; until then every shape above is built from SDK classes,
and the first canary orders replace the rest with recordings.

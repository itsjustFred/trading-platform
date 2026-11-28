# Shared broker fakes

One stateful double per broker, sitting at the same seam the live adapters
use in production. Use these instead of hand-rolling a fake trading client or
protobuf stub: the doubles model the broker behaviours that have actually
shipped bugs (acknowledged-but-unfilled orders, netting vs hedging, partial
fills, post-acceptance rejections, paginated listings, bracket legs), so a
test that passes against them is testing the adapter, not the fake's gaps.

## Alpaca — `tests.fakes.alpaca`

`FakeAlpacaTradingClient` is a drop-in for `alpaca.trading.client.TradingClient`;
`FakeTradingStream` covers the websocket side; `fake_alpaca_transport(client)`
patches `alpaca.trading.client` / `alpaca.trading.stream` /
`alpaca.data.historical` in `sys.modules` so a real
`AlpacaLiveAdapter.connect()` builds the fake with no private-field poking.
`wire_alpaca_adapter(adapter, client)` is the escape hatch for tests that do
not drive `connect()`.

```python
from tests.fakes.alpaca import FakeAlpacaTradingClient, fake_alpaca_transport

client = FakeAlpacaTradingClient(mark_prices={"AAPL": 100.0})
with fake_alpaca_transport(client) as (client, stream, historical):
    adapter = AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True, account_id="acc")
    asyncio.run(adapter.connect())
    asyncio.run(adapter.submit_order(signal, qty=10.0))
    updates = client.advance(30.0)   # the broker fills; returns trade updates
    stream.deliver(client)           # ...which must be pushed to reach _on_trade_update
```

`advance()` only records and returns updates - nothing is pushed for you.
`FakeTradingStream.deliver(client)` hands the recorded updates to subscribed
callbacks; `client.trade_updates` holds them either way.

Known gap: `fake_alpaca_transport` stubs `StockHistoricalDataClient` only.
The adapter's crypto-bar path constructs `CryptoHistoricalDataClient`
directly (`live_runner/alpaca_adapter.py`), so a test exercising crypto
history would hit the real client — keep crypto coverage off this fake.

Key knobs: `mark_prices` (every traded symbol needs one — a missing mark
raises `KeyError` rather than silently filling at a fabricated price),
`fill_seconds` (confirmation delay),
`partial_fill_ratio`, `reject_submissions` (callable or list consumed in
order), `reject_after_acknowledgement`, `cancel_settles`, `allow_shorting`,
`page_size`. `client.advance(seconds)` is the only place fills happen — a
submit response is an acknowledgement, never a fill. `status=all` listings
paginate; bracket/OTO legs ride nested on the parent and are held until it
fills.

## cTrader — `tests.fakes.ctrader`

`FakeCTraderBroker` keeps the order book, positions (netted or hedged via
`account_type`), deals, SL/TP protection and the clock.
`FakeCTraderClient` mirrors upstream `ctrader_open_api.Client`: `send()`
answers synchronously through the same `_received` path the socket uses, so
responses resolve by `clientMsgId` and every payload also reaches the
message-received callback. `fake_ctrader_transport(broker)` patches
`ctrader_open_api.Client` (and `ctrader_open_api.client.Client`, which a
re-import would re-read) so `CTraderLiveAdapter.connect()` builds the fake.

```python
from tests.fakes.ctrader import (
    FakeCTraderBroker, FakeCTraderSymbol, fake_ctrader_transport,
)

broker = FakeCTraderBroker(
    account_id=123,
    account_type="hedged",
    symbols=[FakeCTraderSymbol.fx("EURUSD", 1)],
    mark_prices={"EURUSD": 1.10},
)
with fake_ctrader_transport(broker) as (_broker, client):
    adapter = CTraderLiveAdapter(...)
    asyncio.run(adapter.connect())
    asyncio.run(adapter.submit_order(signal, qty=1.0))
    for event in broker.advance(1.0):   # fills; returns ProtoOAExecutionEvents
        client.deliver(event)           # ...push each through _received -> _on_message
```

Key knobs: `account_type` (`"netted"` merges opposite fills on a symbol,
`"hedged"` opens a position per entry), `partial_fill_ratio`,
`reject_submissions` / `reject_after_acceptance` (the latter produces the
`ORDER_ACCEPTED` → `ORDER_REJECTED` shape; a rule that raises fails the
test loudly rather than silently accepting), `fill_delay_ms`, `page_size`
(order listings), `mark_prices` + `set_mark_price` drive stop/take-profit
triggers on `advance()` — every traded symbol needs a mark, a missing one
raises `KeyError` rather than silently filling at a fabricated price.
`GOOD_TILL_DATE` orders expire at their
`expirationTimestamp`; `IMMEDIATE_OR_CANCEL` unfilled orders cancel.
Relative SL/TP on the entry becomes absolute position levels at fill time.

Reactor note: `ctrader_open_api` binds `twisted.internet.reactor` at import,
so both the fake and `fake_ctrader_transport` import it lazily under
`live_runner.account_discovery._without_installed_reactor` — importing the
fake never decides the process's reactor. A test that drives a real
`connect()` should run where no platform reactor is already installed (the
contract test uses a subprocess for exactly this reason).

Concurrency note: both transports patch `sys.modules` globally and the
fakes keep per-instance state only — run broker-fake tests serially in one
process (no pytest-xdist sharing, no threads driving two fakes at once).

## Contract tests

`tests/fakes/test_alpaca.py` and `tests/fakes/test_ctrader.py` pin the fakes'
broker behaviour. If a fake stops modelling the broker, those fail first.

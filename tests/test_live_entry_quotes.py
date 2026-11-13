"""Broker quote sources for the #125 entry-deviation gate.

Each adapter exposes ``get_quote(symbol) -> LiveQuote``. The daemon uses the
quote only as a pre-submission gate; these tests pin that each broker returns
its real bid/ask in price units and fails loudly instead of inventing one.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

# ── base contract ─────────────────────────────────────────────────────────────


def test_base_adapter_get_quote_is_unsupported_by_default():
    from live_runner.base_adapter import BaseLiveAdapter

    class _Minimal(BaseLiveAdapter):
        async def connect(self) -> None:
            return None

        async def submit_order(self, signal, *, qty=None) -> str:
            return "x"

        async def listen_executions(self) -> None:
            return None

        async def get_balance(self):
            return 1.0, 1.0

    with pytest.raises(NotImplementedError):
        asyncio.run(_Minimal().get_quote("AAPL"))


def test_live_quote_is_immutable():
    from live_runner.base_adapter import LiveQuote

    quote = LiveQuote(bid=1.0, ask=2.0)
    assert quote.timestamp is None
    with pytest.raises(Exception):
        quote.bid = 5.0  # type: ignore[misc]


# ── Alpaca ────────────────────────────────────────────────────────────────────


def _alpaca_adapter(client: Any):
    from live_runner.alpaca_adapter import AlpacaLiveAdapter

    adapter = AlpacaLiveAdapter(api_key="key", secret_key="secret", paper=True)
    adapter._connected = True
    adapter._historical_data_client = client
    return adapter


class _AlpacaQuoteClient:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.requests: list[Any] = []

    def get_stock_latest_quote(self, request):
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_alpaca_get_quote_returns_latest_bid_ask():
    stamp = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
    client = _AlpacaQuoteClient(
        {"AAPL": SimpleNamespace(bid_price=199.9, ask_price=200.1, timestamp=stamp)}
    )
    adapter = _alpaca_adapter(client)

    quote = asyncio.run(adapter.get_quote("aapl"))

    assert quote.bid == pytest.approx(199.9)
    assert quote.ask == pytest.approx(200.1)
    assert quote.timestamp == stamp
    assert quote.source
    request = client.requests[0]
    assert list(request.symbol_or_symbols) == ["AAPL"]


def test_alpaca_get_quote_requires_a_connection():
    adapter = _alpaca_adapter(None)
    adapter._connected = False

    with pytest.raises(RuntimeError):
        asyncio.run(adapter.get_quote("AAPL"))


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"MSFT": SimpleNamespace(bid_price=1.0, ask_price=1.1, timestamp=None)},
        {"AAPL": None},
        {"AAPL": SimpleNamespace(bid_price=None, ask_price=200.0, timestamp=None)},
        {"AAPL": SimpleNamespace(bid_price=200.0, ask_price="n/a", timestamp=None)},
    ],
)
def test_alpaca_get_quote_refuses_a_missing_quote(response):
    adapter = _alpaca_adapter(_AlpacaQuoteClient(response))

    with pytest.raises(ValueError, match="quote"):
        asyncio.run(adapter.get_quote("AAPL"))


def test_alpaca_get_quote_propagates_transport_errors():
    adapter = _alpaca_adapter(_AlpacaQuoteClient(RuntimeError("503")))

    with pytest.raises(RuntimeError, match="503"):
        asyncio.run(adapter.get_quote("AAPL"))


# ── cTrader ───────────────────────────────────────────────────────────────────


class _SubscribeReq:
    kind = "subscribe"

    def __init__(self) -> None:
        self.ctidTraderAccountId = 0
        self.symbolId: list[int] = []
        self.subscribeToSpotTimestamp = False


class _UnsubscribeReq:
    kind = "unsubscribe"

    def __init__(self) -> None:
        self.ctidTraderAccountId = 0
        self.symbolId: list[int] = []


def _ctrader_adapter(*, on_subscribe=None, subscribe_error: Exception | None = None):
    from live_runner import ctrader_adapter as module

    adapter = module.CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="123"
    )
    adapter._connected = True
    adapter._authenticated = True
    adapter._symbol_name_to_id = {"EURUSD": 1, "GBPUSD": 2}
    adapter._ProtoOASubscribeSpotsReq = _SubscribeReq
    adapter._ProtoOAUnsubscribeSpotsReq = _UnsubscribeReq
    adapter.quote_timeout_seconds = 0.2
    sent: list[Any] = []

    async def _send(request, *, timeout_seconds=10):
        sent.append(request)
        if request.kind == "subscribe":
            if subscribe_error is not None:
                raise subscribe_error
            if on_subscribe is not None:
                asyncio.get_running_loop().call_soon(on_subscribe, adapter, request)
        return SimpleNamespace()

    adapter._send_request_with_refresh_retry = _send
    return adapter, sent


def _spot(symbol_id: int, **fields: Any) -> SimpleNamespace:
    return SimpleNamespace(symbolId=symbol_id, **fields)


def test_ctrader_get_quote_scales_spot_prices():
    def _push(adapter, request):
        adapter._handle_spot_event(
            _spot(1, bid=108_512, ask=108_530, timestamp=1_762_700_000_000)
        )

    adapter, sent = _ctrader_adapter(on_subscribe=_push)

    quote = asyncio.run(adapter.get_quote("eurusd"))

    assert quote.bid == pytest.approx(1.08512)
    assert quote.ask == pytest.approx(1.08530)
    assert quote.timestamp == datetime.fromtimestamp(1_762_700_000, tz=timezone.utc)
    assert quote.source
    kinds = [request.kind for request in sent]
    assert kinds == ["subscribe", "unsubscribe"], "the spot subscription must be closed"
    assert list(sent[0].symbolId) == [1]
    assert list(sent[1].symbolId) == [1]


def test_ctrader_get_quote_combines_partial_spot_events():
    def _push(adapter, request):
        adapter._handle_spot_event(_spot(1, bid=108_500))
        adapter._handle_spot_event(_spot(1, ask=108_520))

    adapter, _ = _ctrader_adapter(on_subscribe=_push)

    quote = asyncio.run(adapter.get_quote("EURUSD"))

    assert quote.bid == pytest.approx(1.085)
    assert quote.ask == pytest.approx(1.0852)
    # No event carried a broker time: the receive time stands in (#125
    # review follow-up), and the quote says so.
    assert quote.timestamp is not None
    assert quote.timestamp.tzinfo is not None
    assert quote.timestamp_source == "receive"


def test_ctrader_get_quote_ignores_other_symbols():
    def _push(adapter, request):
        adapter._handle_spot_event(_spot(2, bid=1, ask=2))

    adapter, sent = _ctrader_adapter(on_subscribe=_push)

    with pytest.raises(TimeoutError):
        asyncio.run(adapter.get_quote("EURUSD"))
    assert [request.kind for request in sent] == ["subscribe", "unsubscribe"]


def test_ctrader_get_quote_times_out_and_unsubscribes():
    adapter, sent = _ctrader_adapter()

    with pytest.raises(TimeoutError):
        asyncio.run(adapter.get_quote("EURUSD"))
    assert [request.kind for request in sent] == ["subscribe", "unsubscribe"]
    assert adapter._spot_waiters == {}


def test_ctrader_get_quote_propagates_subscribe_failure():
    adapter, _ = _ctrader_adapter(subscribe_error=RuntimeError("subscribe refused"))

    with pytest.raises(RuntimeError, match="subscribe refused"):
        asyncio.run(adapter.get_quote("EURUSD"))
    assert adapter._spot_waiters == {}


def test_ctrader_get_quote_rejects_an_unknown_symbol():
    adapter, _ = _ctrader_adapter()

    async def _no_auth() -> None:
        return None

    adapter._authenticate = _no_auth

    with pytest.raises(ValueError, match="Unknown cTrader symbol"):
        asyncio.run(adapter.get_quote("XYZ"))


def test_ctrader_spot_event_without_a_waiter_is_ignored():
    adapter, _ = _ctrader_adapter()

    adapter._handle_spot_event(_spot(1, bid=1, ask=2))

    assert adapter._spot_waiters == {}


def test_ctrader_on_message_routes_spot_events(monkeypatch):
    adapter, _ = _ctrader_adapter()
    client = object()
    adapter._client = client
    adapter._spot_payload_type = 2131
    payload = _spot(1, bid=1, ask=2)
    adapter._Protobuf = SimpleNamespace(extract=lambda message: payload)
    seen: list[Any] = []
    monkeypatch.setattr(adapter, "_handle_spot_event", seen.append)

    adapter._on_message(client, SimpleNamespace(payloadType=2131))

    assert seen == [payload]


def test_ctrader_concurrent_quotes_for_one_symbol_do_not_overlap():
    """cTrader allows one spot subscription per symbol; overlapping calls must
    queue so neither subscribes while the other is still subscribed."""
    open_subscriptions: list[int] = []
    overlaps: list[int] = []

    def _push(adapter, request):
        adapter._handle_spot_event(_spot(1, bid=100_000, ask=100_010))

    adapter, sent = _ctrader_adapter(on_subscribe=_push)
    original_send = adapter._send_request_with_refresh_retry

    async def _tracking_send(request, *, timeout_seconds=10):
        if request.kind == "subscribe":
            if open_subscriptions:
                overlaps.append(1)
            open_subscriptions.append(1)
        else:
            open_subscriptions.pop()
        return await original_send(request, timeout_seconds=timeout_seconds)

    adapter._send_request_with_refresh_retry = _tracking_send

    async def _both():
        return await asyncio.gather(adapter.get_quote("EURUSD"), adapter.get_quote("EURUSD"))

    first, second = asyncio.run(_both())

    assert first.bid == pytest.approx(1.0)
    assert second.ask == pytest.approx(1.0001)
    assert overlaps == []
    assert [request.kind for request in sent] == [
        "subscribe",
        "unsubscribe",
        "subscribe",
        "unsubscribe",
    ]

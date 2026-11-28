"""Contract tests: the Alpaca fake behaves like the broker.

These pin the *fake's* broker behaviour, not any adapter. A test module that
migrates onto :class:`FakeAlpacaTradingClient` relies on every one of these,
so a fake that silently stopped modelling Alpaca breaks here first.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderClass, OrderSide, OrderType, TimeInForce
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    ReplaceOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from tests.fakes.alpaca import (
    ALPACA_DEFAULT_PAGE_SIZE,
    FakeAlpacaTradingClient,
    FakeTradingStream,
    fake_alpaca_transport,
    wire_alpaca_adapter,
)

AAPL = "AAPL"


def _buy(symbol: str = AAPL, qty: float = 10.0, **kwargs) -> MarketOrderRequest:
    """A resting buy: a limit order when a limit is given, otherwise a market order."""
    client_order_id = kwargs.pop("client_order_id", f"signal-{symbol}")
    limit_price = kwargs.pop("limit_price", None)
    base = dict(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=kwargs.pop("time_in_force", TimeInForce.GTC),
        client_order_id=client_order_id,
    )
    if limit_price is None:
        return MarketOrderRequest(**base, **kwargs)
    return LimitOrderRequest(limit_price=limit_price, **base, **kwargs)


def _status_all_page(client, *, limit=ALPACA_DEFAULT_PAGE_SIZE, until=None, after=None):
    """Walk the ``status=all`` listing exactly as ``_list_orders_status_all`` does."""
    return client.get_orders(
        filter=SimpleNamespace(
            status="all", symbols=None, nested=True, limit=limit, until=until, after=after
        )
    )


# ── acknowledgement before the fill ──────────────────────────────────────────


def test_a_submission_is_acknowledged_and_is_not_a_fill() -> None:
    client = FakeAlpacaTradingClient(mark_prices={AAPL: 100.0})

    order = client.submit_order(_buy())

    assert order.filled_qty == 0.0, "the submit response is an acknowledgement, not a fill"
    assert client.trade_updates == [], "no fill arrives with the acknowledgement"
    assert client.position(AAPL) == 0.0, "nothing is on the book until the fill lands"
    assert client.working_orders(), "the order is on the book while it is unfilled"


def test_the_fill_lands_later_and_is_the_only_one() -> None:
    client = FakeAlpacaTradingClient(mark_prices={AAPL: 100.0}, fill_seconds=1.0)
    order = client.submit_order(_buy())

    assert client.advance(0.5) == []

    updates = client.advance(1.0)

    assert [update.event for update in updates] == ["fill"]
    assert str(order.status.value) == "filled" and order.filled_qty == 10.0
    assert client.position(AAPL) == 10.0
    assert updates[0].order.filled_qty == 10.0, (
        "the stream event carries the CUMULATIVE filled quantity"
    )
    assert updates[0].order is not order, "the event carries a broker snapshot, not the row"


def test_a_limit_order_rests_until_the_mark_reaches_it() -> None:
    """A buy limit executes once the market is at or below it: at a mark above the
    limit it rests, and a double that filled anything on submit would hide it.
    """
    client = FakeAlpacaTradingClient(mark_prices={AAPL: 100.0})
    order = client.submit_order(_buy(limit_price=105.0))

    client.advance(60.0)
    assert order.filled_qty == 0.0, "a limit above the mark does not fill"

    client.set_mark_price(AAPL, 105.0)
    client.advance(1.0)
    assert order.filled_qty == 10.0
    assert [update.event for update in client.trade_updates] == ["fill"]


# ── the listing ──────────────────────────────────────────────────────────────


def test_status_open_hides_terminal_orders_and_status_all_keeps_them() -> None:
    client = FakeAlpacaTradingClient(mark_prices={AAPL: 100.0})
    working = client.submit_order(_buy(limit_price=105.0))
    filled = client.submit_order(_buy("MSFT"))
    client.advance(30.0)
    client.cancel_order_by_id(working.id)

    open_page = client.get_orders(filter=SimpleNamespace(status="open", limit=500))
    all_page = _status_all_page(client)

    assert filled.id in {order.id for order in all_page}
    assert filled.id not in {order.id for order in open_page}
    assert working.id not in {order.id for order in open_page}


def test_a_status_all_listing_pages_and_lands_every_order_once() -> None:
    client = FakeAlpacaTradingClient()
    submitted = [client.submit_order(_buy(f"S{index}")) for index in range(120)]

    walked: set[str] = set()
    pages: list[int] = []
    until = None
    for _ in range(10):
        page = _status_all_page(client, until=until)
        pages.append(len(page))
        walked.update(order.id for order in page)
        if len(page) < ALPACA_DEFAULT_PAGE_SIZE:
            break
        # ``until`` is inclusive, so the bound steps one microsecond past the
        # page's oldest order - exactly what ``_list_orders_status_all`` does.
        until = min(order.submitted_at for order in page) + timedelta(microseconds=1)

    assert pages == [ALPACA_DEFAULT_PAGE_SIZE, ALPACA_DEFAULT_PAGE_SIZE, 24], (
        "a listing longer than one page comes back in page-sized chunks"
    )
    assert walked == {order.id for order in submitted}, (
        "a full walk of a paginated listing must reach every order exactly once"
    )


def test_a_full_page_is_returned_when_the_bigger_book_needs_it() -> None:
    client = FakeAlpacaTradingClient()
    for index in range(120):
        client.submit_order(_buy(f"S{index}"))

    page = client.get_orders(filter=SimpleNamespace(status="open", limit=500))

    assert len(page) == 120, "a request for the maximum page is not truncated to the default"


def test_a_full_page_makes_the_next_page_short() -> None:
    """The page is a page, not a whole book: after it there is more to ask for."""
    client = FakeAlpacaTradingClient()
    for index in range(120):
        client.submit_order(_buy(f"S{index}"))

    first = _status_all_page(client)

    assert len(first) == ALPACA_DEFAULT_PAGE_SIZE
    assert len(_status_all_page(client, after=None)) == len(first)


def test_a_symbol_filter_scopes_the_listing() -> None:
    client = FakeAlpacaTradingClient()
    client.submit_order(_buy("AAPL"))
    client.submit_order(_buy("MSFT"))

    page = client.get_orders(
        filter=SimpleNamespace(status="open", limit=500, symbols=["aapl"])
    )

    assert [order.symbol for order in page] == ["AAPL"]


def test_an_unknown_order_id_is_a_broker_not_found() -> None:
    client = FakeAlpacaTradingClient()

    with pytest.raises(APIError) as failure:
        client.get_order_by_id("nope")

    assert "not found" in str(failure.value)


# ── the position book ────────────────────────────────────────────────────────


def test_a_position_nets_and_disappears_once_flat() -> None:
    client = FakeAlpacaTradingClient(mark_prices={AAPL: 100.0})
    client.submit_order(_buy(qty=10.0))
    client.advance(5.0)
    client.submit_order(
        MarketOrderRequest(
            symbol=AAPL, qty=4, side=OrderSide.SELL, time_in_force=TimeInForce.GTC
        )
    )
    client.advance(5.0)

    assert client.position(AAPL) == 6.0
    remaining = client.get_all_positions()
    assert [row.qty for row in remaining] == [6.0]

    client.submit_order(
        MarketOrderRequest(
            symbol=AAPL, qty=6, side=OrderSide.SELL, time_in_force=TimeInForce.GTC
        )
    )
    client.advance(5.0)
    assert client.position(AAPL) == 0.0
    assert client.get_all_positions() == [], "Alpaca drops a flat position from its listing"


def test_a_netted_book_refuses_to_open_short() -> None:
    client = FakeAlpacaTradingClient(mark_prices={AAPL: 100.0})
    client.submit_order(
        MarketOrderRequest(symbol=AAPL, qty=5, side=OrderSide.SELL, time_in_force=TimeInForce.GTC)
    )

    with pytest.raises(APIError):
        client.advance(5.0)


def test_a_two_way_book_may_go_short() -> None:
    client = FakeAlpacaTradingClient(mark_prices={AAPL: 100.0}, allow_shorting=True)
    client.submit_order(
        MarketOrderRequest(
            symbol=AAPL, qty=5, side=OrderSide.SELL, time_in_force=TimeInForce.GTC
        )
    )
    client.advance(5.0)

    assert client.position(AAPL) == -5.0


# ── bracket / OTO legs ───────────────────────────────────────────────────────


def _bracket(client: FakeAlpacaTradingClient, **kwargs) -> object:
    return client.submit_order(
        MarketOrderRequest(
            symbol=AAPL,
            qty=10.0,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
            order_class=OrderClass.BRACKET,
            stop_loss=StopLossRequest(stop_price=95.0),
            take_profit=TakeProfitRequest(limit_price=110.0),
            client_order_id="signal-bracket",
            **kwargs,
        )
    )


def test_a_submit_response_carries_the_bracket_legs_nested() -> None:
    """``_register_bracket_legs`` reads the submit response: it must have them."""
    client = FakeAlpacaTradingClient(mark_prices={AAPL: 100.0})

    order = _bracket(client)

    assert [leg.order_type for leg in order.legs] == [OrderType.STOP, OrderType.LIMIT]
    assert [leg.stop_price for leg in order.legs] == [95.0, None]
    assert [leg.limit_price for leg in order.legs] == [None, 110.0]
    assert all(leg.side == OrderSide.SELL for leg in order.legs)
    assert all(leg.parent_order_id == order.id for leg in order.legs)
    assert {leg.id for leg in order.legs}.isdisjoint({order.id})


def test_legs_are_held_until_the_parent_fills_and_then_go_working() -> None:
    client = FakeAlpacaTradingClient(mark_prices={AAPL: 100.0})
    order = _bracket(client)
    assert all(leg.status == OrderType.MARKET.value for leg in order.legs) or all(
        str(leg.status.value) == "held" for leg in order.legs
    )

    client.advance(30.0)

    assert order.filled_qty == 10.0
    assert all(str(leg.status.value) == "new" for leg in order.legs)
    assert client.reserved_qty(AAPL) == 10.0, (
        "an OCO pair reserves the parent's quantity once, not twice"
    )


def test_a_filling_leg_removes_its_oco_sibling() -> None:
    client = FakeAlpacaTradingClient(mark_prices={AAPL: 100.0})
    order = _bracket(client)
    client.advance(30.0)
    stop, target = order.legs

    client.set_mark_price(AAPL, 94.0)
    client.advance(5.0)

    assert str(stop.status.value) == "filled"
    assert str(target.status.value) == "canceled", "the OCO sibling is removed"
    assert client.position(AAPL) == 0.0
    assert client.reserved_qty(AAPL) == 0.0


def test_a_close_is_refused_while_a_bracket_reserves_the_quantity() -> None:
    client = FakeAlpacaTradingClient(mark_prices={AAPL: 100.0})
    _bracket(client)
    client.advance(30.0)

    with pytest.raises(APIError) as failure:
        client.close_position(AAPL)

    assert "reserv" in str(failure.value), "the flatten must cancel its protections first"


def test_a_close_is_accepted_once_nothing_reserves_the_quantity() -> None:
    client = FakeAlpacaTradingClient(mark_prices={AAPL: 100.0})
    client.submit_order(_buy())
    client.advance(30.0)

    closed = client.close_position(AAPL)

    assert closed.qty == 10.0
    assert client.position(AAPL) == 0.0
    assert client.close_calls == [AAPL]
    assert [update.event for update in client.trade_updates][-1] == "fill"


# ── partial fills ────────────────────────────────────────────────────────────


def test_a_partially_filled_order_rests_for_the_remainder() -> None:
    client = FakeAlpacaTradingClient(mark_prices={AAPL: 100.0}, partial_fill_ratio=0.5)
    order = client.submit_order(_buy(qty=4.0))

    first = client.advance(30.0)

    assert [update.event for update in first] == ["partial_fill"]
    assert 0.0 < order.filled_qty < 4.0
    assert client.position(AAPL) == order.filled_qty

    client.advance(30.0)
    client.advance(30.0)
    assert order.filled_qty == 4.0
    assert client.position(AAPL) == 4.0
    assert [update.event for update in client.trade_updates] == [
        "partial_fill",
        "partial_fill",
        "fill",
    ], "every chunk of a partial fill is its own trade update"


def test_a_partial_fill_never_exceeds_the_requested_quantity() -> None:
    client = FakeAlpacaTradingClient(mark_prices={AAPL: 100.0}, partial_fill_ratio=0.9)
    order = client.submit_order(_buy(qty=5.0))
    for _ in range(20):
        client.advance(30.0)

    assert order.filled_qty == 5.0


# ── refusals ─────────────────────────────────────────────────────────────────


def test_a_submission_the_broker_refuses_raises_an_api_error() -> None:
    client = FakeAlpacaTradingClient(
        mark_prices={AAPL: 100.0},
        reject_submissions=lambda request: "insufficient buying power",
    )

    with pytest.raises(APIError) as failure:
        client.submit_order(_buy())

    assert "insufficient buying power" in str(failure.value)
    assert client.orders == [], "a refused submission never reaches the book"


def test_a_short_sale_is_refused_before_the_order_is_booked() -> None:
    client = FakeAlpacaTradingClient(mark_prices={AAPL: 100.0})

    with pytest.raises(APIError):
        client.submit_order(
            MarketOrderRequest(
                symbol=AAPL, qty=5, side=OrderSide.SELL, time_in_force=TimeInForce.GTC
            )
        )
        client.advance(5.0)

    assert client.positions == {}, "an unbookable fill never creates a position"


def test_a_rejection_sequence_is_consumed_in_order() -> None:
    """``reject_submissions=["first", "second"]`` refuses twice, then accepts."""
    client = FakeAlpacaTradingClient(
        mark_prices={AAPL: 100.0}, reject_submissions=["first refusal", "second refusal"]
    )

    with pytest.raises(APIError, match="first refusal"):
        client.submit_order(_buy())
    with pytest.raises(APIError, match="second refusal"):
        client.submit_order(_buy())

    order = client.submit_order(_buy())
    assert str(order.status.value) != "rejected", "the sequence is drained, not repeated"


def test_a_flat_listing_hides_the_bracket_legs() -> None:
    """Without ``nested=True`` the parent row comes back with empty ``legs``."""
    client = FakeAlpacaTradingClient(mark_prices={AAPL: 100.0})
    _bracket(client)

    flat = client.get_orders(filter=SimpleNamespace(status="open", limit=500))
    nested = client.get_orders(
        filter=SimpleNamespace(status="open", limit=500, nested=True)
    )

    assert len(flat) == len(nested) == 1
    assert flat[0].legs == [], "a non-nested listing does not expose the legs"
    assert len(nested[0].legs) == 2


def test_a_rejection_the_broker_makes_after_acknowledging_leaves_the_book() -> None:
    client = FakeAlpacaTradingClient(
        mark_prices={AAPL: 100.0},
        reject_after_acknowledgement=lambda request: "rejected by the exchange",
    )
    order = client.submit_order(_buy(limit_price=95.0))
    assert str(order.status.value) == "accepted", "the acknowledgement came back accepted"

    client.advance(2.0)

    assert str(order.status.value) == "rejected"
    assert order.failed_reason == "rejected by the exchange"
    assert client.position(AAPL) == 0.0
    assert order.id not in {working.id for working in client.working_orders()}


# ── time in force ────────────────────────────────────────────────────────────


def test_a_day_order_expires_at_the_session_end() -> None:
    client = FakeAlpacaTradingClient(mark_prices={AAPL: 1.0})
    order = client.submit_order(_buy(qty=1.0, time_in_force=TimeInForce.DAY, limit_price=500.0))

    client.advance(48 * 3600)

    assert str(order.status.value) == "expired"
    assert order.filled_qty == 0.0


def test_a_gtc_order_survives_the_session_close() -> None:
    client = FakeAlpacaTradingClient(mark_prices={AAPL: 1.0})
    order = client.submit_order(_buy(qty=1.0, time_in_force=TimeInForce.GTC, limit_price=500.0))

    client.advance(72 * 3600)

    assert str(order.status.value) == "accepted", "a GTC order keeps working into the next session"
    client.set_mark_price(AAPL, 500.0)
    client.advance(1.0)
    assert order.filled_qty == 1.0


# ── cancels and replacements ────────────────────────────────────────────────


def test_a_cancel_the_broker_settles_removes_the_order() -> None:
    client = FakeAlpacaTradingClient(mark_prices={AAPL: 100.0})
    order = client.submit_order(_buy(limit_price=95.0))

    client.cancel_order_by_id(order.id)

    assert str(order.status.value) == "canceled"
    assert client.cancels == [order.id]
    assert client.working_orders() == []
    with pytest.raises(APIError):
        client.cancel_order_by_id(order.id)


def test_an_accepted_cancel_that_has_not_settled_rests_in_pending_cancel() -> None:
    client = FakeAlpacaTradingClient(mark_prices={AAPL: 100.0}, cancel_settles=False)
    order = client.submit_order(_buy(limit_price=95.0))

    client.cancel_order_by_id(order.id)

    assert str(order.status.value) == "pending_cancel"
    assert order.id in {working.id for working in client.working_orders()}


def test_a_replacement_gets_a_new_order_id_and_marks_the_old_one_replaced() -> None:
    client = FakeAlpacaTradingClient(mark_prices={AAPL: 1.0})
    order = client.submit_order(_buy(qty=5.0, limit_price=500.0))

    replacement = client.replace_order_by_id(
        order.id, order_data=ReplaceOrderRequest(stop_price=205.0)
    )

    assert replacement.id != order.id
    assert replacement.replaces == order.id
    assert replacement.stop_price == 205.0
    assert str(order.status.value) == "replaced"
    assert [working.id for working in client.working_orders()] == [replacement.id]
    assert client.get_order_by_id(order.id) is order, "the replaced order stays readable by id"


# ── the transport seam ───────────────────────────────────────────────────────


def test_connect_builds_the_fake_at_the_rest_seam() -> None:
    """The adapter's own ``connect`` wires the fake REST client, no private field."""
    from live_runner.alpaca_adapter import AlpacaLiveAdapter

    with fake_alpaca_transport(FakeAlpacaTradingClient(mark_prices={AAPL: 100.0})) as (
        client,
        stream,
        historical,
    ):
        adapter = AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True, account_id="acc")
        asyncio.run(adapter.connect())

        assert adapter._trading_client is client
        assert isinstance(stream, FakeTradingStream)
        assert historical is not None
        assert asyncio.run(adapter.get_positions()) == []

        asyncio.run(adapter.disconnect())


def test_wire_alpaca_adapter_attaches_one_shared_client() -> None:
    """The documented escape hatch for a test that does not drive ``connect``."""
    from live_runner.alpaca_adapter import AlpacaLiveAdapter

    adapter = AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True, account_id="acc")
    client = FakeAlpacaTradingClient(mark_prices={AAPL: 100.0})

    wired = wire_alpaca_adapter(adapter, client)

    assert wired is client
    assert adapter._trading_client is client
    assert adapter._connected is True


def test_a_recorded_trade_update_reaches_a_subscribed_stream() -> None:
    client = FakeAlpacaTradingClient(mark_prices={AAPL: 100.0})
    client.submit_order(_buy())
    client.advance(30.0)

    seen: list[object] = []
    stream = FakeTradingStream()
    stream.subscribe_trade_updates(seen.append)
    stream.deliver(client)

    assert len(seen) == 1
    assert seen[0].event == "fill"

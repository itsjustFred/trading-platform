"""Alpaca control-plane behaviour, against a fake trading client.

The ordering tested here is the whole point of the STOPPED path: attached SL/TP
legs must survive the entry-cancel sweep, because the flatten runs afterwards and
a failed flatten must not leave a naked position.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from live_runner import alpaca_adapter as alpaca_module
from live_runner.alpaca_adapter import (
    _ALPACA_HELD_LEG_LOOKUP_LIMIT,
    _RESTING_LEG_STATUSES,
    AlpacaLiveAdapter,
    _alpaca_market_data_start,
    _order_legs,
)
from live_runner.base_adapter import is_order_not_submitted
from shared.models import LiveSignal
from shared.money import MoneyParseError


@dataclass
class _FakeOrder:
    id: str
    symbol: str
    side: str
    order_type: str = "limit"
    order_class: str = "simple"
    qty: float = 0.0
    stop_price: float | None = None
    limit_price: float | None = None
    status: object = "new"
    legs: list["_FakeOrder"] = field(default_factory=list)
    submitted_at: datetime | None = None
    #: What the broker reports as filled. ``object`` so a test can model a row the
    #: adapter cannot read (a cancelled partial fill carries a quantity, an order
    #: that never filled reads as zero, and a broker that cannot be asked says
    #: something that is not a number).
    filled_qty: object = 0.0
    client_order_id: str = ""


@dataclass
class _FakePosition:
    symbol: str
    qty: float
    avg_entry_price: float = 1.0
    current_price: float = 1.0
    unrealized_pl: float = 0.0


_TERMINAL_ORDER_STATUSES = {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced"}


def _status_text(order: object) -> str:
    raw = getattr(order, "status", "")
    return str(getattr(raw, "value", raw) or "").strip().lower()


def _side_text(order_data: object) -> str:
    raw = getattr(order_data, "side", "")
    return str(getattr(raw, "value", raw) or "").strip().lower()


def _class_text(order_data: object) -> str:
    raw = getattr(order_data, "order_class", "")
    return str(getattr(raw, "value", raw) or "").strip().lower()


def _type_text(order_data: object) -> str:
    raw = getattr(order_data, "type", "")
    return str(getattr(raw, "value", raw) or "").strip().lower()


class _FakeTradingClient:
    """A broker double that enforces Alpaca's quantity-reservation rule.

    A close is refused while any reducing-side order on the symbol still holds
    the position's quantity. That refusal is the whole reason the flatten has
    to cancel the protections first, so the double has to reproduce it - a
    double that closes regardless would let a broken ordering pass its test.
    """

    def __init__(
        self,
        *,
        orders: list[_FakeOrder] | None = None,
        positions: list[_FakePosition] | None = None,
        parents: list[_FakeOrder] | None = None,
        close_error: Exception | None = None,
        submit_error: Exception | None = None,
        cancel_settles: bool = True,
    ) -> None:
        # Alpaca keeps an accepted cancel in the book as ``pending_cancel`` until
        # it settles; ``cancel_settles=False`` reproduces that window.
        self.cancel_settles = cancel_settles
        self.orders = list(orders or [])
        # Filled bracket parents: listed only by ``status=all``, with their legs
        # nested, and never by ``status=open`` (#245).
        self.parents = list(parents or [])
        self.positions = list(positions or [])
        self.cancelled: list[str] = []
        self.close_calls: list[str] = []
        self.submitted: list[object] = []
        self.close_error = close_error
        self.submit_error = submit_error
        self.account = SimpleNamespace(
            cash=100_000.0, equity=100_000.0, currency="USD", multiplier="2"
        )
        self.order_filters: list[object] = []
        self.history: dict[str, _FakeOrder] = {}

    def get_account(self):
        return self.account

    def get_orders(self, filter=None):  # noqa: A002 - mirrors alpaca-py
        # alpaca-py's server-side default page size is 50.
        limit = getattr(filter, "limit", None) or 50
        self.last_order_filter = filter
        self.order_filters.append(filter)
        status = str(getattr(getattr(filter, "status", None), "value", "") or "").lower()
        symbols = getattr(filter, "symbols", None)
        if status == "all":
            # ``status=all``: filled parents come back with their legs nested.
            result = list(self.orders) + list(self.parents)
        else:
            # ``status=open``: top-level working orders only, legs never included.
            result = [
                order
                for order in self.orders
                if _status_text(order) not in _TERMINAL_ORDER_STATUSES
            ]
        if symbols:
            wanted = {str(symbol).strip().upper() for symbol in symbols}
            result = [
                order
                for order in result
                if str(getattr(order, "symbol", "") or "").strip().upper() in wanted
            ]
        until = getattr(filter, "until", None)
        after = getattr(filter, "after", None)
        if until is not None:
            result = [
                order
                for order in result
                if getattr(order, "submitted_at", None) is None
                or order.submitted_at <= until
            ]
        if after is not None:
            result = [
                order
                for order in result
                if getattr(order, "submitted_at", None) is None
                or order.submitted_at >= after
            ]
        return result[:limit]

    def get_order_by_id(self, order_id: str):
        for order in (*self.orders, *self.parents, *self.history.values()):
            if getattr(order, "id", None) == order_id:
                return order
        for order in (*self.orders, *self.parents, *self.history.values()):
            for leg in _order_legs(order):
                if getattr(leg, "id", None) == order_id:
                    return leg
        raise RuntimeError(f"404: order {order_id} not found")

    def get_all_positions(self):
        return list(self.positions)

    def _mark_cancelled(self, order: _FakeOrder) -> None:
        if self.cancel_settles:
            self.orders = [item for item in self.orders if item is not order]
            return
        order.status = "pending_cancel"

    def _cancel_nested(self, order_id: str) -> bool:
        for order in self.orders:
            for leg in order.legs:
                if leg.id == order_id:
                    if self.cancel_settles:
                        leg.status = "canceled"
                    else:
                        leg.status = "pending_cancel"
                    return True
        return False

    def cancel_order_by_id(self, order_id: str) -> None:
        self.cancelled.append(order_id)
        for order in self.orders:
            if order.id == order_id:
                self._mark_cancelled(order)
                return
        if self._cancel_nested(order_id):
            return
        raise RuntimeError(f"order {order_id} not found")

    def _working_orders(self, symbol: str) -> list[_FakeOrder]:
        return [
            order
            for order in self.orders
            if order.symbol == symbol and _status_text(order) not in _TERMINAL_ORDER_STATUSES
        ]

    def _reserved_qty(self, symbol: str) -> float:
        position = next((item for item in self.positions if item.symbol == symbol), None)
        if position is None:
            return 0.0
        side = "sell" if position.qty > 0 else "buy"
        reserved = sum(
            abs(order.qty)
            for order in self._working_orders(symbol)
            if order.side == side and "market" not in order.order_type
        )
        # A bracket's held stop leg reserves the same shares its take-profit
        # sibling does (OCO): count it once via the parent, not twice. The leg
        # can sit under a filled order in ``orders`` or under a filled parent in
        # ``parents``; only while it rests - a filled or settled leg reserves
        # nothing. A leg can arrive without its own symbol, and the parent it
        # rests under is what places it: the reservation is on the position's
        # symbol either way.
        for order in self.orders:
            if _status_text(order) != "filled" or order.symbol != symbol:
                continue
            for leg in order.legs:
                if (
                    _status_text(leg) == "held"
                    and leg.side == side
                    and "market" not in leg.order_type
                ):
                    reserved += abs(leg.qty)
        for parent in self.parents:
            if str(getattr(parent, "status", "new")).lower() != "filled":
                continue
            reserved += sum(
                abs(leg.qty)
                for leg in (getattr(parent, "legs", []) or [])
                if (leg.symbol or parent.symbol) == symbol
                and leg.side == side
                and _resting(leg)
            )
        return reserved

    def close_position(self, symbol: str, close_options=None):
        self.close_calls.append(symbol)
        if self.close_error is not None:
            raise self.close_error
        if self._reserved_qty(symbol) > 0:
            raise RuntimeError("insufficient qty available for order to be filled")
        self.positions = [item for item in self.positions if item.symbol != symbol]
        return SimpleNamespace(id=str(uuid4()), symbol=symbol)

    def _fill_market_order(self, order_data: object) -> None:
        """A market order fills at once in the double; reduce the position."""
        symbol = str(order_data.symbol)
        side = _side_text(order_data)
        qty = abs(float(order_data.qty))
        for index, position in enumerate(self.positions):
            if position.symbol != symbol:
                continue
            if side == "sell" and position.qty > 0:
                position.qty = max(position.qty - qty, 0.0)
            elif side == "buy" and position.qty < 0:
                position.qty = min(position.qty + qty, 0.0)
            if abs(position.qty) <= 1e-9:
                self.positions.pop(index)

    def submit_order(self, order_data):
        if self.submit_error is not None:
            raise self.submit_error
        self.submitted.append(order_data)
        order_class = _class_text(order_data)
        order_type = _type_text(order_data)
        side = _side_text(order_data)
        symbol = str(order_data.symbol)
        qty = abs(float(order_data.qty))
        if order_class == "oco":
            take_profit = getattr(order_data, "take_profit", None)
            stop_loss = getattr(order_data, "stop_loss", None)
            tp_price = (
                float(take_profit.limit_price)
                if take_profit is not None and take_profit.limit_price is not None
                else None
            )
            sl_price = (
                float(stop_loss.stop_price)
                if stop_loss is not None and stop_loss.stop_price is not None
                else None
            )
            sl_limit = (
                float(stop_loss.limit_price)
                if stop_loss is not None and getattr(stop_loss, "limit_price", None) is not None
                else None
            )
            # Alpaca's real OCO shape: the parent IS the take-profit order
            # (a limit order carrying limit_price); the stop-loss is its only
            # leg, with a broker-assigned client order id of its own.
            parent = _FakeOrder(
                id=str(uuid4()),
                symbol=symbol,
                side=side,
                order_type="limit",
                order_class="oco",
                qty=qty,
                limit_price=tp_price,
                status="new",
                legs=[
                    _FakeOrder(
                        id=str(uuid4()),
                        symbol=symbol,
                        side=side,
                        order_type="stop_limit" if sl_limit is not None else "stop",
                        qty=qty,
                        stop_price=sl_price,
                        limit_price=sl_limit,
                        status="new",
                    ),
                ],
            )
            self.orders.append(parent)
            return SimpleNamespace(id=parent.id, symbol=symbol, status="new")
        if order_type == "market":
            position = next(
                (item for item in self.positions if item.symbol == symbol), None
            )
            if position is not None:
                reducing = (side == "sell" and position.qty > 0) or (
                    side == "buy" and position.qty < 0
                )
                if reducing:
                    available = abs(position.qty) - self._reserved_qty(symbol)
                    if qty > available + 1e-9:
                        self.submitted.pop()
                        raise RuntimeError(
                            "insufficient qty available for order to be filled"
                        )
            order = _FakeOrder(
                id=str(uuid4()),
                symbol=symbol,
                side=side,
                order_type="market",
                qty=qty,
                status="filled",
                filled_qty=qty,
            )
            self.orders.append(order)
            self._fill_market_order(order_data)
            return SimpleNamespace(id=order.id, symbol=symbol, status="filled")
        order = _FakeOrder(
            id=str(uuid4()),
            symbol=symbol,
            side=side,
            order_type="stop" if order_type == "stop" else "limit",
            qty=qty,
            stop_price=(
                float(order_data.stop_price)
                if getattr(order_data, "stop_price", None) is not None
                else None
            ),
            limit_price=(
                float(order_data.limit_price)
                if getattr(order_data, "limit_price", None) is not None
                else None
            ),
        )
        self.orders.append(order)
        return SimpleNamespace(id=order.id, symbol=order.symbol, status="new")


class _BracketPositionClient(_FakeTradingClient):
    """Alpaca's recorded bracket shape after the entry fills (issue #245).

    Paper probe 2026-11-16: on a fresh bracket the take-profit leg is ``new`` and
    is the only thing any ``status=open`` listing returns, while the stop leg stays
    ``held`` and is reachable only under the filled parent in a ``status=all``
    listing.

    Paper probe 2026-11-20: cancelling the take profit takes the stop leg with it,
    and both then read back by id as ``canceled`` - still there, terminal, not
    working. So a cancel that removes an order here marks it ``canceled`` rather
    than deleting it, and it refuses the cancel of an order that is already
    terminal. ``oco_siblings=False`` models a broker that leaves the sibling
    working; a sweep has to cope with both.
    """

    def __init__(
        self,
        *,
        positions: list[_FakePosition] | None = None,
        qty: float = 10.0,
        stop_price: float = 95.0,
        oco_siblings: bool = True,
        take_profit_filled: bool = False,
        **kwargs,
    ) -> None:
        stop = _FakeOrder(
            "sl-leg",
            "AAPL",
            "sell",
            order_type="stop",
            qty=qty,
            status="held",
            stop_price=stop_price,
        )
        take_profit = _FakeOrder(
            "tp-leg",
            "AAPL",
            "sell",
            order_type="limit",
            qty=qty,
            status="filled" if take_profit_filled else "new",
        )
        parent = _FakeOrder(
            "parent",
            "AAPL",
            "buy",
            order_type="market",
            order_class="bracket",
            status="filled",
            legs=[take_profit, stop],
        )
        super().__init__(
            orders=[] if take_profit_filled else [take_profit],
            positions=positions,
            parents=[parent],
            **kwargs,
        )
        self.parent = parent
        self.oco_siblings = oco_siblings
        # Every order this broker ever held, which is what get_order_by_id answers from:
        # a terminal order is still readable, the paper probe showed.
        self.history = {order.id: order for order in (parent, take_profit, stop)}

    def cancel_order_by_id(self, order_id: str) -> None:
        order = self.history.get(order_id)
        if order is not None and not _working(order):
            # A non-working order cannot be cancelled, and the broker says so rather
            # than accepting it silently.
            self.cancelled.append(order_id)
            raise RuntimeError(f"422: order {order_id} is not cancelable")
        if order is None:
            self.cancelled.append(order_id)
            raise RuntimeError(f"404: order {order_id} is not working")
        self.cancelled.append(order_id)
        if not self.cancel_settles:
            order.status = "pending_cancel"
            return
        order.status = "canceled"
        self.orders = [item for item in self.orders if item.id != order_id]
        if self.oco_siblings and order in self.parent.legs:
            # The bracket is an OCO pair: the sibling goes with it, as it did in the
            # paper probe - and it stays readable, as canceled.
            for leg in self.parent.legs:
                leg.status = "canceled"

    def get_order_by_id(self, order_id: str):
        order = self.history.get(order_id)
        if order is None:
            raise RuntimeError(f"404: order {order_id} not found")
        return order


class _FullHeldLegPageClient(_FakeTradingClient):
    """A history that never runs out: every page comes back full."""

    def get_orders(self, filter=None):  # noqa: A002 - mirrors alpaca-py
        status = str(getattr(getattr(filter, "status", None), "value", "open")).lower()
        if status == "open":
            return list(self.orders)
        self.order_filters.append(filter)
        cursor = getattr(filter, "until", None) or datetime(
            2026, 11, 20, 21, 0, tzinfo=timezone.utc
        )
        if cursor.tzinfo is None:
            cursor = cursor.replace(tzinfo=timezone.utc)
        return [
            _FakeOrder(
                f"old-{index}",
                "AAPL",
                "buy",
                status="filled",
                submitted_at=cursor - timedelta(seconds=index + 1),
            )
            for index in range(_ALPACA_HELD_LEG_LOOKUP_LIMIT)
        ]


def _status_all_requests(client: _FakeTradingClient) -> list[object]:
    """Every ``status=all`` listing made, so a test can pin the request shape."""
    return [
        request
        for request in client.order_filters
        if str(getattr(getattr(request, "status", None), "value", "open")).lower() == "all"
    ]


#: Statuses no further cancel can be sent for. A ``filled`` order has nothing left to
#: cancel; a ``partially_filled`` one still has, which is why it is not in this set. These
#: are alpaca-py 0.44.0's real states - there is no "done" (the end-of-day one is
#: ``done_for_day``, and it resumes the next session, so it is not terminal here either).
_TERMINAL_STATUSES = frozenset({"canceled", "expired", "rejected", "filled"})


def _working(order: object) -> bool:
    """Whether a cancel can still be sent for this order."""
    return str(getattr(order, "status", "new")).strip().lower() not in _TERMINAL_STATUSES


def _resting(order: object) -> bool:
    """Whether a bracket leg still rests at the broker (the adapter's own set)."""
    return str(getattr(order, "status", "new")).strip().lower() in _RESTING_LEG_STATUSES


def _adapter(client: _FakeTradingClient) -> AlpacaLiveAdapter:
    adapter = AlpacaLiveAdapter(api_key="key", secret_key="secret", paper=True)
    adapter._connected = True
    adapter._trading_client = client
    return adapter


def test_cancel_open_orders_accepts_pending_cancel_as_success() -> None:
    # Found on paper: a cancel Alpaca accepted was still listed as pending_cancel on
    # the immediate re-read and came back as failed=(<id>,).
    client = _FakeTradingClient(
        orders=[_FakeOrder("entry-1", "AAPL", "buy")], cancel_settles=False
    )

    outcome = asyncio.run(_adapter(client).cancel_open_orders())

    assert client.cancelled == ["entry-1"]
    assert outcome.failed == ()
    assert outcome.completed == 1
    assert outcome.complete is True
    # ...but the order may still fill in a cancel/fill race, so it is carried out.
    assert outcome.pending_cancel == ("entry-1",)


def test_cancel_open_orders_reports_no_pending_cancel_when_cancels_settle() -> None:
    client = _FakeTradingClient(orders=[_FakeOrder("entry-1", "AAPL", "buy")])

    outcome = asyncio.run(_adapter(client).cancel_open_orders())

    assert outcome.complete is True
    assert outcome.pending_cancel == ()


def test_cancel_open_orders_accepts_enum_valued_pending_cancel() -> None:
    client = _FakeTradingClient(
        orders=[_FakeOrder("entry-1", "AAPL", "buy")], cancel_settles=False
    )
    original = client.cancel_order_by_id

    def cancel(order_id: str) -> None:
        original(order_id)
        for order in client.orders:
            order.status = SimpleNamespace(value="pending_cancel")  # alpaca-py enum shape

    client.cancel_order_by_id = cancel  # type: ignore[method-assign]

    outcome = asyncio.run(_adapter(client).cancel_open_orders())

    assert outcome.failed == ()


def test_cancel_open_orders_still_reports_a_cancel_the_broker_dropped() -> None:
    # A cancel that was "accepted" but left the order live (status still new) is a
    # real failure and must keep being reported.
    client = _FakeTradingClient(orders=[_FakeOrder("entry-1", "AAPL", "buy")])
    client.cancel_order_by_id = lambda order_id: client.cancelled.append(order_id)  # type: ignore[method-assign]

    outcome = asyncio.run(_adapter(client).cancel_open_orders())

    assert outcome.failed == ("entry-1",)
    assert outcome.complete is False


def test_cancel_open_orders_reports_an_uncancelled_pending_cancel_order() -> None:
    # An order this sweep never cancelled (it appeared between the two reads) and
    # that sits in pending_cancel is not vouched for: the sweep is incomplete.
    client = _FakeTradingClient(orders=[_FakeOrder("entry-1", "AAPL", "buy")])
    stranger = _FakeOrder("stranger-1", "MSFT", "buy")
    stranger.status = "pending_cancel"
    reads = {"n": 0}
    original_get = client.get_orders

    def get_orders(filter=None):  # noqa: A002 - mirrors alpaca-py
        reads["n"] += 1
        result = original_get(filter)
        return [*result, stranger] if reads["n"] > 1 else result

    client.get_orders = get_orders  # type: ignore[method-assign]

    outcome = asyncio.run(_adapter(client).cancel_open_orders())

    assert client.cancelled == ["entry-1"]
    assert outcome.failed == ("stranger-1",)
    assert outcome.complete is False


def test_cancel_open_orders_does_not_excuse_pending_cancel_after_refused_cancel() -> None:
    client = _FakeTradingClient(orders=[_FakeOrder("entry-1", "AAPL", "buy")])

    def refuse(order_id: str) -> None:
        client.orders[0].status = "pending_cancel"
        raise RuntimeError("refused")

    client.cancel_order_by_id = refuse  # type: ignore[method-assign]

    outcome = asyncio.run(_adapter(client).cancel_open_orders())

    assert "entry-1" in outcome.failed
    assert outcome.complete is False


def test_cancel_open_orders_treats_refused_recancel_of_pending_cancel_as_pending() -> None:
    # An order that an earlier pass already cancelled is still pending_cancel. If the
    # broker refuses the repeat cancel, the order is still on its way out: it is
    # reported as pending_cancel (fail closed, retried), not as a failed sweep that
    # hides the cancel that was accepted.
    client = _FakeTradingClient(
        orders=[_FakeOrder("entry-1", "AAPL", "buy", status="pending_cancel")]
    )

    def refuse(order_id: str) -> None:
        client.cancelled.append(order_id)
        raise RuntimeError("422: order is already pending cancel")

    client.cancel_order_by_id = refuse  # type: ignore[method-assign]

    outcome = asyncio.run(_adapter(client).cancel_open_orders())

    assert client.cancelled == ["entry-1"]
    assert outcome.failed == ()
    assert outcome.unidentified == ()
    assert outcome.completed == 0
    assert outcome.pending_cancel == ("entry-1",)


def test_cancel_open_orders_accepts_mixed_case_pending_cancel_status() -> None:
    client = _FakeTradingClient(
        orders=[_FakeOrder("entry-1", "AAPL", "buy")], cancel_settles=False
    )
    original = client.cancel_order_by_id

    def cancel(order_id: str) -> None:
        original(order_id)
        client.orders[0].status = " Pending_Cancel "

    client.cancel_order_by_id = cancel  # type: ignore[method-assign]

    outcome = asyncio.run(_adapter(client).cancel_open_orders())

    assert outcome.failed == ()


def test_cancel_open_orders_accepts_dotted_enum_render_of_pending_cancel() -> None:
    # An enum-like status without ``.value`` renders as ``orderstatus.pending_cancel``.
    client = _FakeTradingClient(
        orders=[_FakeOrder("entry-1", "AAPL", "buy")], cancel_settles=False
    )
    original = client.cancel_order_by_id

    def cancel(order_id: str) -> None:
        original(order_id)
        client.orders[0].status = "OrderStatus.PENDING_CANCEL"

    client.cancel_order_by_id = cancel  # type: ignore[method-assign]

    outcome = asyncio.run(_adapter(client).cancel_open_orders())

    assert outcome.failed == ()


def test_cancel_open_orders_leaves_attached_sl_tp_alone() -> None:
    client = _FakeTradingClient(
        orders=[
            _FakeOrder("entry-1", "AAPL", "buy"),
            _FakeOrder("sl-1", "AAPL", "sell", order_type="stop"),
            _FakeOrder("tp-1", "AAPL", "sell", order_type="limit"),
        ],
        positions=[_FakePosition("AAPL", 10.0)],
    )

    outcome = asyncio.run(_adapter(client).cancel_open_orders())

    assert outcome.complete is True
    assert client.cancelled == ["entry-1"]
    assert [order.id for order in client.orders] == ["sl-1", "tp-1"]


def test_fake_refuses_a_close_while_protections_reserve_the_quantity() -> None:
    """The double reproduces the broker rule the flatten has to work around.

    Without this refusal the flatten tests would pass on any ordering, including
    the one that keeps the SL/TP working and is therefore never executed.
    """
    client = _FakeTradingClient(
        orders=[_FakeOrder("sl-1", "AAPL", "sell", order_type="stop", qty=10.0)],
        positions=[_FakePosition("AAPL", 10.0)],
    )

    with pytest.raises(RuntimeError, match="insufficient qty"):
        client.close_position("AAPL")

    client.cancel_order_by_id("sl-1")
    client.close_position("AAPL")

    assert client.positions == []


def test_flatten_cancels_protections_before_closing() -> None:
    client = _FakeTradingClient(
        orders=[
            _FakeOrder(
                "sl-1", "AAPL", "sell", order_type="stop", qty=10.0, stop_price=190.0
            ),
            _FakeOrder("tp-1", "AAPL", "sell", order_type="limit", qty=10.0),
        ],
        positions=[_FakePosition("AAPL", 10.0)],
    )

    outcome = asyncio.run(_adapter(client).close_all_positions())

    assert outcome.complete is True
    # The legs go first, and only then is the position closed.
    assert client.cancelled == ["sl-1", "tp-1"]
    assert client.close_calls == ["AAPL"]
    assert client.positions == []


def test_refused_close_restores_the_stop_it_cancelled() -> None:
    client = _FakeTradingClient(
        orders=[
            _FakeOrder(
                "sl-1", "AAPL", "sell", order_type="stop", qty=10.0, stop_price=190.0
            ),
            _FakeOrder("tp-1", "AAPL", "sell", order_type="limit", qty=10.0),
        ],
        positions=[_FakePosition("AAPL", 10.0)],
        close_error=RuntimeError("broker refused to close"),
    )

    outcome = asyncio.run(_adapter(client).close_all_positions())

    assert outcome.complete is False
    assert any(item.startswith("AAPL:close:") for item in outcome.failed)
    # The position survived; the stop that was cancelled to unblock the close is
    # back on it, at the price the strategy had chosen.
    restored = [order for order in client.orders if order.order_type == "stop"]
    assert len(restored) == 1
    assert restored[0].side == "sell"
    assert restored[0].qty == 10.0
    assert restored[0].stop_price == 190.0


def test_a_wrong_side_stop_does_not_count_as_protection() -> None:
    """A buy stop on a long is an entry, not protection.

    The flatten cancels the position's sell stop first, but a buy stop on the same
    symbol can still be working - another intent's entry, or a cancel the broker
    accepted without applying. Counting it as protection reports the long as
    protected while it is naked, so the restore has to look at the side.
    """
    client = _FakeTradingClient(
        orders=[
            _FakeOrder(
                "buy-stop", "AAPL", "buy", order_type="stop", qty=10.0, stop_price=210.0
            )
        ],
        positions=[_FakePosition("AAPL", 10.0)],
    )
    adapter = _adapter(client)

    restored = asyncio.run(adapter._restore_stop(client, "AAPL", 190.0))

    assert restored is True
    submitted = [order for order in client.submitted if order.stop_price == 190.0]
    assert len(submitted) == 1, "the long was left without a reducing-side stop"
    assert str(getattr(submitted[0].side, "value", submitted[0].side)).lower() == "sell"


def test_a_reducing_side_stop_is_not_duplicated() -> None:
    """The other half: a stop that does protect the position is left alone.

    Re-submitting next to it would leave two stops on one position, and the second
    becomes an orphan the moment the first fills.
    """
    client = _FakeTradingClient(
        orders=[
            _FakeOrder(
                "sl-1", "AAPL", "sell", order_type="stop", qty=10.0, stop_price=190.0
            )
        ],
        positions=[_FakePosition("AAPL", 10.0)],
    )
    adapter = _adapter(client)

    assert asyncio.run(adapter._restore_stop(client, "AAPL", 190.0)) is True
    assert client.submitted == []


def test_an_unreadable_working_book_does_not_restore_a_stop() -> None:
    """A stop is not re-placed next to one the broker may still be holding.

    The re-read before a restore is what proves no stop still works, and the
    paged held-leg lookup that makes it complete is also what can fail. "Restoring
    anyway" then places a second stop beside whatever the broker kept: two stops
    reduce the position twice, and the extra one is an orphan the moment the
    first fills - an orphaned sell-stop opens a short. A position left
    unprotected until the next enforcement retry is the bounded outcome; a
    duplicate stop is not bounded at all.
    """
    client = _FakeTradingClient(positions=[_FakePosition("AAPL", 10.0)])
    adapter = _adapter(client)

    def _unreadable_book(filter=None):  # noqa: A002 - mirrors alpaca-py
        raise RuntimeError("429: rate limit reached")

    client.get_orders = _unreadable_book  # type: ignore[method-assign]

    assert asyncio.run(adapter._restore_stop(client, "AAPL", 190.0)) is False
    assert client.submitted == []


def test_refused_close_without_a_restorable_stop_is_reported_as_naked() -> None:
    client = _FakeTradingClient(
        orders=[
            _FakeOrder(
                "sl-1", "AAPL", "sell", order_type="stop", qty=10.0, stop_price=190.0
            ),
        ],
        positions=[_FakePosition("AAPL", 10.0)],
        close_error=RuntimeError("broker refused to close"),
        submit_error=RuntimeError("broker is down"),
    )

    outcome = asyncio.run(_adapter(client).close_all_positions())

    assert outcome.complete is False
    # A failure to restore the stop is a naked live position: it must be
    # visible in the outcome, not just in a log line.
    assert any("NO-STOP-RESTORED" in item for item in outcome.failed)


def test_cancel_refusal_does_not_attempt_the_close() -> None:
    client = _FakeTradingClient(
        orders=[_FakeOrder("sl-1", "AAPL", "sell", order_type="stop", qty=10.0)],
        positions=[_FakePosition("AAPL", 10.0)],
    )

    def _refuse_cancel(order_id: str) -> None:
        raise RuntimeError("cancel rejected")

    client.cancel_order_by_id = _refuse_cancel  # type: ignore[method-assign]

    outcome = asyncio.run(_adapter(client).close_all_positions())

    assert outcome.complete is False
    # Closing while the order still reserves the quantity would be refused by
    # the broker anyway; not attempting it keeps the failure unambiguous.
    assert client.close_calls == []
    assert any(item.startswith("AAPL:cancel:") for item in outcome.failed)


def test_cancel_failure_after_a_partial_sweep_restores_the_stop() -> None:
    """Half a cancel leaves the position without its stop; put one back."""
    client = _FakeTradingClient(
        orders=[
            _FakeOrder(
                "sl-1", "AAPL", "sell", order_type="stop", qty=10.0, stop_price=190.0
            ),
            _FakeOrder("tp-1", "AAPL", "sell", order_type="limit", qty=10.0),
        ],
        positions=[_FakePosition("AAPL", 10.0)],
    )
    real_cancel = client.cancel_order_by_id

    def _fail_on_tp(order_id: str) -> None:
        if order_id == "tp-1":
            raise RuntimeError("cancel rejected")
        real_cancel(order_id)

    client.cancel_order_by_id = _fail_on_tp  # type: ignore[method-assign]

    outcome = asyncio.run(_adapter(client).close_all_positions())

    assert outcome.complete is False
    assert client.close_calls == []
    stops = [order for order in client.orders if order.order_type == "stop"]
    assert len(stops) == 1
    assert stops[0].stop_price == 190.0


def test_a_cancel_the_broker_ignores_blocks_the_close() -> None:
    """An accepted-but-unapplied cancel still reserves the quantity.

    The re-read after the cancel is what makes that visible; without it the
    adapter would call close_position and rely on the broker to refuse.
    """
    client = _FakeTradingClient(
        orders=[
            _FakeOrder(
                "sl-1", "AAPL", "sell", order_type="stop", qty=10.0, stop_price=190.0
            ),
        ],
        positions=[_FakePosition("AAPL", 10.0)],
    )

    # The broker acknowledges the cancel and keeps the order working.
    client.cancel_order_by_id = lambda order_id: client.cancelled.append(  # type: ignore[method-assign]
        order_id
    )

    outcome = asyncio.run(_adapter(client).close_all_positions())

    assert outcome.complete is False
    assert client.close_calls == []
    assert any("orders-still-working" in item for item in outcome.failed)
    # The stop still works, so no second stop is submitted on top of it.
    assert [order.id for order in client.orders] == ["sl-1"]
    assert client.submitted == []


def test_close_all_positions_reports_a_partial_flatten() -> None:
    client = _FakeTradingClient(positions=[_FakePosition("AAPL", 10.0)])
    adapter = _adapter(client)

    # The broker acknowledges but the position survives: the re-read must make
    # the outcome incomplete instead of trusting the acknowledgement.
    def _acknowledge_only(symbol: str, close_options=None):
        client.close_calls.append(symbol)
        return SimpleNamespace(id=str(uuid4()), symbol=symbol)

    client.close_position = _acknowledge_only  # type: ignore[method-assign]

    outcome = asyncio.run(adapter.close_all_positions())

    assert outcome.complete is False
    assert "AAPL" in outcome.failed


def test_a_position_whose_qty_cannot_be_read_is_not_treated_as_flat() -> None:
    """An unreadable quantity must not pass as flat in the restore either.

    Reading it as zero would skip the restore and report the position protected
    while nothing protects it - the other half of fail-closed on a broker row this
    adapter cannot read. Sizing a stop from it is just as wrong, so the restore
    refuses instead and the flatten reports the position as unprotected.
    """
    client = _FakeTradingClient(
        positions=[SimpleNamespace(symbol="AAPL", qty="not-a-number")],
        orders=[],
    )
    adapter = _adapter(client)

    live = asyncio.run(adapter._live_position("AAPL"))

    assert live.flat is False, "an unreadable quantity must not pass as flat"
    assert live.stop_qty is None, "a stop must not be sized from an unreadable position"
    assert asyncio.run(adapter._restore_stop(client, "AAPL", 190.0)) is False
    assert client.submitted == []


def test_the_live_position_reads_the_broker_row() -> None:
    """The other half: a live position is not flat, and it can be sized from."""
    client = _FakeTradingClient(positions=[_FakePosition("AAPL", 10.0)], orders=[])
    adapter = _adapter(client)

    live = asyncio.run(adapter._live_position("AAPL"))

    assert (live.flat, live.stop_qty, live.stop_side) == (False, 10.0, "sell")
    assert asyncio.run(adapter._live_position("MSFT")).flat is True


def test_a_positions_read_that_fails_is_reported_not_swallowed(caplog) -> None:
    """A listing that cannot be read is an operator signal, not a silent answer.

    The old fail-closed on it returned "not flat" without a word, so a persistent
    listing failure re-submitted a stop on every refused close with nothing to say
    why - and a second stop next to a working one is an orphan the moment the
    first fills. The answer stays fail-closed; the silence does not.
    """
    import logging

    client = _FakeTradingClient(orders=[], positions=[])
    adapter = _adapter(client)

    def _unreachable():
        raise RuntimeError("positions unreachable")

    client.get_all_positions = _unreachable  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING, logger="trading.live-runner"):
        live = asyncio.run(adapter._live_position("AAPL"))

    assert (live.flat, live.stop_qty) == (False, None)
    warnings = [
        record for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert any(
        "Could not re-read positions" in record.message for record in warnings
    )
    assert any("positions unreachable" in str(getattr(record, "data", {})) for record in warnings)


def test_a_flat_position_is_not_given_a_stop() -> None:
    """A restored stop on a flat position is the orphan the sweeps remove.

    The stop that was cancelled to unblock a close can have been the thing that closed
    the position. Submitting it again then creates a sell-stop with no position behind
    it, and when it triggers it opens a short.
    """
    client = _FakeTradingClient(orders=[], positions=[])
    adapter = _adapter(client)

    assert asyncio.run(adapter._restore_stop(client, "AAPL", 190.0)) is True
    assert client.submitted == []


def test_a_stop_that_fills_during_the_flatten_leaves_no_second_stop() -> None:
    """The same guard, on the flatten's own path: the leg fills as it is cancelled."""
    client = _BracketPositionClient(
        positions=[_FakePosition("AAPL", 10.0)],
        take_profit_filled=True,
        close_error=RuntimeError("broker refused to close"),
    )
    real_cancel = client.cancel_order_by_id

    def _fill_the_stop(order_id: str) -> None:
        real_cancel(order_id)
        # The leg filled instead of resting, so the position it protected is gone.
        client.positions = []

    client.cancel_order_by_id = _fill_the_stop  # type: ignore[method-assign]

    outcome = asyncio.run(_adapter(client).close_all_positions())

    assert outcome.complete is False
    assert outcome.failed == ("AAPL:close:broker refused to close",)
    assert not any("NO-STOP-RESTORED" in item for item in outcome.failed)
    assert client.submitted == []


def test_a_restored_stop_is_sized_from_the_live_position() -> None:
    """The stop is sized from the position, not from the flatten's snapshot.

    The leg can fill while the flatten cancels it, so the live position is then
    smaller than the one the flatten started from. A stop sized from the old
    quantity oversells the position it is meant to protect, and when it triggers
    it sells more than there is - a short opened by the very order that was
    supposed to prevent one.
    """
    client = _FakeTradingClient(
        orders=[
            _FakeOrder(
                "sl-1", "AAPL", "sell", order_type="stop", qty=10.0, stop_price=190.0
            )
        ],
        positions=[_FakePosition("AAPL", 10.0)],
        close_error=RuntimeError("broker refused to close"),
    )
    real_cancel = client.cancel_order_by_id

    def _partly_fill_during_the_cancel(order_id: str) -> None:
        real_cancel(order_id)
        # The leg executed against the position it protected on its way out, so
        # the broker now reports a smaller position than the flatten saw.
        client.positions = [_FakePosition("AAPL", 3.0)]

    client.cancel_order_by_id = _partly_fill_during_the_cancel  # type: ignore[method-assign]

    outcome = asyncio.run(_adapter(client).close_all_positions())

    assert outcome.complete is False
    (restored,) = client.submitted
    assert float(restored.qty) == 3.0, "the stop was sized from the pre-flatten snapshot"
    assert str(getattr(restored.side, "value", restored.side)).lower() == "sell"


def test_a_restored_stop_takes_the_live_side_of_a_flipped_position() -> None:
    """The other half: a position that flipped needs a stop on the other side.

    The side the flatten started from is the side of a position the broker no
    longer reports. Restoring a sell stop onto a short would never trigger on it
    (and on a long it would be the wrong side of the book), so the restore reads
    the position it is about to protect.
    """
    client = _FakeTradingClient(
        orders=[
            _FakeOrder(
                "sl-1", "AAPL", "sell", order_type="stop", qty=10.0, stop_price=190.0
            )
        ],
        positions=[_FakePosition("AAPL", 10.0)],
        close_error=RuntimeError("broker refused to close"),
    )
    real_cancel = client.cancel_order_by_id

    def _flip_during_the_cancel(order_id: str) -> None:
        real_cancel(order_id)
        client.positions = [_FakePosition("AAPL", -5.0)]

    client.cancel_order_by_id = _flip_during_the_cancel  # type: ignore[method-assign]

    outcome = asyncio.run(_adapter(client).close_all_positions())

    assert outcome.complete is False
    (restored,) = client.submitted
    assert float(restored.qty) == 5.0
    assert str(getattr(restored.side, "value", restored.side)).lower() == "buy"


def test_cancel_orphaned_protections_only_touches_flat_symbols() -> None:
    client = _FakeTradingClient(
        orders=[
            _FakeOrder("sl-open", "AAPL", "sell", order_type="stop"),
            _FakeOrder("sl-orphan", "MSFT", "sell", order_type="stop"),
        ],
        positions=[_FakePosition("AAPL", 5.0)],
    )

    outcome = asyncio.run(_adapter(client).cancel_orphaned_protections())

    assert outcome.complete is True
    assert client.cancelled == ["sl-orphan"]
    assert [order.id for order in client.orders] == ["sl-open"]


# ─── #245: the bracket legs Alpaca lists only as ``held`` legs of the parent ────


def test_flatten_cancels_the_held_stop_leg_before_closing() -> None:
    """The stop leg of a filled bracket rests at the broker and must be cancelled.

    Alpaca paper 2026-11-16: on a fresh bracket, ``close_all_positions`` reported
    ``orders-still-working ... NO-STOP-RESTORED``, because the sweep's book never saw
    the held stop leg. The take profit is the only order ``status=open`` lists, so
    the flatten reads the stop leg from the filled parent - the recorded shape
    below - and cancels it before closing.
    """
    client = _BracketPositionClient(positions=[_FakePosition("AAPL", 10.0)])

    outcome = asyncio.run(_adapter(client).close_all_positions())

    assert outcome.complete is True
    assert outcome.failed == ()
    # The take profit first (all ``status=open`` lists), then the held stop leg,
    # whose cancel the broker refuses because its OCO sibling already removed it -
    # and the re-read excuses that. Only then is the position closed.
    assert client.cancelled == ["tp-leg", "sl-leg"]
    assert client.close_calls == ["AAPL"]
    assert client.positions == []
    assert client.submitted == []
    held_leg_requests = _status_all_requests(client)
    assert held_leg_requests, "the held stop leg must come from a status=all listing"
    assert held_leg_requests[0].nested is True
    # Scoped to the position being flattened, not the whole account.
    assert [str(symbol) for symbol in held_leg_requests[0].symbols] == ["AAPL"]


def test_flatten_cancels_the_held_stop_leg_when_the_take_profit_already_filled() -> None:
    """The take profit can fill first: ``status=open`` is then empty.

    The held stop leg is all that still rests on the position, and it still
    reserves its quantity, so the close is refused until the flatten cancels it.
    """
    client = _BracketPositionClient(
        positions=[_FakePosition("AAPL", 10.0)],
        take_profit_filled=True,
        oco_siblings=False,
    )

    outcome = asyncio.run(_adapter(client).close_all_positions())

    assert outcome.complete is True
    assert client.cancelled == ["sl-leg"]
    assert client.close_calls == ["AAPL"]
    assert client.positions == []


def test_a_refused_close_restores_the_held_stop_leg_at_its_own_price() -> None:
    """The probe's CRITICAL: the sweep had no stop price to restore.

    The flatten re-places a stop on every failure after it cancelled a protection,
    at the price that protection carried. A stop leg read from the filled parent
    carries its own, so a bracket position is restored, not reported naked.
    """
    client = _BracketPositionClient(
        positions=[_FakePosition("AAPL", 10.0)],
        oco_siblings=False,
        close_error=RuntimeError("broker refused to close"),
    )

    outcome = asyncio.run(_adapter(client).close_all_positions())

    assert outcome.complete is False
    assert outcome.failed == ("AAPL:close:broker refused to close",)
    assert not any("NO-STOP-RESTORED" in item for item in outcome.failed)
    restored = [order for order in client.orders if order.order_type == "stop"]
    assert [(order.stop_price, order.qty, order.side) for order in restored] == [
        (95.0, 10.0, "sell")
    ]


def test_a_held_stop_leg_the_broker_will_not_cancel_keeps_the_position_protected() -> None:
    """A stop leg the sweep cannot remove is reported, not duplicated.

    Re-placing a stop next to one that still works leaves two stops on one
    position, and the second becomes an orphan when the first fills - an orphaned
    sell-stop opens a short. A held leg is the working stop it is.
    """
    client = _BracketPositionClient(
        positions=[_FakePosition("AAPL", 10.0)], oco_siblings=False
    )
    real_cancel = client.cancel_order_by_id

    def _refuse_the_stop_leg(order_id: str) -> None:
        if order_id == "sl-leg":
            raise RuntimeError("cancel rejected")
        real_cancel(order_id)

    client.cancel_order_by_id = _refuse_the_stop_leg  # type: ignore[method-assign]

    outcome = asyncio.run(_adapter(client).close_all_positions())

    assert outcome.complete is False
    assert outcome.failed == ("AAPL:cancel:sl-leg:cancel rejected",)
    assert client.close_calls == []
    assert client.submitted == []


def test_a_held_leg_left_working_is_reported_as_still_working() -> None:
    """The re-read must see the leg: a pending_cancel bracket leg still rests.

    ``cancel_settles=False`` reproduces the window where the broker accepted the
    cancels and keeps listing the legs. The flatten reports them - fail closed and
    retried - instead of closing a position it did not disarm.
    """
    client = _BracketPositionClient(
        positions=[_FakePosition("AAPL", 10.0)], cancel_settles=False
    )

    outcome = asyncio.run(_adapter(client).close_all_positions())

    assert outcome.complete is False
    (problem,) = outcome.failed
    assert problem.startswith("AAPL:orders-still-working:")
    assert "sl-leg" in problem
    # One entry per resting order: the take profit is listed as an open order and as
    # a resting leg, and a double entry would hide which orders actually remain.
    assert problem.count("tp-leg") == 1
    assert client.close_calls == []
    # The leg still protects the position, so no second stop is placed on it.
    assert client.submitted == []


def test_a_symbol_less_leg_left_working_is_reported_as_still_working() -> None:
    """The re-read places the leg the same way the cancel list did.

    A pending_cancel leg that carries no symbol of its own is still on this
    position, still reserving its quantity. A re-read that filtered it out by the
    leg's own symbol would go on to close a position it did not disarm - and be
    refused by the broker for the quantity the leg reserves, after the position's
    own stop had already been cancelled.
    """
    client = _BracketPositionClient(
        positions=[_FakePosition("AAPL", 10.0)], cancel_settles=False
    )
    client.parent.legs[1].symbol = ""

    outcome = asyncio.run(_adapter(client).close_all_positions())

    assert outcome.complete is False
    (problem,) = outcome.failed
    assert problem.startswith("AAPL:orders-still-working:")
    assert "sl-leg" in problem
    assert client.close_calls == []


def test_a_refused_cancel_is_not_excused_for_an_order_that_filled() -> None:
    """The excusal is for an order that left *without* executing.

    Absent from the resting book is not by itself proof of a cancel: an order also
    leaves the book by executing, and the sell stop that fills on a flat position
    opens a short. Excusing that cancel would let the orphan sweep report a complete
    sweep over a position the broker just opened, so the id is resolved to its
    terminal state and only ``canceled``/``expired``/``rejected`` excuse it.
    """
    client = _BracketPositionClient(positions=[], oco_siblings=False)
    real_cancel = client.cancel_order_by_id

    def _refuse_a_cancel_that_filled(order_id: str) -> None:
        if order_id == "sl-leg":
            # The stop filled as the cancel was sent: the broker refuses the cancel
            # and keeps the order as filled.
            client.history["sl-leg"].status = "filled"
            raise RuntimeError("422: order is already filled")
        real_cancel(order_id)

    client.cancel_order_by_id = _refuse_a_cancel_that_filled  # type: ignore[method-assign]

    outcome = asyncio.run(_adapter(client).cancel_orphaned_protections())

    assert outcome.complete is False
    assert any("sl-leg" in item for item in outcome.failed)


def test_a_refused_cancel_is_not_excused_for_an_order_that_partly_filled() -> None:
    """A stop that executed cannot be excused by its terminal state.

    A sell stop that partly fills and is then cancelled reads back as ``canceled`` -
    with the quantity it filled. The terminal state alone would excuse the
    refusal, the sweep would report itself complete, and the daemon would stamp
    STOPPED enforced while that fill sits open - on a flat symbol it is a short.
    So the excusal also needs the order to have filled nothing.
    """
    client = _BracketPositionClient(positions=[], oco_siblings=False)
    real_cancel = client.cancel_order_by_id

    def _refuse_a_cancel_that_partly_filled(order_id: str) -> None:
        if order_id == "sl-leg":
            # The leg filled as the cancel was sent: the broker keeps the order,
            # cancelled, with the quantity it managed to execute.
            client.history["sl-leg"].status = "canceled"
            client.history["sl-leg"].filled_qty = 4.0
            raise RuntimeError("422: order is already filled")
        real_cancel(order_id)

    client.cancel_order_by_id = _refuse_a_cancel_that_partly_filled  # type: ignore[method-assign]

    outcome = asyncio.run(_adapter(client).cancel_orphaned_protections())

    assert outcome.complete is False
    assert any("sl-leg" in item for item in outcome.failed)


def test_a_lost_cancel_reply_still_counts_as_a_removed_protection() -> None:
    """The broker removed the order but the reply was lost: the re-read is the proof.

    The stop leg is this position's only order, so nothing else in the sweep makes
    the protection "removed": the excusal itself has to. Otherwise a refused close
    would report the position as protected and leave it naked.
    """
    client = _BracketPositionClient(
        positions=[_FakePosition("AAPL", 10.0)],
        take_profit_filled=True,
        oco_siblings=False,
        close_error=RuntimeError("broker refused to close"),
    )
    real_cancel = client.cancel_order_by_id

    def _lose_the_reply(order_id: str) -> None:
        real_cancel(order_id)
        raise RuntimeError("no reply to the cancel")

    client.cancel_order_by_id = _lose_the_reply  # type: ignore[method-assign]

    outcome = asyncio.run(_adapter(client).close_all_positions())

    assert outcome.complete is False
    assert outcome.failed == ("AAPL:close:broker refused to close",)
    assert not any("NO-STOP-RESTORED" in item for item in outcome.failed)
    restored = [order for order in client.orders if order.order_type == "stop"]
    assert [(order.stop_price, order.qty) for order in restored] == [(95.0, 10.0)]


def test_a_refused_cancel_is_settled_by_id_without_a_fresh_book() -> None:
    """The excusal is one read by id, not a book listing per refused cancel.

    A terminal state read by id already proves the order is not resting, so the
    excusal no longer re-lists the page-limited nested history for every refusal:
    on a mature account a sweep with two refusals would issue two 20-page nested
    listings, and a rate limit there fails the sweep closed. The refusal is settled
    by id instead - where this leg still reads as ``held``, so the refusal stands -
    and the flatten's failure report, the untouched close and the restore it refuses
    are all still there.
    """
    client = _BracketPositionClient(
        positions=[_FakePosition("AAPL", 10.0)], oco_siblings=False
    )
    real_cancel = client.cancel_order_by_id
    real_get = client.get_orders
    reads = {"all": 0}

    def _lose_the_book_on_the_second_all_listing(filter=None):  # noqa: A002
        status = str(getattr(getattr(filter, "status", None), "value", "open")).lower()
        if status == "all":
            reads["all"] += 1
            if reads["all"] > 1:
                raise RuntimeError("order book unreachable")
        return real_get(filter)

    client.get_orders = _lose_the_book_on_the_second_all_listing  # type: ignore[method-assign]

    def _refuse_the_stop_leg(order_id: str) -> None:
        if order_id == "sl-leg":
            raise RuntimeError("cancel rejected")
        real_cancel(order_id)

    client.cancel_order_by_id = _refuse_the_stop_leg  # type: ignore[method-assign]

    outcome = asyncio.run(_adapter(client).close_all_positions())

    assert outcome.complete is False
    # The restore was refused on a book nobody could read, and the reason says so: a
    # stop placed beside an order nobody could list reduces the position twice, and
    # the extra one is an orphan the moment the first fills.
    assert outcome.failed == ("AAPL:cancel:sl-leg:cancel rejected;NO-STOP-RESTORED",)
    assert client.close_calls == []
    # Two ``status=all`` listings: the flatten's own book and the restore's re-read.
    # The refusal cost none of its own - a test that only checked the outcome would
    # also pass if the excusal verified nothing at all.
    assert reads["all"] == 2
    # Nothing was submitted: no second stop next to one the broker may still hold.
    restored = [order for order in client.orders if order.order_type == "stop"]
    assert [(order.stop_price, order.qty) for order in restored] == []


def test_a_refused_cancel_is_not_excused_by_a_broker_that_cannot_find_the_order() -> None:
    """A 404 proves nothing: the refusal stands rather than being excused."""
    client = _BracketPositionClient(positions=[], oco_siblings=False)
    real_cancel = client.cancel_order_by_id

    def _refuse_the_stop_leg(order_id: str) -> None:
        if order_id == "sl-leg":
            raise RuntimeError("cancel rejected")
        real_cancel(order_id)

    client.cancel_order_by_id = _refuse_the_stop_leg  # type: ignore[method-assign]

    def _lose_the_order(order_id: str):
        raise RuntimeError(f"404: order {order_id} not found")

    client.get_order_by_id = _lose_the_order  # type: ignore[method-assign]

    outcome = asyncio.run(_adapter(client).cancel_orphaned_protections())

    assert outcome.complete is False
    assert any("sl-leg" in item for item in outcome.failed)


def test_a_refused_cancel_is_not_excused_when_the_broker_cannot_answer_by_id() -> None:
    """A read that raises proves nothing either: the refusal stands.

    The orphan sweep's excusal is a read by id, and a broker that answers it with
    an error - a 503, a dropped connection, a rate limit - has not shown that the
    order left the book. Excusing the cancel on it would report a clean sweep while
    the leg still rests, or, in the flatten, close a position whose stop still
    reserves its quantity.
    """
    client = _FakeTradingClient(
        orders=[_FakeOrder("entry-1", "AAPL", "buy")],
        positions=[_FakePosition("AAPL", 10.0)],
    )

    def _refuse(order_id: str) -> None:
        client.cancelled.append(order_id)
        raise RuntimeError("cancel rejected")

    client.cancel_order_by_id = _refuse  # type: ignore[method-assign]

    def _cannot_answer(order_id: str):
        raise RuntimeError("503: broker unavailable")

    client.get_order_by_id = _cannot_answer  # type: ignore[method-assign]
    adapter = _adapter(client)

    assert asyncio.run(adapter._cancel_already_gone(client, "entry-1")) is False

    outcome = asyncio.run(adapter.close_all_positions())

    assert outcome.complete is False
    assert outcome.failed == ("AAPL:cancel:entry-1:cancel rejected",)
    assert client.close_calls == []


def test_the_flatten_resolves_a_held_leg_symbol_from_its_parent() -> None:
    """A leg that carries no symbol is still this symbol's order to cancel.

    The flatten filters the working book by the order's own symbol, and a held
    stop leg can arrive without one. Read as "another symbol" it survives the
    cancel, its stop goes on reserving the position's quantity, and the close is
    refused for that reservation - a flatten that cannot finish. The parent the
    leg rests under names the symbol, exactly as the orphan sweep resolves it.
    """
    client = _BracketPositionClient(
        positions=[_FakePosition("AAPL", 10.0)], oco_siblings=False
    )
    # The broker reports the held stop leg without a symbol of its own.
    client.parent.legs[1].symbol = ""

    outcome = asyncio.run(_adapter(client).close_all_positions())

    assert outcome.complete is True
    assert outcome.failed == ()
    assert client.cancelled == ["tp-leg", "sl-leg"]
    assert client.close_calls == ["AAPL"]


def test_a_refused_close_restores_a_symbol_less_held_stop_leg() -> None:
    """The stop price must be read through the parent, not the leg's own symbol.

    The flatten's book places a symbol-less held leg on this symbol through the
    parent it rests under, but the leg is still a top-level entry whose own
    symbol is empty. A stop-price lookup that filters on the order's own symbol
    then finds nothing, and a refused close restores no stop at all - a naked
    position reported as merely failed.
    """
    client = _BracketPositionClient(
        positions=[_FakePosition("AAPL", 10.0)],
        oco_siblings=False,
        close_error=RuntimeError("broker refused to close"),
    )
    client.parent.legs[1].symbol = ""

    outcome = asyncio.run(_adapter(client).close_all_positions())

    assert outcome.failed == ("AAPL:close:broker refused to close",)
    assert not any("NO-STOP-RESTORED" in item for item in outcome.failed)
    restored = [order for order in client.orders if order.order_type == "stop"]
    assert [(order.stop_price, order.qty, order.side) for order in restored] == [
        (95.0, 10.0, "sell")
    ]


def test_a_symbol_less_held_stop_the_broker_keeps_is_not_duplicated() -> None:
    """A refused cancel leaves the leg working, and the restore must see it.

    The broker refused the stop leg's cancel, so it still rests - placed on this
    symbol through its parent, with no symbol of its own. Re-reading the book
    for the restore has to resolve that leg the same way, or the restore places
    a second stop beside it: two stops reduce the position twice, and the extra
    one is an orphan the moment the first fills.
    """
    client = _BracketPositionClient(
        positions=[_FakePosition("AAPL", 10.0)], oco_siblings=False
    )
    client.parent.legs[1].symbol = ""
    real_cancel = client.cancel_order_by_id

    def _refuse_the_stop_leg(order_id: str) -> None:
        if order_id == "sl-leg":
            raise RuntimeError("cancel rejected")
        real_cancel(order_id)

    client.cancel_order_by_id = _refuse_the_stop_leg  # type: ignore[method-assign]

    outcome = asyncio.run(_adapter(client).close_all_positions())

    assert outcome.failed == ("AAPL:cancel:sl-leg:cancel rejected",)
    assert client.close_calls == []
    # The leg the broker kept is the working stop: nothing may be submitted.
    assert client.submitted == []


def test_a_held_leg_with_no_readable_id_and_no_symbol_fails_the_flatten_closed() -> None:
    """A leg nobody can name is still this flatten's order to verify.

    The held-leg lookup was scoped to this symbol's parents, so a leg it
    returns belongs to this flatten even when neither the leg nor its parent
    yields a readable id or a symbol of its own. Dropping it would let the
    flatten close over a protection nobody verified; keeping it means the
    flatten refuses on the unreadable id instead.
    """
    client = _BracketPositionClient(
        positions=[_FakePosition("AAPL", 10.0)], oco_siblings=False
    )
    client.parent.legs[1].id = None
    client.parent.legs[1].symbol = ""

    outcome = asyncio.run(_adapter(client).close_all_positions())

    assert outcome.complete is False
    (problem,) = outcome.failed
    assert problem.startswith("AAPL:unreadable-order:")
    assert client.close_calls == []


def test_flatten_refuses_a_full_held_leg_page() -> None:
    """A history longer than the page budget cannot be vouched for."""
    client = _FullHeldLegPageClient(positions=[_FakePosition("AAPL", 10.0)])

    with pytest.raises(RuntimeError, match="refusing to treat the list as complete"):
        asyncio.run(_adapter(client).close_all_positions())


def test_the_orphan_sweep_verifies_the_cancels_it_sent() -> None:
    """An accepted cancel is not a removed leg, and the sweep has to check.

    Alpaca keeps a cancel it accepted listed as ``pending_cancel`` until it settles,
    and a leg that is still listed can still fill. The daemon gates the STOPPED stamp
    on this sweep being complete, so a sweep that counted the acknowledgement as done
    would let a deployment be stamped stopped over a leg that can open a short.
    """
    client = _BracketPositionClient(positions=[], cancel_settles=False)

    outcome = asyncio.run(_adapter(client).cancel_orphaned_protections())

    assert outcome.complete is False
    assert sorted(outcome.failed) == [
        "sl-leg:still-working",
        "tp-leg:still-working",
    ]
    assert outcome.completed == 2, "the broker did accept both cancels"
    # The held leg is only visible under its filled parent, so the verification
    # cannot be an open-orders re-read: it has to be the whole working book.
    verified_with = [
        request
        for request in client.order_filters
        if str(getattr(getattr(request, "status", None), "value", "open")).lower()
        == "all"
    ]
    assert len(verified_with) == 2
    assert verified_with[-1].nested is True


def test_the_orphan_sweep_reports_a_verification_it_could_not_make() -> None:
    """A book that cannot be read cannot vouch for the legs it removed."""
    client = _BracketPositionClient(positions=[])
    real_get = client.get_orders

    def _lose_the_book_on_the_second_all_listing(filter=None):  # noqa: A002
        status = str(getattr(getattr(filter, "status", None), "value", "open")).lower()
        if status == "all" and client.cancelled:
            raise RuntimeError("order book unreachable")
        return real_get(filter)

    client.get_orders = _lose_the_book_on_the_second_all_listing  # type: ignore[method-assign]

    outcome = asyncio.run(_adapter(client).cancel_orphaned_protections())

    assert outcome.complete is False
    assert any(item.startswith("verify:") for item in outcome.failed)


def test_a_held_leg_with_no_readable_id_is_reported_not_dropped() -> None:
    """A leg the adapter cannot address is reported, not silently dropped.

    The merge keeps an entry with an unreadable id on purpose: dropping it would
    hide the leg from every sweep that follows, and a leg nobody reports is a leg
    nobody removes. The orphan sweep then names it as unidentifiable instead of
    completing over it, and the flatten does not close a position the leg still
    reserves.
    """
    client = _BracketPositionClient(positions=[])
    client.parent.legs[1].id = ""  # the held stop leg, still nested under its parent

    outcome = asyncio.run(_adapter(client).cancel_orphaned_protections())

    assert outcome.complete is False
    assert any("order:''" in item for item in outcome.unidentified)

    client = _BracketPositionClient(positions=[_FakePosition("AAPL", 10.0)])
    client.parent.legs[1].id = ""

    outcome = asyncio.run(_adapter(client).close_all_positions())

    # The flatten reports the leg it cannot address instead of closing over it, and
    # it does not let the position go naked either: the leg still works.
    assert outcome.complete is False
    assert any("unreadable-order:''" in item for item in outcome.failed)
    assert client.close_calls == []
    assert not any("NO-STOP-RESTORED" in item for item in outcome.failed)


def test_the_orphan_sweep_places_a_leg_by_the_parent_it_rests_under() -> None:
    """A leg with no symbol of its own belongs to its parent's position.

    The guard that keeps this sweep off a live position reads the order's symbol.
    A leg that arrives without one would read as "no position" and be cancelled -
    disarming the very position the sweep promises not to touch.
    """
    live = _BracketPositionClient(positions=[_FakePosition("AAPL", 10.0)])
    live.parent.legs[1].symbol = ""
    flat = _BracketPositionClient(positions=[])
    flat.parent.legs[1].symbol = ""

    assert asyncio.run(_adapter(live).cancel_orphaned_protections()).complete is True
    # Both legs sit on the parent's symbol, so neither is touched.
    assert live.cancelled == []
    assert "sl-leg" not in live.cancelled

    orphaned = asyncio.run(_adapter(flat).cancel_orphaned_protections())

    assert orphaned.complete is True
    assert "sl-leg" in flat.cancelled


def test_the_orphan_sweep_scopes_its_verification_to_the_symbols_it_cancelled() -> None:
    """The verification is not a second account-wide history scan.

    The daemon runs this sweep every sync tick and retries while it is incomplete,
    so two 20-page account-wide scans per pass is what turns a long order history
    into a rate limit - and a rate limit fails the sweep closed. A leg is found
    under the filled parent of its own symbol, so those symbols are enough.
    """
    client = _BracketPositionClient(positions=[])

    outcome = asyncio.run(_adapter(client).cancel_orphaned_protections())

    assert outcome.complete is True
    verification = _status_all_requests(client)[-1]
    assert verification.symbols == ["AAPL"]

    client = _BracketPositionClient(positions=[])
    client.parent.symbol = ""
    client.parent.legs[1].symbol = ""
    client.orders = []

    outcome = asyncio.run(_adapter(client).cancel_orphaned_protections())

    assert outcome.complete is True
    # A symbol nobody could resolve has no bound, so the sweep verifies on the
    # whole account rather than assume one.
    assert _status_all_requests(client)[-1].symbols is None


def test_the_orphan_sweep_scopes_its_verification_after_two_cancels_on_one_symbol() -> None:
    """Two cancelled ids on one symbol are two cancels, not an account-wide re-read.

    ``oco_siblings=False`` models a broker that leaves the sibling working when the
    other leg is cancelled, so both legs of the one orphaned bracket are cancelled
    here: two ids, one symbol. Counting the ids instead of the symbols they sit on
    is what made the verification fall back to a second account-wide 20-page scan
    on every daemon tick, and a rate limit fails the sweep closed - the retry then
    pays the same price. The scope is bounded by the symbols the sweep touched, so
    it stays scoped however many of their cancels it made.
    """
    client = _BracketPositionClient(positions=[], oco_siblings=False)

    outcome = asyncio.run(_adapter(client).cancel_orphaned_protections())

    assert outcome.complete is True
    # Both legs are cancelled, both under the one symbol.
    assert client.cancelled == ["tp-leg", "sl-leg"]
    verification = _status_all_requests(client)[-1]
    assert verification.symbols == ["AAPL"]


def test_the_orphan_sweep_scopes_its_verification_over_two_brackets_on_one_symbol() -> None:
    """Two orphaned brackets on one symbol are two symbols to verify on, not twenty pages.

    The same id-vs-symbol miscount as the single bracket, from a second filled
    parent on the symbol: the sweep cancelled four ids, and still has to verify
    against the one symbol it cleared.
    """
    first = _BracketPositionClient(positions=[])
    second = _BracketPositionClient(positions=[])
    second.parent.id = "parent-2"
    second.parent.legs[0].id = "tp-leg-2"
    second.parent.legs[1].id = "sl-leg-2"
    second.history = {order.id: order for order in second.parent.legs} | {
        second.parent.id: second.parent
    }
    first.history.update(second.history)
    first.parents.append(second.parent)
    # The open listing shows the second bracket's take profit, as it does the first's.
    first.orders.append(second.parent.legs[0])

    outcome = asyncio.run(_adapter(first).cancel_orphaned_protections())

    assert outcome.complete is True
    assert sorted(first.cancelled) == ["sl-leg", "sl-leg-2", "tp-leg", "tp-leg-2"]
    verification = _status_all_requests(first)[-1]
    assert verification.symbols == ["AAPL"]


def test_cancel_orphaned_protections_cancels_a_held_stop_leg() -> None:
    """A stop leg that outlives its position must not survive a stop.

    ``status=open`` never lists it, and a triggered sell-stop with nothing to
    sell opens a short - so the sweep reads the leg from the filled parent and
    cancels the leg, not the (uncancelable) filled parent.
    """
    client = _BracketPositionClient(positions=[])

    outcome = asyncio.run(_adapter(client).cancel_orphaned_protections())

    assert outcome.complete is True
    assert outcome.failed == ()
    # The take profit first, then the held stop leg the broker's OCO already removed
    # with it - a refusal for an order no longer resting is not a failure.
    assert client.cancelled == ["tp-leg", "sl-leg"]
    assert "parent" not in client.cancelled
    held_leg_requests = _status_all_requests(client)
    assert held_leg_requests, "the held stop leg must come from a status=all listing"
    assert held_leg_requests[0].nested is True
    # Account-wide, and bounded: a bracket's take profit names the symbol a held stop
    # leg rests on, but an OTO entry - which the adapter submits for an unbounded take
    # profit - has a stop leg and no take-profit sibling to derive the symbol from.
    assert not held_leg_requests[0].symbols


def test_cancel_orphaned_protections_leaves_a_held_stop_leg_of_a_live_position() -> None:
    """The orphan sweep must not disarm a live bracket position.

    The held leg is invisible to ``status=open``, so a sweep that did not see it
    could not have skipped it: the position check has to cover it too.
    """
    client = _BracketPositionClient(positions=[_FakePosition("AAPL", 10.0)])

    outcome = asyncio.run(_adapter(client).cancel_orphaned_protections())

    assert outcome.complete is True
    assert client.cancelled == []


def test_cancel_orphaned_protections_refuses_a_full_held_leg_page() -> None:
    """A history longer than the page budget cannot be vouched for."""
    client = _FullHeldLegPageClient(
        positions=[], orders=[_FakeOrder("tp-open", "AAPL", "sell", order_type="limit")]
    )

    with pytest.raises(RuntimeError, match="refusing to treat the list as complete"):
        asyncio.run(_adapter(client).cancel_orphaned_protections())


def test_a_full_held_leg_page_pages_back_to_the_parent() -> None:
    """A full page is a mature account, not a truncated listing.

    The listing is newest-first, so the filled parent that owns a resting leg can sit
    behind a page of terminal orders - an account trading one bracket a day fills a
    500-row page within two years. The sweep pages back until a page comes back not
    full, and a full page must not be read as "no parent". The second page here returns
    the boundary row again, as a cursor past the oldest row produces, and the book must
    still hold each order once.
    """
    client = _BracketPositionClient(positions=[_FakePosition("AAPL", 10.0)])
    newest = datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc)
    page_one = [
        _FakeOrder(
            f"old-{index}",
            "AAPL",
            "buy",
            status="filled",
            submitted_at=newest - timedelta(seconds=index + 1),
        )
        for index in range(_ALPACA_HELD_LEG_LOOKUP_LIMIT)
    ]
    real_get = client.get_orders
    pages = {"requests": 0}

    def _two_pages(filter=None):  # noqa: A002 - mirrors alpaca-py
        status = str(getattr(getattr(filter, "status", None), "value", "open")).lower()
        if status == "open":
            return real_get(filter)
        client.order_filters.append(filter)
        pages["requests"] += 1
        if pages["requests"] > 1:
            # Second page: the end of the history, the filled bracket parent on it - and
            # the boundary row again, as a cursor past it produces.
            return [page_one[-1], client.parent]
        return page_one

    client.get_orders = _two_pages  # type: ignore[method-assign]

    stops = asyncio.run(_adapter(client).list_protective_stops())

    assert [stop.order_id for stop in stops] == ["sl-leg"]
    # It asked for a second page, and the cursor is past the oldest row of the first -
    # never short of it, where the parent we want could be the one order skipped.
    assert pages["requests"] == 2
    assert _status_all_requests(client)[-1].until == (
        page_one[-1].submitted_at + timedelta(microseconds=1)
    )

    outcome = asyncio.run(_adapter(client).close_all_positions())

    assert outcome.complete is True
    assert client.cancelled == ["tp-leg", "sl-leg"]


def test_exit_only_qty_is_capped_to_the_broker_position(monkeypatch) -> None:
    adapter = AlpacaLiveAdapter(api_key="key", secret_key="secret", paper=True)

    async def _positions():
        return [{"symbol": "AAPL", "qty": 5.0}]

    monkeypatch.setattr(adapter, "get_positions", _positions)
    signal = LiveSignal(
        deployment_id="11111111-1111-1111-1111-111111111111",
        symbol="AAPL",
        action="sell",
        theoretical_price=200.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"risk_sizing_mode": "exit_only"},
    )

    assert asyncio.run(adapter._require_exit_position(signal, 10.0)) == 5.0
    assert asyncio.run(adapter._require_exit_position(signal, 3.0)) == 3.0


def test_exit_only_order_without_a_position_is_refused(monkeypatch) -> None:
    adapter = AlpacaLiveAdapter(api_key="key", secret_key="secret", paper=True)

    async def _positions():
        return []

    monkeypatch.setattr(adapter, "get_positions", _positions)
    signal = LiveSignal(
        deployment_id="11111111-1111-1111-1111-111111111111",
        symbol="AAPL",
        action="sell",
        theoretical_price=200.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"risk_sizing_mode": "exit_only"},
    )

    with pytest.raises(ValueError, match="no live broker position"):
        asyncio.run(adapter._require_exit_position(signal, 1.0))


def test_intraday_lookback_covers_the_trading_time_requested() -> None:
    """5m/15m/30m windows must buy enough *session* time, not calendar time.

    A US session is 6.5 hours on 5 of 7 days. The old `limit * 2.2` minutes
    multiplier under-bought by ~5x, so the broker returned a short history that
    `tail()` silently shipped.
    """
    now = datetime.now(timezone.utc)
    session_fraction = (6.5 / 24.0) * (5.0 / 7.0)
    for timeframe, bar_minutes in (("5m", 5), ("15m", 15), ("30m", 30), ("1h", 60)):
        limit = 200
        start = _alpaca_market_data_start(timeframe, limit)
        calendar_minutes = (now - start).total_seconds() / 60.0
        trading_minutes = calendar_minutes * session_fraction
        assert trading_minutes >= limit * bar_minutes, timeframe


def test_short_history_widens_once_then_refuses(monkeypatch) -> None:
    import pandas as pd

    class _DataClient:
        def __init__(self) -> None:
            self.calls = 0
            self.starts: list[object] = []

        def get_stock_bars(self, request):
            self.calls += 1
            self.starts.append(request.start)
            rows = self.calls  # 1 row, then 2 rows: still short
            frame = pd.DataFrame(
                {
                    "open": [1.0] * rows,
                    "high": [1.0] * rows,
                    "low": [1.0] * rows,
                    "close": [1.0] * rows,
                },
                index=pd.DatetimeIndex(
                    [pd.Timestamp.now(tz="UTC")] * rows, name="timestamp"
                ),
            )
            return SimpleNamespace(df=frame)

    adapter = AlpacaLiveAdapter(api_key="key", secret_key="secret", paper=True)
    adapter._connected = True
    client = _DataClient()
    adapter._historical_data_client = client

    with pytest.raises(ValueError, match="shorter history"):
        asyncio.run(adapter.get_market_data("AAPL", timeframe="30m", limit=200))

    assert client.calls == 2
    # The retry widens the window rather than asking for the same one again.
    assert client.starts[1] < client.starts[0]


def test_bulk_response_shape_is_not_read_as_zero() -> None:
    """An unreadable order list must raise, never look like a clean sweep."""
    adapter = _adapter(SimpleNamespace(get_orders=lambda filter=None: object()))

    with pytest.raises(RuntimeError, match="unrecognised shape"):
        asyncio.run(adapter.cancel_open_orders())


def _position_row(**overrides):
    """A broker position row with every money field readable by default."""
    row = {
        "symbol": "AAPL",
        "qty": 1.0,
        "avg_entry_price": 100.0,
        "current_price": 101.0,
        "unrealized_pl": 1.0,
    }
    row.update(overrides)
    return SimpleNamespace(**row)


def test_positions_mark_an_unparseable_qty_instead_of_reading_it_as_flat() -> None:
    """A broker row whose qty cannot be read is flagged, not silently zeroed."""
    client = _FakeTradingClient(
        positions=[_position_row(qty="not-a-number")]
    )
    adapter = _adapter(client)

    positions = asyncio.run(adapter.get_positions())

    assert positions[0]["qty"] == 0.0
    assert positions[0]["qty_unparseable"] is True
    assert positions[0]["qty_raw"] == "not-a-number"


def test_flatten_refuses_to_treat_an_unparseable_qty_as_flat() -> None:
    """The STOPPED stamp is only safe if a malformed qty cannot pass as flat.

    If ``close_all_positions`` read an unreadable qty as zero it would report a
    successful flatten while real exposure stayed open, and the daemon would
    stamp the deployment stopped over a live position.
    """
    client = _FakeTradingClient(
        positions=[_position_row(qty="not-a-number")]
    )
    adapter = _adapter(client)

    with pytest.raises(ValueError, match="unparseable"):
        asyncio.run(adapter.close_all_positions())


def test_cancel_orphaned_protections_refuses_an_unparseable_qty() -> None:
    """The orphan sweep must not treat an unreadable position as flat.

    Reading the row as zero would make its protection look orphaned and cancel a
    stop that is guarding a live position.
    """
    client = _FakeTradingClient(
        positions=[_position_row(qty="nan-ish")],
        orders=[_FakeOrder(id="stop-1", symbol="AAPL", side="sell", order_type="stop")],
    )
    adapter = _adapter(client)

    with pytest.raises(ValueError, match="unparseable"):
        asyncio.run(adapter.cancel_orphaned_protections())


def test_is_flat_uses_a_tolerance_and_rejects_non_finite() -> None:
    """S1244: comparing a broker quantity to 0.0 with `==` is not a flat test."""
    from live_runner.alpaca_adapter import _is_flat

    assert _is_flat(0.0) is True
    assert _is_flat(-0.0) is True
    assert _is_flat(1e-13) is True
    assert _is_flat(-1e-13) is True
    # Far below any tradable unit but still a real quantity.
    assert _is_flat(1e-9) is False
    assert _is_flat(0.5) is False
    assert _is_flat(float("nan")) is False
    assert _is_flat(float("inf")) is False


def test_a_sub_unit_residue_position_is_treated_as_flat() -> None:
    """A residue must not keep a stopped deployment unflattened forever."""
    client = _FakeTradingClient(
        orders=[_FakeOrder("sl-1", "AAPL", "sell", order_type="stop", qty=1e-13)],
        positions=[_FakePosition("AAPL", 1e-13)],
    )

    outcome = asyncio.run(_adapter(client).close_all_positions())

    assert outcome.complete is True
    assert client.close_calls == []


def _entry_signal() -> LiveSignal:
    return LiveSignal(
        deployment_id="11111111-1111-1111-1111-111111111111",
        symbol="AAPL",
        action="buy",
        theoretical_price=200.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"stop_loss": 195.0, "take_profit": 210.0},
    )


def test_pre_submit_refusal_is_marked_as_not_submitted() -> None:
    adapter = _adapter(_FakeTradingClient())

    with pytest.raises(ValueError, match="finite and positive") as raised:
        asyncio.run(adapter.submit_order(_entry_signal(), qty=0.0))

    # No request could have left the process, so the caller may release its
    # submission lease instead of waiting out the TTL.
    assert is_order_not_submitted(raised.value) is True


def test_broker_write_failure_is_not_marked_as_not_submitted(
    isolated_db_sandbox, monkeypatch
) -> None:
    client = _FakeTradingClient(submit_error=RuntimeError("connection reset"))
    adapter = _adapter(client)
    monkeypatch.setattr(
        alpaca_module, "save_live_signal", lambda _signal: (str(uuid4()), True)
    )

    with pytest.raises(RuntimeError, match="connection reset") as raised:
        asyncio.run(adapter.submit_order(_entry_signal(), qty=1.0))

    # The broker may have accepted the order before the connection died, so the
    # caller must treat the outcome as unknown and keep its lease.
    assert is_order_not_submitted(raised.value) is False


# ─── #121 review: what an exit needs to know before it is sized ──────────────


def test_working_order_ids_lists_broker_and_client_order_ids() -> None:
    """A previous exit is matched by its broker id, or by its client order id
    (the signal id) when the broker id was never persisted."""
    client = _FakeTradingClient(
        orders=[
            SimpleNamespace(
                id="o-1", client_order_id="sig-1", symbol="AAPL", side="sell", legs=[]
            ),
            _FakeOrder("o-2", "MSFT", "buy"),
        ]
    )

    ids = asyncio.run(_adapter(client).working_order_ids())

    assert {"o-1", "sig-1", "o-2"} <= set(ids)


def test_alpaca_working_order_ids_include_nested_legs() -> None:
    """An exit working only under a bracket leg's id must still be seen."""
    legs = [
        SimpleNamespace(id="leg-1", client_order_id="leg-sig-1", legs=[]),
        SimpleNamespace(id="leg-2", client_order_id="leg-sig-2", legs=[]),
    ]
    client = _FakeTradingClient(
        orders=[
            SimpleNamespace(
                id="parent-1",
                client_order_id="parent-sig",
                symbol="AAPL",
                side="buy",
                legs=legs,
            )
        ]
    )

    ids = asyncio.run(_adapter(client).working_order_ids())

    assert {"parent-1", "parent-sig", "leg-1", "leg-sig-1", "leg-2", "leg-sig-2"} <= set(ids)


def test_alpaca_exit_qty_floor_absorbs_float_noise_but_never_rounds_up() -> None:
    adapter = _adapter(_FakeTradingClient(positions=[_FakePosition("AAPL", 3.0)]))

    noisy = 2.9999999999999996
    qty = asyncio.run(adapter._require_exit_position(_exit_signal(noisy), noisy))
    assert qty == 3.0
    over = asyncio.run(adapter._require_exit_position(_exit_signal(0.1999999996), 0.1999999996))
    assert over == pytest.approx(0.199999999, abs=1e-15)
    assert over <= 0.1999999996


def test_working_order_ids_sees_an_exit_beyond_the_default_page_of_50_orders() -> None:
    orders = [_FakeOrder(f"o-{index}", "MSFT", "buy") for index in range(120)]
    orders.append(
        SimpleNamespace(
            id="exit-1", client_order_id="sig-exit", symbol="AAPL", side="sell", legs=[]
        )
    )
    client = _FakeTradingClient(orders=orders)

    ids = asyncio.run(_adapter(client).working_order_ids())

    assert {"exit-1", "sig-exit"} <= set(ids)
    assert client.last_order_filter.nested is True
    assert client.last_order_filter.limit == 500


def test_working_order_ids_fails_when_the_order_page_is_full() -> None:
    client = _FakeTradingClient(
        orders=[_FakeOrder(f"o-{index}", "MSFT", "buy") for index in range(500)]
    )

    with pytest.raises(RuntimeError, match="page limit"):
        asyncio.run(_adapter(client).working_order_ids())


def test_working_order_ids_fails_closed_without_an_order_listing() -> None:
    client = SimpleNamespace()

    with pytest.raises(RuntimeError):
        asyncio.run(_adapter(client).working_order_ids())


def test_alpaca_position_qty_tolerance_is_below_one_fractional_share_step() -> None:
    """Alpaca quantities have up to 9 decimals: half a step, never a whole one."""
    tolerance = _adapter(_FakeTradingClient()).position_qty_tolerance("AAPL")

    assert 0.0 < tolerance < 1e-9


# ─── #121 review concern 6: exit quantities round down to 9 decimals ─────────


def _exit_signal(qty: float) -> LiveSignal:
    return LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="sell",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"exit_only": True, "risk_sizing_mode": "exit_only", "qty": qty},
    )


def test_alpaca_exit_qty_rounds_down_to_nine_decimals() -> None:
    """1/3 of a 0.5-share position: 0.1666... closes 0.166666666, never ...667."""
    adapter = _adapter(_FakeTradingClient(positions=[_FakePosition("AAPL", 0.5)]))

    qty = asyncio.run(adapter._require_exit_position(_exit_signal(0.5 / 3), 0.5 / 3))

    assert qty == pytest.approx(0.166666666, abs=1e-15)
    assert qty <= 0.5 / 3


def test_alpaca_exit_qty_below_one_step_is_refused_as_below_step() -> None:
    from live_runner.exit_signals import ExitBelowStepError

    adapter = _adapter(_FakeTradingClient(positions=[_FakePosition("AAPL", 0.5)]))

    with pytest.raises(ExitBelowStepError) as excinfo:
        asyncio.run(adapter._require_exit_position(_exit_signal(4e-10), 4e-10))
    assert excinfo.value.requested_qty == pytest.approx(4e-10)
    assert excinfo.value.qty_step == pytest.approx(1e-9)


def test_alpaca_exit_qty_step_is_one_fractional_share_step() -> None:
    adapter = _adapter(_FakeTradingClient())
    assert adapter.exit_qty_step("AAPL") == pytest.approx(1e-9)


# ─── #145: exits on bracketed positions must free the reserved shares ────────


def _filled_bracket(symbol: str = "AAPL", qty: float = 10.0) -> list[_FakeOrder]:
    """A filled bracket as Alpaca paper showed it (runbook 2026-11-16).

    The take-profit leg is a working top-level order; the stop leg stays
    ``held`` under the filled parent and is invisible to ``status=open``.
    """
    stop_leg = _FakeOrder(
        id="sl-1",
        symbol=symbol,
        side="sell",
        order_type="stop",
        qty=qty,
        stop_price=190.0,
        status="held",
    )
    parent = _FakeOrder(
        id="parent-1",
        symbol=symbol,
        side="buy",
        order_type="market",
        order_class="bracket",
        qty=qty,
        status="filled",
        legs=[stop_leg],
    )
    take_profit = _FakeOrder(
        id="tp-1",
        symbol=symbol,
        side="sell",
        order_type="limit",
        qty=qty,
        limit_price=210.0,
        status="new",
    )
    return [parent, take_profit]


def _patched_signal_store(monkeypatch, captured: list[LiveSignal]) -> None:
    def _save(signal: LiveSignal):
        captured.append(signal)
        return str(signal.id or uuid4()), True

    monkeypatch.setattr(alpaca_module, "save_live_signal", _save)
    monkeypatch.setattr(
        alpaca_module, "set_live_signal_broker_order_id", lambda *_a, **_k: True
    )


def _exit_signal_full() -> LiveSignal:
    return LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="sell",
        theoretical_price=200.0,
        timestamp=datetime.now(timezone.utc),
        metadata={
            "exit_only": True,
            "risk_sizing_mode": "exit_only",
            "signal_intent": "exit",
            "exit_fraction": 1.0,
            "qty": 10.0,
            "explicit_qty": 10.0,
        },
    )


def test_full_exit_on_bracketed_position_cancels_legs_and_closes(
    monkeypatch,
) -> None:
    """The bracket legs reserve the shares; the exit must cancel them first."""
    client = _FakeTradingClient(
        orders=_filled_bracket(),
        positions=[_FakePosition("AAPL", 10.0)],
    )
    adapter = _adapter(client)
    captured: list[LiveSignal] = []
    _patched_signal_store(monkeypatch, captured)

    broker_order_id = asyncio.run(adapter.submit_order(_exit_signal_full(), qty=10.0))

    assert broker_order_id
    # Both legs went before the close; the position is flat afterwards.
    assert set(client.cancelled) == {"tp-1", "sl-1"}
    assert client.positions == []
    market_orders = [
        order for order in client.submitted if _type_text(order) == "market"
    ]
    assert len(market_orders) == 1
    # A full exit leaves nothing to re-protect.
    assert not [
        order for order in client.submitted if _class_text(order) == "oco"
    ]


def test_full_exit_over_a_stop_limit_protection_is_not_refused(
    monkeypatch,
) -> None:
    """A stop-limit leg's limit price belongs to the plan's signature too.

    The flat-remainder branch used to leave ``stop_limit_price`` unset while
    the in-lock signature always derived it from the candidates, so every
    full exit over a stop-limit protection was refused as "the working
    protection changed" before any cancel.
    """
    stop_leg = _FakeOrder(
        id="sl-1",
        symbol="AAPL",
        side="sell",
        order_type="stop_limit",
        qty=10.0,
        stop_price=190.0,
        limit_price=189.0,
        status="held",
    )
    parent = _FakeOrder(
        id="parent-1",
        symbol="AAPL",
        side="buy",
        order_type="market",
        order_class="bracket",
        qty=10.0,
        status="filled",
        legs=[stop_leg],
    )
    take_profit = _FakeOrder(
        id="tp-1",
        symbol="AAPL",
        side="sell",
        order_type="limit",
        qty=10.0,
        limit_price=210.0,
        status="new",
    )
    client = _FakeTradingClient(
        orders=[parent, take_profit],
        positions=[_FakePosition("AAPL", 10.0)],
    )
    adapter = _adapter(client)
    _patched_signal_store(monkeypatch, [])

    broker_order_id = asyncio.run(adapter.submit_order(_exit_signal_full(), qty=10.0))

    assert broker_order_id
    assert client.positions == []
    market_orders = [
        order for order in client.submitted if _type_text(order) == "market"
    ]
    assert len(market_orders) == 1


def test_partial_exit_reprotects_remainder_at_original_levels(
    monkeypatch,
) -> None:
    """Partial exit: remaining qty gets SL/TP again at the legs' own levels."""
    client = _FakeTradingClient(
        orders=_filled_bracket(),
        positions=[_FakePosition("AAPL", 10.0)],
    )
    adapter = _adapter(client)
    captured: list[LiveSignal] = []
    _patched_signal_store(monkeypatch, captured)
    cleared: list[str] = []
    monkeypatch.setattr(
        alpaca_module,
        "clear_live_signal_metadata_key",
        lambda signal_id, key: cleared.append(signal_id) or True,
    )

    signal = _exit_signal_full().model_copy(
        update={"metadata": {**_exit_signal_full().metadata, "qty": 4.0, "explicit_qty": 4.0}}
    )
    asyncio.run(adapter.submit_order(signal, qty=4.0))

    assert set(client.cancelled) == {"tp-1", "sl-1"}
    # Position went 10 -> 6; the OCO re-protection covers exactly the remainder.
    assert client.positions == [_FakePosition("AAPL", 6.0)] or client.positions[0].qty == 6.0
    oco = [order for order in client.submitted if _class_text(order) == "oco"]
    assert len(oco) == 1
    assert float(oco[0].qty) == 6.0
    assert float(oco[0].take_profit.limit_price) == 210.0
    assert float(oco[0].stop_loss.stop_price) == 190.0
    assert _side_text(oco[0]) == "sell"
    # The persisted signal carried the recovery marker. The market exit is
    # already proven terminal and fully filled when the re-place returns, so
    # the watchdog marker is spent immediately rather than left for the next
    # connect or stream update (#350 review).
    assert captured and "exit_reprotection" in (captured[0].metadata or {})
    assert len(cleared) == 1


def test_partial_exit_reprotects_at_current_trailing_level(monkeypatch) -> None:
    """Trailing amended the broker stop: the remainder keeps THAT level (#145)."""
    orders = _filled_bracket()
    # Trailing moved the held stop leg from the entry's 190 to 195.
    orders[0].legs[0].stop_price = 195.0
    client = _FakeTradingClient(
        orders=orders,
        positions=[_FakePosition("AAPL", 10.0)],
    )
    adapter = _adapter(client)
    captured: list[LiveSignal] = []
    _patched_signal_store(monkeypatch, captured)

    signal = _exit_signal_full().model_copy(
        update={"metadata": {**_exit_signal_full().metadata, "qty": 4.0, "explicit_qty": 4.0}}
    )
    asyncio.run(adapter.submit_order(signal, qty=4.0))

    oco = [order for order in client.submitted if _class_text(order) == "oco"]
    assert len(oco) == 1
    assert float(oco[0].stop_loss.stop_price) == 195.0
    assert float(oco[0].take_profit.limit_price) == 210.0


def test_second_partial_exit_releases_the_oco_pair(monkeypatch) -> None:
    """An exit over a position protected by a re-placed OCO releases it too.

    Alpaca's real OCO shape: the parent IS the take-profit (a limit order
    carrying ``limit_price``); the stop is its only leg. Both levels must be
    found - the parent's take-profit price is what the remainder's new OCO
    re-places, per the #145 decision.
    """
    client = _FakeTradingClient(
        orders=[
            _FakeOrder(
                id=str(uuid4()),
                symbol="AAPL",
                side="sell",
                order_type="limit",
                order_class="oco",
                qty=6.0,
                limit_price=210.0,
                status="new",
                legs=[
                    _FakeOrder(
                        id=str(uuid4()),
                        symbol="AAPL",
                        side="sell",
                        order_type="stop",
                        qty=6.0,
                        stop_price=190.0,
                        status="new",
                    ),
                ],
            )
        ],
        positions=[_FakePosition("AAPL", 6.0)],
    )
    adapter = _adapter(client)
    captured: list[LiveSignal] = []
    _patched_signal_store(monkeypatch, captured)

    signal = _exit_signal_full().model_copy(
        update={"metadata": {**_exit_signal_full().metadata, "qty": 2.0, "explicit_qty": 2.0}}
    )
    asyncio.run(adapter.submit_order(signal, qty=2.0))

    # The OCO parent and its stop leg were cancelled; the remainder is
    # re-protected with BOTH recorded levels.
    oco = [order for order in client.submitted if _class_text(order) == "oco"]
    assert len(oco) == 1
    assert float(oco[0].qty) == 4.0
    assert float(oco[0].take_profit.limit_price) == 210.0
    assert float(oco[0].stop_loss.stop_price) == 190.0
    assert client.positions[0].qty == 4.0


def test_partial_exit_refuses_ambiguous_protection_levels(monkeypatch) -> None:
    """Two different stop prices: which tranche the exit hits is unknowable."""
    orders = _filled_bracket()
    second_stop = _FakeOrder(
        id="sl-2",
        symbol="AAPL",
        side="sell",
        order_type="stop",
        qty=5.0,
        stop_price=185.0,
        status="new",
    )
    orders.append(second_stop)
    client = _FakeTradingClient(
        orders=orders,
        positions=[_FakePosition("AAPL", 10.0)],
    )
    adapter = _adapter(client)
    captured: list[LiveSignal] = []
    _patched_signal_store(monkeypatch, captured)

    signal = _exit_signal_full().model_copy(
        update={"metadata": {**_exit_signal_full().metadata, "qty": 4.0, "explicit_qty": 4.0}}
    )
    with pytest.raises(ValueError, match="ambiguous") as raised:
        asyncio.run(adapter.submit_order(signal, qty=4.0))

    # Refused before any broker write: nothing cancelled, nothing submitted,
    # and the failure is marked so the caller may release its submission lease.
    assert client.cancelled == []
    assert client.submitted == []
    assert is_order_not_submitted(raised.value) is True


def test_failed_exit_submit_restores_protection(monkeypatch) -> None:
    """Cancel succeeded but the exit order failed: put the protection back."""
    client = _FakeTradingClient(
        orders=_filled_bracket(),
        positions=[_FakePosition("AAPL", 10.0)],
    )
    adapter = _adapter(client)
    captured: list[LiveSignal] = []
    _patched_signal_store(monkeypatch, captured)
    real_submit = client.submit_order

    def _fail_exit(order_data):
        if _type_text(order_data) == "market":
            raise RuntimeError("broker rejected the exit")
        return real_submit(order_data)

    client.submit_order = _fail_exit  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="broker rejected the exit"):
        asyncio.run(adapter.submit_order(_exit_signal_full(), qty=10.0))

    # The protection the strategy had is back on the still-open position
    # (SL+TP go back as one OCO pair, so the stop lives on its leg).
    flat_orders = list(client.orders)
    for order in client.orders:
        flat_orders.extend(order.legs)
    stops = [
        order
        for order in flat_orders
        if order.order_type == "stop" and _status_text(order) not in _TERMINAL_ORDER_STATUSES
    ]
    assert len(stops) == 1
    assert stops[0].stop_price == 190.0
    assert stops[0].qty == 10.0


def test_failed_reprotection_is_critical_and_marker_survives(
    monkeypatch, caplog
) -> None:
    """Re-place fails after the exit filled: CRITICAL alert, marker kept."""
    client = _FakeTradingClient(
        orders=_filled_bracket(),
        positions=[_FakePosition("AAPL", 10.0)],
    )
    adapter = _adapter(client)
    captured: list[LiveSignal] = []
    _patched_signal_store(monkeypatch, captured)
    cleared: list[str] = []
    monkeypatch.setattr(
        alpaca_module,
        "clear_live_signal_metadata_key",
        lambda signal_id, key: cleared.append(signal_id) or True,
    )
    real_submit = client.submit_order

    def _fail_oco(order_data):
        if _class_text(order_data) == "oco":
            raise RuntimeError("oco rejected")
        return real_submit(order_data)

    client.submit_order = _fail_oco  # type: ignore[method-assign]

    signal = _exit_signal_full().model_copy(
        update={"metadata": {**_exit_signal_full().metadata, "qty": 4.0, "explicit_qty": 4.0}}
    )
    with caplog.at_level("CRITICAL"):
        asyncio.run(adapter.submit_order(signal, qty=4.0))

    # The exit itself went through; the remainder is unprotected and alerted.
    assert client.positions[0].qty == 6.0
    assert any("unprotected" in record.message.lower() or "re-protect" in record.message.lower()
               for record in caplog.records if record.levelname == "CRITICAL")
    assert cleared == []


def test_exit_without_protection_is_unchanged(monkeypatch) -> None:
    """A naked position exits exactly as before: no cancels, plain market order."""
    client = _FakeTradingClient(positions=[_FakePosition("AAPL", 10.0)])
    adapter = _adapter(client)
    captured: list[LiveSignal] = []
    _patched_signal_store(monkeypatch, captured)

    asyncio.run(adapter.submit_order(_exit_signal_full(), qty=10.0))

    assert client.cancelled == []
    assert client.positions == []
    assert not [order for order in client.submitted if _class_text(order) == "oco"]


def test_recovery_replaces_missing_protection_for_marked_exit(
    monkeypatch,
) -> None:
    """After a crash mid-exit, connect() re-places what the marker recorded."""
    client = _FakeTradingClient(positions=[_FakePosition("AAPL", 6.0)])
    adapter = _adapter(client)
    monkeypatch.setattr(
        alpaca_module,
        "list_pending_exit_reprotections",
        lambda account_id, *, limit=50: [
            {
                "signal_id": "sig-1",
                "symbol": "AAPL",
                "deployment_id": "dep-1",
                "timestamp": datetime.now(timezone.utc),
                "marker": {
                    "side": "sell",
                    "stop_price": 190.0,
                    "take_profit_price": 210.0,
                    "position_qty": 10.0,
                    "exit_qty": 4.0,
                    "reprotect_qty": 6.0,
                },
            }
        ],
    )
    cleared: list[str] = []
    monkeypatch.setattr(
        alpaca_module,
        "clear_live_signal_metadata_key",
        lambda signal_id, key: cleared.append(signal_id) or True,
    )

    asyncio.run(adapter._recover_exit_reprotections())

    oco = [order for order in client.submitted if _class_text(order) == "oco"]
    assert len(oco) == 1
    assert float(oco[0].qty) == 6.0
    assert float(oco[0].stop_loss.stop_price) == 190.0
    assert float(oco[0].take_profit.limit_price) == 210.0
    assert cleared == ["sig-1"]


def test_recovery_leaves_existing_protection_alone(monkeypatch) -> None:
    """A stop already working (e.g. trailing moved it) is never moved back."""
    orders = [
        _FakeOrder(
            id="sl-new",
            symbol="AAPL",
            side="sell",
            order_type="stop",
            qty=6.0,
            stop_price=195.0,
            status="new",
        )
    ]
    client = _FakeTradingClient(
        orders=orders, positions=[_FakePosition("AAPL", 6.0)]
    )
    adapter = _adapter(client)
    monkeypatch.setattr(
        alpaca_module,
        "list_pending_exit_reprotections",
        lambda account_id, *, limit=50: [
            {
                "signal_id": "sig-1",
                "symbol": "AAPL",
                "deployment_id": "dep-1",
                "timestamp": datetime.now(timezone.utc),
                "marker": {
                    "side": "sell",
                    "stop_price": 190.0,
                    "take_profit_price": 210.0,
                    "position_qty": 10.0,
                    "exit_qty": 4.0,
                    "reprotect_qty": 6.0,
                },
            }
        ],
    )
    cleared: list[str] = []
    monkeypatch.setattr(
        alpaca_module,
        "clear_live_signal_metadata_key",
        lambda signal_id, key: cleared.append(signal_id) or True,
    )

    asyncio.run(adapter._recover_exit_reprotections())

    # Stop exists -> only the missing TP piece is reported, nothing resubmitted
    # at the stale marker price, and the marker stays for the operator.
    assert client.submitted == []
    assert cleared == []


def test_list_protective_stops_sees_an_oco_stop_leg() -> None:
    """Re-protected positions stay trailable: the OCO stop leg is a stop."""
    client = _FakeTradingClient(
        orders=[
            _FakeOrder(
                id="oco-1",
                symbol="AAPL",
                side="sell",
                order_type="limit",
                order_class="oco",
                qty=6.0,
                limit_price=210.0,
                status="new",
                legs=[
                    _FakeOrder(
                        id="oco-1-sl",
                        symbol="AAPL",
                        side="sell",
                        order_type="stop",
                        qty=6.0,
                        stop_price=190.0,
                        status="held",
                    )
                ],
            )
        ],
        positions=[_FakePosition("AAPL", 6.0)],
    )
    adapter = _adapter(client)

    stops = asyncio.run(adapter.list_protective_stops())

    assert len(stops) == 1
    assert stops[0].order_id == "oco-1-sl"
    assert stops[0].stop_price == 190.0
    assert stops[0].side == "sell"


def test_partial_exit_submits_beyond_a_full_order_history_page(
    monkeypatch,
) -> None:
    """500 newer orders on the symbol must not blind the exit (#350 blocker).

    ``status=all`` is paged backwards with ``until``: the filled bracket
    parent owning the held stop leg sits on the second page and is still
    found, where the old single-page read raised and refused every exit on a
    long-lived symbol.
    """
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    history = [
        _FakeOrder(
            id=f"hist-{index}",
            symbol="AAPL",
            side="buy",
            order_type="limit",
            status="canceled",
            qty=1.0,
            submitted_at=base + timedelta(seconds=600 - index),
        )
        for index in range(500)
    ]
    bracket = _filled_bracket()
    for order in bracket:
        order.submitted_at = base
        for leg in order.legs:
            leg.submitted_at = base
    client = _FakeTradingClient(
        orders=history + bracket,
        positions=[_FakePosition("AAPL", 10.0)],
    )
    adapter = _adapter(client)
    captured: list[LiveSignal] = []
    _patched_signal_store(monkeypatch, captured)

    asyncio.run(adapter.submit_order(_partial_signal(4.0), qty=4.0))

    market_orders = [
        order for order in client.submitted if _type_text(order) == "market"
    ]
    assert len(market_orders) == 1
    oco = [order for order in client.submitted if _class_text(order) == "oco"]
    assert len(oco) == 1
    assert float(oco[0].qty) == 6.0


def _reprotect_row(marker: dict, **overrides) -> dict:
    row = {
        "signal_id": "sig-1",
        "symbol": "AAPL",
        "deployment_id": "dep-1",
        "timestamp": datetime.now(timezone.utc),
        "broker_order_id": "",
        "marker": marker,
    }
    row.update(overrides)
    return row


def _working_oco(qty: float = 6.0) -> _FakeOrder:
    return _FakeOrder(
        id=str(uuid4()),
        symbol="AAPL",
        side="sell",
        order_type="limit",
        order_class="oco",
        qty=qty,
        limit_price=210.0,
        status="new",
        legs=[
            _FakeOrder(
                id=str(uuid4()),
                symbol="AAPL",
                side="sell",
                order_type="stop",
                qty=qty,
                stop_price=190.0,
                status="new",
            )
        ],
    )


def _patch_pending_reprotections(monkeypatch, rows: list[dict]) -> None:
    monkeypatch.setattr(
        alpaca_module,
        "list_pending_exit_reprotections",
        lambda account_id, *, limit=50: rows,
    )


def _patch_marker_clears(monkeypatch) -> list[str]:
    cleared: list[str] = []
    monkeypatch.setattr(
        alpaca_module,
        "clear_live_signal_metadata_key",
        lambda signal_id, key: cleared.append(signal_id) or True,
    )
    return cleared


def test_recovery_tops_up_shares_an_underfilled_exit_left_naked(
    monkeypatch,
) -> None:
    """Exit expired after re-place: 6 of 10 shares protected, 4 naked.

    The persisted marker used to be cleared the moment re-protection was
    placed, so an exit that later expired or filled short left the uncovered
    shares unprotected with no record. The marker now survives until replay
    proves the exit order terminal; the uncovered quantity is topped up at
    the recorded levels (#350 review).
    """
    client = _FakeTradingClient(
        orders=[
            _working_oco(qty=6.0),
            _FakeOrder(
                id="exit-1",
                symbol="AAPL",
                side="sell",
                order_type="market",
                qty=4.0,
                status="expired",
            ),
        ],
        positions=[_FakePosition("AAPL", 10.0)],
    )
    adapter = _adapter(client)
    _patch_pending_reprotections(
        monkeypatch,
        [
            _reprotect_row(
                {
                    "side": "sell",
                    "stop_price": 190.0,
                    "take_profit_price": 210.0,
                    "entry_signal_id": "entry-1",
                },
                broker_order_id="exit-1",
            )
        ],
    )
    cleared = _patch_marker_clears(monkeypatch)

    asyncio.run(adapter._recover_exit_reprotections())

    top_ups = [
        order
        for order in client.submitted
        if _class_text(order) == "oco" and float(order.qty) == 4.0
    ]
    assert len(top_ups) == 1
    assert float(top_ups[0].stop_loss.stop_price) == 190.0
    assert float(top_ups[0].take_profit.limit_price) == 210.0
    assert cleared == ["sig-1"]


def test_recovery_keeps_marker_while_the_exit_order_still_works(
    monkeypatch,
) -> None:
    """A resting exit is not terminal: nothing is topped up, marker stays."""
    client = _FakeTradingClient(
        orders=[
            _working_oco(qty=6.0),
            _FakeOrder(
                id="exit-1",
                symbol="AAPL",
                side="sell",
                order_type="limit",
                qty=4.0,
                status="new",
            ),
        ],
        positions=[_FakePosition("AAPL", 10.0)],
    )
    adapter = _adapter(client)
    _patch_pending_reprotections(
        monkeypatch,
        [
            _reprotect_row(
                {
                    "side": "sell",
                    "stop_price": 190.0,
                    "take_profit_price": 210.0,
                },
                broker_order_id="exit-1",
            )
        ],
    )
    cleared = _patch_marker_clears(monkeypatch)

    asyncio.run(adapter._recover_exit_reprotections())

    assert client.submitted == []
    assert cleared == []


def test_recovery_alerts_but_keeps_marker_when_exit_outcome_unknown(
    monkeypatch, caplog
) -> None:
    """No persisted order id: cannot prove the exit finished - alert, keep."""
    client = _FakeTradingClient(
        orders=[_working_oco(qty=6.0)],
        positions=[_FakePosition("AAPL", 10.0)],
    )
    adapter = _adapter(client)
    _patch_pending_reprotections(
        monkeypatch,
        [
            _reprotect_row(
                {
                    "side": "sell",
                    "stop_price": 190.0,
                    "take_profit_price": 210.0,
                }
            )
        ],
    )
    cleared = _patch_marker_clears(monkeypatch)

    with caplog.at_level("ERROR"):
        asyncio.run(adapter._recover_exit_reprotections())

    assert client.submitted == []
    assert cleared == []
    assert any("unprotected" in record.message for record in caplog.records)


# ─── #350 review: whole-share exits, pinned guards, attribution ─────────────


def _partial_signal(qty: float) -> LiveSignal:
    base = _exit_signal_full()
    return base.model_copy(
        update={
            "metadata": {
                **base.metadata,
                "qty": qty,
                "explicit_qty": qty,
            }
        }
    )


def test_partial_exit_floors_qty_so_the_remainder_stays_whole(monkeypatch) -> None:
    """3 shares, exit 1.5: the OCO/GTC re-place rejects fractions, so the exit
    floors to 1 and the remainder 2 is re-protected as whole shares."""
    client = _FakeTradingClient(
        orders=_filled_bracket(qty=3.0),
        positions=[_FakePosition("AAPL", 3.0)],
    )
    adapter = _adapter(client)
    captured: list[LiveSignal] = []
    _patched_signal_store(monkeypatch, captured)

    asyncio.run(adapter.submit_order(_partial_signal(1.5), qty=1.5))

    market_orders = [
        order for order in client.submitted if _type_text(order) == "market"
    ]
    assert len(market_orders) == 1
    assert float(market_orders[0].qty) == 1.0
    oco = [order for order in client.submitted if _class_text(order) == "oco"]
    assert len(oco) == 1
    assert float(oco[0].qty) == 2.0
    # The persisted signal records the floored qty that was actually sent.
    assert captured[0].metadata["explicit_qty"] == 1.0
    marker = captured[0].metadata["exit_reprotection"]
    assert marker["exit_qty"] == 1.0
    assert marker["reprotect_qty"] == 2.0


def test_partial_exit_refuses_when_flooring_leaves_less_than_one_share(
    monkeypatch,
) -> None:
    """A sub-share requested exit cannot floor to a whole share: refuse before
    any broker write rather than leave an unprotectable remainder."""
    client = _FakeTradingClient(
        orders=_filled_bracket(qty=3.0),
        positions=[_FakePosition("AAPL", 3.0)],
    )
    adapter = _adapter(client)
    captured: list[LiveSignal] = []
    _patched_signal_store(monkeypatch, captured)

    with pytest.raises(ValueError, match="floors"):
        asyncio.run(adapter.submit_order(_partial_signal(0.5), qty=0.5))

    assert client.cancelled == []
    assert client.submitted == []


def test_partial_exit_refuses_a_fractional_broker_position(monkeypatch) -> None:
    """No whole-share exit leaves a whole remainder of a fractional position:
    refuse before any broker write."""
    client = _FakeTradingClient(
        orders=_filled_bracket(qty=3.5),
        positions=[_FakePosition("AAPL", 3.5)],
    )
    adapter = _adapter(client)
    captured: list[LiveSignal] = []
    _patched_signal_store(monkeypatch, captured)

    with pytest.raises(ValueError, match="fractional"):
        asyncio.run(adapter.submit_order(_partial_signal(1.0), qty=1.0))

    assert client.cancelled == []
    assert client.submitted == []


def test_position_change_during_release_refuses_and_restores(monkeypatch) -> None:
    """The position moved between planning and release: refuse the exit and
    put the recorded protection back on the position that remains."""
    client = _FakeTradingClient(
        orders=_filled_bracket(),
        positions=[_FakePosition("AAPL", 10.0)],
    )
    adapter = _adapter(client)
    captured: list[LiveSignal] = []
    _patched_signal_store(monkeypatch, captured)
    calls = {"n": 0}

    async def _qty(symbol: str) -> float | None:
        calls["n"] += 1
        # First read plans against 10; every later read sees 9.
        return 10.0 if calls["n"] == 1 else 9.0

    monkeypatch.setattr(adapter, "_symbol_position_qty", _qty)

    with pytest.raises(RuntimeError, match="position changed"):
        asyncio.run(adapter.submit_order(_exit_signal_full(), qty=10.0))

    # The exit never went out; the restore re-protected the 9 that remain.
    assert not [
        order for order in client.submitted if _type_text(order) == "market"
    ]
    oco = [order for order in client.submitted if _class_text(order) == "oco"]
    assert len(oco) == 1
    assert float(oco[0].qty) == 9.0


def test_still_working_protection_after_cancel_refuses_the_exit(monkeypatch) -> None:
    """A cancel Alpaca accepted but has not settled still reserves the shares:
    the re-read must refuse the exit instead of submitting into the refusal."""
    client = _FakeTradingClient(
        orders=_filled_bracket(),
        positions=[_FakePosition("AAPL", 10.0)],
        cancel_settles=False,
    )
    adapter = _adapter(client)
    captured: list[LiveSignal] = []
    _patched_signal_store(monkeypatch, captured)

    with pytest.raises(RuntimeError, match="still"):
        asyncio.run(adapter.submit_order(_exit_signal_full(), qty=10.0))

    assert not [
        order for order in client.submitted if _type_text(order) == "market"
    ]
    # The protection was put back for the still-whole position.
    oco = [order for order in client.submitted if _class_text(order) == "oco"]
    assert len(oco) == 1
    assert float(oco[0].qty) == 10.0


def test_exit_order_rejected_on_submission_restores_protection(monkeypatch) -> None:
    """The broker answering 'rejected' at submit means the exit never happened:
    the cancelled protection must be re-placed."""
    client = _FakeTradingClient(
        orders=_filled_bracket(),
        positions=[_FakePosition("AAPL", 10.0)],
    )
    adapter = _adapter(client)
    captured: list[LiveSignal] = []
    _patched_signal_store(monkeypatch, captured)
    real_submit = client.submit_order

    def _reject_exit(order_data):
        if _type_text(order_data) == "market":
            return SimpleNamespace(
                id=str(uuid4()), symbol="AAPL", status="rejected"
            )
        return real_submit(order_data)

    client.submit_order = _reject_exit  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="rejected on submission"):
        asyncio.run(adapter.submit_order(_exit_signal_full(), qty=10.0))

    oco = [order for order in client.submitted if _class_text(order) == "oco"]
    assert len(oco) == 1
    assert float(oco[0].qty) == 10.0


def test_changed_protection_between_plan_and_release_refuses_before_cancel(
    monkeypatch,
) -> None:
    """Trailing replaced the stop after the plan was computed: the in-lock
    re-read sees a different book and refuses BEFORE the first cancel, so no
    restore is needed and nothing is written."""
    client = _FakeTradingClient(
        orders=_filled_bracket(),
        positions=[_FakePosition("AAPL", 10.0)],
    )
    adapter = _adapter(client)
    captured: list[LiveSignal] = []
    _patched_signal_store(monkeypatch, captured)
    real_candidates = adapter._exit_protection_candidates
    calls = {"n": 0}

    async def _candidates(client_, symbol, side):
        calls["n"] += 1
        result = await real_candidates(client_, symbol, side)
        if calls["n"] == 2:
            # The second read (inside _order_book_lock) sees the protection
            # gone - e.g. trailing replaced it a moment ago.
            return []
        return result

    monkeypatch.setattr(adapter, "_exit_protection_candidates", _candidates)

    with pytest.raises(RuntimeError, match="changed"):
        asyncio.run(adapter.submit_order(_exit_signal_full(), qty=10.0))

    assert client.cancelled == []
    assert client.submitted == []


def test_unknown_exit_submit_outcome_is_logged_and_protection_restored(
    monkeypatch, caplog
) -> None:
    """A timeout is not a refusal: the log must say the outcome is unknown,
    and protection is still restored for whatever position remains."""
    client = _FakeTradingClient(
        orders=_filled_bracket(),
        positions=[_FakePosition("AAPL", 10.0)],
    )
    adapter = _adapter(client)
    captured: list[LiveSignal] = []
    _patched_signal_store(monkeypatch, captured)

    def _timeout(order_data):
        if _type_text(order_data) == "market":
            raise TimeoutError("read timed out")
        return _FakeTradingClient.submit_order(client, order_data)

    client.submit_order = _timeout  # type: ignore[method-assign]

    with caplog.at_level("WARNING"):
        with pytest.raises(TimeoutError):
            asyncio.run(adapter.submit_order(_exit_signal_full(), qty=10.0))

    assert any("outcome is unknown" in record.message for record in caplog.records)
    oco = [order for order in client.submitted if _class_text(order) == "oco"]
    assert len(oco) == 1
    assert float(oco[0].qty) == 10.0


def test_definite_broker_rejection_does_not_claim_unknown_outcome(
    monkeypatch, caplog
) -> None:
    """An alpaca-py APIError is the broker answering 'no': no unknown-outcome
    warning, but the protection is still restored."""
    from alpaca.common.exceptions import APIError

    client = _FakeTradingClient(
        orders=_filled_bracket(),
        positions=[_FakePosition("AAPL", 10.0)],
    )
    adapter = _adapter(client)
    captured: list[LiveSignal] = []
    _patched_signal_store(monkeypatch, captured)

    def _reject(order_data):
        if _type_text(order_data) == "market":
            raise APIError("insufficient qty available for order to be filled")
        return _FakeTradingClient.submit_order(client, order_data)

    client.submit_order = _reject  # type: ignore[method-assign]

    with caplog.at_level("WARNING"):
        with pytest.raises(APIError):
            asyncio.run(adapter.submit_order(_exit_signal_full(), qty=10.0))

    assert not any(
        "outcome is unknown" in record.message for record in caplog.records
    )
    oco = [order for order in client.submitted if _class_text(order) == "oco"]
    assert len(oco) == 1


def test_lone_stop_limit_leg_is_replaced_as_a_stop_limit(monkeypatch) -> None:
    """A stop-limit protection goes back as a stop-limit, not a plain stop:
    re-placing it as a stop would widen the trigger into a market order."""
    client = _FakeTradingClient(
        orders=[
            _FakeOrder(
                id="sl-1",
                symbol="AAPL",
                side="sell",
                order_type="stop_limit",
                qty=10.0,
                stop_price=190.0,
                limit_price=189.5,
                status="new",
            )
        ],
        positions=[_FakePosition("AAPL", 10.0)],
    )
    adapter = _adapter(client)
    captured: list[LiveSignal] = []
    _patched_signal_store(monkeypatch, captured)

    asyncio.run(adapter.submit_order(_partial_signal(4.0), qty=4.0))

    stops = [
        order
        for order in client.submitted
        if _type_text(order) == "stop_limit"
    ]
    assert len(stops) == 1
    assert float(stops[0].stop_price) == 190.0
    assert float(stops[0].limit_price) == 189.5
    assert float(stops[0].qty) == 6.0


def test_reprotect_order_names_the_parent_entry_signal(monkeypatch) -> None:
    """The re-placed OCO's client_order_id embeds the entry signal id so a
    later broker-side close of the remainder can be booked against the
    parent's ledger (#277 attribution contract)."""
    client = _FakeTradingClient(
        orders=_filled_bracket(),
        positions=[_FakePosition("AAPL", 10.0)],
    )
    adapter = _adapter(client)
    captured: list[LiveSignal] = []
    _patched_signal_store(monkeypatch, captured)
    monkeypatch.setattr(
        alpaca_module,
        "find_latest_filled_live_entry_signals",
        lambda deployment_ids, symbols: {
            "AAPL": {"signal_id": "entry-sig-1", "deployment_id": "dep-1"}
        },
    )

    asyncio.run(adapter.submit_order(_partial_signal(4.0), qty=4.0))

    oco = [order for order in client.submitted if _class_text(order) == "oco"]
    assert len(oco) == 1
    client_order_id = str(getattr(oco[0], "client_order_id", ""))
    entry_part, _, exit_part = client_order_id.partition("-reprotect-")
    assert entry_part == "entry-sig-1"
    assert exit_part  # the exit signal id keeps the key unique per exit
    marker = captured[0].metadata["exit_reprotection"]
    assert marker["entry_signal_id"] == "entry-sig-1"


def test_recovery_replays_only_the_newest_marker_per_symbol(monkeypatch) -> None:
    """Two pending markers on one symbol: the newest (first row) is replayed;
    the older one is left pending rather than placing stale protection."""
    client = _FakeTradingClient(positions=[_FakePosition("AAPL", 6.0)])
    adapter = _adapter(client)
    monkeypatch.setattr(
        alpaca_module,
        "list_pending_exit_reprotections",
        lambda account_id, *, limit=50: [
            {
                "signal_id": "sig-new",
                "symbol": "AAPL",
                "deployment_id": "dep-1",
                "timestamp": datetime.now(timezone.utc),
                "marker": {
                    "side": "sell",
                    "stop_price": 195.0,
                    "take_profit_price": 215.0,
                },
            },
            {
                "signal_id": "sig-old",
                "symbol": "AAPL",
                "deployment_id": "dep-1",
                "timestamp": datetime.now(timezone.utc),
                "marker": {
                    "side": "sell",
                    "stop_price": 180.0,
                    "take_profit_price": 220.0,
                },
            },
        ],
    )
    cleared: list[str] = []
    monkeypatch.setattr(
        alpaca_module,
        "clear_live_signal_metadata_key",
        lambda signal_id, key: cleared.append(signal_id) or True,
    )

    asyncio.run(adapter._recover_exit_reprotections())

    oco = [order for order in client.submitted if _class_text(order) == "oco"]
    assert len(oco) == 1
    assert float(oco[0].stop_loss.stop_price) == 195.0
    assert cleared == ["sig-new"]


def test_recovery_sees_a_held_stop_leg_nested_under_a_filled_parent(
    monkeypatch,
) -> None:
    """The held stop leg is only reachable nested under its filled parent
    (status=all listing); without nested reads the marker looks half-missing
    and is never cleared."""
    client = _FakeTradingClient(
        orders=_filled_bracket(),
        positions=[_FakePosition("AAPL", 6.0)],
    )
    adapter = _adapter(client)
    monkeypatch.setattr(
        alpaca_module,
        "list_pending_exit_reprotections",
        lambda account_id, *, limit=50: [
            {
                "signal_id": "sig-1",
                "symbol": "AAPL",
                "deployment_id": "dep-1",
                "timestamp": datetime.now(timezone.utc),
                "marker": {
                    "side": "sell",
                    "stop_price": 190.0,
                    "take_profit_price": 210.0,
                },
            }
        ],
    )
    cleared: list[str] = []
    monkeypatch.setattr(
        alpaca_module,
        "clear_live_signal_metadata_key",
        lambda signal_id, key: cleared.append(signal_id) or True,
    )

    asyncio.run(adapter._recover_exit_reprotections())

    # Both legs are seen (stop via the filled parent's held leg, tp working):
    # the marker is satisfied and cleared, nothing is re-placed.
    assert client.submitted == []
    assert cleared == ["sig-1"]


def test_recovery_clears_a_marker_whose_side_no_longer_matches(monkeypatch) -> None:
    """A marker for the opposite side is stale: alert, then clear it so it
    cannot pile up ahead of recoverable markers."""
    client = _FakeTradingClient(positions=[_FakePosition("AAPL", 6.0)])
    adapter = _adapter(client)
    monkeypatch.setattr(
        alpaca_module,
        "list_pending_exit_reprotections",
        lambda account_id, *, limit=50: [
            {
                "signal_id": "sig-1",
                "symbol": "AAPL",
                "deployment_id": "dep-1",
                "timestamp": datetime.now(timezone.utc),
                "marker": {
                    "side": "buy",
                    "stop_price": 190.0,
                    "take_profit_price": 210.0,
                },
            }
        ],
    )
    cleared: list[str] = []
    monkeypatch.setattr(
        alpaca_module,
        "clear_live_signal_metadata_key",
        lambda signal_id, key: cleared.append(signal_id) or True,
    )

    asyncio.run(adapter._recover_exit_reprotections())

    assert client.submitted == []
    assert cleared == ["sig-1"]


def test_recovery_clears_a_marker_with_no_recorded_levels(monkeypatch) -> None:
    """A marker that never recorded levels and finds no protection can never
    place anything: alert, then clear it."""
    client = _FakeTradingClient(positions=[_FakePosition("AAPL", 6.0)])
    adapter = _adapter(client)
    monkeypatch.setattr(
        alpaca_module,
        "list_pending_exit_reprotections",
        lambda account_id, *, limit=50: [
            {
                "signal_id": "sig-1",
                "symbol": "AAPL",
                "deployment_id": "dep-1",
                "timestamp": datetime.now(timezone.utc),
                "marker": {"side": "sell"},
            }
        ],
    )
    cleared: list[str] = []
    monkeypatch.setattr(
        alpaca_module,
        "clear_live_signal_metadata_key",
        lambda signal_id, key: cleared.append(signal_id) or True,
    )

    asyncio.run(adapter._recover_exit_reprotections())

    assert client.submitted == []
    assert cleared == ["sig-1"]


# ─── #350 bundle-32 review: real OCO shape, ambiguous-level restore ────────


def test_a_working_market_exit_is_never_cancelled_as_protection() -> None:
    """A working market order on the reducing side is an exit in flight, not
    bracket protection: it must not be collected as a cancel candidate."""
    client = _FakeTradingClient(
        orders=[
            _FakeOrder(
                id="mkt-1",
                symbol="AAPL",
                side="sell",
                order_type="market",
                qty=4.0,
                status="new",
            )
        ],
        positions=[_FakePosition("AAPL", 10.0)],
    )
    adapter = _adapter(client)

    candidates = asyncio.run(adapter._exit_protection_candidates(client, "AAPL", "sell"))

    assert candidates == []


def test_recovery_replaces_a_lone_recorded_stop(monkeypatch) -> None:
    """A marker carrying only a stop level and no working stop gets that stop
    re-placed as a lone order on recovery."""
    client = _FakeTradingClient(positions=[_FakePosition("AAPL", 6.0)])
    adapter = _adapter(client)
    monkeypatch.setattr(
        alpaca_module,
        "list_pending_exit_reprotections",
        lambda account_id, *, limit=50: [
            {
                "signal_id": "sig-1",
                "symbol": "AAPL",
                "deployment_id": "dep-1",
                "timestamp": datetime.now(timezone.utc),
                "marker": {
                    "side": "sell",
                    "stop_price": 190.0,
                    "stop_limit_price": None,
                    "take_profit_price": None,
                    "position_qty": 10.0,
                    "exit_qty": 4.0,
                    "reprotect_qty": 6.0,
                },
            }
        ],
    )
    cleared: list[str] = []
    monkeypatch.setattr(
        alpaca_module,
        "clear_live_signal_metadata_key",
        lambda signal_id, key: cleared.append(signal_id) or True,
    )

    asyncio.run(adapter._recover_exit_reprotections())

    assert not [order for order in client.submitted if _class_text(order) == "oco"]
    stops = [
        order
        for order in client.submitted
        if "stop" in _type_text(order) or getattr(order, "stop_price", None) is not None
    ]
    assert len(stops) == 1
    assert float(stops[0].stop_price) == 190.0
    assert float(stops[0].qty) == 6.0
    assert cleared == ["sig-1"]


def _filled_bracket_tranche(
    symbol: str, qty: float, *, stop_id: str, tp_id: str, parent_id: str,
    stop_price: float, tp_price: float,
) -> list[_FakeOrder]:
    """One filled bracket tranche with distinct order ids (pyramided entries)."""
    stop_leg = _FakeOrder(
        id=stop_id,
        symbol=symbol,
        side="sell",
        order_type="stop",
        qty=qty,
        stop_price=stop_price,
        status="held",
    )
    parent = _FakeOrder(
        id=parent_id,
        symbol=symbol,
        side="buy",
        order_type="market",
        order_class="bracket",
        qty=qty,
        status="filled",
        legs=[stop_leg],
    )
    take_profit = _FakeOrder(
        id=tp_id,
        symbol=symbol,
        side="sell",
        order_type="limit",
        qty=qty,
        limit_price=tp_price,
        status="new",
    )
    return [parent, take_profit]


def test_a_full_exit_over_ambiguous_levels_restores_each_recorded_candidate(
    monkeypatch,
) -> None:
    """Pyramided protection (two stops, two take-profits) on a FULL exit whose
    submit is then refused: the marker's per-candidate record lets the restore
    re-place each tranche as its own OCO - nothing is left unprotected."""
    orders = _filled_bracket_tranche(
        "AAPL", 5.0, stop_id="sl-a", tp_id="tp-a", parent_id="parent-a",
        stop_price=190.0, tp_price=210.0,
    ) + _filled_bracket_tranche(
        "AAPL", 5.0, stop_id="sl-b", tp_id="tp-b", parent_id="parent-b",
        stop_price=185.0, tp_price=215.0,
    )
    client = _FakeTradingClient(
        orders=orders,
        positions=[_FakePosition("AAPL", 10.0)],
    )
    adapter = _adapter(client)
    captured: list[LiveSignal] = []
    _patched_signal_store(monkeypatch, captured)
    real_submit = client.submit_order

    def _fail_exit(order_data):
        if _type_text(order_data) == "market":
            raise RuntimeError("broker rejected the exit")
        return real_submit(order_data)

    client.submit_order = _fail_exit  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="broker rejected the exit"):
        asyncio.run(adapter.submit_order(_exit_signal_full(), qty=10.0))

    # The marker recorded every candidate, not just the ambiguous Nones.
    marker = captured[0].metadata["exit_reprotection"]
    assert marker["stop_price"] is None
    assert marker["take_profit_price"] is None
    assert len(marker["protection"]) == 4

    # The restore re-placed both tranches as OCO pairs at their own levels.
    oco = [order for order in client.submitted if _class_text(order) == "oco"]
    assert len(oco) == 2
    assert sorted(float(order.qty) for order in oco) == [5.0, 5.0]
    assert sorted(float(order.stop_loss.stop_price) for order in oco) == [185.0, 190.0]
    assert sorted(float(order.take_profit.limit_price) for order in oco) == [210.0, 215.0]


# ─── #350 review round 3: marker lifecycle, stream watchdog, scan bounds ────


def test_stream_expired_exit_tops_up_without_a_reconnect(monkeypatch) -> None:
    """A DAY exit that expires while the daemon runs must not strand shares.

    The under-fill watchdog used to run only at ``connect``: an exit order
    going working -> ``expired`` with ``filled_qty < qty`` left the uncovered
    shares naked until the next reconnect - potentially days. A terminal
    trade update now replays the marker immediately (#350 review).
    """
    exit_order = _FakeOrder(
        id="exit-1",
        symbol="AAPL",
        side="sell",
        order_type="limit",
        qty=4.0,
        status="expired",
        filled_qty=1.0,
    )
    client = _FakeTradingClient(
        orders=[_working_oco(qty=6.0), exit_order],
        # The expired exit's 1.0 fill already left the position: 10 - 1 = 9.
        positions=[_FakePosition("AAPL", 9.0)],
    )
    adapter = _adapter(client)
    signal_id = str(uuid4())
    marker = {
        "side": "sell",
        "stop_price": 190.0,
        "take_profit_price": 210.0,
        "entry_signal_id": "entry-1",
    }
    monkeypatch.setattr(
        alpaca_module,
        "get_live_signal_metadata",
        lambda sid: {
            "risk_sizing_mode": "exit_only",
            "signal_intent": "exit",
            "exit_reprotection": marker,
        }
        if str(sid) == signal_id
        else {},
    )
    cleared = _patch_marker_clears(monkeypatch)
    update = SimpleNamespace(
        event="expired",
        order=SimpleNamespace(
            id="exit-1",
            client_order_id=signal_id,
            status="expired",
            symbol="AAPL",
            filled_qty=1.0,
        ),
        timestamp=datetime.now(timezone.utc),
        qty=1.0,
    )

    async def _run() -> None:
        await adapter._on_trade_update(update)
        if adapter._reprotect_watchdog_tasks:
            await asyncio.gather(*adapter._reprotect_watchdog_tasks)

    asyncio.run(_run())

    # 10 held - 6 covered - 1 filled by the expired exit = 3 naked -> top-up.
    top_ups = [
        order
        for order in client.submitted
        if _class_text(order) == "oco" and float(order.qty) == 3.0
    ]
    assert len(top_ups) == 1
    assert cleared == [signal_id]

    # A redelivered terminal update for the same order does not replay again.
    client.submitted.clear()
    cleared.clear()

    async def _run_again() -> None:
        await adapter._on_trade_update(update)
        if adapter._reprotect_watchdog_tasks:
            await asyncio.gather(*adapter._reprotect_watchdog_tasks)

    asyncio.run(_run_again())
    assert client.submitted == []


def test_stream_filled_exit_with_full_coverage_clears_the_marker(
    monkeypatch,
) -> None:
    """A filled exit whose remainder is already protected spends the marker."""
    exit_order = _FakeOrder(
        id="exit-2",
        symbol="AAPL",
        side="sell",
        order_type="market",
        qty=4.0,
        status="filled",
        filled_qty=4.0,
    )
    client = _FakeTradingClient(
        orders=[_working_oco(qty=6.0), exit_order],
        positions=[_FakePosition("AAPL", 6.0)],
    )
    adapter = _adapter(client)
    signal_id = str(uuid4())
    marker = {
        "side": "sell",
        "stop_price": 190.0,
        "take_profit_price": 210.0,
        "entry_signal_id": "entry-1",
    }
    monkeypatch.setattr(
        alpaca_module,
        "get_live_signal_metadata",
        lambda sid: {
            "risk_sizing_mode": "exit_only",
            "signal_intent": "exit",
            "exit_reprotection": marker,
        }
        if str(sid) == signal_id
        else {},
    )
    cleared = _patch_marker_clears(monkeypatch)
    update = SimpleNamespace(
        event="new",
        order=SimpleNamespace(
            id="exit-2",
            client_order_id=signal_id,
            status="filled",
            symbol="AAPL",
            filled_qty=4.0,
        ),
        timestamp=datetime.now(timezone.utc),
        qty=4.0,
    )

    async def _run() -> None:
        await adapter._on_trade_update(update)
        if adapter._reprotect_watchdog_tasks:
            await asyncio.gather(*adapter._reprotect_watchdog_tasks)

    asyncio.run(_run())

    assert client.submitted == []
    assert cleared == [signal_id]


def test_list_orders_status_all_pages_until_plus_one_microsecond() -> None:
    """``until`` steps one microsecond past the page's oldest ``submitted_at``.

    ``until`` is inclusive, so the +1µs can only *widen* the next page by the
    boundary timestamp; the id dedupe absorbs the repeat and no order can
    fall into a gap between pages (#350 nit).
    """
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    orders = [
        _FakeOrder(
            id=f"ord-{index}",
            symbol="AAPL",
            side="buy",
            order_type="limit",
            status="canceled",
            qty=1.0,
            submitted_at=base + timedelta(seconds=1000 - index),
        )
        for index in range(501)
    ]
    client = _FakeTradingClient(orders=orders)
    adapter = _adapter(client)
    filters: list[object] = []
    original = client.get_orders

    def _record(filter=None):  # noqa: A002 - mirrors alpaca-py
        filters.append(filter)
        return original(filter)

    client.get_orders = _record  # type: ignore[method-assign]

    collected = asyncio.run(adapter._list_orders_status_all(client))

    assert len(collected) == 501
    assert len({getattr(order, "id") for order in collected}) == 501
    assert len(filters) == 2
    page1_oldest = orders[499].submitted_at
    assert filters[1].until == page1_oldest + timedelta(microseconds=1)


def test_held_leg_lookup_bounds_the_scan_at_the_ledger(monkeypatch) -> None:
    """The ``status=all`` held-leg scan passes the ledger ``after`` bound."""
    bound = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(
        alpaca_module,
        "earliest_live_signal_timestamp",
        lambda account_id, symbols: bound,
    )
    client = _FakeTradingClient(orders=_filled_bracket())
    adapter = _adapter(client)
    adapter.account_id = "acct-bound"

    asyncio.run(adapter._list_filled_parents_with_held_legs(client, {"AAPL"}))

    assert client.last_order_filter.after == bound - timedelta(minutes=5)


def test_reconcile_reprotect_orders_uses_and_advances_the_watermark(
    monkeypatch,
) -> None:
    """The account-wide reprotect scan is bounded by a persisted watermark.

    Without it a minute-bar account passes the 12,500-order page cap within
    weeks and every connect's reconcile raises - the #277 defect returns.
    The bound is the earliest still-working ``-reprotect`` order's
    ``submitted_at`` (minus the shared margin), or the pass start when none
    worked; a failed pass never moves it (#350 review).
    """
    store: dict[str, dict] = {}
    monkeypatch.setattr(
        alpaca_module,
        "get_live_adapter_state",
        lambda account_id, key: store.get(f"{account_id}|{key}"),
    )
    monkeypatch.setattr(
        alpaca_module,
        "set_live_adapter_state",
        lambda account_id, key, value: store.__setitem__(
            f"{account_id}|{key}", dict(value)
        ),
    )
    monkeypatch.setattr(
        alpaca_module, "get_live_signal_metadata", lambda _sid: {}
    )
    working_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    reprotect = _FakeOrder(
        id=str(uuid4()),
        symbol="AAPL",
        side="sell",
        order_type="stop",
        qty=6.0,
        stop_price=190.0,
        status="new",
        submitted_at=working_at,
        client_order_id=f"{uuid4()}-reprotect-{uuid4()}",
    )
    client = _FakeTradingClient(orders=[reprotect])
    adapter = _adapter(client)
    adapter.account_id = "acct-wm"
    seen_after: list[object] = []
    original = client.get_orders

    def _record(filter=None):  # noqa: A002 - mirrors alpaca-py
        seen_after.append(getattr(filter, "after", None))
        return original(filter)

    client.get_orders = _record  # type: ignore[method-assign]

    asyncio.run(adapter._reconcile_reprotect_orders())

    # First pass: no watermark -> unbounded; the working order bounds it.
    assert seen_after == [None]
    expected = (working_at - timedelta(minutes=5)).isoformat()
    assert store["acct-wm|alpaca_reprotect_reconcile_after"]["after"] == expected

    asyncio.run(adapter._reconcile_reprotect_orders())

    # Second pass: the persisted bound reaches the broker as ``after``.
    assert seen_after[1] == datetime.fromisoformat(expected)


def test_recover_exit_reprotections_clears_superseded_markers(monkeypatch) -> None:
    """Once a symbol's newest marker resolves, its older markers are spent."""
    client = _FakeTradingClient(
        orders=[_working_oco(qty=6.0)],
        positions=[_FakePosition("AAPL", 6.0)],
    )
    adapter = _adapter(client)
    _patch_pending_reprotections(
        monkeypatch,
        [
            _reprotect_row(
                {
                    "side": "sell",
                    "stop_price": 190.0,
                    "take_profit_price": 210.0,
                },
                signal_id="sig-new",
            )
        ],
    )
    _patch_marker_clears(monkeypatch)
    superseded: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        alpaca_module,
        "get_live_signal_metadata",
        lambda _sid: {},  # marker gone -> the newest row resolved
    )
    monkeypatch.setattr(
        alpaca_module,
        "clear_superseded_exit_reprotection_markers",
        lambda account_id, symbol, *, keep_signal_id: superseded.append(
            (account_id, symbol, keep_signal_id)
        )
        or 2,
    )

    asyncio.run(adapter._recover_exit_reprotections())

    assert superseded == [(adapter.account_id, "AAPL", "sig-new")]

def test_reconciliation_orders_do_not_mark_market_exits_as_protection() -> None:
    """#303: a working exit is a market order; "reduces a position" alone must
    not mark it protection, or the reconciler reports it missing and halts
    entries on a healthy account."""
    client = _FakeTradingClient(
        orders=[
            _FakeOrder("exit-1", "AAPL", "sell", order_type="market"),
            _FakeOrder("tp-1", "AAPL", "sell", order_type="limit"),
            _FakeOrder("sl-1", "AAPL", "sell", order_type="stop", stop_price=90.0),
            _FakeOrder("entry-1", "MSFT", "buy", order_type="limit"),
        ],
        positions=[_FakePosition("AAPL", 10.0)],
    )

    views = {
        view.order_id: view
        for view in asyncio.run(_adapter(client).list_reconciliation_orders())
    }

    assert views["exit-1"].protection is False
    assert views["tp-1"].protection is True
    assert views["sl-1"].protection is True
    assert views["entry-1"].protection is False


def test_reconciliation_orders_mark_bracket_legs_as_protection() -> None:
    legs = [
        _FakeOrder("leg-tp", "AAPL", "sell", order_type="limit"),
        _FakeOrder("leg-sl", "AAPL", "sell", order_type="stop", stop_price=90.0),
    ]
    client = _FakeTradingClient(
        orders=[_FakeOrder("parent-1", "AAPL", "buy", order_type="market", legs=legs)],
        positions=[_FakePosition("AAPL", 10.0)],
    )

    views = {
        view.order_id: view
        for view in asyncio.run(_adapter(client).list_reconciliation_orders())
    }

    assert views["leg-tp"].protection is True
    assert views["leg-sl"].protection is True


def test_order_remaining_qty_reports_qty_minus_filled() -> None:
    from live_runner.alpaca_adapter import _order_remaining_qty

    assert _order_remaining_qty(_FakeOrder("o1", "AAPL", "buy", qty=10.0, filled_qty=4.0)) == pytest.approx(6.0)


def test_order_remaining_qty_is_none_when_qty_is_unreadable() -> None:
    from live_runner.alpaca_adapter import _order_remaining_qty

    assert _order_remaining_qty(_FakeOrder("o1", "AAPL", "buy", qty="junk", filled_qty=0.0)) is None
    assert _order_remaining_qty(_FakeOrder("o1", "AAPL", "buy", qty=None, filled_qty=0.0)) is None


def test_order_remaining_qty_is_none_when_filled_qty_is_unreadable() -> None:
    # An unreadable filled_qty is "unknown", never 0: guessing 0 would
    # overstate the working quantity.
    from live_runner.alpaca_adapter import _order_remaining_qty

    assert _order_remaining_qty(_FakeOrder("o1", "AAPL", "buy", qty=10.0, filled_qty="junk")) is None
    assert _order_remaining_qty(_FakeOrder("o1", "AAPL", "buy", qty=10.0, filled_qty=None)) is None


def test_order_remaining_qty_reports_zero_when_filled_covers_qty() -> None:
    # filled >= qty on an order still listed as working means "filled but not
    # yet off the book": the remaining working qty is 0, not the full qty.
    from live_runner.alpaca_adapter import _order_remaining_qty

    assert _order_remaining_qty(_FakeOrder("o1", "AAPL", "buy", qty=10.0, filled_qty=10.0)) == 0.0
    assert _order_remaining_qty(_FakeOrder("o1", "AAPL", "buy", qty=10.0, filled_qty=12.0)) == 0.0


def test_get_order_state_maps_broker_statuses() -> None:
    """Reconciliation lookups: accounted-for statuses drop the finding, the
    rest fail closed (#303)."""
    orders = {
        "ord-expired": SimpleNamespace(status="expired"),
        "ord-filled": SimpleNamespace(status="filled"),
        "ord-working": SimpleNamespace(status="new"),
        "ord-weird": SimpleNamespace(status="some_future_status"),
        "ord-done-for-day": SimpleNamespace(status="done_for_day"),
        "ord-stopped": SimpleNamespace(status="stopped"),
        "ord-suspended": SimpleNamespace(status="suspended"),
        "ord-replaced": SimpleNamespace(status="replaced"),
    }

    def get_order_by_id(order_id):
        order = orders.get(order_id)
        if order is None:
            raise RuntimeError("404 not found")
        return order

    client = SimpleNamespace(get_order_by_id=get_order_by_id)
    adapter = _adapter(client)

    assert asyncio.run(adapter.get_order_state("ord-expired")) == "terminal"
    # A fill is not a dead order: callers must never resubmit it as terminal.
    assert asyncio.run(adapter.get_order_state("ord-filled")) == "filled"
    assert asyncio.run(adapter.get_order_state("ord-working")) == "working"
    assert asyncio.run(adapter.get_order_state("ord-weird")) is None
    assert asyncio.run(adapter.get_order_state("ord-missing")) is None
    for order_id in (
        "ord-done-for-day",
        "ord-stopped",
        "ord-suspended",
        "ord-replaced",
    ):
        assert asyncio.run(adapter.get_order_state(order_id)) == "terminal"


def test_get_order_state_falls_back_to_client_order_id() -> None:
    def get_order_by_id(order_id):
        raise RuntimeError("404 not found")

    def get_order_by_client_id(client_order_id):
        if client_order_id != "sig-1":
            raise RuntimeError("404 not found")
        return SimpleNamespace(status="canceled")

    client = SimpleNamespace(
        get_order_by_id=get_order_by_id,
        get_order_by_client_id=get_order_by_client_id,
    )
    adapter = _adapter(client)

    assert (
        asyncio.run(adapter.get_order_state("broker-id", client_order_id="sig-1"))
        == "terminal"
    )


def test_install_http_timeout_wraps_the_session_once() -> None:
    from live_runner.alpaca_adapter import _install_http_timeout

    calls: list[dict] = []

    class _Session:
        def request(self, method, url, **kwargs):
            calls.append({"method": method, "kwargs": kwargs})
            return "ok"

    client = SimpleNamespace(_session=_Session())
    _install_http_timeout(client, 12.5)
    _install_http_timeout(client, 99.0)  # second call must not double-wrap

    assert client._session.request("GET", "https://example.invalid") == "ok"
    assert calls == [
        {"method": "GET", "kwargs": {"timeout": 12.5}}
    ]

# ── get_max_leverage (#240/#302) ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("multiplier", "expected"),
    [
        # multiplier=4 is intraday buying power only; Reg T caps overnight
        # marginable-stock positions at 2x, and no platform order is marked
        # intraday-only, so the ceiling is the conservative min(multiplier, 2).
        ("4", 2.0),
        ("2", 2.0),
        ("1", 1.0),
    ],
)
def test_get_max_leverage_returns_the_overnight_reg_t_ceiling(
    multiplier: str, expected: float
) -> None:
    client = _FakeTradingClient()
    client.account.multiplier = multiplier
    assert asyncio.run(_adapter(client).get_max_leverage("AAPL")) == pytest.approx(expected)


def test_get_max_leverage_crypto_stays_at_the_1x_default() -> None:
    # Alpaca offers no crypto margin: the multiplier does not apply, so the
    # lookup reports unknown and the caps fail closed to 1x.
    client = _FakeTradingClient()
    client.account.multiplier = "4"
    assert asyncio.run(_adapter(client).get_max_leverage("BTC/USD")) is None
    assert asyncio.run(_adapter(client).get_max_leverage("BTCUSD")) is None


def test_get_max_leverage_unreadable_multiplier_fails_closed() -> None:
    client = _FakeTradingClient()
    client.account = SimpleNamespace(cash=1.0, equity=1.0, currency="USD")
    assert asyncio.run(_adapter(client).get_max_leverage("AAPL")) is None
    client.account.multiplier = "0"
    assert asyncio.run(_adapter(client).get_max_leverage("AAPL")) is None


# ── get_used_margin (#359) ────────────────────────────────────────────────────


def test_get_used_margin_prefers_the_brokers_initial_margin() -> None:
    # The broker's own figure already prices marginable vs non-marginable and
    # crypto positions; it wins over any local approximation.
    client = _FakeTradingClient()
    client.account.initial_margin = "1234.56"
    assert asyncio.run(_adapter(client).get_used_margin()) == pytest.approx(1234.56)


def test_get_used_margin_falls_back_to_position_notionals_over_leverage() -> None:
    # No initial_margin on the account: sum |market value| / effective
    # leverage — min(multiplier, 2) for marginable stocks, 1x for crypto and
    # non-marginable assets.
    client = _FakeTradingClient()
    client.positions = [
        SimpleNamespace(
            symbol="AAPL", market_value="100000", asset_class="us_equity",
            asset_marginable=True,
        ),
        SimpleNamespace(
            symbol="BTC/USD", market_value="-40000", asset_class="crypto",
            asset_marginable=False,
        ),
        SimpleNamespace(
            symbol="XYZ", market_value="30000", asset_class="us_equity",
            asset_marginable=False,
        ),
    ]
    # multiplier "2" -> overnight leverage 2 for the marginable stock only:
    # 100000/2 + 40000/1 + 30000/1 = 120000.
    assert asyncio.run(_adapter(client).get_used_margin()) == pytest.approx(120_000.0)


def test_get_used_margin_fallback_without_positions_is_zero() -> None:
    client = _FakeTradingClient()
    assert asyncio.run(_adapter(client).get_used_margin()) == 0.0


def test_get_used_margin_returns_none_when_positions_are_unreadable() -> None:
    client = _FakeTradingClient()

    def _boom():
        raise RuntimeError("positions endpoint down")

    client.get_all_positions = _boom
    assert asyncio.run(_adapter(client).get_used_margin()) is None


def test_get_used_margin_raises_on_an_unreadable_position_value() -> None:
    # A partial sum would understate used margin and overstate free margin:
    # ``parse_money`` raises ``MoneyParseError`` so the caps refuse.
    from shared.money import MoneyParseError

    client = _FakeTradingClient()
    client.positions = [
        SimpleNamespace(symbol="AAPL", market_value=None),
    ]
    with pytest.raises(MoneyParseError):
        asyncio.run(_adapter(client).get_used_margin())


def test_get_used_margin_raises_on_a_non_finite_position_value() -> None:
    # market_value is Optional[str] on the broker model; "nan" parses but is
    # not a usable notional, so the fallback must fail closed like any other
    # unreadable figure.
    from shared.money import MoneyParseError

    client = _FakeTradingClient()
    client.positions = [
        SimpleNamespace(symbol="AAPL", market_value="nan"),
    ]
    with pytest.raises(MoneyParseError):
        asyncio.run(_adapter(client).get_used_margin())


def test_get_used_margin_raises_on_a_garbage_initial_margin() -> None:
    # A present-but-unreadable ``initial_margin`` is not "absent": it raises
    # so the caps refuse, instead of silently degrading to the approximation.
    from shared.money import MoneyParseError

    client = _FakeTradingClient()
    client.account.initial_margin = "junk"
    with pytest.raises(MoneyParseError):
        asyncio.run(_adapter(client).get_used_margin())


def test_get_max_leverage_garbage_multiplier_raises_money_parse_error() -> None:
    # A present-but-unreadable multiplier is not "absent": it raises so the
    # caps' ``_safe_max_leverage`` maps it to the 1x ceiling, never to a guess.
    from shared.money import MoneyParseError

    client = _FakeTradingClient()
    client.account.multiplier = "junk"
    with pytest.raises(MoneyParseError):
        asyncio.run(_adapter(client).get_max_leverage("AAPL"))


# ── Money fields fail closed (#280) ──────────────────────────────────────────
#
# ``_to_float(..., default=0.0)`` used to map an unreadable money field onto
# zero: a malformed qty read as "flat", a malformed stop price as "no stop",
# a malformed cash balance as "0.00". Every money field below now parses
# through ``shared.money`` and raises on a value the broker did not really send.


def test_get_balance_raises_on_an_unreadable_cash_field() -> None:
    client = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash="not-a-number", equity=100.0)
    )
    adapter = _adapter(client)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="unreadable money value"):
        asyncio.run(adapter.get_balance())


def test_get_balance_raises_on_an_unreadable_equity_field() -> None:
    client = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash=100.0, equity="not-a-number")
    )
    adapter = _adapter(client)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="unreadable money value"):
        asyncio.run(adapter.get_balance())


def test_get_balance_falls_back_to_cash_when_equity_is_absent() -> None:
    client = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash=100.0, equity=None)
    )
    adapter = _adapter(client)  # type: ignore[arg-type]

    balance, equity = asyncio.run(adapter.get_balance())

    assert (balance, equity) == (100.0, 100.0)


def test_get_positions_raises_on_an_unreadable_required_money_field() -> None:
    # avg_entry_price is a required broker field; a garbage value still fails
    # the whole listing rather than reporting a guessed entry price.
    client = _FakeTradingClient(positions=[_position_row(avg_entry_price="junk")])
    adapter = _adapter(client)

    with pytest.raises(ValueError, match="unreadable money value"):
        asyncio.run(adapter.get_positions())


def test_get_positions_tolerates_absent_display_money_fields() -> None:
    # current_price/unrealized_pl are Optional[str] on the Alpaca model: null
    # is a legitimate broker answer and no risk path reads them, so the row
    # must list with None rather than take down get_positions — and with it
    # the flatten and exit-sizing paths.
    client = _FakeTradingClient(
        positions=[_position_row(current_price=None, unrealized_pl=None)]
    )
    adapter = _adapter(client)

    positions = asyncio.run(adapter.get_positions())

    assert positions[0]["current_price"] is None
    assert positions[0]["unrealized_pnl"] is None
    assert "current_price_unparseable" not in positions[0]


def test_get_positions_marks_garbage_display_money_fields() -> None:
    client = _FakeTradingClient(
        positions=[_position_row(current_price="junk", unrealized_pl="junk")]
    )
    adapter = _adapter(client)

    positions = asyncio.run(adapter.get_positions())

    assert positions[0]["current_price"] is None
    assert positions[0]["current_price_unparseable"] is True
    assert positions[0]["unrealized_pnl"] is None
    assert positions[0]["unrealized_pnl_unparseable"] is True


def test_flatten_completes_with_null_display_money_fields() -> None:
    client = _FakeTradingClient(
        orders=[
            _FakeOrder(
                "sl-1", "AAPL", "sell", order_type="stop", qty=1.0, stop_price=90.0
            )
        ],
        positions=[_position_row(current_price=None, unrealized_pl=None, qty=1.0)],
    )

    outcome = asyncio.run(_adapter(client).close_all_positions())

    assert outcome.complete is True
    assert client.close_calls == ["AAPL"]


def test_flatten_completes_with_a_garbage_stop_leg_price() -> None:
    # _best_stop_price only picks a price to *restore* a protection if the
    # close fails; one unreadable leg must not abort the flatten for every
    # remaining symbol.
    client = _FakeTradingClient(
        orders=[
            _FakeOrder(
                "sl-1", "AAPL", "sell", order_type="stop", qty=1.0, stop_price="junk"
            )
        ],
        positions=[_position_row(qty=1.0)],
    )

    outcome = asyncio.run(_adapter(client).close_all_positions())

    assert outcome.complete is True
    assert client.cancelled == ["sl-1"]
    assert client.close_calls == ["AAPL"]


def test_protective_stop_skips_an_unreadable_stop_price() -> None:
    from live_runner.alpaca_adapter import _protective_stop_of

    order = {
        "id": "stop-1",
        "symbol": "AAPL",
        "side": "sell",
        "order_type": "stop",
        "stop_price": "junk",
        "qty": 1.0,
    }

    # Skipped, not raised: the symbol then lists zero stops and the trailing
    # path refuses to amend on anything but exactly one — fail closed per
    # symbol instead of downing the whole account's listing.
    assert _protective_stop_of(order, order, {"AAPL": "sell"}) is None


def test_protective_stop_tolerates_an_unreadable_qty() -> None:
    from live_runner.alpaca_adapter import _protective_stop_of

    order = {
        "id": "stop-1",
        "symbol": "AAPL",
        "side": "sell",
        "order_type": "stop",
        "stop_price": 190.0,
        "qty": "junk",
    }

    stop = _protective_stop_of(order, order, {"AAPL": "sell"})

    assert stop is not None
    assert stop.qty is None


def test_list_protective_stops_isolates_a_malformed_candidate() -> None:
    client = _FakeTradingClient(
        positions=[_FakePosition("AAPL", 10.0), _FakePosition("MSFT", 5.0)],
        orders=[
            _FakeOrder(
                "bad-1", "AAPL", "sell", order_type="stop", stop_price="junk"
            ),
            _FakeOrder(
                "ok-1", "MSFT", "sell", order_type="stop", stop_price=300.0
            ),
        ],
    )
    adapter = _adapter(client)

    stops = asyncio.run(adapter.list_protective_stops())

    assert [stop.order_id for stop in stops] == ["ok-1"]


def test_list_protective_stops_drops_a_symbol_with_an_unreadable_candidate() -> None:
    # A malformed stop on a symbol that also has a readable one is ambiguous:
    # listing only the readable stop would let trailing amend it while the
    # malformed one stays put. The whole symbol must list zero stops so the
    # trailing path fails closed.
    client = _FakeTradingClient(
        positions=[_FakePosition("AAPL", 10.0)],
        orders=[
            _FakeOrder(
                "bad-1", "AAPL", "sell", order_type="stop", stop_price="junk"
            ),
            _FakeOrder(
                "ok-1", "AAPL", "sell", order_type="stop", stop_price=190.0
            ),
        ],
    )
    adapter = _adapter(client)

    stops = asyncio.run(adapter.list_protective_stops())

    assert stops == []


def test_best_stop_price_skips_an_unreadable_stop_price() -> None:
    adapter = _adapter(_FakeTradingClient())
    bad = SimpleNamespace(id="bad-1", symbol="AAPL", stop_price="junk")
    good = SimpleNamespace(id="ok-1", symbol="AAPL", stop_price=190.0)

    assert adapter._best_stop_price([bad], "AAPL") is None
    assert adapter._best_stop_price([bad, good], "AAPL") == 190.0


def test_reconcile_skips_a_money_parse_error_and_continues() -> None:
    adapter = _adapter(_FakeTradingClient())

    async def _page(before, max_signals):
        return ["sig-1", "sig-2"] if before is None else []

    adapter._fetch_reconciliation_page = _page
    seen: list[str] = []

    async def _reconcile(signal_id):
        seen.append(signal_id)
        if signal_id == "sig-1":
            raise MoneyParseError("unreadable money value: 'junk'")
        return True

    adapter._reconcile_signal_fill = _reconcile

    assert asyncio.run(adapter.reconcile_executions()) == 1
    assert seen == ["sig-1", "sig-2"]


def test_reconcile_does_not_swallow_a_non_parse_value_error() -> None:
    # Only MoneyParseError is skipped per signal: a programming error or a
    # database ValueError must surface, not read as "unreadable order data".
    adapter = _adapter(_FakeTradingClient())

    async def _page(before, max_signals):
        return ["sig-1"] if before is None else []

    adapter._fetch_reconciliation_page = _page

    async def _reconcile(signal_id):
        raise ValueError("database constraint violated")

    adapter._reconcile_signal_fill = _reconcile

    with pytest.raises(ValueError, match="database constraint violated"):
        asyncio.run(adapter.reconcile_executions())


def test_reconcile_raises_on_an_unreadable_filled_qty() -> None:
    class _OrderClient:
        def get_order_by_client_id(self, _signal_id):
            return SimpleNamespace(filled_qty="junk")

    adapter = _adapter(_OrderClient())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="unreadable money value"):
        asyncio.run(adapter._reconcile_signal_fill("00000000-0000-0000-0000-000000000001"))


def test_order_filled_qty_reads_an_unreadable_quantity_as_unknown() -> None:
    """``None`` means "unknown", not zero: the refuse-cancel excusal must not
    read a quantity nobody could parse as "nothing filled" (#280)."""
    from live_runner.alpaca_adapter import _order_filled_qty

    assert _order_filled_qty(SimpleNamespace(filled_qty="junk")) is None
    assert _order_filled_qty(SimpleNamespace(filled_qty=float("nan"))) is None
    assert _order_filled_qty(SimpleNamespace()) is None
    assert _order_filled_qty(SimpleNamespace(filled_qty=2.5)) == 2.5


def test_a_refused_cancel_is_not_excused_by_an_unreadable_filled_qty() -> None:
    """A ``canceled`` order whose ``filled_qty`` cannot be read proves nothing:
    it may have executed, so the refusal stands (#280)."""
    client = _FakeTradingClient(
        orders=[
            _FakeOrder(
                "entry-1", "AAPL", "buy", status="canceled", filled_qty="junk"
            )
        ]
    )
    adapter = _adapter(client)

    assert asyncio.run(adapter._cancel_already_gone(client, "entry-1")) is False


# ── #382: cancel_orders adapter-level pins ───────────────────────────────────
#
# The runner-level tests only stub ``cancel_orders``; these pin the broker
# side. Only top-level working orders on the named symbol may be cancelled -
# nested bracket legs are position protection and are never even listed.


def test_cancel_orders_cancels_matching_orders_by_id_and_client_id() -> None:
    client = _FakeTradingClient(
        orders=[
            _FakeOrder("o1", "AAPL", "buy"),
            _FakeOrder("o2", "AAPL", "sell"),
            _FakeOrder("o3", "MSFT", "buy"),
        ]
    )
    client.orders[1].client_order_id = "sig-2"

    outcome = asyncio.run(
        _adapter(client).cancel_orders({"o1", "sig-2"}, symbol="AAPL")
    )

    assert sorted(client.cancelled) == ["o1", "o2"]
    assert outcome.completed == 2
    assert outcome.unidentified == ()
    assert outcome.failed == ()


def test_cancel_orders_leaves_other_symbols_and_unmatched_orders() -> None:
    client = _FakeTradingClient(
        orders=[
            _FakeOrder("o1", "MSFT", "buy"),  # ref names it, wrong symbol
            _FakeOrder("o2", "AAPL", "buy"),  # right symbol, no ref names it
        ]
    )
    client.orders[0].client_order_id = "sig-1"

    outcome = asyncio.run(
        _adapter(client).cancel_orders({"o1", "sig-1", "missing"}, symbol="AAPL")
    )

    assert client.cancelled == []
    assert outcome.completed == 0


def test_cancel_orders_never_lists_nested_bracket_legs() -> None:
    """A bracket leg's id in the refs must not reach it: legs are position
    protection, listed only under their parent - never by the open-orders
    read this sweep uses."""
    leg = _FakeOrder("leg-1", "AAPL", "sell", order_type="stop")
    parent = _FakeOrder(
        "parent-1", "AAPL", "buy", order_class="bracket", legs=[leg]
    )
    client = _FakeTradingClient(orders=[parent])

    outcome = asyncio.run(
        _adapter(client).cancel_orders({"leg-1"}, symbol="AAPL")
    )

    assert client.cancelled == []
    assert outcome.completed == 0


def test_cancel_orders_reports_a_refused_cancel_as_failed() -> None:
    client = _FakeTradingClient(
        orders=[
            _FakeOrder("o1", "AAPL", "buy"),
            _FakeOrder("o2", "AAPL", "buy"),
        ]
    )

    def _cancel(order_id: str) -> None:
        client.cancelled.append(order_id)
        if order_id == "o1":
            raise RuntimeError("broker refused the cancel")

    client.cancel_order_by_id = _cancel

    outcome = asyncio.run(
        _adapter(client).cancel_orders({"o1", "o2"}, symbol="AAPL")
    )

    assert client.cancelled == ["o1", "o2"]
    assert outcome.completed == 1
    assert len(outcome.failed) == 1 and outcome.failed[0].startswith("o1:")


def test_cancel_orders_matched_order_without_id_is_unidentified() -> None:
    client = _FakeTradingClient(orders=[])
    client.orders.append(
        SimpleNamespace(
            id=None, client_order_id="sig-x", symbol="AAPL", side="buy"
        )
    )

    outcome = asyncio.run(
        _adapter(client).cancel_orders({"sig-x"}, symbol="AAPL")
    )

    assert client.cancelled == []
    assert outcome.unidentified == ("order:'sig-x'",)

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
    status: object = "new"
    legs: list["_FakeOrder"] = field(default_factory=list)
    submitted_at: datetime | None = None
    #: What the broker reports as filled. ``object`` so a test can model a row the
    #: adapter cannot read (a cancelled partial fill carries a quantity, an order
    #: that never filled reads as zero, and a broker that cannot be asked says
    #: something that is not a number).
    filled_qty: object = 0.0


@dataclass
class _FakePosition:
    symbol: str
    qty: float
    avg_entry_price: float = 1.0
    current_price: float = 1.0
    unrealized_pl: float = 0.0


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
        status = str(getattr(getattr(filter, "status", None), "value", "open")).lower()
        if status != "open":
            # ``status=all``: filled parents come back with their legs nested.
            return (list(self.orders) + list(self.parents))[:limit]
        # ``status=open``: top-level working orders only, legs never included.
        return list(self.orders)[:limit]

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

    def cancel_order_by_id(self, order_id: str) -> None:
        self.cancelled.append(order_id)
        if self.cancel_settles:
            self.orders = [order for order in self.orders if order.id != order_id]
            return
        for order in self.orders:
            if order.id == order_id:
                order.status = "pending_cancel"

    def _reserved_qty(self, symbol: str) -> float:
        position = next((item for item in self.positions if item.symbol == symbol), None)
        if position is None:
            return 0.0
        side = "sell" if position.qty > 0 else "buy"
        reserved = sum(
            abs(order.qty)
            for order in self.orders
            if order.symbol == symbol and order.side == side
        )
        # A bracket leg the broker lists only under its filled parent reserves
        # quantity too: that is what refuses a close_position on the bracket. Only
        # while it rests - a filled or settled leg reserves nothing. A leg can arrive
        # without its own symbol, and the parent it rests under is what places it:
        # the reservation is on the position's symbol either way.
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
        return SimpleNamespace(id=f"close-{symbol}", symbol=symbol)

    def submit_order(self, order_data):
        if self.submit_error is not None:
            raise self.submit_error
        self.submitted.append(order_data)
        side = str(getattr(order_data.side, "value", order_data.side))
        order = _FakeOrder(
            id=f"restored-{len(self.submitted)}",
            symbol=str(order_data.symbol),
            side=side,
            order_type="stop",
            qty=float(order_data.qty),
            stop_price=float(order_data.stop_price),
        )
        self.orders.append(order)
        return SimpleNamespace(id=order.id, symbol=order.symbol)


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
        return SimpleNamespace(id=f"close-{symbol}", symbol=symbol)

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

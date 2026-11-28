"""A stateful Alpaca REST double for the Alpaca trading-client seam.

``AlpacaLiveAdapter`` talks to the broker through the ``alpaca-py``
``TradingClient``: every read, write, cancel and close goes through
``self._trading_client``. :class:`FakeAlpacaTradingClient` is a drop-in for
that object, and it keeps broker state rather than answering a fixed script:

* **an order book.** ``submit_order`` accepts an order into ``pending_new``,
  acknowledges it, and only then fills it. The fill happens on
  :meth:`advance`, exactly as it happens in production through the trade
  stream - so an adapter that reads the submit response as "filled" fails
  here, and so does one that never re-reads the book.
* **working orders.** ``get_orders`` honours ``status``, ``symbols``,
  ``nested``, ``limit``, ``until`` and ``after``, and pages like the real
  listing does, so ``_list_orders_status_all``'s pagination is exercised
  rather than mocked away.
* **netting positions.** ``buy`` then ``sell`` nets; a full close drops the
  row, as Alpaca does.
* **bracket/OTO legs.** The legs are child orders with their own broker ids,
  nested under the parent in every ``nested=True`` listing - including in the
  ``submit_order`` response, the only place ``_register_bracket_legs`` ever
  learns them.
* **partial fills**, driven by ``partial_fill_ratio``.
* **rejections**: before acceptance (an ``APIError``, the shape Alpaca uses for
  insufficient buying power) and after it (an order the broker acked and then
  rejected).
* **DAY vs GTC.** A DAY order expires at the end of its session; a GTC one
  survives it. ``advance`` walks the fake clock.
* **the quantity-reservation rule.** A close is refused while a working
  reducing-side order still reserves the position's quantity - the rule that
  makes the flatten cancel its protections first.

Usage::

    from tests.fakes.alpaca import FakeAlpacaTradingClient, fake_alpaca_transport

    with fake_alpaca_transport() as (client, _stream, _data):
        adapter = AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
        asyncio.run(adapter.connect())            # the REST client IS the fake
        broker_order_id = await adapter.submit_order(signal, qty=5)
        client.advance(1.0)                       # the broker acknowledges, then fills
        adapter._on_trade_update(client.trade_updates[-1])   # what the stream delivers

Everything the fake records (``trade_updates``, ``submissions``,
``cancels``) is plain Python, so a test can assert on broker behaviour instead
of on which private field the adapter happened to set.
"""

from __future__ import annotations

import math
import sys
import types
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import (
    AssetClass,
    OrderClass,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)

#: alpaca-py's server-side default page size for the orders listing. The fake
#: refuses to hand back more than this per call unless the request asks for a
#: higher ``limit``, because the real listing does not either.
ALPACA_DEFAULT_PAGE_SIZE = 50

#: Every status a stopped book reports as "not working".
_TERMINAL_ORDER_STATUSES = frozenset(
    {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced"}
)

#: Statuses that still hold quantity against a position.
_RESERVING_ORDER_STATUSES = frozenset(
    {"new", "accepted", "partially_filled", "pending_new", "held", "pending_cancel"}
)


def _text(raw: Any) -> str:
    """Lower-case text of an alpaca-py enum or a plain string, ``Enum.`` prefix dropped."""
    return str(getattr(raw, "value", raw) or "").strip().lower().rsplit(".", 1)[-1]


def _float(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return value if value == value and value not in {float("inf"), float("-inf")} else 0.0


def _optional_float(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value == value and abs(value) != float("inf") else None


def _seq(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set, frozenset)):
        return list(raw)
    return [raw]


@dataclass
class FakeAlpacaAccount:
    """The account row ``get_account`` returns."""

    cash: float | str = 100_000.0
    equity: float | str = 100_000.0
    currency: str = "USD"
    multiplier: str = "2"
    buying_power: float | str = 200_000.0
    pattern_day_trader: bool = False
    account_blocked: bool = False
    status: str = "ACTIVE"


@dataclass
class FakeAlpacaPositionRow:
    """A broker position row shaped like ``alpaca-py``'s ``Position``."""

    symbol: str
    qty: float | str
    avg_entry_price: float | str = 0.0
    current_price: float | str = 0.0
    unrealized_pl: float | str = 0.0
    unrealized_plpc: float | str = 0.0
    market_value: float | str = 0.0
    asset_class: Any = AssetClass.US_EQUITY


@dataclass
class FakeAlpacaOrder:
    """A broker order row shaped like ``alpaca-py``'s ``Order``.

    ``status``/``side``/``order_type``/``order_class``/``time_in_force`` carry
    the real ``alpaca-py`` enums, because the adapter accepts either an enum or
    a string and a double that only ever produced strings would stop testing
    the enum half.
    """

    id: str
    client_order_id: str
    symbol: str
    side: Any = OrderSide.BUY
    qty: float = 0.0
    notional: float | None = None
    order_type: Any = OrderType.MARKET
    order_class: Any = OrderClass.SIMPLE
    time_in_force: Any = TimeInForce.GTC
    limit_price: float | None = None
    stop_price: float | None = None
    status: Any = OrderStatus.PENDING_NEW
    asset_class: Any = AssetClass.US_EQUITY
    extended_hours: bool = False
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    canceled_at: datetime | None = None
    expired_at: datetime | None = None
    failed_at: datetime | None = None
    replaced_at: datetime | None = None
    #: The id of the order this one replaced (Alpaca gives a replacement a new id).
    replaces: str | None = None
    #: The id the broker assigned to this order's replacement.
    replaced_by: str | None = None
    #: ``filled_qty``/``filled_avg_price`` are CUMULATIVE, as alpaca-py reports them.
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    #: Bracket/OTO legs, nested under the parent in a ``nested=True`` listing.
    legs: list[FakeAlpacaOrder] = field(default_factory=list)
    #: The parent's id for a leg, ``None`` for a top-level order.
    parent_order_id: str | None = None
    #: Why the broker refused an order it had acked.
    failed_reason: str | None = None
    #: Whether this order fills as soon as the broker has it, as a
    #: ``close_position`` market order does.
    fills_immediately: bool = False


@dataclass
class FakeAlpacaTradeUpdate:
    """One trade update, as the trading stream delivers it.

    ``order`` is a snapshot of the broker row *at the moment of the fill*, so
    ``filled_qty`` on it is cumulative - the property the adapter's dedupe key
    and the "do not persist cumulative as per-event" guard both rely on.
    """

    event: str
    order: FakeAlpacaOrder
    timestamp: datetime
    price: float | None = None
    qty: float | None = None
    execution_id: str | None = None


class FakeAlpacaTradingClient:
    """A broker double that behaves like Alpaca's REST trading client.

    Parameters are deliberately broker-shaped rather than test-shaped: a knob
    exists here when the broker has a corresponding behaviour.
    """

    def __init__(
        self,
        *,
        account: FakeAlpacaAccount | None = None,
        positions: Sequence[FakeAlpacaPositionRow] | None = None,
        orders: Sequence[FakeAlpacaOrder] | None = None,
        page_size: int = ALPACA_DEFAULT_PAGE_SIZE,
        mark_prices: Mapping[str, float] | None = None,
        #: Fake seconds between an order arriving and the broker acknowledging it.
        acknowledgement_seconds: float = 0.0,
        #: Fake seconds between acknowledgement and a marketable order filling.
        fill_seconds: float = 1.0,
        #: Share of the requested quantity a single fill delivers. Below 1.0 the
        #: order fills in chunks, each one its own trade update.
        partial_fill_ratio: float = 1.0,
        #: Refuse a submission before the broker accepts it, like a 403.
        reject_submissions: Callable[[Any], str | None] | Sequence[str] | None = None,
        #: Refuse a submission with an ``APIError`` rather than a rejected row.
        reject_with_api_error: bool = True,
        #: Accept a submission, then reject it on a later ``advance``.
        reject_after_acknowledgement: Callable[[Any], str | None] | Sequence[str] | None = None,
        #: Whether a cancel the broker accepted settles immediately. ``False``
        #: leaves the order in ``pending_cancel`` until it does.
        cancel_settles: bool = True,
        #: Fail every close, whatever the book says (a broker outage).
        close_error: Exception | None = None,
        #: Fail every submit outright (a transport failure, not a refusal).
        submit_error: Exception | None = None,
        #: Whether the book accepts short sales (crypto/two-way accounts) or not.
        allow_shorting: bool = False,
    ) -> None:
        self.account = account or FakeAlpacaAccount()
        self.page_size = page_size
        self.acknowledgement_seconds = acknowledgement_seconds
        self.fill_seconds = fill_seconds
        self.partial_fill_ratio = partial_fill_ratio
        # Sequences are consumed in order: normalize to a list the rule pop
        # can drain, so ``["first", "second"]`` refuses the first matching
        # submission with "first" and the next with "second".
        self.reject_submissions = (
            list(reject_submissions)
            if reject_submissions is not None and not callable(reject_submissions)
            else reject_submissions
        )
        self.reject_with_api_error = reject_with_api_error
        self.reject_after_acknowledgement = (
            list(reject_after_acknowledgement)
            if reject_after_acknowledgement is not None
            and not callable(reject_after_acknowledgement)
            else reject_after_acknowledgement
        )
        self.cancel_settles = cancel_settles
        self.close_error = close_error
        self.submit_error = submit_error
        self.allow_shorting = allow_shorting
        self.mark_prices: dict[str, float] = dict(mark_prices or {})

        self.now = datetime(2026, 1, 1, 14, 30, tzinfo=timezone.utc)
        self.orders: list[FakeAlpacaOrder] = list(orders or [])
        self.positions: dict[str, FakeAlpacaPositionRow] = {
            str(row.symbol).strip().upper(): row for row in (positions or [])
        }
        self.trade_updates: list[FakeAlpacaTradeUpdate] = []
        self.submissions: list[FakeAlpacaOrder] = []
        self.cancels: list[str] = []
        self.replacements: list[FakeAlpacaOrder] = []
        self.close_calls: list[str] = []
        self.order_filters: list[Any] = []
        self._seq = 0
        self._execution_seq = 0

    # ── the broker clock ──────────────────────────────────────────────────────

    def set_mark_price(self, symbol: str, price: float) -> None:
        """Move a symbol's mark price, which is what a limit/stop fills against."""
        self.mark_prices[str(symbol).strip().upper()] = float(price)

    def advance(self, seconds: float = 0.0) -> list[FakeAlpacaTradeUpdate]:
        """Run the broker forward and return the trade updates it emitted.

        An order that only ever reads as ``filled`` on the very next lookup is a
        double that cannot catch an adapter which treats its submit response as
        the fill, so this is the only place fills happen - a ``close_position``
        market order excepted, which the broker fills inside the call.
        """
        if seconds:
            self.now = self.now + timedelta(seconds=seconds)
        emitted: list[FakeAlpacaTradeUpdate] = []
        for order in sorted(self.book(), key=_submitted_at_key):
            if _text(order.status) == "pending_new" and _is_due(
                order.submitted_at, self.acknowledgement_seconds, self.now
            ):
                order.status = OrderStatus.ACCEPTED
            if _text(order.status) in {"accepted", "new", "partially_filled"}:
                # A refusal the broker makes AFTER it acked the order only
                # lands once the acknowledgement has settled: the submit
                # response says ``accepted`` and a later read says
                # ``rejected``. That gap is what production sees for an order
                # the broker accepts and then refuses.
                if _strictly_after(order.submitted_at, self.now):
                    order.failed_reason = _rule_text(
                        self.reject_after_acknowledgement, _request_like(order)
                    )
                if order.failed_reason is not None:
                    order.status = OrderStatus.REJECTED
                    order.failed_at = self.now
                    continue
            if _text(order.status) in {"accepted", "new", "partially_filled"}:
                emitted.extend(self._maybe_fill(order))
            if _is_day(order) and _text(order.status) in ("new", "accepted", "partially_filled"):
                if _session_ended(order.submitted_at, self.now):
                    order.status = OrderStatus.EXPIRED
                    order.expired_at = self.now
        self.trade_updates.extend(emitted)
        return emitted

    # ── listing ───────────────────────────────────────────────────────────────

    def get_orders(self, filter: Any = None) -> list[FakeAlpacaOrder]:  # noqa: A002
        """One page of the order listing, honouring every filter the adapter sets."""
        self.order_filters.append(filter)
        status = _text(getattr(filter, "status", "open"))
        symbols = {str(symbol).strip().upper() for symbol in _seq(getattr(filter, "symbols", None))}
        nested = bool(getattr(filter, "nested", False))
        until = getattr(filter, "until", None)
        after = getattr(filter, "after", None)
        requested_limit = getattr(filter, "limit", None)
        limit = int(requested_limit) if requested_limit else self.page_size
        direction = _text(getattr(filter, "direction", "desc")) or "desc"

        if status == "all":
            # Everything the account ever submitted, terminal orders included.
            page = list(self.orders)
        else:
            page = [
                order
                for order in self.orders
                if _text(order.status) not in _TERMINAL_ORDER_STATUSES
            ]
        if symbols:
            page = [order for order in page if order.symbol.upper() in symbols]
        if until is not None:
            page = [
                order
                for order in page
                if _submitted_at(order) is None or _submitted_at(order) <= until
            ]
        if after is not None:
            page = [
                order
                for order in page
                if _submitted_at(order) is None or _submitted_at(order) >= after
            ]
        page.sort(key=_submitted_at_key, reverse=direction == "desc")
        page = page[:limit]
        if not nested:
            # Alpaca only nests legs when the request asks for them: a flat
            # listing returns the parent rows with an empty ``legs`` list, so
            # a caller that forgot ``nested=True`` cannot see them here either.
            return [replace(order, legs=[]) for order in page]
        return page

    def get_order_by_id(self, order_id: str) -> FakeAlpacaOrder:
        for order in self.book():
            if order.id == order_id:
                return order
        raise _not_found(order_id)

    def get_order_by_client_id(self, client_order_id: str) -> FakeAlpacaOrder:
        matches = [order for order in self.book() if order.client_order_id == client_order_id]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise _not_found(f"client_order_id={client_order_id}")
        raise APIError(
            {
                "code": 409,
                "message": f"multiple orders match client_order_id={client_order_id}",
            }
        )

    def get_all_positions(self) -> list[FakeAlpacaPositionRow]:
        return [row for row in self.positions.values() if _float(row.qty) != 0.0]

    def get_account(self) -> FakeAlpacaAccount:
        return self.account

    # ── writes ────────────────────────────────────────────────────────────────

    def submit_order(self, order_data: Any = None) -> FakeAlpacaOrder:
        """Accept an order into the book and acknowledge it. Never fills it."""
        if self.submit_error is not None:
            raise self.submit_error
        refusal = _rule_text(self.reject_submissions, order_data)
        if refusal is not None:
            if self.reject_with_api_error:
                raise APIError({"code": 40310000, "message": refusal})
            return self._reject(order_data, refusal)
        order = self._new_order(order_data)
        self.submissions.append(order)
        self.orders.append(order)
        self._attach_legs(order_data, order)
        # The acknowledgement happens inside the submit call, as it does at the
        # broker; the fill never does - that is the point of the fake clock.
        self.advance(0.0)
        return order

    def cancel_order_by_id(self, order_id: str) -> None:
        self._cancel(order_id)

    def cancel_order(self, order_id: str | None = None) -> None:
        if order_id is None:
            # ``cancel_order()`` with no id sweeps the book: every working
            # order goes, legs included.
            for order in self.working_orders():
                self._cancel(order.id)
            return
        self._cancel(str(order_id))

    def replace_order_by_id(self, order_id: str, order_data: Any = None) -> FakeAlpacaOrder:
        """Replace a working order; Alpaca gives the replacement a NEW order id."""
        original = self.get_order_by_id(order_id)
        if _text(original.status) in _TERMINAL_ORDER_STATUSES:
            raise _not_found(order_id)
        stop_price = _optional_float(getattr(order_data, "stop_price", None))
        limit_price = _optional_float(getattr(order_data, "limit_price", None))
        qty = _optional_float(getattr(order_data, "qty", None))
        replacement = FakeAlpacaOrder(
            id=self._next_order_id(),
            client_order_id=original.client_order_id,
            symbol=original.symbol,
            side=original.side,
            qty=qty if qty is not None else original.qty,
            order_type=original.order_type,
            order_class=original.order_class,
            time_in_force=getattr(order_data, "time_in_force", None) or original.time_in_force,
            limit_price=limit_price if limit_price is not None else original.limit_price,
            stop_price=stop_price if stop_price is not None else original.stop_price,
            status=OrderStatus.NEW,
            asset_class=original.asset_class,
            extended_hours=original.extended_hours,
            submitted_at=self.now,
            legs=list(original.legs),
            parent_order_id=original.parent_order_id,
            replaces=original.id,
        )
        original.status = OrderStatus.REPLACED
        original.replaced_by = replacement.id
        original.replaced_at = self.now
        if replacement.parent_order_id is not None:
            parent = self.get_order_by_id(replacement.parent_order_id)
            parent.legs = [replacement if leg is original else leg for leg in parent.legs]
        self.orders.append(replacement)
        self.replacements.append(replacement)
        return replacement

    def close_position(self, symbol: str, close_options: Any = None) -> FakeAlpacaOrder:
        """Close a position, refusing while working orders reserve its quantity."""
        self.close_calls.append(symbol)
        if self.close_error is not None:
            raise self.close_error
        key = str(symbol).strip().upper()
        row = self.positions.get(key)
        if row is None or _float(row.qty) == 0.0:
            raise APIError({"code": 40410000, "message": f"position does not exist for {key}"})
        open_qty = _float(row.qty)
        if self.reserved_qty(key) > 0.0:
            # Alpaca: "unable to close position; there are working orders
            # reserving the quantity". This is why the flatten cancels its
            # protections first, so a double that always closes let a flatten
            # that forgets to cancel pass.
            raise APIError(
                {
                    "code": 40310000,
                    "message": (
                        f"unable to close position for {key}; "
                        "working orders reserve the quantity"
                    ),
                }
            )
        requested = _optional_float(getattr(close_options, "qty", None))
        percentage = _optional_float(getattr(close_options, "percentage", None))
        if requested is None and percentage is not None:
            requested = abs(open_qty) * (percentage / 100.0)
        if requested is None:
            requested = abs(open_qty)
        order = FakeAlpacaOrder(
            id=self._next_order_id(),
            client_order_id="",
            symbol=key,
            side=OrderSide.SELL if open_qty > 0 else OrderSide.BUY,
            qty=requested,
            order_type=OrderType.MARKET,
            order_class=OrderClass.SIMPLE,
            time_in_force=TimeInForce.DAY,
            status=OrderStatus.ACCEPTED,
            submitted_at=self.now,
            fills_immediately=True,
        )
        self.orders.append(order)
        self.submissions.append(order)
        # A ``close_position`` market order executes inside the call: Alpaca
        # hands back a filled order and nets the position before returning.
        self._fill(order, requested, self._mark_price(key))
        return order

    def close_all_positions(self, cancel_orders: bool = False) -> list[FakeAlpacaOrder]:
        if cancel_orders:
            self.cancel_order()
        return [self.close_position(row.symbol) for row in list(self.positions.values())]

    # ── the book, read back ───────────────────────────────────────────────────

    def book(self) -> list[FakeAlpacaOrder]:
        """Top-level orders plus their legs: the whole account book."""
        return [item for order in self.orders for item in (order, *order.legs)]

    def reserved_qty(self, symbol: str) -> float:
        """Quantity working orders hold against ``symbol``'s position.

        A bracket's two legs are one OCO pair, so together they reserve the
        parent's quantity once, not twice: whichever fires first removes its
        siblings. A market order does not reserve - it fills.
        """
        key = str(symbol).strip().upper()
        row = self.positions.get(key)
        if row is None:
            return 0.0
        reducing_side = OrderSide.SELL if _float(row.qty) > 0 else OrderSide.BUY
        reserved = 0.0
        for order in self.orders:
            if order.symbol.upper() != key or order.parent_order_id is not None:
                continue
            if order.side != reducing_side or _text(order.order_type) == "market":
                continue
            if _text(order.status) not in _RESERVING_ORDER_STATUSES:
                continue
            reserved += _float(order.qty) - _float(order.filled_qty)
        for parent in self.orders:
            if parent.symbol.upper() != key:
                continue
            # The OCO siblings count once, at the largest open quantity among
            # them: a partially filled leg plus its untouched sibling still
            # leaves only one execution's worth of quantity in play.
            siblings = [
                _float(leg.qty) - _float(leg.filled_qty)
                for leg in parent.legs
                if leg.side == reducing_side
                and _text(leg.order_type) != "market"
                and _text(leg.status) in _RESERVING_ORDER_STATUSES
            ]
            if siblings:
                reserved += max(siblings)
        return reserved

    def working_orders(self, symbol: str | None = None) -> list[FakeAlpacaOrder]:
        """Every order still on the book, legs included."""
        book = [
            order for order in self.book() if _text(order.status) not in _TERMINAL_ORDER_STATUSES
        ]
        if symbol is None:
            return book
        key = str(symbol).strip().upper()
        return [order for order in book if order.symbol.upper() == key]

    def position(self, symbol: str) -> float:
        """The signed netted quantity for ``symbol``; 0.0 when there is none."""
        row = self.positions.get(str(symbol).strip().upper())
        return 0.0 if row is None else _float(row.qty)

    # ── the book's internals ──────────────────────────────────────────────────

    def _new_order(self, request: Any) -> FakeAlpacaOrder:
        """Read a submission request as the book sees it.

        The broker stamps each submission in arrival order, and several arrive
        inside one fake tick: the clock steps a microsecond per submission so
        every order has its own ``submitted_at``, which is the only way the
        ``status=all`` pagination's ``until`` bound can move past a boundary.
        """
        self.now = self.now + timedelta(microseconds=1)
        return FakeAlpacaOrder(
            id=self._next_order_id(),
            client_order_id=str(getattr(request, "client_order_id", "") or ""),
            symbol=str(getattr(request, "symbol", "") or "").strip().upper(),
            side=getattr(request, "side", OrderSide.BUY),
            qty=_optional_float(getattr(request, "qty", None)) or 0.0,
            notional=_optional_float(getattr(request, "notional", None)),
            order_type=getattr(request, "type", OrderType.MARKET),
            order_class=getattr(request, "order_class", OrderClass.SIMPLE),
            time_in_force=getattr(request, "time_in_force", TimeInForce.GTC),
            limit_price=_optional_float(getattr(request, "limit_price", None)),
            stop_price=_optional_float(getattr(request, "stop_price", None)),
            status=OrderStatus.PENDING_NEW,
            asset_class=getattr(request, "asset_class", AssetClass.US_EQUITY),
            extended_hours=bool(getattr(request, "extended_hours", False)),
            submitted_at=self.now,
        )

    def _attach_legs(self, request: Any, parent: FakeAlpacaOrder) -> None:
        """Create a bracket/OTO's legs as the broker does: child orders of their own."""
        if _text(getattr(request, "order_class", OrderClass.SIMPLE)) not in {
            "bracket",
            "oto",
            "oco",
            "mleg",
        }:
            return
        stop_loss = getattr(request, "stop_loss", None)
        take_profit = getattr(request, "take_profit", None)
        substitutes = getattr(request, "legs", None) or []
        legs: list[Any] = [stop_loss, take_profit]
        legs.extend(substitutes)
        for leg in legs:
            if leg is None:
                continue
            leg_stop = _optional_float(getattr(leg, "stop_price", None))
            leg_limit = _optional_float(getattr(leg, "limit_price", None))
            parent.legs.append(
                FakeAlpacaOrder(
                    id=self._next_order_id(),
                    client_order_id="",
                    symbol=parent.symbol,
                    side=_opposite(parent.side),
                    qty=_float(parent.qty),
                    order_type=OrderType.LIMIT
                    if leg_limit is not None and leg_stop is None
                    else OrderType.STOP
                    if leg_stop is not None
                    else OrderType.LIMIT,
                    order_class=OrderClass.SIMPLE,
                    time_in_force=parent.time_in_force,
                    limit_price=leg_limit,
                    stop_price=leg_stop,
                    status=OrderStatus.HELD,
                    asset_class=parent.asset_class,
                    submitted_at=parent.submitted_at,
                    parent_order_id=parent.id,
                )
            )
        # Alpaca lists the stop-loss leg first and the take-profit second.
        parent.legs.sort(key=lambda leg: _text(leg.order_type) != "stop")

    def _next_order_id(self) -> str:
        self._seq += 1
        return f"fake-{self._seq:08d}"

    def _next_execution_id(self) -> str:
        self._execution_seq += 1
        return f"fake-exec-{self._execution_seq}"

    def _reject(self, request: Any, reason: str) -> FakeAlpacaOrder:
        order = self._new_order(request)
        order.status = OrderStatus.REJECTED
        order.failed_reason = reason
        order.failed_at = self.now
        self.orders.append(order)
        return order

    def _cancel(self, order_id: str) -> None:
        self.cancels.append(order_id)
        for order in self.book():
            if order.id != order_id:
                continue
            if _text(order.status) in _TERMINAL_ORDER_STATUSES:
                raise _not_found(order_id)
            if self.cancel_settles:
                order.status = OrderStatus.CANCELED
                order.canceled_at = self.now
            else:
                # Alpaca keeps an accepted cancel in the book as ``pending_cancel``
                # until it settles.
                order.status = OrderStatus.PENDING_CANCEL
            return
        raise _not_found(order_id)

    def _maybe_fill(self, order: FakeAlpacaOrder) -> list[FakeAlpacaTradeUpdate]:
        if _text(order.status) not in {"accepted", "new", "partially_filled"}:
            return []
        if not self._marketable(order):
            return []
        due_at = order.submitted_at or self.now
        if not order.fills_immediately and not _is_due(due_at, self.fill_seconds, self.now):
            return []
        remaining = _float(order.qty) - _float(order.filled_qty)
        if remaining <= 0.0:
            return []
        chunk = remaining
        ratio = self.partial_fill_ratio
        if 0.0 < ratio < 1.0:
            chunk = min(remaining, max(math.floor(remaining * ratio), 1.0))
        return self._fill(order, chunk, self._mark_price(order.symbol))

    def _marketable(self, order: FakeAlpacaOrder) -> bool:
        """Whether the mark price is on the order's fill side, as the exchange sees it."""
        if _text(order.order_type) == "market":
            return True
        mark = self._mark_price(order.symbol)
        buying = _text(order.side) == "buy"
        if order.limit_price is not None:
            return mark >= order.limit_price if buying else mark <= order.limit_price
        if order.stop_price is not None:
            return mark >= order.stop_price if buying else mark <= order.stop_price
        return False

    def _mark_price(self, symbol: str) -> float:
        return self.mark_prices.get(symbol.upper(), 100.0)

    def _fill(
        self, order: FakeAlpacaOrder, qty: float, price: float
    ) -> list[FakeAlpacaTradeUpdate]:
        if qty <= 0.0:
            return []
        # Net the position FIRST: a fill the book cannot take (a short sale on
        # a long-only account) raises before the order row is touched, so the
        # book never shows an order filled against a position that does not
        # exist.
        self._net_position(order.symbol, order.side, qty, price)
        previous = _float(order.filled_qty)
        cumulative = previous + qty
        order.filled_qty = cumulative
        order.filled_avg_price = (
            price
            if previous == 0.0
            else (previous * _float(order.filled_avg_price) + qty * price) / cumulative
        )
        order.filled_at = self.now
        if order.limit_price is None and order.stop_price is None:
            order.limit_price = price
        if cumulative >= _float(order.qty) - 1e-9:
            order.filled_qty = _float(order.qty)
            order.status = OrderStatus.FILLED
            _release_legs(order)
        else:
            order.status = OrderStatus.PARTIALLY_FILLED
        if _text(order.status) == "filled":
            self._cancel_oco_siblings(order)
        return [self._trade_update(order, qty, price)]

    def _trade_update(
        self, order: FakeAlpacaOrder, qty: float, price: float
    ) -> FakeAlpacaTradeUpdate:
        return FakeAlpacaTradeUpdate(
            event="fill" if _text(order.status) == "filled" else "partial_fill",
            order=replace(order),
            timestamp=self.now,
            price=price,
            qty=qty,
            execution_id=self._next_execution_id(),
        )

    def _net_position(self, symbol: str, side: Any, qty: float, price: float) -> None:
        key = symbol.upper()
        sign = 1.0 if _text(side) == "buy" else -1.0
        row = self.positions.get(key)
        before = 0.0 if row is None else _float(row.qty)
        after = before + sign * qty
        if not self.allow_shorting and after < 0.0 and before >= 0.0:
            # A netted book refuses the sell outright, and no position row is
            # invented for it: a fill that cannot happen records nothing.
            raise APIError(
                {"code": 40310000, "message": f"short selling is not permitted for {key}"}
            )
        if row is None:
            row = FakeAlpacaPositionRow(symbol=key, qty=0.0, asset_class=AssetClass.US_EQUITY)
            self.positions[key] = row
        if after == 0.0:
            # Alpaca drops a flat position from its listing.
            self.positions.pop(key, None)
            return
        if sign > 0.0:
            row.avg_entry_price = (
                (before * _float(row.avg_entry_price) + qty * price) / after
                if before > 0.0
                else price
            )
        row.qty = after
        row.current_price = price
        row.market_value = after * price
        row.unrealized_pl = after * (price - _float(row.avg_entry_price))

    def _cancel_oco_siblings(self, filled: FakeAlpacaOrder) -> None:
        """Remove a filled bracket leg's siblings: OCO, one order is all that runs."""
        if filled.parent_order_id is None:
            return
        for parent in self.orders:
            if parent.id != filled.parent_order_id:
                continue
            for leg in parent.legs:
                if leg is not filled and _text(leg.status) not in _TERMINAL_ORDER_STATUSES:
                    leg.status = OrderStatus.CANCELED
            return


def _release_legs(parent: FakeAlpacaOrder) -> None:
    """When a bracket/OTO parent fills, its held legs go working."""
    for leg in parent.legs:
        if _text(leg.status) == "held":
            leg.status = OrderStatus.NEW
            leg.submitted_at = parent.filled_at or leg.submitted_at
            leg.qty = _float(parent.filled_qty) or _float(parent.qty)


def _opposite(side: Any) -> Any:
    return OrderSide.SELL if _text(side) == "buy" else OrderSide.BUY


def _submitted_at(order: FakeAlpacaOrder) -> datetime | None:
    return order.submitted_at


def _submitted_at_key(order: FakeAlpacaOrder) -> datetime:
    return _submitted_at(order) or datetime.min.replace(tzinfo=timezone.utc)


def _is_due(submitted_at: datetime | None, delay: float, now: datetime) -> bool:
    if submitted_at is None:
        return True
    return now >= submitted_at + timedelta(seconds=delay)


def _strictly_after(submitted_at: datetime | None, now: datetime) -> bool:
    return submitted_at is not None and now > submitted_at


def _is_day(order: FakeAlpacaOrder) -> bool:
    return _text(order.time_in_force) == "day"


def _session_ended(submitted_at: datetime | None, now: datetime) -> bool:
    """Whether the broker clock moved past the day ``submitted_at`` fell on."""
    if submitted_at is None:
        return False
    return now.date() > submitted_at.date()


def _not_found(order_id: Any) -> APIError:
    return APIError({"code": 40410000, "message": f"order {order_id} not found"})


def _rule_text(
    rule: Callable[[Any], str | None] | Sequence[str] | None, request: Any
) -> str | None:
    if rule is None:
        return None
    if callable(rule):
        try:
            text = rule(request)
        except Exception:  # pragma: no cover - a rejection rule must not break the broker
            return None
        return str(text) if text else None
    # ``__init__`` normalizes sequences to lists so the refusal is consumed
    # in order; a bare tuple reaches here only if the attribute was reassigned.
    if isinstance(rule, list):
        return str(rule.pop(0)) if rule else None
    return None


def _request_like(order: FakeAlpacaOrder) -> Any:
    """A read-only view of an order for a rejection rule to inspect."""
    return types.SimpleNamespace(
        symbol=order.symbol,
        side=order.side,
        qty=order.qty,
        order_type=order.order_type,
        order_class=order.order_class,
        time_in_force=order.time_in_force,
        stop_price=order.stop_price,
        limit_price=order.limit_price,
        client_order_id=order.client_order_id,
    )


# ── wiring the fake into the adapter's transport seam ────────────────────────


class FakeTradingStream:
    """The stream half of ``connect``: it records what the broker pushed."""

    def __init__(self) -> None:
        self.subscribed = False
        self.stopped = False
        self.handler: Any = None

    def subscribe_trade_updates(self, handler: Any) -> None:
        self.subscribed = True
        self.handler = handler

    def stop(self) -> None:
        self.stopped = True

    def deliver(self, client: FakeAlpacaTradingClient, index: int = -1) -> None:
        """Hand one recorded trade update to the stream handler, as the socket would."""
        if self.handler is None:
            raise AssertionError("the fake stream has no trade-update handler subscribed")
        self.handler(client.trade_updates[index])


class FakeHistoricalDataClient:
    """The market-data half of ``connect``."""

    def __init__(self) -> None:
        self.bar_requests: list[Any] = []

    def get_stock_bars(self, request: Any) -> Any:
        self.bar_requests.append(request)
        return None


def _stub_module(name: str, **attributes: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


@contextmanager
def fake_alpaca_transport(
    client: FakeAlpacaTradingClient | None = None,
) -> Iterator[tuple[FakeAlpacaTradingClient, FakeTradingStream, FakeHistoricalDataClient]]:
    """Install the fake at the seam ``AlpacaLiveAdapter.connect`` builds on.

    ``connect`` imports ``TradingClient``, ``TradingStream`` and
    ``StockHistoricalDataClient`` from ``alpaca``, so substituting those three
    names in ``sys.modules`` is enough for the adapter to build the fake REST
    client itself - no adapter private field is touched, and a test that forgets
    to wire the client fails inside ``connect`` rather than passing against
    nothing.

    Yields ``(trading_client, trading_stream, historical_data_client)``.
    """
    client = client or FakeAlpacaTradingClient()
    stream = FakeTradingStream()
    historical = FakeHistoricalDataClient()
    names = ("alpaca.trading.client", "alpaca.trading.stream", "alpaca.data.historical")
    saved = {name: sys.modules.get(name) for name in names}
    sys.modules["alpaca.trading.client"] = _stub_module(
        "alpaca.trading.client", TradingClient=lambda *args, **kwargs: client
    )
    sys.modules["alpaca.trading.stream"] = _stub_module(
        "alpaca.trading.stream", TradingStream=lambda *args, **kwargs: stream
    )
    sys.modules["alpaca.data.historical"] = _stub_module(
        "alpaca.data.historical",
        StockHistoricalDataClient=lambda *args, **kwargs: historical,
    )
    try:
        yield client, stream, historical
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def wire_alpaca_adapter(
    adapter: Any, client: FakeAlpacaTradingClient | None = None
) -> FakeAlpacaTradingClient:
    """Attach a fake REST client to an already-built adapter.

    The escape hatch for a test that does not drive ``connect`` (one whose
    adapter needs no broker talk, say). It exists so the two private
    assignments live in exactly one place.
    """
    client = client or FakeAlpacaTradingClient()
    adapter._trading_client = client
    adapter._connected = True
    return client


__all__ = [
    "ALPACA_DEFAULT_PAGE_SIZE",
    "FakeAlpacaAccount",
    "FakeAlpacaOrder",
    "FakeAlpacaPositionRow",
    "FakeAlpacaTradeUpdate",
    "FakeAlpacaTradingClient",
    "FakeHistoricalDataClient",
    "FakeTradingStream",
    "fake_alpaca_transport",
    "wire_alpaca_adapter",
]

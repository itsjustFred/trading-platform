"""A stateful cTrader OpenAPI double for the ``client.send`` seam.

``CTraderLiveAdapter`` talks to the broker through one object: the
``ctrader_open_api.Client`` it builds in ``_create_client``. Every request is a
protobuf message handed to ``client.send(request, clientMsgId=...,
responseTimeoutInSeconds=...)``, which returns a Twisted ``Deferred`` that the
server answers with a ``ProtoMessage`` (payload + ``payloadType`` +
``clientMsgId``). Server-initiated events (fills, rejects, spots) arrive on the
same channel through the message-received callback.

:class:`FakeCTraderClient` is a drop-in for that client, and
:class:`FakeCTraderBroker` keeps the broker state behind it:

* **an order book.** ``ProtoOANewOrderReq`` is acknowledged with an
  ``ORDER_ACCEPTED`` execution event; the fill only happens on
  :meth:`FakeCTraderBroker.advance`, so an adapter that treats the submit
  response as a fill fails here.
* **positions, netted or hedged** (``account_type``): a netted account merges
  opposite fills on one symbol; a hedged account opens a new ``positionId``
  per entry.
* **partial fills**, driven by ``partial_fill_ratio``.
* **rejections**: before acceptance (a ``ProtoOAOrderErrorEvent`` response)
  and after it (an accepted order that a later ``advance`` rejects with an
  ``ORDER_REJECTED`` execution event - the shape #428 is about).
* **DAY vs GTC**: cTrader has no DAY; ``GOOD_TILL_DATE`` orders expire at
  their ``expirationTimestamp``, ``GOOD_TILL_CANCEL`` ones survive.
* **bracket/OCO protection as position SL/TP**: ``relativeStopLoss`` /
  ``relativeTakeProfit`` on the entry become absolute ``stopLoss`` /
  ``takeProfit`` on the position at fill time, and a mark crossing either one
  closes the position with a closing order and a deal.
* **paginated listings**: ``ProtoOADealListReq`` honours ``maxRows`` and
  reports ``hasMore``; ``ProtoOAOrderListReq`` pages at ``page_size``.

Usage::

    from tests.fakes.ctrader import (
        FakeCTraderBroker, FakeCTraderClient, fake_ctrader_transport,
    )

    broker = FakeCTraderBroker(account_id=123, symbols=[...])
    with fake_ctrader_transport(broker) as client:
        adapter = CTraderLiveAdapter(...)
        asyncio.run(adapter.connect())      # auth + symbols + reconcile run on the fake
        await adapter.submit_order(signal, qty=...)
        broker.advance(1.0)                 # the broker fills; events reach _on_message

Everything the fake records (``requests``, ``events``, ``deals``) is plain
Python, so a test asserts on broker behaviour rather than on which private
field the adapter happened to set.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from twisted.internet import defer

# ``ctrader_open_api`` is imported LAZILY, never at module level: its
# ``__init__`` pulls in ``Client``, which does ``from twisted.internet import
# reactor`` and installs the *default* reactor as a side effect. A test module
# that merely imports this fake must not decide which reactor the process gets
# - ``CTraderLiveAdapter.connect`` installs the asyncio reactor first and only
# then imports the package. ``_ensure_types`` runs on first broker/client use,
# by which point the caller has already picked a reactor, and it imports under
# ``account_discovery._without_installed_reactor`` so a fake-only test does not
# pin the platform reactor either.
_m: Any = None
_mm: Any = None
ProtoMessage: Any = None
_Protobuf: Any = None


def _ensure_types() -> None:
    global _m, _mm, ProtoMessage, _Protobuf
    if _m is not None:
        return
    # Deferred: account_discovery imports the alpaca adapter chain.
    from live_runner.account_discovery import _without_installed_reactor

    with _without_installed_reactor():
        from ctrader_open_api.messages import OpenApiMessages_pb2 as messages
        from ctrader_open_api.messages import OpenApiModelMessages_pb2 as model
        from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import (
            ProtoMessage as proto_message,
        )
        from ctrader_open_api.protobuf import Protobuf

    _m = messages
    _mm = model
    ProtoMessage = proto_message
    _Protobuf = Protobuf

_MS_PER_SECOND = 1000

#: cTrader prices and relative SL/TP distances are carried in 1/100000 units.
_RELATIVE_PRICE_UNIT = 100_000

_ACCOUNT_TYPE_HEDGED = 0
_ACCOUNT_TYPE_NETTED = 1

_ORDER_STATUS_ACCEPTED = 1
_ORDER_STATUS_FILLED = 2
_ORDER_STATUS_REJECTED = 3
_ORDER_STATUS_EXPIRED = 4
_ORDER_STATUS_CANCELLED = 5

_POSITION_STATUS_OPEN = 1
_POSITION_STATUS_CLOSED = 2

_DEAL_STATUS_FILLED = 2

_SIDE_BUY = 1
_SIDE_SELL = 2

_EXECUTION_ORDER_ACCEPTED = 2
_EXECUTION_ORDER_FILLED = 3
_EXECUTION_ORDER_REPLACED = 4
_EXECUTION_ORDER_CANCELLED = 5
_EXECUTION_ORDER_EXPIRED = 6
_EXECUTION_ORDER_REJECTED = 7
_EXECUTION_ORDER_PARTIAL_FILL = 11

_ORDER_TYPE_MARKET = 1
_ORDER_TYPE_LIMIT = 2
_ORDER_TYPE_STOP = 3
_ORDER_TYPE_SLTP = 4

_TIF_GOOD_TILL_DATE = 1
_TIF_GOOD_TILL_CANCEL = 2
_TIF_IMMEDIATE_OR_CANCEL = 3


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _has(msg: Any, name: str) -> bool:
    has_field = getattr(msg, "HasField", None)
    if has_field is None:
        return getattr(msg, name, None) not in (None, 0, 0.0, "")
    try:
        return bool(has_field(name))
    except (TypeError, ValueError):
        return False


@dataclass
class FakeCTraderSymbol:
    """One tradable symbol, as ``ProtoOALightSymbol`` + ``ProtoOASymbol`` see it.

    ``volume_per_qty`` is the broker's ``lotSize``: cTrader volumes are in
    CENTS of the base asset, so the adapter's ``qty`` units map through
    ``volume_per_qty = lotSize / 100``. Defaults are the demo-verified EURUSD
    values from ``ctrader_adapter``'s volume contract (#184): ``lotSize``
    10_000_000 (100,000 EUR), a 0.01-lot minimum of 100_000 and a matching
    100_000 step - NOT the static fallback table's base-unit values, which
    sit 100x below the broker's cents.
    """

    name: str
    symbol_id: int
    digits: int = 5
    lot_size: float = 10_000_000.0
    min_volume: int = 100_000
    max_volume: int = 1_000_000_000
    step_volume: int = 100_000
    base_asset_id: int = 1
    quote_asset_id: int = 2
    leverage_id: int = 0
    enabled: bool = True

    @classmethod
    def fx(cls, name: str, symbol_id: int, **kwargs: Any) -> "FakeCTraderSymbol":
        return cls(name=name, symbol_id=symbol_id, **kwargs)


@dataclass
class _FakeOrder:
    """A working or terminal order: the proto row plus broker-side bookkeeping."""

    proto: Any
    symbol_name: str
    symbol_id: int
    accepted_ms: int
    limit_price: float | None = None
    stop_price: float | None = None
    relative_stop: int | None = None
    relative_take: int | None = None
    absolute_stop: float | None = None
    absolute_take: float | None = None
    target_position_id: int = 0
    executed_volume: int = 0
    avg_price: float = 0.0
    #: The position this order's fills land on, bound at the first fill so a
    #: hedged account's partial fills accumulate into ONE position.
    bound_position_id: int = 0
    bound_closing: bool = False


@dataclass
class _FakePosition:
    """An open position: the proto row, whose ``price`` IS the entry.

    ``ProtoOAPosition.price`` carries the position's weighted average entry
    price - the same value ``ReconcileReq`` and execution events report - so
    the fake keeps it there rather than inventing a side channel.
    """

    proto: Any
    symbol_name: str
    symbol_id: int

    @property
    def entry_price(self) -> float:
        return _float(getattr(self.proto, "price", 0.0))


class FakeCTraderBroker:
    """The broker's brain: book, positions, clock, and the fill pipeline.

    Parameters are broker-shaped: ``reject_submissions`` is a callable that
    inspects the ``ProtoOANewOrderReq`` and returns a refusal reason (or
    ``None``); ``reject_after_acceptance`` does the same for an order the
    broker already acked, applied on the next :meth:`advance`.
    """

    def __init__(
        self,
        *,
        account_id: int = 123456,
        symbols: Sequence[FakeCTraderSymbol] = (),
        account_type: str = "netted",
        balance: float = 10_000.0,
        money_digits: int = 2,
        leverage_in_cents: int = 100_000,
        mark_prices: dict[str, float] | None = None,
        positions: Sequence[dict[str, Any]] | None = None,
        page_size: int = 50,
        fill_delay_ms: int = 0,
        partial_fill_ratio: float = 1.0,
        reject_submissions: Callable[[Any], str | None] | None = None,
        reject_after_acceptance: Callable[[Any], str | None] | None = None,
        cancel_error: Exception | None = None,
    ) -> None:
        self.account_id = int(account_id)
        self.account_type_name = str(account_type).strip().lower()
        self.balance_ccy = float(balance)
        self.money_digits = int(money_digits)
        self.leverage_in_cents = int(leverage_in_cents)
        self.page_size = int(page_size)
        self.fill_delay_ms = int(fill_delay_ms)
        self.partial_fill_ratio = float(partial_fill_ratio)
        self.reject_submissions = reject_submissions
        self.reject_after_acceptance = reject_after_acceptance
        self.cancel_error = cancel_error

        self._symbols: dict[int, FakeCTraderSymbol] = {}
        self._symbol_by_name: dict[str, FakeCTraderSymbol] = {}
        for spec in symbols:
            self.add_symbol(spec)
        self.mark_prices: dict[str, float] = {
            str(k).strip().upper(): float(v) for k, v in (mark_prices or {}).items()
        }

        self.now_ms = 1_767_225_600_000  # 2026-01-01T00:00:00Z
        self.orders: dict[int, _FakeOrder] = {}
        self.positions: dict[int, _FakePosition] = {}
        self.deals: list[Any] = []
        self.events: list[Any] = []  # every execution event the broker pushed
        self.requests: list[Any] = []  # every request the client saw
        self._order_seq = 0
        self._position_seq = 0
        self._deal_seq = 0

        for spec in positions or []:
            self._seed_position(spec)

    # ── setup helpers ──────────────────────────────────────────────────────

    def add_symbol(self, spec: FakeCTraderSymbol) -> None:
        self._symbols[int(spec.symbol_id)] = spec
        self._symbol_by_name[spec.name.strip().upper()] = spec

    def set_mark_price(self, symbol: str, price: float) -> None:
        self.mark_prices[str(symbol).strip().upper()] = float(price)

    def mark_price(self, symbol_name: str) -> float:
        return self.mark_prices.get(symbol_name.strip().upper(), 100.0)

    def _symbol_for_id(self, symbol_id: int) -> FakeCTraderSymbol | None:
        return self._symbols.get(int(symbol_id))

    def _seed_position(self, spec: dict[str, Any]) -> None:
        """Seed an open position from a plain dict (contract-test convenience)."""
        _ensure_types()
        symbol = self._symbol_by_name.get(str(spec.get("symbol", "")).strip().upper())
        if symbol is None:
            raise KeyError(f"unknown symbol in seeded position: {spec.get('symbol')!r}")
        position_id = int(spec.get("position_id") or self._next_position_id())
        volume = int(spec.get("volume", spec.get("qty", 0)) )
        side = int(spec.get("side", _SIDE_BUY))
        entry = float(spec.get("entry_price", spec.get("price", self.mark_price(symbol.name))))
        proto = _mm.ProtoOAPosition()
        proto.positionId = position_id
        proto.positionStatus = _POSITION_STATUS_OPEN
        proto.price = entry
        proto.moneyDigits = self.money_digits
        proto.swap = 0
        td = proto.tradeData
        td.symbolId = symbol.symbol_id
        td.volume = volume
        td.tradeSide = side
        td.openTimestamp = self.now_ms
        if spec.get("stop_loss"):
            proto.stopLoss = float(spec["stop_loss"])
        if spec.get("take_profit"):
            proto.takeProfit = float(spec["take_profit"])
        self.positions[position_id] = _FakePosition(
            proto=proto,
            symbol_name=symbol.name,
            symbol_id=symbol.symbol_id,
        )

    def _next_order_id(self) -> int:
        self._order_seq += 1
        return 900_000 + self._order_seq

    def _next_position_id(self) -> int:
        self._position_seq += 1
        return 800_000 + self._position_seq

    def _next_deal_id(self) -> int:
        self._deal_seq += 1
        return 700_000 + self._deal_seq

    # ── the clock ──────────────────────────────────────────────────────────

    def advance(self, seconds: float = 0.0) -> list[Any]:
        """Run the broker forward; returns the execution events it pushed.

        This is the ONLY place fills happen: an order the broker acked fills
        here, a position's stop/take-profit triggers here, and a
        ``GOOD_TILL_DATE`` order expires here.
        """
        _ensure_types()
        if seconds:
            self.now_ms += int(seconds * _MS_PER_SECOND)
        emitted: list[Any] = []
        for record in list(self.orders.values()):
            if _int(record.proto.orderStatus) != _ORDER_STATUS_ACCEPTED:
                continue
            emitted.extend(self._advance_order(record))
        emitted.extend(self._advance_position_protection())
        self.events.extend(emitted)
        return emitted

    # ── order lifecycle ────────────────────────────────────────────────────

    def _advance_order(self, record: _FakeOrder) -> list[Any]:
        proto = record.proto
        tif = _int(getattr(proto, "timeInForce", _TIF_GOOD_TILL_CANCEL))
        if tif == _TIF_GOOD_TILL_DATE:
            expiry = _int(getattr(proto, "expirationTimestamp", 0))
            if expiry and self.now_ms > expiry:
                proto.orderStatus = _ORDER_STATUS_EXPIRED
                proto.utcLastUpdateTimestamp = self.now_ms
                return [self._execution_event(
                    _EXECUTION_ORDER_EXPIRED, order=proto, error_code="ORDER_EXPIRED"
                )]
        # A refusal the broker makes AFTER it acked the order lands on a later
        # tick: the submit response said ORDER_ACCEPTED and this read says
        # ORDER_REJECTED.
        reason = None
        if self.reject_after_acceptance is not None:
            try:
                reason = self.reject_after_acceptance(record.proto)
            except Exception:  # pragma: no cover - a rule must not break the broker
                reason = None
        if reason:
            proto.orderStatus = _ORDER_STATUS_REJECTED
            proto.utcLastUpdateTimestamp = self.now_ms
            return [self._execution_event(
                _EXECUTION_ORDER_REJECTED, order=proto, error_code="ORDER_REJECTED",
            )]
        if self.now_ms < record.accepted_ms + self.fill_delay_ms:
            return []
        if not self._marketable(record):
            if tif == _TIF_IMMEDIATE_OR_CANCEL:
                # IOC that cannot fill now is cancelled, not left working.
                proto.orderStatus = _ORDER_STATUS_CANCELLED
                proto.utcLastUpdateTimestamp = self.now_ms
                return [self._execution_event(
                    _EXECUTION_ORDER_CANCELLED, order=proto
                )]
            return []
        return self._fill_order(record)

    def _marketable(self, record: _FakeOrder) -> bool:
        order_type = _int(getattr(record.proto, "orderType", _ORDER_TYPE_MARKET))
        mark = self.mark_price(record.symbol_name)
        side_buy = _int(getattr(record.proto.tradeData, "tradeSide", _SIDE_BUY)) == _SIDE_BUY
        if order_type == _ORDER_TYPE_MARKET:
            return True
        if order_type == _ORDER_TYPE_LIMIT:
            limit = record.limit_price
            if limit is None:
                return False
            return mark <= limit if side_buy else mark >= limit
        if order_type == _ORDER_TYPE_STOP:
            stop = record.stop_price
            if stop is None:
                return False
            return mark >= stop if side_buy else mark <= stop
        return False

    def _fill_order(self, record: _FakeOrder) -> list[Any]:
        proto = record.proto
        volume = _int(getattr(proto.tradeData, "volume", 0))
        remaining = volume - record.executed_volume
        if remaining <= 0:
            return []
        chunk = remaining
        ratio = self.partial_fill_ratio
        if 0.0 < ratio < 1.0:
            chunk = min(remaining, max(math.floor(remaining * ratio), 1))
        price = self.mark_price(record.symbol_name)
        record.executed_volume += chunk
        record.avg_price = (
            price
            if record.executed_volume == chunk
            else (record.avg_price * (record.executed_volume - chunk) + price * chunk)
            / record.executed_volume
        )
        filled = record.executed_volume >= volume
        proto.executedVolume = record.executed_volume
        proto.executionPrice = record.avg_price
        proto.utcLastUpdateTimestamp = self.now_ms
        tif = _int(getattr(proto, "timeInForce", _TIF_GOOD_TILL_CANCEL))
        if filled:
            proto.orderStatus = _ORDER_STATUS_FILLED
        elif tif == _TIF_IMMEDIATE_OR_CANCEL:
            # IOC keeps only what it could fill immediately.
            proto.orderStatus = _ORDER_STATUS_FILLED
            filled = True
        position_id, is_closing = self._resolve_position_id(record)
        deal = self._record_deal(
            record, chunk, price, position_id=position_id, closing=is_closing
        )
        position = self._apply_fill(position_id, record, chunk, price, is_closing)
        execution_type = (
            _EXECUTION_ORDER_FILLED if filled else _EXECUTION_ORDER_PARTIAL_FILL
        )
        return [self._execution_event(
            execution_type,
            order=proto,
            deal=deal,
            position=position,
        )]

    def _resolve_position_id(self, record: _FakeOrder) -> tuple[int, bool]:
        """Which position a fill lands on, and whether it closes it.

        A closing order (``closingOrder`` or an explicit ``positionId``)
        targets that position; a hedged account opens a fresh ``positionId``
        per entry; a netted account reuses the symbol's open position.
        """
        if record.bound_position_id:
            return record.bound_position_id, record.bound_closing
        target = record.target_position_id or _int(
            getattr(record.proto, "positionId", 0)
        )
        if bool(_has(record.proto, "closingOrder")) or target > 0:
            resolved = (target, True)
        elif self.account_type_name == "hedged":
            resolved = (self._next_position_id(), False)
        else:
            existing = self._netted_position_for(record.symbol_id)
            resolved = (
                existing if existing is not None else self._next_position_id(),
                False,
            )
        record.bound_position_id, record.bound_closing = resolved
        return resolved

    def _record_deal(
        self,
        record: _FakeOrder,
        volume: int,
        price: float,
        *,
        position_id: int,
        closing: bool,
    ) -> Any:
        side = _int(getattr(record.proto.tradeData, "tradeSide", _SIDE_BUY))
        deal = _mm.ProtoOADeal()
        deal.dealId = self._next_deal_id()
        deal.orderId = _int(getattr(record.proto, "orderId", 0))
        deal.positionId = position_id
        deal.volume = int(volume)
        deal.filledVolume = int(volume)
        deal.symbolId = record.symbol_id
        deal.createTimestamp = self.now_ms
        deal.executionTimestamp = self.now_ms
        deal.executionPrice = float(price)
        deal.tradeSide = side
        deal.dealStatus = _DEAL_STATUS_FILLED
        deal.moneyDigits = self.money_digits
        # cTrader reports a charge as a NEGATIVE commission cash flow.
        deal.commission = -1
        if closing:
            detail = deal.closePositionDetail
            position = self.positions.get(position_id)
            # ``entryPrice`` is a DOUBLE carrying the position's average
            # entry; ``grossProfit`` is the realised P&L in the account's
            # money digits: price move x base units (volume is in cents,
            # so /100) x the position side's sign.
            entry = position.entry_price if position is not None else price
            position_side = (
                _int(getattr(position.proto.tradeData, "tradeSide", _SIDE_BUY))
                if position is not None
                else (_SIDE_BUY if side == _SIDE_SELL else _SIDE_SELL)
            )
            direction = 1.0 if position_side == _SIDE_BUY else -1.0
            gross_ccy = (price - entry) * direction * (volume / 100.0)
            gross = int(round(gross_ccy * 10 ** self.money_digits))
            detail.entryPrice = float(entry)
            detail.grossProfit = gross
            detail.swap = 0
            detail.commission = -1
            # The realised result lands on the account balance, so a later
            # ``TraderReq`` reports the money the close actually made.
            self.balance_ccy += (gross + detail.commission) / (10 ** self.money_digits)
            detail.balance = int(round(self.balance_ccy * 10 ** self.money_digits))
            detail.closedVolume = int(volume)
            detail.balanceVersion = 1
            detail.moneyDigits = self.money_digits
        self.deals.append(deal)
        return deal

    def _open_position(
        self,
        *,
        position_id: int,
        symbol_name: str,
        symbol_id: int,
        side: int,
        volume: int,
        price: float,
        record: _FakeOrder | None = None,
    ) -> _FakePosition:
        proto = _mm.ProtoOAPosition()
        proto.positionId = position_id
        proto.positionStatus = _POSITION_STATUS_OPEN
        proto.price = float(price)
        proto.moneyDigits = self.money_digits
        proto.swap = 0
        proto.utcLastUpdateTimestamp = self.now_ms
        td = proto.tradeData
        td.symbolId = symbol_id
        td.volume = int(volume)
        td.tradeSide = side
        td.openTimestamp = self.now_ms
        position = _FakePosition(
            proto=proto,
            symbol_name=symbol_name,
            symbol_id=symbol_id,
        )
        self.positions[position_id] = position
        self._apply_protection(position, record)
        return position

    def _apply_protection(self, position: _FakePosition, record: _FakeOrder | None) -> None:
        """Turn an entry's relative/absolute SL-TP into the position's prices."""
        if record is None:
            return
        entry = position.entry_price
        side_buy = _int(getattr(record.proto.tradeData, "tradeSide", _SIDE_BUY)) == _SIDE_BUY
        unit = 1.0 / _RELATIVE_PRICE_UNIT
        stop = record.absolute_stop
        take = record.absolute_take
        if record.relative_stop:
            distance = record.relative_stop * unit
            stop = entry - distance if side_buy else entry + distance
        if record.relative_take:
            distance = record.relative_take * unit
            take = entry + distance if side_buy else entry - distance
        if stop is not None and stop > 0:
            position.proto.stopLoss = float(stop)
        if take is not None and take > 0:
            position.proto.takeProfit = float(take)

    def _apply_fill(
        self,
        position_id: int,
        record: _FakeOrder,
        volume: int,
        price: float,
        is_closing: bool,
    ) -> Any | None:
        """Apply a fill to its position; return the position proto for the event.

        An entry fill on a missing position opens it; a same-side fill grows
        it (weighted entry); an opposite-side fill shrinks it, and a netted
        account flips to the fill's side when the fill overshoots. A closing
        fill on a position that is already gone records the deal but touches
        nothing - the broker still answered the order.
        """
        side = _int(getattr(record.proto.tradeData, "tradeSide", _SIDE_BUY))
        position = self.positions.get(position_id)
        if position is None:
            if is_closing:
                return None
            position = self._open_position(
                position_id=position_id,
                symbol_name=record.symbol_name,
                symbol_id=record.symbol_id,
                side=side,
                volume=volume,
                price=price,
                record=record,
            )
            return position.proto
        proto = position.proto
        current_volume = _int(getattr(proto.tradeData, "volume", 0))
        current_side = _int(getattr(proto.tradeData, "tradeSide", _SIDE_BUY))
        same_side = side == current_side and not is_closing
        if same_side:
            proto.tradeData.volume = current_volume + volume
            # ``price`` is the weighted average entry: a scale-in moves it,
            # a reduce or a flip leaves the surviving entry untouched.
            proto.price = (
                position.entry_price * current_volume + price * volume
            ) / (current_volume + volume)
        else:
            remaining = current_volume - volume
            if remaining > 0:
                proto.tradeData.volume = remaining
            elif remaining < 0 and not is_closing and self.account_type_name == "netted":
                # The fill flipped the netted position: reopen on the fill's
                # side with the overshoot at the fill price.
                proto.tradeData.volume = -remaining
                proto.tradeData.tradeSide = side
                proto.price = float(price)
            else:
                self._close_position_record(position_id)
                proto.positionStatus = _POSITION_STATUS_CLOSED
                proto.utcLastUpdateTimestamp = self.now_ms
                return proto
        proto.utcLastUpdateTimestamp = self.now_ms
        return proto

    def _close_position_record(self, position_id: int) -> None:
        position = self.positions.pop(position_id, None)
        if position is not None:
            position.proto.positionStatus = _POSITION_STATUS_CLOSED
            position.proto.utcLastUpdateTimestamp = self.now_ms

    def _netted_position_for(self, symbol_id: int) -> int | None:
        for position_id, position in self.positions.items():
            if position.symbol_id == symbol_id:
                return position_id
        return None

    def _advance_position_protection(self) -> list[Any]:
        """Trigger stops/take-profits: a crossed level closes the position."""
        emitted: list[Any] = []
        for position_id, position in list(self.positions.items()):
            proto = position.proto
            mark = self.mark_price(position.symbol_name)
            side_buy = _int(getattr(proto.tradeData, "tradeSide", _SIDE_BUY)) == _SIDE_BUY
            stop = _float(getattr(proto, "stopLoss", 0.0))
            take = _float(getattr(proto, "takeProfit", 0.0))
            trigger = None
            if stop > 0 and (mark <= stop if side_buy else mark >= stop):
                trigger = "stop"
            elif take > 0 and (mark >= take if side_buy else mark <= take):
                trigger = "take"
            if trigger is None:
                continue
            volume = _int(getattr(proto.tradeData, "volume", 0))
            if volume <= 0:
                continue
            closing_side = _SIDE_SELL if side_buy else _SIDE_BUY
            record = _FakeOrder(
                proto=self._closing_order_proto(position, volume, closing_side),
                symbol_name=position.symbol_name,
                symbol_id=position.symbol_id,
                accepted_ms=self.now_ms,
                target_position_id=position_id,
            )
            self.orders[record.proto.orderId] = record
            deal = self._record_deal(
                record, volume, mark, position_id=position_id, closing=True
            )
            self._close_position_record(position_id)
            record.proto.orderStatus = _ORDER_STATUS_FILLED
            record.proto.executedVolume = volume
            record.proto.executionPrice = mark
            record.proto.utcLastUpdateTimestamp = self.now_ms
            emitted.append(self._execution_event(
                _EXECUTION_ORDER_FILLED,
                order=record.proto,
                deal=deal,
                position=proto,
            ))
        return emitted

    def _closing_order_proto(
        self,
        position: _FakePosition,
        volume: int,
        side: int,
        order_type: int = _ORDER_TYPE_MARKET,
    ) -> Any:
        proto = _mm.ProtoOAOrder()
        proto.orderId = self._next_order_id()
        proto.orderType = order_type
        proto.orderStatus = _ORDER_STATUS_ACCEPTED
        proto.closingOrder = True
        # ProtoOAOrder.timeInForce defaults to IOC in the .proto; a closing
        # order must rest for its remainder like any other working order.
        proto.timeInForce = _TIF_GOOD_TILL_CANCEL
        proto.positionId = _int(getattr(position.proto, "positionId", 0))
        proto.utcLastUpdateTimestamp = self.now_ms
        td = proto.tradeData
        td.symbolId = position.symbol_id
        td.volume = int(volume)
        td.tradeSide = side
        td.openTimestamp = self.now_ms
        return proto

    # ── request handlers ───────────────────────────────────────────────────

    def handle(self, request: Any) -> Any:
        """Broker-side dispatch: request proto -> response proto."""
        _ensure_types()
        self.requests.append(request)
        name = type(request).__name__
        handler = getattr(self, f"_req_{name.removeprefix('ProtoOA')}", None)
        if handler is None:
            raise AssertionError(f"FakeCTraderBroker has no handler for {name}")
        return handler(request)

    # auth ------------------------------------------------------------------

    def _req_ApplicationAuthReq(self, request: Any) -> Any:
        res = _m.ProtoOAApplicationAuthRes()
        return res

    def _req_AccountAuthReq(self, request: Any) -> Any:
        res = _m.ProtoOAAccountAuthRes()
        res.ctidTraderAccountId = self.account_id
        return res

    def _req_RefreshTokenReq(self, request: Any) -> Any:
        res = _m.ProtoOARefreshTokenRes()
        res.accessToken = "fake-access-token-refreshed"
        res.tokenType = "bearer"
        res.expiresIn = 3600
        res.refreshToken = "fake-refresh-token-refreshed"
        return res

    # reference data ---------------------------------------------------------

    def _req_SymbolsListReq(self, request: Any) -> Any:
        res = _m.ProtoOASymbolsListRes()
        res.ctidTraderAccountId = self.account_id
        for spec in self._symbols.values():
            light = res.symbol.add()
            light.symbolId = spec.symbol_id
            light.symbolName = spec.name
            light.enabled = spec.enabled
            light.baseAssetId = spec.base_asset_id
            light.quoteAssetId = spec.quote_asset_id
            light.symbolCategoryId = 1
            light.description = spec.name
        return res

    def _req_SymbolByIdReq(self, request: Any) -> Any:
        res = _m.ProtoOASymbolByIdRes()
        res.ctidTraderAccountId = self.account_id
        for symbol_id in request.symbolId:
            spec = self._symbols.get(int(symbol_id))
            if spec is None:
                continue
            detail = res.symbol.add()
            detail.symbolId = spec.symbol_id
            detail.digits = spec.digits
            detail.pipPosition = 0
            detail.lotSize = round(spec.lot_size)
            detail.minVolume = spec.min_volume
            detail.maxVolume = spec.max_volume
            detail.stepVolume = spec.step_volume
            detail.enableShortSelling = True
            if spec.leverage_id:
                detail.leverageId = spec.leverage_id
        return res

    def _req_TraderReq(self, request: Any) -> Any:
        res = _m.ProtoOATraderRes()
        res.ctidTraderAccountId = self.account_id
        trader = res.trader
        trader.balance = round(self.balance_ccy * 10 ** self.money_digits)
        trader.moneyDigits = self.money_digits
        trader.leverageInCents = self.leverage_in_cents
        trader.accountType = (
            _ACCOUNT_TYPE_HEDGED
            if self.account_type_name == "hedged"
            else _ACCOUNT_TYPE_NETTED
        )
        trader.traderLogin = 1
        trader.brokerName = "fake"
        trader.depositAssetId = 1
        return res

    def _req_GetPositionUnrealizedPnLReq(self, request: Any) -> Any:
        res = _m.ProtoOAGetPositionUnrealizedPnLRes()
        res.ctidTraderAccountId = self.account_id
        res.moneyDigits = self.money_digits
        for position in self.positions.values():
            proto = position.proto
            mark = self.mark_price(position.symbol_name)
            side_buy = (
                _int(getattr(proto.tradeData, "tradeSide", _SIDE_BUY)) == _SIDE_BUY
            )
            volume = _int(getattr(proto.tradeData, "volume", 0))
            # Unrealised P&L is the price move x BASE units: volume is in
            # cents of the base asset, so volume/100 is the base quantity
            # (EURUSD volume 10_000_000 = 100_000 EUR = 1 lot).
            diff = (mark - position.entry_price) * (1 if side_buy else -1)
            pnl_ccy = diff * volume / 100.0
            entry = res.positionUnrealizedPnL.add()
            entry.positionId = _int(getattr(proto, "positionId", 0))
            gross = round(pnl_ccy * 10 ** self.money_digits)
            entry.grossUnrealizedPnL = gross
            entry.netUnrealizedPnL = gross
        return res

    def _req_GetDynamicLeverageByIDReq(self, request: Any) -> Any:
        res = _m.ProtoOAGetDynamicLeverageByIDRes()
        res.ctidTraderAccountId = self.account_id
        leverage = res.leverage
        leverage.leverageId = _int(getattr(request, "leverageId", 0))
        tier = leverage.tiers.add()
        tier.volume = 0
        tier.leverage = 100
        tier = leverage.tiers.add()
        tier.volume = 1_000_000
        tier.leverage = 50
        return res

    # trading ----------------------------------------------------------------

    def _req_NewOrderReq(self, request: Any) -> Any:
        reason = None
        if self.reject_submissions is not None:
            try:
                reason = self.reject_submissions(request)
            except Exception:  # pragma: no cover
                reason = None
        if reason:
            return self._order_error("ORDER_REJECTED", str(reason))
        spec = self._symbol_for_id(_int(getattr(request, "symbolId", 0)))
        if spec is None:
            return self._order_error("SYMBOL_NOT_FOUND", "unknown symbolId")
        volume = _int(getattr(request, "volume", 0))
        if volume <= 0:
            return self._order_error("TRADING_BAD_VOLUME", "volume must be positive")
        if volume < spec.min_volume:
            return self._order_error(
                "TRADING_BAD_VOLUME", f"volume below minimum {spec.min_volume}"
            )
        if spec.step_volume and volume % spec.step_volume:
            return self._order_error(
                "TRADING_BAD_VOLUME",
                f"volume {volume} is not a multiple of step {spec.step_volume}",
            )
        if volume > spec.max_volume:
            return self._order_error(
                "TRADING_BAD_VOLUME", f"volume above maximum {spec.max_volume}"
            )
        record = self._accept_order(request, spec)
        return self._execution_event(
            _EXECUTION_ORDER_ACCEPTED, order=record.proto
        )

    def _accept_order(self, request: Any, spec: FakeCTraderSymbol) -> _FakeOrder:
        order_id = self._next_order_id()
        proto = _mm.ProtoOAOrder()
        proto.orderId = order_id
        proto.orderType = _int(getattr(request, "orderType", _ORDER_TYPE_MARKET))
        proto.orderStatus = _ORDER_STATUS_ACCEPTED
        proto.timeInForce = _int(getattr(request, "timeInForce", _TIF_GOOD_TILL_CANCEL))
        if _has(request, "expirationTimestamp"):
            proto.expirationTimestamp = _int(request.expirationTimestamp)
        if _has(request, "limitPrice"):
            proto.limitPrice = float(request.limitPrice)
        if _has(request, "stopPrice"):
            proto.stopPrice = float(request.stopPrice)
        if _has(request, "clientOrderId"):
            proto.clientOrderId = str(request.clientOrderId)
        if _has(request, "positionId") and _int(request.positionId) > 0:
            proto.positionId = _int(request.positionId)
            proto.closingOrder = True
        proto.utcLastUpdateTimestamp = self.now_ms
        td = proto.tradeData
        td.symbolId = spec.symbol_id
        td.volume = _int(getattr(request, "volume", 0))
        td.tradeSide = _int(getattr(request, "tradeSide", _SIDE_BUY))
        td.openTimestamp = self.now_ms
        if _has(request, "label"):
            td.label = str(request.label)
        record = _FakeOrder(
            proto=proto,
            symbol_name=spec.name,
            symbol_id=spec.symbol_id,
            accepted_ms=self.now_ms,
            limit_price=_float(getattr(request, "limitPrice", None)) or None,
            stop_price=_float(getattr(request, "stopPrice", None)) or None,
            relative_stop=_int(getattr(request, "relativeStopLoss", 0)) or None,
            relative_take=_int(getattr(request, "relativeTakeProfit", 0)) or None,
            absolute_stop=_float(getattr(request, "stopLoss", None)) or None,
            absolute_take=_float(getattr(request, "takeProfit", None)) or None,
            target_position_id=_int(getattr(request, "positionId", 0)),
        )
        self.orders[order_id] = record
        return record

    def _req_CancelOrderReq(self, request: Any) -> Any:
        if self.cancel_error is not None:
            raise self.cancel_error
        order_id = _int(getattr(request, "orderId", 0))
        record = self.orders.get(order_id)
        if record is None or _int(record.proto.orderStatus) != _ORDER_STATUS_ACCEPTED:
            return self._order_error(
                "ORDER_NOT_FOUND", f"no working order {order_id}"
            )
        record.proto.orderStatus = _ORDER_STATUS_CANCELLED
        record.proto.utcLastUpdateTimestamp = self.now_ms
        return self._execution_event(
            _EXECUTION_ORDER_CANCELLED, order=record.proto
        )

    def _req_ClosePositionReq(self, request: Any) -> Any:
        position_id = _int(getattr(request, "positionId", 0))
        position = self.positions.get(position_id)
        if position is None:
            return self._order_error(
                "POSITION_NOT_FOUND", f"no open position {position_id}"
            )
        volume = _int(getattr(request, "volume", 0))
        current = _int(getattr(position.proto.tradeData, "volume", 0))
        if volume <= 0 or volume >= current:
            volume = current
        record = _FakeOrder(
            proto=self._closing_order_proto(
                position,
                volume,
                _SIDE_SELL
                if _int(getattr(position.proto.tradeData, "tradeSide", _SIDE_BUY))
                == _SIDE_BUY
                else _SIDE_BUY,
            ),
            symbol_name=position.symbol_name,
            symbol_id=position.symbol_id,
            accepted_ms=self.now_ms,
            target_position_id=position_id,
        )
        # The broker ACKNOWLEDGES the closing order; the fill is a later
        # execution event, exactly like a NewOrderReq - never inside the
        # request. ``advance()`` runs it through the same partial-fill and
        # fill-delay pipeline as any other order.
        self.orders[record.proto.orderId] = record
        return self._execution_event(
            _EXECUTION_ORDER_ACCEPTED,
            order=record.proto,
        )

    def _req_AmendPositionSLTPReq(self, request: Any) -> Any:
        position_id = _int(getattr(request, "positionId", 0))
        position = self.positions.get(position_id)
        if position is None:
            return self._order_error(
                "POSITION_NOT_FOUND", f"no open position {position_id}"
            )
        # The request REPLACES both protective levels atomically: a level
        # the request omits is cleared, not preserved. This is why the
        # adapter echoes the current stopLoss back verbatim - a regression
        # that stopped echoing it would silently drop the stop here.
        for field in ("stopLoss", "takeProfit"):
            if _has(request, field) and float(getattr(request, field)) > 0:
                setattr(position.proto, field, float(getattr(request, field)))
            elif position.proto.HasField(field):
                position.proto.ClearField(field)
        for field in (
            "guaranteedStopLoss",
            "trailingStopLoss",
            "stopLossTriggerMethod",
        ):
            if _has(request, field):
                setattr(position.proto, field, getattr(request, field))
        position.proto.utcLastUpdateTimestamp = self.now_ms
        return self._execution_event(
            _EXECUTION_ORDER_REPLACED, position=position.proto
        )

    # listings ---------------------------------------------------------------

    @staticmethod
    def _order_sort_key(order: Any) -> int:
        td = getattr(order, "tradeData", None)
        return _int(getattr(td, "openTimestamp", 0))

    def _orders_in_window(
        self, from_ms: int, to_ms: int, position_id: int | None = None
    ) -> list[Any]:
        rows = []
        for record in self.orders.values():
            if position_id is not None and _int(
                getattr(record.proto, "positionId", 0)
            ) != position_id:
                continue
            stamp = self._order_sort_key(record.proto)
            if from_ms and stamp < from_ms:
                continue
            if to_ms and stamp > to_ms:
                continue
            rows.append(record.proto)
        rows.sort(key=self._order_sort_key)
        return rows

    def _page(self, rows: list[Any], limit: int) -> tuple[list[Any], bool]:
        cap = limit if limit > 0 else self.page_size
        page = rows[:cap]
        return page, len(rows) > cap

    def _req_OrderListReq(self, request: Any) -> Any:
        res = _m.ProtoOAOrderListRes()
        res.ctidTraderAccountId = self.account_id
        rows = self._orders_in_window(
            _int(getattr(request, "fromTimestamp", 0)),
            _int(getattr(request, "toTimestamp", 0)),
        )
        page, has_more = self._page(rows, 0)
        res.order.extend(page)
        res.hasMore = has_more
        return res

    def _req_OrderListByPositionIdReq(self, request: Any) -> Any:
        res = _m.ProtoOAOrderListByPositionIdRes()
        res.ctidTraderAccountId = self.account_id
        rows = self._orders_in_window(
            _int(getattr(request, "fromTimestamp", 0)),
            _int(getattr(request, "toTimestamp", 0)),
            position_id=_int(getattr(request, "positionId", 0)),
        )
        # Newest first, as the broker sends it.
        rows = list(reversed(rows))
        page, has_more = self._page(rows, 0)
        res.order.extend(page)
        res.hasMore = has_more
        return res

    def _req_OrderDetailsReq(self, request: Any) -> Any:
        order_id = _int(getattr(request, "orderId", 0))
        record = self.orders.get(order_id)
        if record is None:
            # The broker answers an unknown order id with an API error, not
            # an empty details page.
            res = _m.ProtoOAErrorRes()
            res.ctidTraderAccountId = self.account_id
            res.errorCode = "ORDER_NOT_FOUND"
            res.description = f"no order {order_id}"
            return res
        res = _m.ProtoOAOrderDetailsRes()
        res.ctidTraderAccountId = self.account_id
        res.order.CopyFrom(record.proto)
        for deal in self.deals:
            if _int(getattr(deal, "orderId", 0)) == order_id:
                res.deal.add().CopyFrom(deal)
        return res

    def _req_DealListReq(self, request: Any) -> Any:
        res = _m.ProtoOADealListRes()
        res.ctidTraderAccountId = self.account_id
        from_ms = _int(getattr(request, "fromTimestamp", 0))
        to_ms = _int(getattr(request, "toTimestamp", 0))
        max_rows = _int(getattr(request, "maxRows", 0))
        rows = [
            deal
            for deal in self.deals
            if (not from_ms or _int(deal.executionTimestamp) >= from_ms)
            and (not to_ms or _int(deal.executionTimestamp) <= to_ms)
        ]
        rows.sort(key=lambda d: _int(d.executionTimestamp))
        page, has_more = self._page(rows, max_rows)
        res.deal.extend(page)
        res.hasMore = has_more
        return res

    def _req_DealListByPositionIdReq(self, request: Any) -> Any:
        res = _m.ProtoOADealListByPositionIdRes()
        res.ctidTraderAccountId = self.account_id
        position_id = _int(getattr(request, "positionId", 0))
        from_ms = _int(getattr(request, "fromTimestamp", 0))
        to_ms = _int(getattr(request, "toTimestamp", 0))
        rows = [
            deal
            for deal in self.deals
            if _int(getattr(deal, "positionId", 0)) == position_id
            and (not from_ms or _int(deal.executionTimestamp) >= from_ms)
            and (not to_ms or _int(deal.executionTimestamp) <= to_ms)
        ]
        rows.sort(key=lambda d: _int(d.executionTimestamp))
        page, has_more = self._page(rows, 0)
        res.deal.extend(page)
        res.hasMore = has_more
        return res

    # reconcile ---------------------------------------------------------------

    def _req_ReconcileReq(self, request: Any) -> Any:
        res = _m.ProtoOAReconcileRes()
        res.ctidTraderAccountId = self.account_id
        for position in self.positions.values():
            res.position.add().CopyFrom(position.proto)
        for record in self.orders.values():
            if _int(record.proto.orderStatus) == _ORDER_STATUS_ACCEPTED:
                res.order.add().CopyFrom(record.proto)
        return res

    # market data --------------------------------------------------------------

    def _req_GetTrendbarsReq(self, request: Any) -> Any:
        res = _m.ProtoOAGetTrendbarsRes()
        res.ctidTraderAccountId = self.account_id
        res.period = _int(getattr(request, "period", 0))
        res.timestamp = _int(getattr(request, "toTimestamp", 0))
        res.symbolId = _int(getattr(request, "symbolId", 0))
        return res

    def _req_GetTickDataReq(self, request: Any) -> Any:
        res = _m.ProtoOAGetTickDataRes()
        res.ctidTraderAccountId = self.account_id
        res.hasMore = False
        return res

    def _req_SubscribeSpotsReq(self, request: Any) -> Any:
        res = _m.ProtoOASubscribeSpotsRes()
        res.ctidTraderAccountId = self.account_id
        return res

    def _req_UnsubscribeSpotsReq(self, request: Any) -> Any:
        res = _m.ProtoOAUnsubscribeSpotsRes()
        res.ctidTraderAccountId = self.account_id
        return res

    # ── response/event builders ─────────────────────────────────────────────

    def _order_error(self, code: str, description: str) -> Any:
        res = _m.ProtoOAOrderErrorEvent()
        res.ctidTraderAccountId = self.account_id
        res.errorCode = code
        res.description = description
        return res

    def _execution_event(
        self,
        execution_type: int,
        *,
        order: Any = None,
        deal: Any = None,
        position: Any = None,
        error_code: str = "",
    ) -> Any:
        event = _m.ProtoOAExecutionEvent()
        event.ctidTraderAccountId = self.account_id
        event.executionType = execution_type
        if order is not None:
            event.order.CopyFrom(order)
        if deal is not None:
            event.deal.CopyFrom(deal)
        if position is not None:
            event.position.CopyFrom(position)
        if error_code:
            event.errorCode = error_code
        return event


class FakeCTraderClient:
    """A drop-in for ``ctrader_open_api.Client`` at the ``send``/``_received`` seam.

    ``send`` answers synchronously through the same ``_received`` path the
    socket uses, so a response is both delivered to the message-received
    callback (as the server push would be) and used to resolve the request's
    ``Deferred`` by ``clientMsgId`` - exactly like upstream ``Client._received``.
    """

    def __init__(self, broker: FakeCTraderBroker) -> None:
        self.broker = broker
        self.running = False
        self.isConnected = False
        self._responseDeferreds: dict[str, Any] = {}
        self._connected_callback: Any = None
        self._disconnected_callback: Any = None
        self._message_received_callback: Any = None
        self.sent: list[Any] = []

    # ClientService surface ---------------------------------------------------

    def startService(self) -> None:
        if self.running:
            return
        self.running = True
        self.isConnected = True
        if self._connected_callback is not None:
            self._connected_callback(self)

    def stopService(self) -> None:
        if self.running:
            self._disconnect("stopped")

    def disconnect(self, reason: Any = "fake disconnect") -> None:
        self._disconnect(reason)

    def _disconnect(self, reason: Any) -> None:
        self.running = False
        self.isConnected = False
        # Upstream ``Client._disconnected`` clears the pending map WITHOUT
        # errbacking it; the adapter's own in-flight set does the errbacking.
        self._responseDeferreds.clear()
        if self._disconnected_callback is not None:
            self._disconnected_callback(self, reason)

    def setConnectedCallback(self, callback: Any) -> None:
        self._connected_callback = callback

    def setDisconnectedCallback(self, callback: Any) -> None:
        self._disconnected_callback = callback

    def setMessageReceivedCallback(self, callback: Any) -> None:
        self._message_received_callback = callback

    # the wire ---------------------------------------------------------------

    def send(
        self,
        message: Any,
        clientMsgId: str | None = None,
        responseTimeoutInSeconds: int = 5,
        **params: Any,
    ) -> Any:
        _ensure_types()
        if isinstance(message, (str, int)):
            assert _Protobuf is not None
            message = _Protobuf.get(message, **params)
        self.sent.append(message)
        deferred = defer.Deferred()
        if clientMsgId is None:
            clientMsgId = str(id(deferred))
        self._responseDeferreds[clientMsgId] = deferred
        try:
            payload = self.broker.handle(message)
        except Exception as exc:  # a broken fake must surface, not hang
            self._responseDeferreds.pop(clientMsgId, None)
            deferred.errback(exc)
            return deferred
        proto_message = ProtoMessage(
            payload=payload.SerializeToString(),
            payloadType=payload.payloadType,
            clientMsgId=clientMsgId,
        )
        self._received(proto_message)
        return deferred

    def _received(self, message: ProtoMessage) -> None:
        """Mirror upstream ``Client._received``: event first, then the reply."""
        _ensure_types()
        if self._message_received_callback is not None:
            self._message_received_callback(self, message)
        client_msg_id = str(getattr(message, "clientMsgId", "") or "")
        deferred = self._responseDeferreds.pop(client_msg_id, None)
        if deferred is not None:
            deferred.callback(message)

    def deliver(self, payload: Any) -> None:
        """Push a server-initiated payload (execution event, spot, error)."""
        _ensure_types()
        self._received(
            ProtoMessage(
                payload=payload.SerializeToString(),
                payloadType=payload.payloadType,
            )
        )


@contextmanager
def fake_ctrader_transport(
    broker: FakeCTraderBroker | None = None,
) -> Iterator[tuple[FakeCTraderBroker, FakeCTraderClient]]:
    """Install the fake at the seam ``CTraderLiveAdapter._create_client`` builds on.

    ``_load_openapi_types`` does ``from ctrader_open_api import Client`` at
    connect time, so substituting the ``Client`` attribute is enough for the
    adapter to build the fake itself - the real protobuf types are still
    loaded, so every request/response is a genuine ``ProtoOA*`` message.

    The patch lands on BOTH ``ctrader_open_api.Client`` and
    ``ctrader_open_api.client.Client``: ``account_discovery``'s reactor sweep
    can evict those modules from ``sys.modules``, in which case the next
    ``from ctrader_open_api import Client`` re-executes ``__init__`` and
    re-reads ``Client`` off the ``client`` submodule. The import itself runs
    under ``_without_installed_reactor`` so entering the context never pins
    the platform reactor; evicted modules are re-seated (with the patch
    applied) so the adapter's later import resolves the same objects.

    Yields ``(broker, client)``.
    """
    import sys

    # Deferred: account_discovery imports the alpaca adapter chain.
    from live_runner.account_discovery import _without_installed_reactor

    broker = broker or FakeCTraderBroker()
    client = FakeCTraderClient(broker)

    def _factory(*args: Any, **kwargs: Any) -> FakeCTraderClient:
        return client

    with _without_installed_reactor():
        import ctrader_open_api
        import ctrader_open_api.client as client_module

    # The sweep above may have evicted the package and its ``client`` module
    # (it does whenever no reactor was installed beforehand). Re-seat the
    # objects we just imported - patched - so ``from ctrader_open_api import
    # Client`` inside ``_load_openapi_types`` resolves them instead of
    # re-executing ``__init__`` against the real ``Client``.
    sys.modules.setdefault("ctrader_open_api", ctrader_open_api)
    sys.modules.setdefault("ctrader_open_api.client", client_module)

    patched: list[tuple[Any, str, Any]] = []
    for module in (ctrader_open_api, client_module):
        previous = getattr(module, "Client", None)
        module.Client = _factory  # type: ignore[attr-defined]
        patched.append((module, "Client", previous))
    try:
        yield broker, client
    finally:
        for module, name, previous in patched:
            if previous is None:
                try:
                    delattr(module, name)
                except AttributeError:
                    pass
            else:
                setattr(module, name, previous)


__all__ = [
    "FakeCTraderBroker",
    "FakeCTraderClient",
    "FakeCTraderSymbol",
    "fake_ctrader_transport",
]

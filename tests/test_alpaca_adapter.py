"""Alpaca control-plane behaviour, against a fake trading client.

The ordering tested here is the whole point of the STOPPED path: attached SL/TP
legs must survive the entry-cancel sweep, because the flatten runs afterwards and
a failed flatten must not leave a naked position.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from live_runner import alpaca_adapter as alpaca_module
from live_runner.alpaca_adapter import AlpacaLiveAdapter, _alpaca_market_data_start
from live_runner.base_adapter import is_order_not_submitted
from shared.models import LiveSignal


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


@dataclass
class _FakePosition:
    symbol: str
    qty: float


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
        close_error: Exception | None = None,
        submit_error: Exception | None = None,
        cancel_settles: bool = True,
    ) -> None:
        # Alpaca keeps an accepted cancel in the book as ``pending_cancel`` until
        # it settles; ``cancel_settles=False`` reproduces that window.
        self.cancel_settles = cancel_settles
        self.orders = list(orders or [])
        self.positions = list(positions or [])
        self.cancelled: list[str] = []
        self.close_calls: list[str] = []
        self.submitted: list[object] = []
        self.close_error = close_error
        self.submit_error = submit_error

    def get_orders(self, filter=None):  # noqa: A002 - mirrors alpaca-py
        # alpaca-py's server-side default page size is 50.
        limit = getattr(filter, "limit", None) or 50
        self.last_order_filter = filter
        status = str(getattr(getattr(filter, "status", None), "value", "") or "").lower()
        symbols = getattr(filter, "symbols", None)
        result = list(self.orders)
        if status != "all":
            result = [
                order
                for order in result
                if _status_text(order) not in _TERMINAL_ORDER_STATUSES
            ]
        if symbols:
            wanted = {str(symbol).strip().upper() for symbol in symbols}
            result = [
                order
                for order in result
                if str(getattr(order, "symbol", "") or "").strip().upper() in wanted
            ]
        return result[:limit]

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
        # sibling does (OCO): count it once via the parent, not twice.
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
            parent = _FakeOrder(
                id=str(uuid4()),
                symbol=symbol,
                side=side,
                order_type="limit",
                order_class="oco",
                qty=qty,
                status="new",
                legs=[
                    _FakeOrder(
                        id=str(uuid4()),
                        symbol=symbol,
                        side=side,
                        order_type="limit",
                        qty=qty,
                        limit_price=tp_price,
                        status="new",
                    ),
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
        return result + [stranger] if reads["n"] > 1 else result

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

    restored = asyncio.run(adapter._restore_stop(client, "AAPL", 10.0, "sell", 190.0))

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

    assert asyncio.run(adapter._restore_stop(client, "AAPL", 10.0, "sell", 190.0)) is True
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


def test_positions_mark_an_unparseable_qty_instead_of_reading_it_as_flat() -> None:
    """A broker row whose qty cannot be read is flagged, not silently zeroed."""
    client = _FakeTradingClient(
        positions=[SimpleNamespace(symbol="AAPL", qty="not-a-number")]
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
        positions=[SimpleNamespace(symbol="AAPL", qty="not-a-number")]
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
        positions=[SimpleNamespace(symbol="AAPL", qty="nan-ish")],
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
    # The persisted signal carried the recovery marker, cleared once protected.
    assert captured and "exit_reprotection" in (captured[0].metadata or {})
    assert cleared


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
    """An exit over a position protected by a re-placed OCO releases it too."""
    client = _FakeTradingClient(
        orders=[
            _FakeOrder(
                id=str(uuid4()),
                symbol="AAPL",
                side="sell",
                order_type="limit",
                order_class="oco",
                qty=6.0,
                status="new",
                legs=[
                    _FakeOrder(
                        id=str(uuid4()),
                        symbol="AAPL",
                        side="sell",
                        order_type="limit",
                        qty=6.0,
                        limit_price=210.0,
                        status="new",
                    ),
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

    # The OCO parent and both legs were cancelled; the remainder is re-protected.
    oco = [order for order in client.submitted if _class_text(order) == "oco"]
    assert len(oco) == 1
    assert float(oco[0].qty) == 4.0
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

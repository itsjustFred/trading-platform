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
    legs: list["_FakeOrder"] = field(default_factory=list)


@dataclass
class _FakePosition:
    symbol: str
    qty: float


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
    ) -> None:
        self.orders = list(orders or [])
        self.positions = list(positions or [])
        self.cancelled: list[str] = []
        self.close_calls: list[str] = []
        self.submitted: list[object] = []
        self.close_error = close_error
        self.submit_error = submit_error

    def get_orders(self, filter=None):  # noqa: A002 - mirrors alpaca-py
        return list(self.orders)

    def get_all_positions(self):
        return list(self.positions)

    def cancel_order_by_id(self, order_id: str) -> None:
        self.cancelled.append(order_id)
        self.orders = [order for order in self.orders if order.id != order_id]

    def _reserved_qty(self, symbol: str) -> float:
        position = next((item for item in self.positions if item.symbol == symbol), None)
        if position is None:
            return 0.0
        side = "sell" if position.qty > 0 else "buy"
        return sum(
            abs(order.qty)
            for order in self.orders
            if order.symbol == symbol and order.side == side
        )

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


def _adapter(client: _FakeTradingClient) -> AlpacaLiveAdapter:
    adapter = AlpacaLiveAdapter(api_key="key", secret_key="secret", paper=True)
    adapter._connected = True
    adapter._trading_client = client
    return adapter


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
        return SimpleNamespace(id=f"close-{symbol}", symbol=symbol)

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

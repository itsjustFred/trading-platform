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

import pytest

from live_runner.alpaca_adapter import AlpacaLiveAdapter, _alpaca_market_data_start
from shared.models import LiveSignal


@dataclass
class _FakeOrder:
    id: str
    symbol: str
    side: str
    order_type: str = "limit"
    order_class: str = "simple"
    legs: list["_FakeOrder"] = field(default_factory=list)


@dataclass
class _FakePosition:
    symbol: str
    qty: float


class _FakeTradingClient:
    def __init__(
        self,
        *,
        orders: list[_FakeOrder] | None = None,
        positions: list[_FakePosition] | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.orders = list(orders or [])
        self.positions = list(positions or [])
        self.cancelled: list[str] = []
        self.close_calls: list[bool] = []
        self.close_error = close_error

    def get_orders(self, filter=None):  # noqa: A002 - mirrors alpaca-py
        return list(self.orders)

    def get_all_positions(self):
        return list(self.positions)

    def cancel_order_by_id(self, order_id: str) -> None:
        self.cancelled.append(order_id)
        self.orders = [order for order in self.orders if order.id != order_id]

    def close_all_positions(self, cancel_orders: bool = False):
        self.close_calls.append(cancel_orders)
        if self.close_error is not None:
            raise self.close_error
        self.positions = []
        return []


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


def test_failed_flatten_keeps_protections_and_does_not_report_complete() -> None:
    client = _FakeTradingClient(
        orders=[
            _FakeOrder("entry-1", "AAPL", "buy"),
            _FakeOrder("sl-1", "AAPL", "sell", order_type="stop"),
            _FakeOrder("tp-1", "AAPL", "sell", order_type="limit"),
        ],
        positions=[_FakePosition("AAPL", 10.0)],
        close_error=RuntimeError("broker refused to flatten"),
    )
    adapter = _adapter(client)

    cancel = asyncio.run(adapter.cancel_open_orders())
    with pytest.raises(RuntimeError):
        asyncio.run(adapter.close_all_positions())

    assert cancel.complete is True
    # The position still exists and both risk controls are still resting: no
    # naked position was created by the failed flatten.
    assert [order.id for order in client.orders] == ["sl-1", "tp-1"]


def test_close_all_positions_reports_a_partial_flatten() -> None:
    client = _FakeTradingClient(positions=[_FakePosition("AAPL", 10.0)])
    adapter = _adapter(client)

    # The broker acknowledges but the position survives: the re-read must make
    # the outcome incomplete instead of trusting the acknowledgement.
    original = client.close_all_positions

    def _acknowledge_only(cancel_orders: bool = False):
        client.close_calls.append(cancel_orders)
        return []

    client.close_all_positions = _acknowledge_only  # type: ignore[method-assign]
    try:
        outcome = asyncio.run(adapter.close_all_positions())
    finally:
        client.close_all_positions = original  # type: ignore[method-assign]

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

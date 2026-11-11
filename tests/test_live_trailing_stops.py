"""Issue #167: `percent` trailing stops are executed in the live path.

The live trail is driven against the real `AlpacaLiveAdapter` on top of the
fake trading client the Alpaca control-plane tests already use, extended with
Alpaca's order replace. Signals and fills go through the real database layer,
because the persisted entry signal *is* the restart-safe half of the trailing
state.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pandas as pd
import pytest

from live_runner import daemon, trailing_stops
from live_runner.alpaca_adapter import AlpacaLiveAdapter
from live_runner.base_adapter import BaseLiveAdapter, ProtectiveStop
from live_runner.ctrader_adapter import CTraderLiveAdapter
from live_runner.trailing_stops import (
    TRAILING_RETRY_INITIAL_SECONDS,
    LiveTrailingStopManager,
    adapter_live_trailing_modes,
)
from risk.config import RiskConfig
from runner.engines.signal_replay_adapter import ReplaySignal, SignalReplayAdapter
from shared.models import LiveDeploymentConfig, LiveExecution, LiveSignal
from shared.trailing import (
    LIVE_SUPPORTED_TRAILING_MODES_BY_BROKER,
    live_supported_trailing_modes,
)
from tests.test_alpaca_adapter import (
    _adapter,
    _FakeOrder,
    _FakePosition,
    _FakeTradingClient,
)

T0 = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)
MINUTE = timedelta(minutes=1)
ACCOUNT = "acc-alpaca"


class _ReplacingClient(_FakeTradingClient):
    """The fake trading client plus Alpaca's atomic order replace.

    Like the broker, a refused replace leaves the original order working, and
    an accepted one swaps it for a new order id in one step.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.replace_calls: list[tuple[str, float]] = []
        self.replace_error: Exception | None = None
        self.position_reads = 0

    def get_all_positions(self):
        self.position_reads += 1
        return super().get_all_positions()

    def replace_order_by_id(self, order_id, order_data=None):
        stop_price = float(order_data.stop_price)
        self.replace_calls.append((str(order_id), stop_price))
        if self.replace_error is not None:
            raise self.replace_error
        for index, order in enumerate(self.orders):
            if order.id == order_id:
                replacement = _FakeOrder(
                    id=f"{order_id}+",
                    symbol=order.symbol,
                    side=order.side,
                    order_type=order.order_type,
                    qty=order.qty,
                    stop_price=stop_price,
                )
                self.orders[index] = replacement
                return replacement
        raise RuntimeError(f"order {order_id} not found")

    def stop_prices(self, symbol: str = "AAPL") -> list[float]:
        return [
            order.stop_price
            for order in self.orders
            if order.symbol == symbol and order.order_type == "stop"
        ]


def _bars(closes: list[float], *, start: datetime = T0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": [pd.Timestamp(start + index * MINUTE) for index in range(len(closes))],
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
        }
    )


class _Live:
    """One Alpaca paper account with a long AAPL position and its bracket stop."""

    def __init__(self, database, *, status: str = "ACTIVE", side: str = "buy") -> None:
        self.database = database
        database.ensure_live_account(ACCOUNT, "alpaca")
        self.deployment = LiveDeploymentConfig(
            id=uuid4(),
            strategy_name="trail-probe",
            account_id=ACCOUNT,
            symbols=["AAPL"],
            timeframe="1m",
            status=status,
            signal_source="internal",
        )
        database.create_live_deployment(self.deployment)
        long = side == "buy"
        self.client = _ReplacingClient(
            positions=[_FakePosition("AAPL", 10 if long else -10)],
            orders=[
                _FakeOrder(
                    "stop-1",
                    "AAPL",
                    "sell" if long else "buy",
                    order_type="stop",
                    qty=10,
                    stop_price=98.0 if long else 102.0,
                ),
                _FakeOrder("tp-1", "AAPL", "sell" if long else "buy", qty=10),
            ],
        )
        self.adapter = _adapter(self.client)
        self.closes: list[float] = []
        self.market_data_calls = 0

        async def _market_data(symbol, *, timeframe, limit=200):
            self.market_data_calls += 1
            assert symbol == "AAPL" and timeframe == "1m"
            return _bars(self.closes)

        self.adapter.get_market_data = _market_data  # type: ignore[method-assign]
        self.entry = self.add_signal(side, {"mode": "percent", "percent": 0.01})

    def add_signal(
        self,
        action: str,
        trailing_cfg: dict | None,
        *,
        filled: bool = True,
        at: datetime = T0,
        extra: dict | None = None,
    ) -> LiveSignal:
        metadata: dict = {"stop_loss": 98.0, **(extra or {})}
        if trailing_cfg is not None:
            metadata["trailing_sl_config"] = trailing_cfg
        signal = LiveSignal(
            deployment_id=str(self.deployment.id),
            symbol="AAPL",
            action=action,  # type: ignore[arg-type]
            theoretical_price=100.0,
            timestamp=at,
            metadata=metadata,
        )
        self.database.save_live_signal(signal)
        if filled:
            self.database.save_live_execution(
                LiveExecution(
                    signal_id=signal.id,
                    actual_price=100.0,
                    filled_qty=10.0,
                    commission=0.0,
                    timestamp=at,
                    broker_order_id=f"entry-{signal.id}",
                )
            )
        return signal

    def tick(self, manager: LiveTrailingStopManager, now: datetime) -> None:
        asyncio.run(
            manager.run_once({ACCOUNT: self.adapter}, [self.deployment], now=now)
        )

    def feed(self, manager: LiveTrailingStopManager, closes: list[float]) -> None:
        """Publish closes one bar at a time, ticking right after each close."""
        for close in closes:
            self.closes.append(close)
            self.tick(manager, T0 + len(self.closes) * MINUTE + timedelta(seconds=2))


@pytest.fixture
def live(isolated_db) -> _Live:
    database, _engine, _data_dir = isolated_db
    return _Live(database)


@pytest.fixture
def surfaced(monkeypatch) -> list[tuple[str, dict]]:
    """Every failure the trailing loop surfaces: (log message, Sentry context)."""
    events: list[tuple[str, dict]] = []
    logged: list[str] = []

    def _capture(exc, *, service_name=None, context=None):
        events.append((logged[-1] if logged else "", {**(context or {}), "error": repr(exc)}))

    def _error(message, *args, **kwargs):
        logged.append(str(message))

    monkeypatch.setattr(trailing_stops, "capture_exception", _capture)
    monkeypatch.setattr(trailing_stops.logger, "error", _error)
    return events


# ── the trail itself ─────────────────────────────────────────────────────────


def test_percent_trailing_moves_the_broker_side_stop(live: _Live, surfaced) -> None:
    manager = LiveTrailingStopManager()

    live.feed(manager, [100.0, 103.0])

    # 100 * 0.99 = 99.00, then 103 * 0.99 = 101.97; the take-profit leg is untouched.
    assert surfaced == []
    assert live.client.replace_calls == [("stop-1", 99.0), ("stop-1+", 101.97)]
    assert live.client.stop_prices() == [101.97]
    assert [order.id for order in live.client.orders] == ["stop-1++", "tp-1"]
    assert live.client.cancelled == []
    assert surfaced == []


def test_live_trail_matches_the_replay_adapter_on_the_same_price_series(live: _Live) -> None:
    closes = [100.0, 100.6, 103.0, 104.4, 104.1, 103.9]
    # Replay: the same closes, then a bar that falls through the trailed stop,
    # so the replay exit price *is* the stop the replay trailed to.
    frame = _bars(closes + [90.0])
    frame.loc[len(closes), ["open", "high"]] = [103.9, 103.9]
    frame["volume"] = 1000
    replay = SignalReplayAdapter(
        fee_params={"fees": "zero"},
        risk_config=RiskConfig(
            max_risk_per_trade=0.01,
            max_total_exposure=0.05,
            max_positions=5,
            default_sl_atr_multiplier=2.0,
        ),
        initial_capital=100000.0,
        default_timeframe="1m",
    ).run(
        signals=[
            ReplaySignal.from_payload(
                {
                    "timestamp": T0.isoformat(),
                    "symbol": "AAPL",
                    "action": "buy",
                    "entry_price": 100.0,
                    "stop_loss": 98.0,
                    "take_profit": 140.0,
                    "timeframe": "1m",
                    "metadata": {"trailing_sl_config": {"mode": "percent", "percent": 0.01}},
                },
                default_timeframe="1m",
            )
        ],
        ohlcv_by_symbol={"AAPL": frame},
        timeframe="1m",
        backtest_start="2026-01-05",
        backtest_end="2026-01-05",
    )
    assert len(replay.trades) == 1
    assert replay.trades[0].exit_reason == "trailing_stop"
    replay_stop = float(replay.trades[0].meta["final_stop_loss"])

    live.feed(LiveTrailingStopManager(), closes)

    (live_stop,) = live.client.stop_prices()
    # Same arithmetic; the broker stop is the replay stop rounded down to the
    # cent (never tighter), so it is at most one tick below.
    assert replay_stop == pytest.approx(104.4 * 0.99)
    assert live_stop == 103.35
    assert 0.0 <= replay_stop - live_stop < 0.01


def test_stop_never_moves_backwards(live: _Live, surfaced) -> None:
    manager = LiveTrailingStopManager()

    live.feed(manager, [103.0, 101.0, 99.5, 102.9, 103.0])

    assert live.client.replace_calls == [("stop-1", 101.97)]
    assert live.client.stop_prices() == [101.97]
    assert surfaced == []


def test_short_position_trails_downwards_and_rounds_up(isolated_db) -> None:
    database, _engine, _data_dir = isolated_db
    live = _Live(database, side="sell")
    manager = LiveTrailingStopManager()

    live.feed(manager, [100.0, 97.33, 99.0])

    # 97.33 * 1.01 = 98.3033 -> rounded up (away from the market) to 98.31.
    assert live.client.replace_calls == [("stop-1", 101.0), ("stop-1+", 98.31)]
    assert live.client.stop_prices() == [98.31]


def test_a_bar_that_has_not_closed_yet_is_not_trailed_on(live: _Live) -> None:
    manager = LiveTrailingStopManager()
    # The second bar (a spike to 110) is still forming when the first closes.
    live.closes = [103.0, 110.0]

    live.tick(manager, T0 + MINUTE + timedelta(seconds=2))
    assert live.client.stop_prices() == [101.97]

    # It closes lower than the spike; only that close counts, as in the replay.
    live.closes[1] = 104.0
    live.tick(manager, T0 + 2 * MINUTE + timedelta(seconds=2))
    assert live.client.stop_prices() == [102.96]


def test_bars_closed_before_the_entry_are_ignored(isolated_db) -> None:
    database, _engine, _data_dir = isolated_db
    live = _Live(database)
    # History before the entry at T0 contains a much higher close.
    live.adapter.get_market_data = _history  # type: ignore[method-assign]

    live.tick(LiveTrailingStopManager(), T0 + MINUTE + timedelta(seconds=2))

    assert live.client.stop_prices() == [99.0]


async def _history(symbol, *, timeframe, limit=200):
    return _bars([150.0, 149.0, 100.0], start=T0 - 2 * MINUTE)


def test_nothing_is_read_from_the_broker_between_bar_closes(live: _Live) -> None:
    manager = LiveTrailingStopManager()
    live.feed(manager, [103.0])
    reads, data_calls = live.client.position_reads, live.market_data_calls

    for seconds in (10, 25, 40, 55):
        live.tick(manager, T0 + MINUTE + timedelta(seconds=seconds))

    assert (live.client.position_reads, live.market_data_calls) == (reads, data_calls)


# ── failure handling ─────────────────────────────────────────────────────────


def test_failed_amendment_is_surfaced_retried_and_leaves_the_old_stop(
    live: _Live, surfaced
) -> None:
    manager = LiveTrailingStopManager()
    live.client.replace_error = RuntimeError("422 stop price rejected")

    live.feed(manager, [103.0])

    # Surfaced with context, and the previous broker-side stop is still working.
    assert len(surfaced) == 1
    message, context = surfaced[0]
    assert "previous broker-side stop stays in force" in message
    assert context["symbol"] == "AAPL"
    assert context["account_id"] == ACCOUNT
    assert context["signal_id"] == str(live.entry.id)
    assert context["stop_order_id"] == "stop-1"
    assert context["current_stop"] == 98.0
    assert context["target_stop"] == 101.97
    assert context["consecutive_failures"] == 1
    assert "422 stop price rejected" in context["error"]
    assert [order.id for order in live.client.orders] == ["stop-1", "tp-1"]
    assert live.client.stop_prices() == [98.0]
    assert live.client.cancelled == []

    # Not retried before the backoff elapses, retried after it - within the
    # same bar, without waiting for the next close.
    failed_at = T0 + MINUTE + timedelta(seconds=2)
    live.tick(manager, failed_at + timedelta(seconds=TRAILING_RETRY_INITIAL_SECONDS - 1))
    assert len(live.client.replace_calls) == 1
    live.tick(manager, failed_at + timedelta(seconds=TRAILING_RETRY_INITIAL_SECONDS))
    assert len(live.client.replace_calls) == 2
    assert len(surfaced) == 2
    assert surfaced[1][1]["consecutive_failures"] == 2

    # The broker recovers: the retry applies the same target.
    live.client.replace_error = None
    live.tick(manager, failed_at + timedelta(seconds=TRAILING_RETRY_INITIAL_SECONDS * 3))
    assert live.client.stop_prices() == [101.97]
    assert len(surfaced) == 2


def test_unconfirmed_replace_is_a_failure(live: _Live, surfaced) -> None:
    manager = LiveTrailingStopManager()
    live.client.replace_order_by_id = lambda order_id, order_data=None: SimpleNamespace(  # type: ignore[method-assign]
        id="x", stop_price=98.0
    )

    live.feed(manager, [103.0])

    assert len(surfaced) == 1
    assert "did not confirm" in surfaced[0][1]["error"]


def test_ambiguous_stops_are_reported_and_none_is_amended(live: _Live, surfaced) -> None:
    live.client.orders.append(
        _FakeOrder("stop-2", "AAPL", "sell", order_type="stop", qty=5, stop_price=97.0)
    )

    live.feed(LiveTrailingStopManager(), [103.0])

    assert live.client.replace_calls == []
    assert len(surfaced) == 1
    assert "exactly one amendable protective stop" in surfaced[0][1]["error"]


def test_position_without_a_stop_is_reported(live: _Live, surfaced) -> None:
    live.client.orders = [order for order in live.client.orders if order.id != "stop-1"]

    live.feed(LiveTrailingStopManager(), [103.0])

    assert live.client.replace_calls == []
    assert "found 0" in surfaced[0][1]["error"]


def test_market_data_failure_is_surfaced_and_leaves_the_stop(live: _Live, surfaced) -> None:
    async def _broken(symbol, *, timeframe, limit=200):
        raise ValueError("Alpaca returned no 1m market data for AAPL")

    live.adapter.get_market_data = _broken  # type: ignore[method-assign]

    live.tick(LiveTrailingStopManager(), T0 + MINUTE)

    assert live.client.replace_calls == []
    assert "no 1m market data" in surfaced[0][1]["error"]


# ── restart safety ───────────────────────────────────────────────────────────


def test_restart_continues_from_the_broker_stop_and_never_moves_it_back(
    live: _Live, surfaced
) -> None:
    live.feed(LiveTrailingStopManager(), [100.0, 103.0])
    assert live.client.stop_prices() == [101.97]
    calls_before_restart = len(live.client.replace_calls)

    # A new daemon process: no in-memory state, same database and broker. The
    # price has fallen back since.
    restarted = LiveTrailingStopManager()
    assert restarted.tracked_positions() == set()
    live.feed(restarted, [101.0, 100.2])

    assert len(live.client.replace_calls) == calls_before_restart
    assert live.client.stop_prices() == [101.97]
    assert restarted.tracked_positions() == {(ACCOUNT, "AAPL")}

    # A new high after the restart keeps trailing, from the broker's stop.
    live.feed(restarted, [105.0])
    assert live.client.replace_calls[-1] == ("stop-1++", 103.95)
    assert live.client.stop_prices() == [103.95]
    assert surfaced == []


def test_restart_catches_up_on_closes_missed_while_down(live: _Live) -> None:
    live.feed(LiveTrailingStopManager(), [100.0])
    # Daemon down while two bars close, the first of them a new high.
    live.closes += [104.0, 102.0]

    live.feed(LiveTrailingStopManager(), [101.0])

    assert live.client.stop_prices() == [102.96]


# ── cleanup and attribution ──────────────────────────────────────────────────


def test_position_close_cleans_up_trailing_state(live: _Live, surfaced) -> None:
    manager = LiveTrailingStopManager()
    live.feed(manager, [103.0])
    assert manager.tracked_positions() == {(ACCOUNT, "AAPL")}

    # The stop fills: position and both legs are gone at the broker.
    live.client.positions = []
    live.client.orders = []
    calls = len(live.client.replace_calls)
    live.feed(manager, [110.0])

    assert manager.tracked_positions() == set()
    assert len(live.client.replace_calls) == calls
    assert surfaced == []


def test_partial_exit_keeps_trailing_the_remaining_position(live: _Live) -> None:
    manager = LiveTrailingStopManager()
    live.feed(manager, [103.0])

    # Half the position is closed by an exit-only signal; the stop leg stays.
    live.client.positions = [_FakePosition("AAPL", 5)]
    live.add_signal(
        "sell",
        None,
        at=T0 + MINUTE,
        extra={"exit_only": True, "risk_sizing_mode": "exit_only"},
    )
    live.feed(manager, [106.0])

    assert live.client.stop_prices() == [104.94]
    assert manager.tracked_positions() == {(ACCOUNT, "AAPL")}


def test_an_unfilled_later_signal_does_not_displace_the_open_entry(live: _Live) -> None:
    live.add_signal("buy", {"mode": "none"}, filled=False, at=T0 + MINUTE)

    live.feed(LiveTrailingStopManager(), [103.0])

    assert live.client.stop_prices() == [101.97]


def test_a_newer_filled_entry_without_trailing_ends_the_trail(live: _Live) -> None:
    manager = LiveTrailingStopManager()
    live.feed(manager, [103.0])
    live.add_signal("buy", {"mode": "none"}, at=T0 + MINUTE)

    live.feed(manager, [110.0])

    assert live.client.stop_prices() == [101.97]
    assert manager.tracked_positions() == set()


def test_position_on_the_other_side_of_the_entry_is_not_trailed(live: _Live) -> None:
    live.client.positions = [_FakePosition("AAPL", -10)]
    live.client.orders = [
        _FakeOrder("stop-s", "AAPL", "buy", order_type="stop", qty=10, stop_price=120.0)
    ]

    live.feed(LiveTrailingStopManager(), [103.0])

    assert live.client.replace_calls == []


def test_position_without_a_platform_entry_signal_is_left_alone(isolated_db) -> None:
    database, _engine, _data_dir = isolated_db
    live = _Live(database)
    live.client.positions.append(_FakePosition("MSFT", 3))
    live.client.orders.append(
        _FakeOrder("stop-m", "MSFT", "sell", order_type="stop", qty=3, stop_price=50.0)
    )

    live.feed(LiveTrailingStopManager(), [103.0])

    assert [call[0] for call in live.client.replace_calls] == ["stop-1"]


@pytest.mark.parametrize("status", ["PAUSED", "STOPPED"])
def test_only_active_deployments_are_trailed(isolated_db, status: str) -> None:
    database, _engine, _data_dir = isolated_db
    live = _Live(database, status=status)

    live.feed(LiveTrailingStopManager(), [103.0])

    assert live.client.position_reads == 0
    assert live.client.replace_calls == []


def test_deactivating_a_deployment_drops_its_trailing_state(live: _Live) -> None:
    manager = LiveTrailingStopManager()
    live.feed(manager, [103.0])

    live.deployment = live.deployment.model_copy(update={"status": "PAUSED"})
    live.feed(manager, [110.0])

    assert manager.tracked_positions() == set()
    assert live.client.stop_prices() == [101.97]


# ── adapter capability ───────────────────────────────────────────────────────


class _NoAmendAdapter(BaseLiveAdapter):
    """A broker adapter that does not declare stop amendment."""

    broker_name = "alpaca"

    def __init__(self) -> None:
        self.touched: list[str] = []

    async def connect(self) -> None: ...

    async def submit_order(self, signal, *, qty=None) -> str:
        raise AssertionError("not used")

    async def listen_executions(self) -> None: ...

    async def get_balance(self) -> tuple[float, float]:
        return (0.0, 0.0)

    async def get_positions(self):
        self.touched.append("get_positions")
        return []


def test_adapter_without_the_capability_is_never_trailed(live: _Live) -> None:
    adapter = _NoAmendAdapter()

    asyncio.run(
        LiveTrailingStopManager().run_once(
            {ACCOUNT: adapter}, [live.deployment], now=T0 + MINUTE
        )
    )

    assert adapter.touched == []
    assert adapter_live_trailing_modes(adapter) == {"none"}


def test_base_adapter_refuses_stop_amendment_by_default() -> None:
    adapter = _NoAmendAdapter()
    stop = ProtectiveStop(order_id="1", symbol="AAPL", side="sell", stop_price=1.0)

    assert adapter.supports_stop_amendment is False
    with pytest.raises(NotImplementedError):
        asyncio.run(adapter.list_protective_stops())
    with pytest.raises(NotImplementedError):
        adapter.round_stop_price(stop, 1.0)
    with pytest.raises(NotImplementedError):
        asyncio.run(adapter.amend_protective_stop(stop, 2.0))


def test_capability_and_central_broker_table_agree() -> None:
    """`percent` is enabled exactly for the brokers whose adapter can amend."""
    alpaca = _adapter(_ReplacingClient())
    assert alpaca.supports_stop_amendment is True
    assert adapter_live_trailing_modes(alpaca) == {"none", "percent"}

    assert CTraderLiveAdapter.broker_name == "ctrader"
    assert CTraderLiveAdapter.supports_stop_amendment is BaseLiveAdapter.supports_stop_amendment
    ctrader = SimpleNamespace(broker_name="ctrader", supports_stop_amendment=False)
    assert adapter_live_trailing_modes(ctrader) == {"none"}
    # Even a cTrader adapter that claimed the capability stays on the table's answer.
    assert adapter_live_trailing_modes(
        SimpleNamespace(broker_name="ctrader", supports_stop_amendment=True)
    ) == {"none"}

    assert "percent" in LIVE_SUPPORTED_TRAILING_MODES_BY_BROKER[AlpacaLiveAdapter.broker_name]
    assert LIVE_SUPPORTED_TRAILING_MODES_BY_BROKER["ctrader"] == {"none"}
    assert adapter_live_trailing_modes(None) == {"none"}
    assert adapter_live_trailing_modes(SimpleNamespace(supports_stop_amendment=True)) == {"none"}
    # A truthy-but-not-True flag is not a declared capability.
    assert adapter_live_trailing_modes(
        SimpleNamespace(broker_name="alpaca", supports_stop_amendment="yes")
    ) == {"none"}


# ── Alpaca adapter: reading and moving the stop ──────────────────────────────


def test_alpaca_lists_only_reducing_side_plain_stops() -> None:
    client = _ReplacingClient(
        positions=[_FakePosition("AAPL", 10), _FakePosition("TSLA", -4)],
        orders=[
            # A bracket parent still listed, with its legs nested.
            _FakeOrder(
                "parent",
                "AAPL",
                "buy",
                order_type="market",
                legs=[
                    _FakeOrder("leg-tp", "AAPL", "sell", order_type="limit"),
                    _FakeOrder("leg-sl", "AAPL", "sell", order_type="stop", stop_price=95.0),
                ],
            ),
            _FakeOrder("entry-stop", "AAPL", "buy", order_type="stop", stop_price=120.0),
            _FakeOrder("sl-limit", "AAPL", "sell", order_type="stop_limit", stop_price=94.0),
            _FakeOrder("native-trail", "AAPL", "sell", order_type="trailing_stop", stop_price=93.0),
            _FakeOrder("no-price", "AAPL", "sell", order_type="stop"),
            _FakeOrder("short-sl", "TSLA", "buy", order_type="stop", stop_price=310.0),
            _FakeOrder("flat-symbol", "MSFT", "sell", order_type="stop", stop_price=10.0),
        ],
    )

    stops = asyncio.run(_adapter(client).list_protective_stops())

    assert sorted(stops, key=lambda stop: stop.order_id) == [
        ProtectiveStop(order_id="leg-sl", symbol="AAPL", side="sell", stop_price=95.0),
        ProtectiveStop(order_id="short-sl", symbol="TSLA", side="buy", stop_price=310.0),
    ]


def test_alpaca_rounds_stops_away_from_the_market() -> None:
    adapter = _adapter(_ReplacingClient())
    sell = ProtectiveStop(order_id="1", symbol="AAPL", side="sell", stop_price=1.0)
    buy = ProtectiveStop(order_id="2", symbol="AAPL", side="buy", stop_price=1.0)

    assert adapter.round_stop_price(sell, 101.979) == 101.97
    assert adapter.round_stop_price(buy, 101.971) == 101.98
    # Float noise is not a tick: 103 * 0.99 is 101.97 either way.
    assert adapter.round_stop_price(sell, 103 * 0.99) == 101.97
    assert adapter.round_stop_price(buy, 103 * 0.99) == 101.97
    # Sub-dollar prices trade in 1/100 of a cent.
    assert adapter.round_stop_price(sell, 0.98765) == 0.9876
    assert adapter.round_stop_price(buy, 0.98761) == 0.9877
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            adapter.round_stop_price(sell, bad)


def test_alpaca_amend_replaces_in_place_and_never_cancels() -> None:
    client = _ReplacingClient(
        positions=[_FakePosition("AAPL", 10)],
        orders=[_FakeOrder("sl", "AAPL", "sell", order_type="stop", qty=10, stop_price=95.0)],
    )
    adapter = _adapter(client)
    (stop,) = asyncio.run(adapter.list_protective_stops())

    amended = asyncio.run(adapter.amend_protective_stop(stop, 96.129))

    assert amended == ProtectiveStop(order_id="sl+", symbol="AAPL", side="sell", stop_price=96.12)
    assert client.cancelled == []
    assert client.submitted == []
    assert client.stop_prices() == [96.12]


def test_alpaca_amend_waits_for_a_running_flatten_and_leaves_no_orphan() -> None:
    """A replace must not land between a flatten's order listing and its cancels.

    The replace gives the stop a new id; landing mid-sweep, that new stop would
    survive the flatten (and, once the position is gone, an orphaned sell stop
    opens a short). The adapter serialises both, so the amend runs after the
    flatten, finds its order gone, and fails without leaving anything behind.
    """
    import threading

    class _SlowCancelClient(_ReplacingClient):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.cancel_started = threading.Event()
            self.release_cancel = threading.Event()

        def cancel_order_by_id(self, order_id: str) -> None:
            self.cancel_started.set()
            assert self.release_cancel.wait(5), "test did not release the cancel"
            super().cancel_order_by_id(order_id)

    client = _SlowCancelClient(
        positions=[_FakePosition("AAPL", 10)],
        orders=[_FakeOrder("sl", "AAPL", "sell", order_type="stop", qty=10, stop_price=95.0)],
    )
    adapter = _adapter(client)
    (stop,) = asyncio.run(adapter.list_protective_stops())

    async def scenario() -> BaseException | None:
        flatten = asyncio.create_task(adapter.close_all_positions())
        assert await asyncio.to_thread(client.cancel_started.wait, 5)
        amend = asyncio.create_task(adapter.amend_protective_stop(stop, 96.0))
        await asyncio.sleep(0.05)
        replace_calls_while_flattening = list(client.replace_calls)
        client.release_cancel.set()
        outcome = await flatten
        assert outcome.failed == ()
        assert replace_calls_while_flattening == []
        try:
            await amend
        except Exception as exc:  # the stop it was asked to move is gone
            return exc
        return None

    error = asyncio.run(scenario())

    assert isinstance(error, RuntimeError)
    assert client.positions == []
    assert client.orders == []
    assert client.submitted == []


# ── daemon wiring ────────────────────────────────────────────────────────────


def test_real_money_adapters_are_never_offered_to_the_trailing_loop() -> None:
    paper = _adapter(_ReplacingClient())
    real = AlpacaLiveAdapter(api_key="key", secret_key="secret", paper=False)
    real._connected = True
    disconnected = AlpacaLiveAdapter(api_key="key", secret_key="secret", paper=True)
    disconnected._connected = False

    eligible = daemon._trailing_eligible_adapters(
        {"paper": paper, "real": real, "down": disconnected}
    )

    assert eligible == {"paper": paper}


def test_trailing_loop_survives_a_failing_tick(monkeypatch, surfaced) -> None:
    ticks: list[int] = []

    class _Manager:
        async def run_once(self, adapters, deployments, *, now=None):
            ticks.append(len(ticks))
            if len(ticks) == 1:
                raise RuntimeError("tick blew up")
            raise asyncio.CancelledError

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(trailing_stops.asyncio, "sleep", _no_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            trailing_stops.run_trailing_stops_loop(
                _Manager(),  # type: ignore[arg-type]
                eligible_adapters=dict,
                current_deployments=list,
            )
        )

    assert ticks == [0, 1]
    assert "tick blew up" in surfaced[0][1]["error"]


def test_prepared_order_keeps_the_trailing_config_that_gets_persisted() -> None:
    """The trail reads its config from the persisted (prepared) signal metadata."""

    class _SizingAdapter(_NoAmendAdapter):
        supports_stop_amendment = True  # type: ignore[assignment]

        async def get_balance(self) -> tuple[float, float]:
            return (100_000.0, 100_000.0)

    deployment = LiveDeploymentConfig(
        strategy_name="trail-probe",
        account_id=ACCOUNT,
        symbols=["AAPL"],
        timeframe="1m",
    )
    trailing_cfg = {"mode": "percent", "percent": 0.01}
    signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=T0,
        metadata={
            "stop_loss": 98.0,
            "take_profit": 110.0,
            "trailing_sl_config": trailing_cfg,
        },
    )

    prepared, qty = asyncio.run(
        daemon.prepare_order_with_risk(_SizingAdapter(), deployment, signal)
    )

    assert qty > 0
    assert prepared.metadata["trailing_sl_config"] == trailing_cfg
    assert prepared.metadata["stop_loss"] == 98.0


# ── supertrend_flip stays refused live (#168) ────────────────────────────────

_FULL_FLIP_CONFIG = {
    "mode": "supertrend_flip",
    "breakeven_after_tp1": True,
    "activation_phase": "wait_for_first_green_after_tp1",
    "exit_phase": "exit_on_first_red_after_activation",
    "supertrend_period": 10,
    "supertrend_multiplier": 3.0,
}


def test_no_live_broker_executes_supertrend_flip() -> None:
    """Replay executes supertrend_flip; live does not, on any broker, because
    the full-quantity entry bracket would close everything at TP1."""
    for broker in ("alpaca", "ctrader", "unknown", None):
        assert "supertrend_flip" not in live_supported_trailing_modes(broker)


def test_persisted_supertrend_flip_entry_is_surfaced_and_never_amended(
    live: _Live, surfaced
) -> None:
    """A fully parameterised supertrend_flip entry that reached the database
    anyway is reported on every pass and its stop is left alone."""
    manager = LiveTrailingStopManager()
    live.add_signal(
        "buy",
        _FULL_FLIP_CONFIG,
        at=T0 + timedelta(seconds=1),
        extra={"tp1_price": 110.0, "tp1_exit_percent": 0.5},
    )

    live.feed(manager, [103.0, 108.0])

    assert live.client.replace_calls == []
    assert live.client.cancelled == []
    assert live.client.stop_prices() == [98.0]
    assert surfaced, "the unsupported persisted mode must be surfaced"
    assert all("supertrend_flip" in event[1]["error"] for event in surfaced)

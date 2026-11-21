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
from pathlib import Path
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
from risk import TradeSignal
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
#: The report the loop raises on every transition into the stop-quantity
#: mismatch (#258): an ERROR with a Sentry event, because only an operator can
#: end that pause.
_OVERSIZED_STOP_REPORT = (
    "Protective stop quantity exceeds the open position; the stop is left "
    "untrailed until it is resized to the quantity held"
)
_RETRY_REPORT = (
    "Live trailing stop could not be updated; the previous broker-side stop stays in "
    "force and the update is retried"
)


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
            assert symbol == "AAPL"
            assert timeframe == "1m"
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
    """Everything the trailing loop surfaces: (log message, Sentry context)."""
    events: list[tuple[str, dict]] = []
    logged: list[str] = []

    def _capture(exc, *, service_name=None, context=None):
        events.append((logged[-1] if logged else "", {**(context or {}), "error": repr(exc)}))

    def _capture_message(message, *, service_name=None, context=None, level="error"):
        events.append((logged[-1] if logged else "", {**(context or {}), "message": message}))

    def _error(message, *args, **kwargs):
        logged.append(str(message))

    monkeypatch.setattr(trailing_stops, "capture_exception", _capture)
    monkeypatch.setattr(trailing_stops, "capture_message", _capture_message)
    monkeypatch.setattr(trailing_stops.logger, "error", _error)
    monkeypatch.setattr(trailing_stops.logger, "critical", _error)
    return events


def _infos(monkeypatch) -> list[str]:
    """Every INFO message the trailing loop logs."""
    logged: list[str] = []
    monkeypatch.setattr(
        trailing_stops.logger,
        "info",
        lambda message, *a, **kw: logged.append(str(message)),
    )
    return logged


def _warnings(monkeypatch) -> list[dict]:
    """Every WARNING the trailing loop logs, as its structured data."""
    logged: list[dict] = []
    monkeypatch.setattr(
        trailing_stops.logger,
        "warning",
        lambda message, *a, **kw: logged.append(kw["extra"]["data"]),
    )
    return logged


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

    # The missed new high would trail the stop to 104 * 0.99 = 102.96, which is
    # above the last close of 101: a stop on the wrong side of the market is an
    # immediate fill, not a stop. The amendment goes to the last level still
    # below it (100 * 0.99 = 99.00) and the closes it stops at stay unprocessed
    # for the next pass, so no close is applied to a stop that would cross.
    assert live.client.stop_prices() == [99.0]


def test_a_restart_catch_up_still_applies_a_peak_the_market_has_not_fallen_through(
    live: _Live,
) -> None:
    live.feed(LiveTrailingStopManager(), [100.0])
    # Daemon down while two bars close, the second of them a new high.
    live.closes += [102.0, 104.0]

    live.feed(LiveTrailingStopManager(), [103.0])

    # 104 * 0.99 = 102.96, still below the last close: the catch-up applies it.
    assert live.client.stop_prices() == [102.96]


# ── a stop must never be sent at or beyond the latest close ───────────────────


def test_a_catch_up_never_amends_a_long_stop_above_the_last_close(live: _Live) -> None:
    """A stop above the market is an immediate fill, not a stop.

    The catch-up replays every close it skipped, so a peak the market has since
    fallen through would trail the stop onto the wrong side of the last close:
    the broker refuses it (and the retry loop would end in give-up) or it fills
    at the market on the spot. The amendment goes to the last level that is
    still below the last close instead, and nothing is loosened: the broker's
    own stop stays in force until a better one is confirmed.
    """
    live.feed(LiveTrailingStopManager(), [100.0])
    # Daemon down while the price peaks at 110 and falls back to 104.
    live.closes += [110.0, 104.0]
    manager = LiveTrailingStopManager()

    live.feed(manager, [104.0])

    # 110 * 0.99 = 108.90 is above 104; 100 * 0.99 = 99.00 is not.
    assert live.client.replace_calls == [("stop-1", 99.0)]
    assert live.client.stop_prices() == [99.0]

    # Still below the peak's trailed level: the skipped closes stay unprocessed
    # and nothing is amended, because a stop of 105 * 0.99 = 103.95 would be a
    # level the peak has already overtaken.
    live.feed(manager, [105.0])
    assert live.client.replace_calls == [("stop-1", 99.0)]
    assert live.client.stop_prices() == [99.0]

    # The peak is not lost, only deferred: back at it, the same pass applies it
    # together with the closes since and the stop moves to 108.90.
    live.feed(manager, [110.0])

    assert live.client.replace_calls[-1] == ("stop-1+", 108.9)
    assert live.client.stop_prices() == [108.9]


def test_a_catch_up_never_amends_a_short_stop_below_the_last_close(isolated_db) -> None:
    database, _engine, _data_dir = isolated_db
    live = _Live(database, side="sell")
    live.feed(LiveTrailingStopManager(), [100.0])
    # Daemon down while the price bottoms at 90 and recovers to 96.
    live.closes += [90.0, 96.0]
    manager = LiveTrailingStopManager()

    live.feed(manager, [96.0])

    # 90 * 1.01 = 90.90 is below 96; 100 * 1.01 = 101.00 is not.
    assert live.client.replace_calls == [("stop-1", 101.0)]
    assert live.client.stop_prices() == [101.0]

    live.feed(manager, [90.0])

    assert live.client.replace_calls[-1] == ("stop-1+", 90.9)
    assert live.client.stop_prices() == [90.9]


def test_the_catch_up_after_the_pause_never_crosses_the_last_close(
    live: _Live, surfaced
) -> None:
    """The pause's catch-up is the case the review found: the peak is skipped,
    the market falls back, and the resumed trail would put the stop above it."""
    live.client.positions = [_FakePosition("AAPL", 6)]
    manager = LiveTrailingStopManager()

    live.feed(manager, [100.0, 110.0])
    assert live.client.replace_calls == []
    assert [message for message, _ in surfaced] == [_OVERSIZED_STOP_REPORT]

    # Resized by hand while the market is back at 104.
    live.client.orders = [
        _FakeOrder("stop-1+", "AAPL", "sell", order_type="stop", qty=6, stop_price=98.0),
        _FakeOrder("tp-1", "AAPL", "sell", qty=10),
    ]
    live.feed(manager, [104.0])

    assert live.client.replace_calls == [("stop-1+", 99.0)]
    assert live.client.stop_prices() == [99.0]

    # Back at the peak: it is applied now, together with the closes since.
    live.feed(manager, [110.0])

    assert live.client.replace_calls[-1] == ("stop-1++", 108.9)
    assert live.client.stop_prices() == [108.9]


def test_a_stop_the_market_has_already_fallen_through_is_not_amended(
    live: _Live, surfaced, monkeypatch
) -> None:
    """A broker stop already above the market cannot be trailed at all.

    Amending to a level at or beyond the last close would be refused by the
    broker, so the pass skips the amendment, leaves the bars unprocessed and
    logs a line. It is not a failure: the old stop keeps protecting the
    position and no retry loop runs.
    """
    warnings = _warnings(monkeypatch)
    live.client.orders = [
        _FakeOrder("stop-1", "AAPL", "sell", order_type="stop", qty=10, stop_price=104.0),
        _FakeOrder("tp-1", "AAPL", "sell", qty=10),
    ]
    manager = LiveTrailingStopManager()
    live.closes = [100.0, 101.0]

    live.tick(manager, T0 + 2 * MINUTE + timedelta(seconds=2))

    assert len(warnings) == 1
    assert warnings[0]["symbol"] == "AAPL"
    assert warnings[0]["current_stop"] == 104.0
    assert warnings[0]["last_close"] == 101.0
    assert warnings[0]["unprocessed_bars"] == 2
    assert live.client.replace_calls == []
    assert live.client.stop_prices() == [104.0]
    assert surfaced == []
    trail = manager._trails[(ACCOUNT, "AAPL")]
    assert trail.outage.failures == 0
    assert trail.outage.given_up is False
    assert trail.last_bar_time is None


def test_a_stop_exactly_at_the_last_close_is_not_sent(live: _Live) -> None:
    """At the last close is as bad as beyond it: the order fills immediately.

    The rule is strict (a long's stop below the last close, a short's above),
    and the broker's own stop sitting exactly on the market is the case that
    pins it: no trailed level can be on the right side of it, so the pass
    amends nothing rather than replace one immediate fill with another.
    """
    live.client.orders = [
        _FakeOrder("stop-1", "AAPL", "sell", order_type="stop", qty=10, stop_price=101.0),
        _FakeOrder("tp-1", "AAPL", "sell", qty=10),
    ]
    manager = LiveTrailingStopManager()
    live.closes = [101.0]

    live.tick(manager, T0 + MINUTE + timedelta(seconds=2))

    assert live.client.replace_calls == []
    assert live.client.stop_prices() == [101.0]


def test_a_short_stop_exactly_at_the_last_close_is_not_sent(isolated_db) -> None:
    database, _engine, _data_dir = isolated_db
    live = _Live(database, side="sell")
    live.client.orders = [
        _FakeOrder("stop-1", "AAPL", "buy", order_type="stop", qty=10, stop_price=99.0),
        _FakeOrder("tp-1", "AAPL", "buy", qty=10),
    ]
    manager = LiveTrailingStopManager()
    live.closes = [99.0]

    live.tick(manager, T0 + MINUTE + timedelta(seconds=2))

    assert live.client.replace_calls == []
    assert live.client.stop_prices() == [99.0]


def test_a_short_stop_the_market_has_recovered_above_is_not_amended(
    isolated_db, monkeypatch
) -> None:
    """The rounded price is what reaches the broker, so that is what is checked.

    A short's stop rounds up, away from the market: the level the engine holds
    after a 95.05 close is 96.0005 and the price that would be sent for it is
    96.01. With the market back at 96.01 that level is not a stop - it sits
    exactly on the market and fills on the spot - so the amendment is skipped
    and the stops stay unprocessed.
    """
    database, _engine, _data_dir = isolated_db
    live = _Live(database, side="sell")
    live.closes = [95.05, 96.01]
    warnings = _warnings(monkeypatch)

    live.tick(LiveTrailingStopManager(), T0 + 2 * MINUTE + timedelta(seconds=2))

    assert len(warnings) == 1
    assert warnings[0]["last_close"] == 96.01
    assert warnings[0]["unprocessed_bars"] == 2
    assert live.client.replace_calls == []
    assert live.client.stop_prices() == [102.0]


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


def test_a_partial_exit_pauses_the_trail_until_the_stop_matches_the_position(
    live: _Live, surfaced
) -> None:
    manager = LiveTrailingStopManager()
    live.feed(manager, [103.0])
    assert live.client.stop_prices() == [101.97]

    # Half the position is closed by an exit-only signal; the stop leg keeps the
    # entry's quantity (the broker shape #258 is about).
    live.client.positions = [_FakePosition("AAPL", 5)]
    live.add_signal(
        "sell",
        None,
        at=T0 + MINUTE,
        extra={"exit_only": True, "risk_sizing_mode": "exit_only"},
    )
    live.feed(manager, [106.0])

    assert [message for message, _ in surfaced] == [_OVERSIZED_STOP_REPORT]
    assert surfaced[0][1]["stop_qty"] == 10.0
    assert surfaced[0][1]["position_qty"] == 5.0
    assert live.client.stop_prices() == [101.97]
    assert manager.tracked_positions() == {(ACCOUNT, "AAPL")}

    # The stop is resized to what is held: the trail picks the close it skipped
    # up (106 * 0.99 = 104.94) and the next one after it.
    live.client.orders = [
        _FakeOrder("stop-1+", "AAPL", "sell", order_type="stop", qty=5, stop_price=101.97),
        _FakeOrder("tp-1", "AAPL", "sell", qty=10),
    ]
    live.feed(manager, [107.0])

    assert [message for message, _ in surfaced] == [_OVERSIZED_STOP_REPORT]
    assert live.client.stop_prices() == [105.93]


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
    listing = adapter.list_protective_stops()
    with pytest.raises(NotImplementedError):
        asyncio.run(listing)
    with pytest.raises(NotImplementedError):
        adapter.round_stop_price(stop, 1.0)
    amending = adapter.amend_protective_stop(stop, 2.0)
    with pytest.raises(NotImplementedError):
        asyncio.run(amending)


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
            # A bracket parent still listed with its legs nested: the parent has not
            # filled, so its stop leg is a pending entry's, never the position's.
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
            _FakeOrder("plain-sl", "AAPL", "sell", order_type="stop", stop_price=96.0),
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
        ProtectiveStop(order_id="plain-sl", symbol="AAPL", side="sell", stop_price=96.0),
        ProtectiveStop(order_id="short-sl", symbol="TSLA", side="buy", stop_price=310.0),
    ]


class _HeldLegClient(_ReplacingClient):
    """Alpaca's real bracket shape, recorded on a paper account (2026-11-16).

    Once the entry fills, the take-profit leg is ``new`` and listed by
    ``status=open`` as a top-level order, while the stop leg stays ``held`` and is
    NOT part of any ``status=open`` listing (nested or flat). It is only reachable
    through the filled parent in a ``status=all`` listing, nested under ``legs``.
    A held leg can still be replaced; the replacement is a new order id.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.all_requests: list[object] = []
        self.parent = _FakeOrder(
            "parent",
            "AAPL",
            "buy",
            order_type="market",
            order_class="bracket",
            status="filled",
            legs=[
                _FakeOrder("tp-leg", "AAPL", "sell", order_type="limit", status="new"),
                _FakeOrder(
                    "sl-leg", "AAPL", "sell", order_type="stop", status="held", stop_price=95.0
                ),
            ],
        )
        self.orders = [self.parent.legs[0]]  # what status=open shows

    def get_orders(self, filter=None):  # noqa: A002 - mirrors alpaca-py
        status = str(getattr(getattr(filter, "status", None), "value", "open")).lower()
        if status == "open":
            return list(self.orders)
        self.all_requests.append(filter)
        return [self.parent]

    def replace_order_by_id(self, order_id, order_data=None):
        leg = next((item for item in self.parent.legs if item.id == order_id), None)
        if leg is None:
            raise RuntimeError(f"order {order_id} not found")
        self.replace_calls.append((str(order_id), float(order_data.stop_price)))
        replacement = _FakeOrder(
            id=f"{order_id}+",
            symbol=leg.symbol,
            side=leg.side,
            order_type=leg.order_type,
            status="held",
            stop_price=float(order_data.stop_price),
        )
        self.parent.legs[self.parent.legs.index(leg)] = replacement
        return replacement


def test_alpaca_finds_the_held_stop_leg_of_a_filled_bracket() -> None:
    """The stop leg of a filled bracket is `held` and absent from `status=open`.

    Observed on Alpaca paper: without reading it from the filled parent, the
    trail would report "no stop" for every real bracket position.
    """
    client = _HeldLegClient(positions=[_FakePosition("AAPL", 10)])
    adapter = _adapter(client)

    stops = asyncio.run(adapter.list_protective_stops())

    assert stops == [ProtectiveStop(order_id="sl-leg", symbol="AAPL", side="sell", stop_price=95.0)]
    (request,) = client.all_requests
    assert request.nested is True
    assert [str(symbol) for symbol in request.symbols] == ["AAPL"]


def test_alpaca_amends_the_held_stop_leg_and_keeps_the_take_profit() -> None:
    client = _HeldLegClient(positions=[_FakePosition("AAPL", 10)])
    adapter = _adapter(client)
    (stop,) = asyncio.run(adapter.list_protective_stops())

    amended = asyncio.run(adapter.amend_protective_stop(stop, 96.5))

    assert amended == ProtectiveStop(
        order_id="sl-leg+", symbol="AAPL", side="sell", stop_price=96.5
    )
    assert client.cancelled == []
    assert [leg.id for leg in client.parent.legs] == ["tp-leg", "sl-leg+"]
    (again,) = asyncio.run(adapter.list_protective_stops())
    assert again.order_id == "sl-leg+"
    assert again.stop_price == 96.5


def test_alpaca_ignores_dead_legs_of_a_filled_bracket() -> None:
    client = _HeldLegClient(positions=[_FakePosition("AAPL", 10)])
    client.parent.legs[1].status = "canceled"

    assert asyncio.run(_adapter(client).list_protective_stops()) == []


def test_alpaca_does_not_list_a_held_stop_without_a_position() -> None:
    client = _HeldLegClient(positions=[])

    assert asyncio.run(_adapter(client).list_protective_stops()) == []
    assert client.all_requests == []


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

    loop = trailing_stops.run_trailing_stops_loop(
        _Manager(),  # type: ignore[arg-type]
        eligible_adapters=dict,
        current_deployments=list,
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(loop)

    assert ticks == [0, 1]
    assert "tick blew up" in surfaced[0][1]["error"]


def test_prepared_order_keeps_the_trailing_config_that_gets_persisted() -> None:
    """The trail reads its config from the persisted (prepared) signal metadata."""

    class _SizingAdapter(_NoAmendAdapter):
        supports_stop_amendment = True  # type: ignore[assignment]
        demo = True  # promotion gate (#165): provably demo, so the gate passes through

        async def get_balance(self) -> tuple[float, float]:
            return (100_000.0, 100_000.0)

        async def get_quote(self, symbol: str):
            # Entries need a fresh broker quote for the #125 deviation gate (#141).
            from live_runner.base_adapter import LiveQuote

            return LiveQuote(
                bid=100.0,
                ask=100.0,
                timestamp=datetime.now(timezone.utc),
            )

        async def get_pip_value(self, symbol):
            # Sizing fails closed without a pip value (#146): a USD-quoted share
            # in a USD account moves 1.0 per 1.0.
            from live_runner.base_adapter import LivePipValue

            return LivePipValue(
                value=1.0, units_per_qty=1.0, conversion_rate=1.0,
                quote_asset="USD", account_asset="USD", source="test",
            )

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


# ── review follow-ups (PR #174) ──────────────────────────────────────────────


def test_alpaca_never_offers_the_stop_leg_of_an_unfilled_parent() -> None:
    """A pending entry's stop leg protects nothing that is open yet."""
    client = _ReplacingClient(
        positions=[_FakePosition("AAPL", 10)],
        orders=[
            _FakeOrder(
                "pending-parent",
                "AAPL",
                "buy",
                order_type="market",
                status="new",
                legs=[
                    _FakeOrder(
                        "pending-sl", "AAPL", "sell", order_type="stop", status="held",
                        stop_price=95.0,
                    )
                ],
            ),
        ],
    )

    assert asyncio.run(_adapter(client).list_protective_stops()) == []


def test_alpaca_refuses_a_full_held_leg_page() -> None:
    """A full page may have cut off the filled parent that owns the held stop leg."""
    client = _HeldLegClient(positions=[_FakePosition("AAPL", 10)])
    page = [
        _FakeOrder(f"old-{index}", "AAPL", "buy", order_type="market", status="canceled")
        for index in range(499)
    ]
    client.get_orders = lambda filter=None: (  # type: ignore[method-assign]
        [] if str(filter.status.value).lower() == "open" else [*page, client.parent]
    )

    with pytest.raises(RuntimeError, match="refusing to treat the list as complete"):
        asyncio.run(_adapter(client).list_protective_stops())


def test_alpaca_accepts_a_held_leg_page_just_below_the_limit() -> None:
    client = _HeldLegClient(positions=[_FakePosition("AAPL", 10)])
    page = [
        _FakeOrder(f"old-{index}", "AAPL", "buy", order_type="market", status="canceled")
        for index in range(498)
    ]
    client.get_orders = lambda filter=None: (  # type: ignore[method-assign]
        [] if str(filter.status.value).lower() == "open" else [*page, client.parent]
    )

    (stop,) = asyncio.run(_adapter(client).list_protective_stops())
    assert stop.order_id == "sl-leg"


def test_a_timeframe_the_broker_cannot_serve_is_given_up_on_once(live: _Live, surfaced) -> None:
    """4h passes every validation stage but Alpaca market data cannot serve it."""
    live.deployment.timeframe = "4h"

    async def _unreachable(symbol, *, timeframe, limit=200):
        raise AssertionError("market data must not be requested for an unservable timeframe")

    live.adapter.get_market_data = _unreachable  # type: ignore[method-assign]
    manager = LiveTrailingStopManager()

    for hours in (4, 8, 12):
        live.tick(manager, T0 + timedelta(hours=hours, seconds=2))

    assert live.client.replace_calls == []
    assert len(surfaced) == 1
    assert "cannot serve '4h'" in surfaced[0][1]["error"]
    assert manager._schedules[ACCOUNT].retry_at is None


def test_alpaca_reports_which_timeframes_it_serves() -> None:
    adapter = _adapter(_ReplacingClient())

    assert [
        adapter.supports_market_data_timeframe(value)
        for value in ("1m", "5m", "15m", "30m", "1h", "1d", "4h", "1w", "s1", "tick", "")
    ] == [True] * 6 + [False] * 5


def test_a_repeating_failure_is_given_up_on_after_the_time_limit(live: _Live, surfaced) -> None:
    live.client.orders.append(
        _FakeOrder("stop-2", "AAPL", "sell", order_type="stop", qty=5, stop_price=97.0)
    )
    manager = LiveTrailingStopManager()
    live.closes.append(103.0)
    moment = T0 + MINUTE + timedelta(seconds=2)
    start = moment
    limit = timedelta(seconds=trailing_stops.TRAILING_GIVE_UP_AFTER_SECONDS)

    while moment - start <= limit + timedelta(hours=1):
        live.tick(manager, moment)
        moment += timedelta(seconds=trailing_stops.TRAILING_RETRY_MAX_SECONDS)

    given_up = [event for event in surfaced if "given up" in event[0]]
    assert len(given_up) == 1
    assert surfaced[-1] is given_up[0]
    assert given_up[0][1]["consecutive_failures"] >= 2
    assert live.client.replace_calls == []
    assert manager._schedules[ACCOUNT].retry_at is None

    # A new entry signal is a new position to trail: the give-up does not carry over.
    live.client.orders.pop()
    live.add_signal("buy", {"mode": "percent", "percent": 0.01}, at=T0 + timedelta(seconds=1))
    live.tick(manager, moment)
    live.tick(manager, moment + timedelta(minutes=1))
    assert live.client.replace_calls != []


class _Outage:
    """Market data that fails with a 503 between two moments (a flaky data API)."""

    def __init__(self, live: _Live) -> None:
        self.live = live
        self.failing = False
        working = live.adapter.get_market_data

        async def _flaky(symbol, *, timeframe, limit=200):
            if self.failing:
                raise RuntimeError("503 Service Unavailable")
            return await working(symbol, timeframe=timeframe, limit=limit)

        live.adapter.get_market_data = _flaky  # type: ignore[method-assign]

    def run(self, manager, *, minutes: int, start: int = 1) -> None:
        """Tick once a minute (a 1m deployment visits every bar boundary)."""
        for minute in range(start, start + minutes):
            if len(self.live.closes) < minute:
                self.live.closes.append(100.0 + minute)
            self.live.tick(manager, T0 + minute * MINUTE + timedelta(seconds=2))


def test_a_short_data_outage_does_not_stop_a_1m_trail(live: _Live, surfaced) -> None:
    outage = _Outage(live)
    manager = LiveTrailingStopManager()
    outage.run(manager, minutes=1)
    outage.failing = True

    outage.run(manager, minutes=9, start=2)  # ~9 minutes of 503s: well under the limit

    assert all("given up" not in message for message, _ in surfaced)
    assert not manager._trails[(ACCOUNT, "AAPL")].outage.given_up
    outage.failing = False
    before = len(live.client.replace_calls)
    outage.run(manager, minutes=1, start=11)
    assert len(live.client.replace_calls) > before
    assert manager._trails[(ACCOUNT, "AAPL")].consecutive_failures == 0


def test_a_long_outage_gives_up_once_then_resumes_when_data_recovers(
    live: _Live, surfaced, monkeypatch
) -> None:
    infos = _infos(monkeypatch)
    outage = _Outage(live)
    manager = LiveTrailingStopManager()
    outage.run(manager, minutes=1)
    outage.failing = True
    minutes = trailing_stops.TRAILING_GIVE_UP_AFTER_SECONDS // 60

    outage.run(manager, minutes=minutes + 40, start=2)  # past the limit, then well beyond

    given_up = [message for message, _ in surfaced if "given up" in message]
    assert len(given_up) == 1  # one CRITICAL, no alert storm while probing
    errors_after = [m for m, _ in surfaced[surfaced.index(
        next(e for e in surfaced if "given up" in e[0])
    ) + 1 :]]
    assert errors_after == []
    assert manager._trails[(ACCOUNT, "AAPL")].outage.given_up

    outage.failing = False
    before = len(live.client.replace_calls)
    outage.run(
        manager,
        minutes=trailing_stops.TRAILING_GIVEN_UP_PROBE_SECONDS // 60 + 1,
        start=minutes + 42,
    )

    assert len(live.client.replace_calls) > before  # trailing resumed and amended
    assert not manager._trails[(ACCOUNT, "AAPL")].outage.given_up
    assert any("resumed" in message for message in infos)


def test_an_account_level_listing_failure_counts_once_per_account(
    live: _Live, surfaced
) -> None:
    live.client.positions.append(_FakePosition("MSFT", 10))
    second = LiveSignal(
        deployment_id=str(live.deployment.id),
        symbol="MSFT",
        action="buy",
        theoretical_price=100.0,
        timestamp=T0,
        metadata={"stop_loss": 98.0, "trailing_sl_config": {"mode": "percent", "percent": 0.01}},
    )
    live.database.save_live_signal(second)
    live.database.save_live_execution(
        LiveExecution(
            signal_id=second.id,
            actual_price=100.0,
            filled_qty=10.0,
            commission=0.0,
            timestamp=T0,
            broker_order_id="entry-msft",
        )
    )
    live.deployment.symbols = ["AAPL", "MSFT"]
    listings: list[int] = []

    async def _boom():
        listings.append(1)
        raise RuntimeError("503 listing orders")

    live.adapter.list_protective_stops = _boom  # type: ignore[method-assign]
    manager = LiveTrailingStopManager()
    live.closes.append(103.0)

    live.tick(manager, T0 + MINUTE + timedelta(seconds=2))

    assert len(surfaced) == 1  # one failure for the account, not one per position
    assert len(listings) == 1
    assert manager._schedules[ACCOUNT].stops_outage.failures == 1
    assert manager._schedules[ACCOUNT].retry_at is not None


@pytest.mark.parametrize("timeframe", ["1d", "4h", "1w"])
def test_unservable_timeframes_are_not_trailable_on_alpaca(live: _Live, timeframe: str) -> None:
    assert trailing_stops.adapter_live_trailing_timeframes(live.adapter) == {
        "1m",
        "5m",
        "15m",
        "30m",
        "1h",
    }
    with pytest.raises(ValueError, match="cannot be executed live on timeframe"):
        trailing_stops.validate_adapter_trailing_timeframe(
            {"mode": "percent", "percent": 0.01},
            timeframe,
            adapter=live.adapter,
            context="test",
        )
    # mode none and no config never need a trailable timeframe
    trailing_stops.validate_adapter_trailing_timeframe(
        {"mode": "none"}, timeframe, adapter=live.adapter, context="test"
    )
    trailing_stops.validate_adapter_trailing_timeframe(
        None, timeframe, adapter=live.adapter, context="test"
    )


def test_a_1d_deployment_is_given_up_on_not_trailed_a_session_late(
    live: _Live, surfaced
) -> None:
    live.deployment.timeframe = "1d"
    manager = LiveTrailingStopManager()

    live.tick(manager, T0 + timedelta(days=1, seconds=2))

    assert live.client.replace_calls == []
    assert len(surfaced) == 1
    assert "given up" in surfaced[0][0]
    assert "cannot serve '1d'" in surfaced[0][1]["error"]


def test_a_success_resets_the_failure_count(live: _Live, surfaced) -> None:
    manager = LiveTrailingStopManager()
    live.client.replace_error = RuntimeError("422")
    live.feed(manager, [103.0])
    assert surfaced[-1][1]["consecutive_failures"] == 1

    live.client.replace_error = None
    live.tick(manager, T0 + MINUTE + timedelta(seconds=2 + TRAILING_RETRY_INITIAL_SECONDS * 3))
    assert manager._trails[(ACCOUNT, "AAPL")].consecutive_failures == 0


def test_a_missing_bar_is_retried_only_while_it_can_still_be_feed_delay(live: _Live) -> None:
    live.deployment.timeframe = "1h"

    async def _stale(symbol, *, timeframe, limit=200):
        return _bars([100.0], start=T0 - timedelta(hours=3))  # closed before the entry

    live.adapter.get_market_data = _stale  # type: ignore[method-assign]
    manager = LiveTrailingStopManager()
    boundary = T0 + timedelta(hours=2)

    live.tick(manager, boundary + timedelta(seconds=2))
    assert manager._schedules[ACCOUNT].retry_at is not None

    manager = LiveTrailingStopManager()
    live.tick(manager, boundary + timedelta(seconds=trailing_stops.TRAILING_BAR_GRACE_SECONDS))
    assert manager._schedules[ACCOUNT].retry_at is None


# ── a stop that covers more than the position holds (#258) ──────────────────


def test_a_stop_larger_than_the_position_is_left_untrailed(
    live: _Live, surfaced
) -> None:
    live.client.positions = [_FakePosition("AAPL", 6)]
    manager = LiveTrailingStopManager()

    live.feed(manager, [100.0, 103.0, 104.0])

    # Reported once, and nothing is amended: the stop's quantity is not resized,
    # so the trail touches nothing it has not verified it can move safely.
    assert [message for message, _ in surfaced] == [_OVERSIZED_STOP_REPORT]
    sentry = surfaced[0][1]
    assert sentry["component"] == "live_trailing_stops"
    assert sentry["symbol"] == "AAPL"
    assert sentry["stop_order_id"] == "stop-1"
    assert sentry["stop_qty"] == 10.0
    assert sentry["position_qty"] == 6.0
    assert live.client.replace_calls == []
    assert live.client.stop_prices() == [98.0]
    # Not even market data is read: there is no stop level to compute.
    assert live.market_data_calls == 0


def test_an_oversized_stop_on_a_short_position_is_left_untrailed(
    isolated_db, surfaced
) -> None:
    database, _engine, _data_dir = isolated_db
    live = _Live(database, side="sell")
    live.client.positions = [_FakePosition("AAPL", -6)]

    live.feed(LiveTrailingStopManager(), [100.0, 96.0])

    assert [message for message, _ in surfaced] == [_OVERSIZED_STOP_REPORT]
    assert surfaced[0][1]["stop_qty"] == 10.0
    assert surfaced[0][1]["position_qty"] == 6.0
    assert live.client.replace_calls == []
    assert live.client.stop_prices() == [102.0]


def test_a_stop_quantity_within_qty_tolerance_is_still_trailed(live: _Live, surfaced) -> None:
    live.client.positions = [_FakePosition("AAPL", 10.0000000001)]

    live.feed(LiveTrailingStopManager(), [103.0])

    # Broker float dust on the position qty is not an oversized stop.
    assert surfaced == []
    assert live.client.stop_prices() == [101.97]


@pytest.mark.parametrize(
    ("stop_qty", "trailed"),
    [
        # Inside the tolerance: a stop that covers more than the position only
        # by the broker's own float noise is the position's stop. Exactly one
        # tolerance over still counts as matching (the slack is inclusive).
        (10.0000000005, True),
        (10.000000001, True),
        # Outside it: one more than a ten-thousandth of a unit over is a real
        # over-coverage, and the stop is left alone.
        (10.00001, False),
    ],
)
def test_the_qty_tolerance_boundary_is_what_decides(
    live: _Live, surfaced, stop_qty: float, trailed: bool
) -> None:
    live.client.positions = [_FakePosition("AAPL", 10)]
    live.client.orders = [
        _FakeOrder("stop-1", "AAPL", "sell", order_type="stop", qty=stop_qty, stop_price=98.0),
        _FakeOrder("tp-1", "AAPL", "sell", qty=10),
    ]

    live.feed(LiveTrailingStopManager(), [103.0])

    assert (len(surfaced) == 1) is not trailed
    assert live.client.stop_prices() == ([98.0] if not trailed else [101.97])


def test_a_stop_whose_quantity_the_broker_does_not_report_is_still_trailed(
    isolated_db, surfaced
) -> None:
    database, _engine, _data_dir = isolated_db
    live = _Live(database)
    live.client.orders = [
        _FakeOrder("stop-1", "AAPL", "sell", order_type="stop", qty=0.0, stop_price=98.0),
        _FakeOrder("tp-1", "AAPL", "sell", qty=10),
    ]

    live.feed(LiveTrailingStopManager(), [103.0])

    assert surfaced == []
    assert live.client.stop_prices() == [101.97]


def test_an_oversized_stop_is_not_a_failure_and_does_not_block_the_next_pass(
    live: _Live, surfaced
) -> None:
    live.client.positions = [_FakePosition("AAPL", 6)]
    manager = LiveTrailingStopManager()

    live.tick(manager, T0 + MINUTE + timedelta(seconds=2))

    # The one report is the mismatch itself, not a failure of the pass.
    assert [message for message, _ in surfaced] == [_OVERSIZED_STOP_REPORT]
    trail = manager._trails[(ACCOUNT, "AAPL")]
    assert trail.outage.failures == 0
    assert trail.outage.given_up is False
    assert manager._schedules[ACCOUNT].retry_at is None


def test_a_failure_run_does_not_outlive_the_pause_it_was_recorded_before(
    live: _Live, surfaced, monkeypatch
) -> None:
    """The pause must not import a stale failure run into the resumed trail.

    The skip is not a failure, so it ends the failure run like any other
    successful pass. Leaving the run alive would make the first failure after
    the resize look like the fortieth minute of an outage: one transient
    refusal would give the position up on the spot, with a CRITICAL alert,
    long after the failure that started the run.
    """
    live.client.replace_error = RuntimeError("422 the amendment was refused")
    manager = LiveTrailingStopManager()

    # One transient amendment failure before the partial exit.
    live.feed(manager, [103.0])
    trail = manager._trails[(ACCOUNT, "AAPL")]
    first_failure_at = trail.outage.first_failure_at
    assert trail.outage.failures == 1
    assert [message for message, _ in surfaced] == [_RETRY_REPORT]

    # The stop is left at the entry quantity, so the trail is paused for longer
    # than the give-up window measured from that first failure.
    live.client.positions = [_FakePosition("AAPL", 6)]
    live.tick(manager, T0 + timedelta(minutes=30))
    live.tick(manager, T0 + timedelta(minutes=42, seconds=2))
    assert live.client.stop_prices() == [98.0]
    assert trail.outage.failures == 0
    assert trail.outage.first_failure_at is None

    # The stop is resized to the quantity held and the first pass after the
    # pause fails once: that failure starts a run of its own.
    live.client.orders = [
        _FakeOrder("stop-1+", "AAPL", "sell", order_type="stop", qty=6, stop_price=98.0),
        _FakeOrder("tp-1", "AAPL", "sell", qty=10),
    ]
    live.closes.append(104.0)
    live.tick(manager, T0 + timedelta(minutes=43, seconds=2))

    assert trail.outage.failures == 1
    assert trail.outage.first_failure_at == T0 + timedelta(minutes=43, seconds=2)
    assert trail.outage.first_failure_at != first_failure_at
    assert trail.outage.given_up is False
    assert [message for message, _ in surfaced] == [
        _RETRY_REPORT,
        _OVERSIZED_STOP_REPORT,
        _RETRY_REPORT,
    ]


def test_the_pause_resumes_a_trail_that_was_already_given_up_on(
    live: _Live, surfaced, monkeypatch
) -> None:
    """A given-up outage is cleared by the pause, not probed through it.

    The skip is a pass that reached the broker and found a state an operator
    can fix, so it counts as a success: without that, a position given up on
    before a partial exit stays given up while the stop is oversized, and the
    trail only comes back when the 15-minute probe finally lands after the
    stop has already been resized by hand.
    """
    infos = _infos(monkeypatch)
    live.client.replace_error = RuntimeError("422 the amendment was refused")
    manager = LiveTrailingStopManager()

    # Forty-one minutes of refusals: the outage is given up on (CRITICAL).
    live.closes = [103.0] * 41
    live.tick(manager, T0 + timedelta(minutes=41, seconds=2))
    live.closes.append(104.0)
    live.tick(manager, T0 + timedelta(minutes=82, seconds=2))
    trail = manager._trails[(ACCOUNT, "AAPL")]
    assert trail.outage.given_up is True
    assert surfaced[0][0] == _RETRY_REPORT
    assert surfaced[1][0].startswith("Live trailing stop given up on for this position")

    # The partial exit leaves the stop oversized; the next pass (the probe that
    # is now due) ends the outage instead of failing through it.
    live.client.positions = [_FakePosition("AAPL", 6)]
    live.tick(manager, T0 + timedelta(minutes=97, seconds=2))
    assert trail.outage.given_up is False
    assert trail.outage.failures == 0
    assert trail.outage.next_probe_at is None
    assert list(infos) == ["Live trailing stop resumed after a given-up failure recovered"]
    # The mismatch report that this pass raised is the last thing surfaced, and
    # it is not a failure: nothing was retried and nothing was given up on.
    assert len(surfaced) == 3
    assert surfaced[2][0] == _OVERSIZED_STOP_REPORT

    # Resized by hand: the trail resumes without any further probe delay.
    live.client.replace_error = None
    live.client.orders = [
        _FakeOrder("stop-1+", "AAPL", "sell", order_type="stop", qty=6, stop_price=98.0),
        _FakeOrder("tp-1", "AAPL", "sell", qty=10),
    ]
    live.tick(manager, T0 + timedelta(minutes=98, seconds=2))

    assert live.client.replace_calls[-1] == ("stop-1+", 102.96)
    assert live.client.stop_prices() == [102.96]
    assert trail.outage.given_up is False


def test_the_trail_resumes_and_catches_up_once_the_stop_matches_the_position(
    live: _Live, surfaced
) -> None:
    live.client.positions = [_FakePosition("AAPL", 6)]
    manager = LiveTrailingStopManager()

    live.feed(manager, [100.0, 110.0])
    assert live.client.replace_calls == []
    assert [message for message, _ in surfaced] == [_OVERSIZED_STOP_REPORT]

    # The stop is resized down to what is held: the closes the skip left
    # unprocessed are all applied, so the pause never loses a trailed peak.
    live.client.orders = [
        _FakeOrder("stop-1+", "AAPL", "sell", order_type="stop", qty=6, stop_price=98.0),
        _FakeOrder("tp-1", "AAPL", "sell", qty=10),
    ]
    live.feed(manager, [109.9])

    # One amendment per pass. The target is the *skipped* peak (110 * 0.99 =
    # 108.90), not the newest close (109.9 * 0.99 = 108.80, one tick lower), so
    # the assertion distinguishes replaying every skipped close from reading
    # only the last one.
    assert live.client.replace_calls == [("stop-1+", 108.9)]
    assert live.client.stop_prices() == [108.9]


def test_a_second_oversized_stop_is_reported_again(live: _Live, surfaced) -> None:
    live.client.positions = [_FakePosition("AAPL", 6)]
    manager = LiveTrailingStopManager()

    live.feed(manager, [100.0])
    assert [message for message, _ in surfaced] == [_OVERSIZED_STOP_REPORT]

    # Resized once, then halved again by a second partial exit: the second
    # mismatch is reported too, not swallowed by the first one's latch.
    live.client.orders = [
        _FakeOrder("stop-1+", "AAPL", "sell", order_type="stop", qty=6, stop_price=99.0),
        _FakeOrder("tp-1", "AAPL", "sell", qty=10),
    ]
    live.feed(manager, [103.0])
    assert [message for message, _ in surfaced] == [_OVERSIZED_STOP_REPORT]
    assert live.client.stop_prices() == [101.97]

    live.client.positions = [_FakePosition("AAPL", 3)]
    live.feed(manager, [104.0])
    assert [message for message, _ in surfaced] == [_OVERSIZED_STOP_REPORT] * 2
    assert surfaced[1][1]["stop_qty"] == 6.0
    assert surfaced[1][1]["position_qty"] == 3.0
    assert live.client.stop_prices() == [101.97]


def test_a_stop_matching_the_position_is_not_reported(live: _Live, surfaced) -> None:
    live.feed(LiveTrailingStopManager(), [100.0])

    assert surfaced == []


def test_the_pause_is_an_error_with_a_sentry_event_once_per_transition(
    live: _Live, surfaced, monkeypatch
) -> None:
    """Only a manual resize ends the pause, so it must not be a WARNING.

    The skip leaves a live position untrailed for as long as the operator takes
    to notice, which is exactly the case a WARNING is lost in: the transition
    into the mismatch is an ERROR with one Sentry event, a repeat while the
    mismatch lasts is silent, and the transition back (the resize) is silent too.
    """
    infos = _infos(monkeypatch)
    live.client.positions = [_FakePosition("AAPL", 6)]
    manager = LiveTrailingStopManager()

    live.feed(manager, [100.0, 103.0, 104.0])
    assert [message for message, _ in surfaced] == [_OVERSIZED_STOP_REPORT]
    assert surfaced[0][1]["message"] == _OVERSIZED_STOP_REPORT
    assert surfaced[0][1]["component"] == "live_trailing_stops"
    assert surfaced[0][1]["account_id"] == ACCOUNT
    assert surfaced[0][1]["signal_id"] == str(live.entry.id)
    assert list(infos) == []

    # Resized: the pause ends, the trail resumes on the closes it skipped, and
    # the transition back raises nothing of its own.
    live.client.orders = [
        _FakeOrder("stop-1+", "AAPL", "sell", order_type="stop", qty=6, stop_price=98.0),
        _FakeOrder("tp-1", "AAPL", "sell", qty=10),
    ]
    live.feed(manager, [111.0])

    assert [message for message, _ in surfaced] == [_OVERSIZED_STOP_REPORT]
    assert live.client.replace_calls == [("stop-1+", 109.89)]
    assert live.client.stop_prices() == [109.89]
    assert "Trailed live stop" in list(infos)


def test_alpaca_reports_the_stop_quantity_and_keeps_it_through_an_amend() -> None:
    client = _ReplacingClient(
        positions=[_FakePosition("AAPL", 10)],
        orders=[_FakeOrder("s", "AAPL", "sell", order_type="stop", qty=10, stop_price=95.0)],
    )
    adapter = _adapter(client)

    (stop,) = asyncio.run(adapter.list_protective_stops())
    amended = asyncio.run(adapter.amend_protective_stop(stop, 96.0))

    assert stop.qty == 10.0
    assert amended.qty == 10.0


def test_an_entry_action_that_is_not_buy_or_sell_is_reported_not_skipped(
    live: _Live, surfaced
) -> None:
    def _loader(_deployments, _symbols):
        return {
            "AAPL": {
                "signal_id": "s1",
                "deployment_id": str(live.deployment.id),
                "symbol": "AAPL",
                "action": "long",
                "timestamp": T0,
                "metadata": {"trailing_sl_config": {"mode": "percent", "percent": 0.01}},
            }
        }

    live.closes.append(103.0)
    asyncio.run(
        LiveTrailingStopManager(load_entry_signals=_loader).run_once(
            {ACCOUNT: live.adapter}, [live.deployment], now=T0 + MINUTE + timedelta(seconds=2)
        )
    )

    assert live.client.replace_calls == []
    assert "neither 'buy' nor 'sell'" in surfaced[0][1]["error"]


def test_a_capability_that_is_not_literally_true_is_not_trail_eligible(live: _Live) -> None:
    class _MaybeAdapter(_NoAmendAdapter):
        supports_stop_amendment = None  # type: ignore[assignment]

    adapter = _MaybeAdapter()

    asyncio.run(
        LiveTrailingStopManager().run_once(
            {ACCOUNT: adapter}, [live.deployment], now=T0 + MINUTE
        )
    )

    assert adapter.touched == []


def test_sync_loop_publishes_the_shared_deployment_snapshot(monkeypatch) -> None:
    deployments = [
        LiveDeploymentConfig(
            strategy_name="snap", account_id=ACCOUNT, symbols=["AAPL"], timeframe="1m"
        )
    ]

    async def _load_state():
        return [], deployments

    async def _stop_after_iteration(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(daemon, "_load_db_accounts_and_deployments", _load_state)
    monkeypatch.setattr(daemon.asyncio, "sleep", _stop_after_iteration)
    snapshot: list[LiveDeploymentConfig] = []

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            daemon.sync_deployments_loop(
                {},
                {},
                {},
                accounts_env=Path("/nonexistent/accounts.env"),
                interval_seconds=0,
                deployment_snapshot=snapshot,
            )
        )

    assert snapshot == deployments


def test_discord_dispatch_threads_the_adapter_into_the_processor_gate(monkeypatch) -> None:
    seen: list[object] = []

    def _processor(signal, deployment, *, adapter=None):
        seen.append(adapter)
        return signal

    monkeypatch.setattr(daemon, "_apply_signal_processor", _processor)
    monkeypatch.setattr(daemon, "_validate_signal_age_before_wait", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "signal_is_fresh", lambda *a, **k: True)
    monkeypatch.setattr(daemon, "_deployment_is_currently_active", lambda _deployment: True)
    adapter = _adapter(_ReplacingClient())
    signal = TradeSignal(
        action="buy",
        symbol="AAPL",
        entry_price=100.0,
        stop_loss=98.0,
        take_profit=None,
        timeframe="1m",
        metadata={"source": "discord"},
    )

    prepared = asyncio.run(
        daemon._prepare_discord_signal_for_submission(
            ACCOUNT,
            live_deployment(),
            signal,
            signal_timeframe="1m",
            wait_enabled=False,
            adapter=adapter,
        )
    )

    assert prepared is not None
    assert seen == [adapter]


def live_deployment() -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        strategy_name="discord-probe",
        account_id=ACCOUNT,
        symbols=["AAPL"],
        timeframe="1m",
        signal_source="discord",
    )


def test_entry_signal_lookup_ignores_zero_fill_executions(isolated_db) -> None:
    database, _engine, _data_dir = isolated_db
    probe = _Live(database)
    probe.entry.timestamp = T0  # the genuinely filled, older entry
    unfilled = probe.add_signal("buy", None, filled=False, at=T0 + 5 * MINUTE)
    database.save_live_execution(
        LiveExecution(
            signal_id=unfilled.id,
            actual_price=100.0,
            filled_qty=0.0,
            commission=0.0,
            timestamp=T0 + 5 * MINUTE,
            broker_order_id="zero-fill",
        )
    )

    found = database.find_latest_filled_live_entry_signals([str(probe.deployment.id)], ["AAPL"])

    assert found["AAPL"]["signal_id"] == str(probe.entry.id)


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

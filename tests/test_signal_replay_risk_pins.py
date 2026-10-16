"""The signal-replay engine's risk arithmetic: three rules, none of them tested.

    Why this file exists

    2026-10-15. `runner/engines/signal_replay_adapter.py` holds seven money-named functions, none named
    by a test. Three mutations, against the full suite:

        M39  `_net_pnl_of` silently returns the gross figure         SURVIVED - 5,048 passed
        M40  a stopless position contributes an invented 2% risk      SURVIVED
        M41  `_current_exposure` no longer clamped at zero            SURVIVED

    **M39 is the #84 rule again**, the third sighting of it. `realized_risk_amount` at 21e7a21,
    `_net_realized_pnl` in the validator at af68e42, and now the backtest replay engine: never report
    gross where net is meant. A backtest that overstates profit per trade overstates every aggregate
    built from it, and it does so silently, because gross looks like a number.

    **M40 is the sharpest of the three, because the code it mutates documents the fix in a comment:**

        # C2: no invented stops (same rule as the native engine's exposure math)
        #     - a stopless position contributes nothing here instead of a
        #     2% fantasy distance.

    The mutation reinstates exactly the 2% fantasy the comment says was removed, and 5,048 tests are
    content. A comment describing a removed bug is documentation, not a control; this is the second
    time in this audit that a stated rule turned out to be unpinned (the first was the tolerance at
    af68e42), and it argues for testing the *claim* whenever a comment asserts one.

    **M41** is smaller and mechanical, but it is the same shape: `max(0.0, ...)` removed, so a negative
    exposure can flow into the sizing call.

    These use the real adapter methods against real `_OpenPosition` values rather than stubbing the
    arithmetic, because the failure modes are about which quantity is read - a stub would agree with
    either.
"""

from __future__ import annotations

import pandas as pd
import pytest

from runner.engines.signal_replay_adapter import (
    SignalReplayAdapter,
    _net_pnl_of,
    _OpenPosition,
)
from shared.models import Trade, TradeExit


def _position(**overrides) -> _OpenPosition:
    fields = {
        "symbol": "EURUSD",
        "direction": "LONG",
        "entry_time": pd.Timestamp("2024-01-01"),
        "entry_bar_index": 0,
        "entry_price": 100.0,
        "signal_entry_price": 100.0,
        "quantity": 2.0,
        "stop_loss": 98.0,
        "take_profit": None,
        "initial_stop_loss": 98.0,
        "initial_take_profit": None,
        "risk_amount": None,
        "risk_percent": None,
        "timeframe": "1h",
        "source": "test",
        "signal_time": pd.Timestamp("2024-01-01"),
        "signal_metadata": {},
        "last_checked_at": pd.Timestamp("2024-01-01"),
    }
    fields.update(overrides)
    return _OpenPosition(**fields)


# --- M39: net is never silently gross ------------------------------------------


def _trade(**overrides) -> Trade:
    fields = {
        "id": 1,
        "symbol": "EURUSD",
        "direction": "LONG",
        "entry_time": "2024-01-01T00:00:00Z",
        "entry_price": 100.0,
        "quantity": 2.0,
        "exits": [TradeExit(time="2024-01-02T00:00:00Z", price=102.0, quantity=2.0, pnl=200.0)],
        "pnl": 200.0,
    }
    fields.update(overrides)
    return Trade(**fields)


def test_net_pnl_is_preferred_when_present() -> None:
    assert _net_pnl_of(_trade(net_pnl=190.0, fees=10.0)) == pytest.approx(190.0)


def test_net_pnl_is_never_silently_gross() -> None:
    """No net figure: the answer is gross minus fees, not gross.

    The docstring on this function names the rule - "never silently gross" - so the test asserts it
    rather than trusting the name to keep it true.
    """
    assert _net_pnl_of(_trade(pnl=200.0, fees=10.0, net_pnl=None)) == pytest.approx(190.0)
    assert _net_pnl_of(_trade(pnl=200.0, fees=0.0, net_pnl=None)) == pytest.approx(200.0)


# --- M40: no invented stops ----------------------------------------------------


def test_a_stopless_position_contributes_no_risk_amount() -> None:
    """The code's comment says a stopless position contributes nothing "instead of a 2% fantasy
    distance". The mutation puts that fantasy back, and 5,048 tests were content with it."""
    adapter = SignalReplayAdapter()
    stopless = _position(stop_loss=None)
    assert adapter._position_risk_amount(stopless) == 0.0


def test_a_stoploss_position_contributes_entry_minus_stop_times_quantity() -> None:
    adapter = SignalReplayAdapter()
    # entry 100, stop 98, qty 2 -> 4.0. Not 2% of 100 times 2 (= 4.0 too), so the stop distance is
    # checked on a figure the fantasy would get wrong:
    assert adapter._position_risk_amount(_position(stop_loss=95.0)) == pytest.approx(10.0)
    assert adapter._position_risk_amount(_position(stop_loss=95.0, quantity=3.0)) == pytest.approx(15.0)


# --- M41: exposure cannot go negative -----------------------------------------


def test_exposure_is_risk_over_equity_and_never_negative() -> None:
    adapter = SignalReplayAdapter()
    positions = {"p1": _position(stop_loss=95.0)}  # risk 10.0
    assert adapter._current_exposure(positions, equity=1_000.0) == pytest.approx(0.01)


def test_exposure_is_zero_when_there_is_no_equity() -> None:
    adapter = SignalReplayAdapter()
    positions = {"p1": _position(stop_loss=95.0)}
    assert adapter._current_exposure(positions, equity=0.0) == 0.0


def test_exposure_never_reports_a_negative_fraction() -> None:
    """With no open positions the risk sum is zero and the fraction is zero, not -0.0 or a negative."""
    adapter = SignalReplayAdapter()
    assert adapter._current_exposure({}, equity=1_000.0) == 0.0
    assert adapter._current_exposure({}, equity=-5.0) == 0.0


def test_a_negative_quantity_cannot_produce_negative_exposure() -> None:
    """This is the only case the clamp exists for, and my first version missed it.

    `_position_risk_amount` sums `abs(entry - stop) * quantity`, so with ordinary positive quantities
    the sum is already non-negative and `max(0.0, ...)` never fires. Remove the clamp and every test
    above still passes. A negative quantity is the case that matters: it makes the sum negative, and
    the fraction feeds the sizing cap, so a negative exposure would *raise* the allowed size rather
    than lower it.

    Whether this adapter can hold a negative quantity in practice was not checked - which is exactly
    why the clamp should be pinned rather than reasoned about.
    """
    adapter = SignalReplayAdapter()
    positions = {"p1": _position(stop_loss=95.0, quantity=-2.0)}
    exposure = adapter._current_exposure(positions, equity=1_000.0)
    assert exposure >= 0.0, "exposure must never be negative; a negative cap allowance is larger, not smaller"


# --- M51: the broker truth that binds max_positions ------------------------------


def _replay_signal(**overrides):
    from runner.engines.signal_replay_adapter import ReplaySignal

    fields = {
        "timestamp": pd.Timestamp("2024-01-01"),
        "symbol": "EURUSD",
        "action": "buy",
        "entry_price": 100.0,
        "stop_loss": 98.0,
        "take_profit": None,
        "timeframe": "1h",
        "source": "internal",
        "metadata": {},
    }
    fields.update(overrides)
    return ReplaySignal(**fields)


def test_the_risk_signal_reports_the_real_number_of_open_positions() -> None:
    """`current_positions` is what makes `max_positions` bind.

    Without it the risk engine sizes every signal as if the account were flat, so a backtest can hold
    more simultaneous positions than the configuration allows - and live, where the same field is
    populated, cannot. That divergence is the expensive kind: the backtest says the strategy works.
    """
    adapter = SignalReplayAdapter()
    positions = {"a": _position(), "b": _position(symbol="GBPUSD")}
    signal = adapter._build_risk_signal(
        effective_signal=_replay_signal(),
        target_direction="LONG",
        fill_price=100.0,
        normalized_timeframe="1h",
        open_positions=positions,
    )
    assert signal.metadata["current_positions"] == 2


def test_the_risk_signal_reports_zero_when_nothing_is_open() -> None:
    adapter = SignalReplayAdapter()
    signal = adapter._build_risk_signal(
        effective_signal=_replay_signal(),
        target_direction="LONG",
        fill_price=100.0,
        normalized_timeframe="1h",
        open_positions={},
    )
    assert signal.metadata["current_positions"] == 0


def test_the_risk_signal_carries_the_signal_metadata_through() -> None:
    """A second thing the same dict is responsible for, so both are pinned together."""
    adapter = SignalReplayAdapter()
    signal = adapter._build_risk_signal(
        effective_signal=_replay_signal(metadata={"equity_at_entry": 50_000.0}),
        target_direction="SHORT",
        fill_price=101.0,
        normalized_timeframe="4h",
        open_positions={"a": _position()},
    )
    assert signal.metadata["equity_at_entry"] == pytest.approx(50_000.0)
    assert signal.metadata["current_positions"] == 1
    assert signal.action == "sell"
    assert signal.entry_price == pytest.approx(101.0)

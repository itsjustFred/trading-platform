from __future__ import annotations

import pandas as pd
import pytest

from risk.config import RiskConfig
from runner.engines.signal_replay_adapter import ReplaySignal, SignalReplayAdapter


def test_signal_replay_adapter_updates_trailing_stop_and_exits() -> None:
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01T00:00:00Z", periods=4, freq="1min"),
            "open": [100.0, 100.5, 102.2, 101.0],
            "high": [100.8, 103.0, 102.5, 101.3],
            "low": [99.8, 100.2, 101.0, 100.7],
            "close": [100.0, 103.0, 101.2, 101.0],
            "volume": [1000, 1000, 1000, 1000],
        }
    )
    signal = ReplaySignal.from_payload(
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "symbol": "AAPL",
            "action": "buy",
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit": 140.0,
            "timeframe": "1m",
            "source": "csv:signals.csv",
            "metadata": {
                "trailing_sl_config": {"mode": "percent", "percent": 0.01},
            },
        },
        default_timeframe="1m",
    )

    adapter = SignalReplayAdapter(fee_params={"fees": "zero"},
        risk_config=RiskConfig(
            max_risk_per_trade=0.01,
            max_total_exposure=0.05,
            max_positions=5,
            default_sl_atr_multiplier=2.0,
        ),
        initial_capital=100000.0,
        default_timeframe="1m",
    )
    result = adapter.run(
        signals=[signal],
        ohlcv_by_symbol={"AAPL": frame},
        timeframe="1m",
        backtest_start="2026-01-01",
        backtest_end="2026-01-01",
    )

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "trailing_stop"
    assert trade.meta["signal_source"] == "csv:signals.csv"
    assert trade.meta["signal_stop_loss"] == 98.0
    assert trade.meta["trailing_stop_updates"] >= 1
    assert trade.meta["fill_delay_seconds"] == 0


def _trailing_replay_signal(mode_config: dict) -> ReplaySignal:
    return ReplaySignal.from_payload(
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "symbol": "AAPL",
            "action": "buy",
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit": 140.0,
            "timeframe": "1m",
            "source": "csv:signals.csv",
            "metadata": {"trailing_sl_config": mode_config},
        },
        default_timeframe="1m",
    )


def _one_bar_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01T00:00:00Z", periods=4, freq="1min"),
            "open": [100.0, 100.5, 102.2, 101.0],
            "high": [100.8, 103.0, 102.5, 101.3],
            "low": [99.8, 100.2, 101.0, 100.7],
            "close": [100.0, 103.0, 101.2, 101.0],
            "volume": [1000, 1000, 1000, 1000],
        }
    )


def test_replay_executes_supertrend_flip() -> None:
    """#168 (decided change of the #150 rejection): replay now executes
    supertrend_flip instead of rejecting it. The workflow itself is covered
    in ``tests/test_replay_supertrend_flip.py``."""
    signal = ReplaySignal.from_payload(
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "symbol": "AAPL",
            "action": "buy",
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit": 102.0,
            "timeframe": "1m",
            "metadata": {
                "trailing_sl_config": {
                    "mode": "supertrend_flip",
                    "breakeven_after_tp1": True,
                    "supertrend_period": 2,
                    "supertrend_multiplier": 1.0,
                },
                "tp1_price": 102.0,
                "tp1_exit_percent": 0.5,
            },
        },
        default_timeframe="1m",
    )
    adapter = SignalReplayAdapter(fee_params={"fees": "zero"}, default_timeframe="1m")
    result = adapter.run(
        signals=[signal],
        ohlcv_by_symbol={"AAPL": _one_bar_frame()},
        timeframe="1m",
        backtest_start="2026-01-01",
        backtest_end="2026-01-01",
    )
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exits[0].reason == "tp1"
    assert trade.exits[0].price == 102.0
    assert trade.meta["supertrend_flip"]["tp1_time"] is not None


def test_replay_rejects_supertrend_flip_without_its_parameters() -> None:
    """A bare {"mode": "supertrend_flip"} names the workflow without its
    parameters; it is refused, never run with invented defaults."""
    adapter = SignalReplayAdapter(fee_params={"fees": "zero"}, default_timeframe="1m")
    with pytest.raises(ValueError, match="supertrend_flip"):
        adapter.run(
            signals=[_trailing_replay_signal({"mode": "supertrend_flip"})],
            ohlcv_by_symbol={"AAPL": _one_bar_frame()},
            timeframe="1m",
            backtest_start="2026-01-01",
            backtest_end="2026-01-01",
        )


def test_replay_rejects_unknown_trailing_mode() -> None:
    adapter = SignalReplayAdapter(fee_params={"fees": "zero"}, default_timeframe="1m")
    with pytest.raises(ValueError, match="Unknown trailing stop mode"):
        adapter.run(
            signals=[_trailing_replay_signal({"mode": "martingale_trail"})],
            ohlcv_by_symbol={"AAPL": _one_bar_frame()},
            timeframe="1m",
            backtest_start="2026-01-01",
            backtest_end="2026-01-01",
        )


def test_replay_mode_none_trades_with_initial_stop_only() -> None:
    """#150: mode 'none' is replay-supported: initial stop, no trailing."""
    adapter = SignalReplayAdapter(fee_params={"fees": "zero"}, default_timeframe="1m")
    result = adapter.run(
        signals=[_trailing_replay_signal({"mode": "none"})],
        ohlcv_by_symbol={"AAPL": _one_bar_frame()},
        timeframe="1m",
        backtest_start="2026-01-01",
        backtest_end="2026-01-01",
    )
    assert len(result.trades) == 1
    # The initial stop is carried untouched (no bar in this frame hits it),
    # proving mode 'none' neither trails nor errors.
    assert result.trades[0].meta["final_stop_loss"] == pytest.approx(98.0)
    assert result.trades[0].meta["trailing_stop_updates"] == 0

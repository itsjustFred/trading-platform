from __future__ import annotations

import pandas as pd

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

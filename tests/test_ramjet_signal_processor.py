from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from signal_bots.ramjet import RamJetSignalProcessor


def _sample_market_data(rows: int = 80) -> pd.DataFrame:
    timestamps = pd.date_range("2026-01-01", periods=rows, freq="5min", tz="UTC")
    base = np.linspace(100.0, 120.0, rows)
    close = base + np.sin(np.linspace(0.0, 6.0, rows))
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "Open": close - 0.2,
            "High": close + 1.0,
            "Low": close - 1.0,
            "Close": close,
            "Volume": np.full(rows, 10_000),
        }
    )


def test_process_signal_ports_ramjet_sl_tp_and_trailing_config():
    processor = RamJetSignalProcessor(
        {
            "ram_jet": {
                "atr_period": 14,
                "sl_multiplier": 2.0,
                "tp1_multiplier": 3.0,
                "tp1_exit_percent": 0.5,
                "supertrend_period": 10,
                "supertrend_multiplier": 3.0,
                "candle_timeframe": "5Min",
            }
        }
    )
    market_data = _sample_market_data()

    signal = processor.process_signal(
        {
            "symbol": "aapl",
            "action": "BUY",
            "timestamp": "2026-01-01T12:00:00Z",
            "signal_price": 111.25,
        },
        market_data,
    )

    atr = float(signal.metadata["atr"])
    expected_entry = float(market_data["Close"].iloc[-1])
    assert signal.action == "buy"
    assert signal.symbol == "AAPL"
    assert signal.timeframe == "5m"
    assert signal.entry_price == pytest.approx(expected_entry)
    assert signal.stop_loss == pytest.approx(expected_entry - (atr * 2.0))
    assert signal.take_profit == pytest.approx(expected_entry + (atr * 3.0))
    assert signal.metadata["tp1_price"] == pytest.approx(signal.take_profit)
    assert signal.metadata["tp1_exit_percent"] == pytest.approx(0.5)
    assert signal.metadata["signal_price"] == pytest.approx(111.25)

    trailing = signal.metadata["trailing_sl_config"]
    assert trailing["mode"] == "supertrend_flip"
    assert trailing["breakeven_after_tp1"] is True
    assert trailing["activation_phase"] == "wait_for_first_green_after_tp1"
    assert trailing["exit_phase"] == "exit_on_first_red_after_activation"


def test_process_signal_rejects_non_buy_actions():
    processor = RamJetSignalProcessor({"ram_jet": {"candle_timeframe": "5Min"}})
    with pytest.raises(ValueError, match="BUY/LONG"):
        processor.process_signal(
            {"symbol": "AAPL", "action": "SELL"},
            _sample_market_data(),
        )


def test_processor_loads_default_ramjet_config_file():
    config_path = (
        Path(__file__).resolve().parent.parent / "signal_bots" / "ramjet" / "config.yaml"
    )
    processor = RamJetSignalProcessor.from_config_file(config_path)
    assert processor.atr_period == 14
    assert processor.sl_multiplier == pytest.approx(2.0)
    assert processor.tp1_multiplier == pytest.approx(3.0)
    assert processor.tp1_exit_percent == pytest.approx(0.5)
    assert processor.default_timeframe == "5m"

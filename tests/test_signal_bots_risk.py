from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from risk.config import RiskConfig
from risk.engine import RiskEngine
from signal_bots.nitro import NitroSignalProcessor
from signal_bots.ramjet import RamJetSignalProcessor
from signal_bots.rocketfuel import RocketFuelSignalProcessor


def _sample_market_data(rows: int = 120) -> pd.DataFrame:
    timestamps = pd.date_range("2026-01-01", periods=rows, freq="5min", tz="UTC")
    basis = np.linspace(100.0, 122.0, rows)
    close = basis + np.sin(np.linspace(0.0, 8.0, rows))
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "Open": close - 0.3,
            "High": close + 1.1,
            "Low": close - 1.2,
            "Close": close,
            "Volume": np.full(rows, 50_000),
        }
    )


def _risk_engine() -> RiskEngine:
    return RiskEngine(
        RiskConfig(
            max_risk_per_trade=0.01,
            max_total_exposure=0.05,
            max_positions=5,
            default_sl_atr_multiplier=2.0,
        )
    )


def test_signal_processors_are_importable_with_process_signal() -> None:
    processors = [
        RamJetSignalProcessor({"ram_jet": {"candle_timeframe": "5Min"}}),
        RocketFuelSignalProcessor(
            {"rocket_fuel": {"candle_timeframe": "5Min", "variant": "longsonly"}}
        ),
        NitroSignalProcessor({"ram_jet": {"candle_timeframe": "5Min"}}),
    ]
    for processor in processors:
        assert callable(getattr(processor, "process_signal", None))


def test_signal_with_sl_sizes_from_stop_distance() -> None:
    engine = _risk_engine()
    processor = RamJetSignalProcessor({"ram_jet": {"atr_period": 14, "sl_multiplier": 2.0}})
    signal = processor.process_signal(
        {"symbol": "AAPL", "action": "BUY", "timestamp": "2026-01-01T12:00:00Z"},
        _sample_market_data(),
    )

    sized = engine.calculate_position_size(
        signal=signal,
        account_balance=100_000.0,
        pip_value=1.0,
        current_exposure=0.0,
    )
    risk_distance = abs(signal.entry_price - float(signal.stop_loss))
    assert risk_distance > 0
    assert sized.risk_amount == pytest.approx(1_000.0)
    notional_cap = 100_000.0 * engine.config.max_notional_per_trade
    expected = min(1_000.0 / risk_distance, notional_cap / signal.entry_price)
    assert sized.position_size == pytest.approx(expected)


def test_signal_without_sl_uses_atr_fallback_stop() -> None:
    engine = _risk_engine()
    processor = RocketFuelSignalProcessor(
        {"rocket_fuel": {"atr_period": 14, "sl_multiplier": 3.0, "variant": "longsonly"}}
    )
    signal = processor.process_signal(
        {"symbol": "MSFT", "action": "SELL", "timestamp": "2026-01-01T12:00:00Z"},
        _sample_market_data(),
    )
    assert signal.stop_loss is None
    assert float(signal.metadata["atr"]) > 0

    sized = engine.calculate_position_size(
        signal=signal,
        account_balance=100_000.0,
        pip_value=1.0,
        current_exposure=0.0,
    )
    assert signal.stop_loss is not None
    assert signal.metadata["resolved_stop_loss"] == signal.stop_loss
    assert sized.position_size > 0


def test_trailing_sl_signal_sizes_then_updates_stop() -> None:
    engine = _risk_engine()
    processor = NitroSignalProcessor({"ram_jet": {"atr_period": 14, "sl_multiplier": 2.0}})
    signal = processor.process_signal(
        {"symbol": "NVDA", "action": "BUY", "timestamp": "2026-01-01T12:00:00Z"},
        _sample_market_data(),
    )
    signal.metadata["trailing_sl_config"] = {"mode": "percent", "percent": 0.02}
    initial_stop = float(signal.stop_loss or 0.0)

    sized = engine.calculate_position_size(
        signal=signal,
        account_balance=100_000.0,
        pip_value=1.0,
        current_exposure=0.0,
    )
    assert sized.position_size > 0

    updated_stop = engine.apply_trailing_stop(signal, market_price=signal.entry_price * 1.03)
    assert updated_stop is not None
    assert float(updated_stop) > initial_stop


def test_exposure_limit_reduces_position_size() -> None:
    engine = _risk_engine()
    processor = RocketFuelSignalProcessor(
        {"rocket_fuel": {"atr_period": 14, "sl_multiplier": 3.0, "variant": "longandshort"}}
    )
    signal = processor.process_signal(
        {"symbol": "TSLA", "action": "BUY", "timestamp": "2026-01-01T12:00:00Z"},
        _sample_market_data(),
    )

    baseline = engine.calculate_position_size(
        signal=signal,
        account_balance=100_000.0,
        pip_value=1.0,
        current_exposure=0.0,
    )
    reduced = engine.calculate_position_size(
        signal=signal,
        account_balance=100_000.0,
        pip_value=1.0,
        current_exposure=0.047,
    )

    assert reduced.risk_percent < baseline.risk_percent
    assert reduced.position_size < baseline.position_size
    assert reduced.position_size > 0.0

"""Contract tests for the shared builders in ``tests.builders``."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from runner.engines.base import BacktestConfig
from shared.models import LiveDeploymentConfig, LiveSignal
from tests.builders import (
    make_backtest_config,
    make_live_deployment,
    make_live_signal,
    make_ohlcv,
)


def test_make_live_deployment_defaults_and_overrides():
    deployment = make_live_deployment()
    assert isinstance(deployment, LiveDeploymentConfig)
    assert deployment.strategy_name == "test-strategy"
    overridden = make_live_deployment(symbols=["MSFT"], timeframe="15m")
    assert overridden.symbols == ["MSFT"]
    assert overridden.timeframe == "15m"
    assert overridden.id != deployment.id


def test_make_live_signal_defaults_are_deterministic():
    signal = make_live_signal()
    assert isinstance(signal, LiveSignal)
    assert signal.timestamp == datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert make_live_signal().timestamp == signal.timestamp
    assert signal.metadata == {}


def test_make_backtest_config_requires_the_window():
    with pytest.raises(TypeError):
        make_backtest_config()  # type: ignore[call-arg]
    config = make_backtest_config(start_date="2024-01-01", end_date="2024-12-31")
    assert isinstance(config, BacktestConfig)
    assert config.start_date == "2024-01-01"
    assert config.end_date == "2024-12-31"


def test_make_ohlcv_shapes():
    frame = make_ohlcv(periods=3)
    assert list(frame.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    indexed = make_ohlcv(closes=[1.0, 2.0], as_index=True)
    assert "timestamp" not in indexed.columns
    assert str(indexed.index.dtype).startswith("datetime64")


def test_make_ohlcv_rejects_malformed_bars():
    with pytest.raises(ValueError, match="malformed bar"):
        make_ohlcv(bars=[(100.0, 99.0, 98.0, 100.0)])
    with pytest.raises(ValueError, match="malformed bar"):
        make_ohlcv(bars=[(100.0, 101.0, 102.0, 100.0)])
    with pytest.raises(ValueError, match="needs one of"):
        make_ohlcv()

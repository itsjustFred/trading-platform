from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from signal_bots.nitro import NitroSignalProcessor
from signal_bots.ramjet import RamJetSignalProcessor
from signal_bots.registry import get_signal_processor, list_signal_processor_options
from signal_bots.rocketfuel import RocketFuelSignalProcessor


def _sample_market_data(rows: int = 80) -> pd.DataFrame:
    timestamps = pd.date_range("2026-01-01", periods=rows, freq="5min", tz="UTC")
    base = np.linspace(90.0, 110.0, rows)
    close = base + np.sin(np.linspace(0.0, 4.0, rows))
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": close - 0.2,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(rows, 10_000.0),
        }
    )


def test_registry_options_expose_all_expected_ids() -> None:
    options = list_signal_processor_options()
    ids = {item["id"] for item in options}
    assert {"stockkit", "ramjet", "rocketfuel_longsonly", "rocketfuel_longandshort", "nitro"} <= ids


def test_registry_returns_expected_processor_classes() -> None:
    assert isinstance(get_signal_processor("stockkit", {}), RamJetSignalProcessor)
    assert isinstance(get_signal_processor("ramjet", {}), RamJetSignalProcessor)
    assert isinstance(get_signal_processor("rocketfuel_longsonly", {}), RocketFuelSignalProcessor)
    assert isinstance(
        get_signal_processor("rocketfuel_longandshort", {}),
        RocketFuelSignalProcessor,
    )
    assert isinstance(get_signal_processor("nitro", {}), NitroSignalProcessor)


def test_rocketfuel_variants_differ_on_sell_signal() -> None:
    market_data = _sample_market_data()
    long_only = get_signal_processor("rocketfuel_longsonly", {})
    long_short = get_signal_processor("rocketfuel_longandshort", {})
    signal_payload = {"symbol": "AMD", "action": "SELL", "timestamp": "2026-01-01T12:00:00Z"}

    long_only_signal = long_only.process_signal(signal_payload, market_data)
    long_short_signal = long_short.process_signal(signal_payload, market_data)

    assert long_only_signal.metadata.get("exit_only") is True
    assert long_only_signal.stop_loss is None
    assert long_short_signal.stop_loss is not None
    assert float(long_short_signal.stop_loss) > long_short_signal.entry_price


def test_nitro_rejects_sell_signals() -> None:
    processor = get_signal_processor("nitro", {})
    with pytest.raises(ValueError, match="BUY/LONG"):
        processor.process_signal(
            {"symbol": "AAPL", "action": "SELL", "timestamp": "2026-01-01T12:00:00Z"},
            _sample_market_data(),
        )

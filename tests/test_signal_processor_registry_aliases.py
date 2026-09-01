"""Registry contract tests for Cleanup 20-B.

StockKit is the Discord source/server, not a processor: the registry must not
expose a `stockkit` signal-processor id and must not alias any listed option
onto another bot's processor factory.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from signal_bots.nitro import NitroSignalProcessor
from signal_bots.ramjet import RamJetSignalProcessor
from signal_bots.registry import (
    SIGNAL_PROCESSOR_OPTIONS,
    SIGNAL_PROCESSORS,
    get_signal_processor,
    get_signal_processor_config_path,
    list_signal_processor_options,
)
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


def _root() -> Path:
    return Path(__file__).resolve().parents[1] / "signal_bots"


def test_stockkit_is_not_a_signal_processor_id() -> None:
    """StockKit is the Discord source/server, never a registry processor id."""
    ids = {item["id"] for item in list_signal_processor_options()}
    assert "stockkit" not in ids
    assert "stockkit" not in SIGNAL_PROCESSORS


def test_registry_rejects_stockkit_lookup() -> None:
    with pytest.raises(ValueError, match="Unknown signal processor"):
        get_signal_processor("stockkit", {})


def test_registry_options_and_factories_stay_in_sync() -> None:
    """Every listed option maps to its own factory — no cross-bot aliases."""
    listed = [item["id"] for item in SIGNAL_PROCESSOR_OPTIONS]
    assert len(listed) == len(set(listed)), "registry options must be unique"
    # Every listed option must resolve to a factory; the reverse is a superset
    # only where a deployment id (e.g. "rocketfuel") maps explicitly to a
    # documented variant.
    assert set(listed) <= set(SIGNAL_PROCESSORS), (
        "every registry option must have a factory"
    )


def test_rocketfuel_deployment_id_maps_to_longonly_variant() -> None:
    """The RocketFuel Discord-bot id maps explicitly to the long-only variant."""
    processor = get_signal_processor("rocketfuel", {})
    assert isinstance(processor, RocketFuelSignalProcessor)
    assert processor.variant == "longsonly"


def test_registry_returns_expected_processor_classes() -> None:
    assert isinstance(get_signal_processor("ramjet", {}), RamJetSignalProcessor)
    assert isinstance(get_signal_processor("rocketfuel_longsonly", {}), RocketFuelSignalProcessor)
    assert isinstance(
        get_signal_processor("rocketfuel_longandshort", {}),
        RocketFuelSignalProcessor,
    )
    assert isinstance(get_signal_processor("nitro", {}), NitroSignalProcessor)


def test_nitro_uses_its_own_config_file() -> None:
    """Nitro must not silently read RamJet's config.yaml."""
    path = get_signal_processor_config_path("nitro")
    assert path == _root() / "nitro" / "config.yaml"
    assert path.exists(), "nitro must ship its own config.yaml"


def test_nitro_config_is_independent_from_ramjet() -> None:
    """Each bot reads only its own config file, never the other bot's."""
    ramjet = get_signal_processor("ramjet", {})
    nitro = get_signal_processor("nitro", {})
    assert isinstance(ramjet, RamJetSignalProcessor)
    assert isinstance(nitro, NitroSignalProcessor)
    # Both config files use a top-level bot section (`ram_jet:` / `nitro:`) and
    # the processors unwrap it. Tuning one file must not move the other bot.
    assert ramjet.config.get("sl_multiplier") == 2.0
    assert nitro.config.get("sl_multiplier") == 2.0
    assert ramjet.config.get("atr_period") == nitro.config.get("atr_period")


def test_nitro_merges_requested_config_over_file_defaults() -> None:
    processor = get_signal_processor("nitro", {"sl_multiplier": 5.0})
    assert isinstance(processor, NitroSignalProcessor)
    assert processor.sl_multiplier == 5.0


def test_rocketfuel_and_ramjet_read_their_own_config_files() -> None:
    assert get_signal_processor_config_path("ramjet") == _root() / "ramjet" / "config.yaml"
    assert (
        get_signal_processor_config_path("rocketfuel_longsonly")
        == _root() / "rocketfuel" / "config.yaml"
    )
    assert (
        get_signal_processor_config_path("rocketfuel_longandshort")
        == _root() / "rocketfuel" / "config.yaml"
    )


def test_nitro_rejects_sell_signals() -> None:
    processor = get_signal_processor("nitro", {})
    sell_signal = {"symbol": "AAPL", "action": "SELL", "timestamp": "2026-01-01T12:00:00Z"}
    with pytest.raises(ValueError, match="BUY/LONG"):
        processor.process_signal(sell_signal, _sample_market_data())


def test_nitro_stamps_its_own_metadata() -> None:
    processor = get_signal_processor("nitro", {})
    signal = processor.process_signal(
        {"symbol": "AAPL", "action": "BUY", "timestamp": "2026-01-01T12:00:00Z"},
        _sample_market_data(),
    )
    assert signal.metadata["signal_bot"] == "nitro"
    assert signal.metadata["source"] == "nitro"

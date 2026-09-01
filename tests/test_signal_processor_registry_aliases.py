"""Registry contract tests for Cleanup 20-B.

StockKit is the Discord source/server, not a processor: the registry must not
expose a `stockkit` signal-processor id and must not alias any listed option
onto another bot's processor factory.
"""
from __future__ import annotations

from pathlib import Path

import yaml
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
from tests.signal_processor_helpers import sample_signal_market_data


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
    long_only = get_signal_processor("rocketfuel_longsonly", {})
    long_short = get_signal_processor("rocketfuel_longandshort", {})
    assert isinstance(long_only, RocketFuelSignalProcessor)
    assert isinstance(long_short, RocketFuelSignalProcessor)
    # Each option id resolves to its distinct intended variant, not just to the
    # shared RocketFuel class.
    assert long_only.variant == "longsonly"
    assert long_short.variant == "longandshort"
    assert isinstance(get_signal_processor("nitro", {}), NitroSignalProcessor)


def test_nitro_uses_its_own_config_file() -> None:
    """Nitro must not silently read RamJet's config.yaml."""
    path = get_signal_processor_config_path("nitro")
    assert path == _root() / "nitro" / "config.yaml"
    assert path.exists(), "nitro must ship its own config.yaml"


def _bot_section(bot_dir: str) -> dict:
    payload = yaml.safe_load(
        get_signal_processor_config_path(bot_dir).read_text(encoding="utf-8")
    )
    return next(iter(payload.values()))


def test_nitro_config_is_independent_from_ramjet() -> None:
    """Each bot reads only its own config file, never the other bot's.

    The processors are checked against their OWN file's values, so the bots may
    diverge freely; only the file->processor wiring is pinned here.
    """
    ramjet = get_signal_processor("ramjet", {})
    nitro = get_signal_processor("nitro", {})
    assert isinstance(ramjet, RamJetSignalProcessor)
    assert isinstance(nitro, NitroSignalProcessor)
    ramjet_section = _bot_section("ramjet")
    nitro_section = _bot_section("nitro")
    assert ramjet.config.get("sl_multiplier") == ramjet_section["sl_multiplier"]
    assert nitro.config.get("sl_multiplier") == nitro_section["sl_multiplier"]
    assert ramjet.config.get("atr_period") == ramjet_section["atr_period"]
    assert nitro.config.get("atr_period") == nitro_section["atr_period"]


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
    market_data = sample_signal_market_data()
    with pytest.raises(ValueError, match="BUY/LONG"):
        processor.process_signal(sell_signal, market_data)


def test_nitro_stamps_its_own_metadata() -> None:
    processor = get_signal_processor("nitro", {})
    signal = processor.process_signal(
        {"symbol": "AAPL", "action": "BUY", "timestamp": "2026-01-01T12:00:00Z"},
        sample_signal_market_data(),
    )
    assert signal.metadata["signal_bot"] == "nitro"
    assert signal.metadata["source"] == "nitro"

def test_every_option_id_resolves_to_a_config_path() -> None:
    """Every listed option resolves to an existing per-bot config file.

    `get_signal_processor_config_path` is derived from the registry; this pins
    the invariant so a new bot cannot pass the health check while its config
    path lookup raises.
    """
    for option in SIGNAL_PROCESSOR_OPTIONS:
        path = get_signal_processor_config_path(option["id"])
        assert path.exists(), f"missing config file for {option['id']}: {path}"


def test_shipped_config_files_have_a_single_bot_section() -> None:
    """Each bot config has exactly one top-level section.

    `_build_from_config` merges caller overrides into that section; a second
    top-level key would silently disable the merge (see registry docstring).
    """
    for option in SIGNAL_PROCESSOR_OPTIONS:
        path = get_signal_processor_config_path(option["id"])
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict), f"{path} must decode to a mapping"
        assert len(payload) == 1, (
            f"{path} must contain exactly one top-level bot section"
        )


def test_ramjet_reads_its_shipped_config_values() -> None:
    """RamJet picks up tuned values from its own config file, not defaults."""
    processor = get_signal_processor("ramjet", {})
    assert isinstance(processor, RamJetSignalProcessor)
    assert processor.config.get("sl_multiplier") == 2.0
    assert processor.config.get("atr_period") == 14

"""Signal processor registry.

Each signal bot/strategy owns its processor factory and config file. There are
no cross-bot aliases: StockKit is the Discord source/server these bots trade
on, not a processor, so it is deliberately absent from this registry.

Adding a bot means adding one option entry, one factory and one config path —
all explicit, all derived from code, never from a user-supplied string.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import yaml

_BOT_CONFIG_FILE = "config.yaml"

from signal_bots.nitro import NitroSignalProcessor
from signal_bots.ramjet import RamJetSignalProcessor
from signal_bots.rocketfuel import RocketFuelSignalProcessor

SignalProcessorFactory = Callable[[dict[str, Any]], object]

_ROOT = Path(__file__).resolve().parent
CONFIG_FILENAME = "config.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        # A missing mapped config must be loud: an empty dict here would make
        # every bot built from it silently fall back to class defaults while the
        # health check keeps validating the (missing) path. Callers that pass a
        # per-call config override still get the override, never a partial file.
        raise FileNotFoundError(
            f"Signal processor config file not found: {path} "
            f"(mapped via _BOT_DIRS — update signal_bots/registry.py if it moved)"
        )
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Signal processor config must decode to mapping: {path}")
    return payload


def _build_from_config(
    config_path: Path,
    factory: Callable[[dict[str, Any]], object],
    config: dict[str, Any],
) -> object:
    """Merge the bot's own config file with caller overrides and build it.

    `config` is caller-controlled data used as *values* only; it never selects
    a file path, module or factory. When the file nests settings under a single
    bot section (e.g. ``ram_jet:``), overrides merge into that section — the
    processor unwraps it, so a top-level copy would be discarded. Only a
    sectionless file merges at the top level, where per-bot tuning always wins
    over file defaults.
    """
    payload = _load_yaml(config_path)
    merged = dict(payload)
    overrides = dict(config or {})
    sections = [key for key, value in payload.items() if isinstance(value, dict)]
    if len(sections) > 1:
        raise ValueError(
            "Signal processor config must have exactly one bot section: "
            f"{config_path}"
        )
    if len(sections) == 1:
        section = dict(payload[sections[0]])
        section.update(overrides)
        merged[sections[0]] = section
    else:
        merged.update(overrides)
    return factory(merged)


# Single source of truth for "which config file backs which registered id".
# Options list, factories and config-path lookups are all derived views of the
# ids listed here; adding a bot means one entry here plus its factory.
_BOT_DIRS: dict[str, str] = {
    "ramjet": "ramjet",
    # "rocketfuel" is the RocketFuel Discord-bot id (live_deployments.yaml) and
    # maps to the long-only variant's config file, like its explicit variants.
    "rocketfuel": "rocketfuel",
    "rocketfuel_longsonly": "rocketfuel",
    "rocketfuel_longandshort": "rocketfuel",
    "nitro": "nitro",
}


def _bot_config_path(bot_dir: str) -> Path:
    """Config file for a bot directory; ids are code-owned, never user input."""
    return _ROOT / bot_dir / CONFIG_FILENAME


def get_signal_processor_config_path(name: str) -> Path:
    """Return the config file that backs a registered processor id.

    Derived from `_BOT_DIRS`, not from the requested string: an unknown id
    raises instead of producing a filesystem path.
    """
    normalized = str(name or "").strip().lower()
    bot_dir = _BOT_DIRS.get(normalized)
    if bot_dir is None:
        raise ValueError(f"Unknown signal processor: {name}")
    return _bot_config_path(bot_dir)


def _build_from_bot_dir(
    bot_dir: str,
    factory: Callable[[dict[str, Any]], object],
    config: dict[str, Any],
) -> object:
    return _build_from_config(_bot_config_path(bot_dir), factory, config)


def _build_ramjet(config: dict[str, Any]) -> RamJetSignalProcessor:
    return _build_from_bot_dir("ramjet", RamJetSignalProcessor, config)


def _build_nitro(config: dict[str, Any]) -> NitroSignalProcessor:
    return _build_from_bot_dir("nitro", NitroSignalProcessor, config)


def _build_rocketfuel(variant: str, config: dict[str, Any]) -> RocketFuelSignalProcessor:
    def _factory(merged: dict[str, Any]) -> RocketFuelSignalProcessor:
        # Shipped configs always carry a `rocket_fuel:` section (pinned by
        # test_shipped_config_files_have_a_single_bot_section). A missing or
        # malformed one is a config error, not a layout to accommodate: the
        # override merge nests the section back under `rocket_fuel`, so check
        # the effective payload at both levels.
        rocket_fuel = merged.get("rocket_fuel")
        if not isinstance(rocket_fuel, dict):
            raise ValueError("RocketFuel config must contain a rocket_fuel mapping")
        rocket_fuel["variant"] = variant
        return RocketFuelSignalProcessor(merged)

    return _build_from_bot_dir("rocketfuel", _factory, config)


def _build_rocketfuel_longsonly(config: dict[str, Any]) -> RocketFuelSignalProcessor:
    return _build_rocketfuel("longsonly", config)


def _build_rocketfuel_longandshort(config: dict[str, Any]) -> RocketFuelSignalProcessor:
    return _build_rocketfuel("longandshort", config)


SIGNAL_PROCESSOR_OPTIONS: list[dict[str, str]] = [
    {
        "id": "ramjet",
        "label": "RamJet",
        "description": "ATR + Supertrend BUY-only setup with TP1 and trailing metadata.",
    },
    {
        "id": "rocketfuel_longsonly",
        "label": "RocketFuel (Long-only)",
        "description": "RocketFuel BUY signals; SELL handled as exit-only (no short entry).",
    },
    {
        "id": "rocketfuel_longandshort",
        "label": "RocketFuel (Long+Short)",
        "description": "RocketFuel BUY and SELL both open positions (SELL => short entry).",
    },
    {
        "id": "nitro",
        "label": "Nitro",
        "description": "RamJet-equivalent formulas with strict BUY-only signal intake.",
    },
]


SIGNAL_PROCESSORS: dict[str, SignalProcessorFactory] = {
    "ramjet": _build_ramjet,
    # "rocketfuel" is the RocketFuel Discord-bot id (live_deployments.yaml); it
    # maps to the long-only variant explicitly, not via an undocumented alias.
    "rocketfuel": _build_rocketfuel_longsonly,
    "rocketfuel_longsonly": _build_rocketfuel_longsonly,
    "rocketfuel_longandshort": _build_rocketfuel_longandshort,
    "nitro": _build_nitro,
}


def list_signal_processor_options() -> list[dict[str, str]]:
    return [dict(item) for item in SIGNAL_PROCESSOR_OPTIONS]


def get_signal_processor(name: str, config: dict[str, Any] | None = None) -> object:
    normalized = str(name or "").strip().lower()
    factory = SIGNAL_PROCESSORS.get(normalized)
    if factory is None:
        raise ValueError(f"Unknown signal processor: {name}")
    return factory(dict(config or {}))

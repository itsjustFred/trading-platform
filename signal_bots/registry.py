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

from signal_bots.nitro import NitroSignalProcessor
from signal_bots.ramjet import RamJetSignalProcessor
from signal_bots.rocketfuel import RocketFuelSignalProcessor

SignalProcessorFactory = Callable[[dict[str, Any]], object]

_ROOT = Path(__file__).resolve().parent
CONFIG_FILENAME = "config.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
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
    a file path, module or factory. Overrides apply at the top level and — when
    the file nests settings under a single bot section (e.g. ``ram_jet:``) —
    inside that section, so per-bot tuning always wins over file defaults.
    """
    payload = _load_yaml(config_path)
    merged = dict(payload)
    overrides = dict(config or {})
    merged.update(overrides)
    sections = [key for key, value in payload.items() if isinstance(value, dict)]
    if len(sections) == 1:
        section = dict(payload[sections[0]])
        section.update(overrides)
        merged[sections[0]] = section
    return factory(merged)


def _bot_config_path(bot_dir: str) -> Path:
    """Config file for a bot directory; ids are code-owned, never user input."""
    return _ROOT / bot_dir / CONFIG_FILENAME


def _ramjet_config_path() -> Path:
    return _bot_config_path("ramjet")


def _rocketfuel_config_path() -> Path:
    return _bot_config_path("rocketfuel")


def _nitro_config_path() -> Path:
    return _bot_config_path("nitro")


def _build_ramjet(config: dict[str, Any]) -> RamJetSignalProcessor:
    return _build_from_config(_ramjet_config_path(), RamJetSignalProcessor, config)


def _build_nitro(config: dict[str, Any]) -> NitroSignalProcessor:
    return _build_from_config(_nitro_config_path(), NitroSignalProcessor, config)


def _build_rocketfuel(variant: str, config: dict[str, Any]) -> RocketFuelSignalProcessor:
    def _factory(merged: dict[str, Any]) -> RocketFuelSignalProcessor:
        if isinstance(merged.get("rocket_fuel"), dict):
            rocket_fuel = dict(merged["rocket_fuel"])
        else:
            rocket_fuel = {
                key: value
                for key, value in merged.items()
                if key in {"atr_period", "sl_multiplier", "candle_timeframe", "variant"}
            }
        rocket_fuel["variant"] = variant
        merged["rocket_fuel"] = rocket_fuel
        return RocketFuelSignalProcessor(merged)

    return _build_from_config(_rocketfuel_config_path(), _factory, config)


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


def get_signal_processor_config_path(name: str) -> Path:
    """Return the config file that backs a registered processor id.

    Derived from code, not from the requested string: an unknown id raises
    instead of producing a filesystem path.
    """
    normalized = str(name or "").strip().lower()
    if normalized == "ramjet":
        return _ramjet_config_path()
    if normalized == "nitro":
        return _nitro_config_path()
    if normalized in {"rocketfuel_longsonly", "rocketfuel_longandshort"}:
        return _rocketfuel_config_path()
    raise ValueError(f"Unknown signal processor: {name}")


def list_signal_processor_options() -> list[dict[str, str]]:
    return [dict(item) for item in SIGNAL_PROCESSOR_OPTIONS]


def get_signal_processor(name: str, config: dict[str, Any] | None = None) -> object:
    normalized = str(name or "").strip().lower()
    factory = SIGNAL_PROCESSORS.get(normalized)
    if factory is None:
        raise ValueError(f"Unknown signal processor: {name}")
    return factory(dict(config or {}))

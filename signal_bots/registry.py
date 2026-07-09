from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import yaml

from signal_bots.nitro import NitroSignalProcessor
from signal_bots.ramjet import RamJetSignalProcessor
from signal_bots.rocketfuel import RocketFuelSignalProcessor

SignalProcessorFactory = Callable[[dict[str, Any]], object]

_ROOT = Path(__file__).resolve().parent


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Signal processor config must decode to mapping: {path}")
    return payload


def _build_ramjet(config: dict[str, Any]) -> RamJetSignalProcessor:
    merged = dict(_load_yaml(_ROOT / "ramjet" / "config.yaml"))
    merged.update(dict(config or {}))
    return RamJetSignalProcessor(merged)


def _build_nitro(config: dict[str, Any]) -> NitroSignalProcessor:
    merged = dict(_load_yaml(_ROOT / "ramjet" / "config.yaml"))
    merged.update(dict(config or {}))
    return NitroSignalProcessor(merged)


def _build_rocketfuel_longsonly(config: dict[str, Any]) -> RocketFuelSignalProcessor:
    merged = dict(_load_yaml(_ROOT / "rocketfuel" / "config.yaml"))
    merged.update(dict(config or {}))
    if isinstance(merged.get("rocket_fuel"), dict):
        rocket_fuel = dict(merged["rocket_fuel"])
        rocket_fuel["variant"] = "longsonly"
        merged["rocket_fuel"] = rocket_fuel
    else:
        merged["variant"] = "longsonly"
    return RocketFuelSignalProcessor(merged)


def _build_rocketfuel_longandshort(config: dict[str, Any]) -> RocketFuelSignalProcessor:
    merged = dict(_load_yaml(_ROOT / "rocketfuel" / "config.yaml"))
    merged.update(dict(config or {}))
    if isinstance(merged.get("rocket_fuel"), dict):
        rocket_fuel = dict(merged["rocket_fuel"])
        rocket_fuel["variant"] = "longandshort"
        merged["rocket_fuel"] = rocket_fuel
    else:
        merged["variant"] = "longandshort"
    return RocketFuelSignalProcessor(merged)


SIGNAL_PROCESSOR_OPTIONS: list[dict[str, str]] = [
    {
        "id": "stockkit",
        "label": "StockKit",
        "description": "StockKit-compatible processor (SL/TP + trailing metadata).",
    },
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
    "stockkit": _build_ramjet,
    "ramjet": _build_ramjet,
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

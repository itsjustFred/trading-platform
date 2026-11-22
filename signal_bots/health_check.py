"""Signal bot health check utilities.

Checks:
- Bot processors import successfully
- Config files exist (explicit registry mapping)
- Registry entries are valid

Run with:
    python -m signal_bots.health_check
"""
from __future__ import annotations

import json
from typing import Any

import pandas as pd

from signal_bots.registry import (
    SIGNAL_PROCESSOR_OPTIONS,
    SIGNAL_PROCESSORS,
    get_signal_processor_config_path,
)

_BOT_CONFIG_FILE = "config.yaml"


def _probe_market_data(rows: int = 80) -> pd.DataFrame:
    timestamps = pd.date_range("2026-01-01T00:00:00Z", periods=rows, freq="5min", tz="UTC")
    closes = [100.0 + (index * 0.2) + ((index % 7) * 0.05) for index in range(rows)]
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [value - 0.1 for value in closes],
            "high": [value + 0.5 for value in closes],
            "low": [value - 0.5 for value in closes],
            "close": closes,
            "volume": [1000.0] * rows,
        }
    )


def _run_processor_probe(factory: Any) -> dict[str, Any]:
    try:
        processor = factory({})
        process_signal = getattr(processor, "process_signal", None)
        if not callable(process_signal):
            raise TypeError("processor does not expose callable process_signal")
        signal = process_signal(
            {
                "symbol": "AAPL",
                "action": "buy",
                "timeframe": "5m",
                "timestamp": "2026-01-01T12:00:00Z",
            },
            _probe_market_data(),
        )
        action = (
            signal.get("action")
            if isinstance(signal, dict)
            else getattr(signal, "action", None)
        )
        symbol = (
            signal.get("symbol")
            if isinstance(signal, dict)
            else getattr(signal, "symbol", None)
        )
        if str(action or "").strip().lower() != "buy":
            raise ValueError(f"probe returned unexpected action: {action!r}")
        if str(symbol or "").strip().upper() != "AAPL":
            raise ValueError(f"probe returned unexpected symbol: {symbol!r}")
        return {"status": "ok", "action": "buy", "symbol": "AAPL"}
    except Exception as exc:
        return {
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }


def check_signal_bots() -> dict[str, Any]:
    """Run signal bot health checks."""
    results: list[dict[str, Any]] = []

    for option in SIGNAL_PROCESSOR_OPTIONS:
        name = option["id"]
        result: dict[str, Any] = {
            "name": name,
            "description": option.get("description", ""),
            "status": "ok",
            "errors": [],
            "probe": {"status": "skipped"},
        }

        factory = SIGNAL_PROCESSORS.get(name)
        if factory is None:
            result["status"] = "error"
            result["errors"].append(f"Processor factory not found: {name}")
        else:
            probe = _run_processor_probe(factory)
            result["probe"] = probe
            if probe["status"] != "ok":
                result["status"] = "error"
                result["errors"].append(probe["error"])

        try:
            config_path = get_signal_processor_config_path(name)
        except ValueError as exc:
            result["status"] = "error"
            result["errors"].append(str(exc))
        else:
            if not config_path.exists():
                result["status"] = "error"
                result["errors"].append(f"Config file not found: {config_path}")

        results.append(result)

    return {
        "status": "ok" if all(r["status"] == "ok" for r in results) else "error",
        "results": results,
    }


def main() -> int:
    """Print JSON health-check result and return a process exit code."""
    result = check_signal_bots()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Signal bot health check utilities.

Checks:
- Bot processors import successfully
- Config files exist
- Registry entries are valid

Run with:
    python -m signal_bots.health_check
"""
from __future__ import annotations

import json
from typing import Any

from signal_bots.registry import (
    SIGNAL_PROCESSORS,
    SIGNAL_PROCESSOR_OPTIONS,
    get_signal_processor_config_path,
)


def check_signal_bots() -> dict[str, Any]:
    """Run signal bot health checks."""
    results = []

    for option in SIGNAL_PROCESSOR_OPTIONS:
        name = option["id"]
        result = {
            "name": name,
            "description": option.get("description", ""),
            "status": "ok",
            "errors": [],
        }

        if name not in SIGNAL_PROCESSORS:
            result["status"] = "error"
            result["errors"].append(f"Processor factory not found: {name}")

        # The registry owns per-bot config paths; unknown ids raise instead of
        # producing a path from the option string.
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
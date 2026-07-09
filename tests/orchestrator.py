from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(slots=True)
class LayerResult:
    layer: str
    exit_code: int
    duration: float
    passed: int
    failed: int
    skipped: int
    errors: int

    @property
    def status(self) -> str:
        return "PASS" if self.exit_code == 0 else "FAIL"


def _layer_pytest_args(layer: str) -> list[str]:
    if layer == "validation":
        return ["tests/integration/test_ai_code_quality.py"]
    if layer == "strategies":
        return ["tests/integration/test_strategy_pipeline.py"]
    if layer == "e2e":
        return ["tests/e2e/lab/test_conversion.py", "--browser", "firefox", "-v"]
    if layer == "unit":
        return ["tests", "-m", "not integration and not e2e and not ai_code"]
    raise ValueError(f"Unsupported layer '{layer}'")


def _run_layer(layer: str) -> LayerResult:
    with tempfile.TemporaryDirectory() as temp_dir:
        report_path = Path(temp_dir) / f"{layer}-report.json"
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            *_layer_pytest_args(layer),
            "--json-report",
            f"--json-report-file={report_path}",
            "-q",
        ]
        started = time.monotonic()
        completed = subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            check=False,
            text=True,
        )
        duration = time.monotonic() - started

        summary = {}
        if report_path.exists():
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            summary = payload.get("summary", {}) if isinstance(payload, dict) else {}

        return LayerResult(
            layer=layer,
            exit_code=completed.returncode,
            duration=duration,
            passed=int(summary.get("passed", 0) or 0),
            failed=int(summary.get("failed", 0) or 0),
            skipped=int(summary.get("skipped", 0) or 0),
            errors=int(summary.get("error", 0) or 0),
        )


def _print_summary(results: list[LayerResult]) -> None:
    header = (
        f"{'Layer':<12} {'Status':<6} {'Duration(s)':>11} {'Passed':>6} "
        f"{'Failed':>6} {'Skipped':>7} {'Errors':>6}"
    )
    print(header)
    print("-" * len(header))
    for result in results:
        print(
            f"{result.layer:<12} {result.status:<6} {result.duration:>11.2f} "
            f"{result.passed:>6} {result.failed:>6} {result.skipped:>7} {result.errors:>6}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run trading-platform test layers.")
    parser.add_argument(
        "--layer",
        choices=["validation", "strategies", "e2e", "unit", "all"],
        default="all",
    )
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()

    requested_layers = (
        ["validation", "strategies", "unit", "e2e"] if args.layer == "all" else [args.layer]
    )

    results: list[LayerResult] = []
    for layer in requested_layers:
        result = _run_layer(layer)
        results.append(result)
        if args.fail_fast and result.exit_code != 0:
            break

    _print_summary(results)
    return 0 if all(item.exit_code == 0 for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

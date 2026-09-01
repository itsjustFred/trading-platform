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

# Generous wall-clock guard per layer: a hung run must not block the
# orchestrator forever (manually invoked script, so this is a safety net).
LAYER_TIMEOUT_SECONDS = 3600

# M6 fix: layer names describe what actually executes. The old
# "validation"/"strategies" names ran neither tests/validation/** nor
# tests/strategies/** — the real strategy smoke block runs in the unit
# selection, so it must not be advertised under a dedicated name.
_LAYER_ALIASES: dict[str, str] = {
    "ai_code": "ai_code",
    "pipeline": "pipeline",
    "unit": "unit",
    "e2e": "e2e",
}

# Legacy names kept working for existing invocations/scripts.
_CANONICAL_LAYER: dict[str, str] = {
    **_LAYER_ALIASES,
    "validation": "ai_code",
    "strategies": "pipeline",
}

_LAYER_ARGS: dict[str, list[str]] = {
    "ai_code": ["tests/integration/test_ai_code_quality.py"],
    "pipeline": ["tests/integration/test_strategy_pipeline.py"],
    "e2e": ["tests/e2e/lab/test_conversion.py", "--browser", "firefox", "-v"],
    "unit": ["tests", "-m", "not integration and not e2e and not ai_code"],
}


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
        if self.exit_code != 0:
            return "FAIL"
        if self.errors:
            return "ERROR"
        return "PASS"


def _canonical_layer(layer: str) -> str:
    canonical = _CANONICAL_LAYER.get(layer)
    if canonical is None:
        raise ValueError(f"Unsupported layer '{layer}'")
    return canonical


def _layer_pytest_args(layer: str) -> list[str]:
    canonical = _canonical_layer(layer)
    return list(_LAYER_ARGS[canonical])


def _read_summary(report_path: Path) -> dict:
    if not report_path.exists():
        return {}
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    summary = payload.get("summary", {}) if isinstance(payload, dict) else {}
    return summary if isinstance(summary, dict) else {}


def _build_layer_result(
    layer: str, *, exit_code: int, duration: float, report_path: Path
) -> LayerResult:
    summary = _read_summary(report_path)
    total = int(summary.get("total", 0) or 0)
    passed = int(summary.get("passed", 0) or 0)
    failed = int(summary.get("failed", 0) or 0)
    skipped = int(summary.get("skipped", 0) or 0)
    errors = int(summary.get("error", 0) or 0) + int(summary.get("errors", 0) or 0)

    # M7 fix: a green exit code with an empty/missing report means the
    # reporter never wrote results (collection crash, plugin missing).
    # Surface that as an ERROR row instead of a zero-count PASS.
    if exit_code == 0 and total == 0:
        errors += 1

    return LayerResult(
        layer=layer,
        exit_code=exit_code,
        duration=duration,
        passed=passed,
        failed=failed,
        skipped=skipped,
        errors=errors,
    )


def _run_layer(layer: str) -> LayerResult:
    canonical = _canonical_layer(layer)
    with tempfile.TemporaryDirectory() as temp_dir:
        report_path = Path(temp_dir) / f"{canonical}-report.json"
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            *_layer_pytest_args(canonical),
            "--json-report",
            f"--json-report-file={report_path}",
            "-q",
        ]
        started = time.monotonic()
        try:
            completed = subprocess.run(
                cmd,
                cwd=PROJECT_ROOT,
                check=False,
                text=True,
                timeout=LAYER_TIMEOUT_SECONDS,
            )
            returncode = completed.returncode
        except subprocess.TimeoutExpired:
            returncode = 124
        duration = time.monotonic() - started

        return _build_layer_result(
            canonical, exit_code=returncode, duration=duration, report_path=report_path
        )


def _print_summary(results: list[LayerResult]) -> None:
    header = (
        f"{'Layer':<12} {'Status':<6} {'Duration(s)':>11} {'Passed':>6} "
        f"{'Failed':>6} {'Skipped':>7} {'Errors':>6}  Targets"
    )
    print(header)
    print("-" * len(header))
    for result in results:
        targets = " ".join(_layer_pytest_args(result.layer))
        print(
            f"{result.layer:<12} {result.status:<6} {result.duration:>11.2f} "
            f"{result.passed:>6} {result.failed:>6} {result.skipped:>7} {result.errors:>6}  "
            f"{targets}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run trading-platform test layers.")
    parser.add_argument(
        "--layer",
        choices=["ai_code", "pipeline", "unit", "e2e", "all", "validation", "strategies"],
        default="all",
        help=(
            "Layer to run: ai_code (AI-generated code quality), pipeline "
            "(strategy pipeline integration), unit, e2e, or all. The legacy "
            "names 'validation' and 'strategies' are accepted as aliases."
        ),
    )
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()

    requested_layers = (
        ["ai_code", "pipeline", "unit", "e2e"] if args.layer == "all" else [_canonical_layer(args.layer)]
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

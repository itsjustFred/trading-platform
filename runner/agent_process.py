"""Process control and logging helpers for the auto strategy agent.

Subprocess launching for validation backtests plus the tolerant task-log
wrapper used by every stage.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TaskLogger = Callable[..., None]


def build_backtest_command(
    *,
    strategy_path: Path,
    engine: str,
    target: Any,
    output_path: Path,
    capital: float,
    default_risk_pct: float,
) -> list[str]:
    relative_strategy_path = strategy_path.relative_to(PROJECT_ROOT)
    return [
        sys.executable,
        "-m",
        "runner.cli",
        "run",
        "--engine",
        engine,
        "--strategy",
        str(relative_strategy_path),
        "--symbols",
        *target.symbols,
        "--timeframe",
        target.timeframe,
        "--start",
        target.start,
        "--end",
        target.end,
        "--capital",
        str(capital),
        "--risk-pct",
        str(default_risk_pct),
        "--output",
        str(output_path),
    ]


def run_backtest(cmd: list[str], *, log: TaskLogger | None) -> tuple[int, str]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    log_message(log, "Launching backtest command: " + " ".join(cmd))

    output_lines: list[str] = []
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(PROJECT_ROOT),
        env=env,
        text=True,
        bufsize=1,
    )

    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                output_lines.append(line)
                log_message(log, line.rstrip("\n"), stream="stdout")
    finally:
        returncode = proc.wait()

    return returncode, "".join(output_lines)


def log_message(
    log: TaskLogger | None,
    message: str,
    *,
    stream: str = "task",
    level: str = "info",
) -> None:
    if log is None:
        return
    try:
        log(message, stream=stream, level=level)
    except TypeError:
        log(message)

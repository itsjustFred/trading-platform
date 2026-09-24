"""Process control and logging helpers for the auto strategy agent.

Subprocess launching for validation backtests plus the tolerant task-log
wrapper used by every stage.
"""

from __future__ import annotations

import os
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent

BACKTEST_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "PYTHONUNBUFFERED",
    "PYTHONPATH",
    "PYTHONHASHSEED",
    "VIRTUAL_ENV",
    "TMPDIR",
    "DATA_DIR",
    "LOG_DIR",
)
BACKTEST_ENV_ALWAYS_SET = {"PYTHONUNBUFFERED": "1"}

TaskLogger = Callable[..., None]


def build_backtest_child_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in set(BACKTEST_ENV_ALLOWLIST)
    }
    env.update(BACKTEST_ENV_ALWAYS_SET)
    return env


def build_backtest_command(
    *,
    strategy_path: Path,
    engine: str,
    target: Any,
    output_path: Path,
    capital: float,
    default_risk_pct: float,
    project_root: Path = PROJECT_ROOT,
) -> list[str]:
    """Build the runner.cli command for the validation backtest.

    ``project_root`` is an explicit parameter (defaulting to the module
    constant) so callers patch a single seam; the strategy path is made
    relative to exactly the root the subprocess will run in.
    """
    relative_strategy_path = strategy_path.relative_to(project_root)
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


def run_backtest(
    cmd: list[str],
    *,
    log: TaskLogger | None,
    project_root: Path = PROJECT_ROOT,
    timeout_seconds: float | None = None,
) -> tuple[int, str]:
    """Run the validation backtest, streaming output to the task log.

    ``timeout_seconds`` bounds the whole child process (optional; the caller
    decides the deadline). On expiry the child is killed and the call returns
    ``(124, collected_output)`` — the conventional timeout exit code — instead
    of hanging forever on a wedged LLM-generated strategy (CodeRabbit round
    on 16-D2).
    """
    env = build_backtest_child_env()
    log_message(log, "Launching backtest command: " + " ".join(cmd))

    output_lines: list[str] = []
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(project_root),
        env=env,
        text=True,
        bufsize=1,
    )

    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds

    def _remaining() -> float | None:
        return None if deadline is None else max(0.0, deadline - time.monotonic())

    timed_out = False
    try:
        stdout_fd = proc.stdout.fileno() if proc.stdout is not None else None
        buffered = b""
        while True:
            remaining = _remaining()
            if remaining is not None and remaining <= 0:
                raise subprocess.TimeoutExpired(cmd, timeout_seconds or 0)

            poll_timeout = min(0.5, remaining) if remaining is not None else 0.5
            ready, _, _ = (
                select.select([stdout_fd], [], [], poll_timeout)
                if stdout_fd is not None
                else ([], [], [])
            )
            if ready:
                chunk = os.read(stdout_fd, 65536)
                if not chunk:
                    if buffered:  # flush a trailing partial line without newline
                        line = buffered.decode("utf-8", errors="replace")
                        buffered = b""
                        output_lines.append(line + "\n")
                        log_message(log, line, stream="stdout")
                    break
                buffered += chunk
                while b"\n" in buffered:
                    raw_line, buffered = buffered.split(b"\n", 1)
                    line = raw_line.decode("utf-8", errors="replace")
                    output_lines.append(line + "\n")
                    log_message(log, line, stream="stdout")
            # Child still alive: keep polling until stdout EOF or deadline.

        remaining = _remaining()
        returncode = proc.wait() if remaining is None else proc.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        try:
            returncode = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - pathological child
            returncode = -9
    finally:
        if proc.stdout is not None:
            proc.stdout.close()

    if timed_out:
        log_message(
            log,
            f"Backtest timed out after {timeout_seconds}s and was terminated.",
            level="error",
        )
        return 124, "".join(output_lines)

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

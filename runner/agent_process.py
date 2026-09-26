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
    "TEMP",
    "TMP",
    "DATA_DIR",
    "LOG_DIR",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "HOMEDRIVE",
    "HOMEPATH",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "NUMBER_OF_PROCESSORS",
)
BACKTEST_ENV_ALWAYS_SET = {"PYTHONUNBUFFERED": "1"}
_BACKTEST_ENV_ALLOWLIST_CASEFOLD = frozenset(
    key.upper() for key in BACKTEST_ENV_ALLOWLIST
)

TaskLogger = Callable[..., None]


def build_backtest_child_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _BACKTEST_ENV_ALLOWLIST_CASEFOLD
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

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(project_root),
        env=env,
        text=True,
        bufsize=1,
    )
    timed_out = False
    partial_output = ""
    try:
        try:
            proc_output, _ = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            partial_output = exc.output or ""
            if isinstance(partial_output, bytes):
                partial_output = partial_output.decode("utf-8", errors="replace")
            proc.kill()
            try:
                proc_output, _ = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc_output = ""
            if isinstance(proc_output, bytes):
                proc_output = proc_output.decode("utf-8", errors="replace")
    finally:
        stdout = getattr(proc, "stdout", None)
        close_stdout = getattr(stdout, "close", None)
        if callable(close_stdout):
            close_stdout()

    output = proc_output or partial_output
    for line in output.splitlines(keepends=True):
        log_message(log, line.rstrip("\r\n"), stream="stdout")
    if timed_out:
        log_message(
            log,
            f"Backtest timed out after {timeout_seconds}s and was terminated.",
            level="error",
        )
        return 124, output

    return proc.returncode, output


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

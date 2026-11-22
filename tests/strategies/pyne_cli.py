"""Shared harness for driving a strategy through the ``pyne`` CLI.

Both ORB test modules shell out to the real strategy file on synthetic CSVs (no
market data, only the ``pyne`` binary). ``pyne`` rewrites the sibling ``.toml``
of the strategy it runs as a side effect, so the strategy - and its ``.toml``
when one exists - is copied into the workdir first and the run happens there:
never in place, never against the committed ``.toml``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def _resolve_pyne_bin() -> str | None:
    """The ``pyne`` CLI, from PATH or this interpreter's venv bin, else ``None``.

    The same two locations `runner.engines.pynecore_adapter` searches and the
    engine-contract suite gates on. The venv-bin fallback matters: a `pyne`
    installed into the venv only is invisible to a bare `shutil.which` when the
    venv bin is not on PATH, and these modules would then skip - silently, since
    their module-level `skipif` bypasses the conftest fixture that records and
    counts skips.
    """
    on_path = shutil.which("pyne")
    if on_path:
        return on_path
    venv_bin = Path(sys.executable).parent / "pyne"
    return str(venv_bin) if venv_bin.exists() else None


# The binary is resolved once here; each module's ``pytestmark`` skips when it
# is absent (CI installs it via the ``pynecore`` extra).
pyne_bin = _resolve_pyne_bin()

# A run compiles the strategy and replays the bars; the guard and the smoke
# suite share one budget so neither can hang the pipeline.
TIMEOUT_SECONDS = 180

# The CLI's two outputs. `TRADES_CSV` is what the tests read; `STATS_CSV` is
# produced next to it (the CLI is handed both paths) and nothing reads it yet.
TRADES_CSV = "trades.csv"
STATS_CSV = "stats.csv"


def run_pyne(strategy: Path, bars: Path, workdir: Path) -> subprocess.CompletedProcess:
    """Copy ``strategy`` into ``workdir`` and run ``pyne`` on ``bars`` there.

    Returns the completed process; the caller asserts on ``returncode`` and
    reads the trades CSV out of ``workdir``.
    """
    local_strategy = workdir / strategy.name
    local_strategy.write_text(strategy.read_text())
    sibling_toml = strategy.with_suffix(".toml")
    if sibling_toml.exists():
        (workdir / sibling_toml.name).write_text(sibling_toml.read_text())
    return subprocess.run(
        [
            pyne_bin,
            "--workdir",
            str(workdir),
            "run",
            str(local_strategy),
            str(bars),
            "--trade",
            str(workdir / TRADES_CSV),
            "--strat",
            str(workdir / STATS_CSV),
        ],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        cwd=str(workdir),
    )

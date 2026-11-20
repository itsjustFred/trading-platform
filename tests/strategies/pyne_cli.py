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
from pathlib import Path

# The binary is resolved once here; each module's ``pytestmark`` skips when it
# is absent (CI installs it via the ``pynecore`` extra).
pyne_bin = shutil.which("pyne")

# A run compiles the strategy and replays the bars; the guard and the smoke
# suite share one budget so neither can hang the pipeline.
TIMEOUT_SECONDS = 180

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

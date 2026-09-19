"""Interval guard for the PyneCore opening-range breakout.

Coarse bars cannot express the 30-minute opening range (a 1h bar spans past
the window end; daily bars have no session clock), so the script must fail
loudly instead of backtesting silently wrong sessions. These tests run the
real strategy file through the ``pyne`` CLI on synthetic CSVs — no market
data needed, only the ``pyne`` binary.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
STRATEGY_PATH = PROJECT_ROOT / "strategies" / "pynecore_orb" / "pynecore_orb.py"

pyne_bin = shutil.which("pyne")

pytestmark = pytest.mark.skipif(
    pyne_bin is None, reason="pyne CLI not on PATH"
)


def _bars(path: Path, freq: str) -> None:
    idx = pd.date_range("2024-01-02 14:00", periods=8, freq=freq, tz="UTC")
    pd.DataFrame(
        {
            "timestamp": idx,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 10,
        }
    ).to_csv(path, index=False)


def _run(strategy: Path, bars: Path, workdir: Path) -> subprocess.CompletedProcess:
    # Copy the strategy: pyne rewrites the sibling .toml with its own
    # defaults as a side effect — never run it in place.
    local_strategy = workdir / strategy.name
    local_strategy.write_text(strategy.read_text())
    return subprocess.run(
        [
            pyne_bin,
            "--workdir",
            str(workdir),
            "run",
            str(local_strategy),
            str(bars),
            "--trade",
            str(workdir / "trades.csv"),
            "--strat",
            str(workdir / "stats.csv"),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(workdir),
    )


def test_30m_bars_run_clean(tmp_path: Path):
    """30m is the coarsest supported interval: one bar covers the OR window."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    bars = tmp_path / "bars_30m.csv"
    _bars(bars, "30min")
    result = _run(STRATEGY_PATH, bars, workdir)
    assert result.returncode == 0, result.stderr[-2000:]


def test_1h_bars_fail_loud(tmp_path: Path):
    """1h bars span past the 09:30-10:00 window end: loud failure, no run."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    bars = tmp_path / "bars_1h.csv"
    _bars(bars, "h")
    result = _run(STRATEGY_PATH, bars, workdir)
    assert result.returncode != 0
    assert "30m-or-finer" in (result.stderr or "") + (result.stdout or "")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

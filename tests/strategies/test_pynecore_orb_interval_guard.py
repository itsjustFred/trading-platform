"""Interval guard for the PyneCore opening-range breakout.

Coarse bars cannot express the 30-minute opening range (a 1h bar spans past
the window end; daily bars have no session clock), so the script must fail
loudly instead of backtesting silently wrong sessions. These tests run the
real strategy file through the ``pyne`` CLI on synthetic CSVs — no market
data needed, only the ``pyne`` binary.
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

try:  # pytest collects this as part of the `tests` package
    from tests.strategies.pyne_cli import pyne_bin, run_pyne
except ModuleNotFoundError:  # direct execution: python <this file>
    # The launcher at the bottom runs this file directly, and a script's own
    # directory - not the repository root - is what lands on `sys.path`, so the
    # absolute `tests.` import cannot resolve. Put the root back before
    # importing, rather than dropping the launcher or making the import
    # relative (it is collected as a package module under pytest).
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tests.strategies.pyne_cli import pyne_bin, run_pyne

PROJECT_ROOT = Path(__file__).parent.parent.parent
STRATEGY_PATH = PROJECT_ROOT / "strategies" / "pynecore_orb" / "pynecore_orb.py"

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


def test_30m_bars_run_clean(tmp_path: Path):
    """30m is the coarsest supported interval: one bar covers the OR window."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    bars = tmp_path / "bars_30m.csv"
    _bars(bars, "30min")
    result = run_pyne(STRATEGY_PATH, bars, workdir)
    assert result.returncode == 0, result.stderr[-2000:]


def test_1h_bars_fail_loud(tmp_path: Path):
    """1h bars span past the 09:30-10:00 window end: loud failure, no run."""
    workdir = tmp_path / "wd"
    workdir.mkdir()
    bars = tmp_path / "bars_1h.csv"
    _bars(bars, "h")
    result = run_pyne(STRATEGY_PATH, bars, workdir)
    assert result.returncode != 0
    assert "30m-or-finer" in (result.stderr or "") + (result.stdout or "")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

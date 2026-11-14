"""The ORB forced exit must be flat AT 15:45 ET under next-open fills.

PyneCore fills ``strategy.close()`` at the NEXT bar's open, so the script raises
the forced-exit order on the first bar whose END reaches 15:45 ET. Before 2.1.0
it fired on the first bar >= 15:45 and therefore filled one bar late (on RTH-only
data: the next session's open, i.e. an overnight hold). These tests run the real
script through the ``pyne`` CLI on synthetic bars - no market data needed.
"""

import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
STRATEGY_PATH = PROJECT_ROOT / "strategies" / "pynecore_orb" / "pynecore_orb.py"

pyne_bin = shutil.which("pyne")

pytestmark = pytest.mark.skipif(pyne_bin is None, reason="pyne CLI not on PATH")


def _bars(path: Path, freq: str, *, drop_before_exit: bool = False) -> None:
    """One ET day (Jan = EST): OR 09:30-10:00, breakout at 10:00, drift upward."""
    idx = pd.date_range("2024-01-02 14:30", "2024-01-02 22:00", freq=freq, tz="UTC", inclusive="left")
    step = pd.Timedelta(freq)
    or_bars = int(pd.Timedelta(minutes=30) / step)
    close = [100.0] * or_bars + [105.0 + 0.01 * i for i in range(len(idx) - or_bars)]
    frame = pd.DataFrame(
        {
            "timestamp": idx,
            "open": close,
            "high": [c + 1 for c in close],
            "low": [c - 1 for c in close],
            "close": close,
            "volume": 10,
        }
    )
    frame.loc[: or_bars - 1, ["high", "low"]] = [101.0, 99.0]
    if drop_before_exit:
        # no bar between 15:30 and 16:00 ET (20:30 .. 21:00 UTC)
        frame = frame[~((frame["timestamp"] >= "2024-01-02 20:30+00:00") & (frame["timestamp"] < "2024-01-02 21:00+00:00"))]
    frame.to_csv(path, index=False)


def _run_and_read_exit(tmp_path: Path, freq: str, **kwargs) -> pd.Timestamp:
    workdir = tmp_path / "wd"
    workdir.mkdir()
    bars = tmp_path / f"bars_{freq}.csv"
    _bars(bars, freq, **kwargs)
    local = workdir / STRATEGY_PATH.name
    local.write_text(STRATEGY_PATH.read_text())  # pyne rewrites the sibling .toml
    result = subprocess.run(
        [pyne_bin, "--workdir", str(workdir), "run", str(local), str(bars),
         "--trade", str(workdir / "trades.csv"), "--strat", str(workdir / "stats.csv")],
        capture_output=True, text=True, timeout=180, cwd=str(workdir),
    )
    assert result.returncode == 0, result.stderr[-2000:]
    trades = pd.read_csv(workdir / "trades.csv")
    exits = trades[trades["Type"].str.startswith("Exit")]
    assert len(exits) == 1
    assert exits.iloc[0]["Signal"] == "time_exit"
    return pd.Timestamp(exits.iloc[0]["Date/Time"]).tz_convert("America/New_York")


@pytest.mark.parametrize(
    ("freq", "expected_hhmm"),
    [
        ("5min", "15:45"),   # the bar before 15:45 raises it -> fills at the 15:45 open
        ("15min", "15:45"),  # 15:30 bar ends at 15:45
        ("30min", "16:00"),  # first open at/after 15:45 (the 15:30 bar ends 16:00)
    ],
)
def test_forced_exit_fills_at_the_first_open_at_or_after_1545(tmp_path, freq, expected_hhmm):
    exit_time = _run_and_read_exit(tmp_path, freq)
    assert exit_time.strftime("%H:%M") == expected_hhmm


def test_missing_bar_before_1545_does_not_hold_the_position_longer(tmp_path):
    # 5m feed without any bar 15:30-16:00: the first bar present at/after the
    # exit is the 16:00 bar, which raises the order; it must still be a same-day
    # exit (next bar's open), never skipped.
    exit_time = _run_and_read_exit(tmp_path, "5min", drop_before_exit=True)
    assert exit_time.strftime("%H:%M") == "16:05"

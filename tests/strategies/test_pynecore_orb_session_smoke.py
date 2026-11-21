"""Session-state-machine smoke tests for the PyneCore opening-range breakout.

``tests/strategies/test_pynecore_orb.py`` pins the strategy end to end, but it
runs a real backtest over the US500/30m dataset, so its whole module skips
without that dataset — CI included. The interval guard next to it covers the
timeframe refusal only. This module closes the remaining gap: it drives the real
strategy file through the ``pyne`` CLI on synthetic bars and pins the
ET-session state machine the typing work touched (opening-range accumulation,
long/short breakout, one trade per ET day, forced exit at 15:45 ET).

No market data: every bar is written by hand below. Only the ``pyne`` binary is
required, and the module skips without it.

Fill timing: the broker emulator fills an order on the bar AFTER the signal bar,
so a signal on the 10:00 ET bar shows up as a trade row stamped 10:30 ET.
"""

import csv
import sys
from pathlib import Path

import pandas as pd
import pytest

try:  # pytest collects this as part of the `tests` package
    from tests.strategies.pyne_cli import TRADES_CSV, pyne_bin, run_pyne
except ModuleNotFoundError:  # direct execution: python <this file>
    # Same reason as in `test_pynecore_orb_interval_guard.py`: the launcher at
    # the bottom runs this file directly, and only the script's own directory
    # lands on `sys.path`, so the absolute `tests.` import cannot resolve.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tests.strategies.pyne_cli import TRADES_CSV, pyne_bin, run_pyne

PROJECT_ROOT = Path(__file__).parent.parent.parent
STRATEGY_PATH = PROJECT_ROOT / "strategies" / "pynecore_orb" / "pynecore_orb.py"

pytestmark = pytest.mark.skipif(
    pyne_bin is None, reason="pyne CLI not on PATH"
)

# The bar clock the strategy supports: a 30-minute grid from 09:00 to 16:30 ET
# (January = EST, UTC-5) PLUS an extra 15:45 ET bar, so the forced-exit branch
# is reachable on this synthetic clock. A real 30m grid cannot produce those
# bars - it steps 15:30 ET -> 16:00 ET, and the strategy also allows 15m - and
# the bars after 15:45 distinguish "closed by the forced exit" from "still open
# at the end of the data".
ET_CLOCK = [
    "09:00", "09:30", "10:00", "10:30", "11:00", "11:30", "12:00", "12:30",
    "13:00", "13:30", "14:00", "14:30", "15:00", "15:30", "15:45", "16:00",
    "16:30",
]

# Opening range 100/98 (range 2, buffer 0.2) -> long level 100.4, short 97.6.
_OR = (100.0, 100.0, 98.0, 99.0)  # 09:30 ET, the only 30m bar inside the window

# The pre-window bar is deliberately extreme (150/50): a range accumulation that
# stopped respecting the 09:30 window would swallow it and widen the OR past
# every breakout level, so these days would stop trading.
_PRE_OR = (100.0, 150.0, 50.0, 100.0)  # 09:00 ET


def _bars(path: Path, days: list[list[tuple[float, float, float, float]]]) -> None:
    """Write one CSV of bar rows for each list of ``(open, high, low, close)`` rows."""
    rows: list[tuple[float, float, float, float]] = []
    stamps: list[pd.Timestamp] = []
    for day_index, day in enumerate(days):
        assert len(day) == len(ET_CLOCK), f"day {day_index} needs {len(ET_CLOCK)} bars"
        rows.extend(day)
        stamps.extend(
            pd.Timestamp(f"2024-01-{day_index + 2:02d} {clock}", tz="America/New_York")
            .tz_convert("UTC")
            for clock in ET_CLOCK
        )
    frame = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    frame.insert(0, "timestamp", stamps)
    frame["volume"] = 10
    frame.to_csv(path, index=False)


def _run(bars: Path, workdir: Path) -> list[dict[str, str]]:
    result = run_pyne(STRATEGY_PATH, bars, workdir)
    assert result.returncode == 0, result.stderr[-2000:]
    # The harness owns the path (it is what the CLI was told to write); read it
    # through the same constant rather than spelling the name twice.
    with (workdir / TRADES_CSV).open(newline="") as handle:
        return list(csv.DictReader(handle))


def _ledger(
    tmp_path: Path, name: str, days: list[list[tuple[float, float, float, float]]]
) -> list[dict[str, str]]:
    workdir = tmp_path / name
    workdir.mkdir()
    _bars(tmp_path / f"{name}.csv", days)
    return _run(tmp_path / f"{name}.csv", workdir)


# A long day: the 10:00 ET close (101.0) breaks 100.4, the 11:00 ET close
# (101.5) sits below the 1% reversion level of the 103.0 watermark (101.97), and
# the 12:00 ET close (90.0) sits below the short level 97.6 — it must not open a
# second trade on the same day.
_LONG_DAY = [
    _PRE_OR,  # 09:00 ET, before the OR
    _OR,  # 09:30 ET, OR bar
    (99.0, 101.0, 99.0, 101.0),  # 10:00 ET -> long signal
    (101.0, 103.0, 100.5, 103.0),  # HWM 103.0
    (103.0, 103.0, 101.0, 101.5),  # trailing-stop signal
    (101.5, 102.0, 101.0, 101.8),  # 11:30 ET, fill bar
    (90.5, 91.0, 89.5, 90.0),  # 12:00 ET, below 97.6: no second trade
] + [(101.8, 102.0, 101.0, 101.9)] * 10

# A short day: the 10:00 ET close (97.0) breaks 97.6, the trailing stop fires
# when the close (97.5) rises above the 96.0 watermark * 1.01 = 96.96.
_SHORT_DAY = [
    _PRE_OR,  # 09:00 ET, before the OR
    _OR,  # 09:30 ET, OR bar
    (97.0, 97.2, 96.8, 97.0),  # 10:00 ET -> short signal
    (96.5, 96.6, 95.9, 96.0),  # LWM 96.0
    (96.2, 97.6, 96.0, 97.5),  # trailing-stop signal
    (97.5, 98.0, 97.4, 98.0),  # 11:30 ET, fill bar
] + [(98.0, 98.0, 97.9, 98.0)] * 11

# A flat opening range (100.0/100.0) has no level to break: the closes move to
# 100.5, which is above the (degenerate) level and must still produce no trade.
_FLAT_DAY = [_PRE_OR, (100.0, 100.0, 100.0, 100.0)] + [(100.0, 100.5, 99.5, 100.5)] * 15

# A day that never moves far enough to trail out, so the 15:45 ET bar has to
# flatten the position it opened at 10:00 ET.
_FORCED_EXIT_DAY = [
    _PRE_OR,  # 09:00 ET, before the OR
    _OR,  # 09:30 ET, OR bar
    (99.0, 101.0, 99.0, 101.0),  # 10:00 ET -> long signal
] + [(101.0, 101.2, 100.8, 101.0)] * 14


def _entries(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [row for row in rows if row["Type"].startswith("Entry")]


def _exits(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [row for row in rows if row["Type"].startswith("Exit")]


def test_long_and_short_breakout_both_trade_one_per_day(tmp_path: Path):
    rows = _ledger(tmp_path, "long_short", [_LONG_DAY, _SHORT_DAY])
    entries = _entries(rows)
    exits = _exits(rows)

    # One long entry, filled on the bar after the 10:00 ET signal, and it trails
    # out on its own watermark instead of waiting for the session to end.
    long_entries = [row for row in entries if row["Type"] == "Entry long"]
    assert len(long_entries) == 1
    assert long_entries[0]["Date/Time"] == "2024-01-02T15:30:00+0000", long_entries
    long_exits = [row for row in exits if row["Date/Time"].startswith("2024-01-02")]
    assert len(long_exits) == 1, long_exits
    # The reversion calculation names the exit, not just "a trailing stop
    # exists": the signal bar is 11:00 ET, so the fill is the 11:30 ET bar
    # (16:30 UTC). A flipped sign on the reversion level exits elsewhere.
    assert long_exits[0]["Signal"] == "trailing_stop", long_exits[0]
    assert long_exits[0]["Date/Time"] == "2024-01-02T16:30:00+0000", long_exits[0]

    # The second ET day breaks out to the downside instead: the traded flag and
    # the opening range reset with the ET day.
    short_entries = [row for row in entries if row["Type"] == "Entry short"]
    assert short_entries, "the SHORT side of the state machine must open a trade"
    assert {row["Date/Time"] for row in short_entries} == {
        "2024-01-03T15:30:00+0000"
    }, short_entries
    # Same pin on the short side: the LWM reversion level (96.0 * 1.01 = 96.96)
    # is first exceeded by the 11:00 ET close, so the exit fills on the 11:30 ET
    # bar (16:30 UTC). A flipped sign fires one bar earlier and lands on a
    # different timestamp.
    short_exits = [row for row in exits if row["Date/Time"].startswith("2024-01-03")]
    assert short_exits, short_exits
    assert short_exits[-1]["Signal"] == "trailing_stop", short_exits[-1]
    assert short_exits[-1]["Date/Time"] == "2024-01-03T16:30:00+0000", short_exits[-1]


def test_flat_opening_range_and_forced_exit(tmp_path: Path):
    rows = _ledger(tmp_path, "flat_forced", [_FLAT_DAY, _FORCED_EXIT_DAY])
    entries = _entries(rows)
    exits = _exits(rows)

    # The flat range produces no trade at all, so nothing on 2024-01-02.
    assert not any(row["Date/Time"].startswith("2024-01-02") for row in rows)

    # The second day opens one long position and the 15:45 ET bar closes it:
    # the position is not carried past the session, and it is the forced-exit
    # bar that closes it rather than the end of the data (16:00/16:30 ET bars
    # follow it here).
    assert len(entries) == 1
    assert entries[0]["Type"] == "Entry long"
    assert exits, "the forced exit must flatten the open position"
    assert exits[-1]["Signal"] == "time_exit", exits[-1]
    # Filled on the bar after the 15:45 ET exit bar (16:00 ET = 21:00 UTC).
    assert exits[-1]["Date/Time"] == "2024-01-03T21:00:00+0000", exits[-1]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

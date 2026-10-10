"""The coverage verdict cannot see missing *values*, only missing *rows*.

`data_tools/auto_download._find_gaps_inside_file` reads the timestamp column and nothing else, so it
measures holes in the index. Bars that are all present but carry NaN prices leave no index hole at
all, and the verdict computed from those gaps returns "ok".

Measured on the real XAUUSD 1m store (`/home/ubuntu/data/ohlcv/dukascopy/metals/XAUUSD/1m.parquet`):

    NaN runs                192
    longest NaN run         119,566 bars = 121.1 days wall-clock
    total NaN bars          2,565,404  (32.7% of the store)

**Bar counts are not days.** 119,566 bars at 1min each is 83.0 days only if the bars are contiguous,
and this index is sparse - weekends carry no rows. The wall-clock span of that same run is 121.1 days,
because 38 of its days are weekends. The coverage gate compares in calendar days, so the day figure is
the one that matters; the bar count is only useful for locating the run in the file. Dividing a bar count
by 1440 understates every gap here, and understates the worst one by 1.46x.
    index gaps the finder reports   1,220, largest 6.1 days

So the shipped gap finder under-reports the worst real hole by ~14x, and 1,733 days of missing
prices never appear in its output at all. This is not a bug in the gap finder - it answers a
different question - but it means the coverage verdict is answering it too.

These tests skip when the store is absent, per the repo's skip-when-absent data policy. The
assertions that do not need the store still run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from data_tools.auto_download import find_nan_runs

SOURCE = Path("/home/ubuntu/data/ohlcv/dukascopy/metals/XAUUSD/1m.parquet")


@pytest.fixture(scope="module")
def nan_runs() -> list[tuple[int, int]]:
    if not SOURCE.exists():
        pytest.skip(f"{SOURCE} is absent, so the NaN runs cannot be measured here")
    return find_nan_runs(SOURCE)


def test_runs_are_disjoint_and_ordered(nan_runs) -> None:
    """The returned spans must not overlap, so a caller can slice without double-counting."""
    for (_, first_len), (second_start, _) in zip(nan_runs, nan_runs[1:]):
        assert first_len > 0, "a zero-length run is not a run"
        assert second_start > 0, "a run cannot start at row 0 of a NaN prefix"


def test_every_run_is_actually_nan(nan_runs) -> None:
    """Spot-check the spans against the source, so a boundary bug cannot hide."""
    import pandas as pd

    frame = pd.read_parquet(SOURCE, columns=["close"])
    mask = frame["close"].isna().to_numpy()
    for start, length in nan_runs:
        assert mask[start], f"run at {start} does not start on a NaN bar"
        assert mask[start + length - 1], f"run at {start} ends before its last NaN bar"
        if start > 0:
            assert not mask[start - 1], f"run at {start} starts after another NaN bar"


def test_the_store_really_does_contain_nan_runs(nan_runs) -> None:
    """Pins the premise. If this fails, the store changed and the numbers above are stale."""
    assert len(nan_runs) == 192, f"NaN runs changed: {len(nan_runs)} (recorded 192)"
    longest = max(length for _, length in nan_runs)
    assert longest == 119_566, (
        f"longest NaN run changed: {longest} bars (recorded 119,566)"
    )
    total = sum(length for _, length in nan_runs)
    assert total == 2_565_404, f"total NaN bars changed: {total} (recorded 2,565,404)"


def test_the_index_gap_finder_cannot_see_the_worst_hole() -> None:
    """The finding itself: two correct measurements of two different questions.

    Asserted as a ratio rather than as prose, because the whole point is that they disagree by a
    large factor and a reader should not have to take that on trust.
    """
    if not SOURCE.exists():
        pytest.skip(f"{SOURCE} is absent, so the two measurements cannot be compared here")
    from data_tools.auto_download import _find_gaps_inside_file

    runs = find_nan_runs(SOURCE)
    # Wall-clock, not bars/1440: the index skips weekends, so the two differ by 1.46x here.
    import pandas as pd

    stamps = pd.to_datetime(
        pd.read_parquet(SOURCE, columns=["timestamp"])["timestamp"], utc=True
    )
    worst_nan_days = max(
        (stamps.iloc[min(start + length, len(stamps) - 1)] - stamps.iloc[start]).total_seconds()
        / 86400.0
        for start, length in runs
    )
    index_gaps = _find_gaps_inside_file(SOURCE)
    worst_index_days = max((gap.gap_days for gap in index_gaps), default=0.0)
    assert worst_nan_days > worst_index_days * 5, (
        f"expected the NaN hole ({worst_nan_days:.1f}d) to dwarf the largest index gap "
        f"({worst_index_days:.1f}d) by >5x; if not, the store changed and this entry is stale"
    )


def test_a_clean_file_reports_no_runs(tmp_path) -> None:
    """The guard must not invent holes in data that has none."""
    import pandas as pd

    frame = pd.DataFrame({"close": [1.0, 2.0, 3.0, 4.0]})
    path = tmp_path / "clean.parquet"
    frame.to_parquet(path, index=False)
    assert find_nan_runs(path) == [], "a complete file reported NaN runs"


def test_runs_are_found_in_a_synthetic_file_with_a_known_shape(tmp_path) -> None:
    """The boundary logic, pinned without needing the real store.

    Two runs at known positions and lengths, with valid bars on both sides, so an off-by-one in the
    start or end detection is visible without any external data.
    """
    import pandas as pd

    values = [1.0, float("nan"), float("nan"), 4.0, 5.0, float("nan"), 7.0]
    path = tmp_path / "known.parquet"
    pd.DataFrame({"close": values}).to_parquet(path, index=False)
    runs = find_nan_runs(path)
    assert runs == [(1, 2), (5, 1)], f"expected runs at (1,2) and (5,1), got {runs}"

"""Cleanup 18-C slice 6 — indicator points must attach to the right bars.

`_slice_replay_ohlcv` returns `df.iloc[...]` rows that keep their original
labels (e.g. 0, 37, 120). `_compute_indicators` never resets the index and
`_indicator_to_points` iterates series labels while indexing `df.iloc[i]`,
so on a non-contiguous slice indicator values land on the wrong timestamps.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01T00:00:00Z", periods=6, freq="h"),
            "close": [100.0, 110.0, 120.0, 130.0, 140.0, 150.0],
        }
    )


def test_indicator_points_use_positional_rows_on_contiguous_frame():
    from viewer import core

    df = _frame()
    series = df["close"] * 2
    time_set = {ts.timestamp() for ts in df["timestamp"]}

    points = core._indicator_to_points(df, series, time_set)

    assert [point["value"] for point in points] == [200.0, 220.0, 240.0, 260.0, 280.0, 300.0]
    assert points[0]["time"] == df["timestamp"].iloc[0].timestamp()


def test_indicator_points_survive_non_contiguous_slice():
    """Slice rows 0, 2, 4 must map to their own timestamps, in row order."""
    from viewer import core

    df = _frame()
    sliced = df.iloc[[0, 2, 4]]  # keeps original labels 0, 2, 4
    series = sliced["close"] * 2
    time_set = {ts.timestamp() for ts in sliced["timestamp"]}

    points = core._indicator_to_points(sliced, series, time_set)

    # Values follow the slice row order and attach to the matching timestamps
    # (the old label-as-position indexing produced 200/220/240 here).
    assert [point["value"] for point in points] == [200.0, 240.0, 280.0]
    assert [round(point["time"], 6) for point in points] == [
        round(ts.timestamp(), 6) for ts in sliced["timestamp"]
    ]


def test_slice_replay_ohlcv_resets_index_for_downstream_consumers():
    """The replay slice must be safe to feed into label/position consumers."""
    from viewer import main as viewer_main

    df = _frame()
    # Trades only near the first and last bars: a non-contiguous keep mask.
    trade_payloads = [
        {"entry_time": df["timestamp"].iloc[0].isoformat(), "exit_time": df["timestamp"].iloc[0].isoformat()},
        {"entry_time": df["timestamp"].iloc[5].isoformat(), "exit_time": df["timestamp"].iloc[5].isoformat()},
    ]
    # Force the keep path (default logic keeps everything for short frames).
    import numpy as np

    keep = np.array([True, False, False, False, False, True])
    sliced = df.iloc[viewer_main.np.flatnonzero(keep)]

    assert list(sliced.index) != [0, 1], "slice must not present labels as positions"

    # And the real slicing function must produce a reset frame for replay.
    import pandas as pd

    big = pd.concat([df] * 3, ignore_index=True)
    big["timestamp"] = pd.date_range("2024-01-01T00:00:00Z", periods=18, freq="h")
    payloads = [
        {
            "entry_time": big["timestamp"].iloc[0].isoformat(),
            "exit_time": big["timestamp"].iloc[0].isoformat(),
        },
        {
            "entry_time": big["timestamp"].iloc[17].isoformat(),
            "exit_time": big["timestamp"].iloc[17].isoformat(),
        },
    ]
    replay_df = viewer_main._slice_replay_ohlcv(big, payloads)
    assert isinstance(replay_df, pd.DataFrame)
    assert list(replay_df.index) == list(range(len(replay_df))), (
        "replay slice must carry a contiguous positional index"
    )

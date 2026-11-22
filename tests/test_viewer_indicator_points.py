"""Cleanup 18-C slice 6 — indicator points must attach to the right bars.

`_slice_replay_ohlcv` returns `df.iloc[...]` rows that keep their original
labels (e.g. 0, 37, 120). `_compute_indicators` never resets the index and
`_indicator_to_points` iterates series labels while indexing `df.iloc[i]`,
so on a non-contiguous slice indicator values land on the wrong timestamps.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
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
    # Force the keep path (default logic keeps everything for short frames).
    keep = np.array([True, False, False, False, False, True])
    sliced = df.iloc[viewer_main.np.flatnonzero(keep)]

    assert list(sliced.index) != [0, 1], "slice must not present labels as positions"

    # And the real slicing function must produce a reset frame for replay.
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


def _ohlcv_frame(rows: int = 60) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01T00:00:00Z", periods=rows, freq="h")
    close = pd.Series(
        100.0 + 5.0 * np.sin(np.linspace(0.0, 4.0 * np.pi, rows)),
        index=idx,
    )
    return pd.DataFrame(
        {
            "timestamp": idx,
            "open": close - 0.2,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(rows, 10_000),
        }
    )


def _time_to_index(df: pd.DataFrame) -> dict[float, int]:
    return {round(ts.timestamp(), 6): i for i, ts in enumerate(df["timestamp"])}


def test_compute_indicators_covers_shared_kinds():
    from viewer import core

    df = _ohlcv_frame()
    times = [ts.timestamp() for ts in df["timestamp"]]
    payloads = core._compute_indicators(
        df,
        [
            {"kind": "SMA", "period": 3},
            {"kind": "EMA", "period": 3},
            {"kind": "RSI", "period": 3},
            {"kind": "ATR", "period": 3},
            {"kind": "BB", "period": 3},
            {"kind": "MACD"},
            {"kind": "SUPERTREND"},
        ],
        times,
    )
    by_name = {payload["name"]: payload for payload in payloads}

    expected_names = {
        "SMA(3)",
        "EMA(3)",
        "RSI(3)",
        "ATR(3)",
        "BB Upper (3)",
        "BB Mid (3)",
        "BB Lower (3)",
        "MACD",
        "Signal",
        "SuperTrend",
    }
    assert expected_names <= set(by_name)
    assert by_name["SMA(3)"]["pane"] == "overlay"
    assert by_name["EMA(3)"]["pane"] == "overlay"
    assert by_name["RSI(3)"]["pane"] == "panel"
    assert by_name["ATR(3)"]["pane"] == "panel"
    assert by_name["BB Upper (3)"]["pane"] == "overlay"
    assert by_name["MACD"]["pane"] == "panel"
    assert by_name["Signal"]["pane"] == "panel"
    assert by_name["SuperTrend"]["pane"] == "overlay"

    for payload in payloads:
        assert payload["data"], payload["name"]
        for point in payload["data"]:
            assert math.isfinite(point["value"]), (payload["name"], point)

    time_to_index = _time_to_index(df)

    def first_index(name: str) -> int:
        return time_to_index[round(by_name[name]["data"][0]["time"], 6)]

    # Warm-up bars stay NaN in shared.indicators and _indicator_to_points
    # skips them: SMA/EMA/BB/ATR(3) start at bar 2, MACD(12,26,9) at bar 25,
    # its signal at bar 33, SuperTrend(7) at bar 7.
    assert first_index("SMA(3)") == 2
    assert first_index("EMA(3)") == 2
    assert first_index("BB Upper (3)") == 2
    assert first_index("ATR(3)") == 2
    assert first_index("MACD") == 25
    assert first_index("Signal") == 33
    assert first_index("SuperTrend") == 7
    assert first_index("RSI(3)") > 0


def test_compute_supertrend_series_uses_active_band_and_ready_mask():
    from shared.indicators import presets as indicator_presets
    from viewer import core

    df = _ohlcv_frame()
    time_set = {ts.timestamp() for ts in df["timestamp"]}
    result = indicator_presets.supertrend_chart(
        df["high"], df["low"], df["close"], 7, 3.0
    )
    expected_line = (
        result.lower_band.where(result.trend == 1, result.upper_band)
        .where(result.ready)
    )

    payloads = core._compute_indicator_series(df, "SUPERTREND", None, time_set)
    assert len(payloads) == 1
    label, data, pane, _color, _style = payloads[0]
    assert label == "SuperTrend"
    assert pane == "overlay"
    assert data == core._indicator_to_points(df, expected_line, time_set)

    ready_times = {
        ts.timestamp() for ts, ready in zip(df["timestamp"], result.ready, strict=False) if ready
    }
    assert data
    assert all(point["time"] in ready_times for point in data)

    ready_positions = [
        i for i, ready in enumerate(result.ready) if bool(ready)
    ]
    up_index = next(
        i for i in ready_positions if result.trend.iloc[i] == 1
    )
    down_index = next(
        i for i in ready_positions if result.trend.iloc[i] == -1
    )
    points_by_time = {point["time"]: point["value"] for point in data}
    assert points_by_time[df["timestamp"].iloc[up_index].timestamp()] == round(
        float(result.lower_band.iloc[up_index]), 6
    )
    assert points_by_time[df["timestamp"].iloc[down_index].timestamp()] == round(
        float(result.upper_band.iloc[down_index]), 6
    )

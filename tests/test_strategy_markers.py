"""Unit tests for shared.strategy_markers (issue #264).

The helper must reproduce the exact marker dicts the per-strategy tails
emitted: same keys in the same order, UTC ISO times, per-group caps, and an
empty list when the frame has no ``timestamp`` column.
"""

from __future__ import annotations

import pandas as pd

from shared.strategy_markers import entry_exit_markers, signal_markers


def _df(rows: int = 10) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=rows, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": idx,
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
        }
    )


def test_entry_exit_markers_shape_and_order():
    df = _df()
    entries = pd.Series([True] * 3 + [False] * 7, index=df.index)
    exits = pd.Series([False] * 4 + [True] * 3 + [False] * 3, index=df.index)

    drawings = entry_exit_markers(df, entries, exits)

    assert drawings == [
        {
            "type": "marker",
            "time": "2024-01-01T00:00:00+00:00",
            "position": "belowBar",
            "shape": "arrowUp",
            "color": "#22c55e",
            "text": "Entry",
        },
        {
            "type": "marker",
            "time": "2024-01-01T01:00:00+00:00",
            "position": "belowBar",
            "shape": "arrowUp",
            "color": "#22c55e",
            "text": "Entry",
        },
        {
            "type": "marker",
            "time": "2024-01-01T02:00:00+00:00",
            "position": "belowBar",
            "shape": "arrowUp",
            "color": "#22c55e",
            "text": "Entry",
        },
        {
            "type": "marker",
            "time": "2024-01-01T04:00:00+00:00",
            "position": "aboveBar",
            "shape": "arrowDown",
            "color": "#ef4444",
            "text": "Exit",
        },
        {
            "type": "marker",
            "time": "2024-01-01T05:00:00+00:00",
            "position": "aboveBar",
            "shape": "arrowDown",
            "color": "#ef4444",
            "text": "Exit",
        },
        {
            "type": "marker",
            "time": "2024-01-01T06:00:00+00:00",
            "position": "aboveBar",
            "shape": "arrowDown",
            "color": "#ef4444",
            "text": "Exit",
        },
    ]


def test_entry_exit_markers_respects_limit_and_text():
    df = _df(50)
    entries = pd.Series(True, index=df.index)
    exits = pd.Series(False, index=df.index)

    drawings = entry_exit_markers(df, entries, exits, entry_text="Long", limit=5)

    assert len(drawings) == 5
    assert all(d["text"] == "Long" for d in drawings)
    assert drawings[-1]["time"] == "2024-01-01T04:00:00+00:00"


def test_signal_markers_multiple_groups_preserve_order():
    df = _df()
    mask = pd.Series([True, False] * 5, index=df.index)

    drawings = signal_markers(
        df,
        [
            (mask, "belowBar", "triangleUp", "#22c55e", "Long Signal", 30),
            (mask, "aboveBar", "triangleDown", "#ef4444", "Short Signal", 2),
        ],
    )

    assert [d["text"] for d in drawings] == ["Long Signal"] * 5 + ["Short Signal"] * 2
    assert drawings[5]["shape"] == "triangleDown"


def test_signal_markers_empty_without_timestamp_column():
    df = _df().drop(columns=["timestamp"])
    mask = pd.Series(True, index=df.index)

    assert signal_markers(df, [(mask, "belowBar", "arrowUp", "#fff", "x", 30)]) == []
    assert entry_exit_markers(df, mask, mask) == []


def test_signal_markers_drops_unparseable_timestamps():
    df = _df(4)
    df["timestamp"] = df["timestamp"].astype(str)
    df.loc[df.index[1], "timestamp"] = "not-a-date"
    mask = pd.Series(True, index=df.index)

    drawings = signal_markers(
        df, [(mask, "belowBar", "arrowUp", "#fff", "x", 30)]
    )

    assert [d["time"] for d in drawings] == [
        "2024-01-01T00:00:00+00:00",
        "2024-01-01T02:00:00+00:00",
        "2024-01-01T03:00:00+00:00",
    ]


def test_signal_markers_accepts_non_utc_input():
    df = _df(2)
    df["timestamp"] = pd.to_datetime(["2024-01-01 09:30", "2024-01-01 16:00"])
    mask = pd.Series(True, index=df.index)

    drawings = signal_markers(
        df, [(mask, "belowBar", "arrowUp", "#fff", "x", 30)]
    )

    assert drawings[0]["time"] == "2024-01-01T09:30:00+00:00"

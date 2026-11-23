"""Deterministic frames for the differential real-data slice contract.

The differential suite (``tests/test_differential_reference_strategies.py``)
uses one real EURUSD 15m slice that is *downloaded*, not committed. The tests
that govern that download must therefore be able to build a file which is valid
by the loader's rules without going to the network, and must be able to break
each rule on demand. This module is the single place that happens, so the
production contract (``data_tools/differential_slice.py``) is the only thing the
tests have to agree with.

Nothing here is random, and nothing here ever reaches the network: these are
frames for a contract, not quotes.
"""

from __future__ import annotations

import pandas as pd

from data_tools.differential_slice import (
    EXPECTED_SLICE_ROWS,
    slice_csv_text,
    slice_grid,
)

#: A EURUSD-like starting price; the walk is deterministic from here.
START_PRICE = 1.18
#: Up/down step of the walk, in price units (large enough to stay visible at 5dp).
STEP = 0.0004


def slice_like_frame(*, rows: int = EXPECTED_SLICE_ROWS) -> pd.DataFrame:
    """A frame that satisfies every rule :func:`validate_slice_frame` enforces.

    Stamped on the slice's own 15m trading-day grid, so ``rows`` defaults to
    exactly the slice's row count and a truncated variant is one argument away.
    """
    grid = slice_grid() if rows == EXPECTED_SLICE_ROWS else pd.date_range(
        start=slice_grid()[0], periods=rows, freq="15min", tz="UTC"
    )
    closes: list[float] = []
    price = START_PRICE
    for index in range(len(grid)):
        if index:
            price += STEP if index % 7 < 4 else -STEP
        closes.append(round(price, 5))
    opens = closes
    highs = [round(c + STEP, 5) for c in closes]
    lows = [round(c - STEP, 5) for c in closes]
    volumes = [float(100 + index) for index in range(len(grid))]
    return pd.DataFrame(
        {
            "timestamp": grid,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    )


def violations() -> dict[str, object]:
    """Named mutations that each break exactly one slice rule.

    Every value is a ``(frame, message_fragment)`` pair: the frame to load and
    the text the loader's error must contain, so a passing test proves the rule
    fired *and* that it named what it found.
    """
    def _truncated() -> tuple[pd.DataFrame, str]:
        return slice_like_frame(rows=EXPECTED_SLICE_ROWS - 1), "599 rows, expected 600"

    def _not_increasing() -> tuple[pd.DataFrame, str]:
        frame = slice_like_frame()
        stamps = frame["timestamp"].to_numpy(copy=True)
        stamps[3], stamps[4] = stamps[4], stamps[3]
        frame["timestamp"] = pd.to_datetime(pd.Series(stamps), utc=True)
        return frame, "not strictly increasing"

    def _missing_stamp() -> tuple[pd.DataFrame, str]:
        # NaT, not a string: a file written with an empty timestamp field reads back
        # as NaT, which is exactly what a truncated or hand-edited slice looks like.
        frame = slice_like_frame()
        frame.loc[2, "timestamp"] = pd.NaT
        return frame, "not parseable"

    def _nan_high() -> tuple[pd.DataFrame, str]:
        frame = slice_like_frame()
        frame.loc[5, "high"] = float("nan")
        return frame, "non-finite OHLC"

    def _high_below_body() -> tuple[pd.DataFrame, str]:
        frame = slice_like_frame()
        frame.loc[6, "high"] = frame.loc[6, ["open", "close"]].min() - 0.001
        return frame, "high < max(open, close)"

    def _low_above_body() -> tuple[pd.DataFrame, str]:
        frame = slice_like_frame()
        frame.loc[7, "low"] = frame.loc[7, ["open", "close"]].max() + 0.001
        return frame, "low > min(open, close)"

    def _high_inside_body() -> tuple[pd.DataFrame, str]:
        # A bar with a real body (open < close): its high sits between min and max of
        # open/close, so only the max-based containment rule can reject it.
        frame = slice_like_frame()
        close = float(frame.loc[6, "close"])
        frame.loc[6, ["open", "high", "low"]] = [
            round(close - 0.0008, 5),
            round(close - 0.0004, 5),
            round(close - 0.0012, 5),
        ]
        return frame, "high < max(open, close)"

    def _duplicate_stamp() -> tuple[pd.DataFrame, str]:
        # Non-decreasing but not strictly increasing: only the duplicate check fires.
        frame = slice_like_frame()
        frame.loc[5, "timestamp"] = frame.loc[4, "timestamp"]
        return frame, "not strictly increasing"

    def _inf_high() -> tuple[pd.DataFrame, str]:
        # A corrupted-but-parseable cache file: "inf" reads back as a float, passes notna().
        frame = slice_like_frame()
        frame.loc[9, "high"] = float("inf")
        return frame, "non-finite OHLC"

    def _nan_volume() -> tuple[pd.DataFrame, str]:
        frame = slice_like_frame()
        frame.loc[10, "volume"] = float("nan")
        return frame, "volume must be finite"

    def _inf_volume() -> tuple[pd.DataFrame, str]:
        frame = slice_like_frame()
        frame.loc[11, "volume"] = float("inf")
        return frame, "volume must be finite"

    def _missing_column() -> tuple[pd.DataFrame, str]:
        frame = slice_like_frame()
        return frame.drop(columns=["volume"]), "missing columns"

    def _zero_close() -> tuple[pd.DataFrame, str]:
        frame = slice_like_frame()
        frame.loc[8, "close"] = 0.0
        return frame, "non-positive prices"

    return {
        "truncated": _truncated(),
        "not_increasing": _not_increasing(),
        "missing_stamp": _missing_stamp(),
        "nan_high": _nan_high(),
        "high_below_body": _high_below_body(),
        "high_inside_body": _high_inside_body(),
        "low_above_body": _low_above_body(),
        "duplicate_stamp": _duplicate_stamp(),
        "inf_high": _inf_high(),
        "nan_volume": _nan_volume(),
        "inf_volume": _inf_volume(),
        "missing_column": _missing_column(),
        "zero_close": _zero_close(),
    }


def write_slice_like(path, frame: pd.DataFrame | None = None):
    """Write a slice-shaped CSV to ``path`` (the default is a valid one)."""
    payload = slice_like_frame() if frame is None else frame
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(slice_csv_text(payload), encoding="utf-8")
    return path

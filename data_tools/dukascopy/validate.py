"""Pre-publish validation for Dukascopy dataset frames.

A failed validation must publish nothing: callers run :func:`validate_frame`
on every frame *before* any file is staged, and the pipeline aborts the whole
run on the first failure.

The published format is an outer join of the BID and ASK sides, so a row may
legitimately carry a whole side as NaN (a minute/bin where only one side
traded — the real dataset has a handful, e.g. SP500 M1). :func:`validate_frame`
therefore tolerates exactly-one-side-NaN rows and returns their count;
partial-side NaN and rows where *both* sides are NaN still fail.
"""

from __future__ import annotations

import pandas as pd

from data_tools.dukascopy.convert import OHLCV


class DatasetValidationError(RuntimeError):
    """A frame failed the dataset contract; nothing may be published."""


def _side_nan_masks(
    df: pd.DataFrame,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """``(bid_all_nan, ask_all_nan, partial_side_nan, both_sides_nan)`` masks."""
    side_cols = {side: [f"{side}_{c}" for c in OHLCV] for side in ("bid", "ask")}
    nan_counts = {
        side: df[cols].isna().sum(axis=1) for side, cols in side_cols.items()
    }
    bid_nan = nan_counts["bid"] == len(OHLCV)
    ask_nan = nan_counts["ask"] == len(OHLCV)
    partial = ((nan_counts["bid"] > 0) & ~bid_nan) | (
        (nan_counts["ask"] > 0) & ~ask_nan
    )
    return bid_nan, ask_nan, partial, bid_nan & ask_nan


def validate_frame(df: pd.DataFrame, *, context: str) -> int:
    """Enforce the published-frame contract on ``df``.

    Checks: ``timestamp`` present and timezone-aware UTC; strictly increasing
    unique timestamps; NaN only as a *complete* side (all five ``bid_*`` or
    ``ask_*`` columns together) — partial-side NaN and both-sides-NaN rows
    fail; ``ask_* >= bid_*`` on every price column where both sides exist;
    and ``low <= open, close <= high`` on each present side.

    Returns the number of one-sided rows tolerated.
    """
    if "timestamp" not in df.columns:
        raise DatasetValidationError(f"{context}: missing 'timestamp' column")
    ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    if ts.isna().any():
        raise DatasetValidationError(f"{context}: {int(ts.isna().sum())} unparsable timestamps")
    if not df["timestamp"].is_monotonic_increasing or df["timestamp"].duplicated().any():
        raise DatasetValidationError(f"{context}: timestamps not strictly increasing/unique")

    for side in ("bid", "ask"):
        for c in OHLCV:
            col = f"{side}_{c}"
            if col not in df.columns:
                raise DatasetValidationError(f"{context}: missing column {col}")

    bid_nan, ask_nan, partial, both_nan = _side_nan_masks(df)
    if partial.any() or both_nan.any():
        detail = []
        if partial.any():
            detail.append(f"partial-side NaN in {int(partial.sum())} rows")
        if both_nan.any():
            detail.append(f"both-sides NaN in {int(both_nan.sum())} rows")
        raise DatasetValidationError(f"{context}: {'; '.join(detail)}")
    one_sided = int((bid_nan ^ ask_nan).sum())

    both = ~(bid_nan | ask_nan)
    for c in ("open", "high", "low", "close"):
        if ((df[f"ask_{c}"] < df[f"bid_{c}"]) & both).any():
            raise DatasetValidationError(f"{context}: ask_{c} < bid_{c}")

    for side, mask in (("bid", ~bid_nan), ("ask", ~ask_nan)):
        o, hi, lo, c = (df[f"{side}_{k}"] for k in ("open", "high", "low", "close"))
        bad = ((hi < o) | (hi < c) | (lo > o) | (lo > c)) & mask
        if bad.any():
            raise DatasetValidationError(
                f"{context}: {side} OHLC inconsistent "
                "(high<open/close or low>open/close)"
            )
    return one_sided

"""Pre-publish validation for Dukascopy dataset frames.

A failed validation must publish nothing: callers run :func:`validate_frame`
on every frame *before* any file is staged, and the pipeline aborts the whole
run on the first failure.
"""

from __future__ import annotations

import pandas as pd

from data_tools.dukascopy.convert import M1_COLUMNS


class DatasetValidationError(RuntimeError):
    """A frame failed the dataset contract; nothing may be published."""


def validate_frame(df: pd.DataFrame, *, context: str) -> None:
    """Enforce the published-frame contract on ``df``.

    Checks: ``timestamp`` present and timezone-aware UTC; strictly increasing
    unique timestamps; no NaN in any column; ``ask_* >= bid_*`` on every price
    column; and ``low <= open, close <= high`` on both sides.
    """
    if "timestamp" not in df.columns:
        raise DatasetValidationError(f"{context}: missing 'timestamp' column")
    ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    if ts.isna().any():
        raise DatasetValidationError(f"{context}: {int(ts.isna().sum())} unparsable timestamps")
    if not df["timestamp"].is_monotonic_increasing or df["timestamp"].duplicated().any():
        raise DatasetValidationError(f"{context}: timestamps not strictly increasing/unique")

    value_cols = [c for c in df.columns if c != "timestamp"]
    if df[value_cols].isna().any().any():
        nan_cols = [c for c in value_cols if df[c].isna().any()]
        raise DatasetValidationError(f"{context}: NaN in columns {nan_cols}")

    for side in ("bid", "ask"):
        for c in ("open", "high", "low", "close"):
            col = f"{side}_{c}"
            if col not in df.columns:
                raise DatasetValidationError(f"{context}: missing column {col}")
    for c in ("open", "high", "low", "close"):
        if (df[f"ask_{c}"] < df[f"bid_{c}"]).any():
            raise DatasetValidationError(f"{context}: ask_{c} < bid_{c}")

    for side in ("bid", "ask"):
        o, hi, lo, c = (df[f"{side}_{k}"] for k in ("open", "high", "low", "close"))
        if ((hi < o) | (hi < c) | (lo > o) | (lo > c)).any():
            raise DatasetValidationError(
                f"{context}: {side} OHLC inconsistent "
                "(high<open/close or low>open/close)"
            )


def validate_dataset_frames(
    frames: dict[str, pd.DataFrame], *, symbol: str
) -> None:
    """Validate every timeframe frame for one symbol."""
    for tf, df in frames.items():
        validate_frame(df, context=f"{symbol} {tf}")


def expected_columns() -> list[str]:
    return list(M1_COLUMNS)

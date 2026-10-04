"""
Resample Tool — Generate higher timeframes from lower ones.

Given M1 (or any source timeframe) data, generate M5, M15, M30, H1, H4, D1.
OHLCV rules: Open=first, High=max, Low=min, Close=last, Volume=sum.

Usage:
    python -m data_tools.resample --source ~/data/ohlcv/dukascopy/forex/EURUSD/1h.parquet \
        --timeframes 4h,1d
    python -m data_tools.resample --source ~/data/ohlcv/dukascopy/forex/EURUSD/1m.parquet \
        --timeframes 5m,15m,30m,1h,4h,1d
    python -m data_tools.resample --source-dir ~/data/ohlcv/dukascopy/forex/ \
        --from 1h --timeframes 4h,1d
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd

from data_tools.path_safety import (
    build_dataset_path,
    ensure_dataset_path_within,
    validate_dataset_component,
)
from shared.atomic_io import write_parquet_atomically

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

# Map our timeframe names to pandas offset aliases
TIMEFRAME_MAP = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "4h": "4h",
    "1d": "1D",
    # `W-MON`, not `1W`. They are the same frequency but not the same *labelling*: `1W` is
    # W-SUN **right-closed**, so pandas labels a weekly bar with the Sunday its week *ends* on.
    # `shared/market_calendar.py` states the rule this repo works to - "A bar's timestamp is the
    # *start* of the interval it summarises" - and says a weekly bar is "stamped on Monday".
    # `live_runner/signal_sources/execution_timing.py::_next_candle_close` anchors weeks on
    # Monday for the same reason. `1W` was the single outlier on all three counts.
    "1w": "W-MON",
}

# Ordering for validation (lower index = smaller timeframe)
TIMEFRAME_ORDER = ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]


def _sum_preserving_missing(values: pd.Series) -> float:
    total = values.sum(min_count=1)
    return float(total) if pd.notna(total) else float("nan")


def _aggregation_for(columns: list[str]) -> dict[str, str | object]:
    """OHLCV aggregation with retention for extra columns (C5).

    Spread / bid_* / ask_* OHLCV siblings aggregate like their plain
    counterparts (high→max, low→min, open→first, close→last, volume→sum),
    so resampled bid/ask ranges stay true ranges instead of last-ticks.
    Other numeric extras ride along with "last"; non-numeric extras keep
    "first". ``zero_volume_bar`` is excluded here and recomputed from the
    merged volume after resampling (a merged bucket is zero-volume only
    when every sub-bucket was).
    """
    agg: dict[str, str | object] = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": _sum_preserving_missing,
    }
    for column in columns:
        if column in agg or column == "timestamp" or column == "zero_volume_bar":
            continue
        lowered = column.lower()
        if lowered.endswith(("_high", ".high")):
            agg[column] = "max"
        elif lowered.endswith(("_low", ".low")):
            agg[column] = "min"
        elif lowered.endswith(("_open", ".open")):
            agg[column] = "first"
        elif lowered.endswith(("_close", ".close")):
            agg[column] = "last"
        elif "volume" in lowered or lowered.endswith("_vol"):
            agg[column] = "sum"
        elif lowered == "spread":
            # Current spread, not the bucket's first tick.
            agg[column] = "last"
        else:
            agg[column] = "first"
    return agg


def resample_with_origin(
    df: pd.DataFrame,
    rule: str,
    *,
    origin: str = "start_day",
    timestamp_col: str = "timestamp",
) -> pd.DataFrame:
    """Shared resample helper with explicit bucket origin (C5).

    Buckets align to UTC via ``origin`` (default ``"start_day"``) instead of
    pandas defaults; empty buckets are dropped LOUDLY (count-logged, so gaps
    stay visible instead of vanishing); extra columns (spread, bid_*/ask_*)
    are retained via :func:`_aggregation_for`.
    """
    frame = df.copy()
    frame[timestamp_col] = pd.to_datetime(frame[timestamp_col], utc=True)
    frame = frame.set_index(timestamp_col).sort_index()
    had_zero_volume_flag = "zero_volume_bar" in frame.columns
    # `label`/`closed` are pinned rather than left to pandas, because the defaults are *per
    # frequency*: intraday and daily offsets default to left/left, while weekly, monthly, quarterly
    # and yearly default to right/right. Left alone, this one function labelled daily bars by
    # their first day and weekly bars by their last - so a daily bar and a weekly bar in the same
    # frame could not be read with the same expression, against the documented rule above.
    #
    # Measured: forcing left/left is bit-identical for 1min/5min/15min/30min/1h/4h/1D. Only `1w`
    # changes, which is the point.
    resampled = frame.resample(rule, origin=origin, label="left", closed="left").agg(
        _aggregation_for([str(column) for column in frame.columns])
    )
    if had_zero_volume_flag:
        if "volume" in resampled.columns:
            # Recompute from merged volume: zero only when every
            # sub-bucket was zero (CodeRabbit round).
            resampled["zero_volume_bar"] = (
                pd.to_numeric(resampled["volume"], errors="coerce").fillna(0.0) <= 0.0
            )
        else:
            resampled["zero_volume_bar"] = (
                frame[["zero_volume_bar"]]
                .resample(rule, origin=origin, label="left", closed="left")
                .min()
            )
    empty_buckets = int(resampled["open"].isna().sum()) if "open" in resampled else 0
    if empty_buckets:
        logger.warning(
            "Resample %s dropped %d empty bucket(s); gaps stay visible in coverage checks",
            rule,
            empty_buckets,
        )
    return resampled.dropna(subset=["open"]).reset_index()


def resample_ohlcv(df: pd.DataFrame, target_tf: str) -> pd.DataFrame:
    """Resample OHLCV DataFrame to a higher timeframe.

    Args:
        df: DataFrame with 'timestamp', 'open', 'high', 'low', 'close', 'volume' columns.
        target_tf: Target timeframe (e.g., '5m', '1h', '4h', '1d').

    Returns:
        Resampled DataFrame.
    """
    if target_tf not in TIMEFRAME_MAP:
        raise ValueError(f"Unknown timeframe '{target_tf}'. Use: {list(TIMEFRAME_MAP.keys())}")

    return resample_with_origin(df, TIMEFRAME_MAP[target_tf])


def merge_resampled_history(existing_path: Path, resampled: pd.DataFrame) -> pd.DataFrame:
    """Merge freshly resampled bars with the stored higher-timeframe history.

    Resampling only sees the rows of the source file it was given, so writing
    the result directly over an existing {tf}.parquet would silently drop any
    older history that is no longer present in the source. Merging keeps the
    union of both, with the newer resampled values winning on duplicate
    timestamps (DV-B23).
    """
    try:
        existing = pd.read_parquet(existing_path)
    except (OSError, ValueError) as exc:
        logger.warning("Could not read existing resample target %s: %s", existing_path, exc)
        return resampled

    if existing.empty:
        return resampled

    existing = existing.copy()
    existing.columns = [str(column).lower() for column in existing.columns]
    if "timestamp" not in existing.columns:
        return resampled

    existing["timestamp"] = pd.to_datetime(existing["timestamp"], utc=True, errors="coerce")
    existing = existing.dropna(subset=["timestamp"])

    combined = (
        pd.concat([existing, resampled], ignore_index=True, sort=False)
        .sort_values("timestamp")
        .drop_duplicates(subset=["timestamp"], keep="last")
        .reset_index(drop=True)
    )
    return combined


def resample_file(
    source_path: Path, timeframes: list[str], output_dir: Path | None = None
) -> list[Path]:
    """Resample a single parquet file to multiple timeframes.

    Args:
        source_path: Path to source OHLCV parquet file.
        timeframes: List of target timeframes (e.g., ['5m', '15m', '1h']).
        output_dir: Output directory (default: same directory as source).

    Returns:
        List of output file paths.
    """
    df = pd.read_parquet(source_path)

    # Normalize columns
    df.columns = [c.lower() for c in df.columns]
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns: {missing}")

    out_dir = output_dir or source_path.parent
    results = []

    for tf in timeframes:
        out_path = build_dataset_path(out_dir, f"{tf}.parquet")
        resampled = resample_ohlcv(df, tf)
        if out_path.exists():
            resampled = merge_resampled_history(out_path, resampled)
        write_parquet_atomically(out_path, resampled)
        logger.info("  %s: %d → %d rows → %s", tf, len(df), len(resampled), out_path)
        results.append(out_path)

    return results


def _path_is_contained(root: Path, path: Path) -> bool:
    try:
        ensure_dataset_path_within(root, path)
    except ValueError:
        return False
    return True


def resample_directory(source_dir: Path, from_tf: str, timeframes: list[str]) -> list[Path]:
    """Resample all parquet files in a directory tree.

    Finds all {from_tf}.parquet files under source_dir and resamples each.
    """
    validate_dataset_component(from_tf, name="source timeframe")
    for timeframe in timeframes:
        validate_dataset_component(timeframe, name="target timeframe")
    source_files = sorted(
        path
        for path in source_dir.rglob(f"{from_tf}.parquet")
        if _path_is_contained(source_dir, path)
    )

    if not source_files:
        logger.warning("No %s.parquet files found under %s", from_tf, source_dir)
        return []

    all_results = []
    for src in source_files:
        symbol = src.parent.name
        logger.info("Resampling %s (%s)...", symbol, src)
        try:
            results = resample_file(src, timeframes)
            all_results.extend(results)
        except Exception:
            logger.exception("  Resampling %s failed", symbol)

    return all_results


def update_catalog(data_dir: Path | None = None):
    """Rebuild the catalog after resampling."""
    try:
        from data_tools.catalog_builder import build_catalog
        from shared.constants import DATA_DIR as _DEFAULT_DATA_DIR

        root = data_dir or _DEFAULT_DATA_DIR
        catalog = build_catalog(root)
        from data_tools.catalog_builder import write_catalog_json

        write_catalog_json(root / "catalog.json", catalog)
        logger.info("Catalog updated: %d datasets", catalog["summary"]["total_datasets"])
    except Exception as e:
        logger.warning("Could not update catalog: %s", e)


def main():
    parser = argparse.ArgumentParser(description="Resample OHLCV data to higher timeframes")
    parser.add_argument("--source", type=str, help="Source parquet file path")
    parser.add_argument(
        "--source-dir", type=str, help="Source directory (resample all matching files)"
    )
    parser.add_argument(
        "--from", dest="from_tf", default="1m", help="Source timeframe (for --source-dir)"
    )
    parser.add_argument(
        "--timeframes",
        required=True,
        help="Comma-separated target timeframes (5m,15m,30m,1h,4h,1d)",
    )
    parser.add_argument("--output", type=str, help="Output directory (default: next to source)")
    parser.add_argument(
        "--update-catalog", action="store_true", help="Update catalog.json after resampling"
    )
    args = parser.parse_args()

    timeframes = [t.strip() for t in args.timeframes.split(",")]

    if args.source:
        source = Path(args.source)
        if not source.exists():
            parser.error(f"Source file not found: {source}")
        output_dir = Path(args.output) if args.output else None
        logger.info("Resampling %s → %s", source, timeframes)
        resample_file(source, timeframes, output_dir)

    elif args.source_dir:
        source_dir = Path(args.source_dir)
        if not source_dir.exists():
            parser.error(f"Source directory not found: {source_dir}")
        logger.info("Resampling all %s files under %s → %s", args.from_tf, source_dir, timeframes)
        resample_directory(source_dir, args.from_tf, timeframes)

    else:
        parser.error("Either --source or --source-dir required")

    if args.update_catalog:
        update_catalog()


if __name__ == "__main__":
    main()

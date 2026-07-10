"""
Resample Tool — Generate higher timeframes from lower ones.

Given M1 (or any source timeframe) data, generate M5, M15, M30, H1, H4, D1.
OHLCV rules: Open=first, High=max, Low=min, Close=last, Volume=sum.

Usage:
    python -m data_tools.resample --source ~/data/ohlcv/dukascopy/forex/EURUSD/1h.parquet --timeframes 4h,1d
    python -m data_tools.resample --source ~/data/ohlcv/dukascopy/forex/EURUSD/1m.parquet --timeframes 5m,15m,30m,1h,4h,1d
    python -m data_tools.resample --source-dir ~/data/ohlcv/dukascopy/forex/ --from 1h --timeframes 4h,1d
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pandas as pd

from shared.constants import normalize_timeframe

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
    "1w": "1W",
}

# Ordering for validation (lower index = smaller timeframe)
TIMEFRAME_ORDER = ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]


def resample_ohlcv(df: pd.DataFrame, target_tf: str) -> pd.DataFrame:
    """Resample OHLCV DataFrame to a higher timeframe.

    Args:
        df: DataFrame with 'timestamp', 'open', 'high', 'low', 'close', 'volume' columns.
        target_tf: Target timeframe (e.g., '5m', '1h', '4h', '1d').

    Returns:
        Resampled DataFrame.
    """
    target_tf = normalize_timeframe(target_tf)
    if target_tf not in TIMEFRAME_MAP:
        raise ValueError(f"Unknown timeframe '{target_tf}'. Use: {list(TIMEFRAME_MAP.keys())}")

    pandas_freq = TIMEFRAME_MAP[target_tf]

    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp").sort_index()

    resampled = (
        df.resample(pandas_freq)
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        .dropna(subset=["open"])
    )

    resampled = resampled.reset_index()
    return resampled


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
    source_tf = normalize_timeframe(source_path.stem)
    if source_tf not in TIMEFRAME_ORDER:
        raise ValueError(f"Unknown source timeframe '{source_path.stem}'")

    # Normalize columns
    df.columns = [c.lower() for c in df.columns]
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns: {missing}")

    out_dir = output_dir or source_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []

    for tf in timeframes:
        normalized_tf = normalize_timeframe(tf)
        if normalized_tf not in TIMEFRAME_ORDER:
            raise ValueError(f"Unknown timeframe '{tf}'. Use: {TIMEFRAME_ORDER}")
        if TIMEFRAME_ORDER.index(normalized_tf) <= TIMEFRAME_ORDER.index(source_tf):
            raise ValueError(
                f"Target timeframe '{normalized_tf}' must be higher than source '{source_tf}'"
            )
        out_path = out_dir / f"{normalized_tf}.parquet"
        resampled = resample_ohlcv(df, normalized_tf)
        resampled.to_parquet(out_path, index=False)
        logger.info("  %s: %d → %d rows → %s", normalized_tf, len(df), len(resampled), out_path)
        results.append(out_path)

    return results


def resample_directory(source_dir: Path, from_tf: str, timeframes: list[str]) -> list[Path]:
    """Resample all parquet files in a directory tree.

    Finds all {from_tf}.parquet files under source_dir and resamples each.
    """
    from_tf = normalize_timeframe(from_tf)
    pattern = f"**/{from_tf}.parquet"
    source_files = sorted(source_dir.rglob(f"{from_tf}.parquet"))

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
        except Exception as e:
            logger.error("  Failed: %s", e)

    return all_results


def update_catalog(data_dir: Path | None = None):
    """Rebuild the catalog after resampling."""
    try:
        from data_tools.catalog_builder import build_catalog
        from shared.constants import DATA_DIR as _DEFAULT_DATA_DIR

        root = data_dir or _DEFAULT_DATA_DIR
        catalog = build_catalog(root)
        catalog_path = root / "catalog.json"
        catalog_path.write_text(json.dumps(catalog, indent=2, default=str))
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
